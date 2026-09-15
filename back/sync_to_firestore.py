#!/usr/bin/env python3
"""
Pousse le contenu généré localement (templates/, zones/, squelettes d'étage) vers Firestore.
À relancer après chaque nouvelle génération pour que les Cloud Functions aient accès au contenu à jour.

⚠️ Ne touche JAMAIS à la collection "zones" (état dynamique des joueurs) — le contenu statique des
zones va dans "zone_content", une collection séparée. Les mélanger écraserait l'état de jeu réel
(respawns en cours, stocks épuisés...) à chaque resynchronisation.

Prérequis :
    pip install firebase-admin
    Le fichier de clé de compte de service Firebase téléchargé depuis la console.

Usage :
    python sync_to_firestore.py --service-account chemin/vers/cle.json
    python sync_to_firestore.py --service-account cle.json --skeleton floor_1_skeleton.json
    python sync_to_firestore.py --service-account cle.json --skip-templates --floor-number 1
"""

import argparse
import json
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore

TEMPLATES_DIR = Path("templates")
ZONES_DIR = Path("zones")

# dossier local -> collection Firestore (même mapping que les scripts de génération)
KIND_TO_FOLDER = {
    "monster": "monsters",
    "resource": "resources",
    "npc": "npcs",
    "quest": "quests",
    "shop": "shops",
    "player_market": "player_markets",
    "item": "items",
    "skill": "skills",
    "title": "titles",
}
COLLECTION_PREFIX = "templates_"

ZONE_CONTENT_COLLECTION = "zone_content"       # contenu statique — jamais "zones" (état dynamique)
FLOOR_SKELETON_COLLECTION = "floor_skeletons"


def sanitize_for_firestore(data, path=""):
    """Firestore refuse les tableaux de tableaux (array-of-array). 'connections' dans le squelette
    d'étage est le seul endroit connu du modèle avec cette forme ([[a,b],[c,d]]) — converti en
    liste d'objets {from, to}. Toute AUTRE occurrence inattendue lève une erreur claire plutôt que
    de planter côté serveur avec un message cryptique."""
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            if k == "connections" and isinstance(v, list) and all(isinstance(p, list) for p in v):
                result[k] = [{"from": p[0], "to": p[1]} for p in v]
            else:
                result[k] = sanitize_for_firestore(v, f"{path}.{k}")
        return result
    elif isinstance(data, list):
        for i, item in enumerate(data):
            if isinstance(item, list):
                raise ValueError(
                    f"Tableau imbriqué inattendu à '{path}[{i}]' — Firestore ne supporte pas "
                    f"les array-of-array. Ajoute un cas spécifique dans sanitize_for_firestore(), "
                    f"comme celui déjà fait pour 'connections'."
                )
        return [sanitize_for_firestore(item, f"{path}[{i}]") for i, item in enumerate(data)]
    else:
        return data


def init_firestore(service_account_path: str):
    cred = credentials.Certificate(service_account_path)
    firebase_admin.initialize_app(cred)
    return firestore.client()


def sync_templates(db) -> int:
    count = 0
    for kind, folder in KIND_TO_FOLDER.items():
        folder_path = TEMPLATES_DIR / folder
        if not folder_path.exists():
            continue
        collection = db.collection(f"{COLLECTION_PREFIX}{folder}")
        for file in folder_path.glob("*.json"):
            data = json.loads(file.read_text(encoding="utf-8"))
            doc_id = data.get("id", file.stem)
            collection.document(doc_id).set(sanitize_for_firestore(data, f"{folder}/{doc_id}"))
            count += 1
            print(f"  📤 {COLLECTION_PREFIX}{folder}/{doc_id}")
    return count


def sync_zone_content(db, floor_number: int = None) -> int:
    count = 0
    collection = db.collection(ZONE_CONTENT_COLLECTION)
    if floor_number is not None:
        floor_dirs = [ZONES_DIR / f"floor_{floor_number}"]
    else:
        floor_dirs = sorted(ZONES_DIR.glob("floor_*"))

    for floor_dir in floor_dirs:
        if not floor_dir.exists():
            print(f"⚠️  Dossier introuvable, ignoré : {floor_dir}")
            continue
        for file in floor_dir.glob("*.json"):
            zone = json.loads(file.read_text(encoding="utf-8"))
            doc_id = zone.get("id", file.stem)
            collection.document(doc_id).set(sanitize_for_firestore(zone, f"zone_content/{doc_id}"))
            count += 1
            print(f"  📤 {ZONE_CONTENT_COLLECTION}/{doc_id}")
    return count


def sync_floor_skeleton(db, skeleton_path: str) -> int:
    path = Path(skeleton_path)
    if not path.exists():
        print(f"⚠️  Squelette introuvable : {path}")
        return 0
    skeleton = json.loads(path.read_text(encoding="utf-8"))
    doc_id = skeleton.get("id", path.stem)
    db.collection(FLOOR_SKELETON_COLLECTION).document(doc_id).set(
        sanitize_for_firestore(skeleton, f"floor_skeletons/{doc_id}")
    )
    print(f"  📤 {FLOOR_SKELETON_COLLECTION}/{doc_id}")
    return 1


def main():
    parser = argparse.ArgumentParser(description="Synchronise le contenu généré vers Firestore.")
    parser.add_argument("--service-account", type=str, required=True,
                         help="Chemin vers le fichier JSON de clé de compte de service")
    parser.add_argument("--skeleton", type=str, default=None,
                         help="Chemin d'un squelette d'étage à synchroniser (optionnel)")
    parser.add_argument("--floor-number", type=int, default=None,
                         help="Ne synchroniser que les zones de cet étage (par défaut : tous)")
    parser.add_argument("--skip-templates", action="store_true")
    parser.add_argument("--skip-zones", action="store_true")
    args = parser.parse_args()

    db = init_firestore(args.service_account)

    total = 0
    if not args.skip_templates:
        print("📚 Synchronisation des templates...")
        total += sync_templates(db)

    if not args.skip_zones:
        print("🗺️  Synchronisation du contenu de zone...")
        total += sync_zone_content(db, args.floor_number)

    if args.skeleton:
        print("🏔️  Synchronisation du squelette d'étage...")
        total += sync_floor_skeleton(db, args.skeleton)

    print(f"\n✅ {total} document(s) synchronisé(s) vers Firestore.")


if __name__ == "__main__":
    main()