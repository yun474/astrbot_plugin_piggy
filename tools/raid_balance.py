"""Report raid boss win rates so the scaling in core/raid.py can be tuned.

Usage:
  python tools/raid_balance.py [--levels 3,8,15] [--trials 300]

Four random catalog pigs of the same level play whole dungeons, always
choosing to continue: health carries over, the party rests between stages
and the usual random events are rolled. For each boss it prints the win rate
among parties that reached it (targets about 75% / 55% / 35%), then the
win rate of a fresh full-health party with no events, and finally how often
all three bosses fall.
"""

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.battle import fighter, validate_battle  # noqa: E402
from core.raid import (  # noqa: E402
    DUNGEONS,
    REST_HEAL,
    boss_fighter,
    boss_level,
    mechanics_for,
    simulate_raid,
)
from core.raid_events import (  # noqa: E402
    new_config,
    new_rewards,
    resolve,
    roll_entry,
    roll_interlude,
)

RESOURCES = ROOT / "resources"
TARGETS = (0.75, 0.55, 0.35)


def load():
    pigs = {p["id"]: p for p in json.loads((RESOURCES / "pigs.json").read_text("utf-8"))}
    raw = json.loads((RESOURCES / "battle.json").read_text("utf-8"))
    return pigs, validate_battle(raw, pigs)


def party(pigs, data, ids, level, rng, hp=None):
    heroes = []
    for seat, pig_id in enumerate(rng.sample(ids, 4)):
        hero = fighter(pigs[pig_id], data[pig_id], level, f"{seat}号{pigs[pig_id]['name']}")
        hero["seat"] = seat
        if hp:
            hero["start_hp"] = max(1, round(hero["stats"]["hp"] * hp[seat]))
        heroes.append(hero)
    return heroes


def boss(pigs, data, slot, level, stage):
    return boss_fighter(
        pigs[slot], data[slot], boss_level([level] * 4), stage, slot, pigs[slot]["name"]
    )


def stage_rate(pigs, data, ids, slot, stage, level, trials, seed):
    rng = random.Random(seed)
    wins = rounds = 0
    for trial in range(trials):
        heroes = party(pigs, data, ids, level, rng)
        result = simulate_raid(
            heroes, boss(pigs, data, slot, level, stage), slot, new_config(), seed + trial
        )
        wins += result["won"]
        rounds += result["rounds"]
    return wins / trials, rounds / trials


def run_rates(pigs, data, ids, info, level, trials, seed):
    """Per-stage win rate among parties that reached the stage, and the clear rate."""
    rng = random.Random(seed)
    reached, won = [0, 0, 0], [0, 0, 0]
    for trial in range(trials):
        team = rng.sample(ids, 4)
        state = [{"seat": s, "label": str(s), "hp": 1.0, "mods": {}} for s in range(4)]
        alive = set(range(4))
        for stage, slot in enumerate(info["bosses"], 1):
            members = [m for m in state if m["seat"] in alive]
            cfg, rewards = new_config(), new_rewards()
            mechanics = mechanics_for(slot).MECHANICS
            if stage > 1:
                for member in members:
                    member["hp"] = min(1.0, member["hp"] + REST_HEAL)
                resolve([roll_interlude(rng)], members, rng, cfg, rewards, mechanics)
            resolve(roll_entry(rng, info["key"]), members, rng, cfg, rewards, mechanics)
            heroes = []
            for member in members:
                pig_id = team[member["seat"]]
                hero = fighter(pigs[pig_id], data[pig_id], level, str(member["seat"]))
                hero.update(
                    seat=member["seat"],
                    start_hp=max(1, round(hero["stats"]["hp"] * member["hp"])),
                    mods=member["mods"],
                )
                heroes.append(hero)
            if cfg.get("ally"):
                ally = rng.choice(ids)
                heroes.append(fighter(pigs[ally], data[ally], level, "援军"))
            result = simulate_raid(
                heroes, boss(pigs, data, slot, level, stage), slot, cfg, seed + trial * 10 + stage
            )
            reached[stage - 1] += 1
            for hero in result["heroes"]:
                if hero["seat"] is None:
                    continue
                if hero["alive"]:
                    state[hero["seat"]]["hp"] = hero["hp"] / hero["max_hp"]
                else:
                    alive.discard(hero["seat"])
            won[stage - 1] += result["won"]
            if not result["won"] or not alive:
                break
    rates = [w / r if r else 0.0 for w, r in zip(won, reached)]
    return rates, won[2] / trials


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--levels", default="3,8,15")
    parser.add_argument("--trials", type=int, default=300)
    args = parser.parse_args()
    pigs, data = load()
    ids = sorted(data)
    for level in (int(v) for v in args.levels.split(",")):
        print(f"Lv{level} party (targets {' / '.join(f'{t:.0%}' for t in TARGETS)})")
        for info in DUNGEONS:
            rates, cleared = run_rates(pigs, data, ids, info, level, args.trials, level * 7)
            cells = []
            for stage, slot in enumerate(info["bosses"], 1):
                fresh, rounds = stage_rate(
                    pigs, data, ids, slot, stage, level, args.trials, level * 1000 + stage
                )
                cells.append(
                    f"{pigs[slot]['name']} {rates[stage - 1]:4.0%} (fresh {fresh:4.0%}, {rounds:4.1f}r)"
                )
            print(f"  {info['name']}: " + " | ".join(cells) + f" | clear {cleared:.0%}")


if __name__ == "__main__":
    main()
