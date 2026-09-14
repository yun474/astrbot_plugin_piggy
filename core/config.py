import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit


class PiggyError(Exception):
    """An actionable error safe to show to the user."""


def https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or any(c in value for c in "\r\n <>()[]{}\\")
    ):
        raise PiggyError("请配置有效的 HTTPS 图片/接口地址，不能包含登录信息或 Markdown 控制字符。")
    return value


@dataclass(frozen=True)
class Settings:
    provider: str = "s3"
    endpoint: str = ""
    bucket: str = ""
    access_key: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    region: str = "auto"
    public_base_url: str = ""
    key_prefix: str = "piggy"
    addressing_style: str = "path"
    upload_url: str = ""
    upload_mode: str = "multipart"
    upload_headers: dict = field(default_factory=dict, repr=False)
    upload_fields: dict = field(default_factory=dict, repr=False)
    file_field: str = "file"
    response_url_path: str = "data.links.url"
    success_path: str = ""
    success_value: object = True
    upload_retry_count: int = 2
    image_retry_count: int = 3
    retry_base_delay: float = 1.0
    request_timeout: float = 20.0
    backup_keep: int = 7
    cache_ttl_hours: int = 168
    command_prefix: str = "/"

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        values = {key: data[key] for key in cls.__dataclass_fields__ if key in data}
        for key in ("upload_headers", "upload_fields", "success_value"):
            if isinstance(values.get(key), str):
                try:
                    values[key] = json.loads(values[key])
                except ValueError:
                    raise PiggyError(f"配置 {key} 必须是合法 JSON。") from None
        obj = cls(**values)
        for name, definition in cls.__dataclass_fields__.items():
            if definition.type is str and not isinstance(getattr(obj, name), str):
                raise PiggyError(f"配置 {name} 必须为字符串。")
        for key, low, high in (
            ("upload_retry_count", 0, 10),
            ("image_retry_count", 0, 10),
            ("backup_keep", 1, 30),
            ("cache_ttl_hours", 1, 8760),
        ):
            value = getattr(obj, key)
            if type(value) is not int or not low <= value <= high:
                raise PiggyError(f"配置 {key} 必须是 {low}–{high} 的整数。")
        for key, low, high in (
            ("retry_base_delay", 0.1, 10),
            ("request_timeout", 1, 60),
        ):
            value = getattr(obj, key)
            if type(value) not in (int, float) or not low <= value <= high:
                raise PiggyError(f"配置 {key} 必须在 {low}–{high} 之间。")
        if obj.provider not in {"s3", "http"}:
            raise PiggyError("图床 provider 仅支持 s3 或 http。")
        if obj.upload_mode not in {"multipart", "json_base64"}:
            raise PiggyError("HTTP 上传模式只能为 multipart 或 json_base64。")
        if obj.addressing_style not in {"path", "virtual", "auto"}:
            raise PiggyError("S3 addressing_style 必须为 path、virtual 或 auto。")
        if not isinstance(obj.upload_headers, dict) or not isinstance(obj.upload_fields, dict):
            raise PiggyError("上传请求头和表单字段必须为 JSON 对象。")
        if any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in obj.upload_headers.items()
        ):
            raise PiggyError("上传请求头必须为字符串键值对。")
        if any(not isinstance(v, (str, int, float, bool)) for v in obj.upload_fields.values()):
            raise PiggyError("上传表单字段只能填写字符串或数值。")
        if any(c in obj.command_prefix for c in "\r\n") or len(obj.command_prefix) > 8:
            raise PiggyError("指令前缀长度不能超过 8，且不能包含换行。")
        parts = obj.key_prefix.strip("/").split("/")
        if any(p in {"", ".", ".."} for p in parts):
            raise PiggyError("对象存储目录前缀不合法。")
        return obj

    def check_host(self) -> None:
        if self.provider == "s3":
            if not all(
                (
                    self.endpoint,
                    self.bucket,
                    self.access_key,
                    self.secret_key,
                    self.public_base_url,
                )
            ):
                raise PiggyError("请先配置 S3/R2 上传接口、桶名、凭据和图片公网域名。")
            https_url(self.endpoint)
            https_url(self.public_base_url)
            if urlsplit(self.public_base_url).query:
                raise PiggyError("图片公网根地址不能带查询参数。")
        else:
            if not self.upload_url or not self.file_field or not self.response_url_path:
                raise PiggyError("请配置 HTTP 图床上传地址、文件字段和直链响应路径。")
            https_url(self.upload_url)

    def delay(self, retry: int) -> float:
        return min(self.retry_base_delay * 2**retry, 15.0)
