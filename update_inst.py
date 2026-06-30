#!/usr/bin/env python3
"""Compatibility entry point for the canonical task-page synchronizer.

The former implementation edited deleted ``*_.mdx`` staging pages from a
mutable sibling checkout.  Public task pages now come from ``task-pages.json``
and a pinned Toolathlon commit through ``scripts/sync_tasks.py``.
"""

from __future__ import annotations

import sys

from scripts.sync_tasks import main


if __name__ == "__main__":
    print(
        "update_inst.py is deprecated; forwarding to scripts/sync_tasks.py",
        file=sys.stderr,
    )
    raise SystemExit(main())
