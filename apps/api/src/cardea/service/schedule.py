"""Pure slot arithmetic for time triggers (ADR-024).

`croniter` expands the cron expression on the trigger's local wall clock (naive
datetimes). This module alone maps local times to UTC, so DST behavior is explicit:

- a local time that occurs twice (DST ends) is one slot, at its first occurrence;
- a local time that does not exist (DST starts) is one slot, at the end of the gap,
  the first instant whose local time is at or after it;
- slots that map to the same instant are one slot.

The mapping is monotone in local time, which is what lets `latest` walk backwards.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter  # type: ignore[import-untyped]

# A slot within this age is on time, whatever the misfire policy. The DBOS clock fires
# once a minute, with up to 10% jitter capped at 6 seconds.
ON_TIME = timedelta(seconds=120)
# Missed-slot counts are reported exactly up to this bound.
MAX_COUNT = 1000
# Larger than any UTC offset change in the IANA database (2 hours).
MARGIN = timedelta(hours=3)
MAX_STEPS = 100_000

Misfire = Literal["skip", "run_once_late", "grace"]
_ATOM = r"(?:\*|[0-9]{1,2}|[a-z]{3})"
_ITEM = rf"{_ATOM}(?:-{_ATOM})?(?:/[0-9]{{1,2}})?"
_FIELD = re.compile(rf"{_ITEM}(?:,{_ITEM})*")


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Timezone must be an IANA name") from None


def validate_schedule(schedule: str, timezone: str) -> None:
    """Accept five-field POSIX/Vixie cron only; refuse croniter's non-standard extensions."""
    fields = schedule.lower().split()
    if len(fields) != 5 or not all(_FIELD.fullmatch(field) for field in fields):
        raise ValueError("Schedule must be a five-field cron expression")
    if not croniter.is_valid(" ".join(fields)):
        raise ValueError("Schedule is not a valid cron expression")
    zone(timezone)
    try:  # Refuse expressions that never fire, such as February 30.
        croniter(" ".join(fields), datetime(2000, 1, 1)).get_next(datetime)
    except Exception:
        raise ValueError("Schedule never fires") from None


def slot_key(slot: datetime) -> str:
    """Compact UTC form used in slot request IDs: `20260329T073000Z`."""
    return slot.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _local(instant: datetime, tz: ZoneInfo) -> datetime:
    return instant.astimezone(tz).replace(tzinfo=None)


def to_utc(local: datetime, tz: ZoneInfo) -> datetime:
    first = local.replace(tzinfo=tz, fold=0).astimezone(UTC)
    if _local(first, tz) == local:
        return first  # Exists; fold=0 is the first of two occurrences.
    # Inside a gap fold=1 applies the later offset and lands before the gap; fold=0
    # lands after it. Bisect for the first instant whose local time reaches `local`.
    low = local.replace(tzinfo=tz, fold=1).astimezone(UTC)
    high = first
    while high - low > timedelta(seconds=1):
        middle = low + (high - low) / 2
        middle = middle.replace(microsecond=0)
        if _local(middle, tz) >= local:
            high = middle
        else:
            low = middle
    return high


def forward(schedule: str, timezone: str, after: datetime) -> Iterator[datetime]:
    """Slots strictly after `after`, ascending, without duplicates."""
    tz = zone(timezone)
    expansion = croniter(schedule.lower(), _local(after, tz))
    last = after
    while True:
        slot = to_utc(expansion.get_next(datetime), tz)
        if slot > last:
            last = slot
            yield slot


def latest(schedule: str, timezone: str, after: datetime, now: datetime) -> datetime | None:
    """The newest slot in `(after, now]`, or None."""
    tz = zone(timezone)
    expansion = croniter(schedule.lower(), _local(now, tz) + MARGIN)
    for _ in range(MAX_STEPS):
        slot = to_utc(expansion.get_prev(datetime), tz)
        if slot <= now:
            return slot if slot > after else None
    raise RuntimeError("Schedule scan exceeded its bound")


@dataclass(frozen=True)
class Decision:
    slot: datetime | None = None
    admit: bool = False
    late: bool = False
    missed: int = 0
    missed_capped: bool = False
    first_missed: datetime | None = None
    last_missed: datetime | None = None


def decide(
    schedule: str,
    timezone: str,
    misfire: Misfire,
    grace_seconds: int | None,
    after: datetime,
    now: datetime,
) -> Decision:
    """Which slot, if any, to admit now; `slot` becomes the trigger's last handled slot.

    At most one slot is admitted per decision: the newest due one. Older slots in
    `(after, slot)` were missed (downtime) and are reported, never admitted.
    """
    newest = latest(schedule, timezone, after, now)
    if newest is None:
        return Decision()
    age = now - newest
    late = age > ON_TIME
    admit = (
        not late
        or misfire == "run_once_late"
        or (misfire == "grace" and age <= timedelta(seconds=grace_seconds or 0))
    )
    count, first, capped = 0, None, False
    for slot in forward(schedule, timezone, after):
        if slot >= newest:
            break
        if count == MAX_COUNT:
            capped = True
            break
        first = first or slot
        count += 1
    previous = latest(schedule, timezone, after, newest - timedelta(microseconds=1))
    if not admit:
        count, first, previous = count + 1, first or newest, newest
    return Decision(
        slot=newest,
        admit=admit,
        late=late,
        missed=count,
        missed_capped=capped,
        first_missed=first,
        last_missed=previous if count else None,
    )
