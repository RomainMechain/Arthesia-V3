"""Acces Firestore pour l'etat des joueurs et des zones dynamiques."""

try:
    from .content_store import get_default_start_zone_id
    from .database import db
except ImportError:
    from content_store import get_default_start_zone_id
    from database import db


def default_player_state(player_id: str) -> dict:
    return {
        "player_id": player_id,
        "level": 1,
        "xp": 0,
        "stat_points": {
            "spent": {"hp": 0, "atk": 0, "def": 0, "agi": 0, "int": 0, "sag": 0},
            "available": 6,
        },
        "inventory": {},
        "equipment": {
            "weapon": None, "head": None, "chest": None, "gloves": None,
            "boots": None, "ring": None, "amulet": None, "tool": None,
        },
        "currency": {"or": 0},
        "skills": {"unlocked": [], "equipped_active": [], "equipped_passive": []},
        "titles": {"unlocked": [], "equipped": None},
        "professions": {},
        "lifetime_counters": {"kills": {}, "gathers": {}, "deliveries": {}},
        "quests": {},
        "unlocks": {"zones": [], "entries": [], "skills": [], "titles": []},
        "current_zone_id": get_default_start_zone_id(),
    }


def build_new_player_state(player_id: str, current_zone_id: str | None = None) -> dict:
    state = default_player_state(player_id)
    if current_zone_id is not None:
        state["current_zone_id"] = current_zone_id
    return state


def get_player(player_id: str) -> dict:
    doc = db.collection("players").document(player_id).get()
    return doc.to_dict() if doc.exists else default_player_state(player_id)


def save_player(player_id: str, player: dict) -> None:
    db.collection("players").document(player_id).set(player)


def get_zone_dynamic_state(zone_id: str) -> dict:
    doc = db.collection("zones").document(zone_id).get()
    return doc.to_dict() if doc.exists else {"zone_id": zone_id, "entries": {}}


def save_zone_dynamic_state(zone_id: str, state: dict) -> None:
    db.collection("zones").document(zone_id).set(state)
