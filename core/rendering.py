import io
import math
import time
from dataclasses import dataclass
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
FONT = Path(__file__).resolve().parents[1] / "resources" / "fonts" / "NotoSansSC.ttf"


@dataclass(frozen=True)
class Card:
    data: bytes
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
        text("彩色 · 已解锁    问号 · 待发现", 58, footer + 19, 17, SUB)
    else:
        text("历史收藏会保留", 58, footer + 19, 17, SUB)
    text(f"{page:02d} / {pages:02d}", 888, footer + 16, 21, bold=True)
    return finish(image)


def finish(image: Image.Image) -> Card:
    with image, io.BytesIO() as output:
        image.save(output, "PNG")
        return Card(output.getvalue(), image.width, image.height)


class Canvas:
    """Small shared drawing helpers for the two new cream cards."""

    def __init__(self, width: int, height: int):
        self.image = Image.new("RGB", (width, height), BG)
        self.draw = ImageDraw.Draw(self.image)
        self.fonts = {}

    def font(self, size, bold=False):
        if (size, bold) not in self.fonts:
            face = ImageFont.truetype(str(FONT), size)
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


def render_today(root: Path, name: str, result: dict, progress: dict, state: str) -> Card:
    pig = result["pig"]
    canvas = Canvas(WIDTH, 1)
    description = " ".join(pig["description"].split())
    analysis = " ".join(pig["analysis"].split())
    lines = canvas.wrap(f"{description}\n\n{analysis}", 28, 880)
    canvas.image.close()
    description_y = 870
    panel_bottom = description_y + 58 + 43 * len(lines)
    canvas = Canvas(WIDTH, panel_bottom + 262)
    canvas.text("PIGGY  /  DAILY", 58, 34, 18, ACCENT, True)
    canvas.text("今日小猪", 54, 76, 56, bold=True)
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
    y = panel_bottom + 35
    for x, label, value in (
        (68, "本猪累计", f"{result['count']} 次"),
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
    canvas.text("每天一只小猪，慢慢填满收藏。", 68, y + 34, 19, SUB)
    return finish(canvas.image)


def render_ranking(boards: dict, avatars: dict[str, bytes]) -> Card:
    """Both top tens in one sheet; nickname and avatar share a pill."""
    rows = max(1, *(len(boards[k][:10]) for k in ("species", "total")))
    canvas = Canvas(1488, 312 + rows * 105 + 92)
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
    canvas.text("每天领一只小猪，把日子攒成一座猪圈。", 58, canvas.image.height - 46, 18, SUB)
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
