"""Craft sans IA : objet composé à partir des ingrédients, de l'atelier et du
paramètre choisi, dans les cibles d'équilibrage (``content_targets``).

Sert de secours quand aucune IA n'est disponible (pas de clé, quota gratuit
épuisé, panne). Le hasard est tiré de la clé de recette : une même recette
donne toujours le même objet. Module pur, sans Firestore.
"""

import hashlib
import random
import re
import unicodedata

try:
    from .balance import POINT_VALUE
    from .content_targets import RARE_CRAFT_MULTIPLIER, base_value, consumable_amount, equipment_points
except ImportError:
    from balance import POINT_VALUE
    from content_targets import RARE_CRAFT_MULTIPLIER, base_value, consumable_amount, equipment_points

# Par atelier : la catégorie produite, les emplacements possibles d'un
# équipement, ses stats (la première domine) et les noms de l'objet.
WORKSHOPS = {
    "forge": {
        "equipment_from": 3,  # quantité totale d'ingrédients à partir de laquelle on forge une pièce
        "slots": {"weapon": ["atk", "agi"], "gloves": ["atk", "def"], "head": ["def", "hp"]},
        "names": {"weapon": ["Lame", "Épée courte", "Hachette", "Masse"], "gloves": ["Gantelets"], "head": ["Heaume", "Casque"]},
        "material": ["Alliage", "Lingot", "Barre forgée"],
    },
    "couture": {
        "equipment_from": 2,
        "slots": {"chest": ["def", "hp"], "boots": ["agi", "def"], "gloves": ["agi", "atk"], "head": ["sag", "def"]},
        "names": {"chest": ["Tunique", "Veste", "Mantelet"], "boots": ["Bottes", "Chausses"], "gloves": ["Gants", "Mitaines"], "head": ["Capuche", "Bonnet"]},
        "material": ["Étoffe", "Cuir travaillé", "Toile"],
    },
    "enchantement": {
        "equipment_from": 2,
        "slots": {"ring": ["int", "sag"], "amulet": ["sag", "int"], "weapon": ["int", "atk"]},
        "names": {"ring": ["Anneau", "Bague"], "amulet": ["Amulette", "Pendentif"], "weapon": ["Bâton", "Baguette"]},
        "material": ["Rune stabilisée", "Éclat chargé", "Glyphe"],
    },
    "sculpture": {
        "equipment_from": 2,
        "slots": {"amulet": ["def", "sag"], "ring": ["agi", "def"], "head": ["def", "hp"]},
        "names": {"amulet": ["Talisman", "Médaillon"], "ring": ["Anneau taillé", "Chevalière"], "head": ["Diadème", "Couronne de bois"]},
        "material": ["Pièce taillée", "Ornement", "Composant sculpté"],
    },
    "alchimie": {"consumable": ["Potion", "Élixir", "Philtre", "Tonique"]},
    "cuisine": {"consumable": ["Ragoût", "Galette", "Brochette", "Bouillon", "Tourte"]},
}

# Le paramètre de l'atelier module la puissance (±10 %) et donne un qualificatif
# (masculin, féminin).
PARAMETER_EFFECTS = {
    "basse": (0.9, "tiède", "tiède"), "moyenne": (1.0, "trempé", "trempée"),
    "haute": (1.05, "ardent", "ardente"), "extreme": (1.1, "incandescent", "incandescente"),
    "point serré": (1.0, "serré", "serrée"), "tissage lâche": (0.95, "léger", "légère"),
    "matelassage": (1.05, "matelassé", "matelassée"),
    "faible": (0.9, "murmurant", "murmurante"), "stable": (1.0, "stable", "stable"),
    "instable": (1.1, "instable", "instable"), "profonde": (1.1, "profond", "profonde"),
    "brute": (0.95, "brut", "brute"), "polie": (1.0, "poli", "polie"), "gravée": (1.05, "gravé", "gravée"),
    "macération froide": (1.0, "macéré", "macérée"), "ébullition rapide": (0.95, "vif", "vive"),
    "distillation lente": (1.1, "distillé", "distillée"),
    "cru-mariné": (0.95, "mariné", "marinée"), "grillé": (1.0, "grillé", "grillée"),
    "mijoté": (1.1, "mijoté", "mijotée"), "flambé": (1.05, "flambé", "flambée"),
}

# Noms féminins (les autres sont masculins), pour accorder le qualificatif.
FEMININE_NOUNS = {
    "Lame", "Épée courte", "Hachette", "Masse", "Barre forgée", "Tunique", "Veste", "Capuche", "Bottes", "Chausses",
    "Mitaines", "Étoffe", "Toile", "Bague", "Amulette", "Baguette", "Rune stabilisée", "Pièce taillée",
    "Chevalière", "Couronne de bois", "Potion", "Galette", "Brochette", "Tourte",
}

# « l'atelier de … » selon l'atelier.
WORKSHOP_PHRASES = {
    "forge": "à la forge", "couture": "à l'atelier de couture", "enchantement": "à l'atelier d'enchantement",
    "sculpture": "à l'atelier de sculpture", "alchimie": "à l'alambic", "cuisine": "en cuisine",
}

# Mots d'ingrédient qui orientent un consommable vers le mana plutôt que les PV.
MANA_WORDS = ("eau", "lac", "lune", "rune", "cristal", "brume", "aube", "lumière", "éclat", "rosée", "écaille")
STOPWORDS = {"de", "du", "des", "la", "le", "les", "d", "l", "et", "à", "au", "aux", "en"}

# Cohérence des ingrédients → multiplicateur du budget (règles du prompt de craft).
NO_LINK, NORMAL, RARE = 0.65, 1.0, RARE_CRAFT_MULTIPLIER[0]


def words(text: str) -> set[str]:
    plain = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    return {w for w in re.split(r"[^a-z]+", plain) if len(w) >= 4 and w not in STOPWORDS}


def coherence(inputs: list[dict]) -> float:
    """Ingrédients qui partagent un mot de leur nom (même créature, même
    matériau) : cohérence rare. Un seul ingrédient : normale. Des catégories
    toutes différentes sans mot commun : aucun rapport."""
    if len(inputs) == 1:
        return NORMAL
    shared = set.intersection(*(words(item.get("name", item["id"])) for item in inputs))
    if shared:
        return RARE
    categories = {item.get("category") for item in inputs}
    return NO_LINK if len(categories) == len(inputs) and len(inputs) >= 3 else NORMAL


def result_level(inputs: list[dict]) -> int:
    """Moyenne des niveaux pondérée par les quantités, jamais au-dessus du plus haut."""
    total = sum(item["quantity"] for item in inputs)
    average = sum(max(int(item.get("level") or 1), 1) * item["quantity"] for item in inputs) / total
    return max(1, min(round(average), max(int(item.get("level") or 1) for item in inputs)))


def split_budget(points: float, stats: list[str]) -> dict[str, int]:
    """Répartit des points équivalents : 70 % sur la première stat, 30 % sur la seconde."""
    shares = [1.0] if len(stats) == 1 or points < 3 else [0.7, 0.3]
    return {stat: max(1, round(points * share * POINT_VALUE[stat])) for stat, share in zip(stats, shares)}


def ingredient_list(inputs: list[dict]) -> str:
    names = [item.get("name", item["id"]) for item in inputs]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " et " + names[-1]


def procedural_craft_item(workshop: dict, parameters: dict, inputs: list[dict], seed: str) -> dict:
    """Objet crafté sans IA. ``inputs`` : templates des ingrédients avec leur
    ``quantity`` ; ``seed`` : la clé de recette (même recette, même objet)."""
    rng = random.Random(hashlib.sha256(seed.encode()).hexdigest())
    rules = WORKSHOPS.get(workshop["id"], WORKSHOPS["sculpture"])
    level = result_level(inputs)
    link = coherence(inputs)
    parameter = next(iter(parameters.values()), "")
    power, masculine, feminine = PARAMETER_EFFECTS.get(parameter, (1.0, "", ""))
    multiplier = link * power
    total_quantity = sum(item["quantity"] for item in inputs)
    ingredients = ingredient_list(inputs)
    where = WORKSHOP_PHRASES.get(workshop["id"], "à l'atelier")

    item = {"level": level, "stack_size": 1}
    if "consumable" in rules:
        noun = rng.choice(rules["consumable"])
        mana = any(w in words(" ".join(i.get("name", "") for i in inputs)) for w in MANA_WORDS) and rng.random() < 0.7
        effect = "restore_mana" if mana else "heal"
        amount = round(consumable_amount(level, effect) * max(0.8, min(1.2, multiplier)))
        item.update({
            "category": "consumable",
            "stack_size": 10,
            "consumable": {"effect_type": effect, "amount": amount, "duration_seconds": 0},
            "description": f"{'Rend un peu de mana' if mana else 'Soigne les blessures'}. Préparé ({parameter}) à partir de {ingredients}.",
        })
    elif total_quantity >= rules["equipment_from"]:
        slot = rng.choice(sorted(rules["slots"]))
        noun = rng.choice(rules["names"][slot])
        points = equipment_points(level) * min(multiplier, RARE_CRAFT_MULTIPLIER[1])
        item.update({
            "category": "equipment",
            "equipment": {"slot": slot, "level_required": level, "stat_bonuses": split_budget(points, rules["slots"][slot])},
            "description": f"Façonné {where} ({parameter}) à partir de {ingredients}."
                           + (" Les matières s'y accordent si bien qu'il en sort quelque chose de rare." if link == RARE else ""),
        })
    else:
        noun = rng.choice(rules["material"])
        item.update({
            "category": "material",
            "description": f"Matière travaillée {where} ({parameter}) à partir de {ingredients}. "
                           "Elle attend d'entrer dans une autre recette.",
        })

    main = max(inputs, key=lambda i: i["quantity"]).get("name", "")
    adjective = feminine if noun in FEMININE_NOUNS else masculine
    item["name"] = " ".join(part for part in (noun, adjective) if part) + (f" ({main})" if main else "")
    item["id"] = item["name"]
    item["base_value"] = base_value(level, item["category"])
    item["origin"] = "procedural"
    return item


def normalize_craft_item(item: dict, inputs: list[dict]) -> dict:
    """Ramène un objet crafté (IA ou procédural) dans les bornes d'équilibrage :
    niveau entre le plus faible et le plus fort ingrédient, bonus entiers sous
    le plafond rare (×2 le budget du niveau), consommable à ±20 % de sa cible."""
    levels = [max(int(i.get("level") or 1), 1) for i in inputs]
    try:
        level = int(item.get("level") or 1)
    except (TypeError, ValueError):
        level = max(levels)
    level = max(min(levels), min(level, max(levels)))
    item["level"] = level
    item["stack_size"] = max(1, int(item.get("stack_size") or 1))
    if item.get("category") == "equipment" and isinstance(item.get("equipment"), dict):
        equipment = item["equipment"]
        equipment["level_required"] = level
        bonuses = {stat: max(1, int(round(float(value)))) for stat, value in (equipment.get("stat_bonuses") or {}).items()
                   if stat in POINT_VALUE and float(value or 0) > 0}
        cap = equipment_points(level) * RARE_CRAFT_MULTIPLIER[1]
        total = sum(value / POINT_VALUE[stat] for stat, value in bonuses.items())
        if total > cap:
            # Réduction proportionnelle ; une stat ramenée à zéro disparaît, puis
            # la plus faible part tant que le total dépasse (au moins une reste).
            scaled = {stat: int(value * cap / total) for stat, value in bonuses.items()}
            main = max(bonuses, key=lambda stat: bonuses[stat] / POINT_VALUE[stat])
            bonuses = {stat: value for stat, value in scaled.items() if value > 0} or {main: 1}
            while len(bonuses) > 1 and sum(v / POINT_VALUE[s] for s, v in bonuses.items()) > cap:
                del bonuses[min(bonuses, key=lambda stat: bonuses[stat] / POINT_VALUE[stat])]
        equipment["stat_bonuses"] = {} if equipment.get("slot") == "tool" else bonuses
    elif item.get("category") == "consumable" and isinstance(item.get("consumable"), dict):
        consumable = item["consumable"]
        if consumable.get("effect_type") in ("heal", "restore_mana"):
            target = consumable_amount(level, consumable["effect_type"])
            consumable["amount"] = max(round(target * 0.8), min(int(consumable.get("amount") or target), round(target * 1.2)))
    # Valeur de revente plafonnée : un objet inventé trop cher ferait du craft
    # une machine à or (sell_unit_price = base_value × K_SELL).
    target_value = base_value(level, item.get("category", "material"))
    try:
        item["base_value"] = max(target_value, min(int(item.get("base_value") or 0), round(target_value * 1.3)))
    except (TypeError, ValueError):
        item["base_value"] = target_value
    return item