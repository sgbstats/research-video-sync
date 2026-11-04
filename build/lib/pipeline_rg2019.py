#!/usr/bin/env python3
"""Compatibility entry point for the installed RG2019 pipeline."""
from __future__ import annotations

import sys

from rg2019 import cli


if __name__ == "__main__":
    sys.exit(cli.main())
else:
    sys.modules[__name__] = cli
