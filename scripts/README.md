# Task page synchronization

The 108 public task pages are generated from two explicit sources:

- `task-pages.json` owns website presentation metadata and legacy trajectory links.
- Toolathlon commit `d57361c0f1582cf9a0675c0753315bb6b004bd0e` owns instructions, tool requirements, and versioned initial state.

Generate pages from a local checkout containing that commit:

```bash
python3 scripts/sync_tasks.py --upstream-repo /path/to/Toolathlon --write
```

Verify that generated pages have not drifted:

```bash
python3 scripts/sync_tasks.py --upstream-repo /path/to/Toolathlon --check
```

The old `*_.mdx`, `update_inst.py`, and `traj.py` page-generation chain is retired. Old underscore URLs are preserved with redirects in `docs.json`.
