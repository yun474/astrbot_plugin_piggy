import asyncio
import contextlib
import sqlite3
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
    QQOfficialMessageEvent,
)
from botpy.message import GroupMessage

from .core.avatars import Avatars
from .core.catalog import initialize_catalog, read_catalog
from .core.config import PiggyError, Settings
from .core.database import Database
from .core.delivery import Message, QQError, QQTransport, Sender, message_key
from .core.diagnostics import exception_detail
from .core.rendering import clean_cards
from .core.storage import ImagePublisher, UploadError
from .core.views import collection_message, ranking_message, today_message

MAX_INFLIGHT = 12
REQUEST_TIMEOUT = 240


@register("astrbot_plugin_piggy", "yun474", "QQ 官方机器人每日小猪收集", "1.0.0")
class PiggyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.settings = Settings.from_dict(config)
        self.root = StarTools.get_data_dir("astrbot_plugin_piggy")
        self.db = Database(self.root)
        self.publisher = ImagePublisher(self.settings, self.db)
        self.transport = QQTransport(self.settings.request_timeout)
        self.sender = Sender(self.settings, self.db, self.publisher, self.transport)
        self.avatars = Avatars()
        self.ready = False
        self.init_lock = asyncio.Lock()
        self.maintenance_lock = asyncio.Lock()
        self.limit = asyncio.Semaphore(3)
        self.inflight = {}
        self.active_users = set()
        self.busy_notice = None
        self.stopping = False
        self.backup_task = None
        self.cleanup_task = None

    async def initialize(self):
        async with self.init_lock:
            if self.ready:
                return
            await self.db.initialize()
            if not await self.db.has_catalog():
                await asyncio.to_thread(
                    initialize_catalog, self.root, Path(__file__).parent / "resources"
                )
            try:
                pigs = await asyncio.to_thread(read_catalog, self.root)
                await self.db.catalog(pigs)
            except PiggyError as exc:
                if not await self.db.has_catalog():
                    raise
                logger.error("[piggy] Catalog reload rejected, keeping previous catalog: %s", exc)
            self.ready = True
            self.backup_task = asyncio.create_task(self._backups())
            self.cleanup_task = asyncio.create_task(self._cleanup())

    async def _backups(self):
        while True:
            try:
                async with self.maintenance_lock:
                    await self.db.backup(self.settings.backup_keep)
            except Exception as exc:
                logger.error(
                    "[piggy] Scheduled backup failed (%s); original database retained.",
                    type(exc).__name__,
                )
            await asyncio.sleep(86400)

    async def _cleanup(self):
        while True:
            try:
                await self.db.prune_cache()
                await asyncio.to_thread(clean_cards, self.root)
            except Exception as exc:
                logger.warning(
                    "[piggy] Cache cleanup failed: %s", exception_detail(exc, self.settings)
                )
            await asyncio.sleep(3600)

    async def _handle(self, event: AstrMessageEvent, command: str, args: tuple = ()):
        if (
            not isinstance(event, QQOfficialMessageEvent)
            or not isinstance(event.message_obj.raw_message, GroupMessage)
            or not event.get_group_id()
        ):
            await event.send(event.plain_result("今日小猪收集版目前支持 QQ 官方机器人群聊。"))
            return
        event.stop_event()
        if self.stopping:
            return
        token = event.bot.api._http._token
        app_id = str(token.app_id or "")
        if not app_id or not event.message_obj.message_id:
            await event.send(event.plain_result("未取得官方消息标识，暂时无法处理收藏。"))
            return
        key = message_key(event, app_id)
        if key in self.inflight:
            await asyncio.shield(self.inflight[key])
            return
        owner = (app_id, event.get_sender_id())
        if owner in self.active_users or len(self.inflight) >= MAX_INFLIGHT:
            # A burst must not create another unbounded queue of busy replies.
            if self.busy_notice is None or self.busy_notice.done():
                self.busy_notice = asyncio.create_task(
                    self._failure(event, "小猪正在处理请求，请等当前请求完成后再试。")
                )
                await asyncio.shield(self.busy_notice)
            return
        task = asyncio.create_task(
            self._run_request(event, app_id, command, args, time.monotonic() + REQUEST_TIMEOUT)
        )
        self.inflight[key] = task
        self.active_users.add(owner)

        def finished(_):
            self.inflight.pop(key, None)
            self.active_users.discard(owner)

        task.add_done_callback(finished)
        await asyncio.shield(task)

    async def _run_request(self, event, app_id, command, args, deadline):
        try:
            await asyncio.wait_for(self.limit.acquire(), max(0, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            if not self.stopping:
                await self._failure(event, "本次请求排队超时，请重新发送指令。")
            return
        try:
            if not self.stopping:
                await self._execute(event, app_id, command, args, deadline)
        finally:
            self.limit.release()

    async def _execute(self, event, app_id: str, command: str, args: tuple, deadline: float):
        try:
            if time.monotonic() >= deadline:
                raise PiggyError("本次请求排队超时，请重新发送指令。")
            await self.initialize()
            receipt = await self.db.delivery(message_key(event, app_id))
            if receipt["done"]:
                return
            raw = event.message_obj.raw_message
            nickname = getattr(getattr(raw, "author", None), "username", "") or ""
            if not nickname:
                nickname = (getattr(raw, "raw_data", {}) or {}).get("author", {}).get(
                    "username", ""
                ) or ""
            user = await self.db.identify(
                app_id, event.get_sender_id(), event.get_group_id(), nickname
            )
            if command == "draw":
                if self.settings.use_host(command):
                    self.settings.check_host()
                result = await self.db.draw(
                    user["id"], event.get_group_id(), event.message_obj.message_id
                )
                message = await asyncio.to_thread(
                    today_message,
                    self.settings,
                    self.root,
                    user,
                    result,
                    await self.db.collection(user["id"]),
                )
            elif command in {"atlas", "pen"}:
                message = await collection_message(
                    self.settings,
                    self.root,
                    user,
                    await self.db.collection(user["id"]),
                    args[0],
                    command == "atlas",
                )
            elif command == "ranking":
                kind = args[0]
                if kind not in {"种类", "数量"}:
                    raise PiggyError("用法：小猪排行 种类/数量")
                boards = await self.db.rankings(app_id, event.get_group_id())
                avatars = await self.avatars.get_many(app_id, boards["species"] + boards["total"])
                message = await asyncio.to_thread(
                    ranking_message, self.settings, self.root, user, boards, avatars
                )
            elif command == "alias":
                await self.db.set_alias(user["id"], args[0])
                message = Message("称呼已经保存啦。")
            elif command == "reload":
                async with self.maintenance_lock:
                    pigs = await asyncio.to_thread(read_catalog, self.root)
                    await self.db.catalog(pigs)
                message = Message(
                    f"猪库重载完成，当前启用 {sum(p['enabled'] for p in pigs)} 种小猪。"
                )
            elif command == "backup":
                async with self.maintenance_lock:
                    await self.db.backup(self.settings.backup_keep)
                message = Message("备份已保存到插件数据目录 backups，包含数据库、猪库及历史素材。")
            else:
                self.settings.check_host()
                progress = await self.db.collection(user["id"])
                pig = next(p for p in progress["entries"] if p["enabled"])
                message = Message(
                    "# 图片链路检查\n\n![诊断小猪 #192px #192px]({{image:0}})\n\nQQ 图片转存检查已通过。",
                    (self.root / "assets" / pig["asset"],),
                )
            await self.sender.send(
                event, app_id, message, deadline, force_upload=command == "diagnose"
            )
        except PiggyError as exc:
            if isinstance(exc, UploadError):
                logger.warning(
                    "[piggy] Upload failed command=%s attempt=%s/%s retryable=%s: %s | %s",
                    command,
                    exc.attempt,
                    self.settings.upload_retry_count + 1,
                    exc.retryable,
                    exc,
                    exc.diagnostic or exception_detail(exc, self.settings),
                )
            else:
                logger.warning("[piggy] Request failed command=%s: %s", command, exc)
            if isinstance(exc, QQError) and exc.uncertain:
                return
            await self._failure(event, str(exc))
        except (sqlite3.Error, OSError) as exc:
            logger.error("[piggy] Storage failure: %s", exception_detail(exc, self.settings))
            await self._failure(
                event,
                "数据或素材暂时无法读写，请联系管理员检查。已有抽取记录不会被清空。",
            )
        except Exception as exc:
            logger.error(
                "[piggy] Unexpected request failure: %s", exception_detail(exc, self.settings)
            )
            await self._failure(event, "处理暂时失败，请联系管理员查看插件日志。")

    async def _failure(self, event, text: str):
        try:
            await self.transport.request(
                event,
                {
                    "msg_type": 0,
                    "content": text,
                    "msg_id": event.message_obj.message_id,
                    "msg_seq": 9999,
                },
            )
        except Exception as exc:
            logger.warning("[piggy] Could not send failure notice (%s).", type(exc).__name__)

    @filter.command("今日小猪", alias={"抽小猪"})
    async def draw(self, event: AstrMessageEvent):
        """每天领一只小猪，跨群共享收藏；当天重复使用会返回同一只。"""
        await self._handle(event, "draw")

    @filter.command("小猪图鉴")
    async def atlas(self, event: AstrMessageEvent, page: int = 1):
        """查看小猪图鉴和解锁进度。"""
        await self._handle(event, "atlas", (page,))

    @filter.command("我的猪圈")
    async def pen(self, event: AstrMessageEvent, page: int = 1):
        """查看已收藏的小猪及数量。"""
        await self._handle(event, "pen", (page,))

    @filter.command("小猪排行")
    async def ranking(self, event: AstrMessageEvent, kind: str = "种类"):
        """查看本群玩家的种类、数量双榜，各前十。"""
        await self._handle(event, "ranking", (kind,))

    @filter.command("小猪称呼")
    async def alias(self, event: AstrMessageEvent, name: str):
        """设置 1–24 字的展示称呼。用法：小猪称呼 名字；不会改变收藏归属。"""
        await self._handle(event, "alias", (name,))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("小猪重载")
    async def reload(self, event: AstrMessageEvent):
        """仅管理员：校验并重载猪库；校验失败时保留上一份有效猪库。"""
        await self._handle(event, "reload")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("小猪备份")
    async def backup(self, event: AstrMessageEvent):
        """仅管理员：将数据库、猪库和历史素材备份到本地 backups 目录。"""
        await self._handle(event, "backup")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("小猪诊断")
    async def diagnose(self, event: AstrMessageEvent):
        """仅管理员：绕过缓存上传测试猪图，发送消息并检查 QQ 图片转存链路。"""
        await self._handle(event, "diagnose")

    async def terminate(self):
        self.stopping = True
        # Drain active thread-backed work before closing clients; queued work skips execution.
        tasks = list(self.inflight.values())
        if self.busy_notice:
            tasks.append(self.busy_notice)
        await asyncio.gather(*tasks, return_exceptions=True)
        async with self.init_lock:
            pass
        for task in (self.backup_task, self.cleanup_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self.publisher.close()
        await self.transport.close()
        await self.avatars.close()
