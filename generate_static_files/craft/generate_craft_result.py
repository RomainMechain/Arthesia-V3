#!/usr/bin/env python3
"""
Essaie une recette de craft hors du jeu : affiche l'objet de base qu'elle donnerait et les chances de
chaque rang de rareté, sans rien écrire (ni fichier, ni Firestore, ni cache).

Passe par le vrai service du serveur (back/craft_service.py) : Gemini si GEMINI_API_KEY est définie,
puis Claude si ANTHROPIC_API_KEY l'est, sinon le générateur sans IA. Pratique pour régler le prompt de
craft (arthesia_craft_prompt.md) ou vérifier l'équilibrage d'une recette.

Prérequis : firebase_key.json à la racine du dépôt (le serveur l'exige pour s'importer).

Usage :
    python generate_craft_result.py --workshop forge --type arme --inputs "branche_morte:2,poil_de_loup_dore:1" --params "temperature=haute"
    python generate_craft_result.py --workshop forge --type arme --inputs "branche_morte:2" --profession-level 4
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from back.balance import RARITIES  # noqa: E402
from back.content_store import load_template  # noqa: E402
from back.content_targets import check_template  # noqa: E402
from back.craft_rules import CraftRuleError, check_output_type, find_item_type, output_generation, rarity_odds  # noqa: E402
from back.craft_service import build_recipe_key, generate_craft_item  # noqa: E402


def parse_pairs(text: str | None, separator: str) -> list[tuple[str, str]]:
    return [tuple(part.split(separator, 1)) for part in (text or "").split(",") if part.strip()]


def main():
    parser = argparse.ArgumentParser(description="Essaie une recette de craft Arthesia, sans rien écrire.")
    parser.add_argument("--workshop", required=True, help="Id de l'atelier (forge, couture, ...)")
    parser.add_argument("--type", required=True, help="Type d'objet de l'atelier (arme, potion_soin, ...)")
    parser.add_argument("--inputs", required=True, help="Format : 'item_id:quantite,item_id:quantite'")
    parser.add_argument("--params", default=None, help="Format : 'param=valeur,param=valeur'")
    parser.add_argument("--profession-level", type=int, default=1, help="Niveau du métier, pour les chances de rareté")
    args = parser.parse_args()

    workshop = load_template("workshop", args.workshop)
    parameters = dict(parse_pairs(args.params, "="))
    inputs = [{**load_template("item", item_id), "quantity": int(quantity)} for item_id, quantity in parse_pairs(args.inputs, ":")]
    try:
        item_type = find_item_type(workshop, args.type)
        generation = output_generation(inputs)
        check_output_type(item_type, generation)
    except CraftRuleError as exc:
        sys.exit(f"❌ {exc}")

    recipe_key = build_recipe_key(args.workshop, [{"item_id": i["id"], "quantity": i["quantity"]} for i in inputs], parameters, args.type)
    item = generate_craft_item(workshop, item_type, parameters, inputs, recipe_key, generation)
    print(json.dumps(item, ensure_ascii=False, indent=2))
    for warning in check_template({**item, "kind": "item"}):
        print(f"⚠️  {warning}")
    odds = rarity_odds(item["coherence"], inputs, item["level"], args.profession_level)
    print("\nChances par fabrication : " + " · ".join(f"{rarity} {chance:.1f} %" for rarity, chance in zip(RARITIES, odds)))


if __name__ == "__main__":
    main()
