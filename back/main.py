#!/usr/bin/env python3
"""
Serveur Arthesia.

Prérequis :
    pip install fastapi uvicorn firebase-admin

Lancement (rechargement automatique à chaque modif du fichier) :
    uvicorn main:app --reload
"""

import json

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
try:
    from back.content_routes import router as content_router
    from back.action_routes import router as action_router
except ModuleNotFoundError:
    from content_routes import router as content_router
    from action_routes import router as action_router

try:
    from .database import db
    from .player_routes import router as player_router
    from .combat_routes import router as combat_router
except ImportError:
    from database import db
    from player_routes import router as player_router
    from combat_routes import router as combat_router

app = FastAPI(title="Arthesia Server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(content_router)
app.include_router(action_router)
app.include_router(player_router)
app.include_router(combat_router)




@app.get("/")
def root():
    return {"message": "Serveur Arthesia en ligne. Va voir /health pour vérifier la connexion Firestore."}


@app.get("/health")
def health_check():
    """Vérifie que le serveur tourne ET arrive à parler à Firestore."""
    try:
        docs = list(db.collection("templates_monsters").limit(1).stream())
        firestore_ok = True
        sample = docs[0].to_dict().get("name") if docs else None
    except Exception as e:
        firestore_ok = False
        sample = str(e)

    return {
        "server": "ok",
        "firestore": "ok" if firestore_ok else "erreur",
        "exemple_lu": sample,
    }