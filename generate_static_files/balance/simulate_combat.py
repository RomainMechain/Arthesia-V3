"""Simulateur de combat d'Arthesia.

Fait jouer des combats complets avec le vrai moteur (``back/combat_engine.py``)
sur une horloge virtuelle : un combat de 4 minutes se simule en quelques
millisecondes. Sert à calibrer l'équilibrage (multiplicateurs des monstres
par rôle) et à vérifier un template de monstre généré.

Le joueur simulé est un personnage de référence (``balance.reference_player_stats``,
build REFERENCE_BUILD) avec un kit de compétences de référence. Il réagit avec un délai humain,
lance ses compétences dès qu'elles sont prêtes, se soigne sous 50 % de PV et
boit une potion sous 35 %.

Commandes (depuis Arthesia-V3/) :

    python generate_static_files/balance/simulate_combat.py calibrate
    python generate_static_files/balance/simulate_combat.py check
    python generate_static_files/balance/simulate_combat.py tiers
    python generate_static_files/balance/simulate_combat.py duel --role boss --level 20 --build tank
    python generate_static_files/balance/simulate_combat.py template --monster gardien_englouti --level 7

``calibrate`` cherche, pour chaque rôle, le multiplicateur de PV et la
puissance des attaques qui atteignent les cibles (TARGETS). ``check`` rejoue
les valeurs retenues (balance.ROLE_PROFILES, à mettre à jour après une calibration) sur plusieurs
niveaux et builds.
"""

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from back import balance  # noqa: E402
from back import combat_engine as engine  # noqa: E402
from back.balance import DIFFICULTY_TIERS, GEAR_SHARE, REFERENCE_WEIGHTS, ROLE_PROFILES  # noqa: E402

# Répartition des points de chaque niveau selon le build.
BUILDS = {
    "aventurier": REFERENCE_WEIGHTS,
    "lame-sort": {"hp": 1, "atk": 1.5, "def": 1, "agi": 0.5, "int": 1.5, "sag": 0.5},
    "generaliste": {"hp": 1, "atk": 1, "def": 1, "agi": 1, "int": 1, "sag": 1},
    "guerrier": {"hp": 1, "atk": 2, "def": 1.5, "agi": 1, "int": 0.25, "sag": 0.25},
    "mage": {"hp": 1, "atk": 0.25, "def": 1, "agi": 0.5, "int": 2, "sag": 1.25},
    "tank": {"hp": 2, "atk": 1, "def": 2, "agi": 0.5, "int": 0.25, "sag": 0.25},
    "assassin": {"hp": 1, "atk": 2, "def": 0.5, "agi": 2, "int": 0.25, "sag": 0.25},
}

# Build de référence pour la calibration : un hybride raisonnable. Les
# spécialistes offensifs tuent ~1,5× plus vite, le tank ~2× moins vite mais
# encaisse mieux, le « 1 point partout » est le plus faible.
REFERENCE_BUILD = "aventurier"

# Cibles d'équilibrage, joueur de référence équipé au niveau du monstre.
#   duration : durée médiane d'une victoire (s) ;
#   hp_lost  : PV perdus en moyenne sur une victoire (normal seulement) ;
#   win_rate : part de victoires (élite et boss, avec potions) ;
#   potions  : potions emportées (chacune soigne POTION_HEAL_SHARE des PV max).
TARGETS = {
    "normal": {"duration": 65, "hp_lost": 0.50, "potions": 0},
    "elite": {"duration": 100, "win_rate": 0.55, "potions": 2},
    "boss": {"duration": 240, "win_rate": 0.40, "potions": 5},
}
POTION_HEAL_SHARE = 0.30

REACTION_SECONDS = (0.15, 0.45)
STEP_SECONDS = 0.1
MAX_SECONDS = 900


# -----------------------------------------------------------------------------
# Joueur et monstre de référence
# -----------------------------------------------------------------------------

def reference_mana(level: int) -> float:
    return engine.compute_max_mana(balance.reference_player_stats(level, GEAR_SHARE, BUILDS[REFERENCE_BUILD])["int"], level)


def reference_kit(level: int, build: str) -> dict[str, dict]:
    """Quatre compétences actives (balance.MAX_ACTIVE_SKILL_SLOTS) : une
    rapide, une moyenne, une lente et un soin. Leur coût en mana est une part
    du mana du joueur de référence du niveau. Un build qui mise autant sur ATK
    que sur INT a des compétences des deux types."""
    weights = BUILDS[build]
    main_type = "physical" if weights["atk"] >= weights["int"] else "magical"
    second_type = "magical" if weights["atk"] == weights["int"] else main_type
    mana = reference_mana(level)
    return {
        "frappe": {"id": "frappe", "damage_type": main_type, "mana_cost": round(0.12 * mana),
                   "cooldown_seconds": 6, "effects": [{"type": "damage", "power": 1.2}]},
        "technique": {"id": "technique", "damage_type": second_type, "mana_cost": round(0.22 * mana),
                      "cooldown_seconds": 12, "effects": [{"type": "damage", "power": 2.0}]},
        "assaut": {"id": "assaut", "damage_type": main_type, "mana_cost": round(0.20 * mana),
                   "cooldown_seconds": 10, "effects": [{"type": "damage", "power": 1.74}]},
        "soin": {"id": "soin", "damage_type": "magical", "mana_cost": round(0.20 * mana),
                 "cooldown_seconds": 15, "effects": [{"type": "heal", "power": 1.0}]},
    }


def reference_monster(level: int, role: str, profile: dict | None = None, tier: str = "coeur") -> dict:
    """Monstre de référence : ``balance.monster_base_stats`` de son niveau ×
    multiplicateurs du rôle, attaques en puissance × ATK/INT."""
    profile = profile or ROLE_PROFILES[role]
    base = balance.monster_base_stats(level)
    stats = {stat: round(base[stat] * profile[stat], 1) for stat in balance.STATS}
    p = profile["power"] * DIFFICULTY_TIERS[tier]
    moveset = [
        {"id": "coup", "damage_type": "physical", "power": p, "cooldown_seconds": 2.5, "weight": 3},
        {"id": "assaut", "damage_type": "physical", "power": round(2 * p, 3), "cooldown_seconds": 7, "weight": 1},
    ]
    phases = None
    if role in ("elite", "boss"):
        moveset.append({"id": "sort", "damage_type": "magical", "power": round(1.5 * p, 3), "cooldown_seconds": 5, "weight": 1})
    if role == "boss":
        moveset.append({"id": "furie", "damage_type": "physical", "power": round(3 * p, 3), "cooldown_seconds": 10, "weight": 1})
        phases = [
            {"hp_above_percent": 50, "moveset_ids": ["coup", "assaut", "sort"]},
            {"hp_above_percent": 0, "moveset_ids": ["coup", "assaut", "sort", "furie"]},
        ]
    return {"id": f"{role}_{level}", "name": f"{role} {level}", "level": level, "role": role,
            "stats": stats, "moveset": moveset, "phases": phases}


# -----------------------------------------------------------------------------
# Un combat
# -----------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def fight(monster: dict, level: int, build: str = REFERENCE_BUILD, potions: int = 0,
          stats: dict | None = None, skills: dict | None = None) -> dict:
    """Simule un combat ; renvoie l'issue, la durée et les PV perdus."""
    clock = Clock()
    engine.now = clock
    stats = stats or balance.reference_player_stats(level, GEAR_SHARE, BUILDS[build])
    skills = skills if skills is not None else reference_kit(level, build)
    session = engine.new_session("sim", "sim", "sim", monster, stats, list(skills), [], skills, level)
    potion = {"id": "potion", "consumable": {"effect_type": "heal", "amount": POTION_HEAL_SHARE * session["player_max_hp"]}}
    # Les plus puissantes d'abord.
    damage_skills = sorted((s for s in skills.values() if any(e["type"] == "damage" for e in s["effects"])),
                           key=lambda s: -max(e.get("power", 0) for e in s["effects"]))
    heal_skills = [s for s in skills.values() if any(e["type"] == "heal" for e in s["effects"])]

    next_tick = engine.TICK_INTERVAL_SECONDS
    next_decision = random.uniform(*REACTION_SECONDS)
    potions_used = 0
    while clock.t < MAX_SECONDS:
        clock.t = round(clock.t + STEP_SECONDS, 3)
        if clock.t >= next_tick:
            next_tick += engine.TICK_INTERVAL_SECONDS
            engine.tick(session)
            engine.trigger_passives(session)
            if session["player_current_hp"] <= 0:
                return {"won": False, "duration": clock.t, "hp_lost": 1.0, "potions_used": potions_used}

        if clock.t < next_decision:
            continue
        hp_ratio = session["player_current_hp"] / session["player_max_hp"]
        acted = False
        if hp_ratio < 0.35 and potions_used < potions and clock.t >= session["item_ready_at"]:
            engine.apply_consumable(session, potion)
            session["item_ready_at"] = clock.t + engine.ITEM_COOLDOWN_SECONDS
            potions_used += 1
            acted = True
        candidates = (heal_skills if hp_ratio < 0.5 else []) + damage_skills
        for skill in candidates if not acted else []:
            try:
                engine.use_skill(session, skill["id"])
                acted = True
                break
            except engine.CombatRefusal:
                continue
        if not acted:
            try:
                engine.basic_attack(session)
                acted = True
            except engine.CombatRefusal:
                pass
        if acted:
            next_decision = clock.t + random.uniform(*REACTION_SECONDS)
            if session["monster_current_hp"] <= 0:
                lost = 1 - session["player_current_hp"] / session["player_max_hp"]
                return {"won": True, "duration": clock.t, "hp_lost": lost, "potions_used": potions_used}
            engine.check_phase_change(session)
            engine.trigger_passives(session)
    return {"won": False, "duration": clock.t, "hp_lost": 1 - session["player_current_hp"] / session["player_max_hp"],
            "potions_used": potions_used, "timeout": True}


def run_many(monster: dict, level: int, build: str, potions: int, count: int) -> dict:
    results = [fight(monster, level, build, potions) for _ in range(count)]
    wins = [r for r in results if r["won"]]
    return {
        "win_rate": len(wins) / count,
        "duration": statistics.median(r["duration"] for r in wins) if wins else None,
        "hp_lost": statistics.mean(r["hp_lost"] for r in wins) if wins else 1.0,
        "potions": statistics.mean(r["potions_used"] for r in results),
    }


# -----------------------------------------------------------------------------
# Calibration
# -----------------------------------------------------------------------------

def bisect(low: float, high: float, measure, target: float, increasing: bool, steps: int = 14) -> float:
    """Cherche x tel que measure(x) ≈ target, measure monotone en x."""
    for _ in range(steps):
        mid = (low + high) / 2
        value = measure(mid)
        if (value < target) == increasing:
            low = mid
        else:
            high = mid
    return round((low + high) / 2, 3)


def calibrate(level: int, count: int) -> dict:
    calibrated = {}
    for role, target in TARGETS.items():
        profile = dict(ROLE_PROFILES[role])

        # 1. PV : durée d'une victoire, monstre sans attaque.
        def duration_for(hp_mult):
            trial = {**profile, "hp": hp_mult, "power": 0}
            # Sans victoire (combat trop long) : la durée maximale.
            return run_many(reference_monster(level, role, trial), level, REFERENCE_BUILD, 0, count)["duration"] or MAX_SECONDS
        profile["hp"] = bisect(0.5, 300, duration_for, target["duration"], increasing=True)

        # 2. Puissance : PV perdus (normal) ou part de victoires (élite, boss).
        if "hp_lost" in target:
            def hp_lost_for(power):
                trial = {**profile, "power": power}
                return run_many(reference_monster(level, role, trial), level, REFERENCE_BUILD, 0, count)["hp_lost"]
            profile["power"] = bisect(0.0, 5.0, hp_lost_for, target["hp_lost"], increasing=True)
        else:
            def win_rate_for(power):
                trial = {**profile, "power": power}
                return run_many(reference_monster(level, role, trial), level, REFERENCE_BUILD, target["potions"], count)["win_rate"]
            profile["power"] = bisect(0.0, 5.0, win_rate_for, target["win_rate"], increasing=False)
        calibrated[role] = profile
        print(f"{role:7s} niv. {level:3d} : PV ×{profile['hp']:.2f}, puissance {profile['power']:.3f}", flush=True)
    return calibrated


# -----------------------------------------------------------------------------
# Rapports
# -----------------------------------------------------------------------------

def describe(result: dict) -> str:
    duration = f"{result['duration']:.0f} s" if result["duration"] is not None else "—"
    return (f"victoires {result['win_rate'] * 100:5.1f} %  durée {duration:>6s}  "
            f"PV perdus {result['hp_lost'] * 100:4.0f} %  potions {result['potions']:.1f}")


def check(levels: list[int], count: int) -> None:
    for role, target in TARGETS.items():
        print(f"\n== {role} (potions : {target['potions']}) ==")
        for level in levels:
            monster = reference_monster(level, role)
            for build in BUILDS:
                result = run_many(monster, level, build, target["potions"], count)
                print(f"niv. {level:3d}  {build:11s}  {describe(result)}")


def level_gap(role: str, level: int, count: int) -> None:
    target = TARGETS[role]
    print(f"\n== écart de niveau, {role} niv. {level}, {REFERENCE_BUILD} ==")
    for gap in (-3, -2, -1, 0, 1, 2, 3, 5):
        player_level = max(1, level + gap)
        result = run_many(reference_monster(level, role), player_level, REFERENCE_BUILD, target["potions"], count)
        print(f"joueur niv. {player_level:3d} ({gap:+d})  {describe(result)}")


def tiers(levels: list[int], count: int) -> None:
    print(f"\n== monstres normaux selon la profondeur de la zone, {REFERENCE_BUILD} ==")
    for level in levels:
        for tier in DIFFICULTY_TIERS:
            result = run_many(reference_monster(level, "normal", tier=tier), level, REFERENCE_BUILD, 0, count)
            print(f"niv. {level:3d}  {tier:11s}  {describe(result)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("calibrate", help="calibre PV et puissance de chaque rôle")
    p.add_argument("--levels", type=int, nargs="+", default=[5, 30])
    p.add_argument("--count", type=int, default=150)

    p = sub.add_parser("check", help="rejoue ROLE_PROFILES sur plusieurs niveaux et builds")
    p.add_argument("--levels", type=int, nargs="+", default=[1, 10, 50, 100])
    p.add_argument("--count", type=int, default=200)

    p = sub.add_parser("gap", help="effet de l'écart de niveau joueur / monstre")
    p.add_argument("--role", choices=list(TARGETS), default="normal")
    p.add_argument("--level", type=int, default=20)
    p.add_argument("--count", type=int, default=200)

    p = sub.add_parser("tiers", help="monstres normaux selon la profondeur de la zone")
    p.add_argument("--levels", type=int, nargs="+", default=[1, 3, 5, 10, 30])
    p.add_argument("--count", type=int, default=200)

    p = sub.add_parser("duel", help="un rôle de référence contre un build")
    p.add_argument("--role", choices=list(TARGETS), default="normal")
    p.add_argument("--level", type=int, default=10)
    p.add_argument("--player-level", type=int)
    p.add_argument("--build", choices=list(BUILDS), default=REFERENCE_BUILD)
    p.add_argument("--potions", type=int)
    p.add_argument("--count", type=int, default=300)

    p = sub.add_parser("template", help="un template de monstre contre le joueur de référence")
    p.add_argument("--monster", required=True)
    p.add_argument("--level", type=int, help="niveau du joueur (défaut : celui du monstre)")
    p.add_argument("--potions", type=int, default=0)
    p.add_argument("--count", type=int, default=300)

    args = parser.parse_args()
    if args.command == "calibrate":
        for level in args.levels:
            calibrate(level, args.count)
    elif args.command == "check":
        check(args.levels, args.count)
    elif args.command == "gap":
        level_gap(args.role, args.level, args.count)
    elif args.command == "tiers":
        tiers(args.levels, args.count)
    elif args.command == "duel":
        potions = args.potions if args.potions is not None else TARGETS[args.role]["potions"]
        monster = reference_monster(args.level, args.role)
        print(describe(run_many(monster, args.player_level or args.level, args.build, potions, args.count)))
    elif args.command == "template":
        path = ROOT / "templates" / "monsters" / f"{args.monster}.json"
        monster = json.loads(path.read_text(encoding="utf-8"))
        level = args.level or monster.get("level", 1)
        for build in BUILDS:
            print(f"{build:11s}  {describe(run_many(monster, level, build, args.potions, args.count))}")


if __name__ == "__main__":
    main()
