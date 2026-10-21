"""Craft sans IA : objet de base composé à partir du type choisi, des
ingrédients et du paramètre de l'atelier, dans les cibles d'équilibrage
(``content_targets``). Contient aussi ``normalize_craft_item``, qui ramène tout
objet de base (IA ou non) dans les bornes.

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
    from .content_targets import base_value, consumable_amount, equipment_points
    from .craft_rules import COHERENCE_BUDGET, COHERENCES
except ImportError:
    from balance import POINT_VALUE
    from content_targets import base_value, consumable_amount, equipment_points
    from craft_rules import COHERENCE_BUDGET, COHERENCES

# Le paramètre de l'atelier module la puissance (±10 %) et donne un qualificatif
# (masculin, féminin).
PARAMETER_EFFECTS = {
    "basse": (0.9, "tiède", "tiède"), "moyenne": (1.0, "trempé", "trempée"),
    "haute": (1.05, "ardent", "ardente"), "extreme": (1.1, "incandescent", "incandescente"),
    "point serré": (1.0, "serré", "serrée"), "tissage lâche": (0.95, "léger", "légère"),
    "matelassage": (1.05, "matelassé", "matelassée"),
    "brute": (0.95, "brut", "brute"), "polie": (1.0, "poli", "polie"), "gravée": (1.05, "gravé", "gravée"),
    "macération froide": (1.0, "macéré", "macérée"), "ébullition rapide": (0.95, "vif", "vive"),
    "distillation lente": (1.1, "distillé", "distillée"),
    "cru-mariné": (0.95, "mariné", "marinée"), "grillé": (1.0, "grillé", "grillée"),
    "mijoté": (1.1, "mijoté", "mijotée"), "flambé": (1.05, "flambé", "flambée"),
}

# Noms féminins des types d'objet (les autres sont masculins), pour l'accord.
FEMININE_NOUNS = {
    "Lame", "Épée courte", "Hachette", "Masse", "Barre forgée", "Tunique", "Veste", "Capuche", "Bottes", "Chausses",
    "Mitaines", "Étoffe", "Toile", "Chevalière", "Couronne de bois", "Pièce taillée", "Potion", "Essence",
    "Galette", "Brochette", "Tourte", "Infusion", "Tisane",
}

# Où l'objet a été fait, selon l'atelier.
WORKSHOP_PHRASES = {
    "forge": "Forgé", "couture": "Cousu", "sculpture": "Taillé", "alchimie": "Préparé", "cuisine": "Cuisiné",
}

STOPWORDS = {"de", "du", "des", "la", "le", "les", "d", "l", "et", "à", "au", "aux", "en"}

# Plafond des bonus d'un objet de base, en part du budget du niveau : la
# puissance au-delà vient du rang de rareté tiré à chaque fabrication.
BASE_BUDGET_CAP = 1.2
# Valeur de l'objet de base : environ 85 % de celle des ingrédients, dans les
# bornes du niveau. Un craft commun fait perdre un peu ; un rang élevé rapporte.
INPUT_VALUE_SHARE = 0.85
BASE_VALUE_BOUNDS = (0.7, 1.3)


def words(text: str) -> set[str]:
    plain = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    return {w for w in re.split(r"[^a-z]+", plain) if len(w) >= 4 and w not in STOPWORDS}


def coherence(inputs: list[dict]) -> str:
    """Ingrédients qui partagent un mot de leur nom (même créature, même
    matériau) : cohérence rare. Trois catégories ou plus toutes différentes
    sans mot commun : aucune. Sinon : normale."""
    if len(inputs) == 1:
        return "normale"
    if set.intersection(*(words(item.get("name", item["id"])) for item in inputs)):
        return "rare"
    categories = {item.get("category") for item in inputs}
    return "aucune" if len(categories) == len(inputs) and len(inputs) >= 3 else "normale"


def result_level(inputs: list[dict]) -> int:
    """Moyenne des niveaux pondérée par les quantités, jamais au-dessus du plus haut."""
    total = sum(item["quantity"] for item in inputs)
    average = sum(max(int(item.get("level") or 1), 1) * item["quantity"] for item in inputs) / total
    return max(1, min(round(average), max(int(item.get("level") or 1) for item in inputs)))


def split_budget(points: float, stats: list[str]) -> dict[str, int]:
    """Répartit des points équivalents : 70 % sur la première stat, 30 % sur la seconde."""
    shares = [1.0] if len(stats) == 1 or points < 3 else [0.7, 0.3]
    return {stat: max(1, round(points * share * POINT_VALUE[stat])) for stat, share in zip(stats, shares)}


def dominant_ingredient(inputs: list[dict]) -> dict:
    """L'ingrédient qui donne son caractère à l'objet : le plus présent, puis le plus haut niveau."""
    return max(inputs, key=lambda item: (item["quantity"], int(item.get("level") or 1)))


def procedural_craft_item(workshop: dict, item_type: dict, parameters: dict, inputs: list[dict], seed: str) -> dict:
    """Objet de base d'un craft, sans IA. ``inputs`` : fiches des ingrédients
    avec leur ``quantity`` ; ``seed`` : la clé de recette (même recette, même objet)."""
    rng = random.Random(hashlib.sha256(seed.encode()).hexdigest())
    level = result_level(inputs)
    link = coherence(inputs)
    parameter = next(iter(parameters.values()), "")
    power, masculine, feminine = PARAMETER_EFFECTS.get(parameter, (1.0, "", ""))
    multiplier = COHERENCE_BUDGET[link] * power
    noun = rng.choice(item_type.get("names") or [item_type["label"]])
    main = dominant_ingredient(inputs).get("name", "")
    made = WORKSHOP_PHRASES.get(workshop["id"], "Fabriqué")

    item = {"level": level, "stack_size": 1, "category": item_type["category"]}
    if item_type["category"] == "consumable":
        effect = item_type.get("effect_type", "heal")
        item["stack_size"] = 10
        item["consumable"] = {"effect_type": effect, "amount": round(consumable_amount(level, effect) * multiplier),
                              "duration_seconds": 0}
        item["description"] = f"{'Rend du mana' if effect == 'restore_mana' else 'Soigne les blessures'}. {made} ({parameter}) autour de {main}."
    elif item_type["category"] == "equipment":
        item["equipment"] = {"slot": item_type["slot"], "level_required": level,
                             "stat_bonuses": split_budget(equipment_points(level) * multiplier, item_type.get("stats") or ["def"])}
        item["description"] = f"{made} ({parameter}) autour de {main}." + (
            " Ses matières s'accordent si bien qu'il en garde une étrange vigueur." if link == "rare" else "")
    else:
        item["description"] = f"{made} ({parameter}) à partir de {main}. Il attend d'entrer dans une autre recette."

    adjective = feminine if noun in FEMININE_NOUNS else masculine
    item["name"] = " ".join(part for part in (noun, adjective) if part) + (f" ({main})" if main else "")
    item["id"] = item["name"]
    item["coherence"] = link
    item["origin"] = "procedural"
    return item


def normalize_craft_item(item: dict, inputs: list[dict], item_type: dict, generation: int) -> dict:
    """Ramène un objet de base (IA ou procédural) dans les bornes : catégorie,
    emplacement et effet du type choisi, niveau entre le plus faible et le plus
    fort ingrédient, bonus entiers sous BASE_BUDGET_CAP fois le budget du
    niveau, consommable à ±20 % de sa cible, valeur proche de celle des
    ingrédients. L'objet de base est toujours commun : le rang se tire à chaque
    fabrication."""
    levels = [max(int(i.get("level") or 1), 1) for i in inputs]
    try:
        level = int(item.get("level") or 1)
    except (TypeError, ValueError):
        level = max(levels)
    level = max(min(levels), min(level, max(levels)))
    item["level"] = level
    item["category"] = item_type["category"]
    item["stack_size"] = max(1, int(item.get("stack_size") or 1))
    item["generation"] = generation
    item["rarity"] = "commun"
    item["coherence"] = item.get("coherence") if item.get("coherence") in COHERENCES else "normale"
    for key in ("equipment", "consumable"):
        if key != item["category"]:
            item.pop(key, None)

    if item["category"] == "equipment":
        equipment = item["equipment"] if isinstance(item.get("equipment"), dict) else {}
        item["equipment"] = equipment
        equipment["slot"] = item_type["slot"]
        equipment["level_required"] = level
        bonuses = {}
        for stat, value in (equipment.get("stat_bonuses") or {}).items():
            try:
                if stat in POINT_VALUE and float(value) > 0:
                    bonuses[stat] = max(1, int(round(float(value))))
            except (TypeError, ValueError):
                continue
        cap = equipment_points(level) * BASE_BUDGET_CAP
        total = sum(value / POINT_VALUE[stat] for stat, value in bonuses.items())
        if not bonuses:
            bonuses = split_budget(equipment_points(level), item_type.get("stats") or ["def"])
        elif total > cap:
            # Réduction proportionnelle ; une stat ramenée à zéro disparaît, puis
            # la plus faible part tant que le total dépasse (au moins une reste).
            scaled = {stat: int(value * cap / total) for stat, value in bonuses.items()}
            main = max(bonuses, key=lambda stat: bonuses[stat] / POINT_VALUE[stat])
            bonuses = {stat: value for stat, value in scaled.items() if value > 0} or {main: 1}
            while len(bonuses) > 1 and sum(v / POINT_VALUE[s] for s, v in bonuses.items()) > cap:
                del bonuses[min(bonuses, key=lambda stat: bonuses[stat] / POINT_VALUE[stat])]
        equipment["stat_bonuses"] = {} if item_type["slot"] == "tool" else bonuses
    elif item["category"] == "consumable":
        consumable = item["consumable"] if isinstance(item.get("consumable"), dict) else {}
        item["consumable"] = consumable
        consumable["effect_type"] = item_type.get("effect_type", "heal")
        target = consumable_amount(level, consumable["effect_type"])
        try:
            amount = int(consumable.get("amount") or target)
        except (TypeError, ValueError):
            amount = target
        consumable["amount"] = max(round(target * 0.8), min(amount, round(target * 1.2)))
        consumable["duration_seconds"] = 0

    # Valeur de revente : proche de celle des ingrédients, dans les bornes du
    # niveau (un objet trop cher ferait du craft une machine à or).
    target_value = base_value(level, item["category"])
    inputs_value = sum((i.get("base_value") or 0) * i.get("quantity", 1) for i in inputs)
    low, high = BASE_VALUE_BOUNDS
    item["base_value"] = max(round(target_value * low), min(round(inputs_value * INPUT_VALUE_SHARE), round(target_value * high)))
    return item
