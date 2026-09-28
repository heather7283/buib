"""Allow ``python -m buibd``."""

from __future__ import annotations

from buibd.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
