import asyncio
import copy
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web

from core.config import PiggyError, Settings
from core.database import Database
from core.delivery import Message, QQError, QQTransport, Sender, message_key
from core.material import MaterialLibrary
from core.storage import MaterialUnavailable


def settings(**kwargs):
    return Settings.from_dict(
        {"image_host": {"provider": "QQ 素材库"}, "retry_base_delay": 0.1, **kwargs}
    )


class FakeTransport:
    """模拟素材库四个接口及群消息发送，按顺序消费预设的失败。"""

    def __init__(self, send_failures=(), material_failures=None, images=()):
        self.send_failures = list(send_failures)
        self.material_failures = material_failures or {}
        self.images = list(images)
        self.payloads = []
        self.calls = []
        self.local_uploads = 0
        self.counter = 0

    async def request(self, event, payload):
        self.payloads.append(copy.deepcopy(payload))
        if self.send_failures:
            raise self.send_failures.pop(0)
        return {"id": "sent"}

    async def upload_image(self, event, data):
        self.local_uploads += 1
        return "file-info"

    async def material(self, event, method, path, payload=None, expected=None):
        self.calls.append((method, path.split("?")[0]))
        failures = self.material_failures.get((method, path.split("?")[0]), [])
        if failures:
            raise failures.pop(0)
        if path.endswith("/prepare"):
            self.md5 = payload["md5"]
            self.file_name = payload["file_name"]
            return {"upload_id": "task", "presigned_url": "https://cos.example.com/put"}
        if path.endswith("/complete"):
            self.counter += 1
            image_id = f"img-{self.counter}"
            return {"image_id": image_id, "url": f"qqbot-media://image/v1/{image_id}"}
        if method == "GET":
            return {"images": self.images, "next_cursor": ""}
        return {}

    async def put_presigned(self, url, data, content_type):
        self.calls.append(("PUT", url))
        return self.md5

    def deleted(self):
        return [path.split("/")[-2] for method, path in self.calls if method == "DELETE"]


class MaterialDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root)
        await self.db.initialize()
        self.path = self.root / "pig.webp"
        self.path.write_bytes(b"pig image payload")
        self.event = SimpleNamespace(
            message_obj=SimpleNamespace(message_id="incoming"),
            get_group_id=lambda: "group",
        )
        self.local = Message("", (b"local card",), local=True)
        self.message = Message(
            "![pig]({{image:0}})", (self.path,), {"rows": []}, fallback=lambda: self.local
        )
        self.libraries = []

    async def asyncTearDown(self):
        for library in self.libraries:
            await library.close()
        self.temp.cleanup()

    def sender(self, transport, config=None, logger=None):
        config = config or settings()
        material = MaterialLibrary(config, transport, logger=logger)
        self.libraries.append(material)
        return Sender(config, self.db, None, transport, logger=logger, material=material)

    async def test_uploads_for_one_message_and_deletes_after_send(self):
        transport = FakeTransport()
        await self.sender(transport).send(self.event, "app", self.message)
        payload = transport.payloads[0]
        self.assertEqual(payload["msg_type"], 2)
        self.assertEqual(payload["markdown"]["content"], "![pig](qqbot-media://image/v1/img-1)")
        self.assertTrue(payload["markdown"]["force_verify_image_resource"])
        self.assertEqual(payload["keyboard"], {"rows": []})
        self.assertTrue(transport.file_name.startswith("piggy-"))
        self.assertTrue(transport.file_name.endswith(".webp"))
        self.assertEqual(transport.deleted(), ["img-1"])
        # 已完成的消息再次投递时不会重复上传。
        await self.sender(transport).send(self.event, "app", self.message)
        self.assertEqual(transport.counter, 1)

    async def test_expired_reference_reuploads_and_deletes_every_copy(self):
        transport = FakeTransport([QQError(40035009, 400)])
        await self.sender(transport).send(self.event, "app", self.message)
        self.assertEqual(len(transport.payloads), 2)
        self.assertIn("img-2", transport.payloads[1]["markdown"]["content"])
        self.assertEqual(transport.deleted(), ["img-1", "img-2"])

    async def test_failed_send_still_deletes_uploaded_material(self):
        transport = FakeTransport([QQError(11255, 400)])
        with self.assertRaises(QQError):
            await self.sender(transport).send(self.event, "app", self.message)
        self.assertEqual(transport.deleted(), ["img-1"])

    async def test_unavailable_library_falls_back_to_local_card(self):
        for error in (QQError(40035007, 400), QQError(0, 429), QQError(11253, 403)):
            with self.subTest(code=error.code, status=error.status):
                transport = FakeTransport(
                    material_failures={("POST", "/v2/media-images/prepare"): [error]}
                )
                logger = Mock()
                await self.sender(transport, logger=logger).send(
                    self.event, f"app-{error.code}-{error.status}", self.message
                )
                self.assertEqual(transport.payloads[-1]["msg_type"], 7)
                self.assertEqual(transport.local_uploads, 1)
                self.assertEqual(transport.deleted(), [])
                logger.warning.assert_called()

    async def test_unavailable_library_without_fallback_explains_reason(self):
        transport = FakeTransport(
            material_failures={("POST", "/v2/media-images/prepare"): [QQError(40035007, 400)]}
        )
        message = Message("![pig]({{image:0}})", (self.path,))
        with self.assertRaisesRegex(MaterialUnavailable, "100 张"):
            await self.sender(transport).send(self.event, "app", message)
        self.assertEqual(transport.payloads, [])

    async def test_expired_upload_task_is_prepared_again(self):
        transport = FakeTransport(
            material_failures={("POST", "/v2/media-images/complete"): [QQError(40035003, 400)]}
        )
        await self.sender(transport).send(self.event, "app", self.message)
        prepares = [c for c in transport.calls if c == ("POST", "/v2/media-images/prepare")]
        self.assertEqual(len(prepares), 2)
        self.assertEqual(len(transport.payloads), 1)

    async def test_delete_failure_is_logged_without_failing_reply(self):
        transport = FakeTransport(
            material_failures={
                ("DELETE", "/v2/media-images/img-1/delete"): [QQError(0, 0, uncertain=True)]
            }
        )
        logger = Mock()
        await self.sender(transport, logger=logger).send(self.event, "app", self.message)
        receipt = await self.db.delivery(message_key(self.event, "app"))
        self.assertTrue(receipt["done"])
        logger.warning.assert_called()

    async def test_first_use_sweeps_only_old_piggy_images_without_repeating_on_every_send(self):
        now = int(time.time())
        transport = FakeTransport(
            images=[
                {"image_id": "old-ours", "file_name": "piggy-a.png", "created_at": now - 3600},
                {"image_id": "new-ours", "file_name": "piggy-b.png", "created_at": now},
                {"image_id": "others", "file_name": "other.png", "created_at": now - 3600},
            ]
        )
        sender = self.sender(transport)
        await sender.send(self.event, "app", self.message)
        self.assertEqual(transport.deleted(), ["old-ours", "img-1"])
        second = Message("![pig]({{image:0}})", (self.path,))
        self.event.message_obj.message_id = "next"
        await sender.send(self.event, "app", second)
        self.assertEqual(
            [c for c in transport.calls if c[0] == "GET"], [("GET", "/v2/media-images")]
        )

    async def test_slow_startup_scan_does_not_block_reply(self):
        transport = FakeTransport()
        original = transport.material
        cancelled = asyncio.Event()

        async def slow_list(event, method, path, payload=None, expected=None):
            if method == "GET":
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return await original(event, method, path, payload, expected)

        transport.material = slow_list
        sender = self.sender(transport)
        with patch("core.material.FOREGROUND_BUDGET", 0.02):
            await asyncio.wait_for(sender.send(self.event, "app", self.message), 1)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(transport.payloads), 1)
        transport.material = original
        account = sender.material.accounts["app"]
        account.next_sweep = 0
        await sender.material._maintain(account, time.monotonic() + 1)
        self.assertIn(("GET", "/v2/media-images"), transport.calls)

    async def test_cleanup_budget_keeps_all_remaining_ids_for_retry(self):
        transport = FakeTransport([QQError(40035009, 400)])
        original = transport.material
        deletes = []

        async def slow_delete(event, method, path, payload=None, expected=None):
            if method == "DELETE":
                deletes.append(path)
                await asyncio.Event().wait()
            return await original(event, method, path, payload, expected)

        transport.material = slow_delete
        sender = self.sender(transport)
        with patch("core.material.FOREGROUND_BUDGET", 0.02):
            await asyncio.wait_for(sender.send(self.event, "app", self.message), 1)
        # Windows 定时器可能提前唤醒一次；验证总等待受限及全部 ID 被保留，
        # 不依赖 20 毫秒内恰好启动了几次网络调用。
        self.assertTrue(deletes)
        account = sender.material.accounts["app"]
        self.assertEqual(set(account.pending), {"img-1", "img-2"})
        transport.material = original
        for item in account.pending.values():
            item.due = 0
        await sender.material._maintain(account, time.monotonic() + 1)
        self.assertFalse(account.pending)
        self.assertTrue((await self.db.delivery(message_key(self.event, "app")))["done"])

    async def test_background_retry_recovers_without_another_message(self):
        transport = FakeTransport(
            material_failures={
                ("DELETE", "/v2/media-images/img-1/delete"): [QQError(0, 0, uncertain=True)]
            }
        )
        sender = self.sender(transport)
        recovered = asyncio.Event()
        with patch("core.material.MAINTENANCE_INTERVAL", 0.01):
            await sender.send(self.event, "app", self.message)
            account = sender.material.accounts["app"]
            self.assertIn("img-1", account.pending)
            original = transport.material

            async def observe_delete(event, method, path, payload=None, expected=None):
                result = await original(event, method, path, payload, expected)
                if method == "DELETE":
                    recovered.set()
                return result

            transport.material = observe_delete
            account.pending["img-1"].due = 0
            await asyncio.wait_for(recovered.wait(), 1)
            await sender.material.close()
        self.assertEqual(transport.deleted(), ["img-1", "img-1"])
        self.assertEqual(len(transport.payloads), 1)

    async def test_delete_retries_back_off_and_missing_counts_as_success(self):
        path = "/v2/media-images/failed/delete"
        transport = FakeTransport(
            material_failures={
                ("DELETE", path): [QQError(0, 429), QQError(0, 429), QQError(40035008, 400)]
            }
        )
        library = self.sender(transport).material
        clock = SimpleNamespace(monotonic=Mock(return_value=100.0), time=time.time)
        with patch("core.material.time", clock):
            await library.delete(self.event, "app", ["failed"])
            account = library.accounts["app"]
            self.assertEqual(account.pending["failed"].due, 130)
            await library._maintain(account, 101, scan=False)
            self.assertEqual(len(transport.deleted()), 1)
            clock.monotonic.return_value = 130
            await library._maintain(account, 131, scan=False)
            self.assertEqual(account.pending["failed"].due, 190)
            clock.monotonic.return_value = 190
            await library._maintain(account, 191, scan=False)
            self.assertFalse(account.pending)

    async def test_periodic_scan_finds_orphans_and_excludes_active_images(self):
        transport = FakeTransport()
        library = self.sender(transport).material
        await library.upload(self.event, "app", b"image", time.monotonic() + 1)
        account = library.accounts["app"]
        transport.images = [
            {"image_id": image_id, "file_name": name, "created_at": int(time.time()) - age}
            for image_id, name, age in (
                ("orphan", "piggy-orphan.png", 3600),
                ("img-1", "piggy-active.png", 3600),
                ("new", "piggy-new.png", 1),
                ("other", "other.png", 3600),
            )
        ]
        account.next_sweep = 0
        await library._maintain(account, time.monotonic() + 1)
        self.assertEqual(transport.deleted(), ["orphan"])
        self.assertEqual(sum(method == "GET" for method, _ in transport.calls), 2)

    async def test_concurrent_cleanup_skips_busy_account_and_isolates_accounts(self):
        transport = FakeTransport()
        library = self.sender(transport).material
        account = library._account(self.event, "app")
        async with account.lock:
            await asyncio.wait_for(library.delete(self.event, "app", ["first"]), 1)
            await library.delete(self.event, "other-app", ["second"])
        self.assertEqual(transport.deleted(), ["second"])
        self.assertEqual(set(account.pending), {"first"})
        self.assertFalse(library.accounts["other-app"].pending)

    async def test_shared_active_id_is_deleted_only_after_last_release(self):
        transport = FakeTransport()
        library = self.sender(transport).material
        account = library._account(self.event, "app")
        account.active["shared"] = 2
        await library.delete(self.event, "app", ["shared"])
        self.assertEqual(transport.deleted(), [])
        await library.delete(self.event, "app", ["shared"])
        self.assertEqual(transport.deleted(), ["shared"])

    async def test_expired_upload_does_not_start_cleanup_or_upload(self):
        transport = FakeTransport()
        library = self.sender(transport).material
        with self.assertRaises(PiggyError):
            await library.upload(self.event, "app", b"image", time.monotonic() - 1)
        self.assertEqual(transport.calls, [])
        self.assertIsNone(library.worker)

    async def test_close_cancels_background_network_call(self):
        transport = FakeTransport()
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def blocked(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        transport.material = blocked
        library = self.sender(transport).material
        with patch("core.material.MAINTENANCE_INTERVAL", 0.01):
            library._account(self.event, "app")
            worker = library.worker
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(library.close(), 1)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(worker.done())
        self.assertFalse(library.accounts)

    async def test_reupload_unavailable_also_falls_back_with_correct_sequence(self):
        for error in (QQError(40035007, 400), QQError(0, 429), QQError(11253, 403)):
            with self.subTest(code=error.code, status=error.status):
                transport = FakeTransport([QQError(40035009, 400), QQError(0, 0, uncertain=True)])
                original = transport.material

                async def unavailable_refresh(event, method, path, payload=None, expected=None):
                    if path.endswith("/prepare") and transport.counter:
                        raise error
                    return await original(event, method, path, payload, expected)

                transport.material = unavailable_refresh
                app_id = f"app-{error.code}-{error.status}"
                await self.sender(transport).send(self.event, app_id, self.message)
                self.assertEqual([p["msg_type"] for p in transport.payloads], [2, 7, 7])
                self.assertEqual([p["msg_seq"] for p in transport.payloads], [100, 101, 101])
                self.assertEqual(transport.local_uploads, 1)
                self.assertEqual(transport.deleted(), ["img-1"])
                self.assertTrue((await self.db.delivery(message_key(self.event, app_id)))["done"])


class MaterialCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root)
        await self.db.initialize()
        self.pig = self.root / "pig.webp"
        self.pig.write_bytes(b"pig image payload")
        self.other = self.root / "other.webp"
        self.other.write_bytes(b"another pig payload")
        self.event = SimpleNamespace(
            message_obj=SimpleNamespace(message_id="m-0"),
            get_group_id=lambda: "group",
        )
        self.config = Settings.from_dict(
            {
                "image_host": {"provider": "QQ 素材库", "qq": {"cache_minutes": 60}},
                "retry_base_delay": 0.1,
            }
        )
        self.libraries = []

    async def asyncTearDown(self):
        for library in self.libraries:
            await library.close()
        self.temp.cleanup()

    def sender(self, transport):
        self.library = MaterialLibrary(self.config, transport)
        self.libraries.append(self.library)
        return Sender(self.config, self.db, None, transport, material=self.library)

    async def send(self, sender, message_id, image, fallback=None):
        self.event.message_obj.message_id = message_id
        message = Message("![pig]({{image:0}})", (image,), fallback=fallback)
        await sender.send(self.event, "app", message)

    async def test_pig_is_reused_until_expiry_and_cards_are_not_cached(self):
        transport = FakeTransport()
        sender = self.sender(transport)
        await self.send(sender, "m-1", self.pig)
        await self.send(sender, "m-2", self.pig)
        self.assertEqual(transport.counter, 1)
        self.assertIn("img-1", transport.payloads[1]["markdown"]["content"])
        self.assertEqual(transport.deleted(), [])
        # 渲染卡片即用即删，不进入缓存。
        await self.send(sender, "m-3", b"card bytes")
        self.assertEqual(transport.deleted(), ["img-2"])
        self.library.accounts["app"].cache[
            hashlib.md5(b"pig image payload").hexdigest()
        ].expires = 0
        await self.send(sender, "m-4", self.pig)
        self.assertEqual(transport.counter, 3)
        self.assertEqual(transport.deleted(), ["img-2", "img-1"])

    async def test_broken_cached_reference_is_replaced(self):
        transport = FakeTransport()
        sender = self.sender(transport)
        await self.send(sender, "m-1", self.pig)
        transport.send_failures = [QQError(40035009, 400)]
        await self.send(sender, "m-2", self.pig)
        self.assertIn("img-2", transport.payloads[-1]["markdown"]["content"])
        self.assertEqual(transport.deleted(), ["img-1"])
        await self.send(sender, "m-3", self.pig)
        self.assertEqual(transport.counter, 2)

    async def test_full_library_releases_cache_and_retries_once(self):
        transport = FakeTransport()
        sender = self.sender(transport)
        await self.send(sender, "m-1", self.pig)
        transport.material_failures[("POST", "/v2/media-images/prepare")] = [QQError(40035007, 400)]
        await self.send(sender, "m-2", self.other, fallback=lambda: self.fail("不应降级"))
        self.assertEqual(transport.payloads[-1]["msg_type"], 2)
        self.assertIn("img-2", transport.payloads[-1]["markdown"]["content"])
        self.assertEqual(transport.deleted(), ["img-1"])

    async def test_close_deletes_cached_images(self):
        transport = FakeTransport()
        await self.send(self.sender(transport), "m-1", self.pig)
        await self.library.close()
        self.assertEqual(transport.deleted(), ["img-1"])

    async def test_purge_cache_or_everything_skips_images_in_flight(self):
        now = int(time.time())
        images = [
            {"image_id": "ours-old", "file_name": "piggy-a.png", "created_at": now - 3600},
            {"image_id": "ours-new", "file_name": "piggy-b.png", "created_at": now},
            {"image_id": "busy", "file_name": "piggy-c.png", "created_at": now},
            {"image_id": "others", "file_name": "other.png", "created_at": now},
        ]
        transport = FakeTransport(images=images)
        library = MaterialLibrary(self.config, transport)
        self.libraries.append(library)
        account = library._account(self.event, "app")
        account.active["busy"] = 1
        result = await library.purge(self.event, "app", time.monotonic() + 5)
        self.assertEqual(result, {"deleted": 2, "failed": 0, "skipped": 1})
        self.assertEqual(transport.deleted(), ["ours-old", "ours-new"])
        transport.calls.clear()
        result = await library.purge(self.event, "app", time.monotonic() + 5, everything=True)
        self.assertEqual(result["skipped"], 1)
        self.assertIn("others", transport.deleted())
        self.assertNotIn("busy", transport.deleted())


class MaterialTransportTests(unittest.IsolatedAsyncioTestCase):
    def event(self):
        http = SimpleNamespace(
            check_session=AsyncMock(), is_sandbox=False, _headers={"Authorization": "QQBot t"}
        )
        return SimpleNamespace(bot=SimpleNamespace(api=SimpleNamespace(_http=http)))

    async def test_material_calls_use_bot_auth_and_accept_empty_delete_body(self):
        transport = QQTransport(20)
        transport.session = Mock()

        class Response:
            status = 200
            content = SimpleNamespace(iter_chunked=self.empty)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        transport.session.delete.return_value = Response()
        self.assertEqual(
            await transport.material(self.event(), "DELETE", "/v2/media-images/a/delete"), {}
        )
        args = transport.session.delete.call_args
        self.assertEqual(args.args[0], "https://api.bot.qq.com/v2/media-images/a/delete")
        self.assertEqual(args.kwargs["headers"], {"Authorization": "QQBot t"})

    @staticmethod
    async def empty(size):
        for chunk in ():
            yield chunk

    async def test_presigned_put_sends_exact_type_and_returns_etag(self):
        data = b"pig bytes"
        received = {}

        async def put(request):
            received["type"] = request.headers["Content-Type"]
            received["body"] = await request.read()
            return web.Response(headers={"ETag": f'"{hashlib.md5(data).hexdigest().upper()}"'})

        app = web.Application()
        app.router.add_put("/put", put)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        transport = QQTransport(20)
        try:
            etag = await transport.put_presigned(f"http://127.0.0.1:{port}/put", data, "image/webp")
        finally:
            await transport.close()
            await runner.cleanup()
        self.assertEqual(etag, hashlib.md5(data).hexdigest())
        self.assertEqual(received, {"type": "image/webp", "body": data})


class MaterialUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_pages_through_library_and_splits_owners(self):
        now = int(time.time())
        pages = {
            "": {
                "images": [
                    {"image_id": "a", "file_name": "piggy-a.png", "created_at": now},
                    {"image_id": "b", "file_name": "piggy-b.png", "created_at": now - 3600},
                ],
                "next_cursor": "page-2",
                "total": 4,
            },
            "page-2": {
                "images": [{"image_id": "c", "file_name": "other.png", "created_at": now}],
                "next_cursor": "",
                "total": 4,
            },
        }
        paths = []

        async def material(event, method, path, payload=None, expected=None):
            paths.append((method, path))
            return pages[path.partition("?cursor=")[2]]

        transport = SimpleNamespace(material=material)
        usage = await MaterialLibrary(settings(), transport).usage(
            object(), "app", time.monotonic() + 5
        )
        # total 以接口口径为准，包含未列出的其他素材。
        self.assertEqual(usage, {"total": 4, "ours": 2, "cached": 0, "stale": 1, "others": 2})
        self.assertEqual(
            paths, [("GET", "/v2/media-images"), ("GET", "/v2/media-images?cursor=page-2")]
        )

    async def test_usage_errors_become_readable_notices(self):
        cases = (
            (QQError(0, 429), "频率限制"),
            (QQError(11253, 403), "权限"),
            (QQError(500, 500, uncertain=True), "错误码 500"),
        )
        for error, text in cases:
            with self.subTest(text=text):
                transport = SimpleNamespace(material=AsyncMock(side_effect=error))
                with self.assertRaisesRegex(PiggyError, text):
                    await MaterialLibrary(settings(), transport).usage(
                        object(), "app", time.monotonic() + 5
                    )
        with self.assertRaisesRegex(PiggyError, "超时"):
            await MaterialLibrary(settings(), SimpleNamespace(material=AsyncMock())).usage(
                object(), "app", time.monotonic() - 1
            )
