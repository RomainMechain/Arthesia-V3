#!/usr/bin/env python3
"""
Génère les compétences et les titres liés à un étage, à partir de son contenu RÉEL (lu directement dans les
fichiers de zones déjà générés, pas juste piochées dans l'index global) — garantit que chaque
condition référencée existe vraiment dans cet étage, pas ailleurs dans la bibliothèque.

Prérequis :
    pip install anthropic
    Toutes les zones de l'étage doivent déjà avoir été générées (generate_zone_content.py).

Usage :
    python generate_floor_skills.py --skeleton floor_1_skeleton.json --count 10 --title-count 8
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

# La clé n'est exigée qu'au moment d'appeler l'API : --print-prompt et
# --from-response fonctionnent sans elle.
API_KEY = os.environ.get("ANTHROPIC_API_KEY")

MODEL = "claude-sonnet-5"
MAX_TOKENS = 32000
PROMPT_FILE = SCRIPT_DIR / "arthesia_floor_skills_prompt.md"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
ZONES_DIR = PROJECT_ROOT / "zones"
INDEX_FILE = TEMPLATES_DIR / "index.json"
SKILLS_DIR = TEMPLATES_DIR / "skills"
TITLES_DIR = TEMPLATES_DIR / "titles"

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


def build_user_message(floor_info: dict, inventory: list[dict], count: int, targets: dict,
                       title_count: int) -> str:
    return (
        "Génère les compétences et les titres de cet étage avec les paramètres suivants :\n\n"
        f"floor:\n{json.dumps(floor_info, ensure_ascii=False, indent=2)}\n\n"
        f"balance_targets:\n{json.dumps(targets, ensure_ascii=False)}\n\n"
        f"title_targets:\n{json.dumps(content_targets.title_targets(), ensure_ascii=False)}\n\n"
        f"floor_content_inventory:\n{json.dumps(inventory, ensure_ascii=False, indent=2)}\n\n"
        f"skill_count_target: {count}\n"
        f"title_count_target: {title_count}\n\n"
        "Réponds uniquement avec le JSON demandé, sans aucun texte avant ou après."
    )


def extract_json(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def generate_skills(floor_info: dict, inventory: list[dict], count: int, targets: dict,
                    title_count: int) -> dict:
    if not API_KEY:
        sys.exit("❌ Variable d'environnement ANTHROPIC_API_KEY absente.")
    client = anthropic.Anthropic(api_key=API_KEY)

    system_prompt = load_system_prompt()
    user_message = build_user_message(floor_info, inventory, count, targets, title_count)

    print(f"🌐 Appel à {MODEL} pour générer {count} compétences et {title_count} titres...")
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
        answer = extract_json(raw_text)
        answer["skills"]
        return answer
    except (json.JSONDecodeError, KeyError):
        print("❌ Réponse invalide (pas un JSON avec une clé 'skills').")
        print("---- Réponse brute ----")
        print(raw_text)
        raise


# =============================================================================
# VALIDATION
# =============================================================================

def condition_reference(leaf: dict) -> Optional[str]:
    """Id de contenu visé par une condition feuille (None pour un niveau)."""
    return {
        "kill_count": leaf.get("monster_id"),
        "gather_count": leaf.get("resource_id"),
        "deliver_item": leaf.get("item_id"),
        "item": leaf.get("item_id"),
        "quest": leaf.get("quest_id"),
        "boss": leaf.get("zone_id"),
    }.get(leaf.get("type"))


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


def validate_skills(skills: list[dict], inventory: list[dict], full_index: list[dict],
                    zone_ids: set | None = None) -> list[str]:
    warnings = []
    # Une condition « boss vaincu » vise la zone du boss, pas un template.
    inventory_ids = {t["id"] for t in inventory} | (zone_ids or set())
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
            target = condition_reference(leaf)
            if target and target not in inventory_ids:
                warnings.append(f"Compétence '{skill.get('id')}' référence '{target}' "
                                 f"({leaf_type}) absent de l'inventaire de cet étage.")
            if leaf_type in content_targets.FORBIDDEN_UNLOCK_CONDITIONS:
                warnings.append(f"Compétence '{skill.get('id')}' : condition « {leaf_type} » interdite "
                                 f"(un boss d'étage est vaincu pour tout le serveur ; utiliser kill_count).")

        top_cond = skill.get("unlock_condition") or {}
        if top_cond.get("type") == "group":
            exceptional_count += 1

        # collision d'id avec la bibliothèque de compétences existante
        pair = ("skill", skill.get("id"))
        if pair in known_pairs:
            warnings.append(f"Collision : une compétence '{skill.get('id')}' existe déjà.")

    for skill in skills:
        warnings.extend(content_targets.check_skill(skill))

    if exceptional_count > 2:
        warnings.append(f"{exceptional_count} compétences de palier 'Exceptionnelle' détectées "
                         f"(condition group) — la règle en demandait 1-2 maximum.")

    return warnings


def validate_titles(titles: list[dict], inventory: list[dict], full_index: list[dict]) -> list[str]:
    """Références réelles de l'étage, règles des titres (content_targets.check_title),
    collisions avec les titres existants et redites entre titres du lot."""
    warnings = []
    inventory_ids = {t["id"] for t in inventory}
    known_pairs = {(t.get("kind"), t["id"]) for t in full_index}
    seen_conditions = set()
    for title in titles:
        leaves = extract_leaf_conditions(title.get("unlock_condition"))
        for leaf in leaves:
            target = condition_reference(leaf)
            if target and target not in inventory_ids:
                warnings.append(f"Titre '{title.get('id')}' référence '{target}' "
                                 f"({leaf.get('type')}) absent de l'inventaire de cet étage.")
        signature = frozenset((leaf.get("type"), condition_reference(leaf)) for leaf in leaves)
        if signature in seen_conditions:
            warnings.append(f"Titre '{title.get('id')}' : mêmes conditions qu'un autre titre du lot.")
        seen_conditions.add(signature)
        if ("title", title.get("id")) in known_pairs:
            warnings.append(f"Collision : un titre '{title.get('id')}' existe déjà.")
        warnings.extend(content_targets.check_title(title))
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


def save_templates(templates: list[dict], kind: str, folder: Path, full_index: list[dict],
                   floor_number: int) -> list[dict]:
    """Écrit les compétences (kind "skill") ou les titres (kind "title") qui
    n'existent pas encore, et renvoie leurs entrées d'index."""
    known_pairs = {(t.get("kind"), t["id"]) for t in full_index}
    folder.mkdir(parents=True, exist_ok=True)
    label = "compétence" if kind == "skill" else "titre"

    new_index_entries = []
    for template in templates:
        if (kind, template.get("id")) in known_pairs:
            print(f"  ⏭️  Ignoré (collision) : {folder.name}/{template.get('id')}.json")
            continue

        path = folder / f"{template['id']}.json"
        path.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  💾 Nouveau {label} : {folder.name}/{template['id']}.json" if kind == "title"
              else f"  💾 Nouvelle {label} : {folder.name}/{template['id']}.json")

        new_index_entries.append({
            "id": template["id"],
            "name": template.get("name"),
            "kind": kind,
            "level": None,
            "tags": [],
            "description": template.get("description", ""),
            "floor_origin": floor_number,  # bookkeeping seulement, pas dans le schéma du template
        })

    return new_index_entries


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Génère les compétences et les titres liés à un étage Arthesia.")
    parser.add_argument("--skeleton", type=str, required=True, help="Chemin du squelette d'étage")
    parser.add_argument("--count", type=int, default=10, help="Nombre de compétences")
    parser.add_argument("--title-count", type=int, default=8, help="Nombre de titres (0 : aucun)")
    # Génération sans l'API (par exemple avec Claude Code) : on écrit le message,
    # la réponse est produite ailleurs, puis validée et sauvegardée ici.
    parser.add_argument("--print-prompt", type=str, metavar="FICHIER",
                        help="Écrit le message utilisateur dans FICHIER, sans appeler l'API")
    parser.add_argument("--from-response", type=str, metavar="FICHIER",
                        help="Utilise la réponse JSON de FICHIER au lieu d'appeler l'API")
    parser.add_argument("--dry-run", action="store_true",
                        help="Valide la réponse sans rien sauvegarder")
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
        "lore_brief": skeleton.get("lore_brief"),
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

    if args.print_prompt:
        Path(args.print_prompt).write_text(
            build_user_message(floor_info, inventory, args.count, targets, args.title_count), encoding="utf-8")
        print(f"\n📝 Message écrit dans {args.print_prompt} (prompt système : {PROMPT_FILE}).")
        return

    if args.from_response:
        answer = extract_json(Path(args.from_response).read_text(encoding="utf-8"))
    else:
        answer = generate_skills(floor_info, inventory, args.count, targets, args.title_count)
    skills = answer["skills"]
    titles = answer.get("titles") or []

    full_index = load_index()
    zone_ids = {zone["id"] for zone in skeleton.get("zones", [])}
    warnings = validate_skills(skills, inventory, full_index, zone_ids)
    warnings += validate_titles(titles, inventory, full_index)
    if warnings:
        print("\n⚠️  Points à vérifier :")
        for w in warnings:
            print(f"  - {w}")
    else:
        print("\n✅ Vérifications automatiques passées (références réelles, paliers respectés).")

    if args.dry_run:
        print("\n🧪 --dry-run : rien n'est sauvegardé.")
        return

    new_skills = save_templates(skills, "skill", SKILLS_DIR, full_index, floor_number)
    new_titles = save_templates(titles, "title", TITLES_DIR, full_index, floor_number)
    if new_skills or new_titles:
        save_index(full_index + new_skills + new_titles)

    print(f"\n💾 {len(new_skills)} compétence(s) et {len(new_titles)} titre(s) sauvegardés "
          f"pour l'étage {floor_number}.")


if __name__ == "__main__":
    main()