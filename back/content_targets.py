"""Chiffres cibles du contenu généré (monstres, objets, quêtes, compétences).

Module pur : les scripts de génération (``generate_static_files/``) l'importent
pour joindre à chaque appel les valeurs exactes du niveau voulu, plutôt que de
laisser le modèle les calculer. Les règles sont décrites dans
``generate_static_files/arthesia_balance_reference.md``.
"""

try:
    from . import balance
    from .combat_engine import compute_max_mana
except ImportError:
    import balance
    from combat_engine import compute_max_mana

# Étages : environ dix niveaux chacun, le boss au dernier.
LEVELS_PER_FLOOR = 10

# Attaques des monstres : l'attaque principale (recharge MAIN_MOVE_COOLDOWN)
# a la puissance du rôle ; une attaque plus lente frappe plus fort, sans
# dépasser le même rythme de dégâts (puissance ≈ P × recharge / 3,5).
MAIN_MOVE_COOLDOWN = 2.5
SLOW_MOVE_SECONDS_PER_POWER = 3.5

# Équipement : sept emplacements de combat se partagent la part de puissance
# de l'équipement (balance.GEAR_SHARE). L'outil ne donne pas de stats de combat.
COMBAT_SLOTS = ("weapon", "head", "chest", "gloves", "boots", "ring", "amulet")
# Un objet fabriqué très cohérent peut dépasser le budget, jamais plus de ×2.
RARE_CRAFT_MULTIPLIER = (1.5, 2.0)

# Valeur de base (or) par niveau et par catégorie ; le rachat au serveur en
# vaut 35 %.
BASE_VALUE_PER_LEVEL = {"material": 5, "consumable": 15, "equipment": 30}

# Consommables : part des PV / du mana du joueur de référence rendue.
CONSUMABLE_SHARE = {"heal": 0.30, "restore_mana": 0.35}

# Or d'une quête, par niveau, selon son ampleur.
QUEST_GOLD_PER_LEVEL = {"small": 3, "normal": 6, "major": 15}
# Objectifs d'une quête (nombre de monstres ou de récoltes), selon son ampleur.
QUEST_OBJECTIVE_COUNTS = {"small": [3, 5], "normal": [6, 10], "major": [12, 20]}

# Compétences : puissance ≈ SKILL_POWER_RATE × recharge^0,75 (1,2 à 6 s,
# 2,0 à 12 s : le kit calibré par le simulateur), × le palier.
SKILL_POWER_RATE = 0.31
SKILL_TIER_MULTIPLIER = {"simple": 1.0, "difficile": 1.25, "exceptionnelle": 1.6}
# Un soin ou un bouclier vaut la moitié d'une attaque de même recharge.
SKILL_SUPPORT_SHARE = 0.5
# Coût en mana : 2 % du mana du joueur de référence par seconde de recharge,
# entre 8 % et 25 %.
SKILL_MANA_SHARE_PER_SECOND = 0.02
SKILL_MANA_SHARE_BOUNDS = (0.08, 0.25)
# Passifs permanents (stat_bonus) : part de la puissance totale du joueur de
# référence (points de niveau et équipement), avec un plancher en points.
PASSIVE_POWER_SHARE = {"simple": 0.04, "difficile": 0.08, "exceptionnelle": 0.15}
PASSIVE_MIN_POINTS = {"simple": 1.5, "difficile": 3, "exceptionnelle": 6}
PASSIVE_PERCENT = {"simple": [3, 5], "difficile": [6, 10], "exceptionnelle": [12, 20]}


def floor_level_range(floor_number: int) -> list[int]:
    first = (floor_number - 1) * LEVELS_PER_FLOOR + 1
    return [first, first + LEVELS_PER_FLOOR - 1]


def reference_player(level: int) -> dict:
    """Joueur de référence équipé : celui contre lequel les monstres sont calibrés."""
    return balance.reference_build_stats(level)


def reference_mana(level: int) -> float:
    return compute_max_mana(reference_player(level)["int"], level)


def monster_targets(level: int, role: str, tier: str = "coeur") -> dict:
    """Stats et puissance d'attaque d'un monstre. La profondeur de la zone
    (``tier``) ne joue que sur les monstres normaux : élites et boss gardent
    leur difficulté."""
    profile = balance.ROLE_PROFILES[role]
    base = balance.monster_base_stats(level)
    stats = {stat: round(base[stat] * profile[stat]) for stat in balance.STATS}
    power = profile["power"] * (balance.DIFFICULTY_TIERS[tier] if role == "normal" else 1.0)
    return {
        "stats": stats,
        "main_move": {"power": round(power, 2), "cooldown_seconds": MAIN_MOVE_COOLDOWN},
        "slow_move_power_per_second": round(power / SLOW_MOVE_SECONDS_PER_POWER, 3),
        "xp": balance.kill_xp(level, role),
    }


def equipment_points(level: int) -> float:
    """Budget d'un objet équipable de combat, en points équivalents."""
    level_points = balance.STAT_POINTS_PER_LEVEL * level
    gear_points = level_points * balance.GEAR_SHARE / (1 - balance.GEAR_SHARE)
    return round(gear_points / len(COMBAT_SLOTS), 1)


def base_value(level: int, category: str) -> int:
    return BASE_VALUE_PER_LEVEL.get(category, BASE_VALUE_PER_LEVEL["material"]) * max(level, 1)


def consumable_amount(level: int, effect_type: str) -> int:
    if effect_type == "restore_mana":
        return round(reference_mana(level) * CONSUMABLE_SHARE["restore_mana"])
    return round(reference_player(level)["hp"] * CONSUMABLE_SHARE["heal"])


def quest_rewards(level: int) -> dict:
    return {
        size: {"xp": balance.quest_xp(level, size), "gold": max(5, QUEST_GOLD_PER_LEVEL[size] * level)}
        for size in balance.QUEST_XP_SHARE
    }


def skill_power(cooldown: float, tier: str = "simple", support: bool = False) -> float:
    power = SKILL_POWER_RATE * max(cooldown, 1) ** 0.75 * SKILL_TIER_MULTIPLIER[tier]
    return round(power * (SKILL_SUPPORT_SHARE if support else 1), 2)


def skill_mana_cost(level: int, cooldown: float) -> int:
    low, high = SKILL_MANA_SHARE_BOUNDS
    share = min(high, max(low, SKILL_MANA_SHARE_PER_SECOND * cooldown))
    return round(reference_mana(level) * share)


def level_targets(level: int, tier: str = "coeur") -> dict:
    """Tout ce qu'un générateur de contenu doit respecter à ce niveau."""
    return {
        "level": level,
        "reference_player": reference_player(level),
        "monsters": {role: monster_targets(level, role, tier) for role in balance.ROLE_PROFILES},
        "equipment_points_per_piece": equipment_points(level),
        "base_value": {category: base_value(level, category) for category in BASE_VALUE_PER_LEVEL},
        "consumable_amount": {effect: consumable_amount(level, effect) for effect in CONSUMABLE_SHARE},
        "quest_rewards": quest_rewards(level),
        "profession_min_level": balance.profession_level_for(level),
    }


def zone_targets(level_range: list[int], tier: str = "coeur") -> dict:
    low, high = level_range[0], level_range[-1]
    return {
        "difficulty_tier": tier,
        "quest_objective_counts": QUEST_OBJECTIVE_COUNTS,
        "levels": [level_targets(level, tier) for level in range(max(low, 1), max(high, low, 1) + 1)],
    }


def passive_points(level: int, tier: str) -> float:
    total = balance.STAT_POINTS_PER_LEVEL * level / (1 - balance.GEAR_SHARE)
    return round(max(PASSIVE_MIN_POINTS[tier], total * PASSIVE_POWER_SHARE[tier]), 1)


def skill_targets(level_range: list[int]) -> dict:
    """Repères des compétences d'un étage : ATK/INT/SAG et mana du joueur de
    référence par niveau, puissance selon la recharge et le palier."""
    cooldowns = (3, 6, 10, 15, 20)
    return {
        "power_by_cooldown": {
            tier: {str(cd): skill_power(cd, tier) for cd in cooldowns} for tier in SKILL_TIER_MULTIPLIER
        },
        "support_power_by_cooldown": {
            tier: {str(cd): skill_power(cd, tier, support=True) for cd in cooldowns} for tier in SKILL_TIER_MULTIPLIER
        },
        "passive_percent": PASSIVE_PERCENT,
        "levels": [
            {
                "level": level,
                "atk": reference_player(level)["atk"],
                "int": reference_player(level)["int"],
                "sag": reference_player(level)["sag"],
                "max_mana": round(reference_mana(level)),
                "mana_cost_by_cooldown": {str(cd): skill_mana_cost(level, cd) for cd in cooldowns},
                "passive_points": {tier: passive_points(level, tier) for tier in PASSIVE_POWER_SHARE},
            }
            for level in range(max(level_range[0], 1), max(level_range[-1], level_range[0], 1) + 1)
        ],
    }


def craft_targets(level: int) -> dict:
    """Repères du résultat d'un craft du niveau ``level``."""
    points = equipment_points(level)
    return {
        "level": level,
        "equipment_points": {
            "normal": points,
            "rare": [round(points * RARE_CRAFT_MULTIPLIER[0], 1), round(points * RARE_CRAFT_MULTIPLIER[1], 1)],
        },
        "base_value": {category: base_value(level, category) for category in BASE_VALUE_PER_LEVEL},
        "consumable_amount": {effect: consumable_amount(level, effect) for effect in CONSUMABLE_SHARE},
    }


def zone_difficulty_tier(skeleton: dict, zone_id: str) -> str:
    """Profondeur d'une zone dans son étage : ``generation_brief.difficulty_tier``
    du squelette, sinon déduite du nombre de sauts depuis la zone de départ
    (premier tiers : entrée, dernier tiers : profondeurs)."""
    zone = next((z for z in skeleton.get("zones", []) if z["id"] == zone_id), {})
    tier = zone.get("generation_brief", {}).get("difficulty_tier")
    if tier in balance.DIFFICULTY_TIERS:
        return tier

    neighbours: dict[str, set] = {}
    for a, b in skeleton.get("connections", []):
        neighbours.setdefault(a, set()).add(b)
        neighbours.setdefault(b, set()).add(a)
    distance = {skeleton.get("start_zone_id"): 0}
    frontier = [skeleton.get("start_zone_id")]
    while frontier:
        current = frontier.pop(0)
        for other in neighbours.get(current, ()):
            if other not in distance:
                distance[other] = distance[current] + 1
                frontier.append(other)

    # Le boss et le passage ne comptent pas : ils ferment l'étage.
    closing = {skeleton.get("boss_zone_id")} | {z["id"] for z in skeleton.get("zones", []) if z.get("type") == "passage"}
    deepest = max((d for z, d in distance.items() if z not in closing), default=1) or 1
    ratio = distance.get(zone_id, deepest) / deepest
    if ratio <= 1 / 3:
        return "entree"
    if ratio > 2 / 3:
        return "profondeurs"
    return "coeur"


# -----------------------------------------------------------------------------
# Contrôle d'un template généré
# -----------------------------------------------------------------------------

# Écart toléré avant de signaler une valeur (0.35 : ±35 % de la cible).
TOLERANCE = 0.35
EQUIPMENT_POINTS_MAX_MULTIPLIER = RARE_CRAFT_MULTIPLIER[1]


def _off(value: float, target: float, tolerance: float = TOLERANCE) -> bool:
    return target > 0 and abs(value - target) / target > tolerance


def equipment_points_of(stat_bonuses: dict) -> float:
    return round(sum(amount / balance.POINT_VALUE.get(stat, 1) for stat, amount in stat_bonuses.items()), 1)


def check_template(template: dict, tier: str = "coeur") -> list[str]:
    """Écarts d'un template de monstre ou d'objet aux chiffres cibles de son
    niveau. Ne bloque rien : la génération affiche ces avertissements."""
    kind = template.get("kind")
    level = max(int(template.get("level") or 1), 1)
    name = template.get("id", "?")
    warnings = []

    if kind == "monster":
        role = template.get("role", "normal")
        if role not in balance.ROLE_PROFILES:
            return [f"{name} : rôle inconnu « {role} »"]
        target = monster_targets(level, role, tier)
        stats = template.get("stats", {})
        for stat in ("hp", "atk", "def"):
            if _off(stats.get(stat, 0), target["stats"][stat]):
                warnings.append(f"{name} : {stat} {stats.get(stat, 0)} (cible {target['stats'][stat]})")
        main_power = target["main_move"]["power"]
        for move in template.get("moveset", []):
            if "power" not in move:
                warnings.append(f"{name} : l'attaque {move.get('id')} n'a pas de power")
                continue
            # Une attaque lente peut frapper plus fort, au même rythme de dégâts.
            allowed = max(main_power, target["slow_move_power_per_second"] * move.get("cooldown_seconds", 0))
            if move["power"] > allowed * (1 + TOLERANCE):
                warnings.append(f"{name} : l'attaque {move.get('id')} a power {move['power']} (max ≈ {round(allowed, 2)})")

    elif kind == "item" or template.get("category"):
        category = template.get("category")
        if category in BASE_VALUE_PER_LEVEL and _off(template.get("base_value", 0), base_value(level, category), 0.5):
            warnings.append(f"{name} : base_value {template.get('base_value')} (cible {base_value(level, category)})")
        equipment = template.get("equipment") or {}
        if category == "equipment" and equipment.get("slot") in COMBAT_SLOTS:
            points = equipment_points_of(equipment.get("stat_bonuses", {}))
            budget = equipment_points(level)
            if points > budget * EQUIPMENT_POINTS_MAX_MULTIPLIER * (1 + TOLERANCE) or points < budget * 0.5:
                warnings.append(f"{name} : {points} points de stats (cible {budget}, max {round(budget * EQUIPMENT_POINTS_MAX_MULTIPLIER, 1)})")
        consumable = template.get("consumable") or {}
        effect = consumable.get("effect_type")
        if category == "consumable" and effect in CONSUMABLE_SHARE and _off(consumable.get("amount", 0), consumable_amount(level, effect), 0.5):
            warnings.append(f"{name} : {effect} {consumable.get('amount')} (cible {consumable_amount(level, effect)})")

    return warnings


def craft_inputs_targets(inputs: list[dict]) -> list[dict]:
    """Repères pour chaque niveau plausible du résultat d'un craft : de l'ingrédient
    le plus faible au plus fort."""
    levels = [max(int(item.get("level") or 1), 1) for item in inputs] or [1]
    return [craft_targets(level) for level in range(min(levels), max(levels) + 1)]
