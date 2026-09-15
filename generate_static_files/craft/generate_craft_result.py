#!/usr/bin/env python3
"""
Tente un craft dans un atelier donné. Vérifie d'abord le cache de recettes (craft_cache.json) :
si la combinaison exacte (atelier + items triés + paramètres triés) existe déjà, renvoie
directement le résultat connu, sans appel API. Sinon, appelle l'IA pour générer le nouvel item,
le sauvegarde comme nouveau template, et enregistre la recette dans le cache pour la prochaine fois.

Prérequis :
    pip install anthropic

Structure de dossiers attendue :
    templates/workshops/<id>.json      <- définitions des 6 ateliers, à créer à la main (pas générées)
    templates/items/<id>.json          <- définitions des items (générées par ce script ou par les zones)
    templates/index.json               <- résumé léger de tous les templates (partagé avec generate_zone_content.py)
    craft_cache.json                   <- { recipe_key: output_item_id }

Usage :
    python generate_craft_result.py --workshop forge --inputs "minerai_fer:1,bois_brut:2" --params "temperature=haute"
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import anthropic

# Le script est dans generate_static_files/craft/, tandis que templates/ et
# craft_cache.json sont à la racine du projet. Ces chemins ne dépendent donc
# pas du dossier depuis lequel la commande est lancée.
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not API_KEY:
    sys.exit("❌ Variable d'environnement ANTHROPIC_API_KEY absente.")

MODEL = "claude-sonnet-5"
PROMPT_FILE = SCRIPT_DIR / "arthesia_craft_prompt.md"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
WORKSHOPS_DIR = TEMPLATES_DIR / "workshops"
ITEMS_DIR = TEMPLATES_DIR / "items"
INDEX_FILE = TEMPLATES_DIR / "index.json"
CRAFT_CACHE_FILE = PROJECT_ROOT / "craft_cache.json"


# =============================================================================
# CHARGEMENT
# =============================================================================

def load_workshop(workshop_id: str) -> dict:
    path = WORKSHOPS_DIR / f"{workshop_id}.json"
    if not path.exists():
        sys.exit(f"❌ Atelier introuvable : {path}\n"
                  f"   Les ateliers sont définis à la main (voir Workshop dans le modèle de données), "
                  f"pas générés par l'IA.")
    return json.loads(path.read_text(encoding="utf-8"))


def load_item(item_id: str) -> dict:
    path = ITEMS_DIR / f"{item_id}.json"
    if not path.exists():
        sys.exit(f"❌ Item introuvable dans templates/items/ : '{item_id}'")
    item = json.loads(path.read_text(encoding="utf-8"))
    item["id"] = item.get("id", item_id)
    return item


def load_index() -> list[dict]:
    if not INDEX_FILE.exists():
        return []
    return json.loads(INDEX_FILE.read_text(encoding="utf-8"))


def save_index(index: list[dict]) -> None:
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def load_craft_cache() -> dict:
    if not CRAFT_CACHE_FILE.exists():
        return {}
    return json.loads(CRAFT_CACHE_FILE.read_text(encoding="utf-8"))


def save_craft_cache(cache: dict) -> None:
    CRAFT_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


# =============================================================================
# PARSING DES ARGUMENTS INPUTS/PARAMS
# =============================================================================

def parse_inputs(raw: str) -> list[dict]:
    """'minerai_fer:1,bois_brut:2' -> [{'item_id': 'minerai_fer', 'quantity': 1}, ...]"""
    result = []
    for part in raw.split(","):
        item_id, _, qty = part.strip().partition(":")
        result.append({"item_id": item_id.strip(), "quantity": int(qty) if qty else 1})
    return result


def parse_params(raw: Optional[str]) -> dict:
    """'temperature=haute,autre=valeur' -> {'temperature': 'haute', 'autre': 'valeur'}"""
    if not raw:
        return {}
    result = {}
    for part in raw.split(","):
        key, _, value = part.strip().partition("=")
        result[key.strip()] = value.strip()
    return result


# =============================================================================
# CLÉ DE RECETTE CANONIQUE
# =============================================================================

def build_recipe_key(workshop_id: str, inputs: list[dict], params: dict) -> str:
    sorted_inputs = sorted(inputs, key=lambda i: i["item_id"])
    inputs_part = ",".join(f"{i['item_id']}:{i['quantity']}" for i in sorted_inputs)
    sorted_params = sorted(params.items())
    params_part = ",".join(f"{k}={v}" for k, v in sorted_params)
    return f"{workshop_id}|{inputs_part}|{params_part}"


# =============================================================================
# VALIDATION DES PARAMÈTRES DEMANDÉS CONTRE L'ATELIER
# =============================================================================

def validate_params_against_workshop(workshop: dict, params: dict) -> list[str]:
    errors = []
    declared = {p["id"]: p.get("options", []) for p in workshop.get("parameters", [])}
    for key, value in params.items():
        if key not in declared:
            errors.append(f"Paramètre inconnu pour l'atelier '{workshop['id']}' : '{key}'")
        elif value not in declared[key]:
            errors.append(f"Valeur '{value}' invalide pour '{key}' — options valides : {declared[key]}")
    missing = set(declared) - set(params)
    if missing:
        errors.append(f"Paramètre(s) manquant(s) : {sorted(missing)}")
    return errors


# =============================================================================
# APPEL API (uniquement si la combinaison est inédite)
# =============================================================================

def load_system_prompt() -> str:
    if not PROMPT_FILE.exists():
        sys.exit(f"❌ Prompt introuvable : {PROMPT_FILE}")
    return PROMPT_FILE.read_text(encoding="utf-8")


def build_user_message(workshop: dict, params: dict, inputs_full: list[dict]) -> str:
    workshop_info = {
        "id": workshop["id"],
        "name": workshop.get("name"),
        "description": workshop.get("description"),
        "parameters_choisis": params,
    }
    return (
        "Génère le résultat de ce craft inédit :\n\n"
        f"workshop:\n{json.dumps(workshop_info, ensure_ascii=False, indent=2)}\n\n"
        f"inputs:\n{json.dumps(inputs_full, ensure_ascii=False, indent=2)}\n\n"
        "Réponds uniquement avec le JSON demandé, sans aucun texte avant ou après."
    )


def extract_json(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def generate_craft_item(workshop: dict, params: dict, inputs_full: list[dict]) -> dict:
    client = anthropic.Anthropic(api_key=API_KEY)

    system_prompt = load_system_prompt()
    user_message = build_user_message(workshop, params, inputs_full)

    print(f"🌐 Combinaison inédite — appel à {MODEL}...")
    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    raw_text = "".join(block.text for block in response.content if block.type == "text")

    try:
        return extract_json(raw_text)["item"]
    except (json.JSONDecodeError, KeyError):
        print("❌ Réponse invalide (pas un JSON avec une clé 'item').")
        print("---- Réponse brute ----")
        print(raw_text)
        raise


# =============================================================================
# SAUVEGARDE
# =============================================================================

def save_new_item(item: dict, full_index: list[dict]) -> Optional[str]:
    """Sauvegarde le nouvel item si son id ne collisionne pas déjà avec un item existant.
    Retourne l'id sauvegardé, ou None si la sauvegarde a été refusée."""
    known_item_ids = {t["id"] for t in full_index if t.get("kind") == "item"}
    if item["id"] in known_item_ids:
        print(f"⚠️  Collision : un item '{item['id']}' existe déjà — craft refusé, rien n'est sauvegardé.")
        print(f"   (Si l'IA a réutilisé un id existant par erreur, relance : la clé de recette est "
              f"toujours inconnue donc un nouvel appel sera fait.)")
        return None

    ITEMS_DIR.mkdir(parents=True, exist_ok=True)
    item_path = ITEMS_DIR / f"{item['id']}.json"
    item_path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  💾 Nouvel item : items/{item['id']}.json")

    full_index.append({
        "id": item["id"],
        "name": item.get("name"),
        "kind": "item",
        "level": item.get("level"),
        "tags": item.get("tags", []),
        "description": item.get("description", ""),
        "base_value": item.get("base_value"),
    })
    save_index(full_index)
    return item["id"]


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Tente un craft dans un atelier Arthesia.")
    parser.add_argument("--workshop", type=str, required=True)
    parser.add_argument("--inputs", type=str, required=True,
                         help="Format: 'item_id:quantite,item_id:quantite'")
    parser.add_argument("--params", type=str, default=None,
                         help="Format: 'param=valeur,param=valeur'")
    args = parser.parse_args()

    workshop = load_workshop(args.workshop)
    inputs = parse_inputs(args.inputs)
    params = parse_params(args.params)

    param_errors = validate_params_against_workshop(workshop, params)
    if param_errors:
        for e in param_errors:
            print(f"❌ {e}")
        sys.exit(1)

    recipe_key = build_recipe_key(args.workshop, inputs, params)
    cache = load_craft_cache()

    if recipe_key in cache:
        output_item_id = cache[recipe_key]
        print(f"✅ Combinaison déjà connue — aucun appel API.")
        print(f"   Résultat : {output_item_id}")
        item_path = ITEMS_DIR / f"{output_item_id}.json"
        if item_path.exists():
            print(f"\n{item_path.read_text(encoding='utf-8')}")
        return

    # combinaison inédite : charger le détail complet des items en entrée pour donner du contexte à l'IA
    inputs_full = []
    for inp in inputs:
        item = load_item(inp["item_id"])
        inputs_full.append({**item, "quantity": inp["quantity"]})

    new_item = generate_craft_item(workshop, params, inputs_full)

    full_index = load_index()
    saved_id = save_new_item(new_item, full_index)

    if saved_id:
        cache[recipe_key] = saved_id
        save_craft_cache(cache)
        print(f"\n💾 Recette mise en cache : {recipe_key} → {saved_id}")
        print(f"   Toute combinaison identique donnera désormais ce résultat, sans repasser par l'IA.")
    else:
        print(f"\n⚠️  Résultat généré mais non sauvegardé (collision). Recette non mise en cache — "
              f"relance la commande pour réessayer.")


if __name__ == "__main__":
    main()
