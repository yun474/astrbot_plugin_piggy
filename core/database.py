import asyncio
import json
import random
import secrets
import sqlite3
import tempfile
import time
import zipfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .battle import SKILL_SLOTS, entry_for, fighter, level_for, simulate
from .config import PiggyError
from .raid import (
    DUNGEONS,
    PARTY_SIZE,
    REST_HEAL,
    TIMEOUT_MINUTES,
    boss_fighter,
    boss_level,
    dungeon,
    mechanics_for,
    simulate_raid,
)
from .raid_events import new_config, new_rewards, resolve, roll_entry, roll_interlude

EAST_ASIA = timezone(timedelta(hours=8))
# Safety valve only: with the default 50% chain chance this is never reached.
MAX_DRAW_ITEMS = 500
_SYSTEM_RANDOM = secrets.SystemRandom()
SHOP_SIZE = 5
REQUEST_LABELS = {"duel": "斗猪", "trade": "交换"}
RAID_ACTIVE = ("forming", "waiting")


def player_name(user) -> str:
    return user["alias"] or user["nickname"] or f"玩家 {user['id']:04d}"


def _owned(conn, user_id: int, pig_id: str) -> int:
    row = conn.execute(
        "SELECT count FROM collections WHERE user_id=? AND pig_id=?", (user_id, pig_id)
    ).fetchone()
    return row[0] if row else 0


def _take(conn, user_id: int, pig_id: str):
    count = _owned(conn, user_id, pig_id)
    if count < 1:
        raise PiggyError("小猪已经不在原主人的猪圈里了。")
    if count == 1:
        conn.execute("DELETE FROM collections WHERE user_id=? AND pig_id=?", (user_id, pig_id))
    else:
        conn.execute(
            "UPDATE collections SET count=count-1 WHERE user_id=? AND pig_id=?", (user_id, pig_id)
        )


def _give(conn, user_id: int, pig_id: str, now: float):
    conn.execute(
        """
        INSERT INTO collections VALUES(?,?,1,?,?) ON CONFLICT(user_id,pig_id)
        DO UPDATE SET count=count+1,last_at=excluded.last_at
        """,
        (user_id, pig_id, now, now),
    )


def _transfer(conn, source: int, target: int, pig_id: str, now: float):
    _take(conn, source, pig_id)
    _give(conn, target, pig_id, now)


def _level_change(pig: dict, before: int, after: int, cap: int) -> dict:
    skills = entry_for(pig.get("battle"))["skills"]
    old = level_for(before, cap) if before else 0
    new = level_for(after, cap) if after else 0
    old_slots, new_slots = min(old, SKILL_SLOTS), min(new, SKILL_SLOTS)
    return {
        "pig": {k: v for k, v in pig.items() if k != "battle"},
        "count_before": before,
        "count_after": after,
        "before": old,
        "after": new,
        "lost": [s["name"] for s in skills[new_slots:old_slots]],
        "gained": [s["name"] for s in skills[old_slots:new_slots]],
    }


class Database:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "piggy.sqlite3"

    async def run(self, function):
        def execute():
            with closing(sqlite3.connect(self.path, timeout=15)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA synchronous=FULL")
                with conn:
                    return function(conn)

        return await asyncio.to_thread(execute)

    async def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)

        def initialize(conn):
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise PiggyError("数据库检查失败，已停止写入；请检查备份，数据库不会被自动清空。")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3):
                raise PiggyError("数据库版本高于当前插件支持版本，请勿降级运行。")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY, app_id TEXT NOT NULL, open_id TEXT NOT NULL,
                    nickname TEXT NOT NULL DEFAULT '', alias TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL, UNIQUE(app_id, open_id)
                );
                CREATE TABLE IF NOT EXISTS group_players (
                    app_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL REFERENCES users(id), last_seen REAL NOT NULL,
                    PRIMARY KEY(app_id, group_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS pigs (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                    analysis TEXT NOT NULL, asset TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), sort_order INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS draw_records (
                    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                    day TEXT NOT NULL, drawn_at REAL NOT NULL, pig_id TEXT NOT NULL REFERENCES pigs(id),
                    source_group TEXT NOT NULL, source_event TEXT NOT NULL, snapshot TEXT NOT NULL,
                    UNIQUE(user_id, day)
                );
                CREATE TABLE IF NOT EXISTS collections (
                    user_id INTEGER NOT NULL REFERENCES users(id), pig_id TEXT NOT NULL REFERENCES pigs(id),
                    count INTEGER NOT NULL CHECK(count > 0), first_at REAL NOT NULL, last_at REAL NOT NULL,
                    PRIMARY KEY(user_id, pig_id)
                );
                CREATE TABLE IF NOT EXISTS image_cache (
                    namespace TEXT NOT NULL, digest TEXT NOT NULL, object_key TEXT NOT NULL,
                    url TEXT NOT NULL, uploaded_at REAL NOT NULL,
                    PRIMARY KEY(namespace, digest)
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    message_key TEXT PRIMARY KEY, sequence INTEGER NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS draws_user_pig ON draw_records(user_id, pig_id);
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY, app_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('duel','trade')),
                    from_user INTEGER NOT NULL REFERENCES users(id),
                    to_user INTEGER NOT NULL REFERENCES users(id),
                    give_pig TEXT NOT NULL REFERENCES pigs(id), want_pig TEXT REFERENCES pigs(id),
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN
                        ('pending','accepted','declined','cancelled','expired','failed')),
                    created_at REAL NOT NULL, expires_at REAL NOT NULL, resolved_at REAL
                );
                CREATE INDEX IF NOT EXISTS requests_to ON requests(to_user, kind, status);
                CREATE INDEX IF NOT EXISTS requests_from ON requests(from_user, kind, status);
                CREATE TABLE IF NOT EXISTS battle_records (
                    id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES requests(id),
                    day TEXT NOT NULL, fought_at REAL NOT NULL,
                    a_user INTEGER NOT NULL REFERENCES users(id), a_pig TEXT NOT NULL,
                    a_level INTEGER NOT NULL,
                    b_user INTEGER NOT NULL REFERENCES users(id), b_pig TEXT NOT NULL,
                    b_level INTEGER NOT NULL,
                    winner INTEGER NOT NULL REFERENCES users(id), seed INTEGER NOT NULL,
                    log TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS battles_a ON battle_records(a_user, day);
                CREATE INDEX IF NOT EXISTS battles_b ON battle_records(b_user, day);
                CREATE TABLE IF NOT EXISTS shop_items (
                    app_id TEXT NOT NULL, group_id TEXT NOT NULL, day TEXT NOT NULL,
                    slot INTEGER NOT NULL, pig_id TEXT NOT NULL REFERENCES pigs(id),
                    sold_to INTEGER REFERENCES users(id), paid_pig TEXT REFERENCES pigs(id),
                    sold_at REAL,
                    PRIMARY KEY(app_id, group_id, day, slot)
                );
                CREATE TABLE IF NOT EXISTS draw_sessions (
                    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                    app_id TEXT NOT NULL, group_id TEXT NOT NULL, day TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('daily','bonus')),
                    event_id TEXT NOT NULL, created_at REAL NOT NULL, items TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS draw_sessions_user ON draw_sessions(user_id, day);
                CREATE TABLE IF NOT EXISTS draw_bonus (
                    app_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL REFERENCES users(id), day TEXT NOT NULL,
                    count INTEGER NOT NULL CHECK(count >= 0),
                    PRIMARY KEY(app_id, group_id, user_id, day)
                );
                CREATE TABLE IF NOT EXISTS wild_pigs (
                    id INTEGER PRIMARY KEY, app_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    day TEXT NOT NULL, pig_id TEXT NOT NULL REFERENCES pigs(id),
                    level INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','defeated','expired')),
                    attempts INTEGER NOT NULL DEFAULT 0, spawned_at REAL NOT NULL,
                    defeated_by INTEGER REFERENCES users(id), defeated_at REAL,
                    UNIQUE(app_id, group_id, day)
                );
                CREATE TABLE IF NOT EXISTS wild_battles (
                    id INTEGER PRIMARY KEY, wild_id INTEGER NOT NULL REFERENCES wild_pigs(id),
                    user_id INTEGER NOT NULL REFERENCES users(id), pig_id TEXT NOT NULL,
                    level INTEGER NOT NULL, won INTEGER NOT NULL, seed INTEGER NOT NULL,
                    log TEXT NOT NULL, summary TEXT NOT NULL, fought_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS raids (
                    id INTEGER PRIMARY KEY, app_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    day TEXT NOT NULL, dungeon INTEGER NOT NULL,
                    leader INTEGER NOT NULL REFERENCES users(id),
                    status TEXT NOT NULL DEFAULT 'forming' CHECK(status IN
                        ('forming','waiting','cleared','retreated','failed','cancelled')),
                    stage INTEGER NOT NULL DEFAULT 0, seed INTEGER NOT NULL,
                    created_at REAL NOT NULL, expires_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS raids_group ON raids(app_id, group_id, status);
                CREATE INDEX IF NOT EXISTS raids_status ON raids(status, expires_at);
                CREATE TABLE IF NOT EXISTS raid_members (
                    raid_id INTEGER NOT NULL REFERENCES raids(id),
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    pig_id TEXT NOT NULL REFERENCES pigs(id), seat INTEGER NOT NULL,
                    hp REAL NOT NULL DEFAULT 1, alive INTEGER NOT NULL DEFAULT 1,
                    mods TEXT NOT NULL DEFAULT '{}', joined_at REAL NOT NULL,
                    PRIMARY KEY(raid_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS raid_members_user ON raid_members(user_id);
                CREATE TABLE IF NOT EXISTS raid_entries (
                    user_id INTEGER NOT NULL REFERENCES users(id), day TEXT NOT NULL,
                    dungeon INTEGER NOT NULL, raid_id INTEGER NOT NULL REFERENCES raids(id),
                    PRIMARY KEY(user_id, day, dungeon)
                );
                CREATE TABLE IF NOT EXISTS raid_battles (
                    id INTEGER PRIMARY KEY, raid_id INTEGER NOT NULL REFERENCES raids(id),
                    stage INTEGER NOT NULL, boss_pig TEXT NOT NULL, boss_level INTEGER NOT NULL,
                    won INTEGER NOT NULL, seed INTEGER NOT NULL, events TEXT NOT NULL,
                    log TEXT NOT NULL, summary TEXT NOT NULL, fought_at REAL NOT NULL
                );
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(pigs)")}
            if "battle" not in columns:
                conn.execute("ALTER TABLE pigs ADD COLUMN battle TEXT NOT NULL DEFAULT ''")
            records = {row[1] for row in conn.execute("PRAGMA table_info(battle_records)")}
            if "summary" not in records:
                # Older records have no summary; posters for them simply omit the HP bars.
                conn.execute(
                    "ALTER TABLE battle_records ADD COLUMN summary TEXT NOT NULL DEFAULT ''"
                )
            conn.execute("PRAGMA user_version=3")
            conn.execute("COMMIT")

        try:
            await self.run(initialize)
        except sqlite3.DatabaseError as exc:
            raise PiggyError("数据库损坏或不可写，已保留原文件，请检查磁盘和备份。") from exc

    async def catalog(self, pigs: list[dict]):
        def update(conn):
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE pigs SET enabled=0")
            conn.executemany(
                """
                INSERT INTO pigs(id,name,description,analysis,asset,enabled,sort_order,battle)
                VALUES(:id,:name,:description,:analysis,:asset,:enabled,:sort_order,:battle)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, description=excluded.description,
                analysis=excluded.analysis, asset=excluded.asset, enabled=excluded.enabled,
                sort_order=excluded.sort_order, battle=excluded.battle
            """,
                [{"battle": "", **pig} for pig in pigs],
            )

        await self.run(update)

    async def has_catalog(self) -> bool:
        return await self.run(
            lambda c: bool(c.execute("SELECT 1 FROM pigs WHERE enabled=1").fetchone())
        )

    async def identify(self, app_id: str, open_id: str, group_id: str, nickname: str) -> dict:
        if not app_id or not open_id:
            raise PiggyError("未取得机器人或用户的官方标识，不能安全关联收藏。")
        nickname = " ".join(nickname.split())[:64]
        now = time.time()

        def update(conn):
            conn.execute(
                """
                INSERT INTO users(app_id,open_id,nickname,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(app_id,open_id) DO UPDATE SET
                nickname=CASE WHEN excluded.nickname<>'' THEN excluded.nickname ELSE users.nickname END,
                updated_at=excluded.updated_at
            """,
                (app_id, open_id, nickname, now),
            )
            user = dict(
                conn.execute(
                    "SELECT * FROM users WHERE app_id=? AND open_id=?",
                    (app_id, open_id),
                ).fetchone()
            )
            if group_id:
                conn.execute(
                    """
                    INSERT INTO group_players VALUES(?,?,?,?) ON CONFLICT(app_id,group_id,user_id)
                    DO UPDATE SET last_seen=excluded.last_seen
                """,
                    (app_id, group_id, user["id"], now),
                )
            return user

        return await self.run(update)

    async def set_alias(self, user_id: int, alias: str):
        alias = " ".join(alias.split())
        if not 1 <= len(alias) <= 24:
            raise PiggyError("称呼请输入 1–24 个字。")
        await self.run(lambda c: c.execute("UPDATE users SET alias=? WHERE id=?", (alias, user_id)))

    async def draw(
        self,
        user_id: int,
        group_id: str,
        event_id: str,
        now: datetime | None = None,
        *,
        duplicate_rate_cap: int = 20,
        duplicate_pity: int = 2,
        gather_chance: int = 0,
        chain_chance: int = 0,
        app_id: str = "",
        rng=None,
    ) -> dict:
        """Daily draw, or a bonus draw when today's is done and a chance is left.

        Every pig drawn (the first one and each chained one) independently rolls
        "gather" (one extra copy) and then "chain" (draw another pig).
        """
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("The draw clock must be timezone-aware")
        day = now.astimezone(EAST_ASIA).date().isoformat()
        below = rng.randrange if rng else secrets.randbelow
        choose = rng.choice if rng else secrets.choice
        roll = rng.random if rng else _SYSTEM_RANDOM.random

        def draw(conn):
            conn.execute("BEGIN IMMEDIATE")

            def repeat_streak():
                records = conn.execute(
                    """
                    SELECT EXISTS(SELECT 1 FROM draw_records earlier
                        WHERE earlier.user_id=d.user_id AND earlier.pig_id=d.pig_id
                        AND earlier.id<d.id) AS repeated
                    FROM draw_records d WHERE d.user_id=? AND d.day<=?
                    ORDER BY d.day DESC LIMIT ?
                    """,
                    (user_id, day, duplicate_pity),
                ).fetchall()
                return next(
                    (i for i, row in enumerate(records) if not row["repeated"]), len(records)
                )

            def finish(session, created):
                items = json.loads(session["items"])
                first = items[0]["pig"]
                sessions_today = conn.execute(
                    "SELECT count(*) FROM draw_sessions WHERE user_id=? AND day=? AND id<=?",
                    (user_id, day, session["id"]),
                ).fetchone()[0]
                return {
                    "pig": first,
                    "items": items,
                    "day": day,
                    "created": created,
                    "kind": session["kind"],
                    "index": max(1, sessions_today),
                    "count": _owned(conn, user_id, first["id"]),
                    "new_species": created and items[0]["new"],
                    "repeat_streak": repeat_streak() if duplicate_pity else 0,
                    "bonus_left": self._bonus_left(conn, app_id, group_id, user_id, day),
                }

            replay = conn.execute(
                "SELECT * FROM draw_sessions WHERE user_id=? AND event_id=?", (user_id, event_id)
            ).fetchone()
            if replay:
                return finish(replay, False)
            existing = conn.execute(
                "SELECT * FROM draw_records WHERE user_id=? AND day=?", (user_id, day)
            ).fetchone()
            if existing:
                if self._bonus_left(conn, app_id, group_id, user_id, day) < 1:
                    latest = conn.execute(
                        "SELECT * FROM draw_sessions WHERE user_id=? AND day=? "
                        "ORDER BY id DESC LIMIT 1",
                        (user_id, day),
                    ).fetchone()
                    if latest:
                        return finish(latest, False)
                    # Drawn before sessions existed: show the single recorded pig.
                    pig = json.loads(existing["snapshot"])
                    legacy = {
                        "id": 0,
                        "kind": "daily",
                        "items": json.dumps(
                            [{"pig": pig, "kind": "base", "parent": None, "new": False}]
                        ),
                    }
                    return finish(legacy, False)
                conn.execute(
                    "UPDATE draw_bonus SET count=count-1 "
                    "WHERE app_id=? AND group_id=? AND user_id=? AND day=?",
                    (app_id, group_id, user_id, day),
                )
            available = [
                dict(row) for row in conn.execute("SELECT * FROM pigs WHERE enabled=1 ORDER BY id")
            ]
            if not available:
                raise PiggyError("猪库没有启用的小猪，请联系管理员检查。")
            for pig in available:
                pig.pop("battle", None)
            owned_ids = {
                row[0]
                for row in conn.execute(
                    "SELECT pig_id FROM collections WHERE user_id=?", (user_id,)
                )
            }
            timestamp = now.timestamp()

            def pick(force_new: bool) -> dict:
                owned = [p for p in available if p["id"] in owned_ids]
                unseen = [p for p in available if p["id"] not in owned_ids]
                if not unseen:
                    pool = owned
                elif not owned or force_new:
                    pool = unseen
                else:
                    # Integer comparison preserves the natural rate without rounding.
                    threshold = min(len(owned) * 100, duplicate_rate_cap * len(available))
                    pool = owned if below(100 * len(available)) < threshold else unseen
                return dict(choose(pool))

            def collect(pig: dict, kind: str, parent) -> dict:
                new = pig["id"] not in owned_ids
                _give(conn, user_id, pig["id"], timestamp)
                owned_ids.add(pig["id"])
                return {"pig": pig, "kind": kind, "parent": parent, "new": new}

            daily = existing is None
            pity = daily and bool(duplicate_pity) and repeat_streak() >= duplicate_pity
            first = pick(pity)
            if daily:
                conn.execute(
                    """
                    INSERT INTO draw_records(user_id,day,drawn_at,pig_id,source_group,source_event,snapshot)
                    VALUES(?,?,?,?,?,?,?)
                """,
                    (
                        user_id,
                        day,
                        timestamp,
                        first["id"],
                        group_id,
                        event_id,
                        json.dumps(first, ensure_ascii=False),
                    ),
                )
            items, current, kind, parent = [], first, "base", None
            while True:
                index = len(items)
                items.append(collect(current, kind, parent))
                if roll() * 100 < gather_chance:
                    items.append(collect(current, "gather", index))
                if len(items) >= MAX_DRAW_ITEMS or roll() * 100 >= chain_chance:
                    break
                current, kind, parent = pick(False), "chain", index
            cursor = conn.execute(
                "INSERT INTO draw_sessions(user_id,app_id,group_id,day,kind,event_id,created_at,"
                "items) VALUES(?,?,?,?,?,?,?,?)",
                (
                    user_id,
                    app_id,
                    group_id,
                    day,
                    "daily" if daily else "bonus",
                    event_id,
                    timestamp,
                    json.dumps(items, ensure_ascii=False),
                ),
            )
            session = conn.execute(
                "SELECT * FROM draw_sessions WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
            return finish(session, True)

        return await self.run(draw)

    @staticmethod
    def _bonus_left(conn, app_id: str, group_id: str, user_id: int, day: str) -> int:
        row = conn.execute(
            "SELECT count FROM draw_bonus WHERE app_id=? AND group_id=? AND user_id=? AND day=?",
            (app_id, group_id, user_id, day),
        ).fetchone()
        return row[0] if row else 0

    async def bonus_count(
        self, app_id: str, group_id: str, user_id: int, now: datetime | None = None
    ) -> int:
        day = (now or datetime.now(timezone.utc)).astimezone(EAST_ASIA).date().isoformat()
        return await self.run(lambda c: self._bonus_left(c, app_id, group_id, user_id, day))

    @staticmethod
    def _wild_today(conn, app_id: str, group_id: str, day: str, level_max: int, stamp: float):
        """One wild pig per group per day, spawned on first sight."""
        row = conn.execute(
            "SELECT * FROM wild_pigs WHERE app_id=? AND group_id=? AND day=?",
            (app_id, group_id, day),
        ).fetchone()
        if row:
            return row
        conn.execute(
            "UPDATE wild_pigs SET status='expired' "
            "WHERE app_id=? AND group_id=? AND day<? AND status='active'",
            (app_id, group_id, day),
        )
        pool = [r[0] for r in conn.execute("SELECT id FROM pigs WHERE enabled=1")]
        if not pool:
            raise PiggyError("猪库没有启用的小猪，野猪暂时无法出现。")
        conn.execute(
            "INSERT OR IGNORE INTO wild_pigs(app_id,group_id,day,pig_id,level,spawned_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                app_id,
                group_id,
                day,
                _SYSTEM_RANDOM.choice(pool),
                _SYSTEM_RANDOM.randint(1, level_max),
                stamp,
            ),
        )
        return conn.execute(
            "SELECT * FROM wild_pigs WHERE app_id=? AND group_id=? AND day=?",
            (app_id, group_id, day),
        ).fetchone()

    @staticmethod
    def _wild_view(conn, row) -> dict:
        wild = dict(row)
        wild["pig"] = dict(
            conn.execute("SELECT * FROM pigs WHERE id=?", (row["pig_id"],)).fetchone()
        )
        wild["victor"] = (
            dict(conn.execute("SELECT * FROM users WHERE id=?", (row["defeated_by"],)).fetchone())
            if row["defeated_by"]
            else None
        )
        return wild

    async def wild_pig(
        self, app_id: str, group_id: str, now: datetime | None = None, level_max: int = 20
    ) -> dict:
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(EAST_ASIA).date().isoformat()

        def read(conn):
            conn.execute("BEGIN IMMEDIATE")
            return self._wild_view(
                conn, self._wild_today(conn, app_id, group_id, day, level_max, now.timestamp())
            )

        return await self.run(read)

    async def challenge_wild(
        self,
        app_id: str,
        group_id: str,
        user_id: int,
        pig_id: str,
        *,
        level_cap: int = 20,
        level_max: int = 20,
        now: datetime | None = None,
        seed: int | None = None,
    ) -> dict:
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(EAST_ASIA).date().isoformat()
        stamp = now.timestamp()

        def challenge(conn):
            conn.execute("BEGIN IMMEDIATE")
            wild = self._wild_view(
                conn, self._wild_today(conn, app_id, group_id, day, level_max, stamp)
            )
            if wild["status"] == "defeated":
                victor = wild["victor"]
                raise PiggyError(
                    f"今天的野生「{wild['pig']['name']}」已经被 {player_name(victor)} 收服了，"
                    "明天 0 点会出现新的野猪。"
                )
            count = _owned(conn, user_id, pig_id)
            if count < 1:
                raise PiggyError("你的猪圈里没有这只小猪，换一只出战吧。")
            user = dict(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
            mine = dict(conn.execute("SELECT * FROM pigs WHERE id=?", (pig_id,)).fetchone())
            level = level_for(count, level_cap)
            fighters = [
                fighter(
                    mine, entry_for(mine["battle"]), level, f"{player_name(user)}的{mine['name']}"
                ),
                fighter(
                    wild["pig"],
                    entry_for(wild["pig"]["battle"]),
                    wild["level"],
                    f"野生的{wild['pig']['name']}",
                ),
            ]
            fight_seed = secrets.randbits(32) if seed is None else seed
            result = simulate(fighters[0], fighters[1], fight_seed)
            won = result["winner"] == 0
            conn.execute("UPDATE wild_pigs SET attempts=attempts+1 WHERE id=?", (wild["id"],))
            rewarded = 0
            if won:
                cursor = conn.execute(
                    "UPDATE wild_pigs SET status='defeated',defeated_by=?,defeated_at=? "
                    "WHERE id=? AND status='active'",
                    (user_id, stamp, wild["id"]),
                )
                if cursor.rowcount != 1:
                    raise PiggyError("野猪刚刚被别人收服了。")
                prize = wild["pig"]
                before = _owned(conn, user_id, prize["id"])
                _give(conn, user_id, prize["id"], stamp)
                change = _level_change(prize, before, before + 1, level_cap)
                players = [
                    row[0]
                    for row in conn.execute(
                        "SELECT user_id FROM group_players WHERE app_id=? AND group_id=?",
                        (app_id, group_id),
                    )
                ]
                conn.executemany(
                    """
                    INSERT INTO draw_bonus VALUES(?,?,?,?,1) ON CONFLICT(app_id,group_id,user_id,day)
                    DO UPDATE SET count=count+1
                    """,
                    [(app_id, group_id, player, day) for player in players],
                )
                rewarded = len(players)
            else:
                _take(conn, user_id, pig_id)
                change = _level_change(mine, count, count - 1, level_cap)
            cursor = conn.execute(
                "INSERT INTO wild_battles(wild_id,user_id,pig_id,level,won,seed,log,summary,"
                "fought_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    wild["id"],
                    user_id,
                    pig_id,
                    level,
                    int(won),
                    fight_seed,
                    json.dumps(result["log"], ensure_ascii=False),
                    json.dumps(
                        {"hp": result["hp"], "max_hp": result["max_hp"], "rounds": result["rounds"]}
                    ),
                    stamp,
                ),
            )
            wild = self._wild_view(
                conn, conn.execute("SELECT * FROM wild_pigs WHERE id=?", (wild["id"],)).fetchone()
            )
            return {
                "id": cursor.lastrowid,
                "day": day,
                "user": user,
                "wild": wild,
                "fighters": fighters,
                "result": result,
                "won": won,
                "change": change,
                "rewarded": rewarded,
            }

        return await self.run(challenge)

    @staticmethod
    def _raid_expire(conn, app_id: str, group_id: str, stamp: float) -> list[dict]:
        """Lobbies that never filled are cancelled; undecided parties retreat."""
        rows = conn.execute(
            "SELECT * FROM raids WHERE status IN ('forming','waiting') AND expires_at<=?",
            (stamp,),
        ).fetchall()
        expired = []
        for row in rows:
            status = "cancelled" if row["status"] == "forming" else "retreated"
            conn.execute(
                "UPDATE raids SET status=?,updated_at=? WHERE id=?", (status, stamp, row["id"])
            )
            if row["app_id"] == app_id and row["group_id"] == group_id:
                expired.append({**dict(row), "status": status, "dungeon": dungeon(row["dungeon"])})
        return expired

    async def _raid_sweep(self, app_id: str, group_id: str, stamp: float) -> list[dict]:
        """Commit timeouts on their own, so a rejected command cannot roll them back."""

        def sweep(conn):
            conn.execute("BEGIN IMMEDIATE")
            return self._raid_expire(conn, app_id, group_id, stamp)

        return await self.run(sweep)

    @staticmethod
    def _raid_view(conn, row) -> dict:
        raid = dict(row)
        raid["dungeon"] = dungeon(row["dungeon"])
        raid["leader_user"] = dict(
            conn.execute("SELECT * FROM users WHERE id=?", (row["leader"],)).fetchone()
        )
        members = []
        for member in conn.execute(
            "SELECT * FROM raid_members WHERE raid_id=? ORDER BY seat", (row["id"],)
        ):
            pig = dict(
                conn.execute("SELECT * FROM pigs WHERE id=?", (member["pig_id"],)).fetchone()
            )
            pig.pop("battle", None)
            members.append(
                {
                    **dict(member),
                    "mods": json.loads(member["mods"]),
                    "user": dict(
                        conn.execute(
                            "SELECT * FROM users WHERE id=?", (member["user_id"],)
                        ).fetchone()
                    ),
                    "pig": pig,
                }
            )
        raid["members"] = members
        return raid

    @staticmethod
    def _boss_names(conn) -> dict:
        """Boss slot id -> display name; a missing boss pig is shown as possessed."""
        names = {
            row["id"]: row["name"]
            for row in conn.execute("SELECT id,name FROM pigs WHERE enabled=1")
        }
        return {
            slot: names.get(slot, "被附身的神秘小猪")
            for item in DUNGEONS
            for slot in item["bosses"]
        }

    @staticmethod
    def _group_raid(conn, app_id: str, group_id: str):
        return conn.execute(
            "SELECT * FROM raids WHERE app_id=? AND group_id=? AND status IN ('forming','waiting') "
            "ORDER BY id DESC LIMIT 1",
            (app_id, group_id),
        ).fetchone()

    @staticmethod
    def _raid_check_member(conn, user_id: int, dungeon_key: int, day: str, pig_id: str):
        busy = conn.execute(
            "SELECT r.* FROM raid_members m JOIN raids r ON r.id=m.raid_id "
            "WHERE m.user_id=? AND r.status IN ('forming','waiting')",
            (user_id,),
        ).fetchone()
        if busy:
            raise PiggyError(
                f"你已经在「{dungeon(busy['dungeon'])['name']}」的队伍里了，"
                "同一时间只能参加一支队伍。"
            )
        if conn.execute(
            "SELECT 1 FROM raid_entries WHERE user_id=? AND day=? AND dungeon=?",
            (user_id, day, dungeon_key),
        ).fetchone():
            raise PiggyError(
                f"你今天已经打过「{dungeon(dungeon_key)['name']}」了，每个副本每人每天 1 次，"
                "可以换一个副本。"
            )
        if _owned(conn, user_id, pig_id) < 1:
            raise PiggyError("你的猪圈里没有这只小猪，换一只出战吧。")

    async def raid_status(
        self, app_id: str, group_id: str, user_id: int, now: datetime | None = None
    ) -> dict:
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(EAST_ASIA).date().isoformat()

        def read(conn):
            conn.execute("BEGIN IMMEDIATE")
            expired = self._raid_expire(conn, app_id, group_id, now.timestamp())
            row = self._group_raid(conn, app_id, group_id)
            done = {
                r[0]
                for r in conn.execute(
                    "SELECT dungeon FROM raid_entries WHERE user_id=? AND day=?", (user_id, day)
                )
            }
            return {
                "raid": self._raid_view(conn, row) if row else None,
                "expired": expired,
                "done": done,
                "day": day,
                "now": now.timestamp(),
                "bosses": self._boss_names(conn),
            }

        return await self.run(read)

    async def open_raid(
        self,
        app_id: str,
        group_id: str,
        user_id: int,
        dungeon_key: int,
        pig_id: str,
        now: datetime | None = None,
    ) -> dict:
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(EAST_ASIA).date().isoformat()
        stamp = now.timestamp()

        expired = await self._raid_sweep(app_id, group_id, stamp)

        def create(conn):
            conn.execute("BEGIN IMMEDIATE")
            self._raid_expire(conn, app_id, group_id, stamp)
            active = self._group_raid(conn, app_id, group_id)
            if active:
                name = dungeon(active["dungeon"])["name"]
                if active["status"] == "forming":
                    raise PiggyError(
                        f"本群已经有一支「{name}」队伍在组队，发送「加入副本 你的小猪」加入吧。"
                    )
                raise PiggyError(f"本群的「{name}」队伍还在副本里，等他们结束后再开新的副本。")
            self._raid_check_member(conn, user_id, dungeon_key, day, pig_id)
            cursor = conn.execute(
                "INSERT INTO raids(app_id,group_id,day,dungeon,leader,seed,created_at,expires_at,"
                "updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    app_id,
                    group_id,
                    day,
                    dungeon_key,
                    user_id,
                    secrets.randbits(32),
                    stamp,
                    stamp + TIMEOUT_MINUTES * 60,
                    stamp,
                ),
            )
            conn.execute(
                "INSERT INTO raid_members(raid_id,user_id,pig_id,seat,joined_at) VALUES(?,?,?,0,?)",
                (cursor.lastrowid, user_id, pig_id, stamp),
            )
            row = conn.execute("SELECT * FROM raids WHERE id=?", (cursor.lastrowid,)).fetchone()
            return {"raid": self._raid_view(conn, row), "expired": expired, "now": stamp}

        return await self.run(create)

    async def join_raid(
        self,
        app_id: str,
        group_id: str,
        user_id: int,
        pig_id: str,
        *,
        level_cap: int = 20,
        now: datetime | None = None,
        seed: int | None = None,
    ) -> dict:
        """Join the group's lobby; the fourth member starts the first boss right away."""
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(EAST_ASIA).date().isoformat()
        stamp = now.timestamp()

        expired = await self._raid_sweep(app_id, group_id, stamp)

        def join(conn):
            conn.execute("BEGIN IMMEDIATE")
            self._raid_expire(conn, app_id, group_id, stamp)
            raid = self._group_raid(conn, app_id, group_id)
            if not raid:
                note = "上一支队伍 10 分钟内没凑满 4 人，已自动取消。" if expired else ""
                raise PiggyError(
                    f"{note}本群现在没有正在组队的副本，发送「开启副本 编号 你的小猪」发起一个吧。"
                )
            if raid["status"] != "forming":
                raise PiggyError("队伍已经出发了，副本开打后不能中途加入。")
            if conn.execute(
                "SELECT 1 FROM raid_members WHERE raid_id=? AND user_id=?", (raid["id"], user_id)
            ).fetchone():
                raise PiggyError("你已经在这支队伍里了，等其他人加入吧。")
            self._raid_check_member(conn, user_id, raid["dungeon"], day, pig_id)
            count = conn.execute(
                "SELECT count(*) FROM raid_members WHERE raid_id=?", (raid["id"],)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO raid_members(raid_id,user_id,pig_id,seat,joined_at) VALUES(?,?,?,?,?)",
                (raid["id"], user_id, pig_id, count, stamp),
            )
            if count + 1 < PARTY_SIZE:
                row = conn.execute("SELECT * FROM raids WHERE id=?", (raid["id"],)).fetchone()
                return {
                    "raid": self._raid_view(conn, row),
                    "started": False,
                    "expired": expired,
                    "now": stamp,
                }
            for (member,) in conn.execute(
                "SELECT user_id FROM raid_members WHERE raid_id=?", (raid["id"],)
            ).fetchall():
                conn.execute(
                    "INSERT INTO raid_entries VALUES(?,?,?,?)",
                    (member, day, raid["dungeon"], raid["id"]),
                )
            battle = self._raid_fight(conn, raid["id"], day, stamp, level_cap, seed, False)
            return {"raid": battle["raid"], "started": True, "battle": battle, "expired": expired}

        return await self.run(join)

    async def leave_raid(
        self, app_id: str, group_id: str, user_id: int, now: datetime | None = None
    ) -> dict:
        now = now or datetime.now(timezone.utc)
        stamp = now.timestamp()

        await self._raid_sweep(app_id, group_id, stamp)

        def leave(conn):
            conn.execute("BEGIN IMMEDIATE")
            self._raid_expire(conn, app_id, group_id, stamp)
            raid = self._group_raid(conn, app_id, group_id)
            if (
                not raid
                or not conn.execute(
                    "SELECT 1 FROM raid_members WHERE raid_id=? AND user_id=?",
                    (raid["id"], user_id),
                ).fetchone()
            ):
                raise PiggyError("你不在本群正在组队的队伍里。")
            if raid["status"] != "forming":
                raise PiggyError("队伍已经出发，不能退出；队长可以发送「撤退副本」结束副本。")
            if raid["leader"] == user_id:
                conn.execute(
                    "UPDATE raids SET status='cancelled',updated_at=? WHERE id=?",
                    (stamp, raid["id"]),
                )
                cancelled = True
            else:
                conn.execute(
                    "DELETE FROM raid_members WHERE raid_id=? AND user_id=?", (raid["id"], user_id)
                )
                for seat, (member,) in enumerate(
                    conn.execute(
                        "SELECT user_id FROM raid_members WHERE raid_id=? ORDER BY joined_at,seat",
                        (raid["id"],),
                    ).fetchall()
                ):
                    conn.execute(
                        "UPDATE raid_members SET seat=? WHERE raid_id=? AND user_id=?",
                        (seat, raid["id"], member),
                    )
                cancelled = False
            row = conn.execute("SELECT * FROM raids WHERE id=?", (raid["id"],)).fetchone()
            return {"raid": self._raid_view(conn, row), "cancelled": cancelled, "now": stamp}

        return await self.run(leave)

    def _raid_leader_waiting(self, conn, app_id, group_id, user_id, stamp, expired):
        self._raid_expire(conn, app_id, group_id, stamp)
        raid = self._group_raid(conn, app_id, group_id)
        if not raid:
            if any(e["status"] == "retreated" for e in expired):
                raise PiggyError("队长 10 分钟内没有决定，队伍已经自动撤退，已得到的奖励都保留着。")
            raise PiggyError("本群现在没有进行中的副本。")
        if raid["status"] != "waiting":
            raise PiggyError("队伍还在组队，满 4 人会自动出发。")
        if raid["leader"] != user_id:
            leader = conn.execute("SELECT * FROM users WHERE id=?", (raid["leader"],)).fetchone()
            raise PiggyError(f"只有队长 {player_name(leader)} 能决定继续还是撤退。")
        return raid

    async def continue_raid(
        self,
        app_id: str,
        group_id: str,
        user_id: int,
        *,
        level_cap: int = 20,
        now: datetime | None = None,
        seed: int | None = None,
    ) -> dict:
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(EAST_ASIA).date().isoformat()
        stamp = now.timestamp()

        expired = await self._raid_sweep(app_id, group_id, stamp)

        def advance(conn):
            conn.execute("BEGIN IMMEDIATE")
            raid = self._raid_leader_waiting(conn, app_id, group_id, user_id, stamp, expired)
            return self._raid_fight(conn, raid["id"], day, stamp, level_cap, seed, True)

        return await self.run(advance)

    async def retreat_raid(
        self, app_id: str, group_id: str, user_id: int, now: datetime | None = None
    ) -> dict:
        now = now or datetime.now(timezone.utc)
        stamp = now.timestamp()

        expired = await self._raid_sweep(app_id, group_id, stamp)

        def retreat(conn):
            conn.execute("BEGIN IMMEDIATE")
            raid = self._raid_leader_waiting(conn, app_id, group_id, user_id, stamp, expired)
            conn.execute(
                "UPDATE raids SET status='retreated',updated_at=? WHERE id=?", (stamp, raid["id"])
            )
            row = conn.execute("SELECT * FROM raids WHERE id=?", (raid["id"],)).fetchone()
            return {"raid": self._raid_view(conn, row)}

        return await self.run(retreat)

    def _raid_fight(
        self, conn, raid_id: int, day: str, stamp: float, level_cap: int, seed, interlude: bool
    ) -> dict:
        """Fight the next boss inside the caller's transaction and settle every member."""
        raid = conn.execute("SELECT * FROM raids WHERE id=?", (raid_id,)).fetchone()
        info = dungeon(raid["dungeon"])
        stage = raid["stage"] + 1
        fight_seed = (raid["seed"] + stage * 7919) & 0xFFFFFFFF if seed is None else seed
        rng = random.Random(fight_seed ^ 0x5EED)
        enabled = [dict(r) for r in conn.execute("SELECT * FROM pigs WHERE enabled=1 ORDER BY id")]
        if not enabled:
            raise PiggyError("猪库没有启用的小猪，副本暂时无法进行。")
        members = self._raid_view(conn, raid)["members"]
        party, absent = [], []
        for member in members:
            if not member["alive"]:
                continue
            count = _owned(conn, member["user_id"], member["pig_id"])
            if count < 1:
                absent.append(member)
                continue
            pig = dict(
                conn.execute("SELECT * FROM pigs WHERE id=?", (member["pig_id"],)).fetchone()
            )
            party.append(
                {
                    "seat": member["seat"],
                    "label": f"{player_name(member['user'])}的{pig['name']}",
                    "hp": member["hp"],
                    "mods": dict(member["mods"]),
                    "member": member,
                    "pig": pig,
                    "count": count,
                }
            )
        slot_id = info["bosses"][stage - 1]
        mechanics = mechanics_for(slot_id).MECHANICS
        cfg, rewards, events = new_config(), new_rewards(), []
        if interlude:
            for member in party:
                member["hp"] = min(1.0, member["hp"] + REST_HEAL)
            for event in resolve([roll_interlude(rng)], party, rng, cfg, rewards, mechanics):
                events.append({**event, "kind": "关间"})
        for event in resolve(roll_entry(rng, info["key"]), party, rng, cfg, rewards, mechanics):
            events.append({**event, "kind": "进场"})
        boss_row = conn.execute(
            "SELECT * FROM pigs WHERE id=? AND enabled=1", (slot_id,)
        ).fetchone()
        possessed = boss_row is None
        boss_pig = dict(boss_row) if boss_row else dict(rng.choice(enabled))
        levels = [level_for(member["count"], level_cap) for member in party]
        level = boss_level(levels)
        boss = boss_fighter(
            boss_pig,
            entry_for(boss_pig["battle"]),
            level,
            stage,
            slot_id,
            ("被附身的" if possessed else "") + boss_pig["name"],
        )
        heroes = []
        for member, hero_level in zip(party, levels):
            hero = fighter(
                member["pig"], entry_for(member["pig"]["battle"]), hero_level, member["label"]
            )
            hero.update(
                seat=member["seat"],
                start_hp=max(1, round(hero["stats"]["hp"] * member["hp"])),
                mods=member["mods"],
            )
            heroes.append(hero)
        ally = None
        if cfg.get("ally") and party:
            ally_pig = rng.choice(enabled)
            ally = fighter(
                ally_pig,
                entry_for(ally_pig["battle"]),
                max(1, round(sum(levels) / len(levels))),
                f"援军·{ally_pig['name']}",
            )
            heroes.append(ally)
        result = None
        if party:
            result = simulate_raid(heroes, boss, slot_id, cfg, fight_seed)
        won = bool(result and result["won"])
        outcome = {h["seat"]: h for h in (result["heroes"] if result else [])}
        changes = {member["user_id"]: [] for member in members}
        fighters = []
        for member, hero, hero_level in zip(party, heroes, levels):
            state = outcome[member["seat"]]
            user_id, pig = member["member"]["user_id"], member["pig"]
            if state["alive"]:
                conn.execute(
                    "UPDATE raid_members SET hp=?,mods=? WHERE raid_id=? AND user_id=?",
                    (
                        state["hp"] / state["max_hp"],
                        json.dumps(member["mods"]),
                        raid_id,
                        user_id,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE raid_members SET hp=0,alive=0 WHERE raid_id=? AND user_id=?",
                    (raid_id, user_id),
                )
                before = _owned(conn, user_id, pig["id"])
                _take(conn, user_id, pig["id"])
                changes[user_id].append(_level_change(pig, before, before - 1, level_cap))
            fighters.append(
                {
                    "seat": member["seat"],
                    "user": member["member"]["user"],
                    "pig": {k: v for k, v in pig.items() if k != "battle"},
                    "level": hero_level,
                    "style": hero["style"],
                    "hp": state["hp"],
                    "max_hp": state["max_hp"],
                    "alive": state["alive"],
                }
            )
        copies = 2 if rewards["mimic"] else 1
        prize = {k: v for k, v in boss_pig.items()}
        if won:
            for member in members:
                before = _owned(conn, member["user_id"], prize["id"])
                for _ in range(copies):
                    _give(conn, member["user_id"], prize["id"], stamp)
                changes[member["user_id"]].append(
                    _level_change(prize, before, before + copies, level_cap)
                )
        for _ in range(rewards["chest"]):
            for member in members:
                found = rng.choice(enabled)
                before = _owned(conn, member["user_id"], found["id"])
                _give(conn, member["user_id"], found["id"], stamp)
                changes[member["user_id"]].append(
                    _level_change(found, before, before + 1, level_cap)
                )
        bonus = int(won) + rewards["clover"]
        rewarded = 0
        if bonus:
            players = [
                row[0]
                for row in conn.execute(
                    "SELECT user_id FROM group_players WHERE app_id=? AND group_id=?",
                    (raid["app_id"], raid["group_id"]),
                )
            ]
            conn.executemany(
                """
                INSERT INTO draw_bonus VALUES(?,?,?,?,?) ON CONFLICT(app_id,group_id,user_id,day)
                DO UPDATE SET count=count+excluded.count
                """,
                [(raid["app_id"], raid["group_id"], player, day, bonus) for player in players],
            )
            rewarded = len(players)
        survivors = any(f["alive"] for f in fighters)
        if won and stage < len(info["bosses"]) and survivors:
            status = "waiting"
        elif won and stage == len(info["bosses"]):
            status = "cleared"
        else:
            status = "failed"
        conn.execute(
            "UPDATE raids SET status=?,stage=?,expires_at=?,updated_at=? WHERE id=?",
            (status, stage, stamp + TIMEOUT_MINUTES * 60, stamp, raid_id),
        )
        summary = {
            "rounds": result["rounds"] if result else 0,
            "boss": result["boss"] if result else None,
            "heroes": result["heroes"] if result else [],
            "timeout": bool(result and result["timeout"]),
        }
        cursor = conn.execute(
            "INSERT INTO raid_battles(raid_id,stage,boss_pig,boss_level,won,seed,events,log,"
            "summary,fought_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                raid_id,
                stage,
                boss_pig["id"],
                level,
                int(won),
                fight_seed,
                json.dumps(events, ensure_ascii=False),
                json.dumps(result["log"] if result else [], ensure_ascii=False),
                json.dumps(summary),
                stamp,
            ),
        )
        prize.pop("battle", None)
        view = self._raid_view(
            conn, conn.execute("SELECT * FROM raids WHERE id=?", (raid_id,)).fetchone()
        )
        return {
            "id": cursor.lastrowid,
            "raid": view,
            "day": day,
            "stage": stage,
            "status": status,
            "won": won,
            "boss": {
                "pig": prize,
                "label": boss["label"],
                "level": level,
                "style": boss["style"],
                "hp": result["boss"]["hp"] if result else boss["stats"]["hp"],
                "max_hp": result["boss"]["max_hp"] if result else boss["stats"]["hp"],
                "mechanics": mechanics,
                "disabled": cfg.get("disabled"),
                "possessed": possessed,
            },
            "fighters": fighters,
            "ally": (
                {
                    "pig": {"name": ally["name"], "asset": ally["asset"]},
                    "label": ally["label"],
                    "level": ally["level"],
                    "style": ally["style"],
                    "hp": result["heroes"][-1]["hp"],
                    "max_hp": result["heroes"][-1]["max_hp"],
                    "alive": result["heroes"][-1]["alive"],
                }
                if ally and result
                else None
            ),
            "absent": absent,
            "retired": [m for m in members if not m["alive"]],
            "events": events,
            "result": result,
            "changes": [(member["user"], changes[member["user_id"]]) for member in members],
            "copies": copies if won else 0,
            "chest": rewards["chest"],
            "bonus": bonus,
            "rewarded": rewarded,
            "next_boss": (
                self._boss_names(conn)[info["bosses"][stage]] if status == "waiting" else ""
            ),
        }

    async def collection(self, user_id: int) -> dict:
        def read(conn):
            conn.execute("BEGIN")
            rows = conn.execute(
                """
                SELECT p.*,coalesce(c.count,0) AS count,c.first_at,c.last_at FROM pigs p
                LEFT JOIN collections c ON c.pig_id=p.id AND c.user_id=?
                WHERE p.enabled=1 OR c.count>0 ORDER BY p.enabled DESC,p.sort_order,p.id
            """,
                (user_id,),
            ).fetchall()
            entries = [dict(r) for r in rows]
            return {
                "entries": entries,
                "active_total": sum(p["enabled"] for p in entries),
                "unlocked": sum(bool(p["enabled"] and p["count"]) for p in entries),
                "total": sum(p["count"] for p in entries),
                "species": sum(p["count"] > 0 for p in entries),
            }

        return await self.run(read)

    async def rankings(self, app_id: str, group_id: str) -> dict:
        if not group_id:
            raise PiggyError("请在群里查看本群玩家排行。")

        def read(conn):
            rows = conn.execute(
                """
                WITH scores AS (
                    SELECT u.id,u.open_id,u.nickname,u.alias,count(c.pig_id) species,coalesce(sum(c.count),0) total
                    FROM group_players g JOIN users u ON u.id=g.user_id
                    LEFT JOIN collections c ON c.user_id=u.id
                    WHERE g.app_id=? AND g.group_id=? GROUP BY u.id
                ), ranked AS (
                    SELECT *,RANK() OVER (ORDER BY species DESC) AS species_rank,
                    RANK() OVER (ORDER BY total DESC) AS total_rank FROM scores
                ) SELECT * FROM ranked
            """,
                (app_id, group_id),
            ).fetchall()
            return {
                kind: [
                    {**dict(row), "rank": row[f"{kind}_rank"]}
                    for row in sorted(rows, key=lambda row: (-row[kind], row["id"]))[:10]
                ]
                for kind in ("species", "total")
            }

        return await self.run(read)

    async def find_pig(self, query: str) -> dict:
        query = " ".join(query.split())
        if not query:
            raise PiggyError("请写上小猪的名字。")

        def read(conn):
            row = conn.execute(
                "SELECT * FROM pigs WHERE id=? OR name=? ORDER BY enabled DESC,sort_order LIMIT 1",
                (query.lower(), query),
            ).fetchone()
            if not row:
                raise PiggyError(f"没有找到「{query}」，请输入图鉴里小猪的完整名字。")
            return dict(row)

        return await self.run(read)

    async def pig_count(self, user_id: int, pig_id: str) -> int:
        return await self.run(lambda c: _owned(c, user_id, pig_id))

    async def find_group_player(self, app_id: str, group_id: str, name: str) -> dict:
        name = " ".join(name.lstrip("@＠").split())
        if not name:
            raise PiggyError("请 @ 你要找的玩家。")

        number = name.lstrip("#＃")
        by_number = name[:1] in "#＃" and number.isdigit()

        def read(conn):
            if by_number:
                row = conn.execute(
                    """
                    SELECT u.* FROM group_players g JOIN users u ON u.id=g.user_id
                    WHERE g.app_id=? AND g.group_id=? AND u.id=?
                    """,
                    (app_id, group_id, int(number)),
                ).fetchone()
                if not row:
                    raise PiggyError(
                        f"本群没有编号为 #{number} 的玩家，请让对方发送「我的猪圈」核对。"
                    )
                return dict(row)
            rows = conn.execute(
                """
                SELECT u.* FROM group_players g JOIN users u ON u.id=g.user_id
                WHERE g.app_id=? AND g.group_id=? AND (u.alias=? OR u.nickname=?)
                """,
                (app_id, group_id, name, name),
            ).fetchall()
            if not rows:
                raise PiggyError(
                    f"本群没有找到叫「{name}」的玩家。群名片无法识别，请改用对方的玩家编号，"
                    "例如：斗猪 #12 你的小猪（对方发送「我的猪圈」即可看到编号）。"
                )
            if len(rows) > 1:
                raise PiggyError(f"本群有多位玩家叫「{name}」，请改用 @ 或对方的玩家编号。")
            return dict(rows[0])

        return await self.run(read)

    @staticmethod
    def _expire(conn, now: float):
        conn.execute(
            "UPDATE requests SET status='expired',resolved_at=? "
            "WHERE status='pending' AND expires_at<=?",
            (now, now),
        )

    @staticmethod
    def _duels_today(conn, user_id: int, day: str) -> int:
        return conn.execute(
            "SELECT count(*) FROM battle_records WHERE day=? AND (a_user=? OR b_user=?)",
            (day, user_id, user_id),
        ).fetchone()[0]

    @staticmethod
    def _request_view(conn, row) -> dict:
        def user(user_id):
            return dict(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

        def pig(pig_id):
            if not pig_id:
                return None
            found = dict(conn.execute("SELECT * FROM pigs WHERE id=?", (pig_id,)).fetchone())
            found.pop("battle", None)
            return found

        return {
            **dict(row),
            "from": user(row["from_user"]),
            "to": user(row["to_user"]),
            "give": pig(row["give_pig"]),
            "want": pig(row["want_pig"]),
        }

    async def create_request(
        self,
        app_id: str,
        group_id: str,
        kind: str,
        from_user: int,
        to_user: int,
        give_pig: str,
        want_pig: str | None = None,
        *,
        ttl_minutes: int = 10,
        daily_limit: int = 5,
        now: datetime | None = None,
    ) -> dict:
        now = now or datetime.now(timezone.utc)
        stamp = now.timestamp()
        day = now.astimezone(EAST_ASIA).date().isoformat()
        label = REQUEST_LABELS[kind]

        def create(conn):
            conn.execute("BEGIN IMMEDIATE")
            self._expire(conn, stamp)
            if from_user == to_user:
                raise PiggyError(f"不能和自己{label}哦。")
            give = conn.execute("SELECT name FROM pigs WHERE id=?", (give_pig,)).fetchone()
            if _owned(conn, from_user, give_pig) < 1:
                raise PiggyError(f"你的猪圈里没有「{give['name']}」。")
            if kind == "trade":
                want = conn.execute("SELECT name FROM pigs WHERE id=?", (want_pig,)).fetchone()
                if give_pig == want_pig:
                    raise PiggyError("同一种小猪就不用交换啦。")
                if _owned(conn, to_user, want_pig) < 1:
                    raise PiggyError(f"对方的猪圈里没有「{want['name']}」。")
            elif self._duels_today(conn, from_user, day) >= daily_limit:
                raise PiggyError(f"你今天已经斗了 {daily_limit} 场猪，明天再来吧。")
            pending = conn.execute(
                """
                SELECT from_user FROM requests WHERE app_id=? AND group_id=? AND kind=?
                AND status='pending' AND (from_user=? OR to_user=?)
                """,
                (app_id, group_id, kind, from_user, to_user),
            ).fetchall()
            if any(row["from_user"] == from_user for row in pending):
                raise PiggyError(f"你在本群已有一个待处理的{label}请求，可以先发送「取消请求」。")
            if pending:
                raise PiggyError(f"对方在本群还有一个待处理的{label}请求，请稍后再试。")
            cursor = conn.execute(
                """
                INSERT INTO requests(app_id,group_id,kind,from_user,to_user,give_pig,want_pig,
                created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    app_id,
                    group_id,
                    kind,
                    from_user,
                    to_user,
                    give_pig,
                    want_pig,
                    stamp,
                    stamp + ttl_minutes * 60,
                ),
            )
            row = conn.execute("SELECT * FROM requests WHERE id=?", (cursor.lastrowid,)).fetchone()
            view = self._request_view(conn, row)
            view["give_count"] = _owned(conn, from_user, give_pig)
            return view

        return await self.run(create)

    async def respond(
        self,
        app_id: str,
        group_id: str,
        user_id: int,
        kind: str,
        accept: bool,
        pig_id: str | None = None,
        *,
        level_cap: int = 20,
        daily_limit: int = 5,
        now: datetime | None = None,
        seed: int | None = None,
    ) -> dict:
        now = now or datetime.now(timezone.utc)
        stamp = now.timestamp()
        day = now.astimezone(EAST_ASIA).date().isoformat()
        label = REQUEST_LABELS[kind]

        def respond(conn):
            conn.execute("BEGIN IMMEDIATE")
            self._expire(conn, stamp)
            row = conn.execute(
                """
                SELECT * FROM requests WHERE app_id=? AND group_id=? AND to_user=? AND kind=?
                AND status='pending' ORDER BY id DESC LIMIT 1
                """,
                (app_id, group_id, user_id, kind),
            ).fetchone()
            if not row:
                raise PiggyError(f"你在本群没有待处理的{label}请求（可能已过期或被撤回）。")
            view = self._request_view(conn, row)

            def close(status: str):
                conn.execute(
                    "UPDATE requests SET status=?,resolved_at=? WHERE id=?",
                    (status, stamp, row["id"]),
                )

            if not accept:
                close("declined")
                return {"request": view, "accepted": False}
            challenger, target = row["from_user"], row["to_user"]
            give_pig = row["give_pig"]
            if _owned(conn, challenger, give_pig) < 1:
                close("failed")
                return {
                    "request": view,
                    "accepted": False,
                    "error": f"对方的「{view['give']['name']}」已经不在猪圈里了，请求作废。",
                }

            def pig(pig_id):
                return dict(conn.execute("SELECT * FROM pigs WHERE id=?", (pig_id,)).fetchone())

            if kind == "trade":
                want_pig = row["want_pig"]
                if _owned(conn, target, want_pig) < 1:
                    raise PiggyError(f"你的猪圈里已经没有「{view['want']['name']}」了。")
                give, want = pig(give_pig), pig(want_pig)
                counts = {
                    (challenger, give_pig): _owned(conn, challenger, give_pig),
                    (challenger, want_pig): _owned(conn, challenger, want_pig),
                    (target, give_pig): _owned(conn, target, give_pig),
                    (target, want_pig): _owned(conn, target, want_pig),
                }
                _transfer(conn, challenger, target, give_pig, stamp)
                _transfer(conn, target, challenger, want_pig, stamp)
                close("accepted")
                return {
                    "request": view,
                    "accepted": True,
                    "changes": {
                        "from_give": _level_change(
                            give,
                            counts[(challenger, give_pig)],
                            counts[(challenger, give_pig)] - 1,
                            level_cap,
                        ),
                        "from_want": _level_change(
                            want,
                            counts[(challenger, want_pig)],
                            counts[(challenger, want_pig)] + 1,
                            level_cap,
                        ),
                        "to_want": _level_change(
                            want,
                            counts[(target, want_pig)],
                            counts[(target, want_pig)] - 1,
                            level_cap,
                        ),
                        "to_give": _level_change(
                            give,
                            counts[(target, give_pig)],
                            counts[(target, give_pig)] + 1,
                            level_cap,
                        ),
                    },
                }

            if not pig_id:
                raise PiggyError("请写上你要出战的小猪，例如：接受斗猪 猪人")
            if _owned(conn, target, pig_id) < 1:
                raise PiggyError("你的猪圈里没有这只小猪，换一只出战吧。")
            if self._duels_today(conn, target, day) >= daily_limit:
                raise PiggyError(f"你今天已经斗了 {daily_limit} 场猪，可以发送「拒绝斗猪」。")
            if self._duels_today(conn, challenger, day) >= daily_limit:
                close("failed")
                return {
                    "request": view,
                    "accepted": False,
                    "error": "对方今天的斗猪次数已经用完，请求作废。",
                }
            users = {
                uid: dict(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
                for uid in (challenger, target)
            }
            pigs = {challenger: pig(give_pig), target: pig(pig_id)}
            counts = {uid: _owned(conn, uid, pigs[uid]["id"]) for uid in pigs}
            levels = {uid: level_for(counts[uid], level_cap) for uid in pigs}
            fighters = [
                fighter(
                    pigs[uid],
                    entry_for(pigs[uid]["battle"]),
                    levels[uid],
                    f"{player_name(users[uid])}的{pigs[uid]['name']}",
                )
                for uid in (challenger, target)
            ]
            fight_seed = secrets.randbits(32) if seed is None else seed
            result = simulate(fighters[0], fighters[1], fight_seed)
            winner = (challenger, target)[result["winner"]]
            loser = target if winner == challenger else challenger
            prize = pigs[loser]
            loser_before = counts[loser]
            winner_before = _owned(conn, winner, prize["id"])
            _transfer(conn, loser, winner, prize["id"], stamp)
            conn.execute(
                """
                INSERT INTO battle_records(request_id,day,fought_at,a_user,a_pig,a_level,b_user,
                b_pig,b_level,winner,seed,log,summary) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["id"],
                    day,
                    stamp,
                    challenger,
                    give_pig,
                    levels[challenger],
                    target,
                    pig_id,
                    levels[target],
                    winner,
                    fight_seed,
                    json.dumps(result["log"], ensure_ascii=False),
                    json.dumps(
                        {
                            "hp": result["hp"],
                            "max_hp": result["max_hp"],
                            "rounds": result["rounds"],
                            "styles": [unit["style"] for unit in fighters],
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            record_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            close("accepted")
            return {
                "request": view,
                "accepted": True,
                "users": users,
                "fighters": fighters,
                "result": result,
                "record_id": record_id,
                "day": day,
                "winner": users[winner],
                "loser": users[loser],
                "loser_change": _level_change(prize, loser_before, loser_before - 1, level_cap),
                "winner_change": _level_change(prize, winner_before, winner_before + 1, level_cap),
                "duels_left": {
                    uid: max(0, daily_limit - self._duels_today(conn, uid, day)) for uid in users
                },
            }

        return await self.run(respond)

    async def cancel_requests(
        self, app_id: str, group_id: str, user_id: int, now: datetime | None = None
    ) -> list[dict]:
        stamp = (now or datetime.now(timezone.utc)).timestamp()

        def cancel(conn):
            conn.execute("BEGIN IMMEDIATE")
            self._expire(conn, stamp)
            rows = conn.execute(
                "SELECT * FROM requests WHERE app_id=? AND group_id=? AND from_user=? "
                "AND status='pending'",
                (app_id, group_id, user_id),
            ).fetchall()
            if not rows:
                raise PiggyError("你在本群没有待处理的请求。")
            conn.executemany(
                "UPDATE requests SET status='cancelled',resolved_at=? WHERE id=?",
                [(stamp, row["id"]) for row in rows],
            )
            return [self._request_view(conn, row) for row in rows]

        return await self.run(cancel)

    async def list_requests(
        self, app_id: str, group_id: str, user_id: int, now: datetime | None = None
    ) -> dict:
        stamp = (now or datetime.now(timezone.utc)).timestamp()

        def read(conn):
            conn.execute("BEGIN IMMEDIATE")
            self._expire(conn, stamp)
            rows = conn.execute(
                "SELECT * FROM requests WHERE app_id=? AND group_id=? AND status='pending' "
                "AND (from_user=? OR to_user=?) ORDER BY id",
                (app_id, group_id, user_id, user_id),
            ).fetchall()
            views = [self._request_view(conn, row) for row in rows]
            return {
                "incoming": [v for v in views if v["to_user"] == user_id],
                "outgoing": [v for v in views if v["from_user"] == user_id],
            }

        return await self.run(read)

    @staticmethod
    def _shop_items(conn, app_id: str, group_id: str, day: str) -> list[dict]:
        """Stock today's shelf on first visit; later visits see the same five pigs."""
        rows = conn.execute(
            "SELECT * FROM shop_items WHERE app_id=? AND group_id=? AND day=? ORDER BY slot",
            (app_id, group_id, day),
        ).fetchall()
        if not rows:
            pool = [r[0] for r in conn.execute("SELECT id FROM pigs WHERE enabled=1")]
            if not pool:
                raise PiggyError("猪库没有启用的小猪，商店暂时无法上货。")
            picks = secrets.SystemRandom().sample(pool, min(SHOP_SIZE, len(pool)))
            conn.executemany(
                "INSERT OR IGNORE INTO shop_items(app_id,group_id,day,slot,pig_id) "
                "VALUES(?,?,?,?,?)",
                [(app_id, group_id, day, slot, pig) for slot, pig in enumerate(picks, 1)],
            )
            rows = conn.execute(
                "SELECT * FROM shop_items WHERE app_id=? AND group_id=? AND day=? ORDER BY slot",
                (app_id, group_id, day),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            pig = dict(conn.execute("SELECT * FROM pigs WHERE id=?", (row["pig_id"],)).fetchone())
            pig.pop("battle", None)
            item["pig"] = pig
            item["buyer"] = (
                dict(conn.execute("SELECT * FROM users WHERE id=?", (row["sold_to"],)).fetchone())
                if row["sold_to"]
                else None
            )
            items.append(item)
        return items

    async def shop(
        self, app_id: str, group_id: str, user_id: int, now: datetime | None = None
    ) -> dict:
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(EAST_ASIA).date().isoformat()

        def read(conn):
            conn.execute("BEGIN IMMEDIATE")
            items = self._shop_items(conn, app_id, group_id, day)
            for item in items:
                item["owned"] = _owned(conn, user_id, item["pig_id"])
            return {"day": day, "items": items}

        return await self.run(read)

    async def shop_exchange(
        self,
        app_id: str,
        group_id: str,
        user_id: int,
        slot: int,
        pay_pig: str,
        *,
        level_cap: int = 20,
        now: datetime | None = None,
    ) -> dict:
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(EAST_ASIA).date().isoformat()
        stamp = now.timestamp()

        def exchange(conn):
            conn.execute("BEGIN IMMEDIATE")
            items = self._shop_items(conn, app_id, group_id, day)
            item = next((i for i in items if i["slot"] == slot), None)
            if not item:
                raise PiggyError(f"商店今天只有 1–{len(items)} 号小猪。")
            if item["sold_to"]:
                raise PiggyError(f"{slot} 号「{item['pig']['name']}」已经被换走了，看看别的吧。")
            if pay_pig == item["pig_id"]:
                raise PiggyError("不能用同一种小猪来换哦。")
            paid = dict(conn.execute("SELECT * FROM pigs WHERE id=?", (pay_pig,)).fetchone())
            pay_before = _owned(conn, user_id, pay_pig)
            if pay_before < 1:
                raise PiggyError(f"你的猪圈里没有「{paid['name']}」。")
            got = dict(conn.execute("SELECT * FROM pigs WHERE id=?", (item["pig_id"],)).fetchone())
            got_before = _owned(conn, user_id, item["pig_id"])
            cursor = conn.execute(
                "UPDATE shop_items SET sold_to=?,paid_pig=?,sold_at=? "
                "WHERE app_id=? AND group_id=? AND day=? AND slot=? AND sold_to IS NULL",
                (user_id, pay_pig, stamp, app_id, group_id, day, slot),
            )
            if cursor.rowcount != 1:
                raise PiggyError(f"{slot} 号小猪刚刚被别人换走了。")
            _take(conn, user_id, pay_pig)
            _give(conn, user_id, item["pig_id"], stamp)
            return {
                "slot": slot,
                "paid": _level_change(paid, pay_before, pay_before - 1, level_cap),
                "got": _level_change(got, got_before, got_before + 1, level_cap),
                "left": sum(1 for i in items if not i["sold_to"]) - 1,
            }

        return await self.run(exchange)

    async def duel_rankings(
        self, app_id: str, group_id: str, user_id: int, min_games: int = 3, limit: int = 10
    ) -> dict:
        def read(conn):
            rows = [
                dict(row)
                for row in conn.execute(
                    """
                    WITH games AS (
                        SELECT a_user AS uid, winner FROM battle_records
                        UNION ALL SELECT b_user, winner FROM battle_records
                    )
                    SELECT u.*, count(*) AS games, sum(g.winner = u.id) AS wins
                    FROM group_players gp JOIN users u ON u.id = gp.user_id
                    JOIN games g ON g.uid = u.id
                    WHERE gp.app_id=? AND gp.group_id=? GROUP BY u.id
                    """,
                    (app_id, group_id),
                )
            ]
            for row in rows:
                row["losses"] = row["games"] - row["wins"]
                row["rate"] = row["wins"] / row["games"]
            ranked = sorted(
                (row for row in rows if row["games"] >= min_games),
                key=lambda row: (-row["rate"], -row["wins"], row["id"]),
            )
            for index, row in enumerate(ranked, 1):
                row["rank"] = index
            mine = next((row for row in rows if row["id"] == user_id), None)
            return {"top": ranked[:limit], "me": mine, "min_games": min_games}

        return await self.run(read)

    @staticmethod
    def _battle_view(conn, row, user_id: int) -> dict:
        record = dict(row)
        mine_a = record["a_user"] == user_id
        pigs = {
            pig["id"]: dict(pig)
            for pig in conn.execute(
                "SELECT id,name,asset FROM pigs WHERE id IN (?,?)",
                (record["a_pig"], record["b_pig"]),
            )
        }
        names = {pig_id: pig["name"] for pig_id, pig in pigs.items()}
        other = conn.execute(
            "SELECT * FROM users WHERE id=?", (record["b_user"] if mine_a else record["a_user"],)
        ).fetchone()
        won = record["winner"] == user_id
        loser_pig = record["b_pig"] if record["winner"] == record["a_user"] else record["a_pig"]
        return {
            **record,
            "won": won,
            "opponent": dict(other),
            "my_pig": names.get(record["a_pig" if mine_a else "b_pig"], "?"),
            "my_level": record["a_level" if mine_a else "b_level"],
            "their_pig": names.get(record["b_pig" if mine_a else "a_pig"], "?"),
            "their_level": record["b_level" if mine_a else "a_level"],
            "prize": names.get(loser_pig, "?"),
            "a_pig_name": names.get(record["a_pig"], "?"),
            "b_pig_name": names.get(record["b_pig"], "?"),
            "a_asset": pigs[record["a_pig"]]["asset"] if record["a_pig"] in pigs else "",
            "b_asset": pigs[record["b_pig"]]["asset"] if record["b_pig"] in pigs else "",
            "my_asset": (pigs.get(record["a_pig" if mine_a else "b_pig"]) or {}).get("asset", ""),
            "their_asset": (pigs.get(record["b_pig" if mine_a else "a_pig"]) or {}).get(
                "asset", ""
            ),
            "summary": json.loads(record["summary"]) if record.get("summary") else None,
        }

    async def duel_history(self, user_id: int, page: int = 1, size: int = 10) -> dict:
        def read(conn):
            total, wins = conn.execute(
                "SELECT count(*), coalesce(sum(winner=?),0) FROM battle_records "
                "WHERE a_user=? OR b_user=?",
                (user_id, user_id, user_id),
            ).fetchone()
            pages = max(1, -(-total // size))
            if not 1 <= page <= pages:
                raise PiggyError(f"页码超出范围，请输入 1–{pages}。")
            rows = conn.execute(
                "SELECT * FROM battle_records WHERE a_user=? OR b_user=? "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (user_id, user_id, size, (page - 1) * size),
            ).fetchall()
            return {
                "records": [self._battle_view(conn, row, user_id) for row in rows],
                "total": total,
                "wins": wins,
                "page": page,
                "pages": pages,
            }

        return await self.run(read)

    async def duel_record(self, user_id: int, record_id: int) -> dict:
        def read(conn):
            row = conn.execute(
                "SELECT * FROM battle_records WHERE id=? AND (a_user=? OR b_user=?)",
                (record_id, user_id, user_id),
            ).fetchone()
            if not row:
                raise PiggyError(f"没有找到你参与的第 {record_id} 场斗猪。")
            view = self._battle_view(conn, row, user_id)
            view["log"] = json.loads(row["log"])
            view["players"] = {
                uid: dict(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
                for uid in (row["a_user"], row["b_user"])
            }
            return view

        return await self.run(read)

    async def forget_bot(self, app_id: str, open_ids) -> None:
        """Remove bot accounts that an earlier version mistook for players."""
        open_ids = [str(i) for i in open_ids if i]
        if not open_ids:
            return
        stamp = time.time()

        def forget(conn):
            marks = ",".join("?" * len(open_ids))
            ids = [
                row[0]
                for row in conn.execute(
                    f"SELECT id FROM users WHERE app_id=? AND open_id IN ({marks})",
                    (app_id, *open_ids),
                )
            ]
            if not ids:
                return
            marks = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM group_players WHERE user_id IN ({marks})", ids)
            conn.execute(
                f"UPDATE requests SET status='cancelled',resolved_at=? "
                f"WHERE status='pending' AND to_user IN ({marks})",
                (stamp, *ids),
            )

        await self.run(forget)

    async def cache_get(self, namespace: str, digest: str):
        def read(conn):
            row = conn.execute(
                "SELECT * FROM image_cache WHERE namespace=? AND digest=?",
                (namespace, digest),
            ).fetchone()
            return dict(row) if row else None

        return await self.run(read)

    async def cache_put(self, namespace: str, digest: str, key: str, url: str):
        await self.run(
            lambda c: c.execute(
                "INSERT OR REPLACE INTO image_cache VALUES(?,?,?,?,?)",
                (namespace, digest, key, url, time.time()),
            )
        )

    async def delivery(self, key: str) -> dict:
        def begin(conn):
            conn.execute("INSERT OR IGNORE INTO deliveries VALUES(?,100,0,?)", (key, time.time()))
            return dict(
                conn.execute("SELECT * FROM deliveries WHERE message_key=?", (key,)).fetchone()
            )

        return await self.run(begin)

    async def delivery_update(self, key: str, sequence: int, done: bool):
        await self.run(
            lambda c: c.execute(
                "UPDATE deliveries SET sequence=?,done=?,updated_at=? WHERE message_key=?",
                (sequence, int(done), time.time(), key),
            )
        )

    async def prune_cache(self):
        def prune(conn):
            conn.execute("DELETE FROM deliveries WHERE updated_at<?", (time.time() - 30 * 86400,))
            conn.execute(
                "DELETE FROM image_cache WHERE uploaded_at<? OR (namespace LIKE '%:v2:temp/%' AND uploaded_at<?)",
                (time.time() - 366 * 86400, time.time() - 2 * 86400),
            )

        await self.run(prune)

    async def backup(self, keep: int) -> Path:
        def backup():
            target = self.root / "backups"
            target.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            output = target / f"piggy-{stamp}.zip"
            temp_zip = output.with_suffix(".tmp")
            try:
                with tempfile.TemporaryDirectory(dir=target) as tmp:
                    db_copy = Path(tmp) / "piggy.sqlite3"
                    with (
                        closing(sqlite3.connect(self.path)) as src,
                        closing(sqlite3.connect(db_copy)) as dst,
                    ):
                        src.backup(dst)
                        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise PiggyError("备份完整性检查失败。")
                        dst.row_factory = sqlite3.Row
                        pigs = [
                            dict(p)
                            for p in dst.execute("SELECT * FROM pigs ORDER BY sort_order,id")
                        ]
                    # Export the accepted catalog from the same DB snapshot. An administrator
                    # may be editing the working JSON while this backup runs.
                    manifest = [
                        {
                            **{k: v for k, v in p.items() if k != "battle"},
                            "enabled": bool(p["enabled"]),
                            "image": f"images/{p['asset']}",
                        }
                        for p in pigs
                    ]
                    battle = {
                        "version": 1,
                        "pigs": {p["id"]: json.loads(p["battle"]) for p in pigs if p.get("battle")},
                    }
                    with zipfile.ZipFile(temp_zip, "w", zipfile.ZIP_DEFLATED) as archive:
                        archive.write(db_copy, "piggy.sqlite3")
                        archive.writestr(
                            "catalog/pigs.json", json.dumps(manifest, ensure_ascii=False, indent=2)
                        )
                        archive.writestr(
                            "catalog/battle.json", json.dumps(battle, ensure_ascii=False, indent=2)
                        )
                        for name in sorted({p["asset"] for p in pigs}):
                            archive.write(self.root / "assets" / name, f"catalog/images/{name}")
                        for path in sorted((self.root / "assets").iterdir()):
                            if (
                                path.is_file()
                                and not path.is_symlink()
                                and not path.name.endswith(".tmp")
                            ):
                                archive.write(path, f"assets/{path.name}")
                temp_zip.replace(output)
                for old in sorted(target.glob("piggy-*.zip"))[:-keep]:
                    old.unlink()
                return output
            finally:
                temp_zip.unlink(missing_ok=True)

        task = asyncio.create_task(asyncio.to_thread(backup))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Threaded SQLite backup cannot be interrupted safely. Finish it before unload.
            await task
            raise
