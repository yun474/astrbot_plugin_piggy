import asyncio
import json
import secrets
import sqlite3
import tempfile
import time
import zipfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import PiggyError

EAST_ASIA = timezone(timedelta(hours=8))


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
            if version not in (0, 1):
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
                PRAGMA user_version=1;
                COMMIT;
            """)

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
                INSERT INTO pigs VALUES(:id,:name,:description,:analysis,:asset,:enabled,:sort_order)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, description=excluded.description,
                analysis=excluded.analysis, asset=excluded.asset, enabled=excluded.enabled,
                sort_order=excluded.sort_order
            """,
                pigs,
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
        self, user_id: int, group_id: str, event_id: str, now: datetime | None = None
    ) -> dict:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("The draw clock must be timezone-aware")
        day = now.astimezone(EAST_ASIA).date().isoformat()

        def draw(conn):
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM draw_records WHERE user_id=? AND day=?", (user_id, day)
            ).fetchone()
            created = existing is None
            if created:
                available = conn.execute(
                    "SELECT * FROM pigs WHERE enabled=1 ORDER BY id"
                ).fetchall()
                if not available:
                    raise PiggyError("猪库没有启用的小猪，请联系管理员检查。")
                pig = dict(secrets.choice(available))
                timestamp = now.timestamp()
                conn.execute(
                    """
                    INSERT INTO draw_records(user_id,day,drawn_at,pig_id,source_group,source_event,snapshot)
                    VALUES(?,?,?,?,?,?,?)
                """,
                    (
                        user_id,
                        day,
                        timestamp,
                        pig["id"],
                        group_id,
                        event_id,
                        json.dumps(pig, ensure_ascii=False),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO collections VALUES(?,?,1,?,?) ON CONFLICT(user_id,pig_id)
                    DO UPDATE SET count=count+1,last_at=excluded.last_at
                """,
                    (user_id, pig["id"], timestamp, timestamp),
                )
            else:
                pig = json.loads(existing["snapshot"])
            count = conn.execute(
                "SELECT count FROM collections WHERE user_id=? AND pig_id=?",
                (user_id, pig["id"]),
            ).fetchone()[0]
            return {
                "pig": pig,
                "day": day,
                "created": created,
                "count": count,
                "new_species": created and count == 1,
            }

        return await self.run(draw)

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

    async def ranking(self, app_id: str, group_id: str, user_id: int, kind: str) -> dict:
        if kind not in {"species", "total"}:
            raise PiggyError("排行类型只能为种类或数量。")
        if not group_id:
            raise PiggyError("请在群里查看本群玩家排行。")

        def read(conn):
            rows = conn.execute(
                f"""
                WITH scores AS (
                    SELECT u.id,u.nickname,u.alias,count(c.pig_id) species,coalesce(sum(c.count),0) total
                    FROM group_players g JOIN users u ON u.id=g.user_id
                    LEFT JOIN collections c ON c.user_id=u.id
                    WHERE g.app_id=? AND g.group_id=? GROUP BY u.id
                ), ranked AS (
                    SELECT *,RANK() OVER (ORDER BY {kind} DESC) AS rank FROM scores
                ) SELECT * FROM ranked ORDER BY {kind} DESC,id
            """,
                (app_id, group_id),
            ).fetchall()
            players = [dict(row) for row in rows]
            return {
                "players": players,
                "mine": next((p for p in players if p["id"] == user_id), None),
                "kind": kind,
            }

        return await self.run(read)

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
                "DELETE FROM image_cache WHERE uploaded_at<?",
                (time.time() - 366 * 86400,),
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
                        {**p, "enabled": bool(p["enabled"]), "image": f"images/{p['asset']}"}
                        for p in pigs
                    ]
                    with zipfile.ZipFile(temp_zip, "w", zipfile.ZIP_DEFLATED) as archive:
                        archive.write(db_copy, "piggy.sqlite3")
                        archive.writestr(
                            "catalog/pigs.json", json.dumps(manifest, ensure_ascii=False, indent=2)
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
