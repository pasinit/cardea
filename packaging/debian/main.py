"""Fixed root-owned import location, independent of cwd and PYTHONPATH."""

import runpy
import sys

sys.path.insert(0, "/usr/lib/cardea")
runpy.run_module("cardea.service", run_name="__main__")
