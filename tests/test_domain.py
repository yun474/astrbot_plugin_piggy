import asyncio
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from core.catalog import initialize_catalog, read_catalog
from core.config import PiggyError, Settings
from core.database import Database
from core.diagnostics import safe_detail
from core.views import collection_message, keyboard, md, ranking_message, today_message


class DomainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root)
        await self.db.initialize()
        (self.root / "catalog" / "images").mkdir(parents=True)
        self.manifest = self.root / "catalog" / "pigs.json"
        self.write_catalog(["pig", "cat"])
        await self.db.catalog(read_catalog(self.root))
        self.user = await self.db.identify("app", "member", "group-a", "昵称")

    async def asyncTearDown(self):
        self.temp.cleanup()

    def write_catalog(self, ids):
        definitions = []
        for index, pig_id in enumerate(ids):
            Image.new("RGBA", (25, 30), (220, 40 + index, 60, 200)).save(
                self.root / "catalog" / "images" / f"{pig_id}.png"
            )
            definitions.append(
                {
                    "id": pig_id,
                    "name": pig_id,
                    "description": "固定描述",
                    "analysis": "固定性格",
                }
            )
        self.manifest.write_text(json.dumps(definitions), "utf-8")

    async def test_cross_group_concurrent_draw_is_one_record_even_with_separate_connections(
        self,
    ):
        now = datetime(2026, 9, 13, 0, 0, tzinfo=timezone.utc)
        results = await asyncio.gather(
            *[self.db.draw(self.user["id"], f"group-{i}", f"event-{i}", now) for i in range(32)]
        )
        self.assertEqual(sum(r["created"] for r in results), 1)
        self.assertEqual(len({r["pig"]["id"] for r in results}), 1)
        self.assertEqual((await self.db.collection(self.user["id"]))["total"], 1)
        await Database(self.root).initialize()
        again = await Database(self.root).draw(self.user["id"], "group-b", "retry", now)
        self.assertFalse(again["created"])
        self.assertEqual(again["count"], 1)

    async def test_east_asia_midnight_and_inactive_history_snapshot(self):
        before = datetime(2026, 9, 12, 15, 59, 59, tzinfo=timezone.utc)
        first = await self.db.draw(self.user["id"], "a", "one", before)
        await self.db.draw(self.user["id"], "a", "two", before + timedelta(seconds=1))
        await self.db.catalog(
            [
                {
                    "id": "new",
                    "name": "新猪",
                    "description": "新增",
                    "analysis": "新增",
                    "asset": first["pig"]["asset"],
                    "enabled": True,
                    "sort_order": 0,
                }
            ]
        )
        old = await self.db.draw(self.user["id"], "b", "three", before)
        self.assertEqual(old["pig"], first["pig"])
        progress = await self.db.collection(self.user["id"])
        self.assertEqual(progress["total"], 2)
        self.assertEqual(progress["active_total"], 1)
        self.assertEqual(progress["unlocked"], 0)
        self.assertTrue(any(not p["enabled"] and p["count"] for p in progress["entries"]))

    async def test_names_are_not_identity_and_app_ids_are_isolated(self):
        second = await self.db.identify("app", "second", "group-a", "昵称")
        foreign = await self.db.identify("other-app", "member", "group-a", "昵称")
        self.assertEqual(len({self.user["id"], second["id"], foreign["id"]}), 3)
        updated = await self.db.identify("app", "member", "group-b", "新昵称")
        blank = await self.db.identify("app", "member", "group-a", "")
        self.assertEqual(updated["id"], self.user["id"])
        self.assertEqual(blank["nickname"], "新昵称")

    async def test_rank_uses_global_counts_but_local_participants_and_ties(self):
        now = datetime.now(timezone.utc)
        second = await self.db.identify("app", "second", "group-a", "昵称")
        hidden = await self.db.identify("app", "hidden", "group-other", "别群玩家")
        for user in (self.user, second, hidden):
            await self.db.draw(user["id"], "group-other", "event", now)
        result = await self.db.rankings("app", "group-a")
        self.assertEqual(len(result["total"]), 2)
        self.assertEqual([p["rank"] for p in result["total"]], [1, 1])
        self.assertEqual({p["open_id"] for p in result["total"]}, {"member", "second"})
        with self.assertRaises(PiggyError):
            await self.db.rankings("app", "")

    async def test_bad_manifest_does_not_replace_catalog(self):
        definitions = json.loads(self.manifest.read_text())
        definitions.append(definitions[0])
        self.manifest.write_text(json.dumps(definitions), "utf-8")
        with self.assertRaises(PiggyError):
            await self.db.catalog(read_catalog(self.root))
        self.assertEqual((await self.db.collection(self.user["id"]))["active_total"], 2)
        definitions = definitions[:1]
        definitions[0]["image"] = "../../outside.png"
        self.manifest.write_text(json.dumps(definitions), "utf-8")
        with self.assertRaises(PiggyError):
            read_catalog(self.root)

    async def test_restore_backup_and_rotation(self):
        await self.db.draw(self.user["id"], "a", "event")
        for _ in range(3):
            backup = await self.db.backup(2)
        self.assertEqual(len(list((self.root / "backups").glob("*.zip"))), 2)
        restore_root = self.root / "restored"
        with zipfile.ZipFile(backup) as archive:
            archive.extractall(restore_root)
        restored = Database(restore_root)
        await restored.initialize()
        self.assertEqual((await restored.collection(self.user["id"]))["total"], 1)
        self.assertEqual(len(read_catalog(restore_root)), 2)
        assets = list((restore_root / "assets").glob("*"))
        self.assertEqual(len(assets), 2)

    async def test_corrupt_database_is_not_overwritten(self):
        target = self.root / "broken"
        target.mkdir()
        path = target / "piggy.sqlite3"
        path.write_bytes(b"not a database")
        with self.assertRaises(PiggyError):
            await Database(target).initialize()
        self.assertEqual(path.read_bytes(), b"not a database")

    async def test_transaction_rolls_back_if_collection_write_fails(self):
        await self.db.run(
            lambda c: c.execute("""
            CREATE TRIGGER reject_collection BEFORE INSERT ON collections
            BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END
        """)
        )
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.draw(self.user["id"], "a", "event")
        count = await self.db.run(
            lambda c: c.execute("SELECT count(*) FROM draw_records").fetchone()[0]
        )
        self.assertEqual(count, 0)

    async def test_atlas_renders_all_slots_without_names_and_hides_locked_pigs(self):
        await self.db.draw(self.user["id"], "group-a", "draw")
        progress = await self.db.collection(self.user["id"])
        labels = []
        draw_text = ImageDraw.ImageDraw.text

        def record_text(canvas, xy, text, *args, **kwargs):
            labels.append(str(text))
            return draw_text(canvas, xy, text, *args, **kwargs)

        with patch.object(ImageDraw.ImageDraw, "text", record_text):
            message = await collection_message(
                Settings(display={"atlas": True}), self.root, self.user, progress, 1, True
            )
        self.assertEqual(len(message.images), 1)
        for pig in progress["entries"]:
            self.assertNotIn(pig["name"], labels)
        self.assertIn("已解锁 1 / 2", labels)
        with Image.open(io.BytesIO(message.images[0])) as img:
            self.assertEqual(img.width, 1080)
            for i, pig in enumerate(progress["entries"]):
                r, g, b = img.getpixel((113 + 122 * i, 439))
                if pig["count"]:
                    self.assertNotEqual(r, g)
                else:
                    self.assertEqual(r, g)
                    self.assertEqual(g, b)
        with self.assertRaises(PiggyError):
            await collection_message(Settings(), self.root, self.user, progress, 0, True)
        with self.assertRaises(PiggyError):
            await collection_message(
                Settings(display={"atlas": True}), self.root, self.user, progress, 2, True
            )

    async def test_locked_atlas_does_not_read_assets_or_reveal_changes_to_them(self):
        progress = await self.db.collection(self.user["id"])
        before = await collection_message(Settings(), self.root, self.user, progress, 1, True)
        for pig in progress["entries"]:
            pig["asset"] = "hidden-does-not-exist.png"
            pig["name"] = "不可泄露的猪名"
        after = await collection_message(Settings(), self.root, self.user, progress, 1, True)
        self.assertEqual(before.images, after.images)

    async def test_pen_only_renders_owned_names_and_empty_pen_is_a_card(self):
        progress = await self.db.collection(self.user["id"])
        empty = await collection_message(Settings(), self.root, self.user, progress, 1, False)
        self.assertEqual(len(empty.images), 1)
        self.assertTrue(empty.images[0].startswith(b"\x89PNG"))
        self.assertFalse((self.root / "cards").exists())
        await self.db.draw(self.user["id"], "a", "draw")
        progress = await self.db.collection(self.user["id"])
        labels = []
        original = ImageDraw.ImageDraw.text

        def record(canvas, xy, text, *args, **kwargs):
            labels.append(str(text))
            return original(canvas, xy, text, *args, **kwargs)

        with patch.object(ImageDraw.ImageDraw, "text", record):
            await collection_message(Settings(), self.root, self.user, progress, 1, False)
        for pig in progress["entries"]:
            self.assertEqual(pig["name"] in labels, bool(pig["count"]))

    async def test_current_96_catalog_is_one_card_and_future_expansion_has_navigation(self):
        progress = await self.db.collection(self.user["id"])
        template = progress["entries"][0]
        progress["entries"] = [{**template, "id": f"pig-{i}"} for i in range(96)]
        progress["active_total"] = 96
        one = await collection_message(
            Settings(display={"atlas": True}), self.root, self.user, progress, 1, True
        )
        self.assertEqual(len(one.images), 1)
        self.assertEqual(len(one.keyboard["content"]["rows"]), 2)
        progress["entries"] += [{**template, "id": f"extra-{i}"} for i in range(100)]
        progress["active_total"] = 196
        last = await collection_message(
            Settings(display={"atlas": True}), self.root, self.user, progress, 2, True
        )
        self.assertEqual(
            last.keyboard["content"]["rows"][-1]["buttons"][0]["action"]["data"], "/小猪图鉴 1"
        )

    async def test_all_bundled_assets_validate_and_existing_catalog_is_preserved(self):
        resources = Path(__file__).resolve().parents[1] / "resources"
        other = self.root / "bundled"
        initialize_catalog(other, resources)
        definitions = read_catalog(other)
        self.assertEqual(len(definitions), 96)
        manifest = other / "catalog" / "pigs.json"
        original = manifest.read_text("utf-8")
        manifest.write_text("[]", "utf-8")
        initialize_catalog(other, resources)
        self.assertEqual(manifest.read_text("utf-8"), "[]")
        manifest.write_text(original, "utf-8")


class ConfigAndButtonsTests(unittest.TestCase):
    def test_s3_config_trims_pasted_whitespace_and_names_invalid_fields(self):
        config = {
            "endpoint": " https://s3.example.com \n",
            "bucket": " pigs ",
            "access_key": " key ",
            "secret_key": " secret ",
            "public_base_url": " https://images.example.com ",
            "region": " auto ",
        }
        settings = Settings.from_dict(config)
        settings.check_host()
        self.assertEqual(settings.access_key, "key")
        self.assertEqual(settings.region, "auto")
        for field, value in (
            ("bucket", "https://example.com/pigs"),
            ("endpoint", "https://s3.example.com?token=x"),
            ("region", " "),
        ):
            with self.assertRaisesRegex(PiggyError, field):
                Settings.from_dict({**config, field: value}).check_host()

    def test_diagnostics_hide_secrets_proxy_passwords_and_signed_queries(self):
        settings = Settings(access_key="ACCESS_PRIVATE", secret_key="SECRET_PRIVATE")
        detail = safe_detail(
            "ACCESS_PRIVATE SECRET_PRIVATE https://user:password@proxy.local:8080/path "
            "https://s3.example.com/a?X-Amz-Signature=hidden-signature&token=hidden-token "
            "\nAuthorization: Bearer hidden-bearer\nSSL CERTIFICATE_VERIFY_FAILED",
            settings,
        )
        for secret in (
            "ACCESS_PRIVATE",
            "SECRET_PRIVATE",
            "user:password",
            "hidden-signature",
            "hidden-token",
            "hidden-bearer",
        ):
            self.assertNotIn(secret, detail)
        self.assertIn("CERTIFICATE_VERIFY_FAILED", detail)
        self.assertNotIn("\n", detail)

    def test_today_mentions_requester_and_quotes_all_description_lines(self):
        result = {
            "pig": {
                "name": "猪",
                "asset": "pig.png",
                "description": "第一行\n第二行",
                "analysis": "性格\n<@!someone>",
            },
            "created": True,
            "new_species": True,
            "day": "2026-09-14",
            "count": 1,
        }
        user = {"id": 1, "open_id": "actual-member", "nickname": "不应出现在正文的昵称"}
        progress = {"unlocked": 1, "active_total": 96, "total": 1}
        message = today_message(Settings(), Path("."), user, result, progress)
        self.assertTrue(message.text.startswith("<@!actual-member>\n"))
        self.assertNotIn(user["nickname"], message.text)
        self.assertIn("\n\n首次解锁！\n\n**猪**\n\n![", message.text)
        self.assertIn("> 第一行\n> 第二行\n>\n> 性格\n> ", message.text)
        self.assertNotIn("<@!someone>", message.text)
        result["created"] = False
        repeated = today_message(Settings(), Path("."), user, result, progress)
        self.assertNotIn("首次解锁", repeated.text)
        self.assertIn("今天已经抽过", repeated.text)

    def test_both_rankings_only_show_ten_players_without_pagination(self):
        user = {"open_id": "requester"}
        players = [
            {"id": i, "nickname": f"玩家{i:02d}", "rank": i, "species": 20 - i, "total": 50 - i}
            for i in range(1, 13)
        ]
        labels = []
        original = ImageDraw.ImageDraw.text

        def record(canvas, xy, text, *args, **kwargs):
            labels.append(str(text))
            return original(canvas, xy, text, *args, **kwargs)

        with patch.object(ImageDraw.ImageDraw, "text", record):
            message = ranking_message(
                Settings(display={"ranking": True}),
                user,
                {"species": players, "total": players},
                {},
            )
        self.assertEqual(labels.count("玩家10"), 2)
        self.assertNotIn("玩家11", labels)
        self.assertNotIn("玩家12", labels)
        self.assertIn("收集种类榜", labels)
        self.assertIn("累计数量榜", labels)
        self.assertEqual(len(message.keyboard["content"]["rows"]), 2)
        self.assertFalse(message.local)

    def test_json_config_and_retry_validation(self):
        settings = Settings.from_dict(
            {
                "upload_headers": '{"Authorization":"Bearer secret"}',
                "success_value": "200",
            }
        )
        self.assertEqual(settings.upload_headers["Authorization"], "Bearer secret")
        self.assertEqual(settings.success_value, 200)
        self.assertNotIn("Bearer secret", repr(settings))
        for bad in (-1, 11, True, "3"):
            with self.assertRaises(PiggyError):
                Settings.from_dict({"image_retry_count": bad})

    def test_button_owner_prefix_and_navigation(self):
        board = keyboard(Settings(command_prefix="!"), "member", "小猪图鉴", 2, 3)
        rows = board["content"]["rows"]
        self.assertEqual(rows[0]["buttons"][0]["action"]["data"], "!今日小猪")
        for button in rows[-1]["buttons"]:
            self.assertEqual(button["action"]["permission"]["specify_user_ids"], ["member"])
            self.assertNotIn("enter", button["action"])
        self.assertNotIn("{{image:0}}", md("{{image:0}}"))


if __name__ == "__main__":
    unittest.main()
