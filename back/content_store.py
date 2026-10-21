"""Lecture du contenu statique local et des templates."""

import json

from fastapi import HTTPException

try:
    from .config import KIND_TO_FOLDER, SKELETONS_DIR, TEMPLATES_DIR, ZONES_DIR
    from .rarity import apply_rarity, split_variant_id
except ImportError:
    from config import KIND_TO_FOLDER, SKELETONS_DIR, TEMPLATES_DIR, ZONES_DIR
    from rarity import apply_rarity, split_variant_id


def load_static_zone_content(zone_id: str) -> dict:
    for floor_dir in ZONES_DIR.glob("floor_*"):
        path = floor_dir / f"{zone_id}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    raise HTTPException(404, f"Zone introuvable en local : {zone_id}")


def find_entry(zone_static: dict, entry_id: str) -> dict:
    for entry in zone_static.get("entries", []):
        if entry["id"] == entry_id:
            return entry
    raise HTTPException(404, f"Entry introuvable dans la zone : {entry_id}")


def find_quest_givers(quest_id: str) -> list[dict]:
    """Liste les zones dont une entry ``quest_giver`` propose cette quête."""
    givers = []
    for path in sorted(ZONES_DIR.glob("floor_*/*.json")):
        zone = json.loads(path.read_text(encoding="utf-8"))
        for entry in zone.get("entries", []):
            if entry.get("template_type") == "quest_giver" and entry.get("template_id") == quest_id:
                givers.append({"zone_id": zone["id"], "zone_name": zone.get("name", zone["id"]), "entry": entry})
    return givers


def load_template(kind: str, template_id: str) -> dict:
    # Une variante de rang (« lame~rare ») se calcule depuis sa fiche de base.
    if kind == "item":
        base_id, rarity = split_variant_id(template_id)
        if rarity is not None:
            return apply_rarity(load_template("item", base_id), rarity)
    folder = KIND_TO_FOLDER.get(kind)
    if folder is None:
        raise HTTPException(404, f"Type de template introuvable en local : {kind}")
    path = TEMPLATES_DIR / folder / f"{template_id}.json"
    if not path.exists():
        raise HTTPException(404, f"Template introuvable en local : {kind}/{template_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def get_default_start_zone_id() -> str | None:
    skeleton_path = SKELETONS_DIR / "floor_1_skeleton.json"
    if not skeleton_path.exists():
        return None
    try:
        skeleton = json.loads(skeleton_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return skeleton.get("start_zone_id")


def floor_number(floor: dict) -> int:
    """Numéro d'un étage d'après son id (``floor_3`` -> 3)."""
    try:
        return int(str(floor.get("id", "")).rsplit("_", 1)[-1])
    except ValueError:
        return 0


def load_ordered_floors() -> list[dict]:
    """Squelettes d'étage triés du premier au dernier."""
    return sorted(load_all_floor_skeletons(), key=floor_number)


def load_all_floor_skeletons() -> list[dict]:
    skeletons = []
    for path in SKELETONS_DIR.glob("floor_*_skeleton.json"):
        try:
            skeletons.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return skeletons
