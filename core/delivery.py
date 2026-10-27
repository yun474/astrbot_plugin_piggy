import asyncio
import base64
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import aiohttp

from .config import PiggyError, Settings
from .database import Database
from .diagnostics import safe_detail
from .storage import ImagePublisher, MaterialUnavailable, UploadError, response_json

# 40035009/40035010：素材库引用已失效，重新上传即可恢复。
IMAGE_ERRORS = {304010, 40034004, 40035009, 40035010}
# 遇到强制校验拒绝时重试，避免不必要地重新上传图片。
RETRYABLE_ERRORS = IMAGE_ERRORS | {40034141}
EXPIRED_ERRORS = {304103, 40034005, 40034128}
DEDUPE_ERRORS = {40054005}


class QQError(PiggyError):
    def __init__(self, code: int, status: int, uncertain: bool = False, *, reason="", trace_id=""):
        self.code = code
        self.status = status
        self.uncertain = uncertain
        self.reason = reason if isinstance(reason, str) else ""
        self.trace_id = trace_id if isinstance(trace_id, str) else ""
        if uncertain:
            message = "QQ 发送结果暂时无法确认，请先查看群消息；抽取记录已保存。"
        elif code in IMAGE_ERRORS:
            message = "QQ 图片转存未成功；抽取记录已保存，请稍后重试。"
        elif code in EXPIRED_ERRORS:
            message = "本条消息的回复时限已过，请重新发送指令；不会重复计数。"
        else:
            message = (
                f"QQ 拒绝本次消息（错误码 {code}，HTTP {status}），请检查平台权限、格式或频率限制。"
            )
        super().__init__(message)

    @property
    def retryable(self) -> bool:
        return self.uncertain or self.status == 429 or self.code in RETRYABLE_ERRORS


class QQTransport:
    """沿用 AstrBot 的 token 生命周期，同时保留 botpy 1.2.1 丢弃的 QQ 错误码。"""

    def __init__(self, timeout: float):
        self.timeout = timeout
        self.session = None

    async def request(self, event, payload: dict) -> dict:
        return await self._request(event, payload, "messages", "id")

    async def upload_image(self, event, data: bytes) -> str:
        if not 0 < len(data) <= 10 * 1024 * 1024:
            raise PiggyError("渲染图片为空或超过 10 MB，请减少单页内容。")
        result = await self._request(
            event,
            {
                "file_type": 1,
                "file_data": base64.b64encode(data).decode("ascii"),
                "srv_send_msg": False,
            },
            "files",
            "file_info",
        )
        return result["file_info"]

    async def material(
        self, event, method: str, path: str, payload: dict | None = None, expected=None
    ) -> dict:
        return await self._call(event, method, path, payload, expected)

    async def put_presigned(self, url: str, data: bytes, content_type: str) -> str:
        """上传到素材库预签名地址，返回去掉引号的 ETag。"""
        self._ensure_session()
        try:
            async with self.session.put(
                url, data=data, headers={"Content-Type": content_type}, allow_redirects=False
            ) as response:
                await response.read()
                if response.status != 200:
                    raise UploadError(f"QQ 素材上传被拒绝（HTTP {response.status}）。", True)
                return response.headers.get("ETag", "").strip('"').lower()
        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError):
            raise UploadError("QQ 素材上传连接超时或中断。", True) from None

    def _ensure_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))

    async def _request(self, event, payload: dict, resource: str, expected: str) -> dict:
        group = event.get_group_id()
        if not group:
            raise PiggyError("此版本面向 QQ 官方群聊，请在群内使用。")
        path = f"/v2/groups/{quote(str(group), safe='')}/{resource}"
        return await self._call(event, "POST", path, payload, expected)

    async def _call(self, event, method: str, path: str, payload, expected) -> dict:
        self._ensure_session()
        http = event.bot.api._http
        await http.check_session()
        domain = (
            "https://sandbox.api.sgroup.qq.com" if http.is_sandbox else "https://api.bot.qq.com"
        )
        headers = {
            k: v for k, v in http._headers.items() if k in {"Authorization", "X-Union-Appid"}
        }
        try:
            async with getattr(self.session, method.lower())(
                domain + path, json=payload, headers=headers, allow_redirects=False
            ) as response:
                try:
                    data = await response_json(response)
                except (ValueError, UnicodeDecodeError):
                    data = None
                # 素材删除接口成功时可能只返回空响应体。
                if data is None and expected is None and response.status in {200, 204}:
                    return {}
                if not isinstance(data, dict):
                    raise QQError(
                        0,
                        response.status,
                        uncertain=response.status >= 500 or response.status < 300,
                    )
                try:
                    code = int(data.get("err_code", data.get("code", 0)))
                except (TypeError, ValueError):
                    raise QQError(0, response.status, uncertain=True) from None
                if code or response.status not in {200, 201, 202}:
                    raise QQError(
                        code,
                        response.status,
                        uncertain=response.status >= 500 and code not in IMAGE_ERRORS,
                        reason=data.get("message", ""),
                        trace_id=data.get("trace_id", ""),
                    )
                if expected is not None and (
                    not isinstance(data.get(expected), str) or not data[expected]
                ):
                    raise QQError(0, response.status, uncertain=True)
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError):
            raise QQError(0, 0, uncertain=True) from None

    async def close(self):
        if self.session is not None:
            await self.session.close()


@dataclass(frozen=True)
class Message:
    text: str
    images: tuple[Path | bytes, ...] = ()
    keyboard: dict | None = None
    local: bool = False
    # 素材库不可用时改发的本地卡片，按需渲染。
    fallback: Callable[[], "Message"] | None = field(default=None, compare=False, repr=False)

    def content(self, urls: list[str]) -> str:
        result = self.text
        for index, url in enumerate(urls):
            result = result.replace(f"{{{{image:{index}}}}}", url)
        return result


def message_key(event, app_id: str) -> str:
    raw = f"{app_id}:{event.get_group_id()}:{event.message_obj.message_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


class Sender:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        publisher: ImagePublisher,
        transport,
        *,
        logger=None,
        material=None,
    ):
        self.settings, self.db, self.publisher, self.transport = (
            settings,
            db,
            publisher,
            transport,
        )
        self.logger = logger
        self.material = material

    def _retry_delay(
        self,
        exc: QQError,
        attempt: int,
        retry_count: int,
        deadline: float,
        stage: str,
        retryable: bool,
    ) -> float | None:
        delay = self.settings.delay(attempt)
        if not retryable:
            stop = "not_retryable"
        elif attempt >= retry_count:
            stop = "exhausted"
        elif time.monotonic() + delay >= deadline:
            stop = "deadline"
        else:
            stop = ""
        if self.logger is not None:
            detail = safe_detail(f"reason={exc.reason} trace_id={exc.trace_id}", self.settings)
            self.logger.warning(
                "[piggy] QQ failure stage=%s attempt=%s/%s code=%s http=%s "
                "retryable=%s stop=%s retry_delay=%s | %s",
                stage,
                attempt + 1,
                retry_count + 1,
                exc.code,
                exc.status,
                retryable,
                stop or "none",
                0 if stop else delay,
                detail,
            )
        return None if stop else delay

    async def send(
        self,
        event,
        app_id: str,
        message: Message,
        deadline: float | None = None,
        *,
        force_upload: bool = False,
    ):
        key = message_key(event, app_id)
        receipt = await self.db.delivery(key)
        if receipt["done"]:
            return
        deadline = deadline if deadline is not None else time.monotonic() + 240
        uploaded = []
        try:
            await self._deliver(
                event, app_id, key, message, receipt["sequence"], deadline, force_upload, uploaded
            )
        finally:
            # 素材库图片只服务本条消息；QQ 发送时已保存副本，删除不影响已发出的消息。
            if uploaded:
                await self.material.delete(event, app_id, uploaded)

    async def _publish(self, event, app_id, message, force, deadline, uploaded) -> list[str]:
        if self.settings.provider != "qq":
            return [
                await self.publisher.publish(path, force=force, deadline=deadline)
                for path in message.images
            ]
        urls = []
        for image in message.images:
            image_id, url = await self.material.upload(event, app_id, image, deadline, force=force)
            uploaded.append(image_id)
            urls.append(url)
        return urls

    async def _prepare(self, event, app_id, message, force, deadline, uploaded):
        """首次上传和图片重传共用降级路径，发送序号由外层统一管理。"""
        urls = []
        media = None
        if not message.local and message.images:
            try:
                urls = await self._publish(event, app_id, message, force, deadline, uploaded)
            except MaterialUnavailable as exc:
                if message.fallback is None:
                    raise
                if self.logger is not None:
                    self.logger.warning("[piggy] %s Falling back to a local card.", exc)
                message = await asyncio.to_thread(message.fallback)
        if message.local:
            if len(message.images) != 1 or not isinstance(message.images[0], bytes):
                raise PiggyError("普通图片消息必须包含一张完整的渲染卡片。")
            media = await self._upload_local(event, message.images[0], deadline)
        return message, urls, media

    async def _deliver(self, event, app_id, key, message, sequence, deadline, force, uploaded):
        message, urls, media = await self._prepare(
            event, app_id, message, force, deadline, uploaded
        )
        refreshed = False
        for attempt in range(self.settings.image_retry_count + 1):
            if time.monotonic() >= deadline:
                raise PiggyError("本次回复等待过久，请重新发送指令；抽取记录已保留。")
            payload = {
                "msg_id": event.message_obj.message_id,
                "msg_seq": sequence,
            }
            if message.local:
                payload.update(msg_type=7, media={"file_info": media})
            elif message.images:
                payload.update(
                    msg_type=2,
                    markdown={
                        "content": message.content(urls),
                        "force_verify_image_resource": True,
                    },
                )
                if message.keyboard:
                    payload["keyboard"] = message.keyboard
            else:
                payload.update(msg_type=0, content=message.text)
            try:
                await self.transport.request(event, payload)
            except QQError as exc:
                if exc.code in DEDUPE_ERRORS:
                    # 已保存的序号相同，说明之前的尝试已经送达 QQ。
                    await self.db.delivery_update(key, sequence, True)
                    return
                image_error = exc.code in IMAGE_ERRORS or (message.local and exc.code == 304080)
                retryable = image_error or exc.retryable
                delay = self._retry_delay(
                    exc, attempt, self.settings.image_retry_count, deadline, "send", retryable
                )
                if not retryable:
                    raise
                if not exc.uncertain:
                    # QQ 明确拒绝了这条消息，可以安全分配下一个序号。
                    sequence += 1
                    await self.db.delivery_update(key, sequence, False)
                if delay is None:
                    raise
                await asyncio.sleep(delay)
                if image_error and message.local:
                    media = await self._upload_local(event, message.images[0], deadline)
                elif image_error and not refreshed:
                    # 每条消息只修复一次过期或被删除的图床对象，然后让 QQ 重新转存。
                    message, urls, media = await self._prepare(
                        event, app_id, message, True, deadline, uploaded
                    )
                    refreshed = True
                continue
            await self.db.delivery_update(key, sequence, True)
            return

    async def _upload_local(self, event, data: bytes, deadline: float) -> str:
        for attempt in range(self.settings.upload_retry_count + 1):
            if time.monotonic() >= deadline:
                raise PiggyError("QQ 本地图片上传超过回复时限，请重新发送指令。")
            try:
                return await self.transport.upload_image(event, data)
            except QQError as exc:
                delay = self._retry_delay(
                    exc,
                    attempt,
                    self.settings.upload_retry_count,
                    deadline,
                    "upload",
                    exc.retryable,
                )
                if delay is None:
                    raise PiggyError(
                        f"QQ 本地图片上传失败（错误码 {exc.code}，HTTP {exc.status}，"
                        f"尝试 {attempt + 1}/{self.settings.upload_retry_count + 1} 次）。"
                    ) from None
                await asyncio.sleep(delay)
        raise AssertionError("Unreachable local upload state")
