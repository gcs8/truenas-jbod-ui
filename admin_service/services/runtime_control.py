from __future__ import annotations

import http.client
import json
import socket
import urllib.parse
from pathlib import Path
from typing import Any

from admin_service.config import AdminSettings


class DockerRuntimeError(RuntimeError):
    pass


class UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: int = 5) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class DockerRuntimeService:
    def __init__(self, settings: AdminSettings) -> None:
        self.settings = settings
        self.socket_path = settings.docker_socket_path
        self.pending_restart_keys: set[str] = set()
        self.managed_containers = {
            "ui": {
                "name": settings.container_ui_name,
                "label": "Read UI",
                "description": "Primary read-mostly enclosure UI.",
            },
            "history": {
                "name": settings.container_history_name,
                "label": "History Sidecar",
                "description": "Optional SQLite history collector.",
            },
            "admin": {
                "name": settings.container_admin_name,
                "label": "Admin Sidecar",
                "description": "Optional maintenance surface.",
            },
        }

    @property
    def available(self) -> bool:
        return Path(self.socket_path).exists()

    def status_payload(self) -> dict[str, Any]:
        if not self.available:
            return {
                "available": False,
                "detail": f"Docker socket {self.socket_path} is not mounted into the admin sidecar.",
                "containers": [self._missing_status_payload(key) for key in self.managed_containers],
            }
        try:
            containers = self._request_json("GET", "/containers/json?all=1")
        except DockerRuntimeError as exc:
            return {
                "available": False,
                "detail": str(exc),
                "containers": [self._missing_status_payload(key) for key in self.managed_containers],
            }

        by_name: dict[str, dict[str, Any]] = {}
        for container in containers:
            for name in container.get("Names") or []:
                by_name[str(name).lstrip("/")] = container

        return {
            "available": True,
            "detail": None,
            "containers": [
                self._build_status_payload(key, by_name.get(meta["name"]))
                for key, meta in self.managed_containers.items()
            ],
        }

    def running_container_keys(self, keys: list[str] | tuple[str, ...] | None = None) -> list[str]:
        requested = set(keys or self.managed_containers.keys())
        payload = self.status_payload()
        return [
            item["key"]
            for item in payload.get("containers", [])
            if item.get("key") in requested and item.get("running")
        ]

    def stop_container(self, key: str) -> None:
        self._request("POST", f"/containers/{self._quoted_name(key)}/stop?t={self.settings.container_control_timeout_seconds}")

    def start_container(self, key: str) -> None:
        self._request("POST", f"/containers/{self._quoted_name(key)}/start")
        self.clear_restart_required([key])

    def restart_container(self, key: str) -> None:
        self._request(
            "POST",
            f"/containers/{self._quoted_name(key)}/restart?t={self.settings.container_control_timeout_seconds}",
        )
        self.clear_restart_required([key])

    def mark_restart_required(self, keys: list[str] | tuple[str, ...]) -> None:
        for key in keys:
            if key in self.managed_containers:
                self.pending_restart_keys.add(key)

    def clear_restart_required(self, keys: list[str] | tuple[str, ...]) -> None:
        for key in keys:
            self.pending_restart_keys.discard(key)

    def _quoted_name(self, key: str) -> str:
        meta = self.managed_containers.get(key)
        if meta is None:
            raise DockerRuntimeError(f"Unknown managed container key '{key}'.")
        return urllib.parse.quote(str(meta["name"]), safe="")

    def _request_json(self, method: str, path: str, body: bytes | None = None) -> Any:
        response_body = self._request(method, path, body=body)
        try:
            return json.loads(response_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DockerRuntimeError("Docker runtime returned invalid JSON.") from exc

    def _request(self, method: str, path: str, body: bytes | None = None) -> bytes:
        if not self.available:
            raise DockerRuntimeError(
                f"Docker socket {self.socket_path} is not mounted into the admin sidecar."
            )
        connection = UnixSocketHTTPConnection(self.socket_path, timeout=self.settings.container_control_timeout_seconds)
        try:
            connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            payload = response.read()
        except OSError as exc:
            raise DockerRuntimeError(f"Unable to talk to the Docker runtime: {exc}.") from exc
        finally:
            connection.close()

        if response.status >= 400:
            detail = payload.decode("utf-8", errors="replace")
            raise DockerRuntimeError(f"Docker runtime returned HTTP {response.status}: {detail}")
        return payload

    def _build_status_payload(self, key: str, container: dict[str, Any] | None) -> dict[str, Any]:
        meta = self.managed_containers[key]
        if not container:
            return self._missing_status_payload(key)

        raw_state = str(container.get("State") or "").lower()
        status_text = str(container.get("Status") or raw_state or "unknown")
        health = self._extract_health(status_text)
        running = raw_state == "running"
        restart_required = key in self.pending_restart_keys
        lifecycle_state = "down"
        lifecycle_label = "Down"
        if running and restart_required:
            lifecycle_state = "needs_restart"
            lifecycle_label = "Needs Restart"
        elif running:
            lifecycle_state = "normal"
            lifecycle_label = "Normal"
        return {
            "key": key,
            "name": meta["name"],
            "label": meta["label"],
            "description": meta["description"],
            "status": raw_state or "unknown",
            "status_text": status_text,
            "running": running,
            "health": health,
            "restart_required": restart_required,
            "lifecycle_state": lifecycle_state,
            "lifecycle_label": lifecycle_label,
            "can_stop": running and key != "admin",
            "can_start": not running,
            "can_restart": running and key != "admin",
        }

    def _missing_status_payload(self, key: str) -> dict[str, Any]:
        meta = self.managed_containers[key]
        return {
            "key": key,
            "name": meta["name"],
            "label": meta["label"],
            "description": meta["description"],
            "status": "unavailable",
            "status_text": "Docker control unavailable",
            "running": False,
            "health": None,
            "restart_required": key in self.pending_restart_keys,
            "lifecycle_state": "down",
            "lifecycle_label": "Down",
            "can_stop": False,
            "can_start": False,
            "can_restart": False,
        }

    @staticmethod
    def _extract_health(status_text: str) -> str | None:
        lowered = status_text.lower()
        if "(healthy)" in lowered:
            return "healthy"
        if "(unhealthy)" in lowered:
            return "unhealthy"
        if "(health: starting)" in lowered:
            return "starting"
        return None
