"""Keep legacy CLI subprocess probes offline during tests."""
from __future__ import annotations

import subprocess
import sys


def _offline_run(args, *pos, **kwargs):
    if isinstance(args, (tuple, list)) and args and args[0] == "/bin/launchctl":
        if len(args) > 1 and args[1] == "print":
            return subprocess.CompletedProcess(args, 1, "", "")
        raise AssertionError("tests must not use launchctl")
    return _run(args, *pos, **kwargs)


_run = subprocess.run
subprocess.run = _offline_run
sys.dont_write_bytecode = True
