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

# Titres : un seul se porte à la fois, et ses bonus sont permanents. Chaque
# titre récompense un accomplissement composite (2 ou 3 conditions dures), avec
# 1 ou 2 bonus en % de 4 à 12 %, 14 % au total au plus.
TITLE_PERCENT_BOUNDS = (4, 12)
TITLE_MAX_TOTAL_PERCENT = 14
TITLE_CONDITION_COUNT = (2, 3)
TITLE_KILL_THRESHOLDS = {"normal": [30, 100], "elite": [1, 5], "boss": [1, 1]}
TITLE_GATHER_THRESHOLDS = [30, 80]
# Un boss d'étage est vaincu une fois pour tout le serveur : une condition
# « boss » serait offerte à tous. Une victoire (ou son écho) compte dans les
# kill_count du monstre, c'est par là qu'on vise un boss.
FORBIDDEN_UNLOCK_CONDITIONS = {"boss"}


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
    targets = {
        "stats": stats,
        "main_move": {"power": round(power, 2), "cooldown_seconds": MAIN_MOVE_COOLDOWN},
        "slow_move_power_per_second": round(power / SLOW_MOVE_SECONDS_PER_POWER, 3),
        "xp": balance.kill_xp(level, role),
    }
    if role in balance.TELEGRAPHED_MOVES:
        targets["telegraphed_moves"] = telegraphed_moves(role, power)
    return targets


def telegraphed_moves(role: str, power: float) -> list[dict]:
    """Grosses attaques télégraphiées d'une élite ou d'un boss, reprises du
    moveset de référence du simulateur : recharge, puissance (déjà multipliée),
    durée de l'annonce et parade."""
    reference = {"assaut": (7, 2), "furie": (10, 3)}  # recharge, puissance en multiples de P
    moves = []
    for move_id, (seconds, counter) in balance.TELEGRAPHED_MOVES[role].items():
        cooldown, multiple = reference[move_id]
        moves.append({
            "cooldown_seconds": cooldown,
            "power": round(power * multiple * balance.TELEGRAPH_POWER_MULTIPLIER, 3),
            "telegraph_seconds": seconds,
            "avoidable_by": counter,
        })
    return moves


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


def skill_effect(level: int, cooldown: float, tier: str = "simple", support: bool = False,
                 physical_shield: bool = False) -> dict:
    """``base`` et ``power`` d'un effet de compétence débloquée au niveau
    ``level`` : pour le joueur de référence de ce niveau, l'effet total vaut
    skill_power × stat, dont SKILL_BASE_SHARE en part fixe.

    La stat de référence est l'ATK pour les dégâts (physiques comme
    magiques : un mage a autant d'INT qu'un guerrier d'ATK) et pour un
    bouclier physique, la SAG pour un soin ou un bouclier magique."""
    total = skill_power(cooldown, tier, support)
    stats = reference_player(level)
    stat = stats["sag"] if support and not physical_shield else stats["atk"]
    return {"base": round(total * stat * balance.SKILL_BASE_SHARE),
            "power": round(total * (1 - balance.SKILL_BASE_SHARE), 2)}


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
    """Repères des compétences d'un étage, par niveau de déblocage : effets
    (``base`` + ``power``) selon la recharge et le palier, coût en mana,
    bonus des passifs."""
    cooldowns = (3, 6, 10, 15, 20)
    return {
        "passive_percent": PASSIVE_PERCENT,
        "levels": [
            {
                "level": level,
                "damage_by_cooldown": {
                    tier: {str(cd): skill_effect(level, cd, tier) for cd in cooldowns} for tier in SKILL_TIER_MULTIPLIER
                },
                "support_by_cooldown": {
                    tier: {str(cd): skill_effect(level, cd, tier, support=True) for cd in cooldowns}
                    for tier in SKILL_TIER_MULTIPLIER
                },
                "mana_cost_by_cooldown": {str(cd): skill_mana_cost(level, cd) for cd in cooldowns},
                "passive_points": {tier: passive_points(level, tier) for tier in PASSIVE_POWER_SHARE},
            }
            for level in range(max(level_range[0], 1), max(level_range[-1], level_range[0], 1) + 1)
        ],
    }


def title_targets() -> dict:
    """Règles des titres, jointes au message du générateur de compétences."""
    return {
        "percent_per_stat": list(TITLE_PERCENT_BOUNDS),
        "max_total_percent": TITLE_MAX_TOTAL_PERCENT,
        "conditions_per_title": list(TITLE_CONDITION_COUNT),
        "kill_count_by_role": TITLE_KILL_THRESHOLDS,
        "gather_count": TITLE_GATHER_THRESHOLDS,
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
# Nombre d'attaques du moveset de référence de chaque rôle (simulate_combat.reference_monster).
MAX_MOVES = {"normal": 2, "elite": 3, "boss": 4}
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
        moves = template.get("moveset", [])
        if len(moves) > MAX_MOVES[role]:
            warnings.append(f"{name} : {len(moves)} attaques (au plus {MAX_MOVES[role]} pour un {role})")
        main_power = target["main_move"]["power"]
        for move in template.get("moveset", []):
            if "power" not in move:
                warnings.append(f"{name} : l'attaque {move.get('id')} n'a pas de power")
                continue
            # Une attaque lente peut frapper plus fort, au même rythme de dégâts ;
            # une attaque télégraphiée encore plus, puisqu'on peut la parer.
            allowed = max(main_power, target["slow_move_power_per_second"] * move.get("cooldown_seconds", 0))
            telegraphed = move.get("telegraph_seconds", 0) > 0
            if telegraphed:
                allowed *= balance.TELEGRAPH_POWER_MULTIPLIER
            if move["power"] > allowed * (1 + TOLERANCE):
                warnings.append(f"{name} : l'attaque {move.get('id')} a power {move['power']} (max ≈ {round(allowed, 2)})")
            if telegraphed and role == "normal":
                warnings.append(f"{name} : attaque télégraphiée {move.get('id')} sur un monstre normal")
            if telegraphed and move.get("avoidable_by") not in ("esquive", "interruption"):
                warnings.append(f"{name} : l'attaque {move.get('id')} doit avoir avoidable_by « esquive » ou « interruption »")
        expected = len(target.get("telegraphed_moves", []))
        found = sum(1 for move in moves if move.get("telegraph_seconds", 0) > 0)
        if found < expected:
            warnings.append(f"{name} : {found} attaque(s) télégraphiée(s), {expected} attendue(s) pour un {role}")

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


def check_skill(skill: dict) -> list[str]:
    """Écarts d'une compétence générée : puissance au-delà du palier
    exceptionnel pour sa recharge, ou bonus de passif hors des bornes."""
    name = skill.get("id", "?")
    # Un passif n'a pas de recharge : son bouclier ne part qu'une fois par
    # combat, on le compare à la recharge la plus longue.
    cooldown = skill.get("cooldown_seconds") or (20 if skill.get("kind") == "passive" else 6)
    level = skill.get("level")
    warnings = []
    if not isinstance(level, int):
        warnings.append(f"{name} : niveau de déblocage (level) manquant")
        level = 1
    for effect in skill.get("effects", []):
        kind = effect.get("type")
        if kind in ("damage", "heal", "shield"):
            support = kind != "damage"
            top = skill_effect(level, cooldown, "exceptionnelle", support,
                               physical_shield=kind == "shield" and skill.get("damage_type") == "physical")
            power = effect.get("power", effect.get("amount_power", 0))
            if "base" not in effect:
                warnings.append(f"{name} : effet {kind} sans base")
            elif effect["base"] > top["base"] * (1 + TOLERANCE / 2):
                warnings.append(f"{name} : {kind} base {effect['base']} (au plus ≈ {top['base']} au niveau {level})")
            if power > top["power"] * (1 + TOLERANCE / 2):
                warnings.append(f"{name} : {kind} power {power} (au plus ≈ {top['power']} à {cooldown} s)")
        elif kind in ("buff", "debuff") and not 5 <= effect.get("amount_percent", 0) <= 35:
            warnings.append(f"{name} : {kind} de {effect.get('amount_percent')} % (10 à 30 % attendus)")
        elif kind == "stat_bonus_percent" and effect.get("amount_percent", 0) > PASSIVE_PERCENT["exceptionnelle"][1]:
            warnings.append(f"{name} : bonus de {effect.get('amount_percent')} % (au plus 20 %)")
    return warnings


def condition_leaves(condition) -> list[dict]:
    """Conditions feuilles d'une condition, groupes dépliés."""
    if not isinstance(condition, dict):
        return []
    if condition.get("type") == "group":
        return [leaf for sub in condition.get("conditions", []) for leaf in condition_leaves(sub)]
    return [condition]


def check_title(title: dict) -> list[str]:
    """Écarts d'un titre : condition trop simple ou interdite, bonus hors des
    bornes ou d'un autre type que ``stat_bonus_percent``."""
    name = title.get("id", "?")
    warnings = []
    condition = title.get("unlock_condition") or {}
    leaves = condition_leaves(condition)
    low, high = TITLE_CONDITION_COUNT
    if condition.get("type") != "group" or condition.get("mode") != "all" or not low <= len(leaves) <= high:
        warnings.append(f"{name} : la condition doit être un groupe « all » de {low} à {high} conditions")
    for leaf in leaves:
        if leaf.get("type") in FORBIDDEN_UNLOCK_CONDITIONS:
            warnings.append(f"{name} : condition « {leaf['type']} » interdite (passer par kill_count du boss)")
    effects = title.get("effects") or []
    if not 1 <= len(effects) <= 2:
        warnings.append(f"{name} : {len(effects)} effets (1 ou 2 attendus)")
    total = 0
    for effect in effects:
        percent = effect.get("amount_percent", 0)
        total += percent
        if effect.get("type") != "stat_bonus_percent":
            warnings.append(f"{name} : effet {effect.get('type')} (seul stat_bonus_percent est permis)")
        elif not TITLE_PERCENT_BOUNDS[0] <= percent <= TITLE_PERCENT_BOUNDS[1]:
            warnings.append(f"{name} : bonus de {percent} % ({TITLE_PERCENT_BOUNDS[0]} à {TITLE_PERCENT_BOUNDS[1]} % attendus)")
    if total > TITLE_MAX_TOTAL_PERCENT:
        warnings.append(f"{name} : {total} % de bonus au total (au plus {TITLE_MAX_TOTAL_PERCENT} %)")
    return warnings


def craft_inputs_targets(inputs: list[dict]) -> list[dict]:
    """Repères pour chaque niveau plausible du résultat d'un craft : de l'ingrédient
    le plus faible au plus fort."""
    levels = [max(int(item.get("level") or 1), 1) for item in inputs] or [1]
    return [craft_targets(level) for level in range(min(levels), max(levels) + 1)]
