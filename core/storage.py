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
from .diagnostics import safe_detail


class UploadError(PiggyError):
    def __init__(self, message: str, retryable: bool = False, *, diagnostic: str = ""):
        super().__init__(message)
        self.retryable = retryable
        self.diagnostic = diagnostic
        self.attempt = 1


class ImageHost(Protocol):
    async def upload(self, data: bytes, key: str, content_type: str) -> str: ...
    async def close(self) -> None: ...


class S3Host:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = None

    async def upload(self, data: bytes, key: str, content_type: str) -> str:
        import boto3
        import botocore
        from botocore.config import Config
        from botocore.exceptions import (
            BotoCoreError,
            ClientError,
            ConnectionClosedError,
            ConnectTimeoutError,
            EndpointConnectionError,
            HTTPClientError,
            NoCredentialsError,
            ParamValidationError,
            PartialCredentialsError,
            ProxyConnectionError,
            ReadTimeoutError,
            SSLError,
        )

        stage = "create_client"

        def failure(exc, message, retryable=False, extra=""):
            detail = safe_detail(
                f"provider=s3 stage={stage} exception={type(exc).__name__} "
                f"boto3={boto3.__version__} botocore={botocore.__version__} "
                f"region={self.settings.region} addressing_style={self.settings.addressing_style} "
                f"{extra} reason={exc} cause={getattr(exc, 'kwargs', {}).get('error', '')}",
                self.settings,
            )
            return UploadError(message, retryable, diagnostic=detail)

        def put():
            nonlocal stage
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
            stage = "put_object"
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
            hints = {
                "AccessDenied": "服务拒绝写入，请检查此凭据是否有目标桶的对象写入权限。",
                "InvalidAccessKeyId": "服务不认可 Access Key ID，请核对 R2 的 S3 凭据与账户接口是否匹配。",
                "SignatureDoesNotMatch": "请求签名不匹配，请核对 Secret Access Key、区域和服务器时间。",
                "NoSuchBucket": "服务找不到该桶，请检查桶名和账户接口是否匹配。",
                "RequestTimeTooSkewed": "服务器时间偏差过大，请同步系统时间。",
                "ExpiredToken": "临时凭据已过期，请更新凭据。",
                "InvalidToken": "服务拒绝凭据令牌，请检查凭据类型。",
                "AuthorizationHeaderMalformed": "签名区域或认证格式不符，请核对区域与接口地址。",
                "NotImplemented": "服务不支持本次上传参数，请查看日志中的 SDK 原因。",
            }
            hint = hints.get(
                code, "服务暂时不可用，将按配置重试。" if retryable else "请查看后台详细原因。"
            )
            request_id = exc.response.get("ResponseMetadata", {}).get("RequestId", "")
            raise failure(
                exc,
                f"对象存储上传失败（HTTP {status}，{safe_detail(code, self.settings)}）。{hint}",
                retryable,
                f"http_status={status} code={code} request_id={request_id}",
            ) from None
        except SSLError as exc:
            certificate_error = (
                "CERTIFICATE_VERIFY_FAILED" in str(exc)
                or "certificate verify failed" in str(exc).lower()
            )
            raise failure(
                exc,
                "对象存储 TLS/SSL 握手或证书校验失败，请检查容器 CA 证书、系统时间和 HTTPS 代理；详细原因见日志。",
                not certificate_error,
            ) from None
        except ProxyConnectionError as exc:
            raise failure(
                exc,
                "对象存储代理连接失败，请检查 AstrBot 进程的 HTTP_PROXY/HTTPS_PROXY 和代理可达性。",
                True,
            ) from None
        except ParamValidationError as exc:
            raise failure(
                exc, "对象存储 SDK 参数校验失败，请查看日志中的具体字段；尚未完成上传。"
            ) from None
        except (NoCredentialsError, PartialCredentialsError) as exc:
            raise failure(
                exc, "对象存储 SDK 未取得完整凭据，请填写 Access Key ID 和 Secret Access Key。"
            ) from None
        except (
            EndpointConnectionError,
            ConnectTimeoutError,
            ReadTimeoutError,
            ConnectionClosedError,
        ) as exc:
            raise failure(
                exc,
                f"对象存储网络请求失败（{type(exc).__name__}），请检查 DNS、接口可达性及超时设置。",
                True,
            ) from None
        except HTTPClientError as exc:
            raise failure(
                exc, "对象存储 HTTP 客户端异常，请查看日志中的底层网络或 SDK 原因。", True
            ) from None
        except (BotoCoreError, ValueError, TypeError) as exc:
            raise failure(
                exc, f"对象存储 SDK 请求失败（{type(exc).__name__}），具体参数或依赖原因见日志。"
            ) from None
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
                    exc.attempt = attempt + 1
                    if not exc.retryable or attempt == self.settings.upload_retry_count:
                        raise
                    delay = self.settings.delay(attempt)
                    if deadline is not None and time.monotonic() + delay >= deadline:
                        raise PiggyError("图片上传超过本次回复时限，请再次发送指令。") from None
                    await asyncio.sleep(delay)
        raise AssertionError("Unreachable upload state")

    async def close(self):
        await self.host.close()
