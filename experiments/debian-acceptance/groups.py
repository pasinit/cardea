"""ADR-034 acceptance tests 3, 4, 5 and 10 on a real daemon, with real kills and restarts.

    python3 groups.py [--tests 3,4,5,10] [--work DIR] [--lease-seconds 300]

One daemon (`launcher.py`: the installed kernel's `serve()`, the development identity, the
scripted harness of `plan.py`) runs on the script's own store. Each test has its own
namespace. "Kill" is SIGKILL of the daemon process this script started; the restart
reopens the same store, and DBOS recovers its workflows.

- **Test 3, no double claim** (`t3`): twenty concurrent `records.claim` tasks from two
  agents have exactly one winner. The daemon is killed after the claim commits; after the
  restart there is still one holder at epoch 1. When the lease runs out the record is
  released with a new epoch; the old holder's write is refused with `claim_lost`, and the
  other agent claims it and writes.
- **Test 4, idle and failed holders** (`t4`): three members of a group each claim a record,
  and the daemon is killed and restarted. The idle, healthy member keeps its claim with the
  same epoch through follow-up wakes, gets no more than `max_followups` of them, and then
  loses the claim with `followups_exhausted` and mail to the supervisor. The member whose
  runs fail loses its claim within one lease and a tick. A lease below the apply-time
  bound is refused with `claim_lease_too_short`.
- **Test 5, message once across a restart** (`t5`): mail is sent and the daemon is killed
  before delivery. After the restart the member's run receives it and fails; the next run
  receives it again (`delivery: 2`) and succeeds; no later run sees it. Pool mail is
  consumed by exactly one member, group mail by every member, and mail is dead-lettered
  after `max_deliveries` with a notice to the sender.
- **Test 10, journal triggers exactly once** (`t10`): each new record wakes the member
  once, even when the daemon is killed at a random point of the minute tick, and a burst
  of records while the member is busy becomes at most one queued run and one follow-up.

The full run takes about 40 minutes with the default 300 s lease, mostly waiting for
leases and minute ticks.
"""

import argparse
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# `python -I` leaves the script's own directory off the path; the shared modules are there.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    Evidence,
    Processes,
    add_common_arguments,
    claim,
    rows,
    run_script,
    use_kernel,
    wait_for,
)
from plan import set_plan, transcript  # noqa: E402
from stack import Stack, agents, inspectable, people, platform, rights  # noqa: E402

NAME = "adr-034"
SUP = "sup"
RUN = ["objects.read", "objects.write", "models.invoke"]
SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}
WEEKLY = {"schedule": {"cron": "0 3 * * 1", "timezone": "UTC", "misfire": "skip"}}
TICK = 60


def collection(namespace: str, name: str, **spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": "cardea/v1alpha1",
        "kind": "Collection",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"supervisor": SUP, "schema": SCHEMA, **spec},
    }


def group(namespace: str, name: str, pool: dict[str, Any], **spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": "cardea/v1alpha1",
        "kind": "AgentGroup",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"supervisor": SUP, "pools": [pool], **spec},
    }


def pool(name: str, replicas: int, grants: list[str], operations: list[str], **run: Any) -> Any:
    triggers = run.pop("triggers")
    return {
        "name": name,
        "replicas": replicas,
        # The supervisor reads back the runs a pool starts (stack.INSPECT).
        "grants": inspectable([*RUN, *grants]),
        "run": {
            "instructions": "Play the acceptance plan.",
            "operations": operations,
            "bounds": {"attempts": 1, "timeout_seconds": 30, "run_timeout_seconds": 120},
            **run,
        },
        "triggers": triggers,
    }


def provisioning(lease: int) -> dict[str, Any]:
    from cardea.service.models import RIGHTS

    base = [*RIGHTS, "groups.manage", *RUN]
    rows_: list[Any] = []
    rows_ += rights(
        SUP,
        "t3",
        # groups.manage: applying the race collection needs it (authority_denied otherwise).
        [
            *RIGHTS,
            "groups.manage",
            *(f"record:race/{a}" for a in ("create", "read", "claim", "update")),
        ],
    )
    rows_ += rights(
        SUP, "t4", [*base, *(f"record:work/{a}" for a in ("create", "read", "claim", "update"))]
    )
    rows_ += rights(
        SUP,
        "t5",
        [
            *base,
            "mailbox.inbox",
            "runs.recent",
            "mail:post.m.0",
            "mail:post.m.1",
            "mail:post.m",
            "mail:post",
        ],
    )
    rows_ += rights(SUP, "t10", [*base, "runs.recent", "record:notes/create", "record:notes/read"])
    return {
        "principals": [*people("runtime", SUP), *agents("filer", "a1", "a2")],
        "permissions": rows_,
        "platform": platform(rows_),
    }


class Run:
    def __init__(self, stack: Stack, evidence: Evidence, lease: int) -> None:
        self.stack = stack
        self.evidence = evidence
        self.lease = lease

    @property
    def db(self) -> Path:
        return self.stack.database

    def done(self, task_id: str, timeout: float = 300) -> dict[str, Any]:
        return self.stack.finished(SUP, task_id, timeout)

    def create(self, namespace: str, name: str, key: str) -> str:
        task_id = self.stack.operation(
            SUP,
            "filer",
            namespace,
            "records.create",
            {"collection": name, "client_key": key, "data": {"title": key}},
            [f"record:{name}/create"],
        )
        self.done(task_id)
        output = self.stack.output(SUP, task_id)
        return str(output["record"]["record_id"])

    def record(self, namespace: str, record_id: str) -> dict[str, Any]:
        (found,) = rows(
            self.db,
            "SELECT holder, claim_epoch, lease_expires_at, followups, version FROM records "
            "WHERE namespace=? AND record_id=?",
            namespace,
            record_id,
        )
        return found

    def history(self, record_id: str) -> list[dict[str, Any]]:
        return rows(
            self.db,
            "SELECT kind, actor, version, at FROM record_events WHERE record_id=? "
            "ORDER BY event_id",
            record_id,
        )

    def member_requests(self, member: str) -> list[str]:
        return [
            r["request_id"]
            for r in rows(
                self.db,
                "SELECT request_id FROM task_bindings WHERE agent_id=? ORDER BY rowid",
                member,
            )
        ]

    def played(self, agent: str) -> list[dict[str, Any]]:
        return [t for t in transcript(self.stack.work) if t["agent_id"] == agent]

    # Test 3

    def test_3(self) -> None:
        ev, st = self.evidence, self.stack
        st.request(
            SUP,
            "PUT",
            "/collections/race",
            json=collection(
                "t3",
                "race",
                claims={
                    "lease_seconds": self.lease,
                    "max_per_holder": 1,
                    "max_followups": 1,
                    "standalone": True,
                },
            ),
        )
        record_id = self.create("t3", "race", "race-1")
        grants = ["record:race/claim", "record:race/read"]

        def submit(n: int) -> str:
            agent = "a1" if n % 2 else "a2"
            return st.operation(
                SUP,
                agent,
                "t3",
                "records.claim",
                {"collection": "race", "record_id": record_id},
                grants,
            )

        with ThreadPoolExecutor(max_workers=20) as pool_:
            tasks = list(pool_.map(submit, range(20)))
        states = [self.done(t)["task"]["state"] for t in tasks]
        claimed = [e for e in self.history(record_id) if e["kind"] == "claimed"]
        held = self.record("t3", record_id)
        ev.check(
            "t3_one_winner_of_twenty",
            len(claimed) == 1
            and held["claim_epoch"] == 1
            and held["holder"] == claimed[0]["actor"],
            states={s: states.count(s) for s in set(states)},
            holder=held["holder"],
        )
        winner = held["holder"]
        loser = "a2" if winner == "a1" else "a1"
        st.restart_after_kill()
        after = self.record("t3", record_id)
        ev.check(
            "t3_one_holder_after_restart",
            (after["holder"], after["claim_epoch"]) == (winner, 1),
            record=after,
        )
        wait_for(lambda: self.record("t3", record_id)["holder"] is None, self.lease + 3 * TICK, 5)
        released = self.record("t3", record_id)
        ev.check(
            "t3_lease_expiry_releases_with_new_epoch", released["claim_epoch"] >= 2, record=released
        )
        stale = st.operation(
            SUP,
            winner,
            "t3",
            "records.update",
            {
                "collection": "race",
                "record_id": record_id,
                "expected_version": released["version"],
                "claim_epoch": 1,
                "data": {"title": "stale"},
            },
            ["record:race/update"],
        )
        self.done(stale)
        code = st.failure(SUP, stale)
        reclaim = st.operation(
            SUP,
            loser,
            "t3",
            "records.claim",
            {"collection": "race", "record_id": record_id},
            grants,
        )
        self.done(reclaim)
        now_held = self.record("t3", record_id)
        write = st.operation(
            SUP,
            loser,
            "t3",
            "records.update",
            {
                "collection": "race",
                "record_id": record_id,
                "expected_version": now_held["version"],
                "claim_epoch": now_held["claim_epoch"],
                "data": {"title": "new"},
            },
            ["record:race/update"],
        )
        state = self.done(write)["task"]["state"]
        ev.check("t3_old_holder_gets_claim_lost", code == "claim_lost", code=code)
        ev.check(
            "t3_new_holder_proceeds",
            now_held["holder"] == loser and state == "succeeded",
            record=now_held,
            write=state,
        )

    # Test 4

    def test_4(self) -> None:
        ev, st = self.evidence, self.stack
        claims = {
            "lease_seconds": self.lease,
            "max_per_holder": 1,
            "max_followups": 2,
            # The members take their records with records.claim, which only a collection
            # with standalone claims accepts (claims_not_standalone otherwise).
            "standalone": True,
        }
        st.request(SUP, "PUT", "/collections/work", json=collection("t4", "work", claims=claims))
        grants = ["record:work/read", "record:work/claim"]
        members = pool("w", 2, grants, ["records.claim"], triggers=[WEEKLY])
        st.request(
            SUP, "PUT", "/groups/hold", json=group("t4", "hold", members, collections=["work"])
        )
        too_long = pool("x", 1, grants, ["records.claim"], triggers=[WEEKLY])
        # The apply-time bound is 2 x attempts x timeout + 240 s: far above any test lease.
        too_long["run"]["bounds"] = {
            "attempts": 3,
            "timeout_seconds": 3600,
            "run_timeout_seconds": 3600,
        }
        refused = st.attempt(
            SUP, "PUT", "/groups/slow", json=group("t4", "slow", too_long, collections=["work"])
        )
        ev.check(
            "t4_short_lease_refused_at_apply",
            refused.get("code") == "claim_lease_too_short",
            result=refused,
        )
        idle, failing = "hold.w.0", "hold.w.1"
        records = {m: self.create("t4", "work", f"{m}-item") for m in (idle, failing)}
        for member, record_id in records.items():
            take = {
                "calls": [
                    {
                        "tool": "records.claim",
                        "arguments": {"collection": "work", "record_id": record_id},
                    }
                ]
            }
            default: dict[str, Any] = {
                "calls": [],
                "outcome": "succeed" if member == idle else "fail",
            }
            set_plan(st.work, member, [take], default)
            run = st.agent_run(
                SUP,
                member,
                "t4",
                operations=["records.claim"],
                grants=[*RUN, *grants],
                attempt_timeout_seconds=30,
            )
            self.done(run)
        claimed = {m: self.record("t4", r) for m, r in records.items()}
        ev.check(
            "t4_both_members_hold_a_record",
            all(claimed[m]["holder"] == m for m in records),
            records=claimed,
        )
        st.restart_after_kill()
        started = time.monotonic()
        # The failing holder: its follow-ups fail and renew nothing.
        wait_for(
            lambda: self.record("t4", records[failing])["holder"] is None, self.lease + 3 * TICK, 5
        )
        ev.check(
            "t4_failed_holder_loses_claim_within_lease_and_tick",
            time.monotonic() - started <= self.lease + 2 * TICK,
            seconds=round(time.monotonic() - started),
            history=self.history(records[failing]),
        )
        # The idle holder: follow-ups succeed and keep the epoch, until they run out.
        epochs: list[int] = []

        def lapsed() -> bool:
            current = self.record("t4", records[idle])
            if current["holder"] == idle:
                epochs.append(int(current["claim_epoch"]))
            return current["holder"] is None

        wait_for(lapsed, 4 * self.lease + 4 * TICK, 10)
        prefix = f"{idle}.claim.{records[idle]}.1."
        followups = [r for r in self.member_requests(idle) if r.startswith(prefix)]
        ev.check(
            "t4_idle_holder_kept_epoch_through_follow_ups",
            bool(epochs) and set(epochs) == {1} and len(followups) >= 1,
            followups=followups,
        )
        ev.check("t4_follow_ups_stop_at_max_followups", len(followups) == 2, followups=followups)
        expired = rows(
            self.db,
            "SELECT payload FROM events WHERE event_type="
            "'cardea.record.claim_expired' AND payload LIKE ?",
            f"%{records[idle]}%",
        )
        # The lapse is a kernel message to the supervisor (claims.py), not a dead-letter
        # `notice`; its data part carries the reason.
        notices = rows(
            self.db,
            "SELECT kind, to_address FROM mailbox_messages WHERE namespace='t4' "
            "AND to_address=? AND parts_json LIKE '%followups_exhausted%'",
            SUP,
        )
        ev.check(
            "t4_lapse_is_followups_exhausted_with_supervisor_mail",
            any("followups_exhausted" in e["payload"] for e in expired) and bool(notices),
            notices=len(notices),
        )

    # Test 5

    def send(self, to: dict[str, str], key: str, **extra: Any) -> str:
        body = {
            "namespace": "t5",
            "client_key": key,
            "to": to,
            "parts": [{"text": f"acceptance message {key}"}],
            **extra,
        }
        return str(self.stack.request(SUP, "POST", "/mailbox", json=body)["message_id"])

    def deliveries(self, message_id: str) -> list[dict[str, Any]]:
        return rows(
            self.db,
            "SELECT recipient_id, pool, state, deliveries, task_id FROM "
            "mailbox_deliveries WHERE message_id=? ORDER BY delivery_id",
            message_id,
        )

    def seen(self, agent: str, key: str) -> list[int]:
        """The `delivery` numbers with which an agent's attempts were given message `key`."""
        found = []
        for played in self.played(agent):
            for entry in (played.get("inbox") or {}).get("messages", []):
                if any(
                    f"acceptance message {key}" == p.get("text") for p in entry.get("parts", [])
                ):
                    found.append(int(entry.get("delivery", 0)))
        return found

    def test_5(self) -> None:
        ev, st = self.evidence, self.stack
        members = pool(
            "m",
            2,
            ["mailbox.inbox", "runs.recent"],
            ["runs.recent"],
            context=[{"operation": "mailbox.inbox"}],
            triggers=[
                {
                    "event": {
                        "types": ["cardea.mailbox.sent"],
                        "subjects": ["$member", "$pool", "$group"],
                    }
                }
            ],
        )
        st.request(SUP, "PUT", "/groups/post", json=group("t5", "post", members))
        first, second = "post.m.0", "post.m.1"
        set_plan(st.work, first, [{"calls": [], "outcome": "fail"}])
        for attempt in range(3):
            key = f"once-{attempt}"
            message = self.send({"instance": first}, key)
            st.kill()
            pending = self.deliveries(message)
            if pending and pending[0]["deliveries"] == 0:
                break
            # The tick delivered it before the kill; try again with a new message.
            st.start()
            set_plan(st.work, first, [{"calls": [], "outcome": "fail"}])
        ev.check("t5_killed_before_delivery", pending[0]["deliveries"] == 0, delivery=pending)
        st.start()
        wait_for(lambda: self.seen(first, key), 4 * TICK, 5)
        wait_for(
            lambda: (
                self.deliveries(message)[0]["state"] == "pending"
                and self.deliveries(message)[0]["deliveries"] == 1
            ),
            2 * TICK,
            2,
        )
        self.send({"instance": first}, f"{key}-next")
        wait_for(lambda: self.deliveries(message)[0]["state"] == "consumed", 4 * TICK, 5)
        ev.check(
            "t5_failed_run_then_redelivered_once",
            self.seen(first, key) == [1, 2] and self.deliveries(message)[0]["deliveries"] == 2,
            seen=self.seen(first, key),
            delivery=self.deliveries(message),
        )
        later = self.send({"instance": first}, f"{key}-later")
        wait_for(lambda: self.deliveries(later)[0]["state"] == "consumed", 4 * TICK, 5)
        ev.check("t5_no_later_run_sees_it", self.seen(first, key) == [1, 2])

        pooled = self.send({"pool": "post.m"}, "pooled")
        wait_for(
            lambda: any(d["state"] == "consumed" for d in self.deliveries(pooled)), 4 * TICK, 5
        )
        time.sleep(2 * TICK)
        readers = [a for a in (first, second) if self.seen(a, "pooled")]
        ev.check(
            "t5_pool_mail_consumed_by_one_member",
            len(readers) == 1,
            readers=readers,
            deliveries=self.deliveries(pooled),
        )
        everyone = self.send({"group": "post"}, "everyone")
        wait_for(
            lambda: len([d for d in self.deliveries(everyone) if d["state"] == "consumed"]) == 2,
            4 * TICK,
            5,
        )
        ev.check(
            "t5_group_mail_delivered_to_every_member",
            all(self.seen(a, "everyone") == [1] for a in (first, second)),
        )

        set_plan(st.work, second, [{"calls": [], "outcome": "fail"}] * 2)
        doomed = self.send({"instance": second}, "doomed", max_deliveries=2)
        wait_for(lambda: self.seen(second, "doomed"), 4 * TICK, 5)
        wait_for(lambda: self.deliveries(doomed)[0]["state"] == "pending", 2 * TICK, 2)
        self.send({"instance": second}, "doomed-next")
        wait_for(lambda: self.deliveries(doomed)[0]["state"] == "dead", 4 * TICK, 5)
        dead = rows(
            self.db,
            "SELECT payload FROM events WHERE event_type='cardea.mailbox.dead' AND payload LIKE ?",
            f"%{doomed}%",
        )
        notices = rows(
            self.db,
            "SELECT to_address FROM mailbox_messages WHERE namespace='t5' AND kind='notice'",
        )
        ev.check(
            "t5_dead_lettered_after_max_deliveries_with_notice",
            bool(dead) and SUP in {n["to_address"] for n in notices},
            notices=notices,
            delivery=self.deliveries(doomed),
        )

    # Test 10

    def admitted(self, member: str) -> list[dict[str, Any]]:
        return rows(
            self.db,
            "SELECT event_id, event_type, task_id FROM trigger_deliveries "
            "WHERE trigger_id LIKE ? AND outcome='admitted' ORDER BY delivery_id",
            f"{member}.t%",
        )

    def _more_admitted(self, member: str, before: int) -> Any:
        return lambda: len(self.admitted(member)) > before

    def test_10(self) -> None:
        ev, st = self.evidence, self.stack
        st.request(SUP, "PUT", "/collections/notes", json=collection("t10", "notes"))
        member = "wake.r.0"
        readers = pool(
            "r",
            1,
            ["runs.recent"],
            ["runs.recent"],
            triggers=[{"event": {"types": ["cardea.record.created"]}}],
        )
        st.request(SUP, "PUT", "/groups/wake", json=group("t10", "wake", readers))
        rounds = 3
        offsets = []
        for n in range(rounds):
            before = len(self.admitted(member))
            self.create("t10", "notes", f"spaced-{n}")
            offset = random.uniform(0, TICK)
            offsets.append(round(offset, 1))
            time.sleep(offset)
            st.restart_after_kill()
            wait_for(self._more_admitted(member, before), 3 * TICK, 5)
        time.sleep(2 * TICK)
        admitted = self.admitted(member)
        by_event: dict[str, int] = {}
        for row in admitted:
            by_event[row["event_id"]] = by_event.get(row["event_id"], 0) + 1
        ev.check(
            "t10_each_event_wakes_once_across_kills",
            len(by_event) == rounds and set(by_event.values()) == {1},
            kill_offsets=offsets,
            admitted=admitted,
        )

        set_plan(st.work, member, [{"calls": [], "sleep": 3 * TICK / 2, "outcome": "succeed"}])
        before = len(self.member_requests(member))
        self.create("t10", "notes", "burst-start")
        wait_for(
            lambda: len(self.played(member)) > rounds or len(self.member_requests(member)) > before,
            3 * TICK,
            5,
        )
        for n in range(5):
            self.create("t10", "notes", f"burst-{n}")
        time.sleep(5 * TICK)
        runs = self.member_requests(member)[before:]
        duplicates = {
            k: v for k, v in _count(r["event_id"] for r in self.admitted(member)).items() if v > 1
        }
        ev.check(
            "t10_burst_coalesces",
            1 <= len(runs) <= 3 and not duplicates,
            runs=runs,
            duplicates=duplicates,
        )


def _count(values: Any) -> dict[Any, int]:
    counted: dict[Any, int] = {}
    for value in values:
        counted[value] = counted.get(value, 0) + 1
    return counted


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_common_arguments(parser, NAME)
    parser.add_argument("--tests", default="3,4,5,10", help="which tests (default: %(default)s)")
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=300,
        help="the claim lease, at least 300 (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    wanted = [t.strip() for t in args.tests.split(",") if t.strip()]
    if not set(wanted) <= {"3", "4", "5", "10"} or args.lease_seconds < 300:
        parser.error("--tests takes 3, 4, 5 and 10; --lease-seconds is at least 300")
    lib = use_kernel(args.cardea_lib)
    work = claim(args.work, NAME)
    evidence = Evidence(NAME, work, args.out)
    processes = Processes(work)

    def body() -> None:
        stack = Stack(work, processes, lib)
        stack.provision(provisioning(args.lease_seconds))
        stack.start()
        run = Run(stack, evidence, args.lease_seconds)
        for test in wanted:
            started = time.monotonic()
            try:
                getattr(run, f"test_{test}")()
            except Exception as error:  # noqa: BLE001 - one failed test must not hide the rest
                evidence.check(
                    f"t{test}_completed", False, error=f"{type(error).__name__}: {error}"
                )
                if stack.daemon is None:
                    stack.start()
            evidence.observe(f"t{test}_seconds", round(time.monotonic() - started))
        stack.stop()

    try:
        return run_script(evidence, body)
    finally:
        processes.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
