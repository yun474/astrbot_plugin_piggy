import asyncio
import importlib
import json
import logging
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from botpy.message import C2CMessage, GroupMessage
from botpy.message import Message as ChannelMessage


class FakeConfig(dict):
    def save_config(self):
        pass


class OfficialEvent:
    def __init__(self, message_id="event", user="member", group="group-a", text="", mentions=()):
        self.group, self.user, self.text = group, user, text
        self.message_obj = SimpleNamespace(
            message_id=message_id,
            raw_message=GroupMessage(
                None,
                message_id,
                {
                    "id": message_id,
                    "group_openid": group,
                    "author": {"member_openid": user},
                    "mentions": [{"member_openid": m} for m in mentions],
                },
            ),
        )
        self.message_obj.raw_message.author.username = "玩家名字"
        self.bot = SimpleNamespace(
            api=SimpleNamespace(_http=SimpleNamespace(_token=SimpleNamespace(app_id="app")))
        )
        self.send = AsyncMock()
        self.stopped = False

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return self.user

    def get_message_str(self):
        return self.text

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_and_private_scenes_do_not_write_or_upload(self):
        self.plugin.db.identify = AsyncMock()
        for raw_type in (ChannelMessage, C2CMessage):
            event = OfficialEvent("unsupported", group="channel-id")
            event.message_obj.raw_message = raw_type(None, "event", {})
            await self.plugin.draw(event)
            event.send.assert_awaited_once()
        self.plugin.db.identify.assert_not_awaited()
        self.plugin.publisher.host.upload.assert_not_awaited()
        self.plugin.transport.request.assert_not_awaited()
        self.assertFalse(self.plugin.db.path.exists())

    async def test_queue_is_bounded_and_one_user_cannot_fill_it(self):
        release = asyncio.Event()
        self.plugin._execute = AsyncMock()
        for _ in range(3):
            await self.plugin.limit.acquire()

        # Use an async side effect so the sole overload notice stays in flight.
        async def busy(*args):
            await release.wait()

        self.plugin._failure = AsyncMock(side_effect=busy)
        events = [OfficialEvent(f"event-{i}", user=f"user-{i}") for i in range(100)]
        calls = [asyncio.create_task(self.plugin.draw(event)) for event in events]
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(len(self.plugin.inflight), self.module.MAX_INFLIGHT)
        self.assertEqual(self.plugin._failure.await_count, 1)
        await self.plugin.draw(OfficialEvent("same-user-again", user="user-0"))
        self.assertEqual(len(self.plugin.inflight), self.module.MAX_INFLIGHT)
        release.set()
        for _ in range(3):
            self.plugin.limit.release()
        await asyncio.gather(*calls)
        self.assertEqual(self.plugin._execute.await_count, self.module.MAX_INFLIGHT)
        self.assertFalse(self.plugin.inflight)
        self.assertFalse(self.plugin.active_users)

    async def test_queued_request_expires_without_waiting_for_a_worker(self):
        self.plugin._execute = AsyncMock()
        self.plugin._failure = AsyncMock()
        for _ in range(3):
            await self.plugin.limit.acquire()
        with patch.object(self.module, "REQUEST_TIMEOUT", 0.02):
            await asyncio.wait_for(self.plugin.draw(OfficialEvent()), 1)
        self.plugin._execute.assert_not_awaited()
        self.plugin._failure.assert_awaited_once()
        self.assertFalse(self.plugin.inflight)
        for _ in range(3):
            self.plugin.limit.release()

    async def test_shutdown_drains_running_work_and_skips_queued_work(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def execute(*args):
            if self.plugin._execute.await_count == 3:
                entered.set()
            await release.wait()

        self.plugin._execute = AsyncMock(side_effect=execute)
        self.plugin.transport.close = AsyncMock()
        calls = [
            asyncio.create_task(self.plugin.draw(OfficialEvent(str(i), user=str(i))))
            for i in range(6)
        ]
        await asyncio.wait_for(entered.wait(), 1)
        shutdown = asyncio.create_task(self.plugin.terminate())
        await asyncio.sleep(0)
        await self.plugin.draw(OfficialEvent("after-stop", user="new-user"))
        self.plugin.transport.close.assert_not_awaited()
        release.set()
        await asyncio.wait_for(asyncio.gather(*calls, shutdown), 1)
        self.assertEqual(self.plugin._execute.await_count, 3)
        self.plugin.transport.close.assert_awaited_once()
        self.assertFalse(self.plugin.inflight)

    async def test_cancelled_handler_keeps_work_registered_until_completion(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def execute(*args):
            entered.set()
            await release.wait()

        self.plugin._execute = AsyncMock(side_effect=execute)
        call = asyncio.create_task(self.plugin.draw(OfficialEvent()))
        await asyncio.wait_for(entered.wait(), 1)
        call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await call
        self.assertEqual(len(self.plugin.inflight), 1)
        release.set()
        await self.plugin.terminate()
        self.assertFalse(self.plugin.inflight)
        self.assertFalse(self.plugin.active_users)

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
        package = types.ModuleType("astrbot_plugin_piggy")
        package.__path__ = [str(Path(__file__).resolve().parents[1])]
        modules["astrbot_plugin_piggy"] = package
        self.patcher = patch.dict(sys.modules, modules)
        self.patcher.start()
        self.parent = str(Path(__file__).resolve().parents[2])
        sys.path.insert(0, self.parent)
        sys.modules.pop("astrbot_plugin_piggy.main", None)
        self.module = importlib.import_module("astrbot_plugin_piggy.main")
        self.config = FakeConfig(
            {
                "endpoint": "https://r2.example.com",
                "bucket": "pigs",
                "access_key": "key",
                "secret_key": "secret",
                "public_base_url": "https://img.example.com",
                "image_retry_count": 0,
                # Keep the classic one-pig draw here; gather/chain have their own tests.
                "draw_gather_chance": 0,
                "draw_chain_chance": 0,
            }
        )
        self.config.save_config = Mock()
        self.plugin = self.module.PiggyPlugin(object(), self.config)
        self.config.save_config.assert_called_once_with()
        self.plugin.transport.upload_image = AsyncMock(return_value="qq-image-info")
        self.plugin.avatars.get_many = AsyncMock(return_value={})
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
            if payload["msg_type"] == 2:
                self.assertTrue(payload["markdown"]["force_verify_image_resource"])
            else:
                self.assertIn(payload["msg_type"], (0, 7))
                self.assertNotIn("keyboard", payload)
                self.assertNotIn("markdown", payload)

    async def test_duel_by_mention_and_trade_by_group_name(self):
        await self.plugin.initialize()
        db = self.plugin.db
        alice = await db.identify("app", "member", "group-a", "阿离")
        bob = await db.identify("app", "rival", "group-a", "阿波")
        await db.set_alias(bob["id"], "阿波")
        for user, pig_id in ((alice, "pig"), (bob, "black-pig")):
            await db.run(
                lambda c, u=user, p=pig_id: c.execute(
                    "INSERT INTO collections VALUES(?,?,3,0,0)", (u["id"], p)
                )
            )
        sent = self.plugin.transport.request

        def last():
            return sent.await_args.args[1]["content"]

        await self.plugin.guide(OfficialEvent("g1", text="小猪玩法"))
        self.assertIn("斗猪 @群友 猪 —— 用你的猪发起挑战", last())
        self.assertIn("你的「猪」有 3 只", last())
        await self.plugin.duel(OfficialEvent("d1", text="斗猪 <@rival> 猪", mentions=("rival",)))
        self.assertIn("@阿波 玩家名字 向你发起斗猪", last())
        await self.plugin.duel_accept(OfficialEvent("d2", user="rival", text="接受斗猪 小黑猪"))
        self.assertEqual(sent.await_args.args[1]["msg_type"], 7)
        counts = [
            await db.pig_count(alice["id"], "pig") + await db.pig_count(bob["id"], "pig"),
            await db.pig_count(alice["id"], "black-pig")
            + await db.pig_count(bob["id"], "black-pig"),
        ]
        self.assertEqual(counts, [3, 3])
        loser_left = {
            await db.pig_count(alice["id"], "pig"),
            await db.pig_count(bob["id"], "black-pig"),
        }
        self.assertIn(2, loser_left)
        await self.plugin.duel_ranking(OfficialEvent("k1", text="斗猪排行"))
        self.assertIn("还没有玩家打满 3 场", last())
        self.assertIn("再打 2 场即可上榜", last())
        await self.plugin.duel_history(OfficialEvent("h1", text="斗猪记录"))
        self.assertEqual(sent.await_args.args[1]["msg_type"], 7)
        await self.plugin.duel_replay(OfficialEvent("p1", user="rival", text="斗猪回放 #1"))
        self.assertEqual(sent.await_args.args[1]["msg_type"], 7)
        await self.plugin.duel_replay(OfficialEvent("p2", text="斗猪回放 abc"))
        self.assertIn("用法：斗猪回放 编号", last())
        await self.plugin.trade(OfficialEvent("t1", text="小猪交换 阿波 猪 换 小黑猪"))
        self.assertIn("想和你交换", last())
        await self.plugin.requests(OfficialEvent("r1", user="rival", text="我的请求"))
        self.assertIn("收到的请求", last())
        await self.plugin.trade_accept(OfficialEvent("t2", user="rival", text="接受交换"))
        self.assertIn("交换成功", last())
        await self.plugin.stats(OfficialEvent("s1", text="小猪属性 坦克猪"))
        self.assertIn("履带碾压", last())
        await self.plugin.duel(OfficialEvent("d3", text="斗猪 <@member> 猪", mentions=("member",)))
        self.assertIn("没有识别到你 @ 的群友", last())
        await self.plugin.cancel(OfficialEvent("c1", text="取消请求"))
        self.assertIn("没有待处理", last())

    async def test_mentions_skip_the_bot_and_fall_back_to_text_markup(self):
        await self.plugin.initialize()
        db = self.plugin.db
        challenger = await db.identify("app", "member", "group-a", "")
        await db.run(
            lambda c: c.execute(
                "INSERT INTO collections VALUES(?,'pig-souffle',2,0,0)", (challenger["id"],)
            )
        )
        await db.identify("app", "bot-openid", "group-a", "猪圈")
        sent = self.plugin.transport.request

        def last():
            return sent.await_args.args[1]["content"]

        async def target_of():
            listed = await db.list_requests("app", "group-a", challenger["id"])
            await db.cancel_requests("app", "group-a", challenger["id"])
            return listed["outgoing"][0]["to"]["open_id"]

        # Newer AstrBot keeps the bot itself (is_you) first in mentions.
        event = OfficialEvent("m1", text="斗猪 <@christina> 猪芙蕾")
        event.message_obj.raw_message.mentions = [
            SimpleNamespace(id="bot-openid", member_openid=None, is_you=True, bot=True),
            SimpleNamespace(
                id="christina", member_openid="christina", username="Christina", is_you=False
            ),
        ]
        await self.plugin.duel(event)
        self.assertIn("Christina 玩家名字 向你发起斗猪", last())
        self.assertEqual(await target_of(), "christina")
        with self.assertRaises(self.module.PiggyError):
            await db.find_group_player("app", "group-a", "猪圈")

        # Older AstrBot drops mention fields; the text markup still carries the id.
        await self.plugin.duel(OfficialEvent("m2", text="斗猪 <@dayangyu> 猪芙蕾"))
        self.assertEqual(await target_of(), "dayangyu")
        markup = '斗猪 <qqbot-at-user id="tagged" /> 猪芙蕾'
        await self.plugin.duel(OfficialEvent("m3", text=markup))
        self.assertEqual(await target_of(), "tagged")
        markup = "斗猪 <qqbot-at-user user_openid='quoted' nick='大洋芋' /> 猪芙蕾"
        await self.plugin.duel(OfficialEvent("m3b", text=markup))
        self.assertEqual(await target_of(), "quoted")

        # No mention and only a pig name: never treat the pig as a player.
        with self.assertLogs("piggy-tests", "INFO") as logs:
            await self.plugin.duel(OfficialEvent("m4", text="斗猪 猪芙蕾"))
        self.assertIn("可以改用对方的玩家编号：斗猪 #编号 你的小猪", last())
        self.assertTrue(any("No mention resolved" in line for line in logs.output))

        # A player number works when QQ drops the mention, e.g. for group-card names.
        card = await db.identify("app", "card-user", "group-a", "原昵称")
        await self.plugin.duel(OfficialEvent("m4b", text=f"斗猪 #{card['id']} 猪芙蕾"))
        self.assertEqual(await target_of(), "card-user")
        await self.plugin.pen(OfficialEvent("m4c", user="card-user", text="我的猪圈"))
        await self.plugin.guide(OfficialEvent("m4d", text="小猪玩法"))
        self.assertIn(f"你的编号是 #{challenger['id']}", last())

        self.plugin.settings = replace(self.plugin.settings, battle_markdown=True)
        await self.plugin.duel(OfficialEvent("m5", text="斗猪 <@christina> 猪芙蕾"))
        request_buttons = sent.await_args.args[1]["keyboard"]["content"]["rows"][0]["buttons"]
        self.assertTrue(all(b["action"]["permission"] == {"type": 2} for b in request_buttons))

    async def test_daily_shop_commands(self):
        await self.plugin.initialize()
        db = self.plugin.db
        user = await db.identify("app", "member", "group-a", "")
        sent = self.plugin.transport.request

        def last():
            return sent.await_args.args[1]["content"]

        await self.plugin.shop(OfficialEvent("s1", text="小猪商店"))
        self.assertEqual(sent.await_args.args[1]["msg_type"], 7)
        shop = await db.shop("app", "group-a", user["id"])
        stocked = {item["pig_id"] for item in shop["items"]}
        pay = await db.find_pig(
            next(
                p
                for p in ("pig", "black-pig", "wild-boar", "human", "pig-human", "tank_pig")
                if p not in stocked
            )
        )
        await db.run(
            lambda c: c.execute(
                "INSERT INTO collections VALUES(?,?,2,0,0)", (user["id"], pay["id"])
            )
        )
        await self.plugin.shop_exchange(OfficialEvent("s2", text=f"商店交换 1号 {pay['name']}"))
        self.assertIn("商店交换成功", last())
        self.assertIn(f"「{pay['name']}」Lv2 → Lv1", last())
        await self.plugin.shop_exchange(OfficialEvent("s3", text=f"商店交换 1 {pay['name']}"))
        self.assertIn("已经被换走了", last())
        await self.plugin.shop_exchange(OfficialEvent("s4", text="商店交换 猪"))
        self.assertIn("用法：商店交换 编号 你的小猪", last())

    async def test_taming_the_wild_pig_lets_today_draw_again(self):
        await self.plugin.initialize()
        db = self.plugin.db
        sent = self.plugin.transport.request
        await self.plugin.draw(OfficialEvent("w1", text="今日小猪"))
        user = await db.identify("app", "member", "group-a", "")
        await self.plugin.wild(OfficialEvent("w2", text="小猪挑战"))
        self.assertEqual(sent.await_args.args[1]["msg_type"], 7)
        await db.run(
            lambda c: c.execute("INSERT INTO collections VALUES(?,'tank_pig',5,0,0)", (user["id"],))
        )

        def win(*args):
            return {"winner": 0, "rounds": 1, "log": ["R1 胜"], "hp": [9, 0], "max_hp": [9, 9]}

        with patch("astrbot_plugin_piggy.core.database.simulate", win):
            await self.plugin.wild_challenge(OfficialEvent("w3", text="挑战小猪 坦克猪"))
        self.assertEqual(sent.await_args.args[1]["msg_type"], 7)
        self.assertEqual(await db.bonus_count("app", "group-a", user["id"]), 1)
        await self.plugin.draw(OfficialEvent("w4", text="今日小猪"))
        self.assertEqual(await db.bonus_count("app", "group-a", user["id"]), 0)
        sessions = await db.run(
            lambda c: c.execute(
                "SELECT kind FROM draw_sessions WHERE user_id=? ORDER BY id", (user["id"],)
            ).fetchall()
        )
        self.assertEqual([row[0] for row in sessions], ["daily", "bonus"])
        await self.plugin.wild_challenge(OfficialEvent("w5", text="挑战小猪 坦克猪"))
        self.assertIn("已经被", sent.await_args.args[1]["content"])

    async def test_raid_commands_from_lobby_to_retreat(self):
        await self.plugin.initialize()
        db = self.plugin.db
        sent = self.plugin.transport.request

        def last():
            return sent.await_args.args[1]

        players = ["lead", "m2", "m3", "m4"]
        for player in players:
            user = await db.identify("app", player, "group-a", player)
            await db.run(
                lambda c, user=user: c.execute(
                    "INSERT INTO collections VALUES(?,'tank_pig',3,0,0)", (user["id"],)
                )
            )
        await self.plugin.raid(OfficialEvent("r0", user="lead", text="猪副本"))
        self.assertIn("被束缚的猪王", last()["content"])
        await self.plugin.raid_open(OfficialEvent("r1", user="lead", text="开启副本 9 坦克猪"))
        self.assertIn("用法：开启副本", last()["content"])
        await self.plugin.raid_open(OfficialEvent("r2", user="lead", text="开启副本 2 坦克猪"))
        self.assertIn("组队中 1/4", last()["content"])
        for index, player in enumerate(players[1:3], 3):
            await self.plugin.raid_join(
                OfficialEvent(f"r{index}", user=player, text="加入副本 坦克猪")
            )
        self.assertIn("组队中 3/4", last()["content"])

        def win(heroes, boss, slot_id, cfg, seed):
            return {
                "winner": 0,
                "won": True,
                "timeout": False,
                "rounds": 3,
                "log": ["R1 胜"],
                "heroes": [
                    {"seat": h.get("seat"), "hp": 5, "max_hp": 10, "alive": True} for h in heroes
                ],
                "boss": {"hp": 0, "max_hp": 99},
                "field_events": [],
            }

        with patch("astrbot_plugin_piggy.core.database.simulate_raid", win):
            await self.plugin.raid_join(OfficialEvent("r5", user="m4", text="加入副本 坦克猪"))
        self.assertEqual(last()["msg_type"], 7)
        lead = await db.identify("app", "lead", "group-a", "")
        self.assertEqual(await db.bonus_count("app", "group-a", lead["id"]), 1)
        await self.plugin.raid_continue(OfficialEvent("r6", user="m2", text="继续副本"))
        self.assertIn("只有队长", last()["content"])
        await self.plugin.raid_retreat(OfficialEvent("r7", user="lead", text="撤退副本"))
        self.assertIn("撤退成功", last()["content"])
        await self.plugin.raid_join(OfficialEvent("r8", user="m2", text="加入副本 坦克猪"))
        self.assertIn("没有正在组队的副本", last()["content"])

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

    async def test_reported_qq_rejection_retries_and_logs_through_astrbot(self):
        from astrbot_plugin_piggy.core.delivery import QQError

        config = replace(self.plugin.settings, image_retry_count=10)
        self.plugin.settings = self.plugin.sender.settings = config
        self.plugin.transport.request.side_effect = [
            QQError(40034141, 400, reason="upstream rejection", trace_id="trace-123")
        ] * 10 + [{"id": "sent"}]
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with self.assertLogs("piggy-tests", level="WARNING") as logs:
                await self.plugin.draw(OfficialEvent("retry-40034141"))
        self.assertEqual(self.plugin.transport.request.await_count, 11)
        self.assertEqual(self.plugin.publisher.host.upload.await_count, 1)
        self.assertIn("stage=send attempt=1/11 code=40034141 http=400", logs.output[0])
        self.assertIn("reason=upstream rejection trace_id=trace-123", logs.output[0])
        user = await self.plugin.db.identify("app", "member", "a", "")
        self.assertEqual((await self.plugin.db.collection(user["id"]))["total"], 1)

    async def test_no_host_configuration_does_not_consume_draw(self):
        from astrbot_plugin_piggy.core.config import Settings

        self.plugin.settings = Settings()
        await self.plugin.draw(OfficialEvent())
        user = await self.plugin.db.identify("app", "member", "a", "")
        self.assertEqual((await self.plugin.db.collection(user["id"]))["total"], 0)

    async def test_every_command_works_without_host_and_switching_keeps_daily_record(self):
        from astrbot_plugin_piggy.core.config import Settings

        config = Settings(display={"draw": False}, draw_gather_chance=0, draw_chain_chance=0)
        self.plugin.settings = self.plugin.sender.settings = config
        self.plugin.publisher.publish = AsyncMock(side_effect=AssertionError("No host expected"))
        for command in (self.plugin.draw, self.plugin.atlas, self.plugin.pen, self.plugin.ranking):
            await command(OfficialEvent(command.__name__))
            payload = self.plugin.transport.request.await_args.args[1]
            self.assertEqual(payload["msg_type"], 7)
            self.assertNotIn("keyboard", payload)
            self.assertNotIn("markdown", payload)
        self.plugin.publisher.publish.assert_not_awaited()
        self.assertFalse((self.root / "cards").exists())
        self.assertFalse((self.root / "thumbnails").exists())
        user = await self.plugin.db.identify("app", "member", "group-a", "")
        self.assertEqual((await self.plugin.db.collection(user["id"]))["total"], 1)
        config = Settings.from_dict(
            {**self.config, "display": dict.fromkeys(("draw", "atlas", "pen", "ranking"), True)}
        )
        self.plugin.settings = self.plugin.sender.settings = config
        self.plugin.publisher.publish = AsyncMock(return_value="https://images.example.com/p.png")
        for command in (self.plugin.draw, self.plugin.atlas, self.plugin.pen, self.plugin.ranking):
            await command(OfficialEvent("hosted-" + command.__name__))
            payload = self.plugin.transport.request.await_args.args[1]
            self.assertEqual(payload["msg_type"], 2)
            self.assertIn("keyboard", payload)
        self.assertEqual((await self.plugin.db.collection(user["id"]))["total"], 1)

    async def test_local_upload_failure_preserves_collection(self):
        from astrbot_plugin_piggy.core.config import Settings
        from astrbot_plugin_piggy.core.delivery import QQError

        self.plugin.settings = self.plugin.sender.settings = Settings(
            display={"draw": False}, draw_gather_chance=0, draw_chain_chance=0
        )
        self.plugin.transport.upload_image.side_effect = QQError(123, 401)
        await self.plugin.draw(OfficialEvent("failed-local"))
        self.assertIn(
            "QQ 本地图片上传失败", self.plugin.transport.request.await_args.args[1]["content"]
        )
        self.plugin.transport.upload_image.side_effect = None
        await self.plugin.draw(OfficialEvent("retry-local"))
        user = await self.plugin.db.identify("app", "member", "group-a", "")
        self.assertEqual((await self.plugin.db.collection(user["id"]))["total"], 1)

    async def test_cleanup_runs_independently_after_backup_failure(self):
        self.plugin.db.backup = AsyncMock(side_effect=OSError("backup disk unavailable"))
        self.plugin.db.prune_cache = AsyncMock()
        with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
            with self.assertLogs("piggy-tests", level="ERROR"):
                with self.assertRaises(asyncio.CancelledError):
                    await self.plugin._backups()
            with patch.object(self.module, "clean_cards") as clean:
                with self.assertRaises(asyncio.CancelledError):
                    await self.plugin._cleanup()
                clean.assert_called_once_with(self.root)
        self.plugin.db.prune_cache.assert_awaited_once()

    async def test_diagnose_bypasses_url_cache_and_logs_upload_details(self):
        from astrbot_plugin_piggy.core.storage import UploadError

        await self.plugin.diagnose(OfficialEvent("diagnose-1"))
        self.assertEqual(self.plugin.publisher.host.upload.await_count, 1)
        self.plugin.publisher.host.upload.side_effect = UploadError(
            "TLS/SSL 证书校验失败",
            diagnostic="provider=s3 stage=put_object exception=SSLError reason=CERTIFICATE_VERIFY_FAILED",
        )
        with self.assertLogs("piggy-tests", level="WARNING") as logs:
            await self.plugin.diagnose(OfficialEvent("diagnose-2"))
        self.assertEqual(self.plugin.publisher.host.upload.await_count, 2)
        self.assertIn("command=diagnose attempt=1/3", logs.output[0])
        self.assertIn("CERTIFICATE_VERIFY_FAILED", logs.output[0])
        payload = self.plugin.transport.request.await_args.args[1]
        self.assertEqual(payload["msg_type"], 0)
        self.assertEqual(payload["content"], "TLS/SSL 证书校验失败")

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
        self.assertEqual(progress["active_total"], len(pigs) - 1)
        await self.plugin.backup(OfficialEvent("backup"))
        self.assertTrue(list((self.root / "backups").glob("*.zip")))
        manifest.unlink()
        await self.plugin.terminate()
        self.plugin.ready = False
        await self.plugin.initialize()
        self.assertFalse(manifest.exists())
        self.assertEqual((await self.plugin.db.collection(999))["active_total"], len(pigs) - 1)


if __name__ == "__main__":
    unittest.main()
