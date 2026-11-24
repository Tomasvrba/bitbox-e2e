#!/usr/bin/env python3
"""Thin CLI shim invoking bitbox_e2e.cli.main()."""

import os
import sys
from pathlib import Path


def _reexec_venv() -> None:
    venv = Path(__file__).resolve().parent / ".venv"
    py = venv / "bin" / "python"
    if py.is_file() and Path(sys.prefix).resolve() != venv.resolve():
        os.execv(str(py), [str(py), *sys.argv])


if __name__ == "__main__":
    _reexec_venv()
    from bitbox_e2e.cli import main

    main()
