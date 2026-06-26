"""Entrypoint for ``python -m mailflow_monitor``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())

