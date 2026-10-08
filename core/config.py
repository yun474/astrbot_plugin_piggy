import json
from copy import deepcopy
from dataclasses import dataclass, field
from urllib.parse import urlsplit

S3_PROVIDERS = {
    "Cloudflare R2": ("r2", "auto", "path", "s3v4"),
    "AWS S3": ("aws", "", "virtual", "s3v4"),
    "阿里云 OSS": ("oss", "us-east-1", "virtual", "s3"),
    "腾讯云 COS": ("cos", "", "virtual", "s3v4"),
    "七牛云 Kodo": ("qiniu", "", "virtual", "s3v4"),
    "MinIO": ("minio", "us-east-1", "path", "s3v4"),
    "Backblaze B2": ("b2", "", "path", "s3v4"),
    "DigitalOcean Spaces": ("spaces", "", "virtual", "s3v4"),
    "其他 S3 兼容存储": ("s3", "us-east-1", "path", "s3v4"),
}
S3_FIELDS = (
    "endpoint",
    "bucket",
    "access_key",
    "secret_key",
    "region",
    "public_base_url",
    "addressing_style",
)
HTTP_FIELDS = (
    "upload_url",
    "upload_mode",
    "upload_headers",
    "upload_fields",
    "file_field",
    "response_url_path",
    "success_path",
    "success_value",
)


def migrate_host_config(data: dict) -> bool:
    """Preserve both legacy backends once, including schema-filled defaults."""
    if data.get("image_host_migrated", False):
        return False
    host = data.setdefault("image_host", {})
    if not isinstance(host, dict):
        raise PiggyError("图床配置必须为对象。")
    defaults = Settings()
    legacy_values = {
        key: data[key] for key in (*S3_FIELDS, *HTTP_FIELDS, "provider") if key in data
    }
    for key in ("upload_headers", "upload_fields", "success_value"):
        if isinstance(legacy_values.get(key), str):
            try:
                legacy_values[key] = json.loads(legacy_values[key])
            except ValueError:
                pass  # Preserve malformed legacy input for normal settings validation.
    legacy = any(value != getattr(defaults, key) for key, value in legacy_values.items())
    if legacy:
        for section, fields in (("s3", S3_FIELDS), ("http", HTTP_FIELDS)):
            target = host.setdefault(section, {})
            if not isinstance(target, dict):
                raise PiggyError(f"图床 {section} 配置必须为对象。")
            section_defaults = {key: getattr(defaults, key) for key in fields}
            if section == "s3":
                section_defaults.update(region="us-east-1", addressing_style="auto")
            else:
                section_defaults.update(
                    upload_headers="{}", upload_fields="{}", success_value="true"
                )
            if all(target.get(key, value) == value for key, value in section_defaults.items()):
                target.update({key: data.get(key, getattr(defaults, key)) for key in fields})
                if section == "http":
                    for key in ("upload_headers", "upload_fields", "success_value"):
                        if not isinstance(target[key], str):
                            target[key] = json.dumps(target[key], ensure_ascii=False)
        # A previously saved new provider choice takes precedence over old fields.
        r2_defaults = {key: getattr(defaults, key) for key in S3_FIELDS}
        r2_defaults["addressing_style"] = "auto"
        r2 = host.get("r2", {})
        if not isinstance(r2, dict):
            raise PiggyError("图床 r2 配置必须为对象。")
        if host.get("provider", "Cloudflare R2") == "Cloudflare R2" and all(
            r2.get(key, value) == value for key, value in r2_defaults.items()
        ):
            old_provider = data.get("provider", "s3")
            host["provider"] = {"s3": "其他 S3 兼容存储", "http": "自定义 HTTP"}.get(
                old_provider, old_provider
            )
    for key in (*S3_FIELDS, *HTTP_FIELDS, "provider"):
        data.pop(key, None)
    data["image_host_migrated"] = True
    return True


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
    duplicate_rate_cap: int = 20
    duplicate_pity: int = 2
    display: dict = field(
        default_factory=lambda: {
            "draw": True,
            "atlas": False,
            "pen": False,
            "ranking": False,
            "shop": False,
            "duel": False,
            "wild": False,
            "raid": False,
        }
    )
    temp_cache_hours: int = 12
    provider: str = "s3"
    endpoint: str = ""
    bucket: str = ""
    access_key: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    region: str = "auto"
    public_base_url: str = ""
    addressing_style: str = "path"
    signature_version: str = "s3v4"
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
    command_prefix: str = ""
    battle_level_cap: int = 20
    duel_daily_limit: int = 5
    request_ttl_minutes: int = 10
    battle_markdown: bool = False
    draw_gather_chance: int = 50
    draw_chain_chance: int = 50
    wild_level_max: int = 20

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        values = {key: data[key] for key in cls.__dataclass_fields__ if key in data}
        if "image_host" in data:
            data = deepcopy(dict(data))
            migrate_host_config(data)
            host = data["image_host"]
            if not isinstance(host, dict):
                raise PiggyError("图床配置必须为对象。")
            provider = host.get("provider", "Cloudflare R2")
            if provider in S3_PROVIDERS:
                section, region, style, signature = S3_PROVIDERS[provider]
                fields = S3_FIELDS
            elif provider in {"自定义 HTTP", "兰空 Lsky Pro V2"}:
                section = "http" if provider == "自定义 HTTP" else "lsky"
                fields = HTTP_FIELDS if section == "http" else ("upload_url",)
            else:
                raise PiggyError("请选择有效的图床类型。")
            selected = host.get(section, {})
            if not isinstance(selected, dict):
                raise PiggyError(f"图床 {section} 配置必须为对象。")
            # Never leak credentials or options from an inactive backend.
            for key in (*S3_FIELDS, *HTTP_FIELDS, "signature_version"):
                values.pop(key, None)
            values.update({key: selected[key] for key in fields if key in selected})
            values["provider"] = "s3" if provider in S3_PROVIDERS else "http"
            if values["provider"] == "s3":
                configured_region = selected.get("region", "")
                values["region"] = (
                    configured_region.strip() or region
                    if isinstance(configured_region, str)
                    else configured_region
                )
                configured_style = selected.get("addressing_style", "auto")
                values["addressing_style"] = (
                    style if configured_style == "auto" or section == "oss" else configured_style
                )
                values["signature_version"] = signature
            elif section == "lsky":
                authorization = selected.get("authorization", "")
                values.update(
                    upload_headers={"Authorization": authorization} if authorization else {},
                    success_path="status",
                    success_value=True,
                )
        for key in ("endpoint", "bucket", "access_key", "secret_key", "region", "public_base_url"):
            if isinstance(values.get(key), str):
                values[key] = values[key].strip()
        for key in ("upload_headers", "upload_fields", "success_value"):
            if isinstance(values.get(key), str):
                try:
                    values[key] = json.loads(values[key])
                except ValueError:
                    raise PiggyError(f"配置 {key} 必须是合法 JSON。") from None
        obj = cls(**values)
        if not isinstance(obj.display, dict) or any(
            key not in {"draw", "atlas", "pen", "ranking", "shop", "duel", "wild", "raid"}
            or type(value) is not bool
            for key, value in obj.display.items()
        ):
            raise PiggyError("消息展示配置必须是各功能的布尔开关。")
        for name, definition in cls.__dataclass_fields__.items():
            if definition.type is str and not isinstance(getattr(obj, name), str):
                raise PiggyError(f"配置 {name} 必须为字符串。")
        for key, low, high in (
            ("duplicate_rate_cap", 0, 100),
            ("duplicate_pity", 0, 30),
            ("upload_retry_count", 0, 10),
            ("image_retry_count", 0, 10),
            ("backup_keep", 1, 30),
            ("cache_ttl_hours", 1, 8760),
            ("temp_cache_hours", 1, 24),
            ("battle_level_cap", 5, 100),
            ("duel_daily_limit", 1, 100),
            ("request_ttl_minutes", 1, 60),
            ("draw_gather_chance", 0, 90),
            ("draw_chain_chance", 0, 90),
            ("wild_level_max", 1, 100),
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
        if type(obj.battle_markdown) is not bool:
            raise PiggyError("配置 battle_markdown 必须为布尔值。")
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
        if any(c in obj.command_prefix for c in "\r\n") or len(obj.command_prefix) > 64:
            raise PiggyError("快捷指令唤醒词长度不能超过 64，且不能包含换行。")
        return obj

    def use_host(self, command: str) -> bool:
        return self.display.get(command, command == "draw")

    def check_host(self) -> None:
        if self.provider == "s3":
            missing = [
                key
                for key in (
                    "endpoint",
                    "bucket",
                    "access_key",
                    "secret_key",
                    "public_base_url",
                    "region",
                )
                if not getattr(self, key)
            ]
            if missing:
                raise PiggyError(f"S3/R2 配置缺少：{', '.join(missing)}。请在插件配置中填写。")
            https_url(self.endpoint)
            https_url(self.public_base_url)
            if urlsplit(self.endpoint).query:
                raise PiggyError("endpoint 必须是 S3 API 地址，不能包含查询参数或签名链接。")
            if any(c.isspace() or c in "/\\" for c in self.bucket):
                raise PiggyError("bucket 只能填写桶名，不能填写 URL、路径或带空格的名称。")
            if urlsplit(self.public_base_url).query:
                raise PiggyError("图片公网根地址不能带查询参数。")
        else:
            if not self.upload_url or not self.file_field or not self.response_url_path:
                raise PiggyError("请配置 HTTP 图床上传地址、文件字段和直链响应路径。")
            https_url(self.upload_url)

    def delay(self, retry: int) -> float:
        return min(self.retry_base_delay * 2**retry, 15.0)
