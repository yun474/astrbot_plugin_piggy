"""QQ 官方素材库：随消息上传、发送后回收，失败时在后台重试，尽量少占共用额度。"""

import asyncio
import contextlib
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from .config import PiggyError, Settings
from .delivery import QQError
from .storage import MaterialUnavailable, UploadError

NAME_PREFIX = "piggy-"
# 只清理足够旧的残留图片，避免误删其他请求正在使用的素材。
SWEEP_AGE = 600
SWEEP_INTERVAL = 600
MAINTENANCE_INTERVAL = 30
FOREGROUND_BUDGET = 1.0
BACKGROUND_BUDGET = 5.0
CLOSE_BUDGET = 3.0
CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
UNAVAILABLE = {
    40035007: "QQ 素材库已满 100 张（与其他插件共用），请清理后重试。",
    11253: "当前机器人暂无 QQ 素材库接口权限。",
}
# 上传任务失效、ETag 不一致或服务端内部错误时，重新申请即可。
RETRYABLE = {40035003, 40035006}
MISSING = 40035008
FULL = 40035007


@dataclass
class PendingDelete:
    attempts: int = 0
    due: float = 0.0


@dataclass
class CachedImage:
    image_id: str
    url: str
    expires: float


@dataclass
class MaterialAccount:
    event: object
    next_sweep: float = 0.0
    pending: dict[str, PendingDelete] = field(default_factory=dict)
    # 引用计数：每条发送中的消息和每个缓存条目各占一次，归零后才进入删除队列。
    active: dict[str, int] = field(default_factory=dict)
    # 按图片内容 MD5 缓存的猪图素材，只在本进程内有效。
    cache: dict[str, CachedImage] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MaterialLibrary:
    def __init__(self, settings: Settings, transport, *, logger=None):
        self.settings = settings
        self.transport = transport
        self.logger = logger
        self.accounts: dict[str, MaterialAccount] = {}
        self.worker = None
        self.closed = False

    def _account(self, event, app_id: str) -> MaterialAccount:
        if self.closed:
            raise PiggyError("素材库正在关闭，请稍后重试。")
        account = self.accounts.setdefault(app_id, MaterialAccount(event))
        account.event = event
        if self.worker is None:
            self.worker = asyncio.create_task(self._run_maintenance())
        return account

    async def _run_maintenance(self):
        while True:
            await asyncio.sleep(MAINTENANCE_INTERVAL)
            for account in list(self.accounts.values()):
                await self._maintain(account, time.monotonic() + BACKGROUND_BUDGET)

    async def close(self):
        self.closed = True
        if self.worker is not None:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
            self.worker = None
        # 插件停用后缓存不再复用，尽量当场删除，避免长期占用共用额度；
        # 未删完的会在下次启动后由扫描回收。
        deadline = time.monotonic() + CLOSE_BUDGET
        for account in list(self.accounts.values()):
            for image_id in self._evict(account):
                if image_id in account.active:
                    continue
                with contextlib.suppress(Exception):
                    await self._call(
                        account,
                        deadline,
                        "DELETE",
                        f"/v2/media-images/{quote(image_id, safe='')}/delete",
                    )
        self.accounts.clear()

    @staticmethod
    def _hold(account, image_id: str):
        account.active[image_id] = account.active.get(image_id, 0) + 1

    @staticmethod
    def _release(account, image_id: str):
        remaining = account.active.get(image_id, 1) - 1
        if remaining > 0:
            account.active[image_id] = remaining
        else:
            account.active.pop(image_id, None)
            account.pending.setdefault(image_id, PendingDelete())

    def _uncache(self, account, digest: str):
        cached = account.cache.pop(digest, None)
        if cached is not None:
            self._release(account, cached.image_id)

    def _evict(self, account, *, expired_only: bool = False) -> list[str]:
        now = time.monotonic()
        evicted = []
        for digest, cached in list(account.cache.items()):
            if not expired_only or cached.expires <= now:
                evicted.append(cached.image_id)
                self._uncache(account, digest)
        return evicted

    async def _list(self, event, deadline: float) -> tuple[list[dict], int]:
        """列出整个素材库，供管理员指令使用；错误转换为可直接展示的提示。"""
        images, total, cursor = [], None, ""
        try:
            for _ in range(10):
                remaining = min(deadline - time.monotonic(), self.settings.request_timeout)
                if remaining <= 0:
                    raise asyncio.TimeoutError
                path = "/v2/media-images" + (f"?cursor={quote(cursor, safe='')}" if cursor else "")
                page = await asyncio.wait_for(
                    self.transport.material(event, "GET", path), remaining
                )
                if total is None and isinstance(page.get("total"), int):
                    total = page["total"]
                images += [item for item in page.get("images") or [] if isinstance(item, dict)]
                cursor = page.get("next_cursor") or ""
                if not cursor:
                    break
        except asyncio.TimeoutError:
            raise PiggyError("查询 QQ 素材库超时，请稍后重试。") from None
        except QQError as exc:
            if exc.status == 429:
                raise PiggyError("QQ 素材库接口触发频率限制，请稍后重试。") from None
            if exc.code in UNAVAILABLE:
                raise PiggyError(UNAVAILABLE[exc.code]) from None
            raise PiggyError(
                f"查询 QQ 素材库失败（错误码 {exc.code}，HTTP {exc.status}），请稍后重试。"
            ) from None
        return images, len(images) if total is None else total

    async def usage(self, event, app_id: str, deadline: float) -> dict:
        """统计机器人素材库占用；本插件的图片按文件名前缀区分。"""
        images, total = await self._list(event, deadline)
        account = self.accounts.get(app_id)
        active = account.active if account else {}
        cached = {c.image_id for c in account.cache.values()} if account else set()
        ours = [item for item in images if str(item.get("file_name", "")).startswith(NAME_PREFIX)]
        stale = [
            item
            for item in ours
            if isinstance(item.get("created_at"), int)
            and item["created_at"] < time.time() - SWEEP_AGE
            and str(item.get("image_id", "")) not in active
        ]
        return {
            "total": total,
            "ours": len(ours),
            "cached": sum(str(item.get("image_id", "")) in cached for item in ours),
            "stale": len(stale),
            "others": max(total - len(ours), 0),
        }

    async def purge(self, event, app_id: str, deadline: float, *, everything=False) -> dict:
        """管理员清理：默认只删本插件素材，everything 时清空整个素材库，跳过发送中的图片。"""
        account = self._account(event, app_id)
        self._evict(account)
        images, _ = await self._list(event, deadline)
        result = {"deleted": 0, "failed": 0, "skipped": 0}
        async with account.lock:
            for item in images:
                image_id = str(item.get("image_id") or "")
                ours = str(item.get("file_name", "")).startswith(NAME_PREFIX)
                if not image_id or not (everything or ours):
                    continue
                if image_id in account.active:
                    result["skipped"] += 1
                    continue
                try:
                    await self._call(
                        account,
                        deadline,
                        "DELETE",
                        f"/v2/media-images/{quote(image_id, safe='')}/delete",
                    )
                except QQError as exc:
                    if exc.code != MISSING:
                        result["failed"] += 1
                        continue
                except Exception:
                    result["failed"] += 1
                    continue
                account.pending.pop(image_id, None)
                result["deleted"] += 1
        return result

    def _warn(self, text: str, *args):
        if self.logger is not None:
            self.logger.warning(text, *args)

    async def upload(
        self, event, app_id: str, image: Path | bytes, deadline: float, *, force: bool = False
    ):
        if time.monotonic() >= deadline:
            raise PiggyError("图片上传超过本次回复时限，请再次发送指令。")
        account = self._account(event, app_id)
        await self._maintain(account, min(deadline, time.monotonic() + FOREGROUND_BUDGET))
        # 渲染卡片均为 PNG；猪库素材的后缀已在读取猪库时按真实格式校正。
        if isinstance(image, bytes):
            data, suffix = image, ".png"
        else:
            try:
                data = await asyncio.to_thread(image.read_bytes)
            except OSError:
                raise PiggyError("本地图片读取失败，请检查素材目录。") from None
            suffix = image.suffix.lower()
        if not 0 < len(data) <= 10 * 1024 * 1024:
            raise PiggyError("图片为空或超过 10 MB。")
        md5 = hashlib.md5(data).hexdigest()
        # 只缓存猪库原图；渲染卡片每次内容不同，缓存只会白占额度。
        ttl = self.settings.material_cache_minutes * 60
        cacheable = ttl > 0 and not isinstance(image, bytes)
        cached = account.cache.get(md5) if cacheable else None
        if cached is not None and (force or cached.expires <= time.monotonic()):
            # 诊断或引用失效重传时绕过缓存，过期条目同样作废。
            self._uncache(account, md5)
            cached = None
        if cached is not None:
            self._hold(account, cached.image_id)
            return cached.image_id, cached.url
        try:
            image_id, url = await self._transfer(event, data, suffix, md5, deadline)
        except MaterialUnavailable as exc:
            # 额度已满时先释放本插件闲置的缓存，再重试一次。
            if not exc.full or not self._evict(account):
                raise
            await self._maintain(
                account, min(deadline, time.monotonic() + FOREGROUND_BUDGET), scan=False
            )
            image_id, url = await self._transfer(event, data, suffix, md5, deadline)
        self._hold(account, image_id)
        if cacheable:
            self._uncache(account, md5)
            self._hold(account, image_id)
            account.cache[md5] = CachedImage(image_id, url, time.monotonic() + ttl)
        return image_id, url

    async def _transfer(self, event, data: bytes, suffix: str, md5: str, deadline: float):
        content_type = CONTENT_TYPES.get(suffix, "image/png")
        body = {
            "file_name": f"{NAME_PREFIX}{md5[:16]}{suffix}",
            "file_size": len(data),
            "content_type": content_type,
            "md5": md5,
            "sha1": hashlib.sha1(data).hexdigest(),
        }
        for attempt in range(self.settings.upload_retry_count + 1):
            if time.monotonic() >= deadline:
                raise PiggyError("图片上传超过本次回复时限，请再次发送指令。")
            try:
                prepared = await self.transport.material(
                    event, "POST", "/v2/media-images/prepare", body, "upload_id"
                )
                etag = await self.transport.put_presigned(
                    prepared["presigned_url"], data, content_type
                )
                if etag != md5:
                    raise UploadError("QQ 素材上传校验不一致。", True)
                done = await self.transport.material(
                    event,
                    "POST",
                    "/v2/media-images/complete",
                    {"upload_id": prepared["upload_id"], "md5": etag},
                    "url",
                )
                if not isinstance(done.get("image_id"), str) or not done["image_id"]:
                    raise UploadError("QQ 素材库没有返回图片 ID。", True)
                return done["image_id"], done["url"]
            except QQError as exc:
                if exc.status == 429:
                    raise MaterialUnavailable("QQ 素材库接口触发频率限制，请稍后重试。") from None
                if exc.code in UNAVAILABLE:
                    raise MaterialUnavailable(
                        UNAVAILABLE[exc.code], full=exc.code == FULL
                    ) from None
                error = UploadError(
                    f"QQ 素材上传失败（错误码 {exc.code}，HTTP {exc.status}）。",
                    exc.uncertain or exc.code in RETRYABLE,
                    diagnostic=f"reason={exc.reason} trace_id={exc.trace_id}",
                )
            except UploadError as exc:
                error = exc
            error.attempt = attempt + 1
            delay = self.settings.delay(attempt)
            if (
                not error.retryable
                or attempt == self.settings.upload_retry_count
                or time.monotonic() + delay >= deadline
            ):
                raise error
            await asyncio.sleep(delay)
        raise AssertionError("Unreachable material upload state")

    async def delete(self, event, app_id: str, image_ids: list[str]):
        account = self._account(event, app_id)
        # 在第一次 await 前登记全部待删除项，超时或取消时也不会漏掉后面的图片。
        for image_id in image_ids:
            self._release(account, image_id)
        await self._maintain(account, time.monotonic() + FOREGROUND_BUDGET, scan=False)

    async def _call(self, account, deadline, method, path):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return await asyncio.wait_for(
            self.transport.material(account.event, method, path),
            min(remaining, self.settings.request_timeout),
        )

    async def _maintain(self, account, deadline, *, scan=True):
        # 到期缓存先转入删除队列；这一步不涉及网络，无需等锁。
        self._evict(account, expired_only=True)
        # 同账号只跑一轮清理；并发回复无需等候其他清理任务释放锁。
        if account.lock.locked():
            return
        async with account.lock:
            await self._delete_pending(account, deadline)
            if scan and time.monotonic() >= account.next_sweep:
                await self._sweep(account, deadline)
            await self._delete_pending(account, deadline)

    async def _delete_pending(self, account, deadline):
        # 按到期时间排序，避免一直失败的旧条目占满每轮预算。
        for image_id, pending in sorted(
            list(account.pending.items()), key=lambda item: item[1].due
        ):
            now = time.monotonic()
            if now >= deadline:
                break
            if image_id in account.active or pending.due > now:
                continue
            pending.attempts += 1
            pending.due = now + min(30 * 2 ** min(pending.attempts - 1, 4), 300)
            try:
                await self._call(
                    account,
                    deadline,
                    "DELETE",
                    f"/v2/media-images/{quote(image_id, safe='')}/delete",
                )
            except QQError as exc:
                if exc.code == MISSING:
                    account.pending.pop(image_id, None)
                else:
                    self._warn(
                        "[piggy] QQ material delete failed code=%s http=%s.", exc.code, exc.status
                    )
            except Exception as exc:
                self._warn("[piggy] QQ material delete failed (%s).", type(exc).__name__)
            else:
                account.pending.pop(image_id, None)

    async def _sweep(self, account, deadline):
        """定期发现残留图片，跳过新图、其他插件及本进程仍在发送的图片。"""
        cursor = ""
        # 失败或预算耗尽后下轮再试，不能永久记成已经扫描成功。
        account.next_sweep = time.monotonic() + MAINTENANCE_INTERVAL
        try:
            for _ in range(10):
                path = "/v2/media-images" + (f"?cursor={quote(cursor, safe='')}" if cursor else "")
                page = await self._call(account, deadline, "GET", path)
                for item in page.get("images") or []:
                    if (
                        isinstance(item, dict)
                        and str(item.get("file_name", "")).startswith(NAME_PREFIX)
                        and isinstance(item.get("created_at"), int)
                        and item["created_at"] < time.time() - SWEEP_AGE
                        and item.get("image_id")
                        and str(item["image_id"]) not in account.active
                    ):
                        account.pending.setdefault(str(item["image_id"]), PendingDelete())
                cursor = page.get("next_cursor") or ""
                if not cursor:
                    account.next_sweep = time.monotonic() + SWEEP_INTERVAL
                    break
        except QQError as exc:
            self._warn("[piggy] QQ material list failed code=%s http=%s.", exc.code, exc.status)
        except Exception as exc:
            self._warn("[piggy] QQ material list failed (%s).", type(exc).__name__)
