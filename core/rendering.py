import hashlib
import json
import math
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .catalog import thumbnail

# One sheet contains the entire initial catalog. Limits keep future expansions
# below QQ's practical image size and the renderer's memory budget.
ATLAS_SHEET_SIZE = 192
PEN_SHEET_SIZE = 96
WIDTH = 1080
BG = "#fcf5ec"
TEXT = "#46392f"
SUB = "#958171"
ACCENT = "#b85969"
PANEL = "#f5e8da"
BORDER = "#ebdfd1"
TRACK = "#e8d6c6"
FONT = Path(__file__).resolve().parents[1] / "resources" / "fonts" / "NotoSansSC.ttf"


@dataclass(frozen=True)
class Card:
    path: Path
    width: int
    height: int


def render_collection(
    root: Path, name: str, progress: dict, entries: list[dict], page: int, pages: int, atlas: bool
) -> Card:
    """Render the approved cream card; atlas tiles deliberately contain no names."""
    columns = 8 if atlas else 4
    step = 122 if atlas else 248
    top = 384 if atlas else 478
    row_step = 122 if atlas else 246
    rows = max(1, math.ceil(len(entries) / columns))
    height = top + rows * row_step + 78
    signature = {
        "revision": "cream-v2",
        "name": name,
        "atlas": atlas,
        "page": page,
        "pages": pages,
        "progress": {k: v for k, v in progress.items() if k != "entries"},
        "entries": entries,
    }
    digest = hashlib.sha256(
        json.dumps(signature, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    directory = root / "cards"
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / f"{digest}.png"
    if output.exists():
        output.touch()
        return Card(output, WIDTH, height)
    image = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(image)
    fonts = {}

    def font(size, bold=False):
        key = size, bold
        if key not in fonts:
            face = ImageFont.truetype(str(FONT), size)
            face.set_variation_by_axes([700 if bold else 400])
            fonts[key] = face
        return fonts[key]

    def text(value, x, y, size=24, color=TEXT, bold=False, width=None, center=False):
        face = font(size, bold)
        value = str(value)
        if width is not None and draw.textlength(value, font=face) > width:
            while value and draw.textlength(value + "…", font=face) > width:
                value = value[:-1]
            value += "…"
        if center:
            x -= draw.textlength(value, font=face) / 2
        draw.text((x, y), value, font=face, fill=color)

    text("PIGGY  /  COLLECTION", 56, 34, 18, ACCENT, True)
    text("小猪图鉴" if atlas else "我的猪圈", 54, 74, 56, bold=True)
    text(name, 58, 157, 24, SUB, width=940)
    total = progress["active_total"]
    unlocked = progress["unlocked"]
    ratio = unlocked / total if total else 0
    if atlas:
        draw.rounded_rectangle((56, 218, 1024, 343), radius=25, fill=PANEL)
        text(f"已解锁 {unlocked} / {total}", 85, 236, 34, bold=True)
        percent = f"{ratio:.1%}"
        draw.text(
            (989 - draw.textlength(percent, font=font(30, True)), 239),
            percent,
            font=font(30, True),
            fill=ACCENT,
        )
        bar_y = 310
    else:
        draw.rounded_rectangle((56, 218, 1024, 395), radius=28, fill=PANEL)
        for x, label, value in (
            (86, "已解锁种类", f"{unlocked} / {total}"),
            (402, "累计收获", f"{progress['total']} 只"),
            (718, "收集进度", f"{ratio:.1%}"),
        ):
            text(label, x, 239, 21, SUB)
            text(value, x, 275, 38, bold=True, width=277)
        bar_y = 363
        text("收藏的小猪", 58, 425, 23, bold=True)
        text(f"共 {progress['species']} 种", 820, 429, 19, SUB, width=205)
    draw.rounded_rectangle((86, bar_y, 992, bar_y + 9), radius=4, fill=TRACK)
    if ratio > 0:
        draw.rounded_rectangle(
            (86, bar_y, 86 + max(9, int(906 * min(ratio, 1))), bar_y + 9), radius=4, fill=ACCENT
        )

    for index, pig in enumerate(entries):
        x = 56 + (index % columns) * step
        y = top + (index // columns) * row_step
        locked = atlas and not pig["count"]
        if atlas:
            draw.rounded_rectangle(
                (x, y, x + 114, y + 110), radius=18, fill="#ececec" if locked else "#ffffff"
            )
            box_w, box_h = 96, 94
            cx, cy = x + 57, y + 55
        else:
            draw.rounded_rectangle((x, y + 3, x + 224, y + 230), radius=23, fill=BORDER)
            draw.rounded_rectangle((x, y, x + 224, y + 227), radius=23, fill="#ffffff")
            draw.rounded_rectangle((x + 12, y + 12, x + 212, y + 154), radius=17, fill="#f9f4ed")
            box_w, box_h = 160, 128
            cx, cy = x + 112, y + 83
        path = thumbnail(root / "assets" / pig["asset"], root / "thumbnails", locked)
        with Image.open(path) as original:
            art = original.convert("RGBA")
            art.thumbnail((box_w, box_h), Image.Resampling.LANCZOS)
            if locked:
                art.putalpha(art.getchannel("A").point(lambda a: int(a * 0.60)))
            image.paste(art, (int(cx - art.width / 2), int(cy - art.height / 2)), art)
        if not atlas:
            text(pig["name"], cx, y + 163, 23, bold=True, width=198, center=True)
            count = f"× {pig['count']}" + (" · 已下架" if not pig["enabled"] else "")
            text(count, cx, y + 197, 16, ACCENT, width=200, center=True)
    if not entries:
        text(
            "猪圈还是空的，先抽一只今日小猪吧。" if not atlas else "暂无可展示的小猪",
            WIDTH / 2,
            top + 60,
            26,
            SUB,
            center=True,
        )
    footer = top + rows * row_step + 12
    draw.line((56, footer, 1024, footer), fill=BORDER, width=2)
    if atlas:
        text("彩色 · 已解锁    灰色 · 未解锁", 58, footer + 19, 17, SUB)
    else:
        text("历史收藏会保留", 58, footer + 19, 17, SUB)
    text(f"{page:02d} / {pages:02d}", 888, footer + 16, 21, bold=True)
    with tempfile.NamedTemporaryFile(dir=directory, suffix=".tmp", delete=False) as file:
        temp = Path(file.name)
    try:
        image.save(temp, "PNG")
        temp.replace(output)
    finally:
        image.close()
        temp.unlink(missing_ok=True)
    return Card(output, WIDTH, height)


def clean_cards(root: Path):
    """Discard only reproducible UI images that have not been used for a week."""
    threshold = time.time() - 7 * 86400
    for path in (root / "cards").glob("*.png"):
        if path.stat().st_mtime < threshold:
            path.unlink(missing_ok=True)
