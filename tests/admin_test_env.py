"""Shared admin test environment.

``admin_service.main`` builds its module-level ASGI app at import time and refuses
to start without a valid ``ADMIN_PUBLIC_ORIGIN``. Test modules that import it
load this helper first so the process has a synthetic origin and any cached
settings read before the variable existed are discarded.
"""

from __future__ import annotations

import os

from admin_service.config import get_admin_settings

ADMIN_TEST_PUBLIC_ORIGIN = "http://admin.example.test"

os.environ.setdefault("ADMIN_PUBLIC_ORIGIN", ADMIN_TEST_PUBLIC_ORIGIN)
get_admin_settings.cache_clear()
