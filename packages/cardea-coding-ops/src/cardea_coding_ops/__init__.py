"""Coding operations for Cardea agent teams (ADR-037).

`code.session` runs inside a confined worker (ADR-028): it checks out a branch of an allowed
repository from a read-only source, runs a headless coding agent on a task, runs the
repository's gates and returns the change as a git bundle artifact (stored by the kernel as
task-owned objects) with a bounded summary. The worker holds no repository-host credential
and keeps nothing between calls.
"""

from importlib.resources import files
from pathlib import Path


def descriptors() -> list[Path]:
    """The package's operation descriptors, for a host owner to install (ADR-023)."""
    return sorted(Path(str(p)) for p in files(__name__).joinpath("descriptors").iterdir())
