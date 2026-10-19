"""Independent cardea-load command line."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cardea_load.agent import agent_compare, run_agent, write_agent_baseline
from cardea_load.delegation import delegation_baseline, delegation_compare, run_delegation
from cardea_load.metrics import baseline, compare, save
from cardea_load.report import render
from cardea_load.runner import run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("delegation-run", "delegation-baseline", "delegation-compare"):
        command = commands.add_parser(name)
        command.add_argument("--output", type=Path, required=True)
        if name == "delegation-run":
            command.add_argument("--kind", choices=("reference", "comparison"), required=True)
            command.add_argument("--repo", type=Path, required=True)
            command.add_argument("--api-python", type=Path, required=True)
        else:
            command.add_argument("--run", type=Path, required=True)
            if name == "delegation-compare":
                command.add_argument("--baseline", type=Path, required=True)
    agent = commands.add_parser("agent-run")
    agent.add_argument("--kind", choices=("reference", "comparison"), required=True)
    agent.add_argument("--repo", type=Path, required=True)
    agent.add_argument("--api-python", type=Path, required=True)
    agent.add_argument("--output", type=Path, required=True)
    agent_reference = commands.add_parser("agent-baseline")
    agent_reference.add_argument("--run", type=Path, required=True)
    agent_reference.add_argument("--output", type=Path, required=True)
    agent_comparison = commands.add_parser("agent-compare")
    agent_comparison.add_argument("--baseline", type=Path, required=True)
    agent_comparison.add_argument("--run", type=Path, required=True)
    agent_comparison.add_argument("--output", type=Path, required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--kind", choices=("reference", "comparison", "stress"), required=True)
    execute.add_argument("--profile", type=Path, required=True)
    execute.add_argument("--repo", type=Path, required=True)
    execute.add_argument("--api-python", type=Path)
    execute.add_argument("--vegeta", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    reference = commands.add_parser("baseline")
    reference.add_argument("--run", type=Path, required=True)
    reference.add_argument("--output", type=Path, required=True)
    comparison = commands.add_parser("compare")
    comparison.add_argument("--baseline", type=Path, required=True)
    comparison.add_argument("--run", type=Path, required=True)
    comparison.add_argument("--output", type=Path, required=True)
    report = commands.add_parser("report")
    report.add_argument("--campaign", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "delegation-run":
        run_delegation(
            args.repo.resolve(), args.api_python.absolute(), args.output.resolve(), args.kind
        )
    elif args.command == "delegation-baseline":
        save(args.output, delegation_baseline(json.loads(args.run.read_text())))
    elif args.command == "delegation-compare":
        result = delegation_compare(
            json.loads(args.baseline.read_text()), json.loads(args.run.read_text())
        )
        save(args.output, result)
        if not result["correctness_passed"]:
            raise SystemExit(2)
    elif args.command == "agent-run":
        run_agent(args.repo.resolve(), args.api_python.absolute(), args.output.resolve(), args.kind)
    elif args.command == "agent-baseline":
        write_agent_baseline(args.run, args.output)
    elif args.command == "agent-compare":
        result = agent_compare(
            json.loads(args.baseline.read_text()), json.loads(args.run.read_text())
        )
        save(args.output, result)
        if not result["correctness_passed"]:
            raise SystemExit(2)
    elif args.command == "run":
        repo = args.repo.resolve()
        result = run(
            repo,
            (args.api_python or repo / "apps/api/.venv/bin/python").absolute(),
            args.vegeta.resolve(),
            json.loads(args.profile.read_text()),
            args.output.resolve(),
            args.kind,
        )
        if not all(p["passed"] for p in result["points"] + result.get("scenarios", [])):
            raise SystemExit(2)
    elif args.command == "baseline":
        save(args.output, baseline(json.loads(args.run.read_text())))
    elif args.command == "report":
        content = render(args.campaign)
        with args.output.open("x") as stream:
            stream.write(content)
    else:
        result = compare(json.loads(args.baseline.read_text()), json.loads(args.run.read_text()))
        save(args.output, result)
        if not result["correctness_passed"]:
            raise SystemExit(2)


if __name__ == "__main__":
    main()
