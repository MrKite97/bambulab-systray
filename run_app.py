"""Frozen-app entry point for PyInstaller.

PyInstaller analyzes/runs a top-level script, not a ``-m package.module`` form.
This thin shim gives the spec a single, clean entry script that simply defers to
the real orchestration in ``src.app.main`` (the same callable ``python -m src.app``
runs). Keeping it this small means the packaged ``.exe`` and the run-from-source
path share one entry, with no duplicated startup logic.
"""

from src.app import main

raise SystemExit(main())
