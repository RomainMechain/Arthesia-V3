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
        # Pseudo visible par les autres joueurs (Autel des Ventes...).
        "display_name": player_id,
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
        # PV hors combat (game_rules.current_hp) ; None : en pleine forme.
        "health": {"current": None, "at": None},
    }


def build_new_player_state(player_id: str, current_zone_id: str | None = None, display_name: str | None = None) -> dict:
    state = default_player_state(player_id)
    if display_name:
        state["display_name"] = display_name
    if current_zone_id is not None:
        state["current_zone_id"] = current_zone_id
    return state


# Les lectures et écritures acceptent une transaction Firestore optionnelle.
# Sans transaction, elles agissent immédiatement ; avec une transaction, les
# lectures sont verrouillées et les écritures ne partent qu'au commit. Toute
# action qui lit puis réécrit un état partagé entre requêtes concurrentes
# (argent, inventaire, annonces, spawns partagés) doit passer par une
# transaction, sinon deux requêtes simultanées peuvent dupliquer des gains.

def player_ref(player_id: str):
    return db.collection("players").document(player_id)


def zone_state_ref(zone_id: str):
    return db.collection("zones").document(zone_id)


def get_player(player_id: str, transaction=None) -> dict:
    doc = player_ref(player_id).get(transaction=transaction)
    return doc.to_dict() if doc.exists else default_player_state(player_id)


def save_player(player_id: str, player: dict, transaction=None) -> None:
    if transaction is None:
        player_ref(player_id).set(player)
    else:
        transaction.set(player_ref(player_id), player)


def get_zone_dynamic_state(zone_id: str, transaction=None) -> dict:
    doc = zone_state_ref(zone_id).get(transaction=transaction)
    return doc.to_dict() if doc.exists else {"zone_id": zone_id, "entries": {}}


def save_zone_dynamic_state(zone_id: str, state: dict, transaction=None) -> None:
    if transaction is None:
        zone_state_ref(zone_id).set(state)
    else:
        transaction.set(zone_state_ref(zone_id), state)
