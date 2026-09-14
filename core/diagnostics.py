import re
import traceback
from pathlib import Path

from .config import Settings


def safe_detail(value: object, settings: Settings) -> str:
    """Keep useful SDK reasons without exposing configured secrets or signed URLs."""
    text = str(value)
    secrets = [settings.access_key, settings.secret_key]
    secrets.extend(str(v) for v in settings.upload_headers.values())
    secrets.extend(str(v) for v in settings.upload_fields.values() if isinstance(v, str))
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[redacted]")
    # SDK errors may embed proxy authentication or complete presigned URLs.
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
