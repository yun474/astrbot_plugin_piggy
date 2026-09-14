import asyncio
import base64
import hashlib
import json
import mimetypes
import time
from dataclasses import asdict
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import aiohttp

from .config import PiggyError, Settings, https_url
from .database import Database


class UploadError(PiggyError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class ImageHost(Protocol):
    async def upload(self, data: bytes, key: str, content_type: str) -> str: ...
    async def close(self) -> None: ...


class S3Host:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = None

    async def upload(self, data: bytes, key: str, content_type: str) -> str:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import (
            BotoCoreError,
            ClientError,
            ConnectionClosedError,
            ConnectTimeoutError,
            EndpointConnectionError,
            ReadTimeoutError,
        )

        def put():
            if self.client is None:
                self.client = boto3.client(
                    "s3",
                    endpoint_url=self.settings.endpoint,
                    aws_access_key_id=self.settings.access_key,
                    aws_secret_access_key=self.settings.secret_key,
                    region_name=self.settings.region,
                    config=Config(
                        signature_version="s3v4",
                        s3={"addressing_style": self.settings.addressing_style},
                        retries={"total_max_attempts": 1},
                        connect_timeout=self.settings.request_timeout,
                        read_timeout=self.settings.request_timeout,
                        request_checksum_calculation="when_required",
                        response_checksum_validation="when_required",
                    ),
                )
            # No ACL: R2 does not implement x-amz-acl. Public access is bucket/domain configuration.
            self.client.put_object(
                Bucket=self.settings.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                CacheControl="public, max-age=31536000, immutable",
            )

        try:
            await asyncio.to_thread(put)
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            code = exc.response.get("Error", {}).get("Code", "")
            retryable = (
                status == 429
                or status >= 500
                or code in {"SlowDown", "RequestTimeout", "InternalError"}
            )
            raise UploadError(
                f"对象存储上传失败（HTTP {status}）；请检查桶、凭据或服务状态。",
                retryable,
            ) from None
        except (
            EndpointConnectionError,
            ConnectTimeoutError,
            ReadTimeoutError,
            ConnectionClosedError,
        ):
            raise UploadError("对象存储连接失败，请检查接口地址及网络。", True) from None
        except BotoCoreError:
            raise UploadError("对象存储请求配置无效，请检查接口、区域、凭据和桶名。") from None
        return https_url(f"{self.settings.public_base_url.rstrip('/')}/{quote(key, safe='/')}")

    async def close(self):
        if self.client is not None:
            await asyncio.to_thread(self.client.close)


def json_path(payload, path: str):
    """Read dot-separated object keys or array indices without evaluating expressions."""
    value = payload
    for part in path.split("."):
        if isinstance(value, list):
            value = value[int(part)]
        else:
            value = value[part]
    return value


async def response_json(response):
    """Read a complete, bounded response even when HTTP delivers JSON in small chunks."""
    body = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        body.extend(chunk)
        if len(body) > 1_048_576:
            raise ValueError("JSON response exceeds 1 MB")
    return json.loads(body)


class HttpHost:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = None

    async def upload(self, data: bytes, key: str, content_type: str) -> str:
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout)
            )
        fields = dict(self.settings.upload_fields)
        if self.settings.upload_mode == "multipart":
            form = aiohttp.FormData()
            for k, v in fields.items():
                form.add_field(str(k), str(v))
            form.add_field(
                self.settings.file_field,
                data,
                filename=Path(key).name,
                content_type=content_type,
            )
            kwargs = {"data": form}
        else:
            fields[self.settings.file_field] = base64.b64encode(data).decode("ascii")
            kwargs = {"json": fields}
        try:
            async with self.session.post(
                self.settings.upload_url,
                headers=self.settings.upload_headers,
                allow_redirects=False,
                **kwargs,
            ) as response:
                if response.status not in range(200, 300):
                    raise UploadError(
                        f"图床上传返回 HTTP {response.status}，请检查接口及凭据。",
                        response.status == 429 or response.status >= 500,
                    )
                payload = await response_json(response)
                if self.settings.success_path:
                    actual = json_path(payload, self.settings.success_path)
                    expected = self.settings.success_value
                    if type(actual) is not type(expected) or actual != expected:
                        raise UploadError("图床业务状态表示上传失败，请检查图床后台记录。")
                url = json_path(payload, self.settings.response_url_path)
                if not isinstance(url, str):
                    raise ValueError("URL is not a string")
                return https_url(url)
        except (aiohttp.ClientError, TimeoutError):
            raise UploadError("图床连接超时或中断。", True) from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise UploadError("图床没有返回配置的图片直链，请检查 JSON 响应路径。") from None

    async def close(self):
        if self.session is not None:
            await self.session.close()


class ImagePublisher:
    def __init__(self, settings: Settings, db: Database, host: ImageHost | None = None):
        self.settings = settings
        self.db = db
        self.host = host or (S3Host(settings) if settings.provider == "s3" else HttpHost(settings))
        # Credentials only participate in this one-way cache fingerprint and are never logged.
        config = asdict(settings)
        self.namespace = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        self.lock = asyncio.Lock()

    async def publish(self, path: Path, force: bool = False, deadline: float | None = None) -> str:
        self.settings.check_host()
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError:
            raise PiggyError("本地图片读取失败，请检查素材目录。") from None
        if not 0 < len(data) <= 10 * 1024 * 1024:
            raise PiggyError("图片为空或超过 10 MB。")
        digest = hashlib.sha256(data).hexdigest()
        async with self.lock:
            cached = await self.db.cache_get(self.namespace, digest)
            if (
                cached
                and not force
                and time.time() - cached["uploaded_at"] < self.settings.cache_ttl_hours * 3600
            ):
                return cached["url"]
            key = f"{self.settings.key_prefix.strip('/')}/{digest}{path.suffix.lower()}"
            content_type = mimetypes.guess_type(path.name)[0] or "image/png"
            for attempt in range(self.settings.upload_retry_count + 1):
                if deadline is not None and time.monotonic() >= deadline:
                    raise PiggyError("图片上传超过本次回复时限，请再次发送指令。")
                try:
                    url = await self.host.upload(data, key, content_type)
                    https_url(url)
                    await self.db.cache_put(self.namespace, digest, key, url)
                    return url
                except UploadError as exc:
                    if not exc.retryable or attempt == self.settings.upload_retry_count:
                        raise
                    delay = self.settings.delay(attempt)
                    if deadline is not None and time.monotonic() + delay >= deadline:
                        raise PiggyError("图片上传超过本次回复时限，请再次发送指令。") from None
                    await asyncio.sleep(delay)
        raise AssertionError("Unreachable upload state")

    async def close(self):
        await self.host.close()
