import asyncio
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import aiohttp

from .config import PiggyError, Settings
from .database import Database
from .storage import ImagePublisher, response_json

IMAGE_ERRORS = {304010, 40034004}
EXPIRED_ERRORS = {304103, 40034005, 40034128}
DEDUPE_ERRORS = {40054005}


class QQError(PiggyError):
    def __init__(self, code: int, status: int, uncertain: bool = False):
        self.code = code
        self.status = status
        self.uncertain = uncertain
        if uncertain:
            message = "QQ 发送结果暂时无法确认，请先查看群消息；抽取记录已保存。"
        elif code in IMAGE_ERRORS:
            message = "QQ 图片转存未成功，已达到配置的重试次数；抽取记录已保存，请稍后重试。"
        elif code in EXPIRED_ERRORS:
            message = "本条消息的回复时限已过，请重新发送指令；不会重复计数。"
        else:
            message = (
                f"QQ 拒绝本次消息（错误码 {code}，HTTP {status}），请检查平台权限、格式或频率限制。"
            )
        super().__init__(message)


class QQTransport:
    """Use AstrBot's token lifecycle, but retain QQ error codes discarded by botpy 1.2.1."""

    def __init__(self, timeout: float):
        self.timeout = timeout
        self.session = None

    async def request(self, event, payload: dict) -> dict:
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
        http = event.bot.api._http
        await http.check_session()
        group = event.get_group_id()
        if not group:
            raise PiggyError("此版本面向 QQ 官方群聊，请在群内使用。")
        domain = (
            "https://sandbox.api.sgroup.qq.com" if http.is_sandbox else "https://api.bot.qq.com"
        )
        url = f"{domain}/v2/groups/{quote(str(group), safe='')}/messages"
        headers = {
            k: v for k, v in http._headers.items() if k in {"Authorization", "X-Union-Appid"}
        }
        try:
            async with self.session.post(
                url, json=payload, headers=headers, allow_redirects=False
            ) as response:
                try:
                    data = await response_json(response)
                except (ValueError, UnicodeDecodeError):
                    data = None
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
                    )
                if not data.get("id"):
                    raise QQError(0, response.status, uncertain=True)
                return data
        except (aiohttp.ClientError, TimeoutError):
            raise QQError(0, 0, uncertain=True) from None

    async def close(self):
        if self.session is not None:
            await self.session.close()


@dataclass(frozen=True)
class Message:
    text: str
    images: tuple[Path, ...] = ()
    keyboard: dict | None = None

    def content(self, urls: list[str]) -> str:
        result = self.text
        for index, url in enumerate(urls):
            result = result.replace(f"{{{{image:{index}}}}}", url)
        return result


def message_key(event, app_id: str) -> str:
    raw = f"{app_id}:{event.get_group_id()}:{event.message_obj.message_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


class Sender:
    def __init__(self, settings: Settings, db: Database, publisher: ImagePublisher, transport):
        self.settings, self.db, self.publisher, self.transport = (
            settings,
            db,
            publisher,
            transport,
        )

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
        sequence = receipt["sequence"]
        deadline = deadline or (time.monotonic() + 240)
        urls = []
        for path in message.images:
            urls.append(await self.publisher.publish(path, force=force_upload, deadline=deadline))
        refreshed = False
        for attempt in range(self.settings.image_retry_count + 1):
            if time.monotonic() >= deadline:
                raise PiggyError("本次回复等待过久，请重新发送指令；抽取记录已保留。")
            payload = {
                "msg_type": 2,
                "msg_id": event.message_obj.message_id,
                "msg_seq": sequence,
                "markdown": {
                    "content": message.content(urls),
                    "force_verify_image_resource": True,
                },
            }
            if message.keyboard:
                payload["keyboard"] = message.keyboard
            try:
                await self.transport.request(event, payload)
            except QQError as exc:
                if exc.code in DEDUPE_ERRORS:
                    # Same persisted sequence: a previous attempt already reached QQ.
                    await self.db.delivery_update(key, sequence, True)
                    return
                retryable = exc.code in IMAGE_ERRORS or exc.uncertain or exc.status == 429
                if not retryable:
                    raise
                if not exc.uncertain:
                    # QQ explicitly rejected this message. It is safe to allocate the next sequence.
                    sequence += 1
                    await self.db.delivery_update(key, sequence, False)
                if attempt == self.settings.image_retry_count:
                    raise
                delay = self.settings.delay(attempt)
                if time.monotonic() + delay >= deadline:
                    raise
                await asyncio.sleep(delay)
                if exc.code in IMAGE_ERRORS and not refreshed:
                    # Repair expired/deleted host objects once per message, then let QQ retry transfer.
                    urls = [
                        await self.publisher.publish(path, force=True, deadline=deadline)
                        for path in message.images
                    ]
                    refreshed = True
                continue
            await self.db.delivery_update(key, sequence, True)
            return
