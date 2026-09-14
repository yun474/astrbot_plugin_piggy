import asyncio
import math
import re
from pathlib import Path

from .config import PiggyError, Settings
from .delivery import Message
from .rendering import (
    ATLAS_SHEET_SIZE,
    PEN_SHEET_SIZE,
    render_collection,
    render_ranking,
    render_today,
)


def md(text: str) -> str:
    text = str(text).replace("{{", "｛｛").replace("}}", "｝｝")
    return re.sub(r"([\\`*_{}\[\]<>()#+.!|~>-])", r"\\\1", text)


def display_name(user: dict) -> str:
    return user.get("alias") or user.get("nickname") or f"玩家 {user['id']:04d}"


def paginate(items: list, page: int, size: int) -> tuple[list, int, int]:
    pages = max(1, math.ceil(len(items) / size))
    if not 1 <= page <= pages:
        raise PiggyError(f"页码超出范围，请输入 1–{pages}。")
    return items[(page - 1) * size : page * size], page, pages


def keyboard(
    settings: Settings, owner: str, command: str = "", page: int = 1, pages: int = 1
) -> dict:
    def button(label: str, data: str, private: bool = False):
        permission = {"type": 0, "specify_user_ids": [owner]} if private else {"type": 2}
        return {
            "id": data,
            "render_data": {"label": label, "visited_label": label, "style": 1},
            "action": {
                "type": 2,
                "permission": permission,
                "data": settings.command_prefix + data,
            },
        }

    rows = [
        {"buttons": [button("今日小猪", "今日小猪"), button("小猪图鉴", "小猪图鉴")]},
        {"buttons": [button("小猪排行", "小猪排行"), button("我的猪圈", "我的猪圈")]},
    ]
    navigation = []
    if page > 1:
        navigation.append(button("上一页", f"{command} {page - 1}", True))
    if page < pages:
        navigation.append(button("下一页", f"{command} {page + 1}", True))
    if navigation:
        rows.append({"buttons": navigation})
    return {"content": {"rows": rows}}


def today_message(
    settings: Settings, root: Path, user: dict, result: dict, progress: dict
) -> Message:
    pig = result["pig"]
    state = (
        "今天已经抽过啦，还是这只"
        if not result["created"]
        else "首次解锁！"
        if result["new_species"]
        else "已收进猪圈"
    )
    if not settings.use_host("draw"):
        card = render_today(
            root,
            display_name(user),
            result,
            progress,
            "今日已领取" if not result["created"] else state,
        )
        return Message("", (card.data,), local=True)
    description = "\n".join(
        f"> {line}" if line else ">"
        for line in md(f"{pig['description']}\n\n{pig['analysis']}").splitlines()
    )
    text = (
        f"<@!{user['open_id']}>\n\n### 🐷 今日小猪\n\n"
        f"{state}\n\n**{md(pig['name'])}**\n\n"
        f"![小猪 #512px #512px]({{{{image:0}}}})\n\n"
        f"{description}\n\n"
        f"本猪累计 **{result['count']}** 次 · 总收获 **{progress['total']}** 只\n\n"
        f"已解锁 **{progress['unlocked']}/{progress['active_total']}** · {result['day']}"
    )
    return Message(text, (root / "assets" / pig["asset"],), keyboard(settings, user["open_id"]))


async def collection_message(
    settings: Settings, root: Path, user: dict, progress: dict, page: int, atlas: bool
) -> Message:
    """The same in-memory cream card serves both delivery modes."""
    title = "小猪图鉴" if atlas else "我的猪圈"
    items = (
        [p for p in progress["entries"] if p["enabled"]]
        if atlas
        else [p for p in progress["entries"] if p["count"]]
    )
    entries, page, pages = paginate(items, page, ATLAS_SHEET_SIZE if atlas else PEN_SHEET_SIZE)
    card = await asyncio.to_thread(
        render_collection, root, display_name(user), progress, entries, page, pages, atlas
    )
    return card_message(settings, user, card, title, "atlas" if atlas else "pen", page, pages)


def ranking_message(
    settings: Settings, root: Path, user: dict, boards: dict, avatars: dict
) -> Message:
    return card_message(
        settings, user, render_ranking(root, boards, avatars), "小猪排行榜", "ranking"
    )


def card_message(settings, user, card, title, command, page=1, pages=1):
    hosted = settings.use_host(command)
    return Message(
        f"![{title} #{card.width}px #{card.height}px]({{{{image:0}}}})" if hosted else "",
        (card.data,),
        keyboard(settings, user["open_id"], title, page, pages) if hosted else None,
        local=not hosted,
    )
