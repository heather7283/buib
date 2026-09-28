"""Allow ``python -m buibctl``."""

from __future__ import annotations

from buibctl.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
