import hashlib
import io
import lzma
import math
import re
import shutil
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

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
FONT = Path(__file__).resolve().parents[1] / "resources" / "fonts" / "NotoSansSC.ttf.xz"
_FONT_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def _font_digest() -> str:
    return hashlib.sha256(FONT.read_bytes()).hexdigest()[:16]


def font_path(root: Path) -> Path:
    """Expand the bundled font once into plugin data, preserving all glyphs and weights."""
    path = root / "fonts" / f"NotoSansSC-{_font_digest()}.ttf"
    with _FONT_LOCK:
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            try:
                with lzma.open(FONT, "rb") as source, temp.open("wb") as output:
                    shutil.copyfileobj(source, output)
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)
    return path


@dataclass(frozen=True)
class Card:
    data: bytes
    width: int
    height: int


def render_collection(
    root: Path,
    name: str,
    progress: dict,
    entries: list[dict],
    page: int,
    pages: int,
    atlas: bool,
    level_cap: int = 20,
) -> Card:
    """Render the approved cream card; atlas tiles deliberately contain no names."""
    columns = 8 if atlas else 4
    step = 122 if atlas else 248
    top = 384 if atlas else 478
    row_step = 122 if atlas else 246
    rows = max(1, math.ceil(len(entries) / columns))
    height = top + rows * row_step + 78
    image = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(image)
    fonts = {}
    font_file = font_path(root)

    def font(size, bold=False):
        key = size, bold
        if key not in fonts:
            face = ImageFont.truetype(str(font_file), size)
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
                (x, y, x + 114, y + 110), radius=18, fill="#e5e5e5" if locked else "#ffffff"
            )
            box_w, box_h = 96, 94
            cx, cy = x + 57, y + 55
            if locked:
                # Do not open or draw the hidden asset, including its silhouette or embedded text.
                face = font(62, True)
                draw.text((cx, cy - 1), "?", font=face, fill="#565656", anchor="mm")
                continue
        else:
            draw.rounded_rectangle((x, y + 3, x + 224, y + 230), radius=23, fill=BORDER)
            draw.rounded_rectangle((x, y, x + 224, y + 227), radius=23, fill="#ffffff")
            draw.rounded_rectangle((x + 12, y + 12, x + 212, y + 154), radius=17, fill="#f9f4ed")
            box_w, box_h = 160, 128
            cx, cy = x + 112, y + 83
        with Image.open(root / "assets" / pig["asset"]) as original:
            art = ImageOps.exif_transpose(original).convert("RGBA")
            art.thumbnail((box_w, box_h), Image.Resampling.LANCZOS)
            image.paste(art, (int(cx - art.width / 2), int(cy - art.height / 2)), art)
        if not atlas:
            text(pig["name"], cx, y + 163, 23, bold=True, width=198, center=True)
            count = f"× {pig['count']} · Lv{min(pig['count'], level_cap)}" + (
                " · 已下架" if not pig["enabled"] else ""
            )
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
        text("彩色 · 已解锁    问号 · 待发现", 58, footer + 19, 17, SUB)
    else:
        text("同种猪越多等级越高 · 发送「小猪玩法」和群友斗猪、换猪", 58, footer + 19, 17, ACCENT)
    text(f"{page:02d} / {pages:02d}", 888, footer + 16, 21, bold=True)
    return finish(image)


def finish(image: Image.Image) -> Card:
    with image, io.BytesIO() as output:
        image.save(output, "PNG")
        return Card(output.getvalue(), image.width, image.height)


class Canvas:
    """Small shared drawing helpers for the two new cream cards."""

    def __init__(self, root: Path, width: int, height: int):
        self.image = Image.new("RGB", (width, height), BG)
        self.draw = ImageDraw.Draw(self.image)
        self.fonts = {}
        self.font_file = font_path(root)

    def font(self, size, bold=False):
        if (size, bold) not in self.fonts:
            face = ImageFont.truetype(str(self.font_file), size)
            face.set_variation_by_axes([700 if bold else 400])
            self.fonts[size, bold] = face
        return self.fonts[size, bold]

    def text(self, text, x, y, size=26, color=TEXT, bold=False, width=None):
        text = str(text)
        face = self.font(size, bold)
        if width is not None and self.draw.textlength(text, font=face) > width:
            while text and self.draw.textlength(text + "…", font=face) > width:
                text = text[:-1]
            text += "…"
        self.draw.text((x, y), text, font=face, fill=color)

    def wrap(self, text, size, width):
        lines = []
        for paragraph in str(text).split("\n"):
            line = ""
            for char in paragraph:
                if line and self.draw.textlength(line + char, font=self.font(size)) > width:
                    lines.append(line)
                    line = ""
                line += char
            lines.append(line)
        return lines


def render_today(
    root: Path,
    name: str,
    result: dict,
    progress: dict,
    state: str,
    protection: str = "",
    level: int = 1,
    *,
    title: str = "今日小猪",
    extras: list = (),
    summary: str = "",
    hint: str = "",
) -> Card:
    """The first pig in full, then a grid of every extra pig from gather/chain."""
    pig = result["pig"]
    canvas = Canvas(root, WIDTH, 1)
    description = " ".join(pig["description"].split())
    analysis = " ".join(pig["analysis"].split())
    lines = canvas.wrap(f"{description}\n\n{analysis}", 28, 880)
    canvas.image.close()
    description_y = 870
    panel_bottom = description_y + 58 + 43 * len(lines)
    columns, tile_w, tile_h = 4, 224, 230
    rows = math.ceil(len(extras) / columns)
    grid_top = panel_bottom + 40
    grid_bottom = grid_top + (70 + rows * 246 if extras else 0)
    canvas = Canvas(root, WIDTH, grid_bottom + 262 + (40 if summary else 0))
    canvas.text("PIGGY  /  DAILY", 58, 34, 18, ACCENT, True)
    canvas.text(title, 54, 76, 56, bold=True, width=760)
    canvas.text(name, 58, 158, 25, SUB, width=700)
    canvas.text(result["day"], 826, 43, 19, SUB)
    canvas.draw.rounded_rectangle((56, 222, 1024, 824), radius=32, fill="#ffffff")
    canvas.draw.rounded_rectangle((84, 244, 994, 704), radius=25, fill="#f9f4ed")
    with Image.open(root / "assets" / pig["asset"]) as original:
        art = original.convert("RGBA")
        art.thumbnail((580, 418), Image.Resampling.LANCZOS)
        canvas.image.paste(art, ((WIDTH - art.width) // 2, 265 + (418 - art.height) // 2), art)
        art.close()
    canvas.text(pig["name"], 88, 734, 43, bold=True, width=566)
    canvas.text(state, 674, 755, 22, ACCENT, width=314)
    canvas.draw.rounded_rectangle((56, description_y, 1024, panel_bottom), radius=28, fill=PANEL)
    for i, line in enumerate(lines):
        canvas.text(line, 94, description_y + 26 + i * 43, 28)
    if extras:
        canvas.text("本次还获得", 58, grid_top, 30, bold=True)
        canvas.text(f"共 {len(extras)} 只", 900, grid_top + 8, 20, SUB)
        for index, item in enumerate(extras):
            x = 56 + (index % columns) * 248
            y = grid_top + 62 + (index // columns) * 246
            canvas.draw.rounded_rectangle(
                (x, y + 3, x + tile_w, y + tile_h + 3), radius=23, fill=BORDER
            )
            canvas.draw.rounded_rectangle((x, y, x + tile_w, y + tile_h), radius=23, fill="#ffffff")
            canvas.draw.rounded_rectangle(
                (x + 12, y + 12, x + 212, y + 154), radius=17, fill="#f9f4ed"
            )
            _paste_art(canvas, root, item["pig"]["asset"], (x + 32, y + 19, 160, 128))
            badge = "聚集" if item["kind"] == "gather" else "连抽"
            canvas.draw.rounded_rectangle(
                (x + 150, y + 18, x + 206, y + 46),
                radius=14,
                fill=ACCENT if badge == "连抽" else TEXT,
            )
            canvas.text(badge, x + 158, y + 19, 18, "#ffffff", True)
            if item["new"]:
                canvas.draw.rounded_rectangle(
                    (x + 18, y + 18, x + 74, y + 46), radius=14, fill="#d9a441"
                )
                canvas.text("NEW", x + 26, y + 20, 17, "#ffffff", True)
            label = item["pig"]["name"]
            face = canvas.font(23, True)
            if canvas.draw.textlength(label, font=face) > 198:
                while label and canvas.draw.textlength(label + "…", font=face) > 198:
                    label = label[:-1]
                label += "…"
            canvas.text(
                label,
                x + 112 - canvas.draw.textlength(label, font=face) / 2,
                y + 168,
                23,
                bold=True,
            )
    y = grid_bottom + 35
    if summary:
        canvas.text(summary, 68, y, 22, ACCENT, True, width=944)
        y += 40
    for x, label, value in (
        (68, f"本猪拥有 · Lv{level}", f"{result['count']} 只"),
        (412, "累计收获", f"{progress['total']} 只"),
        (735, "已解锁", f"{progress['unlocked']} / {progress['active_total']}"),
    ):
        canvas.text(label, x, y, 21, SUB)
        canvas.text(value, x, y + 39, 35, bold=True, width=280)
    ratio = progress["unlocked"] / progress["active_total"] if progress["active_total"] else 0
    y += 111
    canvas.draw.rounded_rectangle((68, y, 1012, y + 10), radius=5, fill=TRACK)
    if ratio:
        canvas.draw.rounded_rectangle(
            (68, y, 68 + max(10, int(944 * min(ratio, 1))), y + 10), radius=5, fill=ACCENT
        )
    hint = hint or "发送「小猪玩法」和群友斗猪、换猪"
    canvas.text(f"{protection} · {hint}" if protection else hint, 68, y + 34, 19, ACCENT, width=944)
    return finish(canvas.image)


def render_wild(root: Path, wild: dict, unit: dict, describe) -> Card:
    """Today's wild pig with its stats and skills, or who tamed it."""
    tamed = wild["status"] == "defeated"
    skills = unit["skills"]
    height = 1000 + len(skills) * 44 + 120
    canvas = Canvas(root, WIDTH, height)
    canvas.text("PIGGY  /  WILD", 58, 34, 18, ACCENT, True)
    canvas.text("野生小猪出没", 54, 76, 56, bold=True)
    canvas.text(
        f"{wild['day']} · 本群今日唯一一只 · 已被挑战 {wild['attempts']} 次",
        58,
        158,
        24,
        SUB,
        width=960,
    )
    canvas.draw.rounded_rectangle(
        (56, 222, 1024, 780), radius=32, fill="#efe9e2" if tamed else "#ffffff"
    )
    canvas.draw.rounded_rectangle((84, 244, 994, 664), radius=25, fill="#f9f4ed")
    _paste_art(canvas, root, wild["pig"]["asset"], (250, 262, 580, 384), faded=tamed)
    canvas.text(wild["pig"]["name"], 88, 690, 43, bold=True, width=640)
    canvas.draw.rounded_rectangle((780, 690, 994, 746), radius=28, fill=SUB if tamed else ACCENT)
    level = f"Lv{wild['level']} · {unit['style']}"
    face = canvas.font(26, True)
    canvas.draw.text(
        (887 - canvas.draw.textlength(level, font=face) / 2, 702), level, font=face, fill="#ffffff"
    )
    stats = unit["stats"]
    numbers = [
        ("生命", stats["hp"]),
        ("攻击", stats["atk"]),
        ("防御", stats["def"]),
        ("速度", stats["spd"]),
        ("暴击", f"{stats['crit']}%"),
        ("闪避", f"{stats['dodge']}%"),
    ]
    canvas.draw.rounded_rectangle((56, 806, 1024, 920), radius=26, fill=PANEL)
    for index, (label, value) in enumerate(numbers):
        x = 86 + index * 156
        canvas.text(label, x, 822, 20, SUB)
        canvas.text(str(value), x, 856, 32, bold=True)
    canvas.text("已解锁技能", 58, 950, 26, bold=True)
    for index, skill in enumerate(skills):
        canvas.text(
            f"{skill['name']}：{describe(skill)}", 70, 996 + index * 44, 22, TEXT, width=950
        )
    footer = height - 96
    canvas.draw.line((56, footer, 1024, footer), fill=BORDER, width=2)
    if tamed:
        message = f"已被 {display(wild['victor'])} 收服 · 明天 0 点出现新的野猪"
    else:
        message = (
            "发送「挑战小猪 你的小猪」出战 · 输了会失去出战的小猪 · 赢了收服它，全群各得 1 次再抽"
        )
    canvas.text(message, 58, footer + 26, 21, ACCENT, True, width=966)
    return finish(canvas.image)


def render_shop(root: Path, name: str, shop: dict, level_cap: int) -> Card:
    """Today's shelf: five tiles with art, name and what the viewer gains."""
    items = shop["items"]
    columns, tile_w, tile_h, gap = 3, 304, 408, 28
    top = 262
    rows = max(1, math.ceil(len(items) / columns))
    canvas = Canvas(root, WIDTH, top + rows * (tile_h + gap) + 104)
    left = sum(1 for item in items if not item["buyer"])
    canvas.text("PIGGY  /  SHOP", 58, 34, 18, ACCENT, True)
    canvas.text("今日小猪商店", 54, 76, 56, bold=True)
    canvas.text(f"{name} · {shop['day']}", 58, 158, 25, SUB, width=700)
    canvas.draw.rounded_rectangle((56, 206, 1024, 240), radius=17, fill=PANEL)
    canvas.text(f"剩余 {left}/{len(items)} 件 · 每件限量 1 只，先到先得", 80, 210, 20, TEXT)
    for index, item in enumerate(items):
        x = 56 + (index % columns) * (tile_w + gap)
        y = top + (index // columns) * (tile_h + gap)
        sold = bool(item["buyer"])
        canvas.draw.rounded_rectangle(
            (x, y + 3, x + tile_w, y + tile_h + 3), radius=24, fill=BORDER
        )
        canvas.draw.rounded_rectangle(
            (x, y, x + tile_w, y + tile_h), radius=24, fill="#efe9e2" if sold else "#ffffff"
        )
        canvas.draw.rounded_rectangle(
            (x + 14, y + 14, x + tile_w - 14, y + 250), radius=18, fill="#f9f4ed"
        )
        with Image.open(root / "assets" / item["pig"]["asset"]) as original:
            art = ImageOps.exif_transpose(original).convert("RGBA")
        art.thumbnail((tile_w - 52, 214), Image.Resampling.LANCZOS)
        if sold:
            art = ImageOps.grayscale(art.convert("RGB")).convert("RGBA")
            art.putalpha(110)
        canvas.image.paste(
            art, (x + (tile_w - art.width) // 2, y + 32 + (214 - art.height) // 2), art
        )
        art.close()
        canvas.draw.ellipse((x + 22, y + 22, x + 66, y + 66), fill=SUB if sold else ACCENT)
        badge = str(item["slot"])
        face = canvas.font(24, True)
        canvas.draw.text(
            (x + 44 - canvas.draw.textlength(badge, font=face) / 2, y + 28),
            badge,
            font=face,
            fill="#ffffff",
        )
        lines = canvas.wrap(item["pig"]["name"], 26, tile_w - 40)
        if len(lines) > 2:
            lines = [lines[0], lines[1][:-1] + "…"]
        for row, line in enumerate(lines):
            canvas.text(line, x + 20, y + 264 + row * 36, 26, SUB if sold else TEXT, True)
        if sold:
            status, color = f"已被 {display(item['buyer'])} 换走", SUB
        elif item["owned"]:
            level = min(item["owned"] + 1, level_cap)
            status, color = f"已有 {item['owned']} 只 · 换后 Lv{level}", TEXT
        else:
            status, color = "新图鉴！换到即解锁", ACCENT
        canvas.text(status, x + 20, y + tile_h - 50, 20, color, width=tile_w - 40)
    footer = canvas.image.height - 78
    canvas.draw.line((56, footer, 1024, footer), fill=BORDER, width=2)
    canvas.text(
        "发送「商店交换 编号 你的小猪」用 1 只自己的小猪换走它 · 每天 0 点刷新",
        58,
        footer + 22,
        20,
        ACCENT,
        width=966,
    )
    return finish(canvas.image)


def _paste_art(canvas, root: Path, asset: str, box: tuple, faded: bool = False):
    x, y, w, h = box
    if not asset or not (root / "assets" / asset).is_file():
        canvas.text("?", x + w / 2 - 16, y + h / 2 - 40, 64, SUB, True)
        return
    with Image.open(root / "assets" / asset) as original:
        art = ImageOps.exif_transpose(original).convert("RGBA")
    art.thumbnail((w, h), Image.Resampling.LANCZOS)
    if faded:
        art = ImageOps.grayscale(art.convert("RGB")).convert("RGBA")
        art.putalpha(120)
    canvas.image.paste(art, (int(x + (w - art.width) / 2), int(y + (h - art.height) / 2)), art)
    art.close()


def render_duel_poster(root: Path, poster: dict) -> Card:
    """A tall VS poster: both pigs, the winner, every log line and the settlement."""
    log_size, log_step, width = 22, 34, 940
    round_tag = re.compile(r"^(R\d+) ")
    measure = Canvas(root, WIDTH, 1)
    log = []
    for entry in poster["log"]:
        match = round_tag.match(entry)
        tag = match.group(1) if match else ""
        body = entry[match.end() :] if match else entry
        for index, line in enumerate(measure.wrap(body, log_size, width - (58 if tag else 0))):
            log.append((tag, line, index == 0, entry))
    settlement = [line for entry in poster["settlement"] for line in measure.wrap(entry, 24, width)]
    measure.image.close()
    log_top = 940
    settle_top = log_top + 70 + len(log) * log_step + 40
    height = settle_top + 70 + len(settlement) * 38 + 130
    canvas = Canvas(root, WIDTH, height)
    canvas.text("PIGGY  /  DUEL", 58, 34, 18, ACCENT, True)
    canvas.text(poster["title"], 54, 76, 56, bold=True)
    canvas.text(poster["subtitle"], 58, 158, 24, SUB, width=960)
    for index, side in enumerate(poster["sides"]):
        x = 56 + index * 516
        won = side["won"]
        canvas.draw.rounded_rectangle((x, 223, x + 452, 783), radius=30, fill=BORDER)
        canvas.draw.rounded_rectangle(
            (x, 220, x + 452, 780), radius=30, fill="#ffffff" if won else "#f1ebe4"
        )
        canvas.draw.rounded_rectangle((x + 18, 238, x + 434, 548), radius=22, fill="#f9f4ed")
        _paste_art(canvas, root, side["asset"], (x + 38, 252, 376, 282), faded=not won)
        ribbon = "WIN" if won else "LOSE"
        canvas.draw.rounded_rectangle(
            (x + 300, 252, x + 420, 296), radius=22, fill=ACCENT if won else SUB
        )
        face = canvas.font(24, True)
        canvas.draw.text(
            (x + 360 - canvas.draw.textlength(ribbon, font=face) / 2, 258),
            ribbon,
            font=face,
            fill="#ffffff",
        )
        canvas.text(side["owner"], x + 28, 566, 22, SUB, width=396)
        names = canvas.wrap(side["pig"], 34, 396)
        if len(names) > 2:
            names = [names[0], names[1][:-1] + "…"]
        for row, line in enumerate(names):
            canvas.text(line, x + 28, 600 + row * 44, 34, TEXT, True)
        info = f"Lv{side['level']}" + (f" · {side['style']}" if side.get("style") else "")
        canvas.text(info, x + 28, 690, 22, ACCENT if won else SUB, True, width=396)
        if side.get("max_hp"):
            ratio = max(0, side["hp"]) / side["max_hp"]
            canvas.draw.rounded_rectangle((x + 28, 736, x + 330, 750), radius=7, fill=TRACK)
            if ratio:
                canvas.draw.rounded_rectangle(
                    (x + 28, 736, x + 28 + max(14, int(302 * ratio)), 750),
                    radius=7,
                    fill=ACCENT if won else SUB,
                )
            canvas.text(f"{side['hp']}/{side['max_hp']}", x + 344, 728, 20, SUB, width=96)
    canvas.draw.ellipse((488, 440, 592, 544), fill=TEXT)
    face = canvas.font(40, True)
    canvas.draw.text(
        (540 - canvas.draw.textlength("VS", font=face) / 2, 466), "VS", font=face, fill="#ffffff"
    )
    canvas.draw.rounded_rectangle((56, 812, 1024, 892), radius=26, fill=PANEL)
    canvas.text(poster["headline"], 86, 830, 32, ACCENT, True, width=908)
    canvas.text("战斗过程", 58, log_top, 28, bold=True)
    for index, (tag, line, first, entry) in enumerate(log):
        y = log_top + 60 + index * log_step
        x = 70 + (58 if tag else 0)
        if tag and first:
            canvas.text(tag, 70, y, log_size, ACCENT, True)
        bold = "倒下了" in entry or "站了起来" in entry
        canvas.text(line, x, y, log_size, TEXT if tag else SUB, bold)
    canvas.draw.line((56, settle_top, 1024, settle_top), fill=BORDER, width=2)
    canvas.text("结算", 58, settle_top + 20, 28, bold=True)
    for index, line in enumerate(settlement):
        canvas.text(line, 70, settle_top + 76 + index * 38, 24, TEXT, width=950)
    footer = height - 78
    canvas.draw.line((56, footer, 1024, footer), fill=BORDER, width=2)
    canvas.text(poster["footer"], 58, footer + 22, 20, ACCENT, width=966)
    return finish(canvas.image)


def _hp_bar(canvas, x: int, y: int, width: int, hp, max_hp, color: str):
    ratio = max(0, hp) / max_hp if max_hp else 0
    canvas.draw.rounded_rectangle((x, y, x + width, y + 12), radius=6, fill=TRACK)
    if ratio:
        canvas.draw.rounded_rectangle(
            (x, y, x + max(12, int(width * min(ratio, 1))), y + 12), radius=6, fill=color
        )


def _tag(canvas, text: str, x: int, y: int, fill: str, size: int = 20):
    face = canvas.font(size, True)
    width = canvas.draw.textlength(text, font=face)
    canvas.draw.rounded_rectangle((x, y, x + width + 28, y + size + 18), radius=14, fill=fill)
    canvas.draw.text((x + 14, y + 7), text, font=face, fill="#ffffff")


def render_raid_poster(root: Path, poster: dict) -> Card:
    """Raid report: the boss, the party row, events, every log line and the next step."""
    log_size, log_step, width = 22, 34, 940
    round_tag = re.compile(r"^(R\d+) ")
    measure = Canvas(root, WIDTH, 1)
    mechanics = [
        (line, disabled, index == 0)
        for name, text, disabled in poster["boss"]["mechanics"]
        for index, line in enumerate(measure.wrap(f"{name}：{text}", 21, 880))
    ]
    events = [line for entry in poster["events"] for line in measure.wrap(entry, 22, 900)]
    log = []
    for entry in poster["log"]:
        match = round_tag.match(entry)
        tag = match.group(1) if match else ""
        body = entry[match.end() :] if match else entry
        for index, line in enumerate(measure.wrap(body, log_size, width - (58 if tag else 0))):
            log.append((tag, line, index == 0, entry))
    settlement = [line for entry in poster["settlement"] for line in measure.wrap(entry, 24, width)]
    next_lines = measure.wrap(poster["next"], 26, 880)
    measure.image.close()

    boss_bottom = 590 + len(mechanics) * 32 + 20
    party_top = boss_bottom + 30
    party_bottom = party_top + 340
    events_top = party_bottom + 30
    events_bottom = events_top + (70 + len(events) * 34 if events else 0)
    headline_top = events_bottom + (30 if events else 0)
    log_top = headline_top + 110
    settle_top = log_top + 70 + len(log) * log_step + 40
    next_top = settle_top + 70 + len(settlement) * 38 + 30
    height = next_top + 40 + len(next_lines) * 40 + 130
    canvas = Canvas(root, WIDTH, height)
    canvas.text("PIGGY  /  RAID", 58, 34, 18, ACCENT, True)
    canvas.text(poster["title"], 54, 76, 56, bold=True, width=960)
    canvas.text(poster["subtitle"], 58, 158, 24, SUB, width=960)

    boss = poster["boss"]
    defeated = boss["defeated"]
    canvas.draw.rounded_rectangle((56, 223, 1024, boss_bottom + 3), radius=30, fill=BORDER)
    canvas.draw.rounded_rectangle(
        (56, 220, 1024, boss_bottom), radius=30, fill="#f1ebe4" if defeated else "#ffffff"
    )
    canvas.draw.rounded_rectangle((80, 244, 500, 554), radius=22, fill="#f9f4ed")
    _paste_art(canvas, root, boss["asset"], (96, 256, 388, 286), faded=defeated)
    canvas.text("BOSS", 540, 252, 20, ACCENT, True)
    names = canvas.wrap(boss["name"], 36, 440)
    if len(names) > 2:
        names = [names[0], names[1][:-1] + "…"]
    for row, line in enumerate(names):
        canvas.text(line, 540, 286 + row * 48, 36, TEXT, True)
    info = f"Lv{boss['level']}" + (f" · {boss['style']}" if boss.get("style") else "")
    canvas.text(info, 540, 392, 24, SUB if defeated else ACCENT, True, width=440)
    _hp_bar(canvas, 540, 446, 330, boss["hp"], boss["max_hp"], SUB if defeated else ACCENT)
    canvas.text(f"{boss['hp']}/{boss['max_hp']}", 884, 438, 20, SUB, width=120)
    _tag(canvas, "已击败" if defeated else "未击败", 540, 490, SUB if defeated else TEXT, 22)
    canvas.text("BOSS 机制", 84, 570 - 4, 22, ACCENT, True)
    for index, (line, disabled, first) in enumerate(mechanics):
        y = 600 + index * 32 - 4
        color = SUB if disabled else TEXT
        canvas.text(
            ("· " if first else "  ") + line + ("（本关失效）" if disabled and first else ""),
            84,
            y,
            21,
            color,
            first,
            width=920,
        )

    party = poster["party"]
    count = max(1, len(party))
    gap = 16
    card_w = (968 - gap * (count - 1)) // count
    for index, side in enumerate(party):
        x = 56 + index * (card_w + gap)
        state = side["state"]
        dim = state in ("fallen", "absent")
        canvas.draw.rounded_rectangle(
            (x, party_top + 3, x + card_w, party_bottom + 3), radius=24, fill=BORDER
        )
        canvas.draw.rounded_rectangle(
            (x, party_top, x + card_w, party_bottom),
            radius=24,
            fill="#f1ebe4" if dim else "#ffffff",
        )
        canvas.draw.rounded_rectangle(
            (x + 12, party_top + 12, x + card_w - 12, party_top + 172), radius=18, fill="#f9f4ed"
        )
        _paste_art(
            canvas, root, side["asset"], (x + 20, party_top + 18, card_w - 40, 148), faded=dim
        )
        label = {"fallen": "倒下", "absent": "缺席", "ally": "援军"}.get(state)
        if label:
            _tag(canvas, label, x + 16, party_top + 16, SUB if dim else ACCENT, 18)
        inner = card_w - 32
        canvas.text(side["owner"], x + 16, party_top + 186, 18, SUB, width=inner)
        canvas.text(side["pig"], x + 16, party_top + 214, 24, TEXT, True, width=inner)
        level = f"Lv{side['level']}" if side.get("level") else ""
        info = " · ".join(part for part in (level, side.get("style") or "") if part)
        canvas.text(info, x + 16, party_top + 252, 18, SUB if dim else ACCENT, True, width=inner)
        if side.get("max_hp"):
            _hp_bar(
                canvas,
                x + 16,
                party_top + 288,
                inner,
                side["hp"],
                side["max_hp"],
                SUB if dim else ACCENT,
            )
            canvas.text(
                f"{side['hp']}/{side['max_hp']}", x + 16, party_top + 304, 17, SUB, width=inner
            )

    if events:
        canvas.draw.rounded_rectangle((56, events_top, 1024, events_bottom), radius=26, fill=PANEL)
        canvas.text("随机事件", 84, events_top + 18, 26, bold=True)
        for index, line in enumerate(events):
            canvas.text(line, 84, events_top + 62 + index * 34, 22, TEXT, width=920)
    canvas.draw.rounded_rectangle(
        (56, headline_top, 1024, headline_top + 80), radius=26, fill=PANEL
    )
    canvas.text(poster["headline"], 86, headline_top + 18, 32, ACCENT, True, width=908)

    canvas.text("战斗过程", 58, log_top, 28, bold=True)
    for index, (tag, line, first, entry) in enumerate(log):
        y = log_top + 60 + index * log_step
        x = 70 + (58 if tag else 0)
        if tag and first:
            canvas.text(tag, 70, y, log_size, ACCENT, True)
        bold = any(key in entry for key in ("倒下了", "站了起来", "【场地事件", "！"))
        canvas.text(line, x, y, log_size, TEXT if tag else SUB, bold)
    canvas.draw.line((56, settle_top, 1024, settle_top), fill=BORDER, width=2)
    canvas.text("结算", 58, settle_top + 20, 28, bold=True)
    for index, line in enumerate(settlement):
        canvas.text(line, 70, settle_top + 76 + index * 38, 24, TEXT, width=950)
    canvas.draw.rounded_rectangle(
        (56, next_top, 1024, next_top + 30 + len(next_lines) * 40), radius=26, fill=ACCENT
    )
    for index, line in enumerate(next_lines):
        canvas.text(line, 86, next_top + 14 + index * 40, 26, "#ffffff", True, width=908)
    footer = height - 78
    canvas.draw.line((56, footer, 1024, footer), fill=BORDER, width=2)
    canvas.text(poster["footer"], 58, footer + 22, 20, ACCENT, width=966)
    return finish(canvas.image)


def render_duel_history(root: Path, name: str, history: dict) -> Card:
    """One row per duel with both pigs' art, the result and what changed hands."""
    records = history["records"]
    top, row_h = 268, 150
    canvas = Canvas(root, WIDTH, top + max(1, len(records)) * row_h + 110)
    total, wins = history["total"], history["wins"]
    rate = f"{wins / total:.0%}" if total else "0%"
    canvas.text("PIGGY  /  DUEL LOG", 58, 34, 18, ACCENT, True)
    canvas.text("斗猪记录", 54, 76, 56, bold=True)
    canvas.text(name, 58, 158, 24, SUB, width=960)
    canvas.draw.rounded_rectangle((56, 204, 1024, 244), radius=20, fill=PANEL)
    canvas.text(f"共 {total} 场 · 胜 {wins} 负 {total - wins} · 胜率 {rate}", 80, 210, 22)
    for index, record in enumerate(records):
        y = top + index * row_h
        won = record["won"]
        canvas.draw.rounded_rectangle((56, y, 1024, y + row_h - 16), radius=22, fill="#ffffff")
        canvas.draw.rounded_rectangle(
            (74, y + 20, 150, y + 96), radius=18, fill=ACCENT if won else SUB
        )
        face = canvas.font(40, True)
        mark = "胜" if won else "负"
        canvas.draw.text(
            (112 - canvas.draw.textlength(mark, font=face) / 2, y + 30),
            mark,
            font=face,
            fill="#ffffff",
        )
        canvas.text(f"#{record['id']}", 80, y + 102, 18, SUB)
        day = time.strftime("%m-%d", time.gmtime(record["fought_at"] + 8 * 3600))
        canvas.text(f"{day} · vs {display(record['opponent'])}", 176, y + 18, 22, SUB, width=520)
        for column, (pig, level, asset) in enumerate(
            (
                (record["my_pig"], record["my_level"], record.get("my_asset", "")),
                (record["their_pig"], record["their_level"], record.get("their_asset", "")),
            )
        ):
            x = 176 + column * 330
            canvas.draw.rounded_rectangle((x, y + 52, x + 70, y + 122), radius=14, fill="#f9f4ed")
            _paste_art(canvas, root, asset, (x + 5, y + 57, 60, 60))
            canvas.text(pig, x + 82, y + 58, 22, TEXT, True, width=230)
            canvas.text(
                ("我方" if column == 0 else "对方") + f" · Lv{level}", x + 82, y + 90, 18, SUB
            )
        canvas.text("VS", 474, y + 74, 22, SUB, True)
        change = ("得到" if won else "失去") + f"「{record['prize']}」"
        canvas.text(change, 820, y + 72, 20, ACCENT if won else SUB, True, width=190)
    if not records:
        canvas.text("还没有斗过猪，发送「小猪玩法」看看怎么开始。", 80, top + 40, 26, SUB)
    footer = canvas.image.height - 78
    canvas.draw.line((56, footer, 1024, footer), fill=BORDER, width=2)
    canvas.text(
        "发送「斗猪回放 编号」看完整战报 · 「斗猪记录 页码」翻页", 58, footer + 22, 20, ACCENT
    )
    canvas.text(f"{history['page']:02d} / {history['pages']:02d}", 900, footer + 18, 22, bold=True)
    return finish(canvas.image)


def display(user: dict) -> str:
    return user.get("alias") or user.get("nickname") or f"玩家 {user['id']:04d}"


def render_ranking(root: Path, boards: dict, avatars: dict[str, bytes]) -> Card:
    """Both top tens in one sheet; nickname and avatar share a pill."""
    rows = max(1, *(len(boards[k][:10]) for k in ("species", "total")))
    canvas = Canvas(root, 1488, 312 + rows * 105 + 92)
    canvas.text("PIGGY  /  LEADERBOARD", 56, 34, 18, ACCENT, True)
    canvas.text("小猪排行榜", 52, 79, 56, bold=True)
    canvas.text("本群玩家 · 跨群累计收藏（含下架收藏）", 58, 166, 24, SUB)
    for column, (kind, label, unit) in enumerate(
        (
            ("species", "收集种类榜", "种"),
            ("total", "累计数量榜", "只"),
        )
    ):
        x = 48 + column * 704
        canvas.draw.rounded_rectangle(
            (x, 234, x + 688, canvas.image.height - 68), radius=28, fill=PANEL
        )
        canvas.text(label, x + 28, 250, 30, bold=True)
        canvas.text("TOP 10", x + 548, 258, 21, ACCENT, True)
        players = boards[kind][:10]
        if not players:
            canvas.text("还没有玩家上榜", x + 32, 337, 26, SUB)
        for index, player in enumerate(players):
            y = 313 + index * 105
            rank = player["rank"]
            color = {1: "#b58b43", 2: "#84949d", 3: "#b57d65"}.get(rank, SUB)
            canvas.text(f"{rank:02d}", x + 24, y + 22, 29, color, True, width=64)
            canvas.draw.rounded_rectangle(
                (x + 91, y + 6, x + 507, y + 82), radius=38, fill="#ffffff"
            )
            avatar = avatars.get(player.get("open_id", ""))
            if avatar:
                with Image.open(io.BytesIO(avatar)) as source:
                    art = ImageOps.fit(source.convert("RGB"), (60, 60))
                mask = Image.new("L", (60, 60))
                ImageDraw.Draw(mask).ellipse((0, 0, 59, 59), fill=255)
                canvas.image.paste(art, (x + 99, y + 14), mask)
                art.close()
                mask.close()
            else:
                canvas.draw.ellipse((x + 99, y + 14, x + 159, y + 74), fill=TRACK)
                canvas.text("猪", x + 115, y + 24, 27, ACCENT, True)
            name = player.get("alias") or player.get("nickname") or f"玩家 {player['id']:04d}"
            canvas.text(name, x + 176, y + 24, 25, bold=True, width=308)
            value = f"{player[kind]} {unit}"
            size = 30 if len(value) <= 7 else 23
            text_width = canvas.draw.textlength(value, font=canvas.font(size, True))
            canvas.text(
                value, max(x + 521, x + 662 - text_width), y + 23, size, color, True, width=144
            )
    canvas.text(
        "每天领一只小猪，把日子攒成一座猪圈 · 发送「小猪玩法」和群友斗猪、换猪",
        58,
        canvas.image.height - 46,
        18,
        SUB,
    )
    return finish(canvas.image)


def clean_cards(root: Path):
    """Discard only reproducible UI images that have not been used for a week."""
    threshold = time.time() - 7 * 86400
    for directory, suffix in (("cards", "*.png"), ("cards", "*.tmp"), ("thumbnails", "*.png")):
        for path in (root / directory).glob(suffix):
            try:
                if path.stat().st_mtime < threshold:
                    path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass
