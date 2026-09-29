"""Long-running backup scheduler sidecar (config-on-change + cron full backups).

``service.py`` holds the logic (build, catalog, ship, groom), ``api.py`` the
Unix-socket HTTP API the admin sidecar proxies, and ``main.py`` the entrypoint.
"""
