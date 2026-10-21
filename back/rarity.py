"""Rareté des objets et variantes de rang.

Un objet crafté a une fiche de base (rang « commun ») et chaque fabrication
tire un rang : le résultat est une variante ``<id de base>~<rang>``, calculée
à la volée depuis la fiche de base (bonus, effet et valeur multipliés). Une
variante n'est jamais écrite sur le disque. Module pur.
"""

import copy

try:
    from .balance import RARITIES, RARITY_CONSUMABLE_MULTIPLIER, RARITY_STAT_MULTIPLIER, RARITY_VALUE_MULTIPLIER
except ImportError:
    from balance import RARITIES, RARITY_CONSUMABLE_MULTIPLIER, RARITY_STAT_MULTIPLIER, RARITY_VALUE_MULTIPLIER

VARIANT_SEPARATOR = "~"


def item_rarity(item: dict) -> str:
    rarity = item.get("rarity", "commun")
    return rarity if rarity in RARITIES else "commun"


def rarity_rank(rarity: str) -> int:
    return RARITIES.index(rarity) if rarity in RARITIES else 0


def split_variant_id(item_id: str) -> tuple[str, str | None]:
    """``"lame~rare"`` → ``("lame", "rare")`` ; un id sans rang → ``(id, None)``."""
    base_id, separator, rarity = item_id.partition(VARIANT_SEPARATOR)
    if separator and rarity in RARITIES:
        return base_id, rarity
    return item_id, None


def variant_id(base_id: str, rarity: str) -> str:
    """Id de la variante d'un rang ; le rang commun garde l'id de base."""
    return base_id if rarity == "commun" else f"{base_id}{VARIANT_SEPARATOR}{rarity}"


def apply_rarity(base: dict, rarity: str) -> dict:
    """Variante d'une fiche de base au rang ``rarity`` : les multiplicateurs
    s'appliquent relativement au rang de la fiche de base."""
    item = copy.deepcopy(base)
    base_rarity = item_rarity(base)
    stat_ratio = RARITY_STAT_MULTIPLIER[rarity] / RARITY_STAT_MULTIPLIER[base_rarity]
    effect_ratio = RARITY_CONSUMABLE_MULTIPLIER[rarity] / RARITY_CONSUMABLE_MULTIPLIER[base_rarity]
    value_ratio = RARITY_VALUE_MULTIPLIER[rarity] / RARITY_VALUE_MULTIPLIER[base_rarity]

    equipment = item.get("equipment")
    if isinstance(equipment, dict) and equipment.get("stat_bonuses"):
        equipment["stat_bonuses"] = {stat: max(1, round(value * stat_ratio)) for stat, value in equipment["stat_bonuses"].items()}
    consumable = item.get("consumable")
    if isinstance(consumable, dict) and consumable.get("amount"):
        consumable["amount"] = round(consumable["amount"] * effect_ratio)
    item["base_value"] = round((item.get("base_value") or 0) * value_ratio)
    item["rarity"] = rarity
    item["id"] = variant_id(base["id"], rarity)
    item["base_item_id"] = base["id"]
    return item
