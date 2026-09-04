"""Shared admin test environment.

``admin_service.main`` builds its module-level ASGI app at import time and refuses
to start without a valid ``ADMIN_PUBLIC_ORIGIN``. Test modules that import it
load this helper first so the process has a synthetic origin and any cached
settings read before the variable was usable are discarded.

The inherited value is replaced whenever it is not a valid origin, not only when
the key is absent: a shell that sourced ``.env`` carries the shipped empty
``ADMIN_PUBLIC_ORIGIN=`` line as a present-but-blank variable.
"""

from __future__ import annotations

import os

from admin_service.config import get_admin_settings
from app.http_auth import configured_origin_identity

ADMIN_TEST_PUBLIC_ORIGIN = "http://admin.example.test"

if configured_origin_identity(os.environ.get("ADMIN_PUBLIC_ORIGIN")) is None:
    os.environ["ADMIN_PUBLIC_ORIGIN"] = ADMIN_TEST_PUBLIC_ORIGIN
get_admin_settings.cache_clear()
