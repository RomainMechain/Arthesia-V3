"""Routes HTTP de gestion du joueur et de ses statistiques."""

from fastapi import APIRouter, HTTPException

try:
    from .database import db
    from .content_store import find_quest_givers, load_ordered_floors, load_template
    from .game_rules import POINT_VALUE, compute_final_stats, quest_progress_view, reachable_floor_ids, xp_to_next_level
    from .models import CreatePlayerRequest
    from .player_store import build_new_player_state
except ImportError:
    from database import db
    from content_store import find_quest_givers, load_ordered_floors, load_template
    from game_rules import POINT_VALUE, compute_final_stats, quest_progress_view, reachable_floor_ids, xp_to_next_level
    from models import CreatePlayerRequest
    from player_store import build_new_player_state

router = APIRouter()


@router.post("/players")
def create_player(req: CreatePlayerRequest):
    doc_ref = db.collection("players").document(req.player_id)
    if doc_ref.get().exists:
        raise HTTPException(409, f"Le joueur '{req.player_id}' existe déjà")

    player_state = build_new_player_state(req.player_id, req.current_zone_id)
    doc_ref.set(player_state)
    return player_state


@router.get("/players/{player_id}")
def get_player_state(player_id: str):
    doc = db.collection("players").document(player_id).get()
    if not doc.exists:
        raise HTTPException(404, f"Joueur '{player_id}' introuvable")
    return doc.to_dict()


@router.get("/players/{player_id}/stats")
def get_player_stats(player_id: str):
    doc = db.collection("players").document(player_id).get()
    if not doc.exists:
        raise HTTPException(404, f"Joueur '{player_id}' introuvable")
    player = doc.to_dict()
    # Le client affiche la barre d'XP et l'effet d'un point de stat sans
    # dupliquer les formules du serveur.
    return {
        "stats": compute_final_stats(player),
        "xp_to_next_level": xp_to_next_level(player["level"]),
        "point_value": POINT_VALUE,
        "profession_xp_to_next_level": {
            profession_id: xp_to_next_level(profession["level"])
            for profession_id, profession in player["professions"].items()
        },
        "note": "Passifs conditionnels (temps réel combat) non inclus — voir catégorie D, hors périmètre.",
    }


@router.get("/players/{player_id}/quests")
def get_player_quests(player_id: str):
    """Journal de quêtes : chaque quête démarrée avec sa définition, sa
    progression et les zones où la rendre."""
    doc = db.collection("players").document(player_id).get()
    if not doc.exists:
        raise HTTPException(404, f"Joueur '{player_id}' introuvable")
    player = doc.to_dict()
    quests = []
    for quest_id, progress in player["quests"].items():
        quest_def = load_template("quest", quest_id)
        quests.append({
            "quest": quest_def,
            **quest_progress_view(quest_def, progress, player),
            "givers": [{"zone_id": giver["zone_id"], "zone_name": giver["zone_name"]} for giver in find_quest_givers(quest_id)],
        })
    return {"quests": quests}


@router.get("/players/{player_id}/floors")
def get_player_floors(player_id: str):
    """Étages que le joueur peut visiter, dans l'ordre (navigation de la carte)."""
    doc = db.collection("players").document(player_id).get()
    if not doc.exists:
        raise HTTPException(404, f"Joueur '{player_id}' introuvable")
    floors = load_ordered_floors()
    return {
        "reachable": reachable_floor_ids(floors, doc.to_dict()),
        "floors": [{"id": f["id"], "name": f.get("name")} for f in floors],
    }
