"""Subprocess admission for crash injection; only explicitly supplied temporary stores."""

import argparse
from pathlib import Path

from adapter import admit

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--value", type=int, default=1)
    parser.add_argument("--scenario", default="local")
    parser.add_argument("--separate", action="store_true")
    parser.add_argument("--engine-first", action="store_true")
    args = parser.parse_args()
    admit(
        args.root,
        args.value,
        scenario=args.scenario,
        separate=args.separate,
        engine_first=args.engine_first,
    )
