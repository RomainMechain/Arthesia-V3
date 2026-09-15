#!/usr/bin/env python3
"""
Serveur Arthesia.

Prérequis :
    pip install fastapi uvicorn firebase-admin

Lancement (rechargement automatique à chaque modif du fichier) :
    uvicorn main:app --reload
"""

from pathlib import Path
from datetime import datetime, timedelta, timezone
import asyncio
import time
import hashlib
import json
import os
import uuid
import random
import re

import anthropic
import firebase_admin
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from firebase_admin import credentials, firestore
try:
    from back.content_routes import router as content_router
except ModuleNotFoundError:
    from content_routes import router as content_router

# La racine du projet est le dossier parent de back/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKELETONS_DIR = PROJECT_ROOT / "zone_skeletons"

# La clé est fournie par l'environnement, jamais stockée dans le code.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CRAFT_MODEL = "claude-sonnet-5"
CRAFT_PROMPT_FILE = PROJECT_ROOT / "generate_static_files" / "craft" / "arthesia_craft_prompt.md"

# ⚠️ Ne jamais commit ce fichier.
SERVICE_ACCOUNT_PATH = PROJECT_ROOT / "firebase_key.json"

TEMPLATES_DIR = PROJECT_ROOT / "templates"
ZONES_DIR = PROJECT_ROOT / "zones"

KIND_TO_FOLDER = {
    "monster": "monsters", "resource": "resources", "npc": "npcs",
    "quest": "quests", "shop": "shops", "player_market": "player_markets",
    "item": "items", "skill": "skills", "title": "titles", "workshop": "workshops",
}

if not SERVICE_ACCOUNT_PATH.exists():
    raise FileNotFoundError(
        f"Clé de compte de service introuvable : {SERVICE_ACCOUNT_PATH}\n"
        f"Ajuste SERVICE_ACCOUNT_PATH en haut de ce fichier."
    )

cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
firebase_admin.initialize_app(cred)
db = firestore.client()

app = FastAPI(title="Arthesia Server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(content_router)


# =============================================================================
# CONTENU STATIQUE — lu depuis les fichiers locaux, pas Firestore (le serveur tourne
# à côté des fichiers générés ; pas besoin de les dupliquer tant qu'il n'est pas hébergé ailleurs)
# =============================================================================

def load_static_zone_content(zone_id: str) -> dict:
    for floor_dir in ZONES_DIR.glob("floor_*"):
        path = floor_dir / f"{zone_id}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    raise HTTPException(404, f"Zone introuvable en local : {zone_id}")


def find_entry(zone_static: dict, entry_id: str) -> dict:
    for entry in zone_static.get("entries", []):
        if entry["id"] == entry_id:
            return entry
    raise HTTPException(404, f"Entry introuvable dans la zone : {entry_id}")


def load_template(kind: str, template_id: str) -> dict:
    folder = KIND_TO_FOLDER.get(kind)
    path = TEMPLATES_DIR / folder / f"{template_id}.json"
    if not path.exists():
        raise HTTPException(404, f"Template introuvable en local : {kind}/{template_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def get_default_start_zone_id() -> str | None:
    """Essaie de récupérer la zone de départ depuis le squelette du floor 1.
    ⚠️ Hardcodé sur floor_1 pour l'instant — à généraliser le jour où il y a plusieurs étages jouables."""
    skeleton_path = SKELETONS_DIR / "floor_1_skeleton.json"
    if not skeleton_path.exists():
        return None
    try:
        skeleton = json.loads(skeleton_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return skeleton.get("start_zone_id")


# =============================================================================
# ÉTAT DYNAMIQUE — lu/écrit dans Firestore (players, zones = état partagé)
# =============================================================================

def default_player_state(player_id: str) -> dict:
    """Nouveau personnage : niveau 1, aucun point dépensé, 6 points disponibles à répartir
    (le premier lot de niveau). Voir arthesia_state_model.md — spent ≠ stats de base."""
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
    """Auto-crée un joueur par défaut s'il n'existe pas — comportement confirmé, gardé pour
    simplifier les tests (pas besoin de passer par POST /players avant chaque action)."""
    doc = db.collection("players").document(player_id).get()
    return doc.to_dict() if doc.exists else default_player_state(player_id)


def save_player(player_id: str, player: dict) -> None:
    db.collection("players").document(player_id).set(player)


def get_zone_dynamic_state(zone_id: str) -> dict:
    doc = db.collection("zones").document(zone_id).get()
    return doc.to_dict() if doc.exists else {"zone_id": zone_id, "entries": {}}


def save_zone_dynamic_state(zone_id: str, state: dict) -> None:
    db.collection("zones").document(zone_id).set(state)


# =============================================================================
# LOGIQUE DE JEU
# =============================================================================

def roll_loot_table(loot_table: list[dict]) -> tuple[str, int] | None:
    """Un seul objet tiré, pondéré par 'weight'."""
    if not loot_table:
        return None
    total_weight = sum(e["weight"] for e in loot_table)
    roll = random.uniform(0, total_weight)
    cumulative = 0
    for entry in loot_table:
        cumulative += entry["weight"]
        if roll <= cumulative:
            qty = random.randint(entry["quantity_min"], entry["quantity_max"])
            return entry["item_id"], qty
    return None


def compute_kill_xp(monster_level: int, role: str) -> int:
    """⚠️ Formule provisoire, pas encore dans arthesia_balance_reference.md — à calibrer."""
    multiplier = {"normal": 10, "elite": 30, "boss": 100}.get(role, 10)
    return monster_level * multiplier


def xp_to_next_level(level: int) -> int:
    """Même formule pour le joueur et les métiers : 50 × niveau
    (arthesia_data_model.md, section Métiers — jamais posée pour le joueur avant, réutilisée
    telle quelle par cohérence plutôt que d'en inventer une différente sans raison)."""
    return 50 * max(level, 1)


def apply_profession_level_ups(profession: dict) -> None:
    while profession["xp"] >= xp_to_next_level(profession["level"]):
        profession["xp"] -= xp_to_next_level(profession["level"])
        profession["level"] += 1


def apply_player_level_ups(player: dict) -> int:
    """Applique les montées de niveau en attente, accorde 6 points de stat par niveau
    (arthesia_balance_reference.md). Retourne le nombre de niveaux gagnés."""
    levels_gained = 0
    while player["xp"] >= xp_to_next_level(player["level"]):
        player["xp"] -= xp_to_next_level(player["level"])
        player["level"] += 1
        player["stat_points"]["available"] += 6
        levels_gained += 1
    return levels_gained


def is_boss_defeated(zone_id: str) -> bool:
    try:
        zone_static = load_static_zone_content(zone_id)
    except HTTPException:
        return False
    zone_state = get_zone_dynamic_state(zone_id)
    for entry in zone_static.get("entries", []):
        if entry["template_type"] != "monster":
            continue
        monster = load_template("monster", entry["template_id"])
        if monster.get("role") == "boss":
            entry_state = zone_state["entries"].get(entry["id"], {})
            if entry_state.get("status") == "defeated":
                return True
    return False


def evaluate_condition(condition: dict | None, player: dict) -> bool:
    if condition is None:
        return True
    t = condition["type"]
    if t == "level":
        return player["level"] >= condition["min"]
    if t == "quest":
        q = player["quests"].get(condition["quest_id"])
        return bool(q) and q.get("state") == condition["state"]
    if t == "item":
        return player["inventory"].get(condition["item_id"], 0) > 0
    if t == "zone":
        return condition["zone_id"] in player["unlocks"]["zones"]
    if t == "boss":
        return is_boss_defeated(condition["zone_id"])
    if t == "kill_count":
        return player["lifetime_counters"]["kills"].get(condition["monster_id"], 0) >= condition["count"]
    if t == "gather_count":
        return player["lifetime_counters"]["gathers"].get(condition["resource_id"], 0) >= condition["count"]
    if t == "deliver_item":
        return player["lifetime_counters"]["deliveries"].get(condition["item_id"], 0) >= condition["count"]
    if t == "group":
        results = [evaluate_condition(c, player) for c in condition["conditions"]]
        return all(results) if condition["mode"] == "all" else any(results)
    return False


def load_all_floor_skeletons() -> list[dict]:
    skeletons = []
    for path in SKELETONS_DIR.glob("floor_*_skeleton.json"):
        try:
            skeletons.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return skeletons


def check_and_apply_unlocks(player_id: str) -> list[str]:
    """Reteste toutes les conditions pas encore débloquées pour ce joueur. Ne filtre pas encore
    par 'zone atteignable' (limite connue déjà notée dans arthesia_state_model.md) — teste toutes
    les zones de tous les squelettes trouvés en local, ok à cette échelle."""
    player = get_player(player_id)
    newly_unlocked = []

    for skeleton in load_all_floor_skeletons():
        for zone in skeleton.get("zones", []):
            if zone["id"] in player["unlocks"]["zones"]:
                continue
            if evaluate_condition(zone.get("unlock_condition"), player):
                player["unlocks"]["zones"].append(zone["id"])
                newly_unlocked.append(f"zone:{zone['id']}")

    skills_dir = TEMPLATES_DIR / "skills"
    if skills_dir.exists():
        for path in skills_dir.glob("*.json"):
            skill = json.loads(path.read_text(encoding="utf-8"))
            if skill["id"] in player["skills"]["unlocked"]:
                continue
            if evaluate_condition(skill.get("unlock_condition"), player):
                player["skills"]["unlocked"].append(skill["id"])
                player["unlocks"]["skills"].append(skill["id"])
                newly_unlocked.append(f"skill:{skill['id']}")

    titles_dir = TEMPLATES_DIR / "titles"
    if titles_dir.exists():
        for path in titles_dir.glob("*.json"):
            title = json.loads(path.read_text(encoding="utf-8"))
            if title["id"] in player["titles"]["unlocked"]:
                continue
            if evaluate_condition(title.get("unlock_condition"), player):
                player["titles"]["unlocked"].append(title["id"])
                player["unlocks"]["titles"].append(title["id"])
                newly_unlocked.append(f"title:{title['id']}")

    if newly_unlocked:
        save_player(player_id, player)

    return newly_unlocked


def build_recipe_key(workshop_id: str, inputs: list[dict], parameters: dict) -> str:
    sorted_inputs = sorted(inputs, key=lambda i: i["item_id"])
    inputs_part = ",".join(f"{i['item_id']}:{i['quantity']}" for i in sorted_inputs)
    params_part = ",".join(f"{k}={v}" for k, v in sorted(parameters.items()))
    return f"{workshop_id}|{inputs_part}|{params_part}"


def recipe_key_doc_id(recipe_key: str) -> str:
    """Firestore n'aime pas certains caractères en id de doc — on hash, la clé lisible
    reste stockée comme champ à l'intérieur du document."""
    return hashlib.sha256(recipe_key.encode()).hexdigest()[:32]


def get_cached_craft(recipe_key: str) -> str | None:
    doc = db.collection("craft_cache").document(recipe_key_doc_id(recipe_key)).get()
    if not doc.exists:
        return None
    return doc.to_dict()["output_item_id"]


def save_craft_cache(recipe_key: str, output_item_id: str) -> None:
    db.collection("craft_cache").document(recipe_key_doc_id(recipe_key)).set({
        "recipe_key": recipe_key,
        "output_item_id": output_item_id,
    })


def load_craft_system_prompt() -> str:
    if not CRAFT_PROMPT_FILE.exists():
        raise HTTPException(500, f"Prompt de craft introuvable : {CRAFT_PROMPT_FILE}")
    return CRAFT_PROMPT_FILE.read_text(encoding="utf-8")


def generate_craft_item_via_ai(workshop: dict, parameters: dict, inputs_full: list[dict]) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    system_prompt = load_craft_system_prompt()

    workshop_info = {
        "id": workshop["id"], "name": workshop.get("name"),
        "description": workshop.get("description"), "parameters_choisis": parameters,
    }
    user_message = (
        "Génère le résultat de ce craft inédit :\n\n"
        f"workshop:\n{json.dumps(workshop_info, ensure_ascii=False, indent=2)}\n\n"
        f"inputs:\n{json.dumps(inputs_full, ensure_ascii=False, indent=2)}\n\n"
        "Réponds uniquement avec le JSON demandé, sans aucun texte avant ou après."
    )

    response = client.messages.create(
        model=CRAFT_MODEL, max_tokens=2000, system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    raw_text = "".join(b.text for b in response.content if b.type == "text")
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        return json.loads(cleaned)["item"]
    except (json.JSONDecodeError, KeyError):
        raise HTTPException(502, "Réponse IA invalide pour le craft — réessaie.")


def save_new_craft_item(item: dict) -> None:
    path = TEMPLATES_DIR / "items" / f"{item['id']}.json"
    if path.exists():
        raise HTTPException(500, f"Collision d'id sur l'item généré ('{item['id']}') — réessaie.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")


class MonsterKillRequest(BaseModel):
    player_id: str
    zone_id: str
    entry_id: str


class CreatePlayerRequest(BaseModel):
    player_id: str = Field(min_length=1)
    current_zone_id: str | None = None


@app.post("/players")
def create_player(req: CreatePlayerRequest):
    doc_ref = db.collection("players").document(req.player_id)
    if doc_ref.get().exists:
        raise HTTPException(409, f"Le joueur '{req.player_id}' existe déjà")

    player_state = build_new_player_state(req.player_id, req.current_zone_id)
    doc_ref.set(player_state)
    return player_state


@app.get("/players/{player_id}")
def get_player_state(player_id: str):
    doc = db.collection("players").document(player_id).get()
    if not doc.exists:
        raise HTTPException(404, f"Joueur '{player_id}' introuvable")
    return doc.to_dict()


BASE_STATS = {"hp": 80, "atk": 8, "def": 5, "agi": 5, "int": 6, "sag": 5}
POINT_VALUE = {"hp": 25, "atk": 2, "def": 2, "agi": 1, "int": 2, "sag": 1}
K_SELL = 0.35  # arthesia_balance_reference.md — rachat au serveur = 35% du base_value


def _accumulate_stat_effects(effects: list[dict], stats: dict, percent_bonuses: dict) -> None:
    for effect in effects:
        if effect["type"] == "stat_bonus":
            stats[effect["stat"]] = stats.get(effect["stat"], 0) + effect["amount"]
        elif effect["type"] == "stat_bonus_percent":
            percent_bonuses[effect["stat"]] = percent_bonuses.get(effect["stat"], 0) + effect["amount_percent"]


def compute_final_stats(player: dict) -> dict:
    """Base + points de niveau dépensés + bonus d'équipement + passifs 'always' + titre équipé
    (arthesia_balance_reference.md). Les bonus % s'appliquent à la fin, sur le total des bonus
    fixes déjà accumulés (base + points + équipement + passifs/titre flat).

    ⚠️ Les passifs à `trigger: "conditional"` (ex. HP en dessous d'un seuil) ne sont PAS inclus
    ici — ce sont des déclencheurs temps réel de combat (catégorie D, hors périmètre), pas des
    bonus permanents. Seuls les passifs `trigger: "always"` comptent dans ce calcul de référence."""
    stats = dict(BASE_STATS)

    for stat, points in player["stat_points"]["spent"].items():
        stats[stat] += points * POINT_VALUE[stat]

    for item_id in player["equipment"].values():
        if not item_id:
            continue
        item = load_template("item", item_id)
        for stat, amount in item.get("equipment", {}).get("stat_bonuses", {}).items():
            stats[stat] = stats.get(stat, 0) + amount

    percent_bonuses: dict = {}

    for skill_id in player["skills"]["equipped_passive"]:
        skill = load_template("skill", skill_id)
        if skill.get("trigger") == "always":
            _accumulate_stat_effects(skill.get("effects", []), stats, percent_bonuses)

    if player["titles"]["equipped"]:
        title = load_template("title", player["titles"]["equipped"])
        _accumulate_stat_effects(title.get("effects", []), stats, percent_bonuses)

    for stat, percent in percent_bonuses.items():
        stats[stat] = round(stats.get(stat, 0) * (1 + percent / 100), 1)

    return stats


@app.get("/players/{player_id}/stats")
def get_player_stats(player_id: str):
    doc = db.collection("players").document(player_id).get()
    if not doc.exists:
        raise HTTPException(404, f"Joueur '{player_id}' introuvable")
    player = doc.to_dict()
    return {
        "stats": compute_final_stats(player),
        "note": "Passifs conditionnels (temps réel combat) non inclus — voir catégorie D, hors périmètre.",
    }


def apply_monster_kill_rewards(player_id: str, zone_id: str, entry_id: str) -> dict:
    """Logique complète d'un kill : état partagé (respawn/boss permanent), loot, XP, progression
    de quête, déblocages. Réutilisée par la route REST /actions/monster_kill ET par la victoire
    en combat temps réel (websocket) — un seul endroit qui fait vraiment foi.

    ⚠️ Limite connue : si deux joueurs combattent en parallèle le même monstre 'shared' et gagnent
    presque en même temps, le second appel ici peut échouer (409, déjà 'depleted' par le premier) —
    la réconciliation d'un combat partagé entre plusieurs sessions concurrentes n'est pas résolue,
    catégorie D avancée, hors périmètre pour l'instant."""
    zone_static = load_static_zone_content(zone_id)
    entry = find_entry(zone_static, entry_id)
    if entry["template_type"] != "monster":
        raise HTTPException(400, "Cette entry n'est pas un monstre")
    monster = load_template("monster", entry["template_id"])

    if entry["state_scope"] == "shared":
        zone_state = get_zone_dynamic_state(zone_id)
        entry_state = zone_state["entries"].get(entry_id, {"status": "available"})

        if entry_state.get("status") == "depleted":
            available_again_at = entry_state.get("available_again_at")
            if available_again_at and datetime.now(timezone.utc) >= available_again_at:
                entry_state = {"status": "available"}

        if entry_state.get("status") != "available":
            raise HTTPException(409, "Monstre indisponible (déjà tué/vaincu, respawn en cours ou permanent)")

        if monster.get("role") == "boss":
            entry_state = {"status": "defeated"}
        else:
            cooldown = entry["spawn"].get("cooldown_seconds", 60)
            entry_state = {
                "status": "depleted",
                "available_again_at": datetime.now(timezone.utc) + timedelta(seconds=cooldown),
            }
        zone_state["entries"][entry_id] = entry_state
        save_zone_dynamic_state(zone_id, zone_state)

    player = get_player(player_id)

    loot = roll_loot_table(monster.get("loot_table", []))
    if loot:
        item_id, qty = loot
        player["inventory"][item_id] = player["inventory"].get(item_id, 0) + qty

    xp_gained = compute_kill_xp(monster["level"], monster.get("role", "normal"))
    player["xp"] += xp_gained
    levels_gained = apply_player_level_ups(player)
    player["lifetime_counters"]["kills"][entry["template_id"]] = \
        player["lifetime_counters"]["kills"].get(entry["template_id"], 0) + 1

    for quest_id, progress in player["quests"].items():
        if progress.get("state") != "active":
            continue
        quest_def = load_template("quest", quest_id)
        for i, obj in enumerate(quest_def["objectives"]):
            if obj["type"] == "kill_count" and obj["monster_id"] == entry["template_id"]:
                progress["objectives_progress"][i]["current_count"] += 1

    save_player(player_id, player)
    newly_unlocked = check_and_apply_unlocks(player_id)

    return {
        "loot": loot,
        "xp_gagne": xp_gained,
        "levels_gained": levels_gained,
        "player_level": player["level"],
        "player_xp_total": player["xp"],
        "player_stat_points": player["stat_points"],
        "player_inventory": player["inventory"],
        "newly_unlocked": newly_unlocked,
    }


@app.post("/actions/monster_kill")
def report_monster_kill(req: MonsterKillRequest):
    return apply_monster_kill_rewards(req.player_id, req.zone_id, req.entry_id)


class ResourceGatherRequest(BaseModel):
    player_id: str
    zone_id: str
    entry_id: str


@app.post("/actions/resource_gather")
def report_resource_gather(req: ResourceGatherRequest):
    zone_static = load_static_zone_content(req.zone_id)
    entry = find_entry(zone_static, req.entry_id)
    if entry["template_type"] != "resource":
        raise HTTPException(400, "Cette entry n'est pas une ressource")
    resource = load_template("resource", entry["template_id"])

    player = get_player(req.player_id)

    # Niveau de métier requis, vérifié avant tout le reste
    prof_req = resource.get("profession_required")
    if prof_req:
        player_prof_check = player["professions"].get(prof_req["profession_id"], {"level": 0, "xp": 0})
        if player_prof_check["level"] < prof_req["min_level"]:
            raise HTTPException(
                403, f"Niveau {prof_req['min_level']} en {prof_req['profession_id']} requis"
            )

    # État partagé : gère le stock à charges multiples (ou dispo simple), respawn à la volée
    if entry["state_scope"] == "shared":
        zone_state = get_zone_dynamic_state(req.zone_id)
        entry_state = zone_state["entries"].get(req.entry_id, {"status": "available"})
        max_stock = entry["spawn"].get("stock")

        if entry_state.get("status") == "depleted":
            available_again_at = entry_state.get("available_again_at")
            if available_again_at and datetime.now(timezone.utc) >= available_again_at:
                entry_state = {"status": "available"}
                if max_stock:
                    entry_state["current_stock"] = max_stock

        if entry_state.get("status") != "available":
            raise HTTPException(409, "Ressource indisponible (épuisée, respawn en cours)")

        cooldown = entry["spawn"].get("cooldown_seconds", 60)
        if max_stock:
            current_stock = entry_state.get("current_stock", max_stock) - 1
            if current_stock <= 0:
                entry_state = {
                    "status": "depleted",
                    "available_again_at": datetime.now(timezone.utc) + timedelta(seconds=cooldown),
                }
            else:
                entry_state = {"status": "available", "current_stock": current_stock}
        else:
            entry_state = {
                "status": "depleted",
                "available_again_at": datetime.now(timezone.utc) + timedelta(seconds=cooldown),
            }
        zone_state["entries"][req.entry_id] = entry_state
        save_zone_dynamic_state(req.zone_id, zone_state)

    # Même logique pondérée que le loot des monstres — champs identiques (item_id/quantity_min/
    # quantity_max/weight), donc roll_loot_table se réutilise tel quel sur les 'yields'.
    yields = roll_loot_table(resource.get("yields", []))
    if yields:
        item_id, qty = yields
        player["inventory"][item_id] = player["inventory"].get(item_id, 0) + qty

    # Pas d'XP de personnage à la récolte (décidé précédemment) — seulement l'XP de métier
    profession_xp_gained = 0
    if prof_req:
        profession_id = prof_req["profession_id"]
        player_prof = player["professions"].setdefault(profession_id, {"level": 0, "xp": 0})
        profession_xp_gained = prof_req.get("xp_reward", 0)
        player_prof["xp"] += profession_xp_gained
        apply_profession_level_ups(player_prof)

    player["lifetime_counters"]["gathers"][entry["template_id"]] = \
        player["lifetime_counters"]["gathers"].get(entry["template_id"], 0) + 1

    # Progression des quêtes actives avec un objectif gather_count sur cette ressource
    for quest_id, progress in player["quests"].items():
        if progress.get("state") != "active":
            continue
        quest_def = load_template("quest", quest_id)
        for i, obj in enumerate(quest_def["objectives"]):
            if obj["type"] == "gather_count" and obj["resource_id"] == entry["template_id"]:
                progress["objectives_progress"][i]["current_count"] += 1

    save_player(req.player_id, player)
    newly_unlocked = check_and_apply_unlocks(req.player_id)

    return {
        "yields": yields,
        "profession_xp_gained": profession_xp_gained,
        "player_professions": player["professions"],
        "player_inventory": player["inventory"],
        "newly_unlocked": newly_unlocked,
    }


class StartQuestRequest(BaseModel):
    player_id: str
    quest_id: str


@app.post("/actions/start_quest")
def start_quest(req: StartQuestRequest):
    quest_def = load_template("quest", req.quest_id)
    player = get_player(req.player_id)

    existing = player["quests"].get(req.quest_id)
    if existing:
        if existing.get("state") == "active":
            raise HTTPException(409, "Quête déjà active")
        if existing.get("state") == "completed" and not quest_def.get("repeatable"):
            raise HTTPException(409, "Quête déjà terminée (non répétable)")
        # completed + repeatable : on autorise à reprendre, réinitialisé ci-dessous

    player["quests"][req.quest_id] = {
        "state": "active",
        "objectives_progress": [
            {"objective_index": i, "current_count": 0}
            for i in range(len(quest_def["objectives"]))
        ],
    }
    save_player(req.player_id, player)
    return player["quests"][req.quest_id]


class TurnInQuestRequest(BaseModel):
    player_id: str
    quest_id: str


@app.post("/actions/turn_in_quest")
def turn_in_quest(req: TurnInQuestRequest):
    quest_def = load_template("quest", req.quest_id)
    player = get_player(req.player_id)

    progress = player["quests"].get(req.quest_id)
    if not progress or progress.get("state") != "active":
        raise HTTPException(409, "Quête non active pour ce joueur")

    for i, obj in enumerate(quest_def["objectives"]):
        p = progress["objectives_progress"][i]
        if obj["type"] in ("kill_count", "gather_count") and p["current_count"] < obj["count"]:
            raise HTTPException(400, f"Objectif {i} non rempli")
        if obj["type"] == "deliver_item" and player["inventory"].get(obj["item_id"], 0) < obj["count"]:
            raise HTTPException(400, f"Objets manquants pour l'objectif {i} (deliver_item)")

    for obj in quest_def["objectives"]:
        if obj["type"] == "deliver_item":
            player["inventory"][obj["item_id"]] -= obj["count"]
            player["lifetime_counters"]["deliveries"][obj["item_id"]] = \
                player["lifetime_counters"]["deliveries"].get(obj["item_id"], 0) + obj["count"]

    rewards = quest_def.get("rewards", {})
    player["xp"] += rewards.get("xp", 0)
    levels_gained = apply_player_level_ups(player)
    player["currency"]["or"] = player["currency"].get("or", 0) + rewards.get("gold", 0)
    for item in rewards.get("items", []):
        player["inventory"][item["item_id"]] = player["inventory"].get(item["item_id"], 0) + item["quantity"]

    progress["state"] = "completed"
    save_player(req.player_id, player)
    newly_unlocked = check_and_apply_unlocks(req.player_id)

    return {
        "quest_completed": req.quest_id,
        "rewards_given": rewards,
        "levels_gained": levels_gained,
        "player_level": player["level"],
        "player_xp_total": player["xp"],
        "player_stat_points": player["stat_points"],
        "player_currency": player["currency"],
        "newly_unlocked": newly_unlocked,
    }


class CraftInput(BaseModel):
    item_id: str
    quantity: int


class CraftRequest(BaseModel):
    player_id: str
    workshop_id: str
    inputs: list[CraftInput]
    parameters: dict[str, str]


@app.post("/actions/craft")
def craft(req: CraftRequest):
    workshop = load_template("workshop", req.workshop_id)

    # Paramètres : chaque clé déclarée par l'atelier doit être fournie avec une valeur valide
    declared = {p["id"]: p.get("options", []) for p in workshop.get("parameters", [])}
    for key in declared:
        if key not in req.parameters:
            raise HTTPException(400, f"Paramètre manquant : '{key}'")
        if req.parameters[key] not in declared[key]:
            raise HTTPException(400, f"Valeur invalide pour '{key}' : options {declared[key]}")

    player = get_player(req.player_id)

    # Accès au métier : première tentative dans cet atelier = on "pratique" le métier (niveau 0)
    profession_id = workshop["profession_required"]["profession_id"]
    player_prof = player["professions"].setdefault(profession_id, {"level": 0, "xp": 0})

    # Charger le détail complet de chaque item en entrée, vérifier inventaire + plafond de niveau
    inputs_full = []
    for inp in req.inputs:
        if player["inventory"].get(inp.item_id, 0) < inp.quantity:
            raise HTTPException(400, f"Pas assez de '{inp.item_id}' en inventaire")
        item = load_template("item", inp.item_id)
        if item.get("level", 0) > player_prof["level"]:
            raise HTTPException(
                403, f"'{inp.item_id}' (niveau {item['level']}) dépasse ton niveau "
                     f"en {profession_id} ({player_prof['level']})"
            )
        inputs_full.append({**item, "quantity": inp.quantity})

    inputs_for_key = [{"item_id": i.item_id, "quantity": i.quantity} for i in req.inputs]
    recipe_key = build_recipe_key(req.workshop_id, inputs_for_key, req.parameters)

    from_cache = True
    output_item_id = get_cached_craft(recipe_key)
    if output_item_id is None:
        from_cache = False
        new_item = generate_craft_item_via_ai(workshop, req.parameters, inputs_full)
        save_new_craft_item(new_item)
        output_item_id = new_item["id"]
        save_craft_cache(recipe_key, output_item_id)

    # Consommer les entrées, donner le résultat
    for inp in req.inputs:
        player["inventory"][inp.item_id] -= inp.quantity
        if player["inventory"][inp.item_id] <= 0:
            del player["inventory"][inp.item_id]
    player["inventory"][output_item_id] = player["inventory"].get(output_item_id, 0) + 1

    # ⚠️ XP de métier placeholder — aucune formule définie pour le craft dans le modèle actuel
    output_item = load_template("item", output_item_id)
    craft_xp_gained = output_item.get("level", 1) * 10
    player_prof["xp"] += craft_xp_gained
    apply_profession_level_ups(player_prof)

    save_player(req.player_id, player)
    newly_unlocked = check_and_apply_unlocks(req.player_id)

    return {
        "output_item_id": output_item_id,
        "from_cache": from_cache,
        "craft_xp_gained": craft_xp_gained,
        "player_professions": player["professions"],
        "player_inventory": player["inventory"],
        "newly_unlocked": newly_unlocked,
    }


VALID_STATS = {"hp", "atk", "def", "agi", "int", "sag"}


class AllocateStatPointsRequest(BaseModel):
    player_id: str
    allocations: dict[str, int]


@app.post("/actions/allocate_stat_points")
def allocate_stat_points(req: AllocateStatPointsRequest):
    invalid_stats = set(req.allocations) - VALID_STATS
    if invalid_stats:
        raise HTTPException(400, f"Stat(s) inconnue(s) : {invalid_stats}")
    if any(v < 0 for v in req.allocations.values()):
        raise HTTPException(400, "Les valeurs doivent être positives")

    player = get_player(req.player_id)
    total_requested = sum(req.allocations.values())
    if total_requested > player["stat_points"]["available"]:
        raise HTTPException(
            400, f"Pas assez de points disponibles ({player['stat_points']['available']} restants)"
        )

    for stat, amount in req.allocations.items():
        player["stat_points"]["spent"][stat] += amount
    player["stat_points"]["available"] -= total_requested

    save_player(req.player_id, player)
    return {"stat_points": player["stat_points"]}


VALID_SLOTS = {"weapon", "head", "chest", "gloves", "boots", "ring", "amulet", "tool"}


class EquipItemRequest(BaseModel):
    player_id: str
    item_id: str


class UnequipItemRequest(BaseModel):
    player_id: str
    slot: str


VALID_SLOTS = {"weapon", "head", "chest", "gloves", "boots", "ring", "amulet", "tool"}


class EquipItemRequest(BaseModel):
    player_id: str
    item_id: str


@app.post("/actions/equip_item")
def equip_item(req: EquipItemRequest):
    player = get_player(req.player_id)

    if player["inventory"].get(req.item_id, 0) < 1:
        raise HTTPException(400, f"'{req.item_id}' absent de l'inventaire")

    item = load_template("item", req.item_id)
    if item.get("category") != "equipment":
        raise HTTPException(400, f"'{req.item_id}' n'est pas un équipement")

    equip_info = item.get("equipment", {})
    slot = equip_info.get("slot")
    if slot not in VALID_SLOTS:
        raise HTTPException(500, f"Slot invalide sur le template '{req.item_id}' : {slot}")

    level_required = equip_info.get("level_required", 1)
    if player["level"] < level_required:
        raise HTTPException(403, f"Niveau {level_required} requis pour équiper '{req.item_id}'")

    previous_item_id = player["equipment"].get(slot)

    # Retire le nouvel item de l'inventaire, remet l'ancien (s'il y en avait un) à la place
    player["inventory"][req.item_id] -= 1
    if player["inventory"][req.item_id] <= 0:
        del player["inventory"][req.item_id]
    if previous_item_id:
        player["inventory"][previous_item_id] = player["inventory"].get(previous_item_id, 0) + 1

    player["equipment"][slot] = req.item_id
    save_player(req.player_id, player)

    return {"slot": slot, "equipped": req.item_id, "previous": previous_item_id, "equipment": player["equipment"]}


class UnequipItemRequest(BaseModel):
    player_id: str
    slot: str


@app.post("/actions/unequip_item")
def unequip_item(req: UnequipItemRequest):
    if req.slot not in VALID_SLOTS:
        raise HTTPException(400, f"Slot invalide : {req.slot}")

    player = get_player(req.player_id)
    current_item_id = player["equipment"].get(req.slot)
    if not current_item_id:
        raise HTTPException(400, f"Rien d'équipé dans le slot '{req.slot}'")

    player["inventory"][current_item_id] = player["inventory"].get(current_item_id, 0) + 1
    player["equipment"][req.slot] = None
    save_player(req.player_id, player)

    return {"slot": req.slot, "unequipped": current_item_id, "equipment": player["equipment"]}


# Nombre de slots de compétences équipables — bornes hautes du modèle de données (4-8 actives),
# en variable globale pour pouvoir resserrer facilement une fois en test réel.
MAX_ACTIVE_SKILL_SLOTS = 8
MAX_PASSIVE_SKILL_SLOTS = 4


class EquipSkillRequest(BaseModel):
    player_id: str
    skill_id: str


@app.post("/actions/equip_skill")
def equip_skill(req: EquipSkillRequest):
    skill = load_template("skill", req.skill_id)
    player = get_player(req.player_id)

    if req.skill_id not in player["skills"]["unlocked"]:
        raise HTTPException(403, f"Compétence '{req.skill_id}' non débloquée")

    kind = skill["kind"]  # "active" ou "passive"
    slot_list_key = "equipped_active" if kind == "active" else "equipped_passive"
    max_slots = MAX_ACTIVE_SKILL_SLOTS if kind == "active" else MAX_PASSIVE_SKILL_SLOTS

    if req.skill_id in player["skills"][slot_list_key]:
        raise HTTPException(409, f"'{req.skill_id}' déjà équipée")
    if len(player["skills"][slot_list_key]) >= max_slots:
        raise HTTPException(400, f"Plus de slot {kind} disponible ({max_slots} max)")

    player["skills"][slot_list_key].append(req.skill_id)
    save_player(req.player_id, player)
    return {"equipped": req.skill_id, "kind": kind, "skills": player["skills"]}


class UnequipSkillRequest(BaseModel):
    player_id: str
    skill_id: str


@app.post("/actions/unequip_skill")
def unequip_skill(req: UnequipSkillRequest):
    player = get_player(req.player_id)

    for slot_list_key in ("equipped_active", "equipped_passive"):
        if req.skill_id in player["skills"][slot_list_key]:
            player["skills"][slot_list_key].remove(req.skill_id)
            save_player(req.player_id, player)
            return {"unequipped": req.skill_id, "skills": player["skills"]}

    raise HTTPException(400, f"'{req.skill_id}' n'est équipée dans aucun slot")


class EquipTitleRequest(BaseModel):
    player_id: str
    title_id: str


@app.post("/actions/equip_title")
def equip_title(req: EquipTitleRequest):
    load_template("title", req.title_id)  # vérifie juste que le titre existe vraiment
    player = get_player(req.player_id)

    if req.title_id not in player["titles"]["unlocked"]:
        raise HTTPException(403, f"Titre '{req.title_id}' non débloqué")

    player["titles"]["equipped"] = req.title_id
    save_player(req.player_id, player)
    return {"equipped": req.title_id, "titles": player["titles"]}


class UnequipTitleRequest(BaseModel):
    player_id: str


@app.post("/actions/unequip_title")
def unequip_title(req: UnequipTitleRequest):
    player = get_player(req.player_id)
    if not player["titles"]["equipped"]:
        raise HTTPException(400, "Aucun titre équipé")

    previous = player["titles"]["equipped"]
    player["titles"]["equipped"] = None
    save_player(req.player_id, player)
    return {"unequipped": previous, "titles": player["titles"]}


class EquipTitleRequest(BaseModel):
    player_id: str
    title_id: str


@app.post("/actions/equip_title")
def equip_title(req: EquipTitleRequest):
    load_template("title", req.title_id)  # vérifie juste que le template existe
    player = get_player(req.player_id)

    if req.title_id not in player["titles"]["unlocked"]:
        raise HTTPException(403, f"Titre '{req.title_id}' non débloqué")

    previous_title_id = player["titles"]["equipped"]
    player["titles"]["equipped"] = req.title_id
    save_player(req.player_id, player)

    return {"equipped": req.title_id, "previous": previous_title_id, "titles": player["titles"]}


class UnequipTitleRequest(BaseModel):
    player_id: str


@app.post("/actions/unequip_title")
def unequip_title(req: UnequipTitleRequest):
    player = get_player(req.player_id)

    if not player["titles"]["equipped"]:
        raise HTTPException(400, "Aucun titre équipé")

    previous_title_id = player["titles"]["equipped"]
    player["titles"]["equipped"] = None
    save_player(req.player_id, player)

    return {"unequipped": previous_title_id, "titles": player["titles"]}


class BuyFromShopRequest(BaseModel):
    player_id: str
    zone_id: str
    entry_id: str
    item_id: str
    quantity: int = 1


@app.post("/actions/buy_from_shop")
def buy_from_shop(req: BuyFromShopRequest):
    zone_static = load_static_zone_content(req.zone_id)
    entry = find_entry(zone_static, req.entry_id)
    if entry["template_type"] != "shop":
        raise HTTPException(400, "Cette entry n'est pas un shop")
    shop = load_template("shop", entry["template_id"])

    offer = next((o for o in shop["sell_offers"] if o["item_id"] == req.item_id), None)
    if not offer:
        raise HTTPException(404, f"'{req.item_id}' n'est pas en vente ici")

    total_price = offer["price"] * req.quantity
    player = get_player(req.player_id)
    currency = shop.get("currency", "or")
    if player["currency"].get(currency, 0) < total_price:
        raise HTTPException(400, f"Pas assez de {currency} ({total_price} requis)")

    # Stock partagé entre joueurs (comme les monstres/ressources), uniquement si un stock
    # limité est défini pour cette offre précise — sinon achat libre (stock: null = infini)
    if entry["state_scope"] == "shared" and offer.get("stock") is not None:
        zone_state = get_zone_dynamic_state(req.zone_id)
        entry_state = zone_state["entries"].get(req.entry_id, {"offers": {}})
        offer_state = entry_state.get("offers", {}).get(
            req.item_id, {"status": "available", "current_stock": offer["stock"]}
        )

        if offer_state.get("status") == "depleted":
            available_again_at = offer_state.get("available_again_at")
            if available_again_at and datetime.now(timezone.utc) >= available_again_at:
                offer_state = {"status": "available", "current_stock": offer["stock"]}

        if offer_state.get("status") != "available" or offer_state.get("current_stock", 0) < req.quantity:
            raise HTTPException(409, f"Stock insuffisant pour '{req.item_id}' (réapprovisionnement en cours)")

        offer_state["current_stock"] -= req.quantity
        if offer_state["current_stock"] <= 0:
            restock = offer.get("restock_seconds")
            offer_state = (
                {"status": "depleted", "available_again_at": datetime.now(timezone.utc) + timedelta(seconds=restock)}
                if restock else
                {"status": "depleted", "available_again_at": None}  # jamais de restock (objet unique)
            )

        entry_state.setdefault("offers", {})[req.item_id] = offer_state
        zone_state["entries"][req.entry_id] = entry_state
        save_zone_dynamic_state(req.zone_id, zone_state)

    player["currency"][currency] = player["currency"].get(currency, 0) - total_price
    player["inventory"][req.item_id] = player["inventory"].get(req.item_id, 0) + req.quantity
    save_player(req.player_id, player)

    return {
        "bought": req.item_id, "quantity": req.quantity, "total_price": total_price,
        "player_currency": player["currency"], "player_inventory": player["inventory"],
    }


K_SELL = 0.35  # arthesia_balance_reference.md — toujours < prix de vente, jamais d'argent infini


class SellToServerRequest(BaseModel):
    player_id: str
    item_id: str
    quantity: int = 1


@app.post("/actions/sell_to_server")
def sell_to_server(req: SellToServerRequest):
    """Règle globale (pas propre à un shop précis) : n'importe quel item se revend au serveur
    à K_SELL × base_value. Ne demande donc ni zone_id ni entry_id — juste être en mesure de vendre."""
    player = get_player(req.player_id)
    if player["inventory"].get(req.item_id, 0) < req.quantity:
        raise HTTPException(400, f"Pas assez de '{req.item_id}' en inventaire")

    item = load_template("item", req.item_id)
    unit_price = round(item.get("base_value", 0) * K_SELL)
    total_gained = unit_price * req.quantity

    player["inventory"][req.item_id] -= req.quantity
    if player["inventory"][req.item_id] <= 0:
        del player["inventory"][req.item_id]
    player["currency"]["or"] = player["currency"].get("or", 0) + total_gained

    save_player(req.player_id, player)
    return {
        "sold": req.item_id, "quantity": req.quantity, "total_gained": total_gained,
        "player_currency": player["currency"], "player_inventory": player["inventory"],
    }


class ShopBuyRequest(BaseModel):
    player_id: str
    zone_id: str
    entry_id: str
    item_id: str
    quantity: int = 1


@app.post("/actions/shop_buy")
def shop_buy(req: ShopBuyRequest):
    zone_static = load_static_zone_content(req.zone_id)
    entry = find_entry(zone_static, req.entry_id)
    if entry["template_type"] != "shop":
        raise HTTPException(400, "Cette entry n'est pas un shop")
    shop = load_template("shop", entry["template_id"])

    if req.quantity < 1:
        raise HTTPException(400, "Quantité invalide")
    offer = next((o for o in shop["sell_offers"] if o["item_id"] == req.item_id), None)
    if offer is None:
        raise HTTPException(404, f"'{req.item_id}' n'est pas vendu ici")

    # Stock partagé et limité : même logique de respawn à la volée que monstres/ressources,
    # mais suivie par offre (un shop vend plusieurs items, chacun avec son propre stock/restock).
    if offer.get("stock") is not None and entry["state_scope"] == "shared":
        zone_state = get_zone_dynamic_state(req.zone_id)
        entry_state = zone_state["entries"].setdefault(req.entry_id, {"offers": {}})
        offer_state = entry_state["offers"].get(
            req.item_id, {"status": "available", "current_stock": offer["stock"]}
        )

        if offer_state.get("status") == "depleted":
            available_again_at = offer_state.get("available_again_at")
            if available_again_at and datetime.now(timezone.utc) >= available_again_at:
                offer_state = {"status": "available", "current_stock": offer["stock"]}

        if offer_state.get("status") != "available" or offer_state.get("current_stock", 0) < req.quantity:
            raise HTTPException(409, f"Stock insuffisant pour '{req.item_id}'")

        offer_state["current_stock"] -= req.quantity
        if offer_state["current_stock"] <= 0:
            restock = offer.get("restock_seconds")
            offer_state = {
                "status": "depleted",
                # restock_seconds: null = ne se réapprovisionne jamais (objet unique/événementiel)
                "available_again_at": (
                    datetime.now(timezone.utc) + timedelta(seconds=restock) if restock else None
                ),
            }
        entry_state["offers"][req.item_id] = offer_state
        zone_state["entries"][req.entry_id] = entry_state
        save_zone_dynamic_state(req.zone_id, zone_state)

    total_cost = offer["price"] * req.quantity
    player = get_player(req.player_id)
    currency = shop.get("currency", "or")
    if player["currency"].get(currency, 0) < total_cost:
        raise HTTPException(400, f"Pas assez de {currency} ({total_cost} requis)")

    player["currency"][currency] -= total_cost
    player["inventory"][req.item_id] = player["inventory"].get(req.item_id, 0) + req.quantity
    save_player(req.player_id, player)

    return {
        "bought": req.item_id,
        "quantity": req.quantity,
        "total_cost": total_cost,
        "player_currency": player["currency"],
        "player_inventory": player["inventory"],
    }


class ShopSellRequest(BaseModel):
    player_id: str
    zone_id: str
    entry_id: str
    item_id: str
    quantity: int = 1


@app.post("/actions/shop_sell")
def shop_sell(req: ShopSellRequest):
    """Règle globale déjà posée dans le modèle : TOUS les shops rachètent TOUS les items, à
    base_value × K_SELL — pas une propriété du template Shop, une règle du jeu."""
    zone_static = load_static_zone_content(req.zone_id)
    entry = find_entry(zone_static, req.entry_id)
    if entry["template_type"] != "shop":
        raise HTTPException(400, "Cette entry n'est pas un shop")
    shop = load_template("shop", entry["template_id"])

    if req.quantity < 1:
        raise HTTPException(400, "Quantité invalide")
    player = get_player(req.player_id)
    if player["inventory"].get(req.item_id, 0) < req.quantity:
        raise HTTPException(400, f"Pas assez de '{req.item_id}' en inventaire")

    item = load_template("item", req.item_id)
    unit_price = round(item.get("base_value", 0) * K_SELL, 2)
    total_earned = round(unit_price * req.quantity, 2)

    player["inventory"][req.item_id] -= req.quantity
    if player["inventory"][req.item_id] <= 0:
        del player["inventory"][req.item_id]

    currency = shop.get("currency", "or")
    player["currency"][currency] = player["currency"].get(currency, 0) + total_earned
    save_player(req.player_id, player)

    return {
        "sold": req.item_id,
        "quantity": req.quantity,
        "total_earned": total_earned,
        "player_currency": player["currency"],
        "player_inventory": player["inventory"],
    }


class ListItemForSaleRequest(BaseModel):
    player_id: str
    zone_id: str
    entry_id: str
    item_id: str
    quantity: int = 1
    price: int


@app.post("/actions/market_list_item")
def market_list_item(req: ListItemForSaleRequest):
    zone_static = load_static_zone_content(req.zone_id)
    entry = find_entry(zone_static, req.entry_id)
    if entry["template_type"] != "player_market":
        raise HTTPException(400, "Cette entry n'est pas un Autel des Ventes")
    market = load_template("player_market", entry["template_id"])

    if req.quantity < 1 or req.price < 0:
        raise HTTPException(400, "Quantité ou prix invalide")

    player = get_player(req.player_id)
    if player["inventory"].get(req.item_id, 0) < req.quantity:
        raise HTTPException(400, f"Pas assez de '{req.item_id}' en inventaire")

    # Mis en dépôt immédiatement — évite de pouvoir dépenser ce qui est déjà en vente ailleurs
    player["inventory"][req.item_id] -= req.quantity
    if player["inventory"][req.item_id] <= 0:
        del player["inventory"][req.item_id]
    save_player(req.player_id, player)

    listing_id = f"listing_{uuid.uuid4().hex[:12]}"
    listing = {
        "listing_id": listing_id,
        "market_id": entry["template_id"],
        "seller_player_id": req.player_id,
        "item_id": req.item_id,
        "quantity": req.quantity,
        "price": req.price,
        "currency": market.get("currency", "or"),
        "listed_at": datetime.now(timezone.utc),
    }
    db.collection("market_listings").document(listing_id).set(listing)
    return listing


@app.get("/market/{market_id}/listings")
def market_browse(market_id: str):
    docs = db.collection("market_listings").where("market_id", "==", market_id).stream()
    return [d.to_dict() for d in docs]


class BuyListingRequest(BaseModel):
    player_id: str
    listing_id: str


@app.post("/actions/market_buy")
def market_buy(req: BuyListingRequest):
    # ⚠️ Pas de transaction Firestore atomique ici — petit risque de course si deux joueurs
    # achètent la même annonce en même temps (acceptable à cette échelle, à durcir plus tard).
    listing_ref = db.collection("market_listings").document(req.listing_id)
    listing_doc = listing_ref.get()
    if not listing_doc.exists:
        raise HTTPException(404, "Annonce introuvable (déjà vendue ou annulée)")
    listing = listing_doc.to_dict()

    if listing["seller_player_id"] == req.player_id:
        raise HTTPException(400, "Impossible d'acheter sa propre annonce")

    buyer = get_player(req.player_id)
    currency = listing["currency"]
    if buyer["currency"].get(currency, 0) < listing["price"]:
        raise HTTPException(400, f"Pas assez de {currency}")

    buyer["currency"][currency] -= listing["price"]
    buyer["inventory"][listing["item_id"]] = buyer["inventory"].get(listing["item_id"], 0) + listing["quantity"]
    save_player(req.player_id, buyer)

    # Le vendeur est crédité directement, sans avoir besoin d'être connecté
    seller = get_player(listing["seller_player_id"])
    seller["currency"][currency] = seller["currency"].get(currency, 0) + listing["price"]
    save_player(listing["seller_player_id"], seller)

    listing_ref.delete()

    return {
        "bought": listing["item_id"],
        "quantity": listing["quantity"],
        "price_paid": listing["price"],
        "player_currency": buyer["currency"],
        "player_inventory": buyer["inventory"],
    }


class CancelListingRequest(BaseModel):
    player_id: str
    listing_id: str


@app.post("/actions/market_cancel_listing")
def market_cancel_listing(req: CancelListingRequest):
    listing_ref = db.collection("market_listings").document(req.listing_id)
    listing_doc = listing_ref.get()
    if not listing_doc.exists:
        raise HTTPException(404, "Annonce introuvable")
    listing = listing_doc.to_dict()

    if listing["seller_player_id"] != req.player_id:
        raise HTTPException(403, "Seul le vendeur peut annuler cette annonce")

    seller = get_player(req.player_id)
    seller["inventory"][listing["item_id"]] = seller["inventory"].get(listing["item_id"], 0) + listing["quantity"]
    save_player(req.player_id, seller)

    listing_ref.delete()
    return {"cancelled": req.listing_id, "item_returned": listing["item_id"], "quantity": listing["quantity"]}



# =============================================================================
# COMBAT TEMPS RÉEL — Étape 2 : boucle de tick autonome du monstre.
# État en mémoire, JAMAIS écrit dans Firestore à chaque tick (un seul combat continu
# épuiserait le quota gratuit en une journée) — seul le résultat final (victoire/défaite)
# déclenchera une écriture, via report_monster_kill (pas encore branché à cette étape).
# ⚠️ Le joueur ne peut encore rien faire (pas d'attaque/compétence gérée) — juste regarder
# le monstre taper, pour valider la boucle avant d'ajouter les actions du joueur.
# =============================================================================

TICK_INTERVAL_SECONDS = 0.5
K_DEF = 20  # arthesia_balance_reference.md — mitigation = def / (def + K_def)
BASE_ATTACK_COOLDOWN = 2.0
K_AGI = 0.03
BASIC_ATTACK_POWER = 1.0  # ⚠️ aucune formule posée pour l'attaque classique dans le modèle —
# coefficient provisoire, même logique que les compétences (power × ATK)


def compute_basic_attack_cooldown(agi: float) -> float:
    return BASE_ATTACK_COOLDOWN / (1 + agi * K_AGI)


BASE_MANA = 50
K_INT = 4
BASE_MANA_REGEN = 1
K_SAG = 0.15


def compute_max_mana(int_stat: float) -> float:
    return BASE_MANA + int_stat * K_INT


def compute_mana_regen(sag_stat: float) -> float:
    return BASE_MANA_REGEN + sag_stat * K_SAG

active_combats: dict[str, dict] = {}


def compute_mitigation(defense: float) -> float:
    return defense / (defense + K_DEF)


async def combat_tick_loop(websocket: WebSocket, combat_id: str):
    """Tourne en tâche de fond, indépendamment des messages reçus du joueur. Décrémente les
    cooldowns du monstre (le fait attaquer, pondéré parmi les compétences prêtes) ET régénère
    le mana du joueur — les deux sont liés au même rythme de tick, regroupés ici plutôt que
    dans deux boucles séparées."""
    try:
        while combat_id in active_combats:
            await asyncio.sleep(TICK_INTERVAL_SECONDS)
            session = active_combats.get(combat_id)
            if session is None:
                break

            player = get_player(session["player_id"])
            player_stats = compute_final_stats(player)

            # Régénération de mana du joueur
            regen = compute_mana_regen(player_stats.get("sag", 0)) * TICK_INTERVAL_SECONDS
            session["player_current_mana"] = min(
                session["player_max_mana"], session["player_current_mana"] + regen
            )

            # IA du monstre
            monster = load_template("monster", session["monster_template_id"])
            moveset = monster.get("moveset", [])
            cooldowns = session["monster_cooldowns"]

            for skill in moveset:
                sid = skill["id"]
                cooldowns[sid] = max(0.0, cooldowns.get(sid, 0.0) - TICK_INTERVAL_SECONDS)

            ready_skills = [s for s in moveset if cooldowns.get(s["id"], 0.0) <= 0]
            if not ready_skills:
                continue

            chosen = random.choices(ready_skills, weights=[s["weight"] for s in ready_skills], k=1)[0]
            cooldowns[chosen["id"]] = chosen["cooldown_seconds"]

            final_damage = round(chosen["damage"] * (1 - compute_mitigation(player_stats.get("def", 0))), 1)
            session["player_current_hp"] = max(0.0, session["player_current_hp"] - final_damage)

            await websocket.send_json({
                "type": "monster_attack",
                "skill_id": chosen["id"],
                "raw_damage": chosen["damage"],
                "final_damage": final_damage,
                "player_hp": session["player_current_hp"],
                "player_max_hp": session["player_max_hp"],
                "player_mana": session["player_current_mana"],
            })

            if session["player_current_hp"] <= 0:
                await websocket.send_json({"type": "combat_lost"})
                del active_combats[combat_id]
                break
    except Exception as e:
        print(f"⚠️  Erreur dans la boucle de combat {combat_id} : {e}")


@app.websocket("/ws/combat")
async def combat_websocket(websocket: WebSocket):
    await websocket.accept()
    combat_id = None
    tick_task = None
    try:
        while True:
            message = await websocket.receive_json()

            if message.get("action") == "start_combat":
                player_id = message["player_id"]
                zone_id = message["zone_id"]
                entry_id = message["entry_id"]

                zone_static = load_static_zone_content(zone_id)
                entry = find_entry(zone_static, entry_id)
                if entry["template_type"] != "monster":
                    await websocket.send_json({"type": "error", "message": "Cette entry n'est pas un monstre"})
                    continue
                monster = load_template("monster", entry["template_id"])
                player = get_player(player_id)
                player_stats = compute_final_stats(player)
                player_hp = player_stats["hp"]
                player_max_mana = compute_max_mana(player_stats.get("int", 0))

                combat_id = str(uuid.uuid4())
                active_combats[combat_id] = {
                    "player_id": player_id,
                    "zone_id": zone_id,
                    "entry_id": entry_id,
                    "monster_template_id": entry["template_id"],
                    "monster_current_hp": monster["stats"]["hp"],
                    "monster_max_hp": monster["stats"]["hp"],
                    "monster_cooldowns": {},
                    "player_current_hp": player_hp,
                    "player_max_hp": player_hp,
                    "player_basic_attack_ready_at": time.monotonic(),
                    "player_current_mana": player_max_mana,
                    "player_max_mana": player_max_mana,
                    "player_skill_cooldowns": {},
                }

                await websocket.send_json({
                    "type": "combat_started",
                    "combat_id": combat_id,
                    "monster_name": monster["name"],
                    "monster_hp": monster["stats"]["hp"],
                    "player_hp": player_hp,
                    "player_mana": player_max_mana,
                })

                tick_task = asyncio.create_task(combat_tick_loop(websocket, combat_id))

            elif message.get("action") == "basic_attack":
                session = active_combats.get(combat_id)
                if session is None:
                    await websocket.send_json({"type": "error", "message": "Aucun combat en cours"})
                    continue

                now = time.monotonic()
                if now < session["player_basic_attack_ready_at"]:
                    await websocket.send_json({"type": "error", "message": "Attaque pas encore prête"})
                    continue

                player = get_player(session["player_id"])
                player_stats = compute_final_stats(player)
                monster = load_template("monster", session["monster_template_id"])

                raw_damage = BASIC_ATTACK_POWER * player_stats["atk"]
                mitigation = compute_mitigation(monster["stats"]["def"])
                final_damage = round(raw_damage * (1 - mitigation), 1)

                session["monster_current_hp"] = max(0.0, session["monster_current_hp"] - final_damage)
                cooldown = compute_basic_attack_cooldown(player_stats["agi"])
                session["player_basic_attack_ready_at"] = now + cooldown

                await websocket.send_json({
                    "type": "player_basic_attack",
                    "raw_damage": round(raw_damage, 1),
                    "final_damage": final_damage,
                    "monster_hp": session["monster_current_hp"],
                    "monster_max_hp": session["monster_max_hp"],
                    "next_ready_in": round(cooldown, 2),
                })

                if session["monster_current_hp"] <= 0:
                    del active_combats[combat_id]
                    try:
                        rewards = apply_monster_kill_rewards(
                            session["player_id"], session["zone_id"], session["entry_id"]
                        )
                        await websocket.send_json({"type": "combat_won", **rewards})
                    except HTTPException as e:
                        await websocket.send_json({
                            "type": "combat_won_no_reward",
                            "reason": e.detail,
                        })

            elif message.get("action") == "use_skill":
                skill_id = message["skill_id"]
                session = active_combats.get(combat_id)
                if session is None:
                    await websocket.send_json({"type": "error", "message": "Aucun combat en cours"})
                    continue

                player = get_player(session["player_id"])
                if skill_id not in player["skills"]["equipped_active"]:
                    await websocket.send_json({"type": "error", "message": "Compétence non équipée"})
                    continue

                skill = load_template("skill", skill_id)
                now = time.monotonic()
                cooldowns = session["player_skill_cooldowns"]
                if now < cooldowns.get(skill_id, 0):
                    await websocket.send_json({"type": "error", "message": "Compétence pas encore prête"})
                    continue
                if session["player_current_mana"] < skill["mana_cost"]:
                    await websocket.send_json({"type": "error", "message": "Pas assez de mana"})
                    continue

                session["player_current_mana"] -= skill["mana_cost"]
                cooldowns[skill_id] = now + skill["cooldown_seconds"]

                player_stats = compute_final_stats(player)
                monster = load_template("monster", session["monster_template_id"])
                results = []

                for effect in skill.get("effects", []):
                    etype = effect["type"]
                    if etype == "damage":
                        scaling_stat = "int" if skill.get("damage_type") == "magical" else "atk"
                        raw = effect["power"] * player_stats.get(scaling_stat, 0)
                        final = round(raw * (1 - compute_mitigation(monster["stats"].get("def", 0))), 1)
                        session["monster_current_hp"] = max(0.0, session["monster_current_hp"] - final)
                        results.append({"effect": "damage", "final_damage": final})
                    elif etype == "heal":
                        raw = round(effect["power"] * player_stats.get("int", 0), 1)
                        session["player_current_hp"] = min(session["player_max_hp"], session["player_current_hp"] + raw)
                        results.append({"effect": "heal", "amount": raw})
                    else:
                        # shield/buff/debuff : reconnus mais pas encore mécaniquement appliqués
                        # (durées qui expirent, absorption qui se consomme — étape à part)
                        results.append({"effect": etype, "note": "pas encore implémenté mécaniquement"})

                await websocket.send_json({
                    "type": "player_skill_used",
                    "skill_id": skill_id,
                    "results": results,
                    "monster_hp": session["monster_current_hp"],
                    "player_hp": session["player_current_hp"],
                    "player_mana": session["player_current_mana"],
                })

                if session["monster_current_hp"] <= 0:
                    del active_combats[combat_id]
                    try:
                        rewards = apply_monster_kill_rewards(
                            session["player_id"], session["zone_id"], session["entry_id"]
                        )
                        await websocket.send_json({"type": "combat_won", **rewards})
                    except HTTPException as e:
                        await websocket.send_json({
                            "type": "combat_won_no_reward",
                            "reason": e.detail,
                        })

            else:
                await websocket.send_json({"type": "error", "message": "Action pas encore gérée à cette étape"})

    except WebSocketDisconnect:
        if tick_task:
            tick_task.cancel()
        if combat_id and combat_id in active_combats:
            del active_combats[combat_id]


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