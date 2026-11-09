"""Pick the target before sk.target loads: `--env <name>` (or MP04_ENV) -> targets/<name>.json. Strips the flag."""
from __future__ import annotations

import os
import sys


def bootstrap(argv: list[str] | None = None) -> list[str]:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--env" in argv:
        i = argv.index("--env")
        os.environ["MP04_ENV"] = argv[i + 1]
        del argv[i:i + 2]
    os.environ.setdefault("MP04_ENV", "productqa")
    return argv
