"""Configuration et chemins partages du serveur Arthesia."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKELETONS_DIR = PROJECT_ROOT / "zone_skeletons"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
ZONES_DIR = PROJECT_ROOT / "zones"
# Hébergé (Render), la clé est un fichier secret monté ailleurs : FIREBASE_KEY_PATH.
SERVICE_ACCOUNT_PATH = Path(os.environ.get("FIREBASE_KEY_PATH") or PROJECT_ROOT / "firebase_key.json")
CRAFT_PROMPT_FILE = PROJECT_ROOT / "generate_static_files" / "craft" / "arthesia_craft_prompt.md"

# Craft inédit : Gemini (offre gratuite de Google) d'abord, Claude si une clé
# est fournie, sinon le générateur sans IA (craft_procedural).
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# Sans GEMINI_MODEL, le serveur choisit lui-même le meilleur modèle « flash »
# disponible pour la clé (craft_service.gemini_model) : les noms changent souvent.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CRAFT_MODEL = "claude-sonnet-5"

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

if not SERVICE_ACCOUNT_PATH.exists():
    raise FileNotFoundError(
        f"Cle de compte de service introuvable : {SERVICE_ACCOUNT_PATH}\n"
        "Place firebase_key.json à la racine du dépôt, ou indique son chemin dans FIREBASE_KEY_PATH."
    )
