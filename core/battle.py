import json
import random
from pathlib import Path

from .config import PiggyError

STATS = ("hp", "atk", "def", "spd", "crit", "dodge")
STAT_NAMES = {
    "hp": "生命",
    "atk": "攻击",
    "def": "防御",
    "spd": "速度",
    "crit": "暴击",
    "dodge": "闪避",
}
STAT_LIMITS = {
    "hp": (40, 250),
    "atk": (5, 50),
    "def": (0, 40),
    "spd": (1, 30),
    "crit": (0, 50),
    "dodge": (0, 40),
}
# Level only scales the four core stats; crit and dodge are percentages.
GROWN = ("hp", "atk", "def", "spd")
GROWTH = 0.08
SKILL_SLOTS = 5
MAX_ROUNDS = 20
CRIT_MULTIPLIER = 1.6
EXECUTE_MULTIPLIER = 1.8
FURY_ROUND = 8
FURY_STEP = 0.15
WHEN = {
    "hurt": "自身生命低于 70% 时",
    "low_hp": "自身生命低于 40% 时",
    "enemy_low": "对手生命低于 35% 时",
}
PASSIVE_EFFECTS = {"buff", "shield", "thorns", "regen", "revive", "evade"}

_num = (int, float)
_EFFECT_FIELDS = {
    "damage": {
        "power": (_num, 0.1, 5),
        "power_max": (_num, 0.1, 6),
        "hits": (int, 1, 6),
        "pierce": (_num, 0, 100),
        "drain": (_num, 0, 100),
        "execute": (_num, 1, 99),
        "true": (bool,),
        "sure": (bool,),
        "scale": (str, {"atk", "def", "spd", "hp"}),
    },
    "heal": {"pct": (_num, 1, 80)},
    "shield": {"pct": (_num, 1, 80)},
    "stun": {
        "turns": (int, 1, 3),
        "label": (str, 1, 8),
        "target": (str, {"enemy", "self"}),
    },
    "dot": {"pct": (_num, 1, 20), "turns": (int, 1, 6), "label": (str, 1, 8)},
    "buff": {
        "stat": (str, {"atk", "def", "spd", "crit", "dodge"}),
        "pct": (_num, -80, 150),
        "turns": (int, 1, 99),
        "target": (str, {"enemy", "self"}),
    },
    "thorns": {"pct": (_num, 1, 100), "turns": (int, 1, 99)},
    "regen": {"pct": (_num, 1, 20), "turns": (int, 1, 99)},
    "evade": {"count": (int, 1, 3)},
    "cleanse": {},
    "dispel": {},
    "copy": {},
    "recoil": {"pct": (_num, 1, 60)},
    "revive": {"pct": (_num, 1, 100)},
}
_REQUIRED = {
    "damage": ("power",),
    "heal": ("pct",),
    "shield": ("pct",),
    "dot": ("pct", "turns"),
    "buff": ("stat", "pct"),
    "thorns": ("pct",),
    "regen": ("pct",),
    "recoil": ("pct",),
    "revive": ("pct",),
}

FALLBACK = {
    "style": "均衡",
    "stats": {"hp": 105, "atk": 19, "def": 10, "spd": 10, "crit": 5, "dodge": 5},
    "skills": [
        {
            "name": "猪突猛进",
            "text": "低头就是一拱。",
            "effects": [{"type": "damage", "power": 1.0}],
        },
        {
            "name": "皮糙肉厚",
            "text": "猪圈里摸爬滚打练出来的厚皮。",
            "passive": True,
            "effects": [{"type": "buff", "stat": "def", "pct": 15}],
        },
        {
            "name": "连环拱",
            "text": "左拱一下右拱一下。",
            "cd": 2,
            "effects": [{"type": "damage", "power": 0.6, "hits": 2}],
        },
        {
            "name": "哼哼回血",
            "text": "躺下哼两声，元气回来了。",
            "cd": 4,
            "when": "hurt",
            "effects": [{"type": "heal", "pct": 22}],
        },
        {
            "name": "全力一拱",
            "text": "把全身的膘都压上去。",
            "cd": 4,
            "effects": [{"type": "damage", "power": 2.0, "pierce": 30}],
        },
    ],
}


def _check(value, spec, where: str):
    kind = spec[0]
    if kind is bool:
        ok = type(value) is bool
    elif kind is int:
        ok = type(value) is int and spec[1] <= value <= spec[2]
    elif kind is _num:
        ok = type(value) in (int, float) and spec[1] <= value <= spec[2]
    elif isinstance(spec[1], set):
        ok = isinstance(value, str) and value in spec[1]
    else:
        ok = isinstance(value, str) and spec[1] <= len(value.strip()) <= spec[2]
    if not ok:
        raise PiggyError(f"战斗数据 {where} 取值不合法。")


def _effects(items, where: str, passive: bool) -> list[dict]:
    if not isinstance(items, list) or not 1 <= len(items) <= 6:
        raise PiggyError(f"战斗数据 {where} 需要 1–6 个效果。")
    result = []
    for index, effect in enumerate(items):
        spot = f"{where} 第 {index + 1} 个效果"
        if not isinstance(effect, dict) or effect.get("type") not in _EFFECT_FIELDS:
            raise PiggyError(f"战斗数据 {spot} 类型不合法。")
        kind = effect["type"]
        if passive and kind not in PASSIVE_EFFECTS:
            raise PiggyError(f"战斗数据 {spot}：被动技能不能使用 {kind}。")
        if not passive and kind == "revive":
            raise PiggyError(f"战斗数据 {spot}：revive 只能用于被动技能。")
        fields = _EFFECT_FIELDS[kind]
        for key, value in effect.items():
            if key == "type":
                continue
            if key == "chance":
                _check(value, (int, 1, 100), spot)
            elif key in fields:
                _check(value, fields[key], spot)
            else:
                raise PiggyError(f"战斗数据 {spot} 含未知字段 {key}。")
        for key in _REQUIRED.get(kind, ()):
            if key not in effect:
                raise PiggyError(f"战斗数据 {spot} 缺少 {key}。")
        if kind == "damage" and effect.get("power_max", effect["power"]) < effect["power"]:
            raise PiggyError(f"战斗数据 {spot} 的 power_max 不能小于 power。")
        result.append(dict(effect))
    return result


def validate_entry(pig_id: str, entry) -> dict:
    where = pig_id
    if not isinstance(entry, dict):
        raise PiggyError(f"战斗数据 {where} 必须为对象。")
    unknown = set(entry) - {"style", "stats", "skills"}
    if unknown:
        raise PiggyError(f"战斗数据 {where} 含未知字段 {', '.join(sorted(unknown))}。")
    _check(entry.get("style"), (str, 1, 8), f"{where} 流派")
    stats = entry.get("stats")
    if not isinstance(stats, dict) or set(stats) != set(STATS):
        raise PiggyError(f"战斗数据 {where} 的属性必须包含 {', '.join(STATS)}。")
    for key in STATS:
        _check(stats[key], (int, *STAT_LIMITS[key]), f"{where} 属性 {key}")
    skills = entry.get("skills")
    if not isinstance(skills, list) or len(skills) != SKILL_SLOTS:
        raise PiggyError(f"战斗数据 {where} 必须恰好有 {SKILL_SLOTS} 个技能。")
    names = set()
    normalized = []
    for index, skill in enumerate(skills):
        spot = f"{where} 技能 {index + 1}"
        if not isinstance(skill, dict):
            raise PiggyError(f"战斗数据 {spot} 必须为对象。")
        unknown = set(skill) - {"name", "text", "cd", "when", "passive", "fail", "effects"}
        if unknown:
            raise PiggyError(f"战斗数据 {spot} 含未知字段 {', '.join(sorted(unknown))}。")
        _check(skill.get("name"), (str, 1, 16), f"{spot} 名称")
        _check(skill.get("text"), (str, 1, 80), f"{spot} 文案")
        if skill["name"] in names:
            raise PiggyError(f"战斗数据 {where} 技能名重复：{skill['name']}")
        names.add(skill["name"])
        passive = skill.get("passive", False)
        _check(passive, (bool,), spot)
        _check(skill.get("cd", 0), (int, 0, 9), f"{spot} 冷却")
        when = skill.get("when")
        if when is not None and when not in WHEN:
            raise PiggyError(f"战斗数据 {spot} 的触发条件不合法。")
        if passive and (skill.get("cd") or when or "fail" in skill):
            raise PiggyError(f"战斗数据 {spot}：被动技能不能设置冷却、条件或失败。")
        item = {
            "name": skill["name"].strip(),
            "text": skill["text"].strip(),
            "cd": skill.get("cd", 0),
            "when": when,
            "passive": passive,
            "effects": _effects(skill.get("effects"), spot, passive),
        }
        fail = skill.get("fail")
        if fail is not None:
            if not isinstance(fail, dict) or set(fail) - {"chance", "text", "effects"}:
                raise PiggyError(f"战斗数据 {spot} 的失败设置不合法。")
            _check(fail.get("chance"), (int, 1, 99), f"{spot} 失败概率")
            _check(fail.get("text"), (str, 1, 80), f"{spot} 失败文案")
            item["fail"] = {
                "chance": fail["chance"],
                "text": fail["text"].strip(),
                "effects": _effects(fail["effects"], f"{spot} 失败", False)
                if fail.get("effects")
                else [],
            }
        normalized.append(item)
    first = normalized[0]
    if (
        first["passive"]
        or first["cd"]
        or first["when"]
        or "fail" in first
        or not any(e["type"] == "damage" for e in first["effects"])
    ):
        raise PiggyError(f"战斗数据 {where} 的第 1 个技能必须是无冷却、无条件的攻击技能。")
    return {"style": entry["style"].strip(), "stats": dict(stats), "skills": normalized}


def validate_battle(data, pig_ids) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("pigs"), dict):
        raise PiggyError("战斗数据必须是包含 pigs 对象的 JSON。")
    unknown = set(data["pigs"]) - set(pig_ids)
    if unknown:
        raise PiggyError(f"战斗数据含有猪库中不存在的小猪：{', '.join(sorted(unknown)[:5])}")
    return {pig_id: validate_entry(pig_id, entry) for pig_id, entry in data["pigs"].items()}


def load_battle(path: Path, pig_ids) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise PiggyError("战斗数据 battle.json 无法读取，原有数据已保留。") from exc
    return validate_battle(data, pig_ids)


def entry_for(raw: str | None) -> dict:
    """Pigs without curated data still fight with a balanced template."""
    return json.loads(raw) if raw else validate_entry("fallback", FALLBACK)


def level_for(count: int, cap: int) -> int:
    return max(1, min(count, cap))


def fighter(pig: dict, entry: dict, level: int, label: str = "") -> dict:
    scale = 1 + GROWTH * (level - 1)
    stats = {
        key: round(value * scale) if key in GROWN else value
        for key, value in entry["stats"].items()
    }
    return {
        "pig_id": pig["id"],
        "name": pig["name"],
        "asset": pig.get("asset", ""),
        "label": label or pig["name"],
        "style": entry["style"],
        "level": level,
        "stats": stats,
        "skills": entry["skills"][: min(level, SKILL_SLOTS)],
        "locked": entry["skills"][min(level, SKILL_SLOTS) :],
    }


def describe_effect(effect: dict) -> str:
    kind = effect["type"]
    chance = f"{effect['chance']}% 概率" if effect.get("chance", 100) < 100 else ""
    if kind == "damage":
        power = f"{effect['power']:g}"
        if "power_max" in effect:
            power += f"~{effect['power_max']:g}"
        base = {"atk": "攻击", "def": "防御", "spd": "速度", "hp": "生命"}[
            effect.get("scale", "atk")
        ]
        text = f"{power} 倍{base}伤害"
        if effect.get("hits", 1) > 1:
            text = f"{effect['hits']} 段 × {text}"
        extras = []
        if effect.get("true"):
            extras.append("真实伤害")
        elif effect.get("pierce"):
            extras.append(f"无视 {effect['pierce']:g}% 防御")
        if effect.get("sure"):
            extras.append("必中")
        if effect.get("drain"):
            extras.append(f"吸血 {effect['drain']:g}%")
        if effect.get("execute"):
            extras.append(f"对手低于 {effect['execute']:g}% 生命时伤害提高")
        if extras:
            text += "，" + "，".join(extras)
    elif kind == "heal":
        text = f"回复 {effect['pct']:g}% 生命"
    elif kind == "shield":
        text = f"获得 {effect['pct']:g}% 生命的护盾"
    elif kind == "stun":
        who = "自己" if effect.get("target") == "self" else "对手"
        text = f"使{who}{effect.get('label', '眩晕')} {effect.get('turns', 1)} 回合"
    elif kind == "dot":
        text = f"{effect['label'] if 'label' in effect else '持续伤害'}：每回合 {effect['pct']:g}% 生命，持续 {effect['turns']} 回合"
    elif kind == "buff":
        who = "对手" if effect.get("target") == "enemy" else "自身"
        sign = "+" if effect["pct"] >= 0 else ""
        unit = "%" if effect["stat"] in ("atk", "def", "spd") else " 点"
        turns = f"，{effect['turns']} 回合" if effect.get("turns", 99) < 99 else ""
        text = f"{who}{STAT_NAMES[effect['stat']]} {sign}{effect['pct']:g}{unit}{turns}"
    elif kind == "thorns":
        turns = f"，{effect['turns']} 回合" if effect.get("turns", 99) < 99 else ""
        text = f"反弹 {effect['pct']:g}% 受到的伤害{turns}"
    elif kind == "regen":
        turns = f"，{effect['turns']} 回合" if effect.get("turns", 99) < 99 else ""
        text = f"每回合回复 {effect['pct']:g}% 生命{turns}"
    elif kind == "evade":
        text = f"完全躲开接下来 {effect.get('count', 1)} 次攻击"
    elif kind == "cleanse":
        text = "清除自身负面状态"
    elif kind == "dispel":
        text = "驱散对手增益和护盾"
    elif kind == "copy":
        text = "复制对手上一个技能"
    elif kind == "recoil":
        text = f"自身损失 {effect['pct']:g}% 生命"
    else:
        text = f"倒下时以 {effect['pct']:g}% 生命复活一次"
    return chance + text


def describe_skill(skill: dict) -> str:
    parts = ["；".join(describe_effect(e) for e in skill["effects"])]
    if skill["passive"]:
        parts.insert(0, "被动")
    if skill["cd"]:
        parts.append(f"冷却 {skill['cd']}")
    if skill["when"]:
        parts.append(WHEN[skill["when"]])
    if "fail" in skill:
        parts.append(f"{skill['fail']['chance']}% 失手")
    return " · ".join(parts)


class _Unit:
    def __init__(self, data: dict):
        self.data = data
        self.label = data["label"]
        self.base = data["stats"]
        self.max_hp = data["stats"]["hp"]
        self.hp = float(data.get("start_hp", self.max_hp))
        self.buffs = []
        self.dots = []
        self.regens = []
        self.thorns = []
        self.shield = 0.0
        self.stun = 0
        self.stun_label = ""
        self.evade = 0
        self.revive = None
        self.cooldowns = {}
        self.last = None
        self.actives = [s for s in data["skills"] if not s["passive"]]

    def stat(self, key: str) -> float:
        bonus = sum(b["pct"] for b in self.buffs if b["stat"] == key)
        if key in ("crit", "dodge"):
            return max(0.0, min(75.0, self.base[key] + bonus))
        return max(1.0 if key != "def" else 0.0, self.base[key] * (1 + max(-80, bonus) / 100))

    @property
    def ratio(self) -> float:
        return max(0.0, self.hp) / self.max_hp

    @property
    def alive(self) -> bool:
        return self.hp > 0


class _Battle:
    def __init__(self, a: dict, b: dict, seed: int):
        self.rng = random.Random(seed)
        self.units = (_Unit(a), _Unit(b))
        self.log = []
        self.round = 0

    def say(self, text: str):
        self.log.append(f"R{self.round} {text}" if self.round else text)

    def run(self) -> dict:
        for unit in self.units:
            for skill in unit.data["skills"]:
                if skill["passive"]:
                    for effect in skill["effects"]:
                        self.apply_passive(unit, effect)
                    self.say(f"{unit.label} 被动「{skill['name']}」生效")
        winner = None
        for self.round in range(1, MAX_ROUNDS + 1):
            if self.round == FURY_ROUND + 1:
                self.say("围观的猪开始起哄，之后每回合伤害都会更高！")
            a, b = self.units
            speeds = (a.stat("spd"), b.stat("spd"))
            if speeds[0] == speeds[1]:
                first = self.rng.randrange(2)
            else:
                first = 0 if speeds[0] > speeds[1] else 1
            for index in (first, 1 - first):
                self.turn(self.units[index], self.units[1 - index])
                winner = self.decided()
                if winner is not None:
                    break
            if winner is not None:
                break
        else:
            self.round = 0
            a, b = self.units
            if a.ratio == b.ratio:
                winner = self.rng.randrange(2)
            else:
                winner = 0 if a.ratio > b.ratio else 1
            self.say(
                f"{MAX_ROUNDS} 回合未分胜负，按剩余生命判定："
                f"{a.label} {a.ratio:.0%} / {b.label} {b.ratio:.0%}"
            )
        return {
            "winner": winner,
            "rounds": self.round or MAX_ROUNDS,
            "log": self.log,
            "hp": [round(max(0, u.hp)) for u in self.units],
            "max_hp": [u.max_hp for u in self.units],
        }

    def decided(self):
        a, b = self.units
        if not a.alive and not b.alive:
            return 0 if a.hp >= b.hp else 1
        if not a.alive:
            return 1
        if not b.alive:
            return 0
        return None

    def apply_passive(self, unit: _Unit, effect: dict):
        kind = effect["type"]
        if kind == "buff":
            target = self.other(unit) if effect.get("target") == "enemy" else unit
            target.buffs.append(
                {"stat": effect["stat"], "pct": effect["pct"], "turns": effect.get("turns", 99)}
            )
        elif kind == "shield":
            unit.shield += unit.max_hp * effect["pct"] / 100
        elif kind == "thorns":
            unit.thorns.append({"pct": effect["pct"], "turns": effect.get("turns", 99)})
        elif kind == "regen":
            unit.regens.append({"pct": effect["pct"], "turns": effect.get("turns", 99)})
        elif kind == "evade":
            unit.evade += effect.get("count", 1)
        elif kind == "revive":
            unit.revive = effect["pct"]

    def fury(self) -> float:
        """Long fights escalate so sustain-heavy pigs cannot stall forever."""
        return 1 + FURY_STEP * max(0, self.round - FURY_ROUND)

    def other(self, unit: _Unit) -> _Unit:
        return self.units[1] if unit is self.units[0] else self.units[0]

    def hurt(self, unit: _Unit, amount: float, source: str = "") -> float:
        if amount <= 0 or not unit.alive:
            return 0.0
        absorbed = min(unit.shield, amount)
        unit.shield -= absorbed
        amount -= absorbed
        unit.hp -= amount
        self.check_revive(unit)
        return amount + absorbed

    def check_revive(self, unit: _Unit):
        if unit.hp <= 0 and unit.revive:
            unit.hp = unit.max_hp * unit.revive / 100
            unit.revive = None
            unit.dots.clear()
            self.say(f"{unit.label} 倒下后又站了起来！恢复 {round(unit.hp)} 生命")

    def heal(self, unit: _Unit, amount: float) -> int:
        before = unit.hp
        unit.hp = min(unit.max_hp, unit.hp + amount)
        return round(unit.hp - before)

    def turn(self, unit: _Unit, enemy: _Unit):
        for dot in list(unit.dots):
            dot["turns"] -= 1
            if dot["turns"] <= 0 and dot in unit.dots:
                unit.dots.remove(dot)
            damage = self.hurt(unit, self.dot_amount(unit, dot))
            self.say(f"{unit.label} 受到{dot['label']}影响，损失 {round(damage)} 生命")
            if not unit.alive:
                return
        for regen in list(unit.regens):
            healed = self.heal(unit, unit.max_hp * regen["pct"] / 100)
            regen["turns"] -= 1
            if regen["turns"] <= 0:
                unit.regens.remove(regen)
            if healed:
                self.say(f"{unit.label} 回复 {healed} 生命")
        for name in list(unit.cooldowns):
            unit.cooldowns[name] = max(0, unit.cooldowns[name] - 1)
        if unit.stun:
            unit.stun -= 1
            self.say(f"{unit.label} 处于{unit.stun_label}状态，这回合动不了")
        else:
            self.act(unit, enemy)
        for group in (unit.buffs, unit.thorns):
            for item in list(group):
                item["turns"] -= 1
                if item["turns"] <= 0:
                    group.remove(item)

    def act(self, unit: _Unit, enemy: _Unit):
        self.cast(unit, enemy, self.choose(unit, enemy))

    def choose(self, unit: _Unit, enemy: _Unit) -> dict:
        basic = unit.actives[0]
        ready = []
        for skill in unit.actives[1:]:
            if unit.cooldowns.get(skill["name"], 0):
                continue
            when = skill["when"]
            if when == "hurt" and unit.ratio >= 0.7:
                continue
            if when == "low_hp" and unit.ratio >= 0.4:
                continue
            if when == "enemy_low" and enemy.ratio >= 0.35:
                continue
            if any(e["type"] == "copy" for e in skill["effects"]) and not enemy.last:
                continue
            ready.append(skill)
        if not ready or self.rng.random() < 0.2:
            return basic
        weights = [unit.actives.index(skill) + 1 for skill in ready]
        return self.rng.choices(ready, weights)[0]

    def cast(self, unit: _Unit, enemy: _Unit, skill: dict, copied: bool = False):
        if not copied:
            unit.cooldowns[skill["name"]] = skill["cd"] + 1 if skill["cd"] else 0
            unit.last = skill
        fail = skill.get("fail")
        if fail and self.rng.randrange(100) < fail["chance"]:
            self.say(f"{unit.label}「{skill['name']}」失手了：{fail['text']}")
            self.apply(unit, enemy, fail["effects"], skill)
            return
        self.apply(unit, enemy, skill["effects"], skill)

    def apply(self, unit: _Unit, enemy: _Unit, effects: list, skill: dict):
        notes = []
        for effect in effects:
            if not unit.alive or not enemy.alive:
                break
            if self.rng.randrange(100) >= effect.get("chance", 100):
                continue
            note = self.effect(unit, enemy, effect, skill)
            if note:
                notes.append(note)
        self.say(
            f"{unit.label}「{skill['name']}」" + ("，".join(notes) if notes else "，但什么也没发生")
        )
        self.announce_falls(unit, enemy)

    def announce_falls(self, unit: _Unit, enemy: _Unit):
        for target in (enemy, unit):
            if not target.alive:
                self.say(f"{target.label} 倒下了")

    def dot_amount(self, unit: _Unit, dot: dict) -> float:
        return unit.max_hp * dot["pct"] / 100

    def crit_roll(self, unit: _Unit, enemy: _Unit) -> bool:
        return self.rng.random() * 100 < unit.stat("crit")

    def pierce(self, unit: _Unit, enemy: _Unit, effect: dict) -> float:
        return effect.get("pierce", 0)

    def adjust_hit(self, unit: _Unit, enemy: _Unit, raw: float, effect: dict) -> float:
        """Return 0 to void the hit entirely."""
        return raw

    def after_hit(self, unit: _Unit, enemy: _Unit, dealt: float):
        pass

    def effect(self, unit: _Unit, enemy: _Unit, effect: dict, skill: dict) -> str:
        kind = effect["type"]
        if kind == "damage":
            return self.damage(unit, enemy, effect)
        if kind == "heal":
            return f"回复 {self.heal(unit, unit.max_hp * effect['pct'] / 100)} 生命"
        if kind == "shield":
            amount = unit.max_hp * effect["pct"] / 100
            unit.shield += amount
            return f"获得 {round(amount)} 护盾"
        if kind == "stun":
            target = unit if effect.get("target") == "self" else enemy
            label = effect.get("label", "眩晕")
            target.stun = max(target.stun, effect.get("turns", 1))
            target.stun_label = label
            return f"{'自己' if target is unit else target.label}陷入{label}"
        if kind == "dot":
            enemy.dots.append(
                {
                    "pct": effect["pct"],
                    "turns": effect["turns"],
                    "label": effect.get("label", "持续伤害"),
                }
            )
            return f"{enemy.label}陷入{effect.get('label', '持续伤害')}"
        if kind == "buff":
            target = enemy if effect.get("target") == "enemy" else unit
            # Own buffs tick down at the end of this same turn.
            turns = effect.get("turns", 2) + (target is unit)
            target.buffs.append({"stat": effect["stat"], "pct": effect["pct"], "turns": turns})
            sign = "+" if effect["pct"] >= 0 else ""
            unit_text = "%" if effect["stat"] in ("atk", "def", "spd") else ""
            who = target.label if target is enemy else ""
            return f"{who}{STAT_NAMES[effect['stat']]}{sign}{effect['pct']:g}{unit_text}"
        if kind == "thorns":
            unit.thorns.append({"pct": effect["pct"], "turns": effect.get("turns", 2) + 1})
            return f"进入反弹状态 {effect['pct']:g}%"
        if kind == "regen":
            unit.regens.append({"pct": effect["pct"], "turns": effect.get("turns", 3)})
            return "开始持续回复"
        if kind == "evade":
            unit.evade += effect.get("count", 1)
            return "准备躲开下一次攻击"
        if kind == "cleanse":
            unit.dots.clear()
            unit.buffs = [b for b in unit.buffs if b["pct"] >= 0]
            unit.stun = 0
            return "清除了负面状态"
        if kind == "dispel":
            enemy.buffs = [b for b in enemy.buffs if b["pct"] < 0]
            enemy.shield = 0
            enemy.thorns.clear()
            enemy.evade = 0
            return f"驱散了{enemy.label}的增益"
        if kind == "copy":
            source = enemy.last
            if not source or any(e["type"] == "copy" for e in source["effects"]):
                return "没找到能复制的技能"
            self.say(f"{unit.label} 复制了「{source['name']}」！")
            self.cast(unit, enemy, source, copied=True)
            return ""
        if kind == "recoil":
            lost = self.hurt(unit, unit.max_hp * effect["pct"] / 100)
            return f"自己也损失 {round(lost)} 生命"
        return ""

    def damage(self, unit: _Unit, enemy: _Unit, effect: dict) -> str:
        true = effect.get("true", False)
        if enemy.evade and not true:
            enemy.evade -= 1
            return f"被{enemy.label}完全躲开"
        scale = effect.get("scale", "atk")
        if scale == "atk":
            base = unit.stat("atk")
        elif scale == "hp":
            base = unit.max_hp * 0.18
        else:
            base = unit.stat(scale) * 1.6
        hits = effect.get("hits", 1)
        total, crits, misses, voids = 0.0, 0, 0, 0
        self.void_note = ""
        for _ in range(hits):
            if not enemy.alive:
                break
            if not (true or effect.get("sure")) and self.rng.random() * 100 < enemy.stat("dodge"):
                misses += 1
                continue
            power = effect["power"]
            if "power_max" in effect:
                power = self.rng.uniform(power, effect["power_max"])
            raw = base * power * self.rng.uniform(0.9, 1.1) * self.fury()
            if effect.get("execute") and enemy.ratio * 100 <= effect["execute"]:
                raw *= EXECUTE_MULTIPLIER
            if self.crit_roll(unit, enemy):
                raw *= CRIT_MULTIPLIER
                crits += 1
            if not true:
                defense = enemy.stat("def") * (1 - min(100, self.pierce(unit, enemy, effect)) / 100)
                raw *= 60 / (60 + defense)
            raw = self.adjust_hit(unit, enemy, raw, effect)
            if raw <= 0:
                voids += 1
                continue
            dealt = self.hurt(enemy, max(1.0, raw))
            total += dealt
            self.after_hit(unit, enemy, dealt)
            for thorn in enemy.thorns:
                self.hurt(unit, dealt * thorn["pct"] / 100)
        if misses == hits:
            return f"被{enemy.label}闪开了"
        if misses + voids == hits:
            return f"打了个空（{self.void_note or enemy.label + '化解了攻击'}）"
        text = f"造成 {round(total)} 伤害"
        if hits > 1:
            text = f"{hits - misses - voids} 段命中，共{text}"
        if crits:
            text += "（暴击）"
        if effect.get("drain"):
            healed = self.heal(unit, total * effect["drain"] / 100)
            if healed:
                text += f"，吸血 {healed}"
        if enemy.thorns and total:
            text += "，被反弹了一部分"
        return text


def simulate(a: dict, b: dict, seed: int) -> dict:
    return _Battle(a, b, seed).run()
