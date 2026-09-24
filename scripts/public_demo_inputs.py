from __future__ import annotations

from pathlib import Path


# This is the single authoritative semantic input graph for the checked public
# demo. The builder records it and the checker verifies the same declaration.
PUBLIC_DEMO_INPUT_PATHS: tuple[Path, ...] = tuple(
    sorted(
        (
            Path("tests/fixtures/public_demo/public_demo.json"),
            Path("app/__init__.py"),
            Path("app/config.py"),
            Path("app/config_errors.py"),
            Path("app/env_values.py"),
            Path("app/logging_config.py"),
            Path("app/main.py"),
            Path("app/metrics.py"),
            Path("app/models/domain.py"),
            Path("app/perf.py"),
            Path("app/request_context.py"),
            Path("app/script_json.py"),
            Path("app/secret_files.py"),
            Path("app/slot_layout.py"),
            Path("app/services/history_backend.py"),
            Path("app/services/history_status.py"),
            Path("app/services/profile_registry.py"),
            Path("app/services/public_demo_fixture.py"),
            Path("app/services/snapshot_export.py"),
            Path("app/services/storage_view_templates.py"),
            Path("app/services/storage_views.py"),
            Path("scripts/build_public_demo.py"),
            Path("scripts/public_demo_inputs.py"),
            Path("scripts/public_demo_source_parity.py"),
            Path("app/static/app.js"),
            Path("app/static/style.css"),
            Path("app/templates/base.html"),
            Path("app/templates/index.html"),
            Path("history_service/operation_bounds.py"),
            Path("history_service/scheduled_backup.py"),
            Path("app/static/images/aoc-slg4-2h8m2.jpg"),
            Path("app/static/images/hyper-m2-gen3-card.png"),
            Path("app/static/images/satadom-ml-3ie3-v2.png"),
        ),
        key=lambda path: path.as_posix(),
    )
)
