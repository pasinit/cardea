"""Upgrade an M5-schema store to the current schema head with the installed package.

    python3 upgrade.py --source SNAPSHOT [--work DIR] [--cardea /usr/bin/cardea]
    python3 upgrade.py --synthesize [--work DIR]

`--source` takes a `cardea backup` of an M5 store (schema `m5_001`), for example from the
M5 acceptance machine (experiments/m5). The script copies it into its own store, never
touching the original, and then runs the installed `cardea init` and `cardea verify` on
the copy. `--synthesize` builds an empty `m5_001` store with the installed package's own
migrations instead; it checks the schema path but has no rows to carry.

Checks, written to the evidence JSON:

- the copy starts at `m5_001` and ends at the installed package's `SCHEMA_HEAD`;
- `cardea init` kept a pre-migration snapshot, and `cardea verify` passes;
- every table that existed before has the same row count after;
- the upgraded schema (tables, indexes, triggers) equals a fresh store's at the head;
- every unconditional append-only trigger refuses an `UPDATE` or `DELETE` of a row it
  guards, tried on a scratch copy and rolled back.
"""

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

# `python -I` leaves the script's own directory off the path; the shared modules are there.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    Evidence,
    add_common_arguments,
    claim,
    read_only,
    run_script,
    use_kernel,
)

NAME = "upgrade"
M5 = "m5_001"
GUARD = re.compile(
    r"CREATE TRIGGER\s+(\w+)\s+BEFORE\s+(DELETE|UPDATE(?:\s+OF\s+([\w\s,]+?))?)\s+ON\s+(\w+)\s",
    re.IGNORECASE,
)


def revision(path: Path) -> str | None:
    with read_only(path) as conn:
        try:
            row = conn.execute("SELECT version_num FROM cardea_alembic_version").fetchone()
        except sqlite3.Error:
            return None
    return str(row[0]) if row else None


def counts(path: Path) -> dict[str, int]:
    with read_only(path) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {t: int(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]) for t in tables}


def schema(path: Path) -> dict[str, str]:
    """Tables, indexes and triggers by `type:name`, with whitespace normalised."""
    with read_only(path) as conn:
        return {
            f"{kind}:{name}": " ".join((sql or "").split())
            for kind, name, sql in conn.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','index','trigger')"
            )
        }


def copy(source: Path, destination: Path) -> None:
    """SQLite's backup API: a consistent standalone copy; the source is opened read-only."""
    with read_only(source) as src:
        target = sqlite3.connect(destination)
        try:
            src.backup(target)
        finally:
            target.close()
    destination.chmod(0o600)


def guards(path: Path) -> list[dict[str, Any]]:
    """Unconditional BEFORE UPDATE/DELETE triggers that abort: the append-only rules."""
    found = []
    with read_only(path) as conn:
        for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'"):
            text = " ".join((sql or "").split())
            match = GUARD.match(text + " ")
            if not match or "RAISE(ABORT" not in text.upper() or " WHEN " in text.upper():
                continue
            action = match.group(2).split()[0].upper()
            columns = [c.strip() for c in (match.group(3) or "").split(",") if c.strip()]
            found.append(
                {"trigger": name, "action": action, "columns": columns, "table": match.group(4)}
            )
    return found


def probe(path: Path, guard: dict[str, Any]) -> str:
    """Try the guarded change on one row of a scratch copy; 'refused', 'allowed' or 'empty'."""
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        table = guard["table"]
        row = conn.execute(f'SELECT rowid FROM "{table}" LIMIT 1').fetchone()
        if row is None:
            return "empty"
        column = (guard["columns"] or [_first_column(conn, table)])[0]
        statement = (
            f'DELETE FROM "{table}" WHERE rowid=?'
            if guard["action"] == "DELETE"
            else f'UPDATE "{table}" SET "{column}"="{column}" WHERE rowid=?'
        )
        conn.execute("BEGIN")
        try:
            conn.execute(statement, (row[0],))
        except sqlite3.DatabaseError:
            return "refused"
        finally:
            conn.execute("ROLLBACK")
        return "allowed"
    finally:
        conn.close()


def _first_column(conn: sqlite3.Connection, table: str) -> str:
    return str(conn.execute(f'PRAGMA table_info("{table}")').fetchone()[1])


def cli(cardea: list[str], command: str, config: Path) -> dict[str, Any]:
    done = subprocess.run(
        [*cardea, command, "--config", str(config)],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    return {"exit": done.returncode, "stdout": done.stdout[-4000:], "stderr": done.stderr[-4000:]}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_common_arguments(parser, NAME)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", type=Path, help="a `cardea backup` of an M5 store")
    source.add_argument("--synthesize", action="store_true", help="build an empty m5_001 store")
    parser.add_argument(
        "--cardea",
        default="/usr/bin/cardea",
        help="the installed CLI (default: %(default)s); a path, or a command line",
    )
    args = parser.parse_args(argv)
    lib = use_kernel(args.cardea_lib)
    work = claim(args.work, NAME)
    evidence = Evidence(NAME, work, args.out)

    def body() -> None:
        from cardea.service.admin import initialize
        from cardea.service.database import SCHEMA_HEAD, Database
        from cardea.service.models import ServiceConfig

        store = work / "store"
        store.mkdir(mode=0o700)
        database = store / "kernel.sqlite3"
        evidence.observe("kernel_lib", str(lib))
        evidence.observe("schema_head", SCHEMA_HEAD)
        if args.source:
            if revision(args.source) != M5:
                raise SystemExit(f"{args.source} is not an {M5} store")
            copy(args.source, database)
            evidence.observe("source", {"path": str(args.source), "revision": M5})
        else:
            import cardea.service.admin as admin_module
            from alembic import command
            from alembic.config import Config

            created = Database(store)
            config = Config()
            migrations = Path(admin_module.__file__).parent / "migrations"
            config.set_main_option("script_location", str(migrations))
            with created.engine.begin() as conn:
                config.attributes["connection"] = conn
                command.upgrade(config, M5)
            created.close()
            evidence.observe("source", {"synthesized": True, "revision": M5})
        evidence.check("starts_at_m5", revision(database) == M5)
        before = counts(database)
        evidence.observe("rows_before", before)

        service = ServiceConfig.model_validate(
            {
                "data_dir": str(store),
                "spiffe_socket": "unix:///nonexistent",
                "trust_domain": "acceptance.invalid",
                "runtime_spiffe_id": "spiffe://acceptance.invalid/runtime",
            }
        )
        config_path = work / "config.json"
        config_path.write_text(service.model_dump_json(exclude_none=True))
        config_path.chmod(0o600)
        cardea = args.cardea.split()
        init = cli(cardea, "init", config_path)
        evidence.check("cardea_init_succeeds", init["exit"] == 0, init=init)
        evidence.check("reaches_schema_head", revision(database) == SCHEMA_HEAD)
        snapshots = sorted(p.name for p in store.glob("before-migration-*.sqlite3"))
        evidence.check("pre_migration_snapshot_kept", len(snapshots) == 1, snapshots=snapshots)
        if snapshots:
            evidence.check("snapshot_is_the_m5_store", revision(store / snapshots[0]) == M5)
        verified = cli(cardea, "verify", config_path)
        try:
            report = json.loads(verified["stdout"] or "{}")
        except ValueError:
            report = {}
        evidence.check("cardea_verify_passes", verified["exit"] == 0, verify=verified)
        evidence.observe("verify_report", report)
        again = cli(cardea, "init", config_path)
        evidence.check(
            "second_init_changes_nothing",
            again["exit"] == 0
            and sorted(p.name for p in store.glob("before-migration-*.sqlite3")) == snapshots,
        )

        after = counts(database)
        evidence.observe("rows_after", after)
        changed = {
            t: {"before": n, "after": after.get(t)}
            for t, n in before.items()
            if t not in {"cardea_alembic_version", "service_meta"} and after.get(t) != n
        }
        evidence.check("row_counts_preserved", not changed, changed=changed)

        fresh = work / "fresh"
        initialize(fresh)
        upgraded, expected = schema(database), schema(fresh / "kernel.sqlite3")
        difference = {
            "missing": sorted(set(expected) - set(upgraded)),
            "extra": sorted(set(upgraded) - set(expected)),
            "different": sorted(
                k for k in set(expected) & set(upgraded) if expected[k] != upgraded[k]
            ),
        }
        evidence.check(
            "schema_equals_a_fresh_head_store", not any(difference.values()), difference=difference
        )

        scratch = work / "probe.sqlite3"
        copy(database, scratch)
        results = {g["trigger"]: {**g, "result": probe(scratch, g)} for g in guards(scratch)}
        evidence.observe("append_only_triggers", results)
        evidence.check("append_only_triggers_found", bool(results))
        evidence.check(
            "append_only_triggers_refuse_changes",
            all(r["result"] in {"refused", "empty"} for r in results.values()),
            allowed=[n for n, r in results.items() if r["result"] == "allowed"],
            exercised=sorted(n for n, r in results.items() if r["result"] == "refused"),
        )
        scratch.unlink()

    return run_script(evidence, body)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
