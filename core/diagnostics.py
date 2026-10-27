import re
import traceback
from pathlib import Path

from .config import Settings


def safe_detail(value: object, settings: Settings) -> str:
    """保留有用的 SDK 错误原因，同时不暴露配置的密钥或签名链接。"""
    text = str(value)
    secrets = [settings.access_key, settings.secret_key]
    secrets.extend(str(v) for v in settings.upload_headers.values())
    secrets.extend(str(v) for v in settings.upload_fields.values() if isinstance(v, str))
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[redacted]")
    # SDK 错误中可能包含代理认证信息或完整的预签名链接。
    text = re.sub(r"(https?://)[^\s/]+@", r"\1[redacted]@", text)
    text = re.sub(r"(https?://[^\s?\"'<>]+)\?[^\s\"'<>]*", r"\1?[redacted]", text)
    text = re.sub(
        r"(?i)\b(authorization|x-amz-security-token|x-amz-signature|aws_session_token)"
        r"\s*[:=]\s*[^\r\n]+",
        r"\1=[redacted]",
        text,
    )
    return " ".join(text.split())[:1600]


def exception_detail(exc: Exception, settings: Settings) -> str:
    frames = traceback.extract_tb(exc.__traceback__)[-6:]
    location = " > ".join(f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in frames)
    return safe_detail(f"exception={type(exc).__name__} reason={exc} location={location}", settings)
