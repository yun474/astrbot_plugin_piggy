"""Signature mechanics of the nine raid bosses.

Each boss is its own class; the raid engine calls the hooks below at fixed
moments. `battle` is the running raid battle (see core.raid), `boss` is
`battle.boss`. Messages go through `battle.note` so they land after the action
that triggered them.
"""


class BossMechanics:
    pig_id = ""
    # (name, description) for each of the three mechanics.
    MECHANICS: tuple = ()
    # Per-boss multipliers on top of the shared raid scaling.
    HP = 1.0
    ATK = 1.0
    DEF = 1.0

    def __init__(self, disabled: int | None = None):
        self.disabled = disabled

    def on(self, index: int) -> bool:
        return index != self.disabled

    def setup(self, battle):
        pass

    def round_start(self, battle):
        pass

    def round_end(self, battle):
        pass

    def before_act(self, battle) -> bool:
        """Return True when the mechanic replaces the boss's normal action."""
        return False

    def extra_actions(self, battle) -> int:
        return 0

    def pick_target(self, battle, candidates: list):
        return battle.rng.choice(candidates)

    def incoming(self, battle, attacker, raw: float, effect: dict) -> float:
        return raw

    def crit_against(self, battle, attacker) -> bool:
        return True

    def pierce(self, battle, value: float) -> float:
        return value

    def outgoing(self, battle, target, dealt: float):
        pass

    def damaged(self, battle):
        pass

    def hero_fell(self, battle, hero):
        pass

    def dispelled(self, battle):
        pass


class Goblin(BossMechanics):
    pig_id = "goblin-pig"
    MECHANICS = (
        (
            "角落潜伏",
            "每 3 回合钻回角落 1 回合，这回合打它全部落空；出来时偷袭生命最低的队员，必定暴击",
        ),
        ("邋遢光环", "每回合给全体队员叠 1 层熏味，每层速度 -5%，最多 6 层，清除负面效果可以驱散"),
        ("摆烂", "生命第一次低于 30% 时躺平 2 回合回血，但躺平期间受到的伤害 +30%"),
    )
    HP = 0.85

    def __init__(self, disabled=None):
        super().__init__(disabled)
        self.lurking = False
        self.flat = False

    def round_start(self, battle):
        boss = battle.boss
        if self.on(0):
            if self.lurking:
                self.lurking = False
                targets = battle.alive_heroes()
                if targets:
                    target = min(targets, key=lambda h: h.ratio)
                    dealt = battle.strike(boss, target, 1.2, crit=True)
                    battle.note(
                        f"{boss.label}从角落里窜出来偷袭 {target.label}，暴击造成 {round(dealt)} 伤害"
                    )
            elif battle.round % 3 == 0:
                self.lurking = True
                boss.stun = max(boss.stun, 1)
                boss.stun_label = "躲在角落"
                battle.note(f"{boss.label}钻回了角落，这回合谁也打不到它")
        if self.on(1):
            for hero in battle.alive_heroes():
                stacks = sum(1 for b in hero.buffs if b.get("tag") == "stench")
                if stacks < 6:
                    hero.buffs.append({"stat": "spd", "pct": -5, "turns": 99, "tag": "stench"})
            if battle.round == 1:
                battle.note("邋遢光环弥漫开来：一股说不清的味道，全队每回合速度 -5%")

    def incoming(self, battle, attacker, raw, effect):
        if self.lurking:
            battle.void_note = f"{battle.boss.label}躲在角落里"
            return 0
        if self.flat and battle.boss.stun and battle.boss.stun_label == "摆烂躺平":
            raw *= 1.3
        return raw

    def damaged(self, battle):
        boss = battle.boss
        if self.on(2) and not self.flat and boss.ratio < 0.3:
            self.flat = True
            boss.stun = max(boss.stun, 2)
            boss.stun_label = "摆烂躺平"
            boss.regens.append({"pct": 8, "turns": 2})
            battle.note(f"{boss.label}开始摆烂：躺在角落什么都不管，慢慢回血，但浑身都是破绽")


class Frozen(BossMechanics):
    pig_id = "frozen-pig"
    MECHANICS = (
        ("寒气层数", "每次攻击给目标叠 1 层寒冷，叠满 3 层冻结 1 回合"),
        ("冰块外壳", "开场自带 25% 生命的冰壳，冰壳在时不会被暴击，伤害先扣冰壳"),
        ("融化", "冰壳碎掉后防御 -30%、攻击 +20%"),
    )
    HP = 0.7

    def __init__(self, disabled=None):
        super().__init__(disabled)
        self.shell = 0.0

    def setup(self, battle):
        if self.on(1):
            self.shell = battle.boss.max_hp * 0.25
            battle.note(f"{battle.boss.label}裹着一层 {round(self.shell)} 点的冰块外壳：生人勿近")

    def crit_against(self, battle, attacker):
        return self.shell <= 0

    def incoming(self, battle, attacker, raw, effect):
        if self.shell <= 0:
            return raw
        absorbed = min(self.shell, raw)
        self.shell -= absorbed
        raw -= absorbed
        if self.shell <= 0:
            self.melt(battle)
        elif raw <= 0:
            battle.void_note = f"冰壳吸收了 {round(absorbed)} 伤害"
        return raw

    def melt(self, battle):
        boss = battle.boss
        if self.on(2):
            boss.mods["def"] = boss.mods.get("def", 0) - 30
            boss.mods["atk"] = boss.mods.get("atk", 0) + 20
            battle.note(
                f"冰块外壳碎了……外表冷硬，内心却渴望温暖。{boss.label}融化了：防御 -30%，攻击 +20%"
            )
        else:
            battle.note("冰块外壳碎了")

    def outgoing(self, battle, target, dealt):
        if not self.on(0) or not target.alive:
            return
        target.chill += 1
        if target.chill < 3:
            return
        target.chill = 0
        if target.frost_ward:
            target.frost_ward = False
            battle.note(f"篝火的余温护住了 {target.label}，没有被冻住")
            return
        target.stun = max(target.stun, 1)
        target.stun_label = "冻结"
        battle.note(f"寒气叠满 3 层，{target.label} 被冻成了冰块")


class Everest(BossMechanics):
    pig_id = "everest-pig"
    MECHANICS = (
        ("海拔上升", "每回合全队速度 -4%，可以累积；第 12 回合起全队缺氧，每回合损失 4% 生命"),
        ("不可逾越", "单次受到的伤害最多只算它最大生命的 6%，多段和持续伤害更划算"),
        ("雪崩", "第 5、10、15 回合对全队造成 1.2 倍攻击伤害，每只猪 30% 概率臣服跳过下一回合"),
    )
    HP = 0.5
    DEF = 1.1

    def round_start(self, battle):
        boss = battle.boss
        if self.on(0):
            for hero in battle.alive_heroes():
                hero.mods["spd"] = hero.mods.get("spd", 0) - 4
            if battle.round == 1:
                battle.note("海拔越来越高，空气越来越稀薄，全队每回合都会慢一点")
            if battle.round >= 12:
                lost = [
                    round(battle.hurt(hero, hero.max_hp * 0.04)) for hero in battle.alive_heroes()
                ]
                if lost:
                    battle.note(f"高处不胜寒，全队缺氧，各损失约 {max(lost)} 生命")
        if self.on(2) and battle.round in (5, 10, 15):
            parts = []
            for hero in battle.alive_heroes():
                dealt = battle.strike(boss, hero, 1.2)
                text = f"{hero.label} -{round(dealt)}"
                if hero.alive and battle.rng.random() < 0.3:
                    hero.stun = max(hero.stun, 1)
                    hero.stun_label = "臣服"
                    text += "（臣服）"
                parts.append(text)
            if parts:
                battle.note("雪峰压顶，雪崩来了！" + "，".join(parts))

    def incoming(self, battle, attacker, raw, effect):
        if self.on(1):
            cap = battle.boss.max_hp * 0.06
            if raw > cap:
                return cap
        return raw


class Error404(BossMechanics):
    pig_id = "error-404-pig"
    MECHANICS = (
        (
            "薛定谔状态",
            "每回合 50% 进入未被观测，此时受到的每次伤害 50% 作废；必中和真实伤害不受影响",
        ),
        ("页面不存在", "每 4 回合让一只队员猪 404，它的下一次行动变成原地刷新，并损失 5% 生命"),
        ("缓存回滚", "生命第一次低于 40% 时回滚到 60%，同时清除身上的持续伤害"),
    )
    HP = 1.85

    def __init__(self, disabled=None):
        super().__init__(disabled)
        self.hidden = False
        self.rolled = False

    def round_start(self, battle):
        boss = battle.boss
        if self.on(0):
            self.hidden = battle.rng.random() < 0.5
            if self.hidden:
                battle.note(f"{boss.label}进入未被观测状态：没猪能确定它是否存在")
        if self.on(1) and battle.round % 4 == 0:
            targets = battle.alive_heroes()
            if targets:
                target = battle.rng.choice(targets)
                target.glitch = True
                battle.note(f"页面不存在：{target.label} 的下一次行动返回了 404")

    def incoming(self, battle, attacker, raw, effect):
        if (
            self.hidden
            and not (effect.get("true") or effect.get("sure"))
            and battle.rng.random() < 0.5
        ):
            battle.void_note = "薛定谔：这一击没被观测到"
            return 0
        return raw

    def damaged(self, battle):
        boss = battle.boss
        if self.on(2) and not self.rolled and boss.ratio < 0.4:
            self.rolled = True
            boss.hp = boss.max_hp * 0.6
            boss.dots.clear()
            battle.note(f"缓存回滚！{boss.label}疯狂刷新，生命回到 60%，持续伤害也被清掉了")


class Mechanical(BossMechanics):
    pig_id = "mechanical-pig"
    MECHANICS = (
        ("光学锁定", "每 3 回合锁定当前生命最高的队员，下回合对它打出必中的 2 倍伤害"),
        ("金属外壳", "防御很高，无视防御效果对它翻倍；每挨 6 次攻击会过热，防御归零 2 回合"),
        ("系统 bug", "每回合 10% 概率忘了自己是猪，原地哼哼一回合"),
    )
    HP = 1.1
    DEF = 1.6

    def __init__(self, disabled=None):
        super().__init__(disabled)
        self.locked = None
        self.lock_round = 0
        self.hits = 0
        self.overheat = 0

    def round_start(self, battle):
        if self.on(0) and battle.round % 3 == 0:
            targets = battle.alive_heroes()
            if targets:
                self.locked = max(targets, key=lambda h: h.hp)
                self.lock_round = battle.round
                battle.note(f"红色光学眼锁定了 {self.locked.label}，运算精准")

    def before_act(self, battle):
        boss = battle.boss
        if self.locked is not None and battle.round == self.lock_round + 1:
            target, self.locked = self.locked, None
            if target.alive:
                dealt = battle.strike(boss, target, 2.0)
                battle.say(
                    f"{boss.label}按锁定坐标开火，对 {target.label} 造成 {round(dealt)} 伤害"
                )
                return True
        if self.on(2) and battle.rng.random() < 0.1:
            battle.say(f"{boss.label}忘了自己其实是只猪，原地哼哼了一回合")
            return True
        return False

    def pierce(self, battle, value):
        return min(100, value * 2) if self.on(1) else value

    def incoming(self, battle, attacker, raw, effect):
        if self.on(1) and not self.overheat:
            self.hits += 1
            if self.hits >= 6:
                self.hits = 0
                self.overheat = 2
                battle.boss.zeroed.add("def")
                battle.note("金属外壳过热冒烟了！防御归零 2 回合")
        return raw

    def round_end(self, battle):
        if self.locked is not None and battle.round > self.lock_round:
            self.locked = None
        if self.overheat:
            self.overheat -= 1
            if not self.overheat:
                battle.boss.zeroed.discard("def")
                battle.note("金属外壳冷却完毕，防御恢复")


class Cyberpunk(BossMechanics):
    pig_id = "cyberpunk-pig"
    MECHANICS = (
        ("黑客入侵", "每 3 回合清除全队的增益、护盾和反弹"),
        ("义体进化", "每回合攻击 +4%，一直累积；驱散效果能把累积的加成清零"),
        ("超频过载", "生命低于 40% 后每回合行动 2 次，但每回合损失 4% 生命"),
    )
    HP = 1.1

    def __init__(self, disabled=None):
        super().__init__(disabled)
        self.evolved = 0
        self.overclock = False

    def round_start(self, battle):
        boss = battle.boss
        if self.on(1):
            self.evolved += 4
            boss.mods["atk"] = boss.mods.get("atk", 0) + 4
            if self.evolved in (4, 20, 40, 60):
                battle.note(f"义体改造进行中：{boss.label}的攻击已累计 +{self.evolved}%")
        if self.on(0) and battle.round % 3 == 0:
            for hero in battle.alive_heroes():
                hero.buffs = [b for b in hero.buffs if b["pct"] < 0]
                hero.shield = 0
                hero.thorns.clear()
                hero.evade = 0
            battle.note("黑客入侵！全队的增益、护盾和反弹都被删掉了")

    def dispelled(self, battle):
        if self.evolved:
            battle.boss.mods["atk"] = battle.boss.mods.get("atk", 0) - self.evolved
            battle.note(f"驱散生效，{battle.boss.label}累计 +{self.evolved}% 的义体进化被清零")
            self.evolved = 0

    def damaged(self, battle):
        if self.on(2) and not self.overclock and battle.boss.ratio < 0.4:
            self.overclock = True
            battle.note(f"{battle.boss.label}处理器超频运行！之后每回合行动 2 次")

    def extra_actions(self, battle):
        return 1 if self.overclock else 0

    def round_end(self, battle):
        boss = battle.boss
        if self.overclock and boss.alive:
            lost = battle.hurt(boss, boss.max_hp * 0.04)
            battle.note(f"超频过热，{boss.label}损失 {round(lost)} 生命")


class Demon(BossMechanics):
    pig_id = "demon-pig"
    MECHANICS = (
        ("满肚子坏点子", "开场给每只队员猪随机挂一个诅咒：虚弱、迟缓、流血或易伤"),
        ("恶作剧", "每 4 回合把两只队员猪的生命比例对调"),
        ("得逞的坏笑", "每有一只队员猪倒下，它回复 15% 生命、攻击 +10%"),
    )
    HP = 2.1
    CURSES = ("虚弱", "迟缓", "流血", "易伤")

    def setup(self, battle):
        if not self.on(0):
            return
        parts = []
        for hero in battle.alive_heroes():
            curse = battle.rng.choice(self.CURSES)
            if curse == "虚弱":
                hero.buffs.append({"stat": "atk", "pct": -20, "turns": 99})
            elif curse == "迟缓":
                hero.buffs.append({"stat": "spd", "pct": -25, "turns": 99})
            elif curse == "流血":
                hero.dots.append({"pct": 3, "turns": 8, "label": "流血"})
            else:
                hero.vuln += 20
            parts.append(f"{hero.label}「{curse}」")
        if parts:
            battle.note("满肚子坏点子：" + "，".join(parts))

    def round_start(self, battle):
        heroes = battle.alive_heroes()
        if self.on(1) and battle.round % 4 == 0 and len(heroes) >= 2:
            a, b = battle.rng.sample(heroes, 2)
            ra, rb = a.ratio, b.ratio
            a.hp, b.hp = a.max_hp * rb, b.max_hp * ra
            battle.note(f"恶作剧！{a.label} 和 {b.label} 的生命被对调了（{ra:.0%} ⇄ {rb:.0%}）")

    def hero_fell(self, battle, hero):
        boss = battle.boss
        if self.on(2) and boss.alive:
            healed = battle.heal(boss, boss.max_hp * 0.15)
            boss.mods["atk"] = boss.mods.get("atk", 0) + 10
            battle.note(f"{boss.label}露出得逞的坏笑，回复 {healed} 生命，攻击 +10%")


class ChainedKing(BossMechanics):
    pig_id = "chained_crown_pig"
    MECHANICS = (
        (
            "三重锁链",
            "开场被 3 条锁链束缚，攻击、速度 -30%；生命每降 25% 挣断 1 条，压制减少 10%，并冲击全队",
        ),
        ("王者归来", "3 条锁链全断时恢复全部属性，回复 15% 生命"),
        ("王者威压", "每 4 回合让速度最低的队员猪臣服，跳过 1 回合"),
    )
    HP = 0.85

    def __init__(self, disabled=None):
        super().__init__(disabled)
        self.chains = 0
        self.returned = False

    def setup(self, battle):
        if self.on(0):
            self.chains = 3
            boss = battle.boss
            boss.mods["atk"] = boss.mods.get("atk", 0) - 30
            boss.mods["spd"] = boss.mods.get("spd", 0) - 30
            battle.note(f"{boss.label}被 3 条锁链束缚着：欲戴王冠，必承其重")

    def damaged(self, battle):
        boss = battle.boss
        while self.chains and boss.ratio <= 0.25 * self.chains:
            self.chains -= 1
            boss.mods["atk"] += 10
            boss.mods["spd"] += 10
            hits = [
                f"{hero.label} -{round(battle.strike(boss, hero, 0.7))}"
                for hero in battle.alive_heroes()
            ]
            battle.note(f"锁链崩断一条（剩 {self.chains} 条），冲击波扫过全队：" + "，".join(hits))
            if not self.chains:
                self.king_returns(battle)
        if not self.on(0) and not self.returned and boss.ratio <= 0.25:
            self.king_returns(battle)

    def king_returns(self, battle):
        if not self.on(1) or self.returned:
            return
        self.returned = True
        boss = battle.boss
        healed = battle.heal(boss, boss.max_hp * 0.15)
        battle.note(f"挣断锁链，王者归来！{boss.label}恢复全部属性，回复 {healed} 生命")

    def round_start(self, battle):
        heroes = battle.alive_heroes()
        if self.on(2) and battle.round % 4 == 0 and heroes:
            target = min(heroes, key=lambda h: h.stat("spd"))
            target.stun = max(target.stun, 1)
            target.stun_label = "臣服"
            battle.note(f"王者威压：{target.label} 被震慑得跪了下来")


class PigGod(BossMechanics):
    pig_id = "pig_god"
    MECHANICS = (
        ("好运加持", "暴击率 +25；每次被暴击有 30% 概率靠好运抵消"),
        ("神谕", "每 3 回合看穿全队并预告下回合的目标，下一回合全队闪避归零"),
        ("守护神降临", "生命第一次低于 50% 时召唤 2 只小猪守护灵，守护灵在场时必须先打守护灵"),
    )
    HP = 0.5

    def __init__(self, disabled=None):
        super().__init__(disabled)
        self.blind_round = 0
        self.prophecy = None
        self.summoned = False

    def setup(self, battle):
        if self.on(0):
            battle.boss.mods["crit"] = battle.boss.mods.get("crit", 0) + 25

    def crit_against(self, battle, attacker):
        if self.on(0) and battle.rng.random() < 0.3:
            battle.note(f"好运加持：{battle.boss.label}靠运气抵消了一次暴击")
            return False
        return True

    def round_start(self, battle):
        if not self.on(1):
            return
        if battle.round == self.blind_round:
            for hero in battle.alive_heroes():
                hero.blind = True
        if battle.round % 3 == 0:
            targets = battle.alive_heroes()
            if targets:
                self.blind_round = battle.round + 1
                self.prophecy = battle.rng.choice(targets)
                battle.note(f"神谕：下回合全队无处可躲，{self.prophecy.label} 将首当其冲")

    def pick_target(self, battle, candidates):
        if (
            self.prophecy is not None
            and self.prophecy.alive
            and battle.round == self.blind_round
            and self.prophecy in candidates
        ):
            return self.prophecy
        return battle.rng.choice(candidates)

    def damaged(self, battle):
        if self.on(2) and not self.summoned and battle.boss.ratio < 0.5:
            self.summoned = True
            for index in (1, 2):
                battle.summon(f"小猪守护灵{index}", 0.15, 0.5)
            battle.note("守护神降临！两只小猪守护灵挡在了神明面前，得先打倒它们")


BOSSES = {
    cls.pig_id: cls
    for cls in (
        Goblin,
        Frozen,
        Everest,
        Error404,
        Mechanical,
        Cyberpunk,
        Demon,
        ChainedKing,
        PigGod,
    )
}
