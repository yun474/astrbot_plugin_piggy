import asyncio
import json
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from core import raid_events
from core.battle import FALLBACK, fighter, validate_entry
from core.catalog import read_catalog
from core.config import PiggyError, Settings
from core.database import Database
from core.raid import DUNGEONS, _RaidBattle, boss_fighter, boss_level, simulate_raid
from core.raid_events import (
    ENTRY_EVENTS,
    FIELD_EVENTS,
    INTERLUDE_EVENTS,
    field_event,
    new_config,
    new_rewards,
    resolve,
    roll_entry,
)
from core.views import raid_battle_message, raid_list_message, raid_lobby_message

NOW = datetime(2026, 10, 8, 4, 0, tzinfo=timezone.utc)
BOSS_IDS = [slot for item in DUNGEONS for slot in item["bosses"]]
ENTRY = validate_entry("fallback", FALLBACK)


def outcome(won=True, dead=(), rounds=6, timeout=False):
    """A fake simulate_raid: `dead` seats fall, everyone else ends at half health."""

    def fake(heroes, boss, slot_id, cfg, seed):
        return {
            "winner": 0 if won else 1,
            "won": won,
            "timeout": timeout,
            "rounds": rounds,
            "log": ["R1 测试战斗"],
            "heroes": [
                {
                    "seat": h.get("seat"),
                    "hp": 0 if h.get("seat") in dead else h["stats"]["hp"] // 2,
                    "max_hp": h["stats"]["hp"],
                    "alive": h.get("seat") not in dead,
                }
                for h in heroes
            ],
            "boss": {"hp": 0 if won else 9, "max_hp": boss["stats"]["hp"]},
            "field_events": [],
        }

    return fake


def event(event_id):
    return next(e for e in ENTRY_EVENTS + INTERLUDE_EVENTS if e["id"] == event_id)


def unit(name, level=5, seat=None):
    pig = {"id": name, "name": name, "asset": ""}
    data = fighter(pig, ENTRY, level, name)
    data["seat"] = seat
    return data


def battle(slot, heroes=4, cfg=None, seed=1):
    boss = boss_fighter({"id": slot, "name": "BOSS", "asset": ""}, ENTRY, 5, 1, slot, "BOSS")
    party = [unit(f"猪{i}", seat=i) for i in range(heroes)]
    return _RaidBattle(party, boss, slot, cfg or new_config(), seed)


class RaidStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root)
        await self.db.initialize()
        images = self.root / "catalog" / "images"
        images.mkdir(parents=True)
        ids = BOSS_IDS + [f"p{i}" for i in range(6)]
        definitions = []
        for index, pig_id in enumerate(ids):
            Image.new("RGB", (8, 8), (index * 12, 90, 120)).save(images / f"{pig_id}.png")
            definitions.append(
                {"id": pig_id, "name": f"猪{pig_id}", "description": "描述", "analysis": "性格"}
            )
        (self.root / "catalog" / "pigs.json").write_text(json.dumps(definitions), "utf-8")
        await self.db.catalog(read_catalog(self.root))
        self.users = [await self.db.identify("app", f"u{i}", "group", f"玩家{i}") for i in range(6)]
        self.outsider = await self.db.identify("app", "out", "elsewhere", "路人")
        # Random events would change the payouts; tests that need them inject their own.
        for name, value in (("roll_entry", []), ("roll_interlude", event("nothing"))):
            patcher = patch(f"core.database.{name}", lambda *args, value=value: value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for index, user in enumerate(self.users):
            await self.own(user, f"p{index}", 2)

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

    async def raid_row(self):
        return await self.db.run(
            lambda c: dict(c.execute("SELECT * FROM raids ORDER BY id DESC LIMIT 1").fetchone())
        )

    async def entries(self):
        return await self.db.run(
            lambda c: c.execute("SELECT count(*) FROM raid_entries").fetchone()[0]
        )

    async def fill(self, key=1, now=NOW, users=None, fake=None):
        users = users or self.users[:4]
        await self.db.open_raid(
            "app", "group", users[0]["id"], key, f"p{self.users.index(users[0])}", now
        )
        result = None
        with patch("core.database.simulate_raid", fake or outcome()):
            for user in users[1:]:
                result = await self.db.join_raid(
                    "app", "group", user["id"], f"p{self.users.index(user)}", now=now
                )
        return result

    async def test_fourth_member_starts_the_first_boss_and_locks_the_party(self):
        leader = self.users[0]
        opened = await self.db.open_raid("app", "group", leader["id"], 1, "p0", NOW)
        self.assertEqual(len(opened["raid"]["members"]), 1)
        with self.assertRaises(PiggyError):
            await self.db.open_raid("app", "group", self.users[1]["id"], 2, "p1", NOW)
        with self.assertRaises(PiggyError):
            await self.db.join_raid("app", "group", leader["id"], "p0", now=NOW)
        with self.assertRaises(PiggyError):
            await self.db.join_raid("app", "group", self.users[1]["id"], "p5", now=NOW)
        with patch("core.database.simulate_raid", outcome()):
            for user in self.users[1:3]:
                result = await self.db.join_raid(
                    "app", "group", user["id"], f"p{self.users.index(user)}", now=NOW
                )
                self.assertFalse(result["started"])
            self.assertEqual(await self.entries(), 0)
            result = await self.db.join_raid("app", "group", self.users[3]["id"], "p3", now=NOW)
        self.assertTrue(result["started"])
        self.assertEqual(result["battle"]["stage"], 1)
        self.assertEqual(result["battle"]["status"], "waiting")
        self.assertEqual(await self.entries(), 4)
        with self.assertRaises(PiggyError) as caught:
            await self.db.join_raid("app", "group", self.users[4]["id"], "p4", now=NOW)
        self.assertIn("不能中途加入", str(caught.exception))
        lobby = raid_lobby_message(Settings(), opened, "open")
        self.assertIn("1/4", lobby.text)

    async def test_only_one_player_becomes_the_fourth_member(self):
        await self.db.open_raid("app", "group", self.users[0]["id"], 1, "p0", NOW)
        for index in (1, 2):
            await self.db.join_raid("app", "group", self.users[index]["id"], f"p{index}", now=NOW)
        with patch("core.database.simulate_raid", outcome()):
            results = await asyncio.gather(
                *(
                    Database(self.root).join_raid(
                        "app", "group", self.users[index]["id"], f"p{index}", now=NOW
                    )
                    for index in (3, 4, 5)
                ),
                return_exceptions=True,
            )
        self.assertEqual(sum(isinstance(r, dict) and r["started"] for r in results), 1)
        self.assertTrue(all(isinstance(r, (dict, PiggyError)) for r in results))
        self.assertEqual(await self.entries(), 4)

    async def test_members_can_leave_and_be_replaced_but_the_leader_disbands(self):
        await self.db.open_raid("app", "group", self.users[0]["id"], 1, "p0", NOW)
        await self.db.join_raid("app", "group", self.users[1]["id"], "p1", now=NOW)
        await self.db.join_raid("app", "group", self.users[2]["id"], "p2", now=NOW)
        left = await self.db.leave_raid("app", "group", self.users[1]["id"], NOW)
        self.assertFalse(left["cancelled"])
        self.assertEqual([m["seat"] for m in left["raid"]["members"]], [0, 1])
        await self.db.join_raid("app", "group", self.users[4]["id"], "p4", now=NOW)
        cancelled = await self.db.leave_raid("app", "group", self.users[0]["id"], NOW)
        self.assertTrue(cancelled["cancelled"])
        self.assertEqual((await self.raid_row())["status"], "cancelled")
        self.assertEqual(await self.entries(), 0)
        await self.db.open_raid("app", "group", self.users[1]["id"], 1, "p1", NOW)

    async def test_lobby_expires_after_ten_minutes_without_spending_entries(self):
        await self.db.open_raid("app", "group", self.users[0]["id"], 1, "p0", NOW)
        await self.db.join_raid("app", "group", self.users[1]["id"], "p1", now=NOW)
        later = NOW + timedelta(minutes=11)
        with self.assertRaises(PiggyError) as caught:
            await self.db.join_raid("app", "group", self.users[2]["id"], "p2", now=later)
        self.assertIn("自动取消", str(caught.exception))
        self.assertEqual((await self.raid_row())["status"], "cancelled")
        self.assertEqual(await self.entries(), 0)
        status = await self.db.raid_status("app", "group", self.users[0]["id"], later)
        self.assertIsNone(status["raid"])
        await self.db.open_raid("app", "group", self.users[0]["id"], 1, "p0", later)

    async def test_each_dungeon_once_per_player_per_day(self):
        await self.fill(1, fake=outcome(won=False))
        with self.assertRaises(PiggyError) as caught:
            await self.db.open_raid("app", "group", self.users[0]["id"], 1, "p0", NOW)
        self.assertIn("每个副本每人每天 1 次", str(caught.exception))
        await self.db.open_raid("app", "group", self.users[0]["id"], 2, "p0", NOW)
        await self.db.leave_raid("app", "group", self.users[0]["id"], NOW)
        tomorrow = NOW + timedelta(days=1)
        await self.db.open_raid("app", "group", self.users[0]["id"], 1, "p0", tomorrow)

    async def test_fallen_pigs_are_lost_and_victory_rewards_everyone(self):
        result = await self.fill(1, fake=outcome(won=True, dead=(1,)))
        battle = result["battle"]
        boss_id = DUNGEONS[0]["bosses"][0]
        self.assertTrue(battle["won"])
        self.assertEqual(await self.count(self.users[1], "p1"), 1)
        self.assertEqual(await self.count(self.users[0], "p0"), 2)
        for user in self.users[:4]:
            self.assertEqual(await self.count(user, boss_id), 1)
        for user, expected in ((self.users[0], 1), (self.users[5], 1), (self.outsider, 0)):
            self.assertEqual(await self.db.bonus_count("app", "group", user["id"], NOW), expected)
        members = {m["user_id"]: m for m in battle["raid"]["members"]}
        self.assertEqual(members[self.users[1]["id"]]["alive"], 0)
        self.assertAlmostEqual(members[self.users[0]["id"]]["hp"], 0.5, delta=0.02)
        with self.assertRaises(PiggyError):
            await self.db.continue_raid("app", "group", self.users[2]["id"], now=NOW)
        with patch("core.database.simulate_raid", outcome(won=False, dead=(0, 2, 3))):
            second = await self.db.continue_raid("app", "group", self.users[0]["id"], now=NOW)
        self.assertEqual(second["stage"], 2)
        self.assertEqual(second["status"], "failed")
        self.assertEqual([f["seat"] for f in second["fighters"]], [0, 2, 3])
        self.assertEqual(len(second["retired"]), 1)
        self.assertEqual(await self.count(self.users[0], "p0"), 1)
        self.assertEqual(await self.count(self.users[0], DUNGEONS[0]["bosses"][1]), 0)
        with self.assertRaises(PiggyError):
            await self.db.continue_raid("app", "group", self.users[0]["id"], now=NOW)

    async def test_undecided_leader_retreats_after_ten_minutes(self):
        await self.fill(2)
        later = NOW + timedelta(minutes=11)
        with self.assertRaises(PiggyError) as caught:
            await self.db.continue_raid("app", "group", self.users[0]["id"], now=later)
        self.assertIn("自动撤退", str(caught.exception))
        self.assertEqual((await self.raid_row())["status"], "retreated")

    async def test_leader_retreats_and_keeps_the_rewards(self):
        await self.fill(3)
        with self.assertRaises(PiggyError):
            await self.db.retreat_raid("app", "group", self.users[1]["id"], NOW)
        result = await self.db.retreat_raid("app", "group", self.users[0]["id"], NOW)
        self.assertEqual(result["raid"]["status"], "retreated")
        self.assertEqual(await self.count(self.users[1], DUNGEONS[2]["bosses"][0]), 1)
        self.assertIn("撤退", raid_lobby_message(Settings(), result, "retreated").text)
        with self.assertRaises(PiggyError):
            await self.db.continue_raid("app", "group", self.users[0]["id"], now=NOW)

    async def test_three_wins_clear_the_dungeon(self):
        await self.fill(1)
        with patch("core.database.simulate_raid", outcome()):
            second = await self.db.continue_raid("app", "group", self.users[0]["id"], now=NOW)
            third = await self.db.continue_raid("app", "group", self.users[0]["id"], now=NOW)
        self.assertEqual(second["status"], "waiting")
        self.assertEqual(third["status"], "cleared")
        self.assertEqual(third["next_boss"], "")
        for slot in DUNGEONS[0]["bosses"]:
            self.assertEqual(await self.count(self.users[2], slot), 1)
        with self.assertRaises(PiggyError):
            await self.db.continue_raid("app", "group", self.users[0]["id"], now=NOW)

    async def test_absent_pig_sits_out_and_missing_boss_is_possessed(self):
        await self.fill(1)
        await self.db.run(lambda c: c.execute("DELETE FROM collections WHERE pig_id='p2'"))
        await self.db.run(
            lambda c: c.execute("UPDATE pigs SET enabled=0 WHERE id=?", (DUNGEONS[0]["bosses"][1],))
        )
        with patch("core.database.simulate_raid", outcome()):
            second = await self.db.continue_raid("app", "group", self.users[0]["id"], now=NOW)
        self.assertEqual([m["user_id"] for m in second["absent"]], [self.users[2]["id"]])
        self.assertTrue(second["boss"]["possessed"])
        self.assertTrue(second["boss"]["label"].startswith("被附身的"))

    async def test_rare_events_pay_out(self):
        with patch(
            "core.database.roll_entry",
            lambda rng, key: [event("chest"), event("clover"), event("mimic")],
        ):
            result = await self.fill(1)
        battle = result["battle"]
        boss_id = DUNGEONS[0]["bosses"][0]
        self.assertEqual((battle["chest"], battle["bonus"], battle["copies"]), (1, 2, 2))
        self.assertGreaterEqual(await self.count(self.users[0], boss_id), 2)
        # 2 starting pigs, 2 boss pigs from the mimic and 1 random pig from the chest.
        self.assertEqual((await self.db.collection(self.users[0]["id"]))["total"], 5)
        self.assertEqual(await self.db.bonus_count("app", "group", self.users[5]["id"], NOW), 2)
        gained = sum(len(changes) for _, changes in battle["changes"])
        self.assertEqual(gained, 8)

    async def test_real_battle_poster_with_an_ally(self):
        await self.db.run(
            lambda c: c.executemany(
                "INSERT OR REPLACE INTO collections VALUES(?,?,5,0,0)",
                [(u["id"], f"p{i}") for i, u in enumerate(self.users[:4])],
            )
        )
        with patch("core.database.roll_entry", lambda rng, key: [event("ally")]):
            await self.db.open_raid("app", "group", self.users[0]["id"], 3, "p0", NOW)
            for index in (1, 2, 3):
                result = await self.db.join_raid(
                    "app", "group", self.users[index]["id"], f"p{index}", now=NOW, seed=11
                )
        battle = result["battle"]
        self.assertIsNotNone(battle["ally"])
        self.assertGreater(len(battle["result"]["log"]), 4)
        labels = []
        original = ImageDraw.ImageDraw.text

        def record(canvas, xy, text, *args, **kwargs):
            labels.append(str(text))
            return original(canvas, xy, text, *args, **kwargs)

        with patch.object(ImageDraw.ImageDraw, "text", record):
            poster = await raid_battle_message(Settings(), self.root, battle)
        self.assertTrue(poster.local)
        joined = "".join(labels)
        for text in ("BOSS", "援军", "野生援军", "战斗过程", "结算"):
            self.assertIn(text, joined)
        status = await self.db.raid_status("app", "group", self.users[0]["id"], NOW)
        listing = raid_list_message(Settings(), self.users[0], status)
        self.assertIn("三重锁链", listing.text)
        self.assertIn("今天已打过", listing.text)


class RaidEngineTests(unittest.TestCase):
    def setUp(self):
        self.boss_pig = {"id": "pig_god", "name": "猪神", "asset": ""}

    def test_same_seed_same_fight(self):
        party = [unit(f"猪{i}", seat=i) for i in range(4)]
        boss = boss_fighter(self.boss_pig, ENTRY, 5, 3, "pig_god", "猪神")
        first = simulate_raid(party, boss, "pig_god", new_config(), 42)
        again = simulate_raid(party, boss, "pig_god", new_config(), 42)
        self.assertEqual(first["log"], again["log"])
        self.assertEqual(first["heroes"], again["heroes"])

    def test_boss_follows_party_level_and_stage(self):
        self.assertEqual(boss_level([4, 6, 8, 10]), 7)
        weak = boss_fighter(self.boss_pig, ENTRY, 3, 1, "pig_god", "猪神")
        strong = boss_fighter(self.boss_pig, ENTRY, 12, 1, "pig_god", "猪神")
        later = boss_fighter(self.boss_pig, ENTRY, 12, 3, "pig_god", "猪神")
        self.assertLess(weak["stats"]["hp"], strong["stats"]["hp"])
        self.assertLess(strong["stats"]["hp"], later["stats"]["hp"])
        self.assertLess(strong["stats"]["atk"], later["stats"]["atk"])
        self.assertEqual(strong["dot_basis"], fighter(self.boss_pig, ENTRY, 12)["stats"]["hp"])

    def test_goblin_lurks_out_of_reach(self):
        fight = battle("goblin-pig")
        fight.round = 3
        fight.mech.round_start(fight)
        before = fight.boss.hp
        text = fight.damage(
            fight.heroes[0], fight.boss, {"type": "damage", "power": 1.0, "sure": True}
        )
        self.assertIn("打了个空", text)
        self.assertEqual(fight.boss.hp, before)
        fight.round = 4
        weakest = fight.heroes[2]
        weakest.hp = 20
        fight.mech.round_start(fight)
        self.assertLess(weakest.hp, 20)

    def test_frozen_chills_into_freeze_and_shell_blocks_crits(self):
        fight = battle("frozen-pig")
        fight.mech.setup(fight)
        self.assertFalse(fight.mech.crit_against(fight, fight.heroes[0]))
        hero = fight.heroes[0]
        for _ in range(3):
            fight.mech.outgoing(fight, hero, 1)
        self.assertEqual((hero.stun, hero.stun_label, hero.chill), (1, "冻结", 0))
        fight.mech.incoming(fight, hero, fight.mech.shell + 5, {})
        self.assertLess(fight.boss.mods["def"], 0)

    def test_everest_caps_each_hit(self):
        fight = battle("everest-pig")
        capped = fight.adjust_hit(fight.heroes[0], fight.boss, 10_000, {})
        self.assertAlmostEqual(capped, fight.boss.max_hp * 0.06)

    def test_404_rolls_back_once(self):
        fight = battle("error-404-pig")
        boss = fight.boss
        fight.hurt(boss, boss.max_hp * 0.7)
        self.assertAlmostEqual(boss.ratio, 0.6)
        fight.hurt(boss, boss.max_hp * 0.4)
        self.assertLess(boss.ratio, 0.4)

    def test_mechanical_overheats_after_six_hits(self):
        fight = battle("mechanical-pig")
        self.assertGreater(fight.boss.stat("def"), 0)
        for _ in range(6):
            fight.mech.incoming(fight, fight.heroes[0], 10, {})
        self.assertEqual(fight.boss.stat("def"), 0)
        fight.mech.round_end(fight)
        fight.mech.round_end(fight)
        self.assertGreater(fight.boss.stat("def"), 0)

    def test_cyberpunk_overclocks_into_two_actions(self):
        fight = battle("cyberpunk-pig")
        self.assertEqual(fight.mech.extra_actions(fight), 0)
        fight.hurt(fight.boss, fight.boss.max_hp * 0.7)
        self.assertEqual(fight.mech.extra_actions(fight), 1)
        fight.round = 1
        fight.mech.round_start(fight)
        fight.mech.dispelled(fight)
        self.assertEqual(fight.boss.mods.get("atk", 0), 0)

    def test_demon_feeds_on_fallen_pigs(self):
        fight = battle("demon-pig")
        fight.boss.hp = fight.boss.max_hp * 0.5
        fight.hurt(fight.heroes[0], 10_000)
        self.assertAlmostEqual(fight.boss.ratio, 0.65)
        self.assertEqual(fight.boss.mods["atk"], 10)

    def test_chained_king_breaks_free(self):
        fight = battle("chained_crown_pig")
        fight.mech.setup(fight)
        self.assertEqual(fight.boss.mods["atk"], -30)
        fight.hurt(fight.boss, fight.boss.max_hp * 0.3)
        self.assertEqual((fight.mech.chains, fight.boss.mods["atk"]), (2, -20))
        fight.hurt(fight.boss, fight.boss.max_hp * 0.5)
        self.assertEqual((fight.mech.chains, fight.boss.mods["atk"]), (0, 0))
        self.assertTrue(fight.mech.returned)
        self.assertAlmostEqual(fight.boss.ratio, 0.35)

    def test_pig_god_guardians_must_fall_first(self):
        fight = battle("pig_god")
        self.assertIs(fight.target_for(fight.heroes[0]), fight.boss)
        fight.hurt(fight.boss, fight.boss.max_hp * 0.6)
        guards = fight.foes[1:]
        self.assertEqual(len(guards), 2)
        for _ in range(10):
            self.assertIn(fight.target_for(fight.heroes[0]), guards)
        for guard in guards:
            fight.hurt(guard, 10_000)
        self.assertIs(fight.target_for(fight.heroes[0]), fight.boss)

    def test_boss_mechanics_are_listed_for_every_boss(self):
        for slot in BOSS_IDS:
            fight = battle(slot)
            self.assertEqual(len(fight.mech.MECHANICS), 3, slot)
            result = fight.run()
            self.assertIn(result["winner"], (0, 1))


class RaidEventTests(unittest.TestCase):
    def party(self):
        return [{"seat": i, "label": f"猪{i}", "hp": 0.5, "mods": {}} for i in range(4)]

    def test_every_entry_and_interlude_event_resolves(self):
        for item in ENTRY_EVENTS + INTERLUDE_EVENTS:
            party, cfg, rewards = self.party(), new_config(), new_rewards()
            resolved = resolve([item], party, random.Random(3), cfg, rewards, (("机制", ""),) * 3)
            self.assertNotIn("{", resolved[0]["text"], item["id"])
            fight = battle("pig_god", cfg=cfg)
            fight.run()

    def test_event_effects(self):
        party, cfg, rewards = self.party(), new_config(), new_rewards()
        rng = random.Random(1)
        resolve(
            [event("spring"), event("drums"), event("nap"), event("charm")],
            party,
            rng,
            cfg,
            rewards,
        )
        self.assertEqual([m["hp"] for m in party], [0.8] * 4)
        fight = battle("goblin-pig", cfg=cfg)
        self.assertEqual(fight.heroes[0].mods["atk"], 15)
        self.assertEqual(fight.boss.stun, 1)
        hero = fight.heroes[1]
        fight.hurt(hero, 10_000)
        self.assertEqual(hero.hp, 1.0)
        resolve([event("rockfall")] * 10, party, rng, cfg, rewards)
        self.assertTrue(all(m["hp"] == 0.01 for m in party))
        resolve([event("page")], party, rng, cfg, rewards)
        self.assertEqual(sum(m["mods"].get("atk", 0) for m in party), 10)
        resolve([event("tablet")], party, rng, cfg, rewards, (("甲", ""), ("乙", ""), ("丙", "")))
        self.assertIn(cfg["disabled"], (0, 1, 2))
        self.assertFalse(battle("pig_god", cfg=cfg).mech.on(cfg["disabled"]))

    def test_dungeon_events_stay_in_their_dungeon(self):
        rng = random.Random(5)
        seen = {1: set(), 2: set(), 3: set()}
        for key in seen:
            for _ in range(600):
                seen[key] |= {e["dungeon"] for e in roll_entry(rng, key)}
            self.assertEqual(seen[key], {None, key})

    def test_every_field_event(self):
        for item in FIELD_EVENTS:
            fight = battle("pig_god")
            fight.heroes[0].buffs.append({"stat": "atk", "pct": 10, "turns": 3})
            with (
                patch.object(raid_events, "FIELD_CHANCE", 1.0),
                patch.object(raid_events, "_pick", lambda rng, pool, item=item: item),
            ):
                field_event(fight)
            fight.flush()
            self.assertEqual(fight.field_events, [item["name"]])
            self.assertTrue(any(item["name"] in line for line in fight.log))


if __name__ == "__main__":
    unittest.main()
