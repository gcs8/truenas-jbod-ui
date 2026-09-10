#!/usr/bin/env python3
"""Offline admission, paused replay and explicit evidence-preserving finalization."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from history_service.explicit_recovery import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
