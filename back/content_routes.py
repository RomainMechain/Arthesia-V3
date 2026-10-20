from pathlib import Path
import json

from fastapi import APIRouter, HTTPException


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKELETONS_DIR = PROJECT_ROOT / "zone_skeletons"
ZONES_DIR = PROJECT_ROOT / "zones"
TEMPLATES_DIR = PROJECT_ROOT / "templates"

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
    "workshop": "workshops",
}

router = APIRouter(prefix="/content", tags=["content"])


def read_json(path: Path) -> dict | list:
    if not path.exists():
        raise HTTPException(404, f"Fichier introuvable : {path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(500, f"JSON invalide : {path.name}") from exc


def load_zone(zone_id: str) -> dict:
    for floor_dir in sorted(ZONES_DIR.glob("floor_*")):
        path = floor_dir / f"{zone_id}.json"
        if path.exists():
            return read_json(path)
    raise HTTPException(404, f"Zone introuvable : {zone_id}")


def load_floor(floor_id: str) -> dict:
    return read_json(SKELETONS_DIR / f"{floor_id}_skeleton.json")


@router.get("/floors")
def list_floors() -> list[dict]:
    result = []
    for path in sorted(SKELETONS_DIR.glob("floor_*_skeleton.json")):
        floor = read_json(path)
        result.append({
            "id": floor.get("id", path.stem.removesuffix("_skeleton")),
            "name": floor.get("name"),
            "description": floor.get("description"),
            "start_zone_id": floor.get("start_zone_id"),
            "boss_zone_id": floor.get("boss_zone_id"),
            "zone_count": len(floor.get("zones", [])),
        })
    return result


@router.get("/floors/{floor_id}")
def get_floor(floor_id: str) -> dict:
    floor = load_floor(floor_id)
    return {
        "id": floor.get("id", floor_id),
        "name": floor.get("name"),
        "description": floor.get("description"),
        "start_zone_id": floor.get("start_zone_id"),
        "boss_zone_id": floor.get("boss_zone_id"),
        "zones": [public_zone(zone) for zone in floor.get("zones", [])],
        "connections": floor.get("connections", []),
    }


def public_zone(zone: dict) -> dict:
    """Zone telle que le client la voit : sa plage de niveaux, mais pas le
    ``generation_brief``, qui contient les consignes de génération (et donc
    des secrets de l'étage)."""
    view = {key: value for key, value in zone.items() if key != "generation_brief"}
    view["level_range"] = (zone.get("generation_brief") or {}).get("level_range")
    return view


@router.get("/zones")
def list_zones(floor_id: str | None = None) -> list[dict]:
    floor_paths = [SKELETONS_DIR / f"{floor_id}_skeleton.json"] if floor_id else sorted(SKELETONS_DIR.glob("floor_*_skeleton.json"))
    result = []
    for path in floor_paths:
        floor = read_json(path)
        for zone in floor.get("zones", []):
            result.append({
                "id": zone["id"],
                "name": zone.get("name"),
                "type": zone.get("type"),
                "position": zone.get("position"),
                "visibility": zone.get("visibility"),
                "unlock_condition": zone.get("unlock_condition"),
                "floor_id": floor.get("id", path.stem.removesuffix("_skeleton")),
            })
    return result


@router.get("/zones/{zone_id}")
def get_zone(zone_id: str) -> dict:
    zone = load_zone(zone_id)
    return {
        "id": zone["id"],
        "name": zone.get("name"),
        "type": zone.get("type"),
        "position": zone.get("position"),
        "description": zone.get("description"),
        "unlock_condition": zone.get("unlock_condition"),
        "hint": zone.get("hint"),
        "entries": zone.get("entries", []),
    }


@router.get("/zones/{zone_id}/entries")
def list_zone_entries(zone_id: str) -> list[dict]:
    return load_zone(zone_id).get("entries", [])


# Templates dont la condition de déblocage n'est jamais envoyée au client.
SECRET_CONDITION_KINDS = {"skill", "title"}


@router.get("/templates/{kind}/{template_id}")
def get_template(kind: str, template_id: str) -> dict:
    folder = KIND_TO_FOLDER.get(kind)
    if folder is None:
        raise HTTPException(400, f"Type de template inconnu : {kind}")
    template = read_json(TEMPLATES_DIR / folder / f"{template_id}.json")
    if kind in SECRET_CONDITION_KINDS:
        # Comment débloquer une compétence ou un titre reste un secret à
        # découvrir (ou à se transmettre entre joueurs) : le serveur seul
        # évalue la condition.
        template = {k: v for k, v in template.items() if k != "unlock_condition"}
    return template


@router.get("/templates/{kind}")
def list_templates(kind: str) -> list[dict]:
    folder = KIND_TO_FOLDER.get(kind)
    if folder is None:
        raise HTTPException(400, f"Type de template inconnu : {kind}")
    result = []
    for path in sorted((TEMPLATES_DIR / folder).glob("*.json")):
        template = read_json(path)
        result.append({
            "id": template.get("id", path.stem),
            "name": template.get("name"),
            "kind": kind,
            "level": template.get("level"),
            "description": template.get("description"),
        })
    return result
