import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from core.catalog import read_catalog
from core.config import PiggyError, Settings
from core.database import Database
from core.views import (
    battle_message,
    duel_history_message,
    duel_ranking_message,
    duel_replay_message,
    request_message,
    shop_exchange_message,
    shop_message,
    stats_message,
    trade_message,
)

NOW = datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc)


async def drawn(message_coro):
    """Await a card message and return it with every string drawn on the card."""
    labels = []
    original = ImageDraw.ImageDraw.text

    def record(canvas, xy, text, *args, **kwargs):
        labels.append(str(text))
        return original(canvas, xy, text, *args, **kwargs)

    with patch.object(ImageDraw.ImageDraw, "text", record):
        message = await message_coro
    return message, "".join(labels).replace(" ", "")


def has(text: str, joined: str) -> bool:
    return text.replace(" ", "") in joined


class ExchangeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root)
        await self.db.initialize()
        images = self.root / "catalog" / "images"
        images.mkdir(parents=True)
        definitions = []
        for index, pig_id in enumerate(("pig", "cat", "owl")):
            Image.new("RGB", (8, 8), (index * 40, 90, 120)).save(images / f"{pig_id}.png")
            definitions.append(
                {"id": pig_id, "name": f"{pig_id}猪", "description": "描述", "analysis": "性格"}
            )
        (self.root / "catalog" / "pigs.json").write_text(json.dumps(definitions), "utf-8")
        await self.db.catalog(read_catalog(self.root))
        self.alice = await self.db.identify("app", "alice", "group", "阿离")
        self.bob = await self.db.identify("app", "bob", "group", "阿波")

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def own(self, user, pig_id, count):
        await self.db.run(
            lambda c: c.execute(
                "INSERT OR REPLACE INTO collections VALUES(?,?,?,0,0)", (user["id"], pig_id, count)
            )
        )

    async def counts(self, user):
        rows = await self.db.run(
            lambda c: c.execute(
                "SELECT pig_id,count FROM collections WHERE user_id=?", (user["id"],)
            ).fetchall()
        )
        return {row[0]: row[1] for row in rows}

    async def duel(self, **kwargs):
        return await self.db.create_request(
            "app", "group", "duel", self.alice["id"], self.bob["id"], "pig", now=NOW, **kwargs
        )

    async def test_trade_swaps_one_pig_each_and_reports_level_changes(self):
        await self.own(self.alice, "pig", 2)
        await self.own(self.bob, "cat", 1)
        request = await self.db.create_request(
            "app", "group", "trade", self.alice["id"], self.bob["id"], "pig", "cat", now=NOW
        )
        self.assertIn("想和你交换", request_message(Settings(), request, 2).text)
        result = await self.db.respond("app", "group", self.bob["id"], "trade", True, now=NOW)
        self.assertTrue(result["accepted"])
        self.assertEqual(await self.counts(self.alice), {"pig": 1, "cat": 1})
        self.assertEqual(await self.counts(self.bob), {"pig": 1})
        changes = result["changes"]
        self.assertEqual((changes["from_give"]["before"], changes["from_give"]["after"]), (2, 1))
        self.assertEqual(changes["to_want"]["after"], 0)
        self.assertEqual(changes["from_want"]["gained"], ["猪突猛进"])
        self.assertIn("交换成功", trade_message(Settings(), result).text)
        with self.assertRaises(PiggyError):
            await self.db.respond("app", "group", self.bob["id"], "trade", True, now=NOW)

    async def test_duel_moves_one_pig_from_loser_and_relocks_skills(self):
        await self.own(self.alice, "pig", 5)
        await self.own(self.bob, "cat", 5)
        await self.duel()
        result = await self.db.respond(
            "app", "group", self.bob["id"], "duel", True, "cat", now=NOW, seed=7
        )
        winner, loser = result["winner"], result["loser"]
        prize = result["loser_change"]["pig"]["id"]
        self.assertEqual((await self.counts(loser))[prize], 4)
        self.assertEqual((await self.counts(winner))[prize], 1)
        self.assertEqual(result["loser_change"]["before"], 5)
        self.assertEqual(result["loser_change"]["after"], 4)
        self.assertEqual(result["loser_change"]["lost"], ["全力一拱"])
        self.assertTrue(result["winner_change"]["after"] > result["winner_change"]["before"])
        records = await self.db.run(
            lambda c: c.execute("SELECT winner,seed,log FROM battle_records").fetchall()
        )
        self.assertEqual(records[0][0], winner["id"])
        self.assertEqual(records[0][1], 7)
        self.assertEqual(json.loads(records[0][2]), result["result"]["log"])
        card, text = await drawn(battle_message(Settings(), self.root, result))
        self.assertTrue(card.local)
        for line in result["result"]["log"]:
            self.assertTrue(has(line, text), line)
        self.assertTrue(has("获胜", text))
        self.assertTrue(has("Lv5 → Lv4", text))
        self.assertTrue(has("WIN", text) and has("LOSE", text))
        summary = await self.db.run(
            lambda c: c.execute("SELECT summary FROM battle_records").fetchone()[0]
        )
        self.assertEqual(json.loads(summary)["hp"], result["result"]["hp"])

    async def test_losing_the_last_pig_removes_it_from_the_pen(self):
        await self.own(self.alice, "pig", 1)
        await self.own(self.bob, "cat", 1)
        await self.duel()
        result = await self.db.respond(
            "app", "group", self.bob["id"], "duel", True, "cat", now=NOW, seed=3
        )
        loser_pig = result["loser_change"]["pig"]["id"]
        self.assertNotIn(loser_pig, await self.counts(result["loser"]))
        self.assertEqual(result["loser_change"]["after"], 0)
        _, text = await drawn(battle_message(Settings(), self.root, result))
        self.assertTrue(has("已失去", text))

    async def test_decline_cancel_expiry_and_one_pending_request_per_side(self):
        await self.own(self.alice, "pig", 1)
        await self.own(self.bob, "cat", 1)
        with self.assertRaises(PiggyError):
            await self.db.create_request(
                "app", "group", "duel", self.alice["id"], self.alice["id"], "pig", now=NOW
            )
        with self.assertRaises(PiggyError):
            await self.db.create_request(
                "app", "group", "duel", self.alice["id"], self.bob["id"], "owl", now=NOW
            )
        await self.duel()
        with self.assertRaises(PiggyError):
            await self.duel()
        listed = await self.db.list_requests("app", "group", self.bob["id"], now=NOW)
        self.assertEqual(len(listed["incoming"]), 1)
        declined = await self.db.respond("app", "group", self.bob["id"], "duel", False, now=NOW)
        self.assertFalse(declined["accepted"])
        await self.duel()
        cancelled = await self.db.cancel_requests("app", "group", self.alice["id"], now=NOW)
        self.assertEqual(len(cancelled), 1)
        await self.duel(ttl_minutes=10)
        with self.assertRaises(PiggyError):
            await self.db.respond(
                "app", "group", self.bob["id"], "duel", True, "cat", now=NOW + timedelta(minutes=11)
            )
        self.assertEqual(await self.counts(self.bob), {"cat": 1})

    async def test_daily_duel_limit_and_vanished_challenger_pig(self):
        await self.own(self.alice, "pig", 5)
        await self.own(self.bob, "cat", 5)
        for seed in range(2):
            await self.duel(daily_limit=2)
            await self.db.respond(
                "app",
                "group",
                self.bob["id"],
                "duel",
                True,
                "cat",
                daily_limit=2,
                now=NOW,
                seed=seed,
            )
        with self.assertRaises(PiggyError):
            await self.duel(daily_limit=2)
        await self.db.run(lambda c: c.execute("DELETE FROM battle_records"))
        await self.duel()
        await self.db.run(lambda c: c.execute("DELETE FROM collections WHERE pig_id='pig'"))
        result = await self.db.respond("app", "group", self.bob["id"], "duel", True, "cat", now=NOW)
        self.assertFalse(result["accepted"])
        self.assertIn("作废", result["error"])

    async def test_concurrent_accepts_complete_the_trade_only_once(self):
        await self.own(self.alice, "pig", 1)
        await self.own(self.bob, "cat", 1)
        await self.db.create_request(
            "app", "group", "trade", self.alice["id"], self.bob["id"], "pig", "cat", now=NOW
        )
        results = await asyncio.gather(
            *(
                Database(self.root).respond("app", "group", self.bob["id"], "trade", True, now=NOW)
                for _ in range(8)
            ),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertTrue(all(isinstance(r, (dict, PiggyError)) for r in results))
        self.assertEqual(await self.counts(self.alice), {"cat": 1})

    async def test_stats_lookup_and_group_player_fallback(self):
        await self.own(self.alice, "pig", 3)
        pig = await self.db.find_pig("pig猪")
        self.assertEqual(pig["id"], (await self.db.find_pig("PIG"))["id"])
        text = stats_message(Settings(), self.alice, pig, 3, 20).text
        self.assertIn("Lv3", text)
        self.assertIn("【Lv4 解锁】", text)
        with self.assertRaises(PiggyError):
            await self.db.find_pig("不存在的猪")
        self.assertEqual(
            (await self.db.find_group_player("app", "group", "@阿波"))["id"], self.bob["id"]
        )
        await self.db.identify("app", "other", "group", "阿波")
        with self.assertRaises(PiggyError):
            await self.db.find_group_player("app", "group", "阿波")
        found = await self.db.find_group_player("app", "group", f"#{self.bob['id']}")
        self.assertEqual(found["id"], self.bob["id"])
        outsider = await self.db.identify("app", "outsider", "elsewhere", "外人")
        for query in (f"＃{outsider['id']}", "#9999"):
            with self.assertRaises(PiggyError):
                await self.db.find_group_player("app", "group", query)

    async def fight(self, challenger, target, seed, group="group"):
        await self.db.create_request(
            "app", group, "duel", challenger["id"], target["id"], "pig", now=NOW, daily_limit=99
        )
        return await self.db.respond(
            "app", group, target["id"], "duel", True, "cat", now=NOW, seed=seed, daily_limit=99
        )

    async def test_duel_ranking_history_and_replay(self):
        carol = await self.db.identify("app", "carol", "group", "阿卡")
        dave = await self.db.identify("app", "dave", "other", "阿戴")
        await self.db.identify("app", "alice", "other", "")
        for user in (self.alice, self.bob, dave):
            await self.own(user, "pig", 50)
            await self.own(user, "cat", 50)
        results = [await self.fight(self.alice, self.bob, seed) for seed in range(4)]
        results += [await self.fight(dave, self.alice, seed, "other") for seed in range(4, 7)]
        alice_wins = sum(r["winner"]["id"] == self.alice["id"] for r in results)

        board = await self.db.duel_rankings("app", "group", carol["id"])
        ranked = {row["id"]: row for row in board["top"]}
        self.assertEqual(set(ranked), {self.alice["id"], self.bob["id"]})
        self.assertEqual(ranked[self.alice["id"]]["games"], 7)
        self.assertEqual(ranked[self.alice["id"]]["wins"], alice_wins)
        self.assertEqual(ranked[self.bob["id"]]["games"], 4)
        rates = [row["rate"] for row in board["top"]]
        self.assertEqual(rates, sorted(rates, reverse=True))
        self.assertIsNone(board["me"])
        text = duel_ranking_message(Settings(), carol, board).text
        self.assertIn("斗猪胜率排行", text)
        self.assertIn("你还没有斗过猪", text)
        bob_board = await self.db.duel_rankings("app", "group", self.bob["id"], min_games=5)
        self.assertEqual([row["id"] for row in bob_board["top"]], [self.alice["id"]])
        self.assertIn(
            "再打 1 场即可上榜", duel_ranking_message(Settings(), self.bob, bob_board).text
        )

        history = await self.db.duel_history(self.alice["id"], 1, size=5)
        self.assertEqual((history["total"], history["wins"], history["pages"]), (7, alice_wins, 2))
        ids = [record["id"] for record in history["records"]]
        self.assertEqual(ids, sorted(ids, reverse=True))
        latest = history["records"][0]
        self.assertEqual(latest["opponent"]["id"], dave["id"])
        self.assertEqual(latest["my_pig"], "cat猪")
        _, text = await drawn(duel_history_message(Settings(), self.root, self.alice, history))
        self.assertTrue(has(f"#{latest['id']}", text))
        self.assertTrue(has("vs 阿戴", text))
        self.assertTrue(has("01 / 02", text))
        with self.assertRaises(PiggyError):
            await self.db.duel_history(self.alice["id"], 3, size=5)

        record = await self.db.duel_record(self.bob["id"], 1)
        self.assertEqual(record["log"], results[0]["result"]["log"])
        self.assertEqual(record["summary"]["rounds"], results[0]["result"]["rounds"])
        _, replay = await drawn(duel_replay_message(Settings(), self.root, self.bob, record))
        for line in record["log"]:
            self.assertTrue(has(line, replay), line)
        self.assertTrue(has("斗猪回放 #1", replay))
        with self.assertRaises(PiggyError):
            await self.db.duel_record(carol["id"], 1)

    async def test_duel_cards_carry_buttons_when_hosted(self):
        for user in (self.alice, self.bob):
            await self.own(user, "pig", 50)
            await self.own(user, "cat", 50)
        results = [await self.fight(self.alice, self.bob, seed) for seed in range(12)]
        board = await self.db.duel_rankings("app", "group", self.alice["id"])
        history = await self.db.duel_history(self.alice["id"], 1)
        last_page = await self.db.duel_history(self.alice["id"], 2)
        record = await self.db.duel_record(self.alice["id"], 1)
        hosted, local = Settings(display={"duel": True}), Settings()

        def labels(message):
            rows = message.keyboard["content"]["rows"]
            return [b["render_data"]["label"] for row in rows for b in row["buttons"]]

        cards = (
            (lambda s: battle_message(s, self.root, results[-1]), ["斗猪记录", "斗猪排行"]),
            (
                lambda s: duel_history_message(s, self.root, self.alice, history),
                ["斗猪回放", "下一页", "斗猪排行"],
            ),
            (
                lambda s: duel_history_message(s, self.root, self.alice, last_page),
                ["斗猪回放", "斗猪排行"],
            ),
            (
                lambda s: duel_replay_message(s, self.root, self.alice, record),
                ["斗猪记录", "斗猪排行"],
            ),
        )
        for render, expected in cards:
            rich = await render(hosted)
            self.assertFalse(rich.local)
            self.assertEqual(labels(rich), expected)
            plain = await render(local)
            self.assertTrue(plain.local)
            self.assertIsNone(plain.keyboard)
        ranking = duel_ranking_message(Settings(battle_markdown=True), self.alice, board)
        self.assertEqual(labels(ranking), ["斗猪记录", "斗猪玩法"])
        self.assertIn(
            "发送「斗猪记录」查看自己的对战",
            duel_ranking_message(Settings(), self.alice, board).text,
        )

    async def test_forget_bot_removes_it_from_group_and_cancels_requests(self):
        bot = await self.db.identify("app", "bot-id", "group", "猪圈")
        await self.own(self.alice, "pig", 1)
        await self.db.create_request(
            "app", "group", "duel", self.alice["id"], bot["id"], "pig", now=NOW
        )
        await self.db.forget_bot("app", {"bot-id", "unknown"})
        with self.assertRaises(PiggyError):
            await self.db.find_group_player("app", "group", "猪圈")
        listed = await self.db.list_requests("app", "group", self.alice["id"], now=NOW)
        self.assertEqual(listed["outgoing"], [])

    async def test_markdown_mode_mentions_target_and_limits_buttons(self):
        await self.own(self.alice, "pig", 1)
        request = await self.duel()
        message = request_message(Settings(battle_markdown=True), request, 1)
        self.assertTrue(message.markdown)
        self.assertIn('<qqbot-at-user id="bob" />', message.text)
        buttons = message.keyboard["content"]["rows"][0]["buttons"]
        self.assertTrue(all(b["action"]["permission"] == {"type": 2} for b in buttons))


class ShopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root)
        await self.db.initialize()
        images = self.root / "catalog" / "images"
        images.mkdir(parents=True)
        definitions = []
        for index in range(8):
            pig_id = f"p{index}"
            Image.new("RGB", (8, 8), (index * 20, 90, 120)).save(images / f"{pig_id}.png")
            definitions.append(
                {"id": pig_id, "name": f"猪{index}", "description": "描述", "analysis": "性格"}
            )
        (self.root / "catalog" / "pigs.json").write_text(json.dumps(definitions), "utf-8")
        await self.db.catalog(read_catalog(self.root))
        self.alice = await self.db.identify("app", "alice", "group", "阿离")
        self.bob = await self.db.identify("app", "bob", "group", "阿波")

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def own(self, user, pig_id, count):
        await self.db.run(
            lambda c: c.execute(
                "INSERT OR REPLACE INTO collections VALUES(?,?,?,0,0)", (user["id"], pig_id, count)
            )
        )

    async def count(self, user, pig_id):
        return await self.db.pig_count(user["id"], pig_id)

    def payment_for(self, shop, slot):
        """A pig that is not on sale in this slot, to pay with."""
        stocked = {item["pig_id"] for item in shop["items"]}
        return next(f"p{i}" for i in range(8) if f"p{i}" not in stocked)

    async def test_shelf_is_stable_within_a_day_and_refreshes_at_midnight(self):
        before_midnight = datetime(2026, 10, 7, 15, 59, tzinfo=timezone.utc)
        first = await self.db.shop("app", "group", self.alice["id"], now=NOW)
        again = await self.db.shop("app", "group", self.bob["id"], now=before_midnight)
        pigs = [item["pig_id"] for item in first["items"]]
        self.assertEqual(len(pigs), 5)
        self.assertEqual(len(set(pigs)), 5)
        self.assertEqual(pigs, [item["pig_id"] for item in again["items"]])
        self.assertEqual(first["day"], "2026-10-07")
        tomorrow = await self.db.shop(
            "app", "group", self.alice["id"], now=before_midnight + timedelta(minutes=1)
        )
        self.assertEqual(tomorrow["day"], "2026-10-08")
        other_group = await self.db.shop("app", "elsewhere", self.alice["id"], now=NOW)
        self.assertEqual(len(other_group["items"]), 5)

    async def test_exchange_takes_one_payment_and_sells_each_slot_once(self):
        shop = await self.db.shop("app", "group", self.alice["id"], now=NOW)
        item = shop["items"][0]
        pay = self.payment_for(shop, 1)
        await self.own(self.alice, pay, 2)
        await self.own(self.bob, pay, 1)
        result = await self.db.shop_exchange("app", "group", self.alice["id"], 1, pay, now=NOW)
        self.assertEqual(await self.count(self.alice, pay), 1)
        self.assertEqual(await self.count(self.alice, item["pig_id"]), 1)
        self.assertEqual((result["paid"]["before"], result["paid"]["after"]), (2, 1))
        self.assertEqual(result["got"]["after"], 1)
        self.assertEqual(result["left"], 4)
        text = shop_exchange_message(Settings(), self.alice, result).text
        self.assertIn(f"换到了 1 号「{item['pig']['name']}」", text)
        with self.assertRaises(PiggyError):
            await self.db.shop_exchange("app", "group", self.bob["id"], 1, pay, now=NOW)
        self.assertEqual(await self.count(self.bob, pay), 1)
        labels = []
        original = ImageDraw.ImageDraw.text

        def record(canvas, xy, text, *args, **kwargs):
            labels.append(str(text))
            return original(canvas, xy, text, *args, **kwargs)

        shop = await self.db.shop("app", "group", self.bob["id"], now=NOW)
        with patch.object(ImageDraw.ImageDraw, "text", record):
            card = await shop_message(Settings(), self.root, self.bob, shop)
        self.assertTrue(card.local)
        self.assertIn("已被 阿离 换走", labels)
        self.assertIn("新图鉴！换到即解锁", labels)
        self.assertTrue(any("剩余 4/5 件" in label for label in labels))
        hosted = await shop_message(Settings(display={"shop": True}), self.root, self.bob, shop)
        self.assertFalse(hosted.local)
        self.assertIn("今日小猪商店", hosted.text)

    async def test_exchange_rejects_missing_payment_same_species_and_bad_slot(self):
        shop = await self.db.shop("app", "group", self.alice["id"], now=NOW)
        stocked = shop["items"][1]["pig_id"]
        pay = self.payment_for(shop, 2)
        await self.own(self.alice, stocked, 3)
        for slot, payment in ((2, stocked), (2, pay), (9, stocked)):
            with self.assertRaises(PiggyError):
                await self.db.shop_exchange(
                    "app", "group", self.alice["id"], slot, payment, now=NOW
                )
        self.assertEqual(await self.count(self.alice, stocked), 3)

    async def test_concurrent_buyers_only_one_gets_the_pig(self):
        shop = await self.db.shop("app", "group", self.alice["id"], now=NOW)
        pay = self.payment_for(shop, 3)
        buyers = [await self.db.identify("app", f"buyer{i}", "group", "") for i in range(6)]
        for buyer in buyers:
            await self.own(buyer, pay, 1)
        results = await asyncio.gather(
            *(
                Database(self.root).shop_exchange("app", "group", b["id"], 3, pay, now=NOW)
                for b in buyers
            ),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertTrue(all(isinstance(r, (dict, PiggyError)) for r in results))
        remaining = [await self.count(b, pay) for b in buyers]
        self.assertEqual(sorted(remaining), [0, 1, 1, 1, 1, 1])


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_version_one_database_gains_battle_tables_without_losing_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with closing(sqlite3.connect(root / "piggy.sqlite3")) as conn:
                conn.executescript("""
                    CREATE TABLE pigs (
                        id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                        analysis TEXT NOT NULL, asset TEXT NOT NULL,
                        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                        sort_order INTEGER NOT NULL
                    );
                    INSERT INTO pigs VALUES('pig','猪','d','a','x.png',1,0);
                    PRAGMA user_version=1;
                """)
            db = Database(root)
            await db.initialize()
            await db.initialize()
            rows = await db.run(lambda c: c.execute("SELECT id,battle FROM pigs").fetchall())
            self.assertEqual([tuple(r) for r in rows], [("pig", "")])
            version = await db.run(lambda c: c.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(version, 3)
            tables = await db.run(
                lambda c: {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
            )
            self.assertTrue({"requests", "battle_records"} <= tables)


if __name__ == "__main__":
    unittest.main()
