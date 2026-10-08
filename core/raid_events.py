"""Raid random events: before each boss, during fights, and between bosses.

Entry and interlude events are data; their ops are resolved against the
party before the fight (persistent HP and rewards) and into a battle config
(modifiers that only last for this fight). Field events act on a running
battle directly.
"""

from .battle import STAT_NAMES

QUIET_CHANCE = 0.15
SECOND_EVENT_CHANCE = 0.35
FIELD_CHANCE = 0.08


def _event(id, name, text, weight, ops, dungeon=None, rare=False, pick=False):
    return {
        "id": id,
        "name": name,
        "text": text,
        "weight": weight,
        "ops": ops,
        "dungeon": dungeon,
        "rare": rare,
        "pick": pick,
    }


ENTRY_EVENTS = (
    _event("spring", "温泉", "路边冒着热气的温泉，全队泡了个澡，回复 30% 生命", 6, [("heal", 30)]),
    _event("rockfall", "落石陷阱", "头顶掉下一堆石头，全队各损失 12% 生命", 6, [("hurt", 12)]),
    _event("drums", "战鼓", "远处传来战鼓声，全队攻击 +15%", 6, [("team", "atk", 15)]),
    _event("fog", "迷雾", "四周起了大雾，全队闪避 +10", 5, [("team", "dodge", 10)]),
    _event("feed", "猪饲料补给", "捡到一袋猪饲料，全队获得 20% 生命的护盾", 5, [("shield", 20)]),
    _event("mud", "泥坑", "一脚踩进泥坑，全队速度 -15%", 5, [("team", "spd", -15)]),
    _event("guard", "守卫苏醒", "动静太大把 boss 吵醒了，boss 攻击 +15%", 5, [("boss", "atk", 15)]),
    _event("nap", "打盹的 boss", "boss 还在打盹，开场眩晕 1 回合", 4, [("boss_stun", 1)]),
    _event("grumpy", "暴躁的 boss", "boss 起床气很重，生命 +20%", 4, [("boss_hp", 20)]),
    _event(
        "intel", "弱点情报", "小道消息透露了 boss 的弱点，boss 防御 -25%", 4, [("boss", "def", -25)]
    ),
    _event("ally", "野生援军", "一只路过的野生小猪决定帮忙，本关一起作战", 4, [("ally",)]),
    _event(
        "teammate",
        "猪队友",
        "{who} 被同伴踩了一脚，开场混乱 1 回合",
        4,
        [("one_stun", 1)],
        pick=True,
    ),
    _event("insight", "灵光一闪", "{who} 灵光一闪，暴击 +30", 4, [("one", "crit", 30)], pick=True),
    _event("drag", "拖后腿", "{who} 昨晚没睡好，攻击 -20%", 4, [("one", "atk", -20)], pick=True),
    _event(
        "charm",
        "平安符",
        "每只猪都揣上了平安符，第一次受到致命伤害时保留 1 点生命",
        3,
        [("charm",)],
    ),
    _event(
        "mimic",
        "宝箱怪",
        "宝箱突然张嘴咬人！全队各损失 8% 生命，但本关胜利后每位队员能得到 2 只 boss 猪",
        3,
        [("hurt", 8), ("mimic",)],
    ),
    _event("chest", "宝箱", "发现一个宝箱！每位队员各得 1 只随机小猪", 2, [("chest",)], rare=True),
    _event(
        "clover", "幸运草", "四叶草！本群所有玩家额外得到 1 次再抽", 2, [("clover",)], rare=True
    ),
    _event(
        "tablet",
        "古老石碑",
        "古老石碑封印了 boss 的「{mech}」，本关失效",
        2,
        [("tablet",)],
        rare=True,
    ),
    _event("crowd", "猪群围观", "一群猪围过来看热闹，起哄提前到第 4 回合开始", 3, [("fury", 3)]),
    _event(
        "blizzard",
        "暴风雪",
        "暴风雪刮起来了，全队速度 -10%，boss 早已习惯",
        4,
        [("team", "spd", -10)],
        1,
    ),
    _event(
        "bonfire",
        "篝火",
        "围着篝火取暖，全队回复 20% 生命，第一次冻结无效",
        4,
        [("heal", 20), ("frost_ward",)],
        1,
    ),
    _event(
        "ice", "冰面打滑", "脚下是一大片冰面，每回合每只队员猪有 10% 概率滑倒", 3, [("slip",)], 1
    ),
    _event(
        "icecave", "冰窟宝藏", "冰窟里冻着宝藏！每位队员各得 1 只随机小猪", 2, [("chest",)], 1, True
    ),
    _event("conveyor", "传送带", "战场是一条传送带，本关行动顺序完全随机", 4, [("conveyor",)], 2),
    _event(
        "leak", "漏电", "电线漏电了，所有单位每回合损失 3% 生命，boss 也一样", 3, [("leak", 3)], 2
    ),
    _event("repair", "维修站", "找到一个维修站，全队回复 25% 生命", 4, [("heal", 25)], 2),
    _event("scrap", "机械残骸", "用机械残骸拼了身盔甲，全队防御 +20%", 4, [("team", "def", 20)], 2),
    _event(
        "altar",
        "祭坛献祭",
        "在祭坛上献出鲜血，全队各损失 15% 生命，攻击 +25%",
        3,
        [("hurt", 15), ("team", "atk", 25)],
        3,
    ),
    _event("holy", "神光", "神殿深处洒下神光，全队回复 40% 生命", 2, [("heal", 40)], 3, True),
    _event("statue", "诅咒石像", "石像的眼睛盯着全队，暴击 -10", 4, [("team", "crit", -10)], 3),
    _event(
        "prophecy",
        "古老预言",
        "壁画上画着 boss 的出手顺序，全队闪避 +15",
        4,
        [("team", "dodge", 15)],
        3,
    ),
)

INTERLUDE_EVENTS = (
    _event("rest", "休整", "找了个避风处歇了歇，全队额外回复 15% 生命", 5, [("heal", 15)]),
    _event("lost", "迷路", "绕了好大一圈冤枉路，全队各损失 10% 生命", 4, [("hurt", 10)]),
    _event(
        "caravan",
        "商队",
        "遇到路过的商队，本群所有玩家额外得到 1 次再抽",
        2,
        [("clover",)],
        rare=True,
    ),
    _event(
        "mushroom", "神秘蘑菇", "{who} 吃了一朵神秘蘑菇，{result}", 3, [("mushroom",)], pick=True
    ),
    _event(
        "page",
        "秘籍残页",
        "{who} 捡到一页秘籍残页，本副本内攻击 +10%",
        3,
        [("persist_one", "atk", 10)],
        pick=True,
    ),
    _event("nothing", "无事发生", "一路上风平浪静", 5, []),
)

FIELD_EVENTS = (
    {"id": "quake", "name": "地震", "weight": 3},
    {"id": "feed", "name": "天降猪饲料", "weight": 3},
    {"id": "meteor", "name": "流星", "weight": 2},
    {"id": "wind", "name": "一阵风", "weight": 3},
    {"id": "crow", "name": "乌鸦叼走", "weight": 2},
    {"id": "lightning", "name": "闪电", "weight": 2},
    {"id": "cheer", "name": "猪群欢呼", "weight": 3},
    {"id": "rainbow", "name": "彩虹", "weight": 2},
    {"id": "boar", "name": "野猪乱入", "weight": 2},
    {"id": "sneeze", "name": "打喷嚏", "weight": 2},
)


def _pick(rng, pool):
    return rng.choices(pool, [e["weight"] for e in pool])[0]


def roll_entry(rng, dungeon: int) -> list[dict]:
    if rng.random() < QUIET_CHANCE:
        return []
    pool = [e for e in ENTRY_EVENTS if e["dungeon"] in (None, dungeon)]
    first = _pick(rng, pool)
    events = [first]
    if rng.random() < SECOND_EVENT_CHANCE:
        events.append(_pick(rng, [e for e in pool if e is not first]))
    return events


def roll_interlude(rng) -> dict:
    return _pick(rng, INTERLUDE_EVENTS)


def new_config() -> dict:
    return {
        "team_mods": {},
        "boss_mods": {},
        "unit_mods": {},
        "unit_stun": {},
        "shield": 0,
        "boss_hp": 0,
        "boss_stun": 0,
    }


def new_rewards() -> dict:
    return {"chest": 0, "clover": 0, "mimic": False}


def resolve(events, party: list[dict], rng, cfg: dict, rewards: dict, mechanics=()) -> list[dict]:
    """Apply events to `party` (dicts with seat/label/hp/mods), `cfg` and `rewards`.

    Returns the events with their final text, for the battle report.
    """
    resolved = []
    for event in events:
        picked = rng.choice(party) if event["pick"] and party else None
        fill = {"who": picked["label"] if picked else "", "mech": "", "result": ""}
        for op in event["ops"]:
            kind = op[0]
            if kind == "heal":
                for member in party:
                    member["hp"] = min(1.0, member["hp"] + op[1] / 100)
            elif kind == "hurt":
                for member in party:
                    member["hp"] = max(0.01, member["hp"] - op[1] / 100)
            elif kind == "team":
                cfg["team_mods"][op[1]] = cfg["team_mods"].get(op[1], 0) + op[2]
            elif kind == "boss":
                cfg["boss_mods"][op[1]] = cfg["boss_mods"].get(op[1], 0) + op[2]
            elif kind == "shield":
                cfg["shield"] += op[1]
            elif kind == "boss_hp":
                cfg["boss_hp"] += op[1]
            elif kind == "boss_stun":
                cfg["boss_stun"] = max(cfg["boss_stun"], op[1])
            elif kind in ("ally", "charm", "frost_ward", "slip", "conveyor", "mimic"):
                if kind == "mimic":
                    rewards["mimic"] = True
                else:
                    cfg[kind] = True
            elif kind == "leak":
                cfg["leak"] = op[1]
            elif kind == "fury":
                cfg["fury_round"] = op[1]
            elif kind == "tablet":
                cfg["disabled"] = rng.randrange(3)
                fill["mech"] = mechanics[cfg["disabled"]][0] if mechanics else "机制"
            elif kind in ("chest", "clover"):
                rewards[kind] += 1
            elif picked is None:
                continue
            elif kind == "one_stun":
                cfg["unit_stun"][picked["seat"]] = op[1]
            elif kind == "one":
                mods = cfg["unit_mods"].setdefault(picked["seat"], {})
                mods[op[1]] = mods.get(op[1], 0) + op[2]
            elif kind == "persist_one":
                picked["mods"][op[1]] = picked["mods"].get(op[1], 0) + op[2]
            elif kind == "mushroom":
                if rng.random() < 0.5:
                    picked["hp"] = 1.0
                    fill["result"] = "浑身充满力量，生命回满"
                else:
                    picked["hp"] = min(picked["hp"], 0.3)
                    fill["result"] = "肚子疼得打滚，生命只剩三成"
        resolved.append(
            {"id": event["id"], "name": event["name"], "text": event["text"].format(**fill)}
        )
    return resolved


def field_event(battle):
    rng = battle.rng
    if rng.random() >= FIELD_CHANCE:
        return
    event = _pick(rng, FIELD_EVENTS)
    units = battle.alive_units()
    heroes = battle.alive_heroes()
    kind = event["id"]
    if kind == "quake":
        for unit in units:
            battle.hurt(unit, unit.dot_basis * 0.05)
        text = "大地震动，所有单位损失 5% 生命"
    elif kind == "feed":
        unit = rng.choice(units)
        text = f"一袋猪饲料砸在 {unit.label} 面前，回复 {battle.heal(unit, unit.dot_basis * 0.15)} 生命"
    elif kind == "meteor":
        unit = rng.choice(units)
        text = (
            f"流星砸中了 {unit.label}，造成 {round(battle.hurt(unit, unit.dot_basis * 0.1))} 伤害"
        )
    elif kind == "wind" and heroes:
        hero = rng.choice(heroes)
        hero.evade += 1
        text = f"一阵风吹过，{hero.label} 准备借风躲开下一次攻击"
    elif kind == "crow":
        owners = [u for u in units if any(b["pct"] > 0 for b in u.buffs)]
        if owners:
            unit = rng.choice(owners)
            buff = next(b for b in unit.buffs if b["pct"] > 0)
            unit.buffs.remove(buff)
            text = f"一只乌鸦叼走了 {unit.label} 的{STAT_NAMES[buff['stat']]}增益"
        else:
            text = "一只乌鸦飞过，什么也没叼到"
    elif kind == "lightning":
        unit = rng.choice(units)
        unit.stun = max(unit.stun, 1)
        unit.stun_label = "麻痹"
        text = f"一道闪电劈中了 {unit.label}，麻痹 1 回合"
    elif kind == "cheer":
        battle.cheer = 1.2
        text = "围观的猪齐声欢呼，本回合所有伤害 +20%"
    elif kind == "rainbow":
        for hero in heroes:
            hero.dots.clear()
            hero.buffs = [b for b in hero.buffs if b["pct"] >= 0]
            hero.vuln = 0
            hero.chill = 0
        text = "天边挂起彩虹，全队的负面效果一扫而空"
    elif kind == "boar":
        boss = battle.boss
        text = f"一头野猪冲进来撞了 {boss.label} 一下，造成 {round(battle.hurt(boss, boss.max_hp * 0.08))} 伤害"
    elif kind == "sneeze" and heroes:
        hero = rng.choice(heroes)
        battle.sneezer = hero
        text = f"{hero.label} 打了个大喷嚏，精神一振，本回合行动两次"
    else:
        return
    battle.note(f"【场地事件·{event['name']}】{text}")
    battle.field_events.append(event["name"])
