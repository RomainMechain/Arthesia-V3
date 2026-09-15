"""Routes HTTP de gestion du joueur et de ses statistiques."""

from fastapi import APIRouter, HTTPException

try:
    from .database import db
    from .game_rules import compute_final_stats
    from .models import CreatePlayerRequest
    from .player_store import build_new_player_state
except ImportError:
    from database import db
    from game_rules import compute_final_stats
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
    return {
        "stats": compute_final_stats(player),
        "note": "Passifs conditionnels (temps réel combat) non inclus — voir catégorie D, hors périmètre.",
    }
