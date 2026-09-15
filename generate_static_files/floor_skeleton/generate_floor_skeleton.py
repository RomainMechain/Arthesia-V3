#!/usr/bin/env python3
"""
Génère le squelette d'un étage Arthesia (zones, connexions, conditions de déblocage)
via l'API Claude, à partir du prompt arthesia_floor_skeleton_prompt.md.

Prérequis :
    pip install anthropic
    export ANTHROPIC_API_KEY=sk-ant-...   (ou variable d'environnement Windows équivalente)

Usage :
    python generate_floor_skeleton.py --floor-number 1 --theme "Village au bord d'un lac, ambiance paisible mais mystérieuse, ruines architectes en périphérie" --zones 8
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import anthropic

# Le script est dans generate_static_files/floor_skeleton/ et les sorties
# existantes sont stockées dans zone_skeletons/ à la racine du projet.
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
SKELETONS_DIR = PROJECT_ROOT / "zone_skeletons"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    sys.exit("❌ Variable d'environnement ANTHROPIC_API_KEY absente.")

MODEL = "claude-sonnet-5"
PROMPT_FILE = SCRIPT_DIR / "arthesia_floor_skeleton_prompt.md"


def load_system_prompt() -> str:
    if not PROMPT_FILE.exists():
        sys.exit(f"❌ Prompt introuvable : {PROMPT_FILE}\n"
                  f"   Place arthesia_floor_skeleton_prompt.md dans le même dossier que ce script.")
    return PROMPT_FILE.read_text(encoding="utf-8")


def build_user_message(floor_number: int, theme_brief: str, zone_count: int,
                        previous_summary: Optional[str]) -> str:
    lines = [
        "Génère le squelette de cet étage avec les paramètres suivants :",
        f"- floor_number: {floor_number}",
        f"- theme_brief: {theme_brief}",
        f"- zone_count_target: {zone_count}",
    ]
    if previous_summary:
        lines.append(f"- previous_floor_summary: {previous_summary}")
    lines.append("\nRéponds uniquement avec le JSON demandé, sans aucun texte avant ou après.")
    return "\n".join(lines)


def extract_json(text: str) -> dict:
    """Enlève d'éventuels ```json ... ``` autour de la réponse avant de parser."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def generate_floor_skeleton(floor_number: int, theme_brief: str, zone_count: int,
                             previous_summary: Optional[str] = None) -> dict:
    client = anthropic.Client(api_key=ANTHROPIC_API_KEY)

    system_prompt = load_system_prompt()
    user_message = build_user_message(floor_number, theme_brief, zone_count, previous_summary)

    print(f"🌐 Appel à {MODEL}...")
    response = client.messages.create(
        model=MODEL,
        max_tokens=10000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    raw_text = "".join(block.text for block in response.content if block.type == "text")

    try:
        return extract_json(raw_text)
    except json.JSONDecodeError:
        print("❌ La réponse n'est pas un JSON valide.")
        print(f"📡 stop_reason = {response.stop_reason}")
        print("---- Réponse brute ----")
        print(raw_text)
        raise


def extract_leaf_conditions(cond: Optional[dict]) -> list[dict]:
    """Déplie récursivement un group/all_of pour récupérer toutes les conditions
    'feuilles' (item/quest/boss/level/zone), peu importe leur profondeur d'imbrication."""
    if not cond:
        return []
    if cond.get("type") == "group":
        leaves = []
        for sub_cond in cond.get("conditions", []):
            leaves.extend(extract_leaf_conditions(sub_cond))
        return leaves
    return [cond]


def validate_skeleton(skeleton: dict) -> list[str]:
    """Vérifications automatiques côté script, en plus de la checklist demandée au modèle."""
    warnings = []
    zones = skeleton.get("zones", [])
    zone_ids = {z["id"] for z in zones}

    if skeleton.get("start_zone_id") not in zone_ids:
        warnings.append("start_zone_id ne correspond à aucune zone.")
    if skeleton.get("boss_zone_id") not in zone_ids:
        warnings.append("boss_zone_id ne correspond à aucune zone.")

    # Connectivité du graphe depuis la zone de départ
    connected = {skeleton.get("start_zone_id")}
    changed = True
    while changed:
        changed = False
        for a, b in skeleton.get("connections", []):
            if a in connected and b not in connected:
                connected.add(b)
                changed = True
            elif b in connected and a not in connected:
                connected.add(a)
                changed = True
    unreachable = zone_ids - connected
    if unreachable:
        warnings.append(f"Zones non atteignables depuis le départ : {sorted(unreachable)}")

    # Toute condition item/quest/boss du manifeste doit avoir un resolved_by
    for cond in skeleton.get("condition_manifest", []):
        if not cond.get("resolved_by"):
            warnings.append(f"Condition sans resolved_by : {cond.get('condition')}")

    # Zones avec condition item/quest/boss (même imbriquée dans un group) absente du manifeste
    manifest_targets = {c["condition"].get("item_id") or c["condition"].get("quest_id")
                         or c["condition"].get("zone_id")
                         for c in skeleton.get("condition_manifest", [])}
    for zone in zones:
        for leaf in extract_leaf_conditions(zone.get("unlock_condition")):
            if leaf.get("type") in ("item", "quest", "boss"):
                key = leaf.get("item_id") or leaf.get("quest_id") or leaf.get("zone_id")
                if key not in manifest_targets:
                    warnings.append(f"Zone '{zone['id']}' a une condition {leaf.get('type')} "
                                     f"({key}) absente du condition_manifest.")

    return warnings


def main():
    parser = argparse.ArgumentParser(description="Génère le squelette d'un étage Arthesia.")
    parser.add_argument("--floor-number", type=int, required=True)
    parser.add_argument("--theme", type=str, required=True, help="Brief thématique de l'étage")
    parser.add_argument("--zones", type=int, default=8, help="Nombre de zones visé")
    parser.add_argument("--previous-summary", type=str, default=None)
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    skeleton = generate_floor_skeleton(
        floor_number=args.floor_number,
        theme_brief=args.theme,
        zone_count=args.zones,
        previous_summary=args.previous_summary,
    )

    warnings = validate_skeleton(skeleton)
    if warnings:
        print("\n⚠️  Points à vérifier avant d'utiliser ce squelette :")
        for w in warnings:
            print(f"  - {w}")
    else:
        print("\n✅ Vérifications automatiques passées (graphe connecté, manifeste résolu).")

    output_path = (Path(args.output).resolve()
                   if args.output
                   else SKELETONS_DIR / f"floor_{args.floor_number}_skeleton.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(skeleton, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n💾 Squelette sauvegardé : {output_path}")
    print(f"   {len(skeleton.get('zones', []))} zones, {len(skeleton.get('connections', []))} connexions")


if __name__ == "__main__":
    main()