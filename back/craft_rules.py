"""Règles du craft : type d'objet choisi, générations, tirage de la rareté et
enchantement. Module pur, sans Firestore ; ses refus lèvent CraftRuleError.

- Le joueur choisit le type d'objet dans l'atelier (``workshop.item_types``).
- Une ressource, un butin ou un objet de boutique est de génération 0 ; un
  objet crafté vaut la plus haute génération de ses ingrédients + 1, au plus
  MAX_CRAFT_GENERATION. Dès qu'un ingrédient a déjà été travaillé, on ne
  fabrique plus de matériau : il ne pourrait plus servir à rien.
- Chaque fabrication tire un rang de rareté : la cohérence de la recette donne
  les chances de départ, les ingrédients rares et l'avance du métier les
  décalent vers le haut.
- L'enchantement renforce un équipement existant, une seule fois : un objet
  enchanté ne peut plus servir dans aucun craft.
"""

import random

try:
    from .balance import POINT_VALUE, RARITIES, RARITY_STAT_MULTIPLIER, profession_level_for
    from .content_targets import equipment_points
    from .rarity import item_rarity, rarity_rank
except ImportError:
    from balance import POINT_VALUE, RARITIES, RARITY_STAT_MULTIPLIER, profession_level_for
    from content_targets import equipment_points
    from rarity import item_rarity, rarity_rank

MAX_CRAFT_GENERATION = 2
COHERENCES = ("aucune", "normale", "rare")
# Budget de stats de l'objet de base selon la cohérence : la puissance d'une
# belle recette passe surtout par la rareté tirée.
COHERENCE_BUDGET = {"aucune": 0.7, "normale": 1.0, "rare": 1.15}

# Chances de départ (en %) de chaque rang, du commun au légendaire.
RARITY_ODDS = {
    "aucune": [92, 7, 1, 0, 0],
    "normale": [70, 22, 7, 1, 0],
    "rare": [25, 35, 27, 10, 3],
}
# Part des chances d'un rang qui monte d'un cran : par rang de rareté de chaque
# ingrédient (au-dessus de commun, quantité comprise) et par niveau d'avance du
# métier sur l'objet. Au plus MAX_LUCK.
INGREDIENT_LUCK_PER_RANK = 0.05
PROFESSION_LUCK_PER_LEVEL = 0.04
MAX_LUCK = 0.6

# Enchantement : bonus en part du budget de stats de l'équipement, selon la
# qualité des matériaux (niveau et rareté), modulé par l'intensité de la rune.
ENCHANT_SHARE = (0.2, 0.4)
ENCHANT_INTENSITY = {"faible": -0.04, "stable": 0.0, "instable": 0.04, "profonde": 0.06}
MAX_ENCHANT_MATERIALS = 3


class CraftRuleError(Exception):
    """Craft refusé par une règle ; le message est montré au joueur."""


def item_generation(item: dict) -> int:
    return int(item.get("generation") or 0)


def item_label(item: dict) -> str:
    return item.get("name", item.get("id", "?"))


def is_enchant_workshop(workshop: dict) -> bool:
    return workshop.get("mode") == "enchant"


def find_item_type(workshop: dict, type_id: str | None) -> dict:
    types = workshop.get("item_types") or []
    for item_type in types:
        if item_type["id"] == type_id:
            return item_type
    labels = ", ".join(t["label"] for t in types)
    raise CraftRuleError(f"Choisis ce que tu veux obtenir : {labels}")


def output_generation(inputs: list[dict]) -> int:
    """Génération de l'objet fabriqué ; refuse les ingrédients enchantés ou trop travaillés."""
    for item in inputs:
        if item.get("enchanted"):
            raise CraftRuleError(f"« {item_label(item)} » est enchanté : il ne peut plus servir dans un craft")
        if item_generation(item) >= MAX_CRAFT_GENERATION:
            raise CraftRuleError(f"« {item_label(item)} » a déjà été trop travaillé pour servir d'ingrédient")
    return max(item_generation(item) for item in inputs) + 1


def check_output_type(item_type: dict, generation: int) -> None:
    if item_type.get("category") == "material" and generation > 1:
        raise CraftRuleError("Avec un ingrédient déjà travaillé, on ne fabrique plus qu'un équipement ou un consommable")


# --- Rareté --------------------------------------------------------------------

def rarity_odds(coherence: str, inputs: list[dict], output_level: int, profession_level: int) -> list[float]:
    """Chances (en %) de chaque rang pour une fabrication."""
    odds = [float(chance) for chance in RARITY_ODDS.get(coherence, RARITY_ODDS["normale"])]
    ingredient_ranks = sum(rarity_rank(item_rarity(item)) * item.get("quantity", 1) for item in inputs)
    lead = max(0, profession_level - profession_level_for(output_level))
    luck = min(MAX_LUCK, ingredient_ranks * INGREDIENT_LUCK_PER_RANK + lead * PROFESSION_LUCK_PER_LEVEL)
    # Du haut vers le bas, une part de chaque rang monte d'un cran (une seule fois).
    for rank in range(len(odds) - 2, -1, -1):
        moved = odds[rank] * luck
        odds[rank] -= moved
        odds[rank + 1] += moved
    # Un ingrédient épique ou légendaire garantit au moins le rang du dessous.
    floor = max((rarity_rank(item_rarity(item)) - 1 for item in inputs if rarity_rank(item_rarity(item)) >= 3), default=0)
    if floor > 0:
        odds[floor] += sum(odds[:floor])
        odds[:floor] = [0.0] * floor
    return odds


def roll_rarity(odds: list[float], rng: random.Random | None = None) -> str:
    roll = (rng or random).uniform(0, sum(odds))
    for rarity, chance in zip(RARITIES, odds):
        if roll < chance:
            return rarity
        roll -= chance
    return RARITIES[0]


# --- Enchantement ----------------------------------------------------------------

def split_enchant_inputs(inputs: list[dict]) -> tuple[dict, list[dict]]:
    """Sépare l'équipement à enchanter (un seul, en un exemplaire) des matériaux."""
    equipment = [item for item in inputs if item.get("category") == "equipment"]
    materials = [item for item in inputs if item.get("category") != "equipment"]
    if len(equipment) != 1 or equipment[0].get("quantity", 1) != 1:
        raise CraftRuleError("Pose un seul équipement à enchanter, avec 1 à 3 matériaux")
    if not 1 <= sum(item.get("quantity", 1) for item in materials) <= MAX_ENCHANT_MATERIALS:
        raise CraftRuleError("Un enchantement demande 1 à 3 matériaux en plus de l'équipement")
    if any(item.get("category") != "material" for item in materials):
        raise CraftRuleError("Seuls des matériaux accompagnent l'équipement à enchanter")
    target = equipment[0]
    if target.get("enchanted"):
        raise CraftRuleError(f"« {item_label(target)} » est déjà enchanté : un objet ne s'enchante qu'une fois")
    if target.get("equipment", {}).get("slot") == "tool":
        raise CraftRuleError("Un outil ne s'enchante pas")
    for item in materials:
        if item.get("enchanted") or item_generation(item) >= MAX_CRAFT_GENERATION:
            raise CraftRuleError(f"« {item_label(item)} » ne peut plus servir dans un craft")
    return target, materials


def enchant_share(target: dict, materials: list[dict], intensity: str) -> float:
    """Part du budget de stats gagnée : 20 % à 40 % selon les matériaux."""
    level = max(int(target.get("level") or 1), 1)
    total = sum(item.get("quantity", 1) for item in materials)
    quality = sum(min(1.0, max(int(item.get("level") or 1), 1) / level) * (1 + 0.15 * rarity_rank(item_rarity(item)))
                  * item.get("quantity", 1) for item in materials) / total
    low, high = ENCHANT_SHARE
    share = low + (high - low) * min(quality, 1.0) + ENCHANT_INTENSITY.get(intensity, 0.0)
    return max(low, min(high + ENCHANT_INTENSITY["profonde"], share))


def enchant_item(target: dict, materials: list[dict], orientation: dict, intensity: str) -> dict:
    """Équipement enchanté : bonus dans les stats de l'orientation, marque ✦."""
    level = max(int(target.get("level") or 1), 1)
    budget = equipment_points(level) * RARITY_STAT_MULTIPLIER[item_rarity(target)]
    points = budget * enchant_share(target, materials, intensity)
    stats = orientation["stats"]
    shares = [1.0] if len(stats) == 1 else [0.6, 0.4]
    bonus = {stat: max(1, round(points * share * POINT_VALUE[stat])) for stat, share in zip(stats, shares)}

    item = {key: value for key, value in target.items() if key not in ("quantity", "base_item_id", "origin")}
    item["equipment"] = dict(target["equipment"])
    bonuses = dict(target["equipment"].get("stat_bonuses") or {})
    for stat, value in bonus.items():
        bonuses[stat] = bonuses.get(stat, 0) + value
    item["equipment"]["stat_bonuses"] = bonuses
    item["id"] = f"{target['id']}_{orientation['id']}"
    item["name"] = f"{target.get('name', target['id'])} ✦ {orientation['label']}"
    item["enchanted"] = True
    item["enchantment"] = {"orientation": orientation["id"], "bonus": bonus, "intensity": intensity}
    item["description"] = (target.get("description", "").rstrip()
                           + f" Une rune de {orientation['label'].lower()} y a été gravée : il ne pourra plus être retravaillé.").strip()
    materials_value = sum((item_.get("base_value") or 0) * item_.get("quantity", 1) for item_ in materials)
    item["base_value"] = round((target.get("base_value") or 0) * 1.2 + materials_value * 0.5)
    item["origin"] = "enchantement"
    return item
