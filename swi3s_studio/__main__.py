"""Launcher: `python3 -m swi3s_studio`, and the PyInstaller entry script.

PyInstaller runs its entry script as a top-level `__main__` with no parent package, where
`swi3s_studio/app.py`'s relative imports cannot resolve. This file imports the package by its
absolute name, so it runs the same way as a module and as a script.
"""
from swi3s_studio.app import main

if __name__ == "__main__":
    raise SystemExit(main())
