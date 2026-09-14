import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from botocore.exceptions import ClientError
from PIL import Image
from test_delivery_storage import FakeHost, FakeTransport, settings

from core.avatars import Avatars
from core.config import PiggyError, Settings
from core.database import Database
from core.delivery import Message, QQError, QQTransport, Sender
from core.rendering import clean_cards
from core.storage import ImagePublisher, S3Host, UploadError


class ModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root)
        await self.db.initialize()
        self.event = SimpleNamespace(
            message_obj=SimpleNamespace(message_id="event"),
            get_group_id=lambda: "group",
        )
        self.transport = FakeTransport()
        self.transport.upload_image = AsyncMock(return_value="file-info")
        self.publisher = SimpleNamespace(
            publish=AsyncMock(side_effect=AssertionError("must not use host"))
        )
        self.config = Settings(upload_retry_count=2, image_retry_count=2)
        self.sender = Sender(self.config, self.db, self.publisher, self.transport)
        self.message = Message("", (b"png-data",), local=True)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_local_without_host_is_one_media_message_without_md_or_buttons(self):
        await self.sender.send(self.event, "app", self.message)
        await self.sender.send(self.event, "app", self.message)
        self.transport.upload_image.assert_awaited_once_with(self.event, b"png-data")
        self.publisher.publish.assert_not_awaited()
        self.assertEqual(
            self.transport.payloads,
            [
                {
                    "msg_type": 7,
                    "msg_id": "event",
                    "msg_seq": 100,
                    "media": {"file_info": "file-info"},
                }
            ],
        )

    async def test_upload_retries_are_bounded_and_do_not_send_on_failure(self):
        self.transport.upload_image.side_effect = QQError(0, 0, uncertain=True)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaisesRegex(PiggyError, "QQ 本地图片上传失败.*3/3"):
                await self.sender.send(self.event, "app", self.message)
        self.assertEqual(self.transport.upload_image.await_count, 3)
        self.assertFalse(self.transport.payloads)

    async def test_upload_auth_failure_is_not_retried(self):
        self.transport.upload_image.side_effect = QQError(123, 401)
        with self.assertRaisesRegex(PiggyError, "123.*401.*1/3"):
            await self.sender.send(self.event, "app", self.message)
        self.assertEqual(self.transport.upload_image.await_count, 1)

    async def test_local_uncertain_delivery_reuses_sequence_without_reupload(self):
        self.transport.failures = [QQError(0, 0, True), QQError(40054005, 400)]
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await self.sender.send(self.event, "app", self.message)
        self.assertEqual([p["msg_seq"] for p in self.transport.payloads], [100, 100])
        self.assertEqual(self.transport.upload_image.await_count, 1)

    async def test_local_invalid_media_refreshes_only_qq_upload(self):
        self.transport.failures = [QQError(304080, 400)]
        self.transport.upload_image.side_effect = ["old", "new"]
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await self.sender.send(self.event, "app", self.message)
        self.assertEqual([p["msg_seq"] for p in self.transport.payloads], [100, 101])
        self.assertEqual(self.transport.payloads[-1]["media"], {"file_info": "new"})
        self.publisher.publish.assert_not_awaited()

    async def test_expired_deadline_prevents_qq_upload(self):
        with self.assertRaises(PiggyError):
            await self.sender.send(self.event, "app", self.message, time.monotonic() - 1)
        self.transport.upload_image.assert_not_awaited()

    async def test_temp_cache_rotates_keys_and_does_not_alias_permanent_assets(self):
        host = FakeHost()
        publisher = ImagePublisher(settings(temp_cache_hours=12), self.db, host)
        asset = self.root / "pig.png"
        asset.write_bytes(b"same-png")
        with patch("core.storage.time.time", return_value=1000000):
            first = await publisher.publish(b"same-png")
            self.assertEqual(await publisher.publish(b"same-png"), first)
            permanent = await publisher.publish(asset)
        self.assertEqual(len(host.calls), 2)
        self.assertIn("/temp/", first)
        self.assertIn("/assets/", permanent)
        with patch("core.storage.time.time", return_value=1000000 + 43200):
            second = await publisher.publish(b"same-png")
        self.assertNotEqual(first, second)
        self.assertEqual(len(host.calls), 3)

    async def test_temp_s3_cache_control_is_short_and_asset_cache_is_long(self):
        host = S3Host(settings())
        host.client = Mock()
        await host.upload(b"png", "piggy/temp/0/p.png", "image/png")
        self.assertEqual(
            host.client.put_object.call_args.kwargs["CacheControl"], "public, max-age=3600"
        )
        await host.upload(b"png", "piggy/assets/p.png", "image/png")
        self.assertIn("immutable", host.client.put_object.call_args.kwargs["CacheControl"])

    async def test_s3_creates_both_fixed_directories_before_images_once(self):
        host = S3Host(settings(key_prefix="ignored-old-path"))
        host.client = Mock()
        await host.upload(b"png", "piggy/assets/p.png", "image/png")
        await host.upload(b"png", "piggy/temp/0/p.png", "image/png")
        calls = host.client.put_object.call_args_list
        self.assertEqual(
            [c.kwargs["Key"] for c in calls],
            [
                "piggy/assets/",
                "piggy/temp/",
                "piggy/assets/p.png",
                "piggy/temp/0/p.png",
            ],
        )
        for call in calls[:2]:
            self.assertEqual(call.kwargs["Body"], b"")
            self.assertNotIn("ACL", call.kwargs)

    async def test_directory_failure_identifies_stage_and_retry_finishes_initialization(self):
        host = S3Host(settings())
        host.client = Mock()
        host.client.put_object.side_effect = [
            {},
            ClientError(
                {"Error": {"Code": "SlowDown"}, "ResponseMetadata": {"HTTPStatusCode": 503}},
                "PutObject",
            ),
            {},
            {},
            {},
        ]
        with self.assertRaises(UploadError) as error:
            await host.upload(b"png", "piggy/assets/p.png", "image/png")
        self.assertTrue(error.exception.retryable)
        self.assertIn("stage=create_directory:piggy/temp/", error.exception.diagnostic)
        self.assertFalse(host.directories_ready)
        await host.upload(b"png", "piggy/assets/p.png", "image/png")
        self.assertTrue(host.directories_ready)
        self.assertEqual(host.client.put_object.call_count, 5)

    async def test_qq_file_upload_contract_does_not_send_active_message(self):
        transport = QQTransport(20)
        transport._request = AsyncMock(return_value={"file_info": "qq-info"})
        self.assertEqual(await transport.upload_image(self.event, b"image"), "qq-info")
        args = transport._request.await_args.args
        self.assertEqual(args[1], {"file_type": 1, "file_data": "aW1hZ2U=", "srv_send_msg": False})
        self.assertEqual(args[2:], ("files", "file_info"))
        with self.assertRaises(PiggyError):
            await transport.upload_image(self.event, b"")

    async def test_qq_upload_posts_to_group_files_and_accepts_file_info_without_message_id(self):
        class Response:
            status = 200

            def __init__(self):
                self.content = self

            async def iter_chunked(self, size):
                yield b'{"file_info":"result-info","ttl":300}'

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        session = Mock()
        session.post.return_value = Response()
        transport = QQTransport(20)
        transport.session = session
        self.event.bot = SimpleNamespace(
            api=SimpleNamespace(
                _http=SimpleNamespace(
                    check_session=AsyncMock(),
                    is_sandbox=False,
                    _headers={"Authorization": "QQBot token", "X-Union-Appid": "app"},
                )
            )
        )
        result = await transport.upload_image(self.event, b"png")
        self.assertEqual(result, "result-info")
        self.assertEqual(
            session.post.call_args.args[0], "https://api.bot.qq.com/v2/groups/group/files"
        )
        self.assertFalse(session.post.call_args.kwargs["json"]["srv_send_msg"])

    async def test_avatar_cache_is_bounded_and_isolates_app_ids(self):
        class Response:
            status = 404

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        avatars = Avatars()
        avatars.session = Mock()
        avatars.session.get.return_value = Response()
        await avatars.get("app", "member")
        await avatars.get("app", "member")
        self.assertEqual(avatars.session.get.call_count, 1)
        await avatars.get("other-app", "member")
        self.assertEqual(avatars.session.get.call_count, 2)
        for i in range(256):
            await avatars.get("app", str(i))
        self.assertEqual(len(avatars.cache), 256)
        self.assertNotIn(("app", "member"), avatars.cache)


class CacheAndConfigTests(unittest.TestCase):
    def test_schema_group_defaults_and_boolean_validation(self):
        schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text("utf-8"))
        group = schema["display"]
        self.assertNotIn("key_prefix", schema)
        self.assertEqual(group["type"], "object")
        parsed = Settings.from_dict(
            {"display": {k: v["default"] for k, v in group["items"].items()}}
        )
        self.assertEqual(
            [parsed.use_host(k) for k in ("draw", "atlas", "pen", "ranking")],
            [True, False, False, False],
        )
        for value in (None, [], {"draw": "false"}, {"atlas": 1}, {"unknown": True}):
            with self.assertRaises(PiggyError):
                Settings.from_dict({"display": value})

    def test_cleanup_only_removes_old_reproducible_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ("cards", "thumbnails", "assets", "catalog"):
                (root / folder).mkdir()
                old = root / folder / "old.png"
                old.write_bytes(b"old")
                os.utime(old, (0, 0))
            (root / "cards" / "fresh.png").write_bytes(b"fresh")
            (root / "cards" / "unrelated.txt").write_text("retain")
            clean_cards(root)
            self.assertFalse((root / "cards" / "old.png").exists())
            self.assertFalse((root / "thumbnails" / "old.png").exists())
            self.assertTrue((root / "assets" / "old.png").exists())
            self.assertTrue((root / "catalog" / "old.png").exists())
            self.assertTrue((root / "cards" / "fresh.png").exists())
            self.assertTrue((root / "cards" / "unrelated.txt").exists())

    def test_avatar_is_normalized_and_bad_content_rejected(self):
        output = io.BytesIO()
        Image.new("RGB", (200, 100), "red").save(output, "PNG")
        with Image.open(io.BytesIO(Avatars.normalize(output.getvalue()))) as avatar:
            self.assertEqual(avatar.size, (96, 96))
        with self.assertRaises(OSError):
            Avatars.normalize(b"not an image")
