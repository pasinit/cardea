"""Throwaway test environments for Cardea agent teams (ADR-038).

`env.try` checks out one commit of an allowed repository into a throwaway confined worker
(the ADR-028 process tier), runs named scenarios from the host owner's catalog against it,
and returns bounded pass or fail evidence with artifacts stored as kernel objects. The
same commit and scenarios within the run window return the same result (the kernel's run
table, `cardea.service.env_runs`). A transition can require a passing run with
`requires_receipt: {operation: env.try, result: "result.passed == true"}` (ADR-034).
"""

from importlib.resources import files

from cardea.service.operations import OperationDescriptor


def descriptors() -> tuple[OperationDescriptor, ...]:
    """The package's descriptors, as installed under the host's `operations_dir`."""
    folder = files(__package__) / "descriptors"
    return tuple(
        OperationDescriptor.model_validate_json(entry.read_text())
        for entry in sorted(folder.iterdir(), key=lambda e: e.name)
        if entry.name.endswith(".json")
    )
