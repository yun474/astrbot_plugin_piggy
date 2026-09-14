import hashlib
import json
import re
import shutil
from pathlib import Path

from PIL import Image, ImageOps

from .config import PiggyError


def initialize_catalog(data_dir: Path, resources: Path) -> None:
    target = data_dir / "catalog"
    target.mkdir(parents=True, exist_ok=True)
    if not (target / "pigs.json").exists():
        # The manifest is the completion marker, copied only after the images.
        shutil.copytree(resources / "images", target / "images", dirs_exist_ok=True)
        temp = target / "pigs.json.tmp"
        shutil.copyfile(resources / "pigs.json", temp)
        temp.replace(target / "pigs.json")


def read_catalog(data_dir: Path) -> list[dict]:
    root = (data_dir / "catalog").resolve()
    try:
        definitions = json.loads((root / "pigs.json").read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise PiggyError("猪库 JSON 无法读取，原有收藏和有效猪库均已保留。") from exc
    if not isinstance(definitions, list) or not definitions:
        raise PiggyError("猪库必须是非空列表。")
    archive = data_dir / "assets"
    archive.mkdir(parents=True, exist_ok=True)
    ids = set()
    validated = []
    for index, item in enumerate(definitions):
        if not isinstance(item, dict):
            raise PiggyError(f"猪库第 {index + 1} 项必须为对象。")
        pig_id = item.get("id", "")
        if not isinstance(pig_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", pig_id):
            raise PiggyError(f"猪库第 {index + 1} 项 ID 不合法。")
        if pig_id in ids:
            raise PiggyError(f"猪库存在重复 ID：{pig_id}")
        ids.add(pig_id)
        for key, limit in (("name", 40), ("description", 160), ("analysis", 600)):
            if not isinstance(item.get(key), str) or not 1 <= len(item[key].strip()) <= limit:
                raise PiggyError(f"{pig_id} 的 {key} 必须是 1–{limit} 字的文本。")
        enabled = item.get("enabled", True)
        order = item.get("sort_order", index)
        if type(enabled) is not bool or type(order) is not int:
            raise PiggyError(f"{pig_id} 的 enabled/sort_order 类型错误。")
        image_name = item.get("image", f"images/{pig_id}.png")
        if not isinstance(image_name, str):
            raise PiggyError(f"{pig_id} 的图片路径必须为字符串。")
        path = (root / image_name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise PiggyError(f"{pig_id} 图片不存在或超出猪库目录。")
        try:
            if path.stat().st_size > 10 * 1024 * 1024:
                raise ValueError("Image too large")
            with Image.open(path) as img:
                if img.width * img.height > 16_000_000:
                    raise ValueError("Image dimensions too large")
                img.verify()
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            suffix = path.suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
                raise ValueError("Unsupported image format")
            asset_name = f"{digest}{suffix}"
            dest = archive / asset_name
            if not dest.exists():
                temp = dest.with_suffix(dest.suffix + ".tmp")
                temp.write_bytes(raw)
                temp.replace(dest)
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise PiggyError(f"{pig_id} 图片无法解析或超过限制。") from exc
        validated.append(
            {
                "id": pig_id,
                "name": item["name"].strip(),
                "description": item["description"].strip(),
                "analysis": item["analysis"].strip(),
                "asset": asset_name,
                "enabled": enabled,
                "sort_order": order,
            }
        )
    if not any(p["enabled"] for p in validated):
        raise PiggyError("至少保留一只启用的小猪；本次重载未生效。")
    return validated


def thumbnail(source: Path, output_dir: Path, gray: bool) -> Path:
    """Prepare a reusable image asset; final card layout is intentionally separate."""
    output_dir.mkdir(parents=True, exist_ok=True)
    name = f"{source.stem}-{'gray' if gray else 'color'}-192-v1.png"
    output = output_dir / name
    if output.exists():
        return output
    with Image.open(source) as original:
        img = ImageOps.exif_transpose(original).convert("RGBA")
        img.thumbnail((192, 192), Image.Resampling.LANCZOS)
        if gray:
            alpha = img.getchannel("A")
            img = ImageOps.grayscale(img).convert("RGBA")
            img.putalpha(alpha)
        # Unique temporary name avoids collisions between independent workers.
        import tempfile

        with tempfile.NamedTemporaryFile(dir=output_dir, suffix=".png", delete=False) as f:
            temp = Path(f.name)
        try:
            img.save(temp, "PNG")
            temp.replace(output)
        finally:
            temp.unlink(missing_ok=True)
    return output
