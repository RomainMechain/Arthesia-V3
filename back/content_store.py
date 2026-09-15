"""Lecture du contenu statique local et des templates."""

import json

from fastapi import HTTPException

try:
    from .config import KIND_TO_FOLDER, SKELETONS_DIR, TEMPLATES_DIR, ZONES_DIR
except ImportError:
    from config import KIND_TO_FOLDER, SKELETONS_DIR, TEMPLATES_DIR, ZONES_DIR


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


def load_template(kind: str, template_id: str) -> dict:
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


def load_all_floor_skeletons() -> list[dict]:
    skeletons = []
    for path in SKELETONS_DIR.glob("floor_*_skeleton.json"):
        try:
            skeletons.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return skeletons
