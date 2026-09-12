"""
PyInstaller entry point. Deliberately NOT `src/fx_connector/main.py`
itself -- main.py uses package-relative imports (`from .config import
...`), which break with `ImportError: attempted relative import with no
known parent package` when PyInstaller runs a script directly as
`__main__` rather than as a submodule of an imported package (a real,
documented PyInstaller gotcha for exactly this project layout, confirmed
by actually running the frozen build during Phase 5e -- see
docs/ARCHITECTURE.md). This tiny shim has no relative imports of its own
and does a normal absolute import of the package instead, which sidesteps
the problem entirely: fx_connector's own internal relative imports
between its submodules are unaffected once the package itself is
imported properly.
"""
from fx_connector.main import main

if __name__ == "__main__":
    main()
