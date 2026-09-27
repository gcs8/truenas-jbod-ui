from __future__ import annotations

import asyncio
import io
import json
import socket
import ssl
import sys
import threading
import time
import unittest
from email.message import Message
from http.client import BadStatusLine, IncompleteRead, RemoteDisconnected
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from unittest.mock import MagicMock, patch

from app.config import TrueNASConfig
from app.services.quantastor_api import QuantastorRESTClient
from app.services.truenas_ws import TrueNASAPIError


ENDPOINT_FIELDS = {
    "storageSystemEnum": "systems",
    "physicalDiskEnum": "disks",
    "storagePoolEnum": "pools",
    "storagePoolDeviceEnum": "pool_devices",
    "haGroupEnum": "ha_groups",
    "hwDiskEnum": "hw_disks",
    "hwEnclosureEnum": "hw_enclosures",
}
OPTIONAL_ENDPOINTS = tuple(ENDPOINT_FIELDS)[3:]
REQUIRED_ENDPOINTS = tuple(ENDPOINT_FIELDS)[:3]


def transport_errors():
    return (
        socket.timeout("synthetic read timeout"),
        ConnectionResetError("synthetic reset"),
        OSError("synthetic socket failure"),
        ssl.SSLError("synthetic TLS read failure"),
        IncompleteRead(b"[", 20),
        RemoteDisconnected("synthetic disconnect"),
        BadStatusLine("synthetic malformed status"),
        URLError("synthetic URL failure"),
    )


class SyntheticTransport:
    """Replace only urlopen; retain the real client, body decode and collection."""

    def __init__(self, endpoint=None, error=None, *, phase="read", body=None, charset="utf-8", status=None):
        self.endpoint = endpoint
        self.error = error
        self.phase = phase
        self.body = body
        self.charset = charset
        self.status = status
        self.responses = {}
        self.calls = []
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.completed = 0
        self.read_entered = threading.Event()
        self.release_read = threading.Event()

    def complete(self):
        with self.lock:
            self.completed += 1
            if self.completed == len(ENDPOINT_FIELDS):
                self.finished.set()

    def __call__(self, request, **kwargs):
        endpoint = urlsplit(request.full_url).path.rsplit("/", 1)[-1]
        if endpoint not in ENDPOINT_FIELDS:
            raise AssertionError(f"Unexpected endpoint: {endpoint}")
        with self.lock:
            self.calls.append(endpoint)
        targeted = endpoint == self.endpoint
        if targeted and self.phase == "open":
            self.complete()
            assert self.error is not None
            raise self.error
        transport = self

        class Response(io.BytesIO):
            headers: Message

            def read(self, *args):
                if targeted:
                    transport.read_entered.set()
                    if transport.phase == "blocked" and not transport.release_read.wait(2):
                        raise AssertionError("Synthetic read was not released")
                    if transport.error is not None:
                        raise transport.error
                return super().read(*args)

            def close(self):
                if not self.closed:
                    super().close()
                    transport.complete()

        body = self.body if targeted and self.body is not None else json.dumps([{"endpoint": endpoint}]).encode()
        response = Response(body)
        response.headers = Message()
        response.headers["Content-Type"] = f"application/json; charset={self.charset if targeted else 'utf-8'}"
        with self.lock:
            self.responses[endpoint] = response
        if targeted and self.status is not None:
            raise HTTPError(request.full_url, self.status, "synthetic HTTP failure", response.headers, response)
        return response


class QuantastorRESTClientTests(unittest.IsolatedAsyncioTestCase):
    def transport_client(self):
        return QuantastorRESTClient(TrueNASConfig(
            host="http://quantastor.example.invalid",
            api_user="synthetic-user",
            api_password="synthetic-password",
            platform="quantastor",
        ))

    async def fetch_transport(self, transport):
        with patch("app.services.quantastor_api.urlopen_with_tls_config", side_effect=transport):
            try:
                return await self.transport_client().fetch_all()
            finally:
                # gather can raise before sibling threads finish; keep the patch
                # alive and drain every synthetic response before the next case.
                # HTTPError owns a separate response, including on the baseline
                # where the client does not close it. Clean up the test fixture.
                if transport.status is not None and transport.endpoint in transport.responses:
                    transport.responses[transport.endpoint].close()
                self.assertTrue(await asyncio.to_thread(transport.finished.wait, 2))
                self.assertCountEqual(transport.calls, ENDPOINT_FIELDS)
                self.assertTrue(all(response.closed for response in transport.responses.values()))

    def assert_bundle(self, payload, missing=None):
        for endpoint, field in ENDPOINT_FIELDS.items():
            self.assertEqual(getattr(payload, field), [] if endpoint == missing else [{"endpoint": endpoint}])
        self.assertEqual(payload.enclosures, payload.systems)

    async def test_fetch_all_read_timeout_at_each_optional_endpoint_preserves_required_rows(self):
        for endpoint in OPTIONAL_ENDPOINTS:
            with self.subTest(endpoint=endpoint):
                transport = SyntheticTransport(endpoint, socket.timeout("synthetic read timeout"))
                with self.assertLogs("app.services.quantastor_api", level="WARNING") as logs:
                    payload = await self.fetch_transport(transport)
                self.assert_bundle(payload, missing=endpoint)
                self.assertEqual(len(logs.records), 1)
                self.assertIn(endpoint, logs.output[0])
                self.assertIn("TimeoutError", logs.output[0])
                self.assertTrue(transport.read_entered.is_set())

    async def test_fetch_all_supported_optional_transport_failures_degrade(self):
        for phase in ("open", "read"):
            for error in transport_errors():
                with self.subTest(phase=phase, error=type(error).__name__):
                    with self.assertLogs("app.services.quantastor_api", level="WARNING") as logs:
                        payload = await self.fetch_transport(SyntheticTransport("hwDiskEnum", error, phase=phase))
                    self.assert_bundle(payload, missing="hwDiskEnum")
                    self.assertIn(type(error).__name__, logs.output[0])

    async def test_fetch_all_optional_decode_failures_degrade(self):
        for body, charset, cause in ((b"\xff", "utf-8", "UnicodeDecodeError"),
                                     (b"[]", "unknown-synthetic-codec", "LookupError"),
                                     (b"not JSON", "utf-8", "JSONDecodeError")):
            with self.subTest(cause=cause):
                with self.assertLogs("app.services.quantastor_api", level="WARNING") as logs:
                    payload = await self.fetch_transport(SyntheticTransport("haGroupEnum", body=body, charset=charset))
                self.assert_bundle(payload, missing="haGroupEnum")
                self.assertIn(cause, logs.output[0])

    async def test_fetch_all_read_timeout_at_each_required_endpoint_fails(self):
        for endpoint in REQUIRED_ENDPOINTS:
            with self.subTest(endpoint=endpoint):
                error = socket.timeout("synthetic required timeout")
                with self.assertRaisesRegex(TrueNASAPIError, endpoint) as raised:
                    await self.fetch_transport(SyntheticTransport(endpoint, error))
                self.assertIs(raised.exception.__cause__, error)

    async def test_fetch_all_supported_required_transport_failures_remain_typed_failures(self):
        for phase in ("open", "read"):
            for error in transport_errors():
                with self.subTest(phase=phase, error=type(error).__name__):
                    with self.assertRaisesRegex(TrueNASAPIError, "physicalDiskEnum") as raised:
                        await self.fetch_transport(SyntheticTransport("physicalDiskEnum", error, phase=phase))
                    self.assertIs(raised.exception.__cause__, error)

    async def test_fetch_all_required_decode_failures_remain_typed_failures(self):
        for body, charset, cause in ((b"\xff", "utf-8", UnicodeDecodeError),
                                     (b"[]", "unknown-synthetic-codec", LookupError),
                                     (b"not JSON", "utf-8", json.JSONDecodeError)):
            with self.subTest(cause=cause.__name__):
                with self.assertRaisesRegex(TrueNASAPIError, "storagePoolEnum") as raised:
                    await self.fetch_transport(SyntheticTransport("storagePoolEnum", body=body, charset=charset))
                self.assertIsInstance(raised.exception.__cause__, cause)

    async def test_fetch_all_optional_http_error_body_read_failures_degrade(self):
        for endpoint in OPTIONAL_ENDPOINTS:
            for error in transport_errors():
                with self.subTest(endpoint=endpoint, error=type(error).__name__):
                    with self.assertLogs("app.services.quantastor_api", level="WARNING") as logs:
                        payload = await self.fetch_transport(SyntheticTransport(endpoint, error, status=503))
                    self.assert_bundle(payload, missing=endpoint)
                    self.assertIn(endpoint, logs.output[0])
                    self.assertIn(type(error).__name__, logs.output[0])

    async def test_fetch_all_required_http_error_body_read_failures_retain_status_and_cause(self):
        for error in transport_errors():
            with self.subTest(error=type(error).__name__):
                with self.assertRaisesRegex(TrueNASAPIError, r"physicalDiskEnum.*503") as raised:
                    await self.fetch_transport(SyntheticTransport("physicalDiskEnum", error, status=503))
                self.assertIs(raised.exception.__cause__, error)

    async def test_fetch_all_http_error_with_readable_body_retains_status_and_cause(self):
        with self.assertRaisesRegex(TrueNASAPIError, r"physicalDiskEnum.*403.*synthetic denial") as raised:
            await self.fetch_transport(SyntheticTransport("physicalDiskEnum", body=b"synthetic denial", status=403))
        self.assertIsInstance(raised.exception.__cause__, HTTPError)

    async def test_fetch_all_programmer_exceptions_are_not_optional_degradation(self):
        for phase in ("open", "read"):
            for error_type in (RuntimeError, TypeError, ValueError, KeyError, IndexError, AssertionError):
                with self.subTest(phase=phase, error=error_type.__name__):
                    error = error_type("synthetic programming failure")
                    with self.assertRaises(error_type) as raised:
                        await self.fetch_transport(SyntheticTransport("hwDiskEnum", error, phase=phase))
                    self.assertIs(raised.exception, error)

    async def test_fetch_all_transport_cancellation_remains_cancellation(self):
        for endpoint in ENDPOINT_FIELDS:
            with self.subTest(endpoint=endpoint):
                error = asyncio.CancelledError("synthetic cancellation")
                with self.assertRaises(asyncio.CancelledError):
                    await self.fetch_transport(SyntheticTransport(endpoint, error))

    async def test_fetch_all_caller_cancellation_during_optional_read_remains_cancellation(self):
        transport = SyntheticTransport("hwDiskEnum", phase="blocked")
        task = asyncio.create_task(self.fetch_transport(transport))
        try:
            self.assertTrue(await asyncio.to_thread(transport.read_entered.wait, 2))
            task.cancel()
        finally:
            transport.release_read.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_fetch_all_releases_raw_body_before_json_parse(self):
        # Observe the real request frame, not a Mock intermediary. Keep only
        # scalar measurements so the observer cannot extend the body lifetime.
        original_loads = json.loads
        for endpoint in ("physicalDiskEnum", "hwDiskEnum"):
            for size in (4096, 65536):
                for malformed in (False, True):
                    with self.subTest(endpoint=endpoint, size=size, malformed=malformed):
                        rows = [{"value": 37, "pad": "x" * size}]
                        body = json.dumps(rows).encode() + (b"!" if malformed else b"")
                        transport = SyntheticTransport(endpoint, body=body)
                        observations = []

                        def observe_parse(payload, *args, **kwargs):
                            frame = sys._getframe(1)
                            try:
                                self.assertIs(frame.f_code, QuantastorRESTClient._request_json.__code__)
                                if frame.f_locals["endpoint"] == endpoint:
                                    observations.append({
                                        "decoded_chars": len(payload),
                                        "raw_bytes": sum(len(value) for value in frame.f_locals.values()
                                                         if isinstance(value, bytes)),
                                        "closed": transport.responses[endpoint].closed,
                                    })
                            finally:
                                del frame
                            return original_loads(payload, *args, **kwargs)

                        with patch("app.services.quantastor_api.json.loads", new=observe_parse):
                            if malformed and endpoint in REQUIRED_ENDPOINTS:
                                with self.assertRaisesRegex(TrueNASAPIError, endpoint) as raised:
                                    await self.fetch_transport(transport)
                                self.assertIsInstance(raised.exception.__cause__, json.JSONDecodeError)
                            elif malformed:
                                with self.assertLogs("app.services.quantastor_api", level="WARNING") as logs:
                                    data = await self.fetch_transport(transport)
                                self.assert_bundle(data, missing=endpoint)
                                self.assertIn("JSONDecodeError", logs.output[0])
                            else:
                                data = await self.fetch_transport(transport)
                                for sibling, field in ENDPOINT_FIELDS.items():
                                    self.assertEqual(getattr(data, field), rows if sibling == endpoint
                                                     else [{"endpoint": sibling}])
                        self.assertEqual(observations, [{
                            "decoded_chars": len(body), "raw_bytes": 0, "closed": True,
                        }])

    async def test_fetch_all_parse_programmer_errors_and_cancellation_propagate(self):
        original_loads = json.loads
        for error_type in (RuntimeError, TypeError, ValueError, KeyError, IndexError,
                           AssertionError, asyncio.CancelledError):
            with self.subTest(error=error_type.__name__):
                error = error_type("synthetic parse failure")

                def fail_parse(payload, *args, **kwargs):
                    if "hwDiskEnum" in payload:
                        raise error
                    return original_loads(payload, *args, **kwargs)

                with patch("app.services.quantastor_api.json.loads", new=fail_parse):
                    with self.assertRaises(error_type) as raised:
                        await self.fetch_transport(SyntheticTransport())
                self.assertIs(raised.exception, error)

    async def test_fetch_all_real_client_success_preserves_all_sources(self):
        self.assert_bundle(await self.fetch_transport(SyntheticTransport()))

    async def test_fetch_all_real_client_accepts_declared_charset(self):
        payload = await self.fetch_transport(SyntheticTransport(
            "haGroupEnum", body='[{"name": "caf\u00e9"}]'.encode("latin-1"), charset="iso-8859-1",
        ))
        self.assertEqual(payload.ha_groups, [{"name": "caf\u00e9"}])
        self.assertEqual(payload.disks, [{"endpoint": "physicalDiskEnum"}])

    async def test_fetch_all_collects_endpoints_in_parallel(self) -> None:
        class TrackingClient(QuantastorRESTClient):
            def __init__(self) -> None:
                super().__init__(TrueNASConfig(api_user="admin", api_password="secret", platform="quantastor"))
                self.active_calls = 0
                self.max_active_calls = 0
                self.lock = threading.Lock()

            def _track(self, result):
                with self.lock:
                    self.active_calls += 1
                    self.max_active_calls = max(self.max_active_calls, self.active_calls)
                try:
                    time.sleep(0.02)
                    return result
                finally:
                    with self.lock:
                        self.active_calls -= 1

            def _fetch_required_list(self, endpoint: str):
                return self._track([{"endpoint": endpoint}])

            def _fetch_optional_list(self, endpoint: str):
                return self._track([{"endpoint": endpoint}])

        client = TrackingClient()

        payload = await client.fetch_all()

        self.assertGreater(client.max_active_calls, 1)
        self.assertEqual(payload.enclosures[0]["endpoint"], "storageSystemEnum")
        self.assertEqual(payload.disks[0]["endpoint"], "physicalDiskEnum")
        self.assertEqual(payload.pools[0]["endpoint"], "storagePoolEnum")
        self.assertEqual(payload.pool_devices[0]["endpoint"], "storagePoolDeviceEnum")
        self.assertEqual(payload.ha_groups[0]["endpoint"], "haGroupEnum")
        self.assertEqual(payload.hw_disks[0]["endpoint"], "hwDiskEnum")
        self.assertEqual(payload.hw_enclosures[0]["endpoint"], "hwEnclosureEnum")

    async def test_fetch_all_tolerates_an_appliance_with_no_storage_pools(self) -> None:
        responses = {
            "storageSystemEnum": [{"id": "sys-1", "name": "node-a"}],
            "physicalDiskEnum": [{"id": "disk-1", "serialNumber": "SERIAL-1"}],
            "storagePoolEnum": [],
            "storagePoolDeviceEnum": [],
            "haGroupEnum": [],
            "hwDiskEnum": [{"id": "hw-1"}],
            "hwEnclosureEnum": [{"id": "enc-1"}],
        }
        client = QuantastorRESTClient(
            TrueNASConfig(api_user="admin", api_password="secret", platform="quantastor")
        )

        with patch.object(
            QuantastorRESTClient,
            "_request_json",
            lambda self, endpoint, params=None: responses[endpoint],
        ):
            payload = await client.fetch_all()

        self.assertEqual(payload.pools, [])
        self.assertEqual([row["id"] for row in payload.disks], ["disk-1"])
        self.assertEqual([row["id"] for row in payload.systems], ["sys-1"])

    async def test_fetch_all_still_fails_when_required_disk_rows_are_missing(self) -> None:
        responses = {
            "storageSystemEnum": [{"id": "sys-1"}],
            "physicalDiskEnum": [],
            "storagePoolEnum": [],
            "storagePoolDeviceEnum": [],
            "haGroupEnum": [],
            "hwDiskEnum": [],
            "hwEnclosureEnum": [],
        }
        client = QuantastorRESTClient(
            TrueNASConfig(api_user="admin", api_password="secret", platform="quantastor")
        )

        with patch.object(
            QuantastorRESTClient,
            "_request_json",
            lambda self, endpoint, params=None: responses[endpoint],
        ):
            with self.assertRaisesRegex(TrueNASAPIError, "physicalDiskEnum returned no usable rows"):
                await client.fetch_all()

    def test_empty_pool_exemption_rejects_malformed_collection_payloads(self) -> None:
        client = QuantastorRESTClient(
            TrueNASConfig(api_user="admin", api_password="secret", platform="quantastor")
        )

        malformed_payloads = (
            None,
            "malformed",
            [1, None],
            [{"id": "pool-1"}, 1],
            {"result": [1]},
            {"result": [{"id": "pool-1"}, 1]},
            {"result": [{"id": "pool-1"}], "items": [{"id": "pool-2"}]},
            {"result": [], "items": []},
            {"foo": "bar"},
            {"foo": []},
        )
        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                with patch.object(client, "_request_json", return_value=payload):
                    with self.assertRaisesRegex(TrueNASAPIError, "storagePoolEnum returned no usable rows"):
                        client._fetch_required_list("storagePoolEnum")

    def test_pool_rows_accept_supported_collection_shapes(self) -> None:
        client = QuantastorRESTClient(
            TrueNASConfig(api_user="admin", api_password="secret", platform="quantastor")
        )

        supported_payloads = [
            ([], []),
            ([{"id": "pool-1"}], [{"id": "pool-1"}]),
        ]
        for wrapper in ("result", "list", "items", "objects", "data"):
            supported_payloads.extend(
                [
                    ({wrapper: []}, []),
                    ({wrapper: [{"id": "pool-1"}]}, [{"id": "pool-1"}]),
                ]
            )

        for payload, expected in supported_payloads:
            with self.subTest(payload=payload):
                with patch.object(client, "_request_json", return_value=payload):
                    self.assertEqual(client._fetch_required_list("storagePoolEnum"), expected)

    @patch("app.services.quantastor_api.build_tls_client_context")
    @patch("app.services.quantastor_api.urlopen_with_tls_config")
    def test_request_json_passes_tls_server_name_override(
        self,
        urlopen_with_tls_config_mock: MagicMock,
        build_tls_client_context_mock: MagicMock,
    ) -> None:
        client = QuantastorRESTClient(
            TrueNASConfig(
                host="https://10.13.37.10",
                api_user="admin",
                api_password="secret",
                platform="quantastor",
                tls_server_name="TrueNAS.gcs8.io",
            )
        )

        ssl_context = MagicMock()
        build_tls_client_context_mock.return_value = ssl_context

        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.headers.get_content_charset.return_value = "utf-8"
        response.read.return_value = b"[]"
        urlopen_with_tls_config_mock.return_value = response

        payload = client._request_json("storageSystemEnum", {"flags": 0})

        self.assertEqual(payload, [])
        self.assertEqual(urlopen_with_tls_config_mock.call_args.kwargs["server_hostname"], "TrueNAS.gcs8.io")


if __name__ == "__main__":
    unittest.main()
