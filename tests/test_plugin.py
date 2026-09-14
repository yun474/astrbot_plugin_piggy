import asyncio
import importlib
import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class OfficialEvent:
    def __init__(self, message_id="event", user="member", group="group-a"):
        self.group, self.user = group, user
        self.message_obj = SimpleNamespace(
            message_id=message_id,
            raw_message=SimpleNamespace(author=SimpleNamespace(username="玩家名字")),
        )
        self.bot = SimpleNamespace(
            api=SimpleNamespace(_http=SimpleNamespace(_token=SimpleNamespace(app_id="app")))
        )
        self.send = AsyncMock()
        self.stopped = False

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return self.user

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        modules = {}
        names = [
            "astrbot",
            "astrbot.api",
            "astrbot.api.event",
            "astrbot.api.star",
            "astrbot.core",
            "astrbot.core.platform",
            "astrbot.core.platform.sources",
            "astrbot.core.platform.sources.qqofficial",
            "astrbot.core.platform.sources.qqofficial.qqofficial_message_event",
        ]
        for name in names:
            modules[name] = types.ModuleType(name)

        def command(name, **kwargs):
            def decorate(function):
                function.command_name = name
                return function

            return decorate

        def permission(value):
            def decorate(function):
                function.admin_only = True
                return function

            return decorate

        class Star:
            def __init__(self, context):
                self.context = context

        modules["astrbot.api"].AstrBotConfig = dict
        modules["astrbot.api"].logger = logging.getLogger("piggy-tests")
        modules["astrbot.api.event"].AstrMessageEvent = OfficialEvent
        modules["astrbot.api.event"].filter = SimpleNamespace(
            command=command, permission_type=permission, PermissionType=SimpleNamespace(ADMIN=1)
        )
        modules["astrbot.api.star"].Context = object
        modules["astrbot.api.star"].Star = Star
        modules["astrbot.api.star"].StarTools = SimpleNamespace(get_data_dir=lambda name: self.root)
        modules["astrbot.api.star"].register = lambda *args: lambda cls: cls
        modules[names[-1]].QQOfficialMessageEvent = OfficialEvent
        self.patcher = patch.dict(sys.modules, modules)
        self.patcher.start()
        self.parent = str(Path(__file__).resolve().parents[2])
        sys.path.insert(0, self.parent)
        sys.modules.pop("astrbot_plugin_piggy.main", None)
        self.module = importlib.import_module("astrbot_plugin_piggy.main")
        self.config = {
            "endpoint": "https://r2.example.com",
            "bucket": "pigs",
            "access_key": "key",
            "secret_key": "secret",
            "public_base_url": "https://img.example.com",
            "image_retry_count": 0,
        }
        self.plugin = self.module.PiggyPlugin(object(), self.config)
        self.plugin.transport.request = AsyncMock(return_value={"id": "sent"})
        self.plugin.publisher.host.upload = AsyncMock(
            return_value="https://img.example.com/pig.png"
        )

    async def asyncTearDown(self):
        await self.plugin.terminate()
        sys.modules.pop("astrbot_plugin_piggy.main", None)
        sys.path.remove(self.parent)
        self.patcher.stop()
        self.temp.cleanup()

    async def test_commands_share_global_account_and_duplicate_events_are_suppressed(self):
        events = [OfficialEvent(f"event-{i}", group=f"group-{i}") for i in range(4)]
        await asyncio.gather(*(self.plugin.draw(e) for e in events))
        self.assertTrue(all(e.stopped for e in events))
        user = await self.plugin.db.identify("app", "member", "group-a", "")
        self.assertEqual((await self.plugin.db.collection(user["id"]))["total"], 1)
        count = self.plugin.transport.request.await_count
        await self.plugin.draw(events[0])
        self.assertEqual(self.plugin.transport.request.await_count, count)
        await self.plugin.atlas(OfficialEvent("atlas"), 1)
        await self.plugin.pen(OfficialEvent("pen"))
        await self.plugin.ranking(OfficialEvent("rank"), "数量")
        await self.plugin.alias(OfficialEvent("alias"), "自定义称呼")
        updated = await self.plugin.db.identify("app", "member", "group-a", "")
        self.assertEqual(updated["alias"], "自定义称呼")
        for call in self.plugin.transport.request.await_args_list:
            payload = call.args[1]
            self.assertEqual(payload["msg_type"], 2)
            self.assertTrue(payload["markdown"]["force_verify_image_resource"])

    async def test_upload_failure_keeps_draw_and_next_command_displays_same_pig(self):
        from astrbot_plugin_piggy.core.storage import UploadError

        self.plugin.publisher.host.upload.side_effect = UploadError("temporary")
        await self.plugin.draw(OfficialEvent("failed"))
        user = await self.plugin.db.identify("app", "member", "a", "")
        before = await self.plugin.db.collection(user["id"])
        self.assertEqual(before["total"], 1)
        self.plugin.publisher.host.upload.side_effect = None
        await self.plugin.draw(OfficialEvent("retry"))
        self.assertEqual((await self.plugin.db.collection(user["id"]))["total"], 1)

    async def test_no_host_configuration_does_not_consume_draw(self):
        from astrbot_plugin_piggy.core.config import Settings

        self.plugin.settings = Settings()
        await self.plugin.draw(OfficialEvent())
        user = await self.plugin.db.identify("app", "member", "a", "")
        self.assertEqual((await self.plugin.db.collection(user["id"]))["total"], 0)

    async def test_reload_backup_admin_declarations_and_missing_manifest_preserves_catalog(self):
        await self.plugin.initialize()
        self.assertTrue(self.plugin.reload.admin_only)
        self.assertTrue(self.plugin.backup.admin_only)
        self.assertTrue(self.plugin.diagnose.admin_only)
        manifest = self.root / "catalog" / "pigs.json"
        pigs = json.loads(manifest.read_text("utf-8"))
        pigs[0]["enabled"] = False
        manifest.write_text(json.dumps(pigs, ensure_ascii=False), "utf-8")
        await self.plugin.reload(OfficialEvent("reload"))
        progress = await self.plugin.db.collection(999)
        self.assertEqual(progress["active_total"], 95)
        await self.plugin.backup(OfficialEvent("backup"))
        self.assertTrue(list((self.root / "backups").glob("*.zip")))
        manifest.unlink()
        await self.plugin.terminate()
        self.plugin.ready = False
        await self.plugin.initialize()
        self.assertFalse(manifest.exists())
        self.assertEqual((await self.plugin.db.collection(999))["active_total"], 95)


if __name__ == "__main__":
    unittest.main()
