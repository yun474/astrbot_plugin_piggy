"""Pig raids: four players' pigs against three bosses in a row."""

import random

from .battle import CRIT_MULTIPLIER, FURY_ROUND, FURY_STEP, _Battle, _Unit, fighter
from .raid_bosses import BOSSES, BossMechanics
from .raid_events import field_event

PARTY_SIZE = 4
TIMEOUT_MINUTES = 10
MAX_RAID_ROUNDS = 25
REST_HEAL = 0.3
# Bosses match the party's average level; later stages hit harder through these
# multipliers instead, so the difficulty curve is the same at every level.
STAGE_HP = (4.6, 5.2, 5.8)
STAGE_ATK = (1.35, 1.45, 1.55)

DUNGEONS = (
    {
        "key": 1,
        "name": "冰封猪圈",
        "intro": "寒风刺骨的废弃猪圈，越往里越冷。",
        "bosses": ("goblin-pig", "frozen-pig", "everest-pig"),
    },
    {
        "key": 2,
        "name": "机械猪厂",
        "intro": "嗡嗡作响的自动化猪厂，到处是故障的机器。",
        "bosses": ("error-404-pig", "mechanical-pig", "cyberpunk-pig"),
    },
    {
        "key": 3,
        "name": "猪神殿",
        "intro": "供奉着猪神的古老神殿，诅咒与神迹并存。",
        "bosses": ("demon-pig", "chained_crown_pig", "pig_god"),
    },
)

GUARD_SKILL = {
    "name": "守护之光",
    "text": "替神明挡下一切。",
    "cd": 0,
    "when": None,
    "passive": False,
    "effects": [{"type": "damage", "power": 0.8}],
}


def dungeon(key) -> dict:
    for item in DUNGEONS:
        if str(item["key"]) == str(key).strip("#＃号") or item["name"] == str(key):
            return item
    raise KeyError(key)


def mechanics_for(pig_id: str) -> type[BossMechanics]:
    return BOSSES.get(pig_id, BossMechanics)


def boss_level(levels: list[int]) -> int:
    return max(1, round(sum(levels) / len(levels))) if levels else 1


def boss_fighter(pig: dict, entry: dict, level: int, stage: int, slot_id: str, label: str) -> dict:
    """The boss pig at raid strength; `slot_id` picks the mechanics' own multipliers."""
    cls = mechanics_for(slot_id)
    unit = fighter(pig, entry, level, label)
    stats = unit["stats"]
    unit["dot_basis"] = stats["hp"]
    # Low-level pigs have few skills and fights drag on; ease them in until Lv5.
    rookie = min(1.0, 0.7 + 0.06 * level)
    stats["hp"] = round(stats["hp"] * STAGE_HP[stage - 1] * cls.HP * rookie)
    stats["atk"] = round(stats["atk"] * STAGE_ATK[stage - 1] * cls.ATK)
    stats["def"] = round(stats["def"] * cls.DEF)
    return unit


class _RaidUnit(_Unit):
    def __init__(self, data: dict, side: int):
        super().__init__(data)
        self.side = side
        self.seat = data.get("seat")
        self.mods = dict(data.get("mods") or {})
        self.dot_basis = data.get("dot_basis", self.max_hp)
        self.vuln = 0
        self.chill = 0
        self.frost_ward = False
        self.charm = False
        self.glitch = False
        self.blind = False
        self.zeroed = set()

    def stat(self, key: str) -> float:
        if key in self.zeroed or (key == "dodge" and self.blind):
            return 0.0
        value = super().stat(key)
        bonus = self.mods.get(key, 0)
        if key in ("crit", "dodge"):
            return max(0.0, min(75.0, value + bonus))
        return max(1.0 if key != "def" else 0.0, value * (1 + max(-90, bonus) / 100))


class _RaidBattle(_Battle):
    def __init__(self, heroes: list[dict], boss: dict, slot_id: str, cfg: dict, seed: int):
        self.rng = random.Random(seed)
        self.log = []
        self.round = 0
        self.cfg = cfg
        self.heroes = [_RaidUnit(data, 0) for data in heroes]
        self.boss = _RaidUnit(boss, 1)
        self.foes = [self.boss]
        self.units = (self.heroes[0], self.boss)
        self.mech = mechanics_for(slot_id)(cfg.get("disabled"))
        self.fury_round = cfg.get("fury_round", FURY_ROUND)
        self.cheer = 1.0
        self.sneezer = None
        self.notes = []
        self.falls = []
        self.field_events = []
        self.void_note = ""
        self._configure()

    def _configure(self):
        cfg, boss = self.cfg, self.boss
        if cfg.get("boss_hp"):
            boss.max_hp = round(boss.max_hp * (1 + cfg["boss_hp"] / 100))
            boss.hp = float(boss.max_hp)
        for key, value in cfg.get("boss_mods", {}).items():
            boss.mods[key] = boss.mods.get(key, 0) + value
        if cfg.get("boss_stun"):
            boss.stun, boss.stun_label = cfg["boss_stun"], "打盹"
        for hero in self.heroes:
            for key, value in cfg.get("team_mods", {}).items():
                hero.mods[key] = hero.mods.get(key, 0) + value
            for key, value in cfg.get("unit_mods", {}).get(hero.seat, {}).items():
                hero.mods[key] = hero.mods.get(key, 0) + value
            if cfg.get("unit_stun", {}).get(hero.seat):
                hero.stun, hero.stun_label = cfg["unit_stun"][hero.seat], "混乱"
            hero.shield += hero.max_hp * cfg.get("shield", 0) / 100
            hero.charm = bool(cfg.get("charm"))
            hero.frost_ward = bool(cfg.get("frost_ward"))

    # ----- helpers used by boss mechanics and field events -----

    def note(self, text: str):
        self.notes.append(text)

    def flush(self):
        for text in self.notes + self.falls:
            self.say(text)
        self.notes, self.falls = [], []

    def alive_heroes(self) -> list:
        return [h for h in self.heroes if h.alive]

    def alive_units(self) -> list:
        return [u for u in self.heroes + self.foes if u.alive]

    def strike(self, attacker, target, power: float, *, crit: bool = False) -> float:
        """A mechanic's hit: never misses, ignores the attacker's skill list."""
        if not target.alive:
            return 0.0
        raw = attacker.stat("atk") * power * self.rng.uniform(0.9, 1.1) * self.fury()
        if crit:
            raw *= CRIT_MULTIPLIER
        raw *= 60 / (60 + target.stat("def"))
        raw *= 1 + target.vuln / 100
        return self.hurt(target, max(1.0, raw))

    def summon(self, label: str, hp_pct: float, atk_pct: float):
        base = self.boss.data
        stats = dict(base["stats"])
        stats["hp"] = max(1, round(self.boss.max_hp * hp_pct))
        stats["atk"] = max(1, round(stats["atk"] * atk_pct))
        data = {
            **base,
            "label": label,
            "stats": stats,
            "skills": [GUARD_SKILL],
            "dot_basis": stats["hp"],
            "start_hp": stats["hp"],
        }
        unit = _RaidUnit(data, 1)
        self.foes.append(unit)
        return unit

    # ----- engine overrides -----

    def fury(self) -> float:
        return (1 + FURY_STEP * max(0, self.round - self.fury_round)) * self.cheer

    def other(self, unit):
        return self.target_for(unit)

    def target_for(self, unit):
        if unit.side == 0:
            foes = [f for f in self.foes if f.alive]
            guards = [f for f in foes if f is not self.boss]
            return self.rng.choice(guards) if guards else self.boss
        heroes = self.alive_heroes()
        if not heroes:
            return None
        if unit is self.boss:
            return self.mech.pick_target(self, heroes)
        return self.rng.choice(heroes)

    def apply_passive(self, unit, effect):
        if effect["type"] == "buff" and effect.get("target") == "enemy":
            targets = [self.boss] if unit.side == 0 else self.heroes
            for target in targets:
                target.buffs.append(
                    {"stat": effect["stat"], "pct": effect["pct"], "turns": effect.get("turns", 99)}
                )
            return
        super().apply_passive(unit, effect)

    def hurt(self, unit, amount: float, source: str = "") -> float:
        was_alive = unit.alive
        dealt = super().hurt(unit, amount)
        if was_alive and not unit.alive and unit.charm:
            unit.charm = False
            unit.hp = 1.0
            self.note(f"平安符护住了 {unit.label}，保留 1 点生命")
        if unit is self.boss and unit.alive:
            self.mech.damaged(self)
        if was_alive and not unit.alive:
            self.falls.append(f"{unit.label} 倒下了")
            if unit.side == 0 and unit.seat is not None:
                self.mech.hero_fell(self, unit)
        return dealt

    def announce_falls(self, unit, enemy):
        self.flush()

    def dot_amount(self, unit, dot):
        return unit.dot_basis * dot["pct"] / 100

    def crit_roll(self, unit, enemy) -> bool:
        crit = super().crit_roll(unit, enemy)
        if crit and enemy is self.boss:
            return self.mech.crit_against(self, unit)
        return crit

    def pierce(self, unit, enemy, effect):
        value = super().pierce(unit, enemy, effect)
        return self.mech.pierce(self, value) if enemy is self.boss else value

    def adjust_hit(self, unit, enemy, raw, effect):
        raw *= 1 + enemy.vuln / 100
        if enemy is self.boss:
            raw = self.mech.incoming(self, unit, raw, effect)
        return raw

    def after_hit(self, unit, enemy, dealt):
        if unit is self.boss and enemy.side == 0:
            self.mech.outgoing(self, enemy, dealt)

    def effect(self, unit, enemy, effect, skill):
        text = super().effect(unit, enemy, effect, skill)
        if effect["type"] == "cleanse":
            unit.vuln = 0
            unit.chill = 0
        elif effect["type"] == "dispel" and enemy is self.boss:
            self.mech.dispelled(self)
        return text

    def act(self, unit, enemy):
        if unit is self.boss and self.mech.before_act(self):
            return
        if getattr(unit, "glitch", False):
            unit.glitch = False
            lost = self.hurt(unit, unit.max_hp * 0.05)
            self.say(f"{unit.label} 返回了 404，原地疯狂刷新，损失 {round(lost)} 生命")
            return
        super().act(unit, enemy)

    def decided(self):
        if not self.boss.alive:
            return 0
        if not self.alive_heroes():
            return 1
        return None

    def order(self) -> list:
        units = self.alive_units()
        if self.cfg.get("conveyor"):
            self.rng.shuffle(units)
            return units
        keyed = [(-u.stat("spd"), self.rng.random(), index) for index, u in enumerate(units)]
        return [units[k[2]] for k in sorted(keyed)]

    def run(self) -> dict:
        for unit in self.heroes + self.foes:
            for skill in unit.data["skills"]:
                if skill["passive"]:
                    for effect in skill["effects"]:
                        self.apply_passive(unit, effect)
                    self.say(f"{unit.label} 被动「{skill['name']}」生效")
        self.mech.setup(self)
        self.flush()
        winner, timeout = None, False
        for self.round in range(1, MAX_RAID_ROUNDS + 1):
            if self.round == self.fury_round + 1:
                self.say("围观的猪开始起哄，之后每回合伤害都会更高！")
            self.cheer, self.sneezer = 1.0, None
            for hero in self.heroes:
                hero.blind = False
            self.mech.round_start(self)
            self.flush()
            field_event(self)
            self.flush()
            winner = self.decided()
            if winner is not None:
                break
            for unit in self.order():
                if not unit.alive:
                    continue
                if self.cfg.get("slip") and unit.side == 0 and self.rng.random() < 0.1:
                    self.say(f"{unit.label} 在冰面上滑倒了，这回合爬不起来")
                    continue
                actions = 1
                if unit is self.boss:
                    actions += self.mech.extra_actions(self)
                if unit is self.sneezer:
                    actions += 1
                for index in range(actions):
                    if not unit.alive or self.decided() is not None:
                        break
                    enemy = self.target_for(unit)
                    if enemy is None:
                        break
                    if index == 0:
                        self.turn(unit, enemy)
                    elif not unit.stun:
                        self.act(unit, enemy)
                    self.flush()
                winner = self.decided()
                if winner is not None:
                    break
            if winner is not None:
                break
            self.mech.round_end(self)
            if self.cfg.get("leak"):
                for unit in self.alive_units():
                    self.hurt(unit, unit.dot_basis * self.cfg["leak"] / 100)
                self.note(f"漏电，所有单位损失 {self.cfg['leak']}% 生命")
            self.flush()
            winner = self.decided()
            if winner is not None:
                break
        else:
            self.round = 0
            winner, timeout = 1, True
            self.say(f"{MAX_RAID_ROUNDS} 回合过去，{self.boss.label}仍未倒下，队伍被迫撤出战斗")
        return {
            "winner": winner,
            "won": winner == 0,
            "timeout": timeout,
            "rounds": self.round or MAX_RAID_ROUNDS,
            "log": self.log,
            "heroes": [
                {
                    "seat": h.seat,
                    "hp": round(max(0, h.hp)),
                    "max_hp": h.max_hp,
                    "alive": h.alive,
                }
                for h in self.heroes
            ],
            "boss": {"hp": round(max(0, self.boss.hp)), "max_hp": self.boss.max_hp},
            "field_events": self.field_events,
        }


def simulate_raid(heroes: list[dict], boss: dict, slot_id: str, cfg: dict, seed: int) -> dict:
    return _RaidBattle(heroes, boss, slot_id, cfg, seed).run()
