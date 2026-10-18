#!/usr/bin/env python3
"""
Génère les compétences liées à un étage, à partir de son contenu RÉEL (lu directement dans les
fichiers de zones déjà générés, pas juste piochées dans l'index global) — garantit que chaque
condition référencée existe vraiment dans cet étage, pas ailleurs dans la bibliothèque.

Prérequis :
    pip install anthropic
    Toutes les zones de l'étage doivent déjà avoir été générées (generate_zone_content.py).

Usage :
    python generate_floor_skills.py --skeleton floor_1_skeleton.json --count 10
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import anthropic

# Le script est dans generate_static_files/skill/, tandis que les zones et
# les templates sont stockés à la racine du projet.
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
SKELETONS_DIR = PROJECT_ROOT / "zone_skeletons"

# Chiffres cibles d'équilibrage, calculés par le serveur (back/content_targets.py).
sys.path.insert(0, str(PROJECT_ROOT))
from back import content_targets  # noqa: E402

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not API_KEY:
    sys.exit("❌ Variable d'environnement ANTHROPIC_API_KEY absente.")

MODEL = "claude-sonnet-5"
MAX_TOKENS = 32000
PROMPT_FILE = SCRIPT_DIR / "arthesia_floor_skills_prompt.md"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
ZONES_DIR = PROJECT_ROOT / "zones"
INDEX_FILE = TEMPLATES_DIR / "index.json"
SKILLS_DIR = TEMPLATES_DIR / "skills"

# dossier de stockage selon template_type d'Entry (quest_giver -> fichier "quest")
ENTRY_TYPE_TO_KIND = {
    "monster": "monster",
    "resource": "resource",
    "npc": "npc",
    "quest_giver": "quest",
    "shop": "shop",
    "player_market": "player_market",
}
KIND_TO_FOLDER = {
    "monster": "monsters",
    "resource": "resources",
    "npc": "npcs",
    "quest": "quests",
    "shop": "shops",
    "player_market": "player_markets",
    "item": "items",
}


# =============================================================================
# COLLECTE DE L'INVENTAIRE RÉEL DE L'ÉTAGE
# =============================================================================

def load_template_file(kind: str, template_id: str) -> Optional[dict]:
    folder = KIND_TO_FOLDER.get(kind)
    if not folder:
        return None
    path = TEMPLATES_DIR / folder / f"{template_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    data["id"] = data.get("id", template_id)
    return data


def collect_item_refs(tpl: dict, kind: str) -> set:
    """item_id référencés en profondeur par un template (loot_table/yields/rewards/sell_offers)."""
    refs = set()
    if kind == "monster":
        for e in tpl.get("loot_table", []) or []:
            refs.add(e.get("item_id"))
    elif kind == "resource":
        for e in tpl.get("yields", []) or []:
            refs.add(e.get("item_id"))
    elif kind == "quest":
        for e in (tpl.get("rewards", {}) or {}).get("items", []) or []:
            refs.add(e.get("item_id"))
        for obj in tpl.get("objectives", []) or []:
            if obj.get("type") == "deliver_item":
                refs.add(obj.get("item_id"))
    elif kind == "shop":
        for e in tpl.get("sell_offers", []) or []:
            refs.add(e.get("item_id"))
    refs.discard(None)
    return refs


def collect_floor_inventory(floor_number: int) -> list[dict]:
    """Lit tous les fichiers de zone de cet étage et récupère chaque template réellement utilisé
    (directement via les Entry, et en profondeur via loot_table/yields/rewards/sell_offers)."""
    zone_dir = ZONES_DIR / f"floor_{floor_number}"
    if not zone_dir.exists():
        sys.exit(f"❌ Aucune zone générée trouvée dans {zone_dir}. "
                  f"Génère d'abord le contenu des zones de cet étage.")

    seen_pairs = set()
    inventory = []

    def add(kind: str, template_id: str):
        pair = (kind, template_id)
        if pair in seen_pairs or template_id is None:
            return
        tpl = load_template_file(kind, template_id)
        if tpl is None:
            print(f"⚠️  Référencé mais introuvable sur disque : {kind}/{template_id}")
            return
        seen_pairs.add(pair)
        inventory.append({
            "id": tpl["id"],
            "name": tpl.get("name"),
            "kind": kind,
            "level": tpl.get("level"),
            "role": tpl.get("role"),  # pertinent pour les monstres (repère le boss)
            "description": tpl.get("description", "")[:200],
        })
        # suit les références en profondeur (ex. le loot d'un monstre) pour inclure les items aussi
        for item_id in collect_item_refs(tpl, kind):
            add("item", item_id)

    zone_files = sorted(zone_dir.glob("*.json"))
    for zone_file in zone_files:
        zone = json.loads(zone_file.read_text(encoding="utf-8"))
        for entry in zone.get("entries", []):
            kind = ENTRY_TYPE_TO_KIND.get(entry.get("template_type"))
            if kind:
                add(kind, entry.get("template_id"))

    return inventory


# =============================================================================
# APPEL API
# =============================================================================

def load_system_prompt() -> str:
    if not PROMPT_FILE.exists():
        sys.exit(f"❌ Prompt introuvable : {PROMPT_FILE}")
    return PROMPT_FILE.read_text(encoding="utf-8")


def build_user_message(floor_info: dict, inventory: list[dict], count: int, targets: dict) -> str:
    return (
        "Génère les compétences de cet étage avec les paramètres suivants :\n\n"
        f"floor:\n{json.dumps(floor_info, ensure_ascii=False, indent=2)}\n\n"
        f"balance_targets:\n{json.dumps(targets, ensure_ascii=False)}\n\n"
        f"floor_content_inventory:\n{json.dumps(inventory, ensure_ascii=False, indent=2)}\n\n"
        f"skill_count_target: {count}\n\n"
        "Réponds uniquement avec le JSON demandé, sans aucun texte avant ou après."
    )


def extract_json(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def generate_skills(floor_info: dict, inventory: list[dict], count: int, targets: dict) -> list[dict]:
    client = anthropic.Anthropic(api_key=API_KEY)

    system_prompt = load_system_prompt()
    user_message = build_user_message(floor_info, inventory, count, targets)

    print(f"🌐 Appel à {MODEL} pour générer {count} compétences...")
    # Streaming : le SDK refuse un appel non streamé au-delà d'environ 21 000
    # tokens de limite. La réflexion du modèle compte dans cette limite.
    with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        response = stream.get_final_message()

    # Une réponse coupée donne un JSON inutilisable : on s'arrête sans rien écrire.
    if response.stop_reason == "max_tokens":
        sys.exit(f"❌ Réponse coupée à {MAX_TOKENS} tokens : rien n'est sauvegardé. "
                 f"Augmente MAX_TOKENS ou relance.")

    raw_text = "".join(block.text for block in response.content if block.type == "text")

    try:
        return extract_json(raw_text)["skills"]
    except (json.JSONDecodeError, KeyError):
        print("❌ Réponse invalide (pas un JSON avec une clé 'skills').")
        print("---- Réponse brute ----")
        print(raw_text)
        raise


# =============================================================================
# VALIDATION
# =============================================================================

def extract_leaf_conditions(cond: Optional[dict]) -> list[dict]:
    """Déplie récursivement un group pour récupérer toutes les conditions feuilles."""
    if not cond:
        return []
    if cond.get("type") == "group":
        leaves = []
        for sub in cond.get("conditions", []):
            leaves.extend(extract_leaf_conditions(sub))
        return leaves
    return [cond]


def validate_skills(skills: list[dict], inventory: list[dict], full_index: list[dict]) -> list[str]:
    warnings = []
    inventory_ids = {t["id"] for t in inventory}
    known_pairs = {(t.get("kind"), t["id"]) for t in full_index}

    exceptional_count = 0
    POWER_SANITY_MAX = 5  # au-delà, quasi certainement une confusion coefficient/valeur brute

    for skill in skills:
        # power/amount_power doivent rester des coefficients, pas des valeurs brutes
        for effect in skill.get("effects", []) or []:
            for field in ("power", "amount_power"):
                value = effect.get(field)
                if value is not None and value > POWER_SANITY_MAX:
                    warnings.append(f"Compétence '{skill.get('id')}' : {field}={value} sur un effet "
                                     f"'{effect.get('type')}' — probable confusion coefficient/valeur "
                                     f"brute (devrait rester sous {POWER_SANITY_MAX}).")

    for skill in skills:
        # chaque référence de unlock_condition doit exister dans l'inventaire de CET étage
        for leaf in extract_leaf_conditions(skill.get("unlock_condition")):
            leaf_type = leaf.get("type")
            target = None
            if leaf_type == "kill_count":
                target = leaf.get("monster_id")
            elif leaf_type == "gather_count":
                target = leaf.get("resource_id")
            elif leaf_type in ("deliver_item", "item"):
                target = leaf.get("item_id")
            elif leaf_type == "quest":
                target = leaf.get("quest_id")
            elif leaf_type == "boss":
                target = leaf.get("zone_id")

            if target and target not in inventory_ids:
                warnings.append(f"Compétence '{skill.get('id')}' référence '{target}' "
                                 f"({leaf_type}) absent de l'inventaire de cet étage.")

            if leaf_type == "boss" or (leaf_type == "group" and len(extract_leaf_conditions(leaf)) > 1):
                pass  # compté au niveau du group parent ci-dessous, pas ici

        top_cond = skill.get("unlock_condition") or {}
        if top_cond.get("type") == "group" or top_cond.get("type") == "boss":
            exceptional_count += 1

        # collision d'id avec la bibliothèque de compétences existante
        pair = ("skill", skill.get("id"))
        if pair in known_pairs:
            warnings.append(f"Collision : une compétence '{skill.get('id')}' existe déjà.")

    if exceptional_count > 2:
        warnings.append(f"{exceptional_count} compétences de palier 'Exceptionnelle' détectées "
                         f"(condition group/boss) — la règle en demandait 1-2 maximum.")

    return warnings


# =============================================================================
# SAUVEGARDE
# =============================================================================

def load_index() -> list[dict]:
    if not INDEX_FILE.exists():
        return []
    return json.loads(INDEX_FILE.read_text(encoding="utf-8"))


def save_index(index: list[dict]) -> None:
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def save_skills(skills: list[dict], full_index: list[dict], floor_number: int) -> list[dict]:
    known_pairs = {(t.get("kind"), t["id"]) for t in full_index}
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    new_index_entries = []
    for skill in skills:
        pair = ("skill", skill.get("id"))
        if pair in known_pairs:
            print(f"  ⏭️  Ignoré (collision) : skills/{skill.get('id')}.json")
            continue

        path = SKILLS_DIR / f"{skill['id']}.json"
        path.write_text(json.dumps(skill, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  💾 Nouvelle compétence : skills/{skill['id']}.json")

        new_index_entries.append({
            "id": skill["id"],
            "name": skill.get("name"),
            "kind": "skill",
            "level": None,
            "tags": [],
            "description": skill.get("description", ""),
            "floor_origin": floor_number,  # bookkeeping seulement, pas dans le schéma Skill lui-même
        })

    return new_index_entries


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Génère les compétences liées à un étage Arthesia.")
    parser.add_argument("--skeleton", type=str, required=True, help="Chemin du squelette d'étage")
    parser.add_argument("--count", type=int, default=10)
    args = parser.parse_args()

    skeleton_path = Path(args.skeleton)
    if not skeleton_path.is_absolute():
        project_path = PROJECT_ROOT / skeleton_path
        skeleton_path = project_path if project_path.exists() else SKELETONS_DIR / skeleton_path
    if not skeleton_path.exists():
        sys.exit(f"❌ Squelette introuvable : {skeleton_path}")

    skeleton = json.loads(skeleton_path.read_text(encoding="utf-8"))
    match = re.search(r"(\d+)", skeleton.get("id", "floor_0"))
    floor_number = int(match.group(1)) if match else 0

    floor_info = {
        "id": skeleton.get("id"),
        "name": skeleton.get("name"),
        "description": skeleton.get("description"),
    }

    inventory = collect_floor_inventory(floor_number)
    print(f"📦 Inventaire réel de l'étage : {len(inventory)} éléments "
          f"({sum(1 for t in inventory if t['kind'] == 'monster')} monstres, "
          f"{sum(1 for t in inventory if t['kind'] == 'item')} items, "
          f"{sum(1 for t in inventory if t['kind'] == 'quest')} quêtes, ...)")

    # Niveaux réels du contenu de l'étage, sinon ceux prévus pour ce numéro d'étage.
    levels = [t["level"] for t in inventory if isinstance(t.get("level"), int)]
    level_range = [min(levels), max(levels)] if levels else content_targets.floor_level_range(floor_number)
    targets = content_targets.skill_targets(level_range)

    skills = generate_skills(floor_info, inventory, args.count, targets)

    full_index = load_index()
    warnings = validate_skills(skills, inventory, full_index)
    if warnings:
        print("\n⚠️  Points à vérifier :")
        for w in warnings:
            print(f"  - {w}")
    else:
        print("\n✅ Vérifications automatiques passées (références réelles, paliers respectés).")

    new_entries = save_skills(skills, full_index, floor_number)
    if new_entries:
        save_index(full_index + new_entries)

    print(f"\n💾 {len(new_entries)} compétence(s) sauvegardée(s) pour l'étage {floor_number}.")


if __name__ == "__main__":
    main()