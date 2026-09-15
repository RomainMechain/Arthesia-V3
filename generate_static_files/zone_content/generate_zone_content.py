#!/usr/bin/env python3
"""
Génère le contenu d'une zone (entries + nouveaux templates) via l'API Claude,
à partir du squelette d'étage déjà généré et de la bibliothèque de templates existante.

Prérequis :
    pip install anthropic

Structure de dossiers attendue/créée :
    templates/index.json                  <- résumé léger de tous les templates (tous types confondus)
    templates/<kind>s/<id>.json            <- définition complète de chaque template
    zones/floor_<n>/<zone_id>.json         <- contenu complet et jouable de la zone

Usage :
    python generate_zone_content.py --skeleton floor_1_skeleton.json --zone-id foret_sombre
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import anthropic

# Le script est dans generate_static_files/zone_content/, tandis que les
# squelettes, templates et zones sont stockés à la racine du projet.
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
SKELETONS_DIR = PROJECT_ROOT / "zone_skeletons"

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not API_KEY:
    sys.exit("❌ Variable d'environnement ANTHROPIC_API_KEY absente.")

MODEL = "claude-sonnet-5"
PROMPT_FILE = SCRIPT_DIR / "arthesia_zone_content_prompt.md"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
ZONES_DIR = PROJECT_ROOT / "zones"
INDEX_FILE = TEMPLATES_DIR / "index.json"

# dossier de stockage par kind de template
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
# BIBLIOTHÈQUE DE TEMPLATES
# =============================================================================

def load_index() -> list[dict]:
    if not INDEX_FILE.exists():
        return []
    return json.loads(INDEX_FILE.read_text(encoding="utf-8"))


def save_index(index: list[dict]) -> None:
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def filter_relevant_templates(index: list[dict], zone: dict, max_items: int = 25) -> list[dict]:
    """Filtrage simple v1 : recoupement de tags + proximité de niveau avec la zone.
    À affiner plus tard (embeddings, filtrage par type de zone, etc.) si le token cost pique trop."""
    brief = zone.get("generation_brief", {})
    zone_tags = set(brief.get("tags", []))
    level_range = brief.get("level_range", [1, 99])
    level_mid = (level_range[0] + level_range[1]) / 2

    scored = []
    for tpl in index:
        tag_overlap = len(zone_tags & set(tpl.get("tags", [])))
        level_val = tpl.get("level")
        # npc/quest/shop/player_market n'ont pas de notion de niveau : ne pas les pénaliser
        level_distance = abs(level_val - level_mid) if level_val is not None else 0
        score = tag_overlap * 10 - level_distance
        scored.append((score, tpl))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [tpl for _, tpl in scored[:max_items]]


def build_library_excerpt(templates: list[dict]) -> list[dict]:
    """Version allégée envoyée au modèle : pas la définition mécanique complète,
    juste de quoi décider si on réutilise. base_value est inclus (uniquement rempli pour les
    items) car le modèle en a besoin pour respecter le prix plancher d'un shop."""
    return [
        {
            "id": t["id"],
            "name": t.get("name"),
            "kind": t.get("kind"),
            "level": t.get("level"),
            "tags": t.get("tags", []),
            "description": t.get("description", "")[:150],
            "base_value": t.get("base_value"),
        }
        for t in templates
    ]


# =============================================================================
# SQUELETTE D'ÉTAGE
# =============================================================================

def load_skeleton(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def find_zone(skeleton: dict, zone_id: str) -> dict:
    for zone in skeleton.get("zones", []):
        if zone["id"] == zone_id:
            return zone
    sys.exit(f"❌ Zone '{zone_id}' introuvable dans le squelette.")


def find_manifest_obligations(skeleton: dict, zone_id: str) -> list[dict]:
    return [
        c for c in skeleton.get("condition_manifest", [])
        if c.get("resolved_by", "").startswith(f"zone:{zone_id}")
    ]


# =============================================================================
# APPEL API
# =============================================================================

def load_system_prompt() -> str:
    if not PROMPT_FILE.exists():
        sys.exit(f"❌ Prompt introuvable : {PROMPT_FILE}")
    return PROMPT_FILE.read_text(encoding="utf-8")


def build_user_message(zone: dict, obligations: list[dict], library_excerpt: list[dict]) -> str:
    return (
        "Génère le contenu de cette zone avec les paramètres suivants :\n\n"
        f"zone:\n{json.dumps(zone, ensure_ascii=False, indent=2)}\n\n"
        f"manifest_obligations:\n{json.dumps(obligations, ensure_ascii=False, indent=2)}\n\n"
        f"template_library_excerpt:\n{json.dumps(library_excerpt, ensure_ascii=False, indent=2)}\n\n"
        "Réponds uniquement avec le JSON demandé, sans aucun texte avant ou après."
    )


def extract_json(text: str) -> dict:
    """Tente de trouver un objet JSON valide dans une réponse Claude, même si elle
    contient des blocs de réflexion ou du texte avant/après le JSON."""
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("Réponse vide: aucun texte JSON reçu.")

    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    candidates = [cleaned]
    if cleaned.startswith("{") or cleaned.startswith("["):
        candidates = [cleaned]
    else:
        start = min((cleaned.find("{"), cleaned.find("[")), default=-1)
        end = max(cleaned.rfind("}"), cleaned.rfind("]"))
        if start != -1 and end != -1 and end > start:
            candidates = [cleaned[start : end + 1]]
        else:
            candidates = [cleaned]

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

        trimmed = re.sub(r"^[^\[{]+", "", candidate, count=1)
        if trimmed != candidate:
            try:
                return json.loads(trimmed)
            except json.JSONDecodeError:
                pass

    raise json.JSONDecodeError("Aucun JSON valide trouvé dans la réponse", cleaned, 0)


def generate_zone_content(zone: dict, obligations: list[dict], library_excerpt: list[dict]) -> dict:
    client = anthropic.Anthropic(api_key=API_KEY)

    system_prompt = load_system_prompt()
    user_message = build_user_message(zone, obligations, library_excerpt)

    print(f"🌐 Appel à {MODEL} pour la zone '{zone['id']}'...")
    response = client.messages.create(
        model=MODEL,
        max_tokens=20000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    raw_text = "".join(
        block.text for block in getattr(response, "content", [])
        if getattr(block, "type", None) == "text"
    )
    if not raw_text and hasattr(response, "output_text"):
        raw_text = response.output_text

    if not raw_text:
        print("❌ Claude n'a renvoyé aucun bloc texte exploitable.")
        print("---- Réponse brute ----")
        print(response)
        raise ValueError("Réponse vide de Claude sans bloc text.")

    try:
        return extract_json(raw_text)
    except (json.JSONDecodeError, ValueError):
        print("❌ La réponse n'est pas un JSON valide.")
        print("---- Réponse brute ----")
        print(raw_text)
        raise


# =============================================================================
# VALIDATION
# =============================================================================

def collect_referenced_item_ids(new_templates: list[dict]) -> set:
    """Récupère tous les item_id référencés en profondeur dans les nouveaux templates :
    loot_table (monster), yields (resource), rewards.items + objectifs deliver_item (quest),
    sell_offers (shop). Ce sont des références qu'Entry.template_id ne couvre pas."""
    referenced = set()
    for tpl in new_templates:
        kind = tpl.get("kind")
        if kind == "monster":
            for entry in tpl.get("loot_table", []) or []:
                referenced.add(entry.get("item_id"))
        elif kind == "resource":
            for entry in tpl.get("yields", []) or []:
                referenced.add(entry.get("item_id"))
        elif kind == "quest":
            for item in (tpl.get("rewards", {}) or {}).get("items", []) or []:
                referenced.add(item.get("item_id"))
            for obj in tpl.get("objectives", []) or []:
                if obj.get("type") == "deliver_item":
                    referenced.add(obj.get("item_id"))
        elif kind == "shop":
            for offer in tpl.get("sell_offers", []) or []:
                referenced.add(offer.get("item_id"))
    referenced.discard(None)
    return referenced


def validate_result(result: dict, full_index: list[dict], obligations: list[dict]) -> list[str]:
    warnings = []

    # la vraie clé de collision est (kind, id) : deux templates de kinds différents peuvent
    # légitimement partager un id (ex. la ressource "argile_humide" et l'item "argile_humide"
    # qu'elle donne) sans jamais s'ambiguïser, puisque chaque référence dans le modèle est déjà
    # typée (template_type + template_id, item_id dans un contexte précis...).
    known_pairs = {(t.get("kind"), t["id"]) for t in full_index}
    known_ids_any_kind = {t["id"] for t in full_index}  # pour la vérif des Entry, qui n'a pas de kind

    new_templates = result.get("new_templates", [])

    # doublons internes à cette réponse, au sein du MÊME kind (écraserait un fichier à la sauvegarde)
    seen_pairs = set()
    duplicate_pairs = set()
    for tpl in new_templates:
        pair = (tpl.get("kind"), tpl.get("id"))
        if pair in seen_pairs:
            duplicate_pairs.add(pair)
        seen_pairs.add(pair)
    if duplicate_pairs:
        warnings.append(f"Même (kind, id) réutilisé plusieurs fois dans cette réponse : "
                         f"{sorted(duplicate_pairs)} — un fichier en écraserait un autre à la sauvegarde.")

    # collision avec la bibliothèque existante, même granularité (kind, id)
    collisions = seen_pairs & known_pairs
    if collisions:
        warnings.append(f"Collision (kind, id) avec un template déjà existant : {sorted(collisions)}")

    all_available_ids = known_ids_any_kind | {t["id"] for t in new_templates}
    for entry in result.get("entries", []):
        if entry.get("template_id") not in all_available_ids:
            warnings.append(f"Entry '{entry.get('id')}' référence un template inconnu : "
                             f"'{entry.get('template_id')}'")

    # item_id référencés en profondeur (loot_table/yields/rewards/sell_offers) — pas couverts par
    # la vérification des Entry ci-dessus, puisqu'ils ne sont jamais des template_id d'Entry.
    known_item_ids = {t["id"] for t in full_index if t.get("kind") == "item"}
    new_item_ids = {t["id"] for t in new_templates if t.get("kind") == "item"}
    available_item_ids = known_item_ids | new_item_ids
    for item_id in collect_referenced_item_ids(new_templates):
        if item_id not in available_item_ids:
            warnings.append(f"item_id référencé (loot/yields/rewards/sell_offers) mais introuvable : "
                             f"'{item_id}'")

    # prix de vente d'un shop jamais en dessous du base_value de l'item — sinon achat/revente
    # au serveur = argent infini
    base_value_by_id = {t["id"]: t.get("base_value") for t in full_index if t.get("kind") == "item"}
    base_value_by_id.update({t["id"]: t.get("base_value") for t in new_templates if t.get("kind") == "item"})
    for tpl in new_templates:
        if tpl.get("kind") != "shop":
            continue
        for offer in tpl.get("sell_offers", []) or []:
            item_id = offer.get("item_id")
            price = offer.get("price")
            base_value = base_value_by_id.get(item_id)
            if base_value is not None and price is not None and price < base_value:
                warnings.append(f"Shop '{tpl.get('id')}' vend '{item_id}' à {price}, en dessous de son "
                                 f"base_value ({base_value}) — permet l'argent infini par achat/revente.")

    # obligations du manifeste : vérification best-effort (présence de l'id_suggere quelque part).
    # Exclu "zone_id" du blob scanné : le format de sortie échoue toujours le zone_id de la zone
    # elle-même en en-tête, ce qui rendrait un check "boss" (qui référence sa propre zone) toujours
    # vrai par construction, sans avoir rien vérifié.
    scan_blob = json.dumps(
        {"entries": result.get("entries", []), "new_templates": new_templates},
        ensure_ascii=False,
    )
    for obl in obligations:
        cond = obl.get("condition", {})
        cond_type = cond.get("type")

        if cond_type == "boss":
            # vérification structurelle : au moins une entry monster dont le template a role="boss"
            # (si le template est réutilisé depuis la bibliothèque plutôt que nouveau, on ne peut pas
            # vérifier son role ici sans charger le fichier complet — accepté par défaut, limite connue)
            monster_entries = [e for e in result.get("entries", [])
                                if e.get("template_type") == "monster"]
            new_monster_ids = {t["id"] for t in new_templates if t.get("kind") == "monster"}
            boss_found = False
            for e in monster_entries:
                tid = e.get("template_id")
                if tid not in new_monster_ids:
                    boss_found = True  # template réutilisé, non vérifiable ici : accepté
                    break
                tpl = next((t for t in new_templates if t["id"] == tid), None)
                if tpl and tpl.get("role") == "boss":
                    boss_found = True
                    break
            if not boss_found:
                warnings.append(f"Obligation boss potentiellement non remplie : aucune entry monster "
                                 f"avec role='boss' trouvée pour {cond}")
        else:
            target = cond.get("item_id") or cond.get("quest_id") or cond.get("zone_id")
            if target and target not in scan_blob:
                warnings.append(f"Obligation potentiellement non remplie : {cond} "
                                 f"(id '{target}' introuvable dans la réponse)")

    return warnings, duplicate_pairs | collisions


# =============================================================================
# SAUVEGARDE
# =============================================================================

def save_new_templates(new_templates: list[dict], skip_pairs: set) -> list[dict]:
    """Sauvegarde chaque nouveau template dans son dossier, retourne les entrées d'index à ajouter.
    Ignore les templates dont (kind, id) est en collision (déjà existant ou dupliqué dans la réponse)
    pour ne jamais écraser un fichier existant silencieusement."""
    index_entries = []
    seen_pairs = set()
    for tpl in new_templates:
        kind = tpl.get("kind")
        pair = (kind, tpl.get("id"))

        if pair in skip_pairs:
            print(f"  ⏭️  Ignoré (collision) : {kind}/{tpl.get('id')}.json")
            continue
        if pair in seen_pairs:
            print(f"  ⏭️  Ignoré (doublon dans cette réponse, garde le premier) : {kind}/{tpl.get('id')}.json")
            continue
        seen_pairs.add(pair)

        folder = KIND_TO_FOLDER.get(kind)
        if not folder:
            print(f"⚠️  kind inconnu '{kind}' pour le template '{tpl.get('id')}', ignoré.")
            continue

        target_dir = TEMPLATES_DIR / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{tpl['id']}.json"
        tpl_to_save = {k: v for k, v in tpl.items() if k != "kind"}
        target_path.write_text(json.dumps(tpl_to_save, ensure_ascii=False, indent=2), encoding="utf-8")

        index_entries.append({
            "id": tpl["id"],
            "name": tpl.get("name"),
            "kind": kind,
            "level": tpl.get("level"),
            "tags": tpl.get("tags", []),
            "description": tpl.get("description", ""),
            "base_value": tpl.get("base_value"),
        })
        print(f"  💾 Nouveau template : {folder}/{tpl['id']}.json")

    return index_entries


def save_zone_content(skeleton_zone: dict, result: dict, floor_number: int) -> Path:
    zone_id = skeleton_zone["id"]
    full_zone = {**skeleton_zone, "entries": result.get("entries", [])}

    zone_dir = ZONES_DIR / f"floor_{floor_number}"
    zone_dir.mkdir(parents=True, exist_ok=True)
    zone_path = zone_dir / f"{zone_id}.json"
    zone_path.write_text(json.dumps(full_zone, ensure_ascii=False, indent=2), encoding="utf-8")
    return zone_path


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Génère le contenu détaillé d'une zone Arthesia.")
    parser.add_argument("--skeleton", type=str, required=True, help="Chemin du squelette d'étage")
    parser.add_argument("--zone-id", type=str, required=True)
    parser.add_argument("--floor-number", type=int, default=None,
                         help="Par défaut, déduit de l'id du squelette (floor_N).")
    parser.add_argument("--max-templates", type=int, default=25,
                         help="Nombre max de templates envoyés au modèle pour réutilisation")
    args = parser.parse_args()

    skeleton_path = Path(args.skeleton)
    if not skeleton_path.is_absolute():
        project_path = PROJECT_ROOT / skeleton_path
        skeleton_path = project_path if project_path.exists() else SKELETONS_DIR / skeleton_path
    if not skeleton_path.exists():
        sys.exit(f"❌ Squelette introuvable : {skeleton_path}")

    skeleton = load_skeleton(skeleton_path)
    zone = find_zone(skeleton, args.zone_id)
    obligations = find_manifest_obligations(skeleton, args.zone_id)

    floor_number = args.floor_number
    if floor_number is None:
        match = re.search(r"(\d+)", skeleton.get("id", "floor_0"))
        floor_number = int(match.group(1)) if match else 0

    full_index = load_index()
    relevant = filter_relevant_templates(full_index, zone, max_items=args.max_templates)
    library_excerpt = build_library_excerpt(relevant)
    print(f"📚 {len(full_index)} templates connus, {len(library_excerpt)} envoyés au modèle "
          f"(filtrés par tags/niveau).")

    if obligations:
        print(f"📋 {len(obligations)} obligation(s) du manifeste à honorer pour cette zone.")

    result = generate_zone_content(zone, obligations, library_excerpt)

    warnings, colliding_pairs = validate_result(result, full_index, obligations)
    if warnings:
        print("\n⚠️  Points à vérifier :")
        for w in warnings:
            print(f"  - {w}")
    else:
        print("\n✅ Vérifications automatiques passées (références valides, obligations trouvées).")

    new_index_entries = save_new_templates(result.get("new_templates", []), colliding_pairs)
    if new_index_entries:
        save_index(full_index + new_index_entries)

    zone_path = save_zone_content(zone, result, floor_number)
    print(f"\n💾 Zone sauvegardée : {zone_path}")
    print(f"   {len(result.get('entries', []))} entries, "
          f"{len(result.get('new_templates', []))} nouveaux templates")


if __name__ == "__main__":
    main()