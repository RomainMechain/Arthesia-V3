"""Routes HTTP de gestion du joueur et de ses statistiques."""

import re

from fastapi import APIRouter, Depends, HTTPException
from firebase_admin import firestore

try:
    from .auth import require_player
    from .carnet import build_carnet
    from .database import db
    from .content_store import find_quest_givers, load_ordered_floors, load_template
    from .balance import profession_xp_to_next_level, respec_cost
    from .game_rules import (
        POINT_VALUE, compute_final_stats, health_view, quest_progress_view, reachable_floor_ids, xp_to_next_level,
        zone_type,
    )
    from .models import CreatePlayerRequest
    from .player_store import build_new_player_state
    from .progression import check_and_apply_unlocks
except ImportError:
    from auth import require_player
    from carnet import build_carnet
    from database import db
    from content_store import find_quest_givers, load_ordered_floors, load_template
    from balance import profession_xp_to_next_level, respec_cost
    from game_rules import (
        POINT_VALUE, compute_final_stats, health_view, quest_progress_view, reachable_floor_ids, xp_to_next_level,
        zone_type,
    )
    from models import CreatePlayerRequest
    from player_store import build_new_player_state
    from progression import check_and_apply_unlocks

# Toutes les routes joueur exigent d'être connecté en tant que ce joueur.
router = APIRouter(dependencies=[Depends(require_player)])

# Pseudo : 3 à 20 caractères, lettres (accents compris), chiffres, espace, - _ '
DISPLAY_NAME_PATTERN = re.compile(r"^[\w À-ÿ'-]{3,20}$")


def with_health(player: dict) -> dict:
    """Ajoute les PV actuels (régénération comprise) à l'état renvoyé au client."""
    return {**player, "health_view": health_view(player)}


@router.post("/players")
def create_player(req: CreatePlayerRequest):
    """Crée le personnage du joueur connecté. Le pseudo est unique (sans
    tenir compte des majuscules) : il est réservé dans ``display_names``."""
    display_name = (req.display_name or req.player_id).strip()
    if not DISPLAY_NAME_PATTERN.match(display_name):
        raise HTTPException(400, "Pseudo invalide : 3 à 20 caractères, lettres, chiffres, espaces, - _ '")
    _create_player_tx(db.transaction(), req, display_name)
    # Les contenus sans condition ou à la portée d'un niveau 1 sont débloqués
    # dès la création.
    check_and_apply_unlocks(req.player_id)
    return with_health(db.collection("players").document(req.player_id).get().to_dict())


@firestore.transactional
def _create_player_tx(transaction, req: CreatePlayerRequest, display_name: str) -> dict:
    doc_ref = db.collection("players").document(req.player_id)
    name_ref = db.collection("display_names").document(display_name.lower())
    if doc_ref.get(transaction=transaction).exists:
        raise HTTPException(409, "Ce compte a déjà un personnage")
    if name_ref.get(transaction=transaction).exists:
        raise HTTPException(409, f"Le pseudo « {display_name} » est déjà pris")

    player_state = build_new_player_state(req.player_id, req.current_zone_id, display_name)
    transaction.set(doc_ref, player_state)
    transaction.set(name_ref, {"player_id": req.player_id, "display_name": display_name})
    return player_state


@router.get("/players/{player_id}")
def get_player_state(player_id: str):
    doc = db.collection("players").document(player_id).get()
    if not doc.exists:
        raise HTTPException(404, f"Joueur '{player_id}' introuvable")
    return with_health(doc.to_dict())


@router.post("/players/{player_id}/sync")
def sync_player(player_id: str):
    """Début de session : réévalue les déblocages (un contenu ajouté ou
    régénéré depuis la dernière session peut être déjà accessible), puis
    renvoie l'état du joueur."""
    doc_ref = db.collection("players").document(player_id)
    # Vérifier l'existence avant : le déblocage créerait sinon un joueur vide.
    if not doc_ref.get().exists:
        raise HTTPException(404, f"Joueur '{player_id}' introuvable")
    check_and_apply_unlocks(player_id)
    return with_health(doc_ref.get().to_dict())


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
        # Réinitialisation des points : prix et disponibilité (en ville seulement).
        "respec": {
            "cost": respec_cost(player.get("level", 1), player.get("stat_resets", 0)),
            "in_town": zone_type(player.get("current_zone_id")) == "town",
        },
        "profession_xp_to_next_level": {
            profession_id: profession_xp_to_next_level(profession["level"])
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


@router.get("/players/{player_id}/carnet")
def get_player_carnet(player_id: str):
    """Carnet d'aventure : bestiaire, récoltes et habitants découverts, par étage."""
    doc = db.collection("players").document(player_id).get()
    if not doc.exists:
        raise HTTPException(404, f"Joueur '{player_id}' introuvable")
    return {"floors": build_carnet(doc.to_dict())}


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
