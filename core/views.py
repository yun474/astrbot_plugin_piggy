import asyncio
import math
import re
import time
from datetime import datetime
from pathlib import Path

from .battle import STAT_NAMES, STATS, describe_skill, entry_for, fighter, level_for
from .config import PiggyError, Settings
from .database import EAST_ASIA
from .delivery import Message
from .raid import DUNGEONS, PARTY_SIZE, REST_HEAL, TIMEOUT_MINUTES, mechanics_for
from .rendering import (
    ATLAS_SHEET_SIZE,
    PEN_SHEET_SIZE,
    render_collection,
    render_duel_history,
    render_duel_poster,
    render_raid_poster,
    render_ranking,
    render_shop,
    render_today,
    render_wild,
)


def md(text: str) -> str:
    text = str(text).replace("{{", "｛｛").replace("}}", "｝｝")
    # QQ renders backslash-escaped parentheses incorrectly; full-width ones read the same.
    text = text.replace("(", "（").replace(")", "）")
    return re.sub(r"([\\`*_{}\[\]<>#+.!|~>-])", r"\\\1", text)


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
        {
            "buttons": [
                button("小猪排行", "小猪排行"),
                button("我的猪圈", "我的猪圈"),
                button("小猪商店", "小猪商店"),
                button("猪副本", "猪副本"),
            ]
        },
        {
            "buttons": [
                button("斗猪玩法", "小猪玩法"),
                button("斗猪排行", "斗猪排行"),
                button("斗猪记录", "斗猪记录"),
                button("小猪挑战", "小猪挑战"),
            ]
        },
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
    items = result.get("items") or [
        {"pig": result["pig"], "kind": "base", "parent": None, "new": result["new_species"]}
    ]
    extras = items[1:]
    if not result["created"]:
        state = "今天已经抽过啦，还是这些" if extras else "今天已经抽过啦，还是这只"
    elif result["new_species"]:
        state = "首次解锁！"
    else:
        state = "老朋友又来啦！"
    protection = ""
    streak = result.get("repeat_streak", 0)
    if settings.duplicate_pity and streak and progress["unlocked"] < progress["active_total"]:
        protection = (
            "下次领取必出未收集小猪！"
            if streak >= settings.duplicate_pity
            else f"重复保护 {streak}/{settings.duplicate_pity}"
        )
    level = level_for(result["count"], settings.battle_level_cap)
    index = result.get("index", 1)
    title = "今日小猪" if index == 1 else f"今日小猪 · 第 {index} 抽"
    summary = ""
    if extras:
        new = sum(1 for item in items if item["new"])
        gathers = sum(1 for item in items if item["kind"] == "gather")
        chains = sum(1 for item in items if item["kind"] == "chain")
        summary = f"本次共 {len(items)} 只 · 新图鉴 {new} 只 · 聚集 {gathers} 次 · 连抽 {chains} 次"
    left = result.get("bonus_left", 0)
    if left:
        hint = f"还有 {left} 次再抽机会，再发「今日小猪」即可"
    elif not result["created"]:
        hint = "打败本群的野生小猪，全群都能再抽一次 · 发送「小猪挑战」"
    else:
        hint = f"Lv{level} · 已解锁 {min(level, 5)}/5 个技能 · 发送「小猪玩法」和群友斗猪、换猪"
    card = render_today(
        root,
        display_name(user),
        result,
        progress,
        state,
        protection,
        level,
        title=title,
        extras=extras,
        summary=summary,
        hint=hint,
    )
    return card_message(settings, user, card, "今日小猪", "draw", mention=user)


def wild_message(settings: Settings, root: Path, user: dict, wild: dict) -> Message:
    entry = entry_for(wild["pig"].get("battle"))
    unit = fighter(wild["pig"], entry, wild["level"])
    card = render_wild(root, wild, unit, describe_skill)
    buttons = [] if wild["status"] == "defeated" else [("挑战小猪", "挑战小猪 ")]
    return card_message(
        settings, user, card, "野生小猪", "wild", buttons=buttons + [("今日小猪", "今日小猪")]
    )


async def wild_battle_message(settings: Settings, root: Path, result: dict) -> Message:
    mine, wild_unit = result["fighters"]
    fight = result["result"]
    user, wild = result["user"], result["wild"]
    name = display_name(user)
    if result["won"]:
        headline = f"{name} 收服了野生「{wild['pig']['name']}」！"
        settlement = [
            f"{name}：{_level_text(result['change'])}",
            f"本群 {result['rewarded']} 位玩家各获得 1 次再抽机会，今天再发「今日小猪」即可使用",
        ]
    else:
        headline = f"野生「{wild['pig']['name']}」获胜，{name} 失去了「{mine['name']}」"
        settlement = [f"{name}：{_level_text(result['change'])}", "野猪还在，大家可以继续挑战"]
    poster = {
        "title": "野猪挑战",
        "subtitle": f"{result['day']} · 第 {wild['attempts']} 次挑战 · {fight['rounds']} 回合",
        "sides": [
            {
                "owner": owner,
                "pig": unit["name"],
                "asset": unit["asset"],
                "level": unit["level"],
                "style": unit["style"],
                "hp": fight["hp"][index],
                "max_hp": fight["max_hp"][index],
                "won": fight["winner"] == index,
            }
            for index, (unit, owner) in enumerate(((mine, name), (wild_unit, "野生小猪")))
        ],
        "headline": headline,
        "log": fight["log"],
        "settlement": settlement,
        "footer": "发送「小猪挑战」查看野猪 · 每个群每天只有一只野猪",
    }
    card = await asyncio.to_thread(render_duel_poster, root, poster)
    buttons = [("今日小猪", "今日小猪")] if result["won"] else [("挑战小猪", "挑战小猪 ")]
    return card_message(settings, user, card, "野猪挑战", "wild", buttons=buttons)


def _expired_notice(expired: list) -> list[str]:
    notes = []
    for raid in expired:
        name = raid["dungeon"]["name"]
        if raid["status"] == "cancelled":
            notes.append(f"上一支「{name}」队伍 {TIMEOUT_MINUTES} 分钟内没凑满 4 人，已自动取消。")
        else:
            notes.append(f"上一支「{name}」队伍的队长没有及时决定，已自动撤退。")
    return notes


def _raid_members(raid: dict) -> list[str]:
    return [
        f"{index}. {'队长 ' if m['user_id'] == raid['leader'] else ''}{display_name(m['user'])}"
        f"：{m['pig']['name']}"
        for index, m in enumerate(raid["members"], 1)
    ]


def raid_list_message(settings: Settings, user: dict, status: dict) -> Message:
    blocks = _expired_notice(status["expired"])
    names = status["bosses"]
    for item in DUNGEONS:
        state = "今天已打过" if item["key"] in status["done"] else "今天可挑战"
        blocks.append(f"【{item['key']} {item['name']}】{item['intro']}（{state}）")
        lines = []
        for stage, slot in enumerate(item["bosses"], 1):
            mechanics = mechanics_for(slot).MECHANICS
            lines.append(f"第 {stage} 关 {names[slot]}")
            lines += [f"　{name}：{text}" for name, text in mechanics]
        blocks.append(lines)
    raid = status["raid"]
    if raid:
        minutes = max(1, math.ceil((raid["expires_at"] - status["now"]) / 60))
        if raid["status"] == "forming":
            blocks.append(
                f"本群正在组队：「{raid['dungeon']['name']}」{len(raid['members'])}/{PARTY_SIZE}，"
                f"约 {minutes} 分钟后过期，发送「加入副本 你的小猪」加入"
            )
        else:
            blocks.append(
                f"本群的「{raid['dungeon']['name']}」队伍已打完第 {raid['stage']} 关，"
                f"等队长决定继续还是撤退（约 {minutes} 分钟）"
            )
        blocks.append(_raid_members(raid))
    blocks.append(
        [
            "开启副本 编号 你的小猪 —— 发起组队，例如：开启副本 1 猪人",
            "加入副本 你的小猪 —— 加入本群正在组队的队伍，满 4 人自动出发",
            f"组队 {TIMEOUT_MINUTES} 分钟内没满 4 人自动取消；开打后不能中途加入",
            "每关打赢：每位队员各得 1 只 boss 猪，本群所有玩家各得 1 次再抽",
            "战斗中倒下的猪，主人失去 1 只；每个副本每人每天 1 次",
        ]
    )
    return text_message(
        settings,
        "猪副本",
        blocks,
        buttons=[("开启副本", "开启副本 "), ("加入副本", "加入副本 ")],
    )


def raid_lobby_message(settings: Settings, result: dict, kind: str) -> Message:
    raid = result["raid"]
    name = raid["dungeon"]["name"]
    blocks = _expired_notice(result.get("expired", []))
    if kind == "cancelled":
        return text_message(
            settings, f"「{name}」队伍已解散", blocks + ["队长退出了，组队取消，不扣次数。"]
        )
    if kind == "retreated":
        return text_message(
            settings,
            f"「{name}」撤退成功",
            blocks
            + [
                f"队伍打完第 {raid['stage']} 关后选择撤退，已得到的 boss 猪和再抽机会都保留着。",
                _raid_members(raid),
            ],
            buttons=[("猪副本", "猪副本")],
        )
    minutes = max(1, math.ceil((raid["expires_at"] - result["now"]) / 60))
    count = len(raid["members"])
    title = {
        "open": f"「{name}」开始组队！",
        "join": f"加入了「{name}」队伍",
        "leave": f"有人退出了「{name}」队伍",
    }[kind]
    blocks += [
        f"组队中 {count}/{PARTY_SIZE} · 约 {minutes} 分钟后没满员自动取消",
        _raid_members(raid),
        f"还差 {PARTY_SIZE - count} 人，发送「加入副本 你的小猪」加入，满 {PARTY_SIZE} 人自动开打第一关。",
    ]
    return text_message(
        settings,
        title,
        blocks,
        buttons=[("加入副本", "加入副本 "), ("退出副本", "退出副本")],
    )


async def raid_battle_message(settings: Settings, root: Path, battle: dict) -> Message:
    raid, boss, result = battle["raid"], battle["boss"], battle["result"]
    info = raid["dungeon"]
    leader = display_name(raid["leader_user"])
    stages = len(info["bosses"])
    party = [
        {
            "owner": display_name(f["user"]),
            "pig": f["pig"]["name"],
            "asset": f["pig"]["asset"],
            "level": f["level"],
            "style": f["style"],
            "hp": f["hp"],
            "max_hp": f["max_hp"],
            "state": "alive" if f["alive"] else "fallen",
        }
        for f in battle["fighters"]
    ]
    party += [
        {
            "owner": display_name(m["user"]),
            "pig": m["pig"]["name"],
            "asset": m["pig"]["asset"],
            "level": 0,
            "style": style,
            "state": state,
        }
        for key, state, style in (
            ("retired", "fallen", "前几关已倒下"),
            ("absent", "absent", "猪不在猪圈里"),
        )
        for m in battle[key]
    ]
    ally = battle["ally"]
    if ally:
        party.append(
            {
                "owner": "路过的野生小猪",
                "pig": ally["pig"]["name"],
                "asset": ally["pig"]["asset"],
                "level": ally["level"],
                "style": ally["style"],
                "hp": ally["hp"],
                "max_hp": ally["max_hp"],
                "state": "ally",
            }
        )
    events = [f"【{e['kind']}·{e['name']}】{e['text']}" for e in battle["events"]]
    if result and result["field_events"]:
        events.append("【场地事件】战斗中触发了：" + "、".join(result["field_events"]))
    if not result:
        headline = "没有能出战的小猪，副本结束"
    elif battle["won"]:
        headline = f"击败了 {boss['label']}！"
    elif result["timeout"]:
        headline = f"{result['rounds']} 回合没能打倒 {boss['label']}，队伍撤出战斗"
    else:
        headline = f"团灭了……{boss['label']} 获胜"
    settlement = []
    for user, changes in battle["changes"]:
        text = "；".join(_level_text(c) for c in changes) if changes else "没有变化"
        settlement.append(f"{display_name(user)}：{text}")
    if battle["copies"] > 1:
        settlement.append(f"宝箱怪兑现了承诺：每位队员得到 {battle['copies']} 只 boss 猪")
    if battle["chest"]:
        settlement.append(f"宝箱：每位队员额外得到 {battle['chest']} 只随机小猪")
    if battle["bonus"]:
        settlement.append(
            f"本群 {battle['rewarded']} 位玩家各获得 {battle['bonus']} 次再抽机会，"
            "今天再发「今日小猪」即可使用"
        )
    status = battle["status"]
    if status == "waiting":
        next_step = (
            f"第 {battle['stage']} 关通关！队长 {leader} 发送「继续副本」挑战第 {battle['stage'] + 1} 关"
            f"「{battle['next_boss']}」，或发送「撤退副本」带着奖励离开。存活的猪会先回复 {REST_HEAL:.0%} 生命；"
            f"{TIMEOUT_MINUTES} 分钟内不决定自动撤退。"
        )
    elif status == "cleared":
        next_step = f"「{info['name']}」三关全部通关！恭喜全队，明天还能再来。"
    elif battle["won"]:
        next_step = "boss 倒下了，但队员全部倒下，副本到此结束。"
    else:
        next_step = "副本失败，到此结束。可以换一个副本，或者明天再来。"
    rounds = f" · {result['rounds']} 回合" if result else ""
    poster = {
        "title": f"{info['name']} · 第 {battle['stage']}/{stages} 关",
        "subtitle": f"{battle['day']} · 队长 {leader}{rounds}",
        "boss": {
            "name": boss["label"],
            "asset": boss["pig"]["asset"],
            "level": boss["level"],
            "style": boss["style"],
            "hp": boss["hp"],
            "max_hp": boss["max_hp"],
            "defeated": battle["won"],
            "mechanics": [
                (name, text, index == boss["disabled"])
                for index, (name, text) in enumerate(boss["mechanics"])
            ],
        },
        "party": party,
        "events": events,
        "headline": headline,
        "log": result["log"] if result else [],
        "settlement": settlement,
        "next": next_step,
        "footer": f"发送「猪副本」查看副本和 boss 机制 · 每个副本每人每天 1 次 · 第 #{battle['id']} 场",
    }
    card = await asyncio.to_thread(render_raid_poster, root, poster)
    buttons = (
        [("继续副本", "继续副本"), ("撤退副本", "撤退副本")]
        if status == "waiting"
        else [("猪副本", "猪副本"), ("今日小猪", "今日小猪")]
    )
    return card_message(
        settings,
        raid["leader_user"],
        card,
        "猪副本战报",
        "raid",
        buttons=buttons,
        mention=raid["leader_user"] if status == "waiting" and settings.use_host("raid") else None,
    )


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
        render_collection,
        root,
        f"{display_name(user)} · 玩家编号 #{user['id']}",
        progress,
        entries,
        page,
        pages,
        atlas,
        settings.battle_level_cap,
    )
    return card_message(settings, user, card, title, "atlas" if atlas else "pen", page, pages)


def ranking_message(
    settings: Settings, root: Path, user: dict, boards: dict, avatars: dict
) -> Message:
    return card_message(
        settings, user, render_ranking(root, boards, avatars), "小猪排行榜", "ranking"
    )


def _button(settings: Settings, label: str, data: str) -> dict:
    # Anyone may tap; respond() only matches requests addressed to the sender.
    return {
        "id": data,
        "render_data": {"label": label, "visited_label": label, "style": 1},
        "action": {"type": 2, "permission": {"type": 2}, "data": settings.command_prefix + data},
    }


def text_message(
    settings: Settings,
    title: str,
    blocks: list,
    mention: dict | None = None,
    buttons: list[tuple[str, str]] = (),
    plain_hint: str = "",
) -> Message:
    """Blocks are paragraphs (str) or bullet lists (list of str).

    plain_hint stands in for the buttons when the message is sent as plain text.
    """
    if not settings.battle_markdown:
        parts = [title]
        if mention:
            parts[0] = f"@{display_name(mention)} {title}"
        for block in blocks:
            parts.append("\n".join(block) if isinstance(block, list) else block)
        if plain_hint:
            parts.append(plain_hint)
        return Message("\n".join(parts))
    parts = []
    if mention:
        parts.append(f'<qqbot-at-user id="{mention["open_id"]}" />')
    parts.append(f"### {md(title)}")
    for block in blocks:
        if isinstance(block, list):
            parts.append("\n".join(f"- {md(line)}" for line in block))
        else:
            parts.append(md(block))
    keyboard = None
    if buttons:
        keyboard = {
            "content": {
                "rows": [{"buttons": [_button(settings, label, data) for label, data in buttons]}]
            }
        }
    return Message("\n\n".join(parts), keyboard=keyboard, markdown=True)


def guide_message(settings: Settings, user: dict, favorite: dict | None) -> Message:
    example = favorite["name"] if favorite else "猪人"
    blocks = [
        "【等级】",
        [
            f"同种猪有几只就是几级（上限 Lv{settings.battle_level_cap}），等级越高属性越强",
            "1–5 级各解锁 1 个专属技能，每只猪最多 5 个技能",
            f"小猪属性 {example} —— 查看属性和技能",
        ],
        "【斗猪】",
        [
            f"斗猪 @群友 {example} —— 用你的猪发起挑战",
            f"@ 识别不到时用玩家编号代替：斗猪 #编号 {example}（你的编号是 #{user['id']}，"
            "编号显示在「我的猪圈」上）",
            "对方发送「接受斗猪 他的猪」立即开打，或「拒绝斗猪」",
            "回合制自动对战，赢家把输家出战的那只猪收进猪圈，输家这只猪降 1 级",
            f"每天最多 {settings.duel_daily_limit} 场",
        ],
        "【交换】",
        [
            f"小猪交换 @群友 {example} 对方的猪 —— 一换一",
            "对方发送「接受交换」成交，或「拒绝交换」",
        ],
        "【请求】",
        [
            f"我的请求 / 取消请求 —— 查看或撤回，{settings.request_ttl_minutes} 分钟内未处理自动作废",
        ],
        "【战绩】",
        [
            "斗猪排行 —— 本群斗猪胜率排行，至少 3 场上榜",
            "斗猪记录 [页码] —— 自己的历史对战",
            "斗猪回放 编号 —— 重看某场的完整战报",
        ],
        "【抽卡】",
        [
            "今日小猪 —— 每天一抽，每只猪都可能触发猪群聚集（多得 1 只相同的）和小猪连抽（再抽 1 只）",
            "有再抽机会时，当天再发「今日小猪」就能再抽一次",
        ],
        "【野猪】",
        [
            "小猪挑战 —— 查看本群今天的野生小猪（每天一只，1–20 级随机）",
            f"挑战小猪 {example} —— 输了失去出战的猪，赢了收服野猪，全群各得 1 次再抽（当天有效）",
        ],
        "【商店】",
        [
            "小猪商店 —— 每天 0 点上架 5 只小猪，每只限量 1 个，先到先得",
            f"商店交换 编号 {example} —— 用自己的 1 只小猪换走它",
        ],
        "【副本】",
        [
            "猪副本 —— 查看 3 个副本、9 个 boss 的专属机制",
            f"开启副本 编号 {example} —— 发起组队；群友发送「加入副本 他的猪」加入",
            f"满 {PARTY_SIZE} 人自动开打，{TIMEOUT_MINUTES} 分钟没满员自动取消，开打后不能中途加入",
            "每关打赢：每位队员各得 1 只 boss 猪，全群各得 1 次再抽；倒下的猪，主人失去 1 只",
            "打完一关由队长发送「继续副本」或「撤退副本」；每个副本每人每天 1 次",
        ],
    ]
    if favorite:
        blocks.append(f"你的「{example}」有 {favorite['count']} 只，是你现在最强的出战选择。")
    return text_message(
        settings,
        "小猪玩法：斗猪与交换",
        blocks,
        buttons=[("小猪属性", "小猪属性 "), ("斗猪", "斗猪 "), ("我的请求", "我的请求")],
    )


def _skill_line(skill: dict, slot: int, level: int) -> str:
    state = "已解锁" if slot <= level else f"Lv{slot} 解锁"
    return f"【{state}】{skill['name']}：{skill['text']}（{describe_skill(skill)}）"


def stats_message(settings: Settings, user: dict, pig: dict, count: int, cap: int) -> Message:
    entry = entry_for(pig.get("battle"))
    level = level_for(count, cap)
    unit = fighter(pig, entry, level)
    stats = unit["stats"]
    if count:
        owned = f"{display_name(user)} 拥有 {count} 只 · Lv{level}（上限 Lv{cap}）"
    else:
        owned = f"{display_name(user)} 还没有这只小猪，以下为 Lv1 数据"
    numbers = " · ".join(
        f"{STAT_NAMES[key]} {stats[key]}{'%' if key in ('crit', 'dodge') else ''}" for key in STATS
    )
    skills = [
        _skill_line(skill, slot, level if count else 0)
        for slot, skill in enumerate(entry["skills"], 1)
    ]
    return text_message(
        settings, f"{pig['name']}（{entry['style']}）", [owned, numbers, "技能", skills]
    )


def _level_text(change: dict) -> str:
    name = change["pig"]["name"]
    if not change["after"]:
        text = f"「{name}」全部输光，Lv{change['before']} → 已失去"
    elif not change["before"]:
        text = f"新获得「{name}」Lv{change['after']}"
    elif change["before"] == change["after"]:
        text = f"「{name}」Lv{change['after']}（已达等级上限）"
    else:
        text = f"「{name}」Lv{change['before']} → Lv{change['after']}"
    if change["lost"]:
        text += f"，失去技能：{'、'.join(change['lost'])}"
    if change["gained"]:
        text += f"，解锁技能：{'、'.join(change['gained'])}"
    return text


def request_message(settings: Settings, request: dict, level: int) -> Message:
    sender, target = display_name(request["from"]), request["to"]
    minutes = max(1, round((request["expires_at"] - request["created_at"]) / 60))
    if request["kind"] == "duel":
        return text_message(
            settings,
            f"{sender} 向你发起斗猪！",
            [
                f"对方出战：「{request['give']['name']}」Lv{level}",
                "发送「接受斗猪 你的小猪」应战，或发送「拒绝斗猪」。",
                f"败者会失去出战的那只小猪，等级随之下降。请求 {minutes} 分钟内有效。",
            ],
            mention=target,
            buttons=[("接受斗猪", "接受斗猪 "), ("拒绝斗猪", "拒绝斗猪")],
        )
    return text_message(
        settings,
        f"{sender} 想和你交换小猪！",
        [
            f"对方给出「{request['give']['name']}」，想换你的「{request['want']['name']}」。",
            f"发送「接受交换」或「拒绝交换」，请求 {minutes} 分钟内有效。",
        ],
        mention=target,
        buttons=[("接受交换", "接受交换"), ("拒绝交换", "拒绝交换")],
    )


def declined_message(settings: Settings, result: dict) -> Message:
    request = result["request"]
    label = "斗猪" if request["kind"] == "duel" else "交换"
    if result.get("error"):
        return text_message(settings, f"{label}请求已作废", [result["error"]], request["from"])
    return text_message(
        settings,
        f"{display_name(request['to'])} 拒绝了你的{label}请求",
        ["下次再约吧。"],
        mention=request["from"],
    )


async def battle_message(settings: Settings, root: Path, result: dict) -> Message:
    a, b = result["fighters"]
    fight = result["result"]
    winner, loser = result["winner"], result["loser"]
    users = list(result["users"].values())
    left = result["duels_left"]
    poster = {
        "title": "斗猪对决",
        "subtitle": f"{result['day']} · 第 #{result['record_id']} 场 · {fight['rounds']} 回合",
        "sides": [
            {
                "owner": display_name(user),
                "pig": unit["name"],
                "asset": unit["asset"],
                "level": unit["level"],
                "style": unit["style"],
                "hp": fight["hp"][index],
                "max_hp": fight["max_hp"][index],
                "won": fight["winner"] == index,
            }
            for index, (unit, user) in enumerate(zip((a, b), users))
        ],
        "headline": f"{display_name(winner)} 获胜！赢走「{result['loser_change']['pig']['name']}」",
        "log": fight["log"],
        "settlement": [
            f"{display_name(loser)}：{_level_text(result['loser_change'])}",
            f"{display_name(winner)}：{_level_text(result['winner_change'])}",
            "今日剩余斗猪次数："
            + "，".join(
                f"{display_name(user)} {left[uid]} 场" for uid, user in result["users"].items()
            ),
        ],
        "footer": "发送「斗猪记录」查看历史战绩 · 「斗猪排行」看本群胜率榜",
    }
    card = await asyncio.to_thread(render_duel_poster, root, poster)
    return card_message(
        settings,
        result["request"]["from"],
        card,
        "斗猪对决",
        "duel",
        buttons=[("斗猪记录", "斗猪记录"), ("斗猪排行", "斗猪排行")],
    )


def trade_message(settings: Settings, result: dict) -> Message:
    request, changes = result["request"], result["changes"]
    sender, target = display_name(request["from"]), display_name(request["to"])
    return text_message(
        settings,
        "交换成功！",
        [
            f"{sender} 的「{request['give']['name']}」⇄ {target} 的「{request['want']['name']}」",
            [
                f"{sender}：{_level_text(changes['from_give'])}",
                f"{sender}：{_level_text(changes['from_want'])}",
                f"{target}：{_level_text(changes['to_want'])}",
                f"{target}：{_level_text(changes['to_give'])}",
            ],
        ],
        mention=request["from"],
    )


async def shop_message(settings: Settings, root: Path, user: dict, shop: dict) -> Message:
    card = await asyncio.to_thread(
        render_shop, root, display_name(user), shop, settings.battle_level_cap
    )
    return card_message(settings, user, card, "今日小猪商店", "shop")


def shop_exchange_message(settings: Settings, user: dict, result: dict) -> Message:
    paid, got = result["paid"], result["got"]
    return text_message(
        settings,
        "商店交换成功！",
        [
            f"{display_name(user)} 用「{paid['pig']['name']}」换到了 {result['slot']} 号"
            f"「{got['pig']['name']}」",
            [_level_text(paid), _level_text(got)],
            f"商店今天还剩 {result['left']} 件",
        ],
        mention=user,
    )


def _rate(wins: int, games: int) -> str:
    return f"{wins / games:.0%}" if games else "0%"


def duel_ranking_message(settings: Settings, user: dict, board: dict) -> Message:
    need = board["min_games"]
    lines = [
        f"{row['rank']:02d}. {display_name(row)} #{row['id']}  胜 {row['wins']} / 负 {row['losses']}  "
        f"胜率 {_rate(row['wins'], row['games'])}"
        for row in board["top"]
    ]
    blocks = [lines or [f"还没有玩家打满 {need} 场，快去斗猪吧。"]]
    me = board["me"]
    if not me:
        blocks.append("你还没有斗过猪，发送「小猪玩法」看看怎么开始。")
    elif not any(row["id"] == me["id"] for row in board["top"]):
        mine = f"你：胜 {me['wins']} / 负 {me['losses']}，胜率 {_rate(me['wins'], me['games'])}"
        if me["games"] < need:
            mine += f"，再打 {need - me['games']} 场即可上榜"
        elif "rank" in me:
            mine += f"，排第 {me['rank']} 名"
        blocks.append(mine)
    blocks.append(f"本群玩家 · 跨群累计战绩 · 至少 {need} 场上榜")
    return text_message(
        settings,
        "斗猪胜率排行",
        blocks,
        buttons=[("斗猪记录", "斗猪记录"), ("斗猪玩法", "小猪玩法")],
        plain_hint="发送「斗猪记录」查看自己的对战",
    )


async def duel_history_message(
    settings: Settings, root: Path, user: dict, history: dict
) -> Message:
    card = await asyncio.to_thread(render_duel_history, root, display_name(user), history)
    buttons = [("斗猪回放", "斗猪回放 ")]
    if history["page"] < history["pages"]:
        buttons.append(("下一页", f"斗猪记录 {history['page'] + 1}"))
    buttons.append(("斗猪排行", "斗猪排行"))
    return card_message(settings, user, card, "斗猪记录", "duel", buttons=buttons)


async def duel_replay_message(settings: Settings, root: Path, user: dict, record: dict) -> Message:
    players = record["players"]
    summary = record["summary"] or {}
    day = datetime.fromtimestamp(record["fought_at"], EAST_ASIA).strftime("%Y-%m-%d %H:%M")
    sides = []
    for index, side in enumerate("ab"):
        owner = players[record[f"{side}_user"]]
        sides.append(
            {
                "owner": display_name(owner),
                "pig": record[f"{side}_pig_name"],
                "asset": record[f"{side}_asset"],
                "level": record[f"{side}_level"],
                "style": (summary.get("styles") or [None, None])[index],
                "hp": (summary.get("hp") or [None, None])[index],
                "max_hp": (summary.get("max_hp") or [None, None])[index],
                "won": record["winner"] == owner["id"],
            }
        )
    rounds = f" · {summary['rounds']} 回合" if summary.get("rounds") else ""
    winner = players[record["winner"]]
    poster = {
        "title": f"斗猪回放 #{record['id']}",
        "subtitle": f"{day}{rounds}",
        "sides": sides,
        "headline": f"{display_name(winner)} 获胜！赢走「{record['prize']}」",
        "log": record["log"],
        "settlement": [f"{display_name(winner)} 赢走了「{record['prize']}」"],
        "footer": "发送「斗猪记录」查看历史战绩 · 「斗猪排行」看本群胜率榜",
    }
    card = await asyncio.to_thread(render_duel_poster, root, poster)
    return card_message(
        settings,
        user,
        card,
        f"斗猪回放 #{record['id']}",
        "duel",
        buttons=[("斗猪记录", "斗猪记录"), ("斗猪排行", "斗猪排行")],
    )


def _request_line(request: dict, incoming: bool) -> str:
    other = display_name(request["from"] if incoming else request["to"])
    minutes = max(1, math.ceil((request["expires_at"] - time.time()) / 60))
    if request["kind"] == "duel":
        text = (
            f"斗猪：{other} 出战「{request['give']['name']}」"
            if incoming
            else (f"斗猪：你用「{request['give']['name']}」挑战 {other}")
        )
    elif incoming:
        text = f"交换：{other} 用「{request['give']['name']}」换你的「{request['want']['name']}」"
    else:
        text = f"交换：你用「{request['give']['name']}」换 {other} 的「{request['want']['name']}」"
    return f"{text}（约 {minutes} 分钟后过期）"


def requests_message(settings: Settings, requests: dict) -> Message:
    blocks = []
    if requests["incoming"]:
        blocks += ["收到的请求", [_request_line(r, True) for r in requests["incoming"]]]
    if requests["outgoing"]:
        blocks += ["发出的请求", [_request_line(r, False) for r in requests["outgoing"]]]
    if not blocks:
        blocks = ["你在本群没有待处理的请求。"]
    return text_message(settings, "我的请求", blocks)


def cancelled_message(settings: Settings, cancelled: list[dict]) -> Message:
    return text_message(
        settings,
        f"已撤回 {len(cancelled)} 个请求",
        [[_request_line(r, False).rsplit("（", 1)[0] for r in cancelled]],
    )


def card_message(settings, user, card, title, command, page=1, pages=1, buttons=(), mention=None):
    hosted = settings.use_host(command)
    board = None
    if hosted:
        board = (
            {"content": {"rows": [{"buttons": [_button(settings, *b) for b in buttons]}]}}
            if buttons
            else keyboard(settings, user["open_id"], title, page, pages)
        )
    return Message(
        (f'<qqbot-at-user id="{mention["open_id"]}" />\n\n' if mention else "")
        + f"![{title} #{card.width}px #{card.height}px]({{{{image:0}}}})"
        if hosted
        else "",
        (card.data,),
        board,
        local=not hosted,
    )
