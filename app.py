"""Vercel entrypoint for the read-only dashboard (Vercel looks for a FastAPI `app` in app.py).

Only the dashboard runs on Vercel. The pipeline runs in GitHub Actions (see .github/workflows).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ainews.dashboard.app import app  # noqa: E402

__all__ = ["app"]
