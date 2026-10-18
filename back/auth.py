"""Authentification des joueurs par jeton Firebase.

Le client se connecte à Firebase Authentication (e-mail et mot de passe) et
envoie son jeton d'identité dans l'en-tête ``Authorization: Bearer <jeton>``.
L'identifiant du joueur est son ``uid`` Firebase : une requête qui agit pour
un autre joueur (``player_id`` dans le chemin, la query ou le corps JSON) est
refusée.

En développement, ``ARTHESIA_AUTH=off`` désactive la vérification (pages HTML
de debug, scripts de test) : le ``player_id`` envoyé est alors cru tel quel.
"""

import os

from fastapi import HTTPException, Request
from firebase_admin import auth as firebase_auth

try:
    from . import database  # noqa: F401  (initialise l'application Firebase)
except ImportError:
    import database  # noqa: F401

AUTH_REQUIRED = os.environ.get("ARTHESIA_AUTH", "on").lower() != "off"


def verify_id_token(token: str) -> str:
    """Vérifie un jeton d'identité Firebase et renvoie l'uid du joueur."""
    if not token:
        raise HTTPException(401, "Connexion requise")
    try:
        return firebase_auth.verify_id_token(token)["uid"]
    except Exception as exc:
        raise HTTPException(401, "Session expirée ou invalide : reconnecte-toi") from exc


def check_player(uid: str | None, claimed_player_id: str | None) -> None:
    """Refuse d'agir pour un autre joueur que celui qui est connecté."""
    if uid is not None and claimed_player_id and claimed_player_id != uid:
        raise HTTPException(403, "Ce personnage ne t'appartient pas")


async def require_player(request: Request) -> str | None:
    """Dépendance FastAPI des routes joueur.

    Vérifie le jeton, puis que tout ``player_id`` de la requête (chemin, query,
    corps JSON) est celui du joueur connecté. Renvoie l'uid, ou ``None`` quand
    l'authentification est désactivée.
    """
    if not AUTH_REQUIRED:
        return None
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(401, "Connexion requise")
    uid = verify_id_token(header.removeprefix("Bearer ").strip())

    check_player(uid, request.path_params.get("player_id"))
    check_player(uid, request.query_params.get("player_id"))
    if request.method in ("POST", "PUT", "PATCH") and "application/json" in request.headers.get("content-type", ""):
        # FastAPI met le corps en cache : la route le relit normalement après.
        body = await request.json()
        if isinstance(body, dict):
            check_player(uid, body.get("player_id"))
    return uid
