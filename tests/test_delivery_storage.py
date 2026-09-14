import base64
import copy
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import boto3
from aiohttp import web
from botocore.exceptions import (
    EndpointConnectionError,
    ParamValidationError,
    ProxyConnectionError,
    SSLError,
)
from botocore.stub import Stubber

from core.config import PiggyError, Settings
from core.database import Database
from core.delivery import Message, QQError, QQTransport, Sender
from core.storage import HttpHost, ImagePublisher, S3Host, UploadError, response_json


def settings(**kwargs):
    return Settings.from_dict(
        {
            "endpoint": "https://account.r2.cloudflarestorage.com",
            "bucket": "pigs",
            "access_key": "key",
            "secret_key": "secret",
            "public_base_url": "https://images.example.com",
            "retry_base_delay": 0.1,
            **kwargs,
        }
    )


class FakeHost:
    def __init__(self, failures=()):
        self.failures = list(failures)
        self.calls = []

    async def upload(self, data, key, content_type):
        self.calls.append((data, key, content_type))
        if self.failures:
            raise self.failures.pop(0)
        return f"https://images.example.com/{key}"

    async def close(self):
        pass


class FakeTransport:
    def __init__(self, failures=()):
        self.failures = list(failures)
        self.payloads = []

    async def request(self, event, payload):
        self.payloads.append(copy.deepcopy(payload))
        if self.failures:
            raise self.failures.pop(0)
        return {"id": "sent-message"}


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root)
        await self.db.initialize()
        self.path = self.root / "pig.png"
        self.path.write_bytes(b"test image payload")
        self.event = SimpleNamespace(
            message_obj=SimpleNamespace(message_id="incoming"),
            get_group_id=lambda: "group",
        )
        self.message = Message("![pig]({{image:0}})", (self.path,), {"content": {"rows": []}})
        self.sleep = patch("asyncio.sleep", new_callable=AsyncMock)
        self.sleep.start()

    async def asyncTearDown(self):
        self.sleep.stop()
        self.temp.cleanup()

    async def test_image_rejection_refreshes_host_and_is_bounded(self):
        config = settings(image_retry_count=2)
        host = FakeHost()
        transport = FakeTransport([QQError(304010, 400), QQError(40034004, 400)])
        sender = Sender(config, self.db, ImagePublisher(config, self.db, host), transport)
        await sender.send(self.event, "app", self.message)
        self.assertEqual(len(transport.payloads), 3)
        self.assertEqual(len(host.calls), 2)
        self.assertEqual([p["msg_seq"] for p in transport.payloads], [100, 101, 102])
        for payload in transport.payloads:
            self.assertTrue(payload["markdown"]["force_verify_image_resource"])
            self.assertNotIn("content", payload)
            self.assertNotIn("{{image:", payload["markdown"]["content"])
            self.assertEqual(payload["msg_id"], "incoming")
        await sender.send(self.event, "app", self.message)
        self.assertEqual(len(transport.payloads), 3)

    async def test_zero_retries_means_one_attempt_and_no_reupload(self):
        config = settings(image_retry_count=0)
        host = FakeHost()
        transport = FakeTransport([QQError(40034004, 400)])
        sender = Sender(config, self.db, ImagePublisher(config, self.db, host), transport)
        with self.assertRaises(QQError):
            await sender.send(self.event, "app", self.message)
        self.assertEqual(len(transport.payloads), 1)
        self.assertEqual(len(host.calls), 1)

    async def test_persistent_image_failure_stops_at_configured_budget(self):
        config = settings(image_retry_count=2)
        host = FakeHost()
        transport = FakeTransport([QQError(40034004, 400)] * 3)
        sender = Sender(config, self.db, ImagePublisher(config, self.db, host), transport)
        with self.assertRaises(QQError):
            await sender.send(self.event, "app", self.message)
        self.assertEqual(len(transport.payloads), 3)
        self.assertEqual(len(host.calls), 2)

    async def test_ambiguous_failure_preserves_sequence_and_dedup_is_success(self):
        config = settings()
        transport = FakeTransport([QQError(0, 0, True), QQError(40054005, 400)])
        sender = Sender(config, self.db, ImagePublisher(config, self.db, FakeHost()), transport)
        await sender.send(self.event, "app", self.message)
        self.assertEqual([p["msg_seq"] for p in transport.payloads], [100, 100])
        await sender.send(self.event, "app", self.message)
        self.assertEqual(len(transport.payloads), 2)

    async def test_restart_keeps_last_sequence_after_explicit_image_rejection(self):
        config = settings(image_retry_count=0)
        host = FakeHost()
        one = FakeTransport([QQError(304010, 400)])
        with self.assertRaises(QQError):
            await Sender(config, self.db, ImagePublisher(config, self.db, host), one).send(
                self.event, "app", self.message
            )
        two = FakeTransport()
        reopened = Database(self.root)
        await reopened.initialize()
        await Sender(config, reopened, ImagePublisher(config, reopened, host), two).send(
            self.event, "app", self.message
        )
        self.assertEqual(two.payloads[0]["msg_seq"], 101)

    async def test_nonretryable_auth_content_and_expired_errors_do_not_loop(self):
        config = settings()
        for index, code in enumerate((40034006, 40034005, 40034127)):
            event = SimpleNamespace(
                message_obj=SimpleNamespace(message_id=f"event-{index}"),
                get_group_id=lambda: "group",
            )
            transport = FakeTransport([QQError(code, 400)])
            sender = Sender(config, self.db, ImagePublisher(config, self.db, FakeHost()), transport)
            with self.assertRaises(QQError):
                await sender.send(event, "app", self.message)
            self.assertEqual(len(transport.payloads), 1)

    async def test_upload_budget_cache_expiry_and_provider_changes(self):
        config = settings(upload_retry_count=2)
        host = FakeHost([UploadError("temporary", True), UploadError("temporary", True)])
        publisher = ImagePublisher(config, self.db, host)
        url = await publisher.publish(self.path)
        self.assertEqual(len(host.calls), 3)
        await publisher.publish(self.path)
        self.assertEqual(len(host.calls), 3)
        await self.db.run(lambda c: c.execute("UPDATE image_cache SET uploaded_at=0"))
        await publisher.publish(self.path)
        self.assertEqual(len(host.calls), 4)
        other = ImagePublisher(settings(bucket="another"), self.db, host)
        await other.publish(self.path)
        self.assertEqual(len(host.calls), 5)
        self.assertIn(hashlib.sha256(self.path.read_bytes()).hexdigest(), url)

    async def test_permanent_upload_failure_never_sends_qq_and_deadline_stops_work(
        self,
    ):
        config = settings()
        host = FakeHost([UploadError("forbidden")])
        transport = FakeTransport()
        sender = Sender(config, self.db, ImagePublisher(config, self.db, host), transport)
        with self.assertRaises(UploadError):
            await sender.send(self.event, "app", self.message)
        self.assertEqual(len(host.calls), 1)
        self.assertFalse(transport.payloads)
        with self.assertRaises(PiggyError):
            await sender.send(self.event, "app", self.message, time.monotonic() - 1)
        self.assertEqual(len(host.calls), 1)


class HttpAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received = []
        app = web.Application()

        async def upload(request):
            if request.content_type == "application/json":
                body = await request.json()
                self.received.append((dict(request.headers), body))
            else:
                form = await request.post()
                field = form.get("file", form.get("source"))
                self.received.append((dict(request.headers), dict(form), field.file.read()))
            return web.json_response(
                {
                    "status": True,
                    "data": {"links": {"url": "https://images.example.com/pig.png"}},
                }
            )

        app.router.add_post("/upload", upload)

        async def fail(request):
            return web.json_response({"status": False}, status=200)

        async def busy(request):
            return web.Response(status=503)

        app.router.add_post("/fail", fail)
        app.router.add_post("/busy", busy)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.url = f"http://127.0.0.1:{self.site._server.sockets[0].getsockname()[1]}"
        self.hosts = []

    async def asyncTearDown(self):
        for host in self.hosts:
            await host.close()
        await self.runner.cleanup()

    def host(self, **kwargs):
        host = HttpHost(settings(provider="http", upload_url=self.url + "/upload", **kwargs))
        self.hosts.append(host)
        return host

    async def test_lsky_style_multipart_auth_fields_and_response_path(self):
        host = self.host(
            upload_headers={"Authorization": "Bearer test-token"},
            upload_fields={"strategy_id": 1},
            success_path="status",
        )
        url = await host.upload(b"image", "pig.png", "image/png")
        self.assertEqual(url, "https://images.example.com/pig.png")
        self.assertEqual(self.received[0][0]["Authorization"], "Bearer test-token")
        self.assertEqual(self.received[0][1]["strategy_id"], "1")
        self.assertEqual(self.received[0][2], b"image")

    async def test_custom_base64_json_transport(self):
        host = self.host(
            upload_mode="json_base64",
            file_field="image",
            upload_fields={"quality": 90, "private": False},
        )
        await host.upload(b"binary\0data", "pig.png", "image/png")
        self.assertEqual(base64.b64decode(self.received[0][1]["image"]), b"binary\0data")
        self.assertEqual(self.received[0][1]["quality"], 90)
        self.assertIs(self.received[0][1]["private"], False)

    async def test_invalid_business_status_and_retryable_http_status(self):
        host = HttpHost(
            settings(provider="http", upload_url=self.url + "/fail", success_path="status")
        )
        self.hosts.append(host)
        with self.assertRaises(UploadError) as error:
            await host.upload(b"a", "a.png", "image/png")
        self.assertFalse(error.exception.retryable)
        busy = HttpHost(settings(provider="http", upload_url=self.url + "/busy"))
        self.hosts.append(busy)
        with self.assertRaises(UploadError) as error:
            await busy.upload(b"a", "a.png", "image/png")
        self.assertTrue(error.exception.retryable)


class S3Tests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_failures_keep_reason_stage_and_redact_credentials(self):
        cfg = settings(access_key="ACCESS_PRIVATE", secret_key="SECRET_PRIVATE")
        cases = [
            (
                SSLError(endpoint_url=cfg.endpoint, error="CERTIFICATE_VERIFY_FAILED"),
                False,
                "TLS/SSL",
            ),
            (
                SSLError(endpoint_url=cfg.endpoint, error="EOF occurred in violation of protocol"),
                True,
                "TLS/SSL",
            ),
            (
                ProxyConnectionError(proxy_url="http://proxy-user:proxy-password@proxy.local:8080"),
                True,
                "代理",
            ),
            (ParamValidationError(report='Invalid bucket "SECRET_PRIVATE"'), False, "参数校验"),
            (
                EndpointConnectionError(
                    endpoint_url=cfg.endpoint, error=OSError("DNS resolution failed")
                ),
                True,
                "网络",
            ),
        ]
        for error, retryable, hint in cases:
            host = S3Host(cfg)
            host.client = Mock()
            host.client.put_object.side_effect = error
            host.directories_ready = True
            with self.assertRaises(UploadError) as raised:
                await host.upload(b"image", "piggy/a.png", "image/png")
            failure = raised.exception
            self.assertEqual(failure.retryable, retryable)
            self.assertIn(hint, str(failure))
            self.assertIn(type(error).__name__, failure.diagnostic)
            self.assertIn("stage=put_object", failure.diagnostic)
            self.assertIn("botocore=", failure.diagnostic)
            for secret in ("ACCESS_PRIVATE", "SECRET_PRIVATE", "proxy-user", "proxy-password"):
                self.assertNotIn(secret, failure.diagnostic)
            if isinstance(error, EndpointConnectionError):
                self.assertIn("DNS resolution failed", failure.diagnostic)
        with patch(
            "boto3.client", side_effect=TypeError("unexpected keyword request_checksum_calculation")
        ):
            with self.assertRaises(UploadError) as raised:
                await S3Host(cfg).upload(b"image", "a.png", "image/png")
            self.assertIn("stage=create_client", raised.exception.diagnostic)
            self.assertIn("request_checksum_calculation", raised.exception.diagnostic)

    async def test_real_s3_http_request_and_xml_error_keep_service_code(self):
        requests = []

        async def upload(request):
            requests.append((request.path, await request.read(), dict(request.headers)))
            if request.path.endswith("/"):
                return web.Response(status=200)
            return web.Response(
                status=403,
                text="<Error><Code>SignatureDoesNotMatch</Code><Message>signature invalid</Message><RequestId>r2-request-123</RequestId></Error>",
                headers={"x-amz-request-id": "r2-request-123"},
                content_type="application/xml",
            )

        app = web.Application()
        app.router.add_put("/{path:.*}", upload)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        host = S3Host(settings(endpoint=f"http://127.0.0.1:{port}"))
        try:
            # Local protocol fixture only; production Settings.check_host requires HTTPS.
            with patch.dict("os.environ", {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
                with self.assertRaises(UploadError) as raised:
                    await host.upload(b"image", "piggy/a.png", "image/png")
            self.assertIn("SignatureDoesNotMatch", str(raised.exception))
            self.assertIn("request_id=r2-request-123", raised.exception.diagnostic)
            self.assertIn("http_status=403", raised.exception.diagnostic)
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(len(requests), 3)
            self.assertEqual(
                [r[:2] for r in requests[:2]],
                [("/pigs/piggy/assets/", b""), ("/pigs/piggy/temp/", b"")],
            )
            self.assertEqual(requests[-1][:2], ("/pigs/piggy/a.png", b"image"))
            self.assertIn("AWS4-HMAC-SHA256", requests[0][2]["Authorization"])
            self.assertNotIn("x-amz-acl", {k.lower() for k in requests[0][2]})
        finally:
            await host.close()
            await runner.cleanup()

    async def test_r2_client_disables_hidden_retries_and_uses_auto_region(self):
        host = S3Host(settings())
        client = boto3.client(
            "s3",
            endpoint_url=host.settings.endpoint,
            region_name="auto",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        with Stubber(client) as stub, patch("boto3.client", return_value=client) as factory:
            stub.add_response("put_object", {"ETag": "folder"})
            stub.add_response("put_object", {"ETag": "folder"})
            stub.add_response("put_object", {"ETag": "hash"})
            await host.upload(b"image", "piggy/image.png", "image/png")
            config = factory.call_args.kwargs["config"]
            self.assertEqual(config.retries, {"total_max_attempts": 1})
            self.assertEqual(factory.call_args.kwargs["region_name"], "auto")
            self.assertEqual(config.s3["addressing_style"], "path")
        await host.close()

    async def test_s3_auth_errors_are_permanent_and_server_errors_retryable(self):
        host = S3Host(settings())
        host.client = boto3.client(
            "s3",
            endpoint_url=host.settings.endpoint,
            region_name="auto",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        with Stubber(host.client) as stub:
            stub.add_client_error(
                "put_object", service_error_code="AccessDenied", http_status_code=403
            )
            stub.add_client_error("put_object", service_error_code="SlowDown", http_status_code=503)
            with self.assertRaises(UploadError) as error:
                await host.upload(b"image", "piggy/a.png", "image/png")
            self.assertFalse(error.exception.retryable)
            with self.assertRaises(UploadError) as error:
                await host.upload(b"image", "piggy/a.png", "image/png")
            self.assertTrue(error.exception.retryable)
        await host.close()

    async def test_r2_put_object_uses_no_acl_and_returns_public_domain(self):
        host = S3Host(settings())
        client = boto3.client(
            "s3",
            region_name="auto",
            endpoint_url=host.settings.endpoint,
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        host.client = client
        expected = {
            "Bucket": "pigs",
            "Key": "piggy/image.png",
            "Body": b"image",
            "ContentType": "image/png",
            "CacheControl": "public, max-age=31536000, immutable",
        }
        with Stubber(client) as stub:
            for prefix in ("piggy/assets/", "piggy/temp/"):
                stub.add_response(
                    "put_object",
                    {"ETag": "folder"},
                    {
                        "Bucket": "pigs",
                        "Key": prefix,
                        "Body": b"",
                        "ContentType": "application/x-directory",
                        "CacheControl": "no-store",
                    },
                )
            stub.add_response("put_object", {"ETag": "hash"}, expected)
            self.assertEqual(
                await host.upload(b"image", "piggy/image.png", "image/png"),
                "https://images.example.com/piggy/image.png",
            )
            stub.assert_no_pending_responses()
        await host.close()


class RawQQResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_json_and_response_limit(self):
        async def chunks(size):
            for piece in [b'{"data":', b'{"url":', b'"https://images.example.com/pig.png"}}']:
                yield piece

        response = SimpleNamespace(content=SimpleNamespace(iter_chunked=chunks))
        self.assertEqual(
            (await response_json(response))["data"]["url"], "https://images.example.com/pig.png"
        )

        async def large(size):
            for _ in range(17):
                yield b"x" * 65536

        with self.assertRaises(ValueError):
            await response_json(SimpleNamespace(content=SimpleNamespace(iter_chunked=large)))

    async def test_preserves_numeric_error_code_ignores_error_message_and_charset(self):
        async def chunks(size):
            yield b'{"err_code":304010,"message":"arbitrary localized text"}'

        response = SimpleNamespace(
            status=400,
            content=SimpleNamespace(iter_chunked=chunks),
        )

        class ContextManager:
            async def __aenter__(self):
                return response

            async def __aexit__(self, *args):
                return False

        captured = []

        class Session:
            def post(self, url, **kwargs):
                captured.append((url, kwargs))
                return ContextManager()

        transport = QQTransport(20)
        transport.session = Session()
        http = SimpleNamespace(
            check_session=AsyncMock(),
            is_sandbox=False,
            _headers={
                "Authorization": "QQBot token",
                "X-Union-Appid": "app",
                "unrelated": "omit",
            },
        )
        event = SimpleNamespace(
            bot=SimpleNamespace(api=SimpleNamespace(_http=http)),
            get_group_id=lambda: "group",
        )
        with self.assertRaises(QQError) as error:
            await transport.request(event, {"msg_type": 2})
        self.assertEqual(error.exception.code, 304010)
        self.assertFalse(error.exception.uncertain)
        self.assertNotIn("unrelated", captured[0][1]["headers"])
        self.assertFalse(captured[0][1]["allow_redirects"])


if __name__ == "__main__":
    unittest.main()
