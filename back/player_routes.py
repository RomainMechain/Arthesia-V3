"""Routes HTTP de gestion du joueur et de ses statistiques."""

from fastapi import APIRouter, HTTPException

try:
    from .database import db
    from .game_rules import POINT_VALUE, compute_final_stats, xp_to_next_level
    from .models import CreatePlayerRequest
    from .player_store import build_new_player_state
except ImportError:
    from database import db
    from game_rules import POINT_VALUE, compute_final_stats, xp_to_next_level
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
