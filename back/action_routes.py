"""Routes HTTP des actions métier du joueur.

Chaque handler valide la requête, charge le contenu statique local nécessaire,
met à jour l'état partagé dans Firestore, puis renvoie l'état utile au client.
La logique transversale est conservée dans les modules suivants :

* ``content_store`` pour les zones et templates locaux ;
* ``player_store`` pour les joueurs et les états dynamiques ;
* ``game_rules`` pour les règles de progression et de statistiques ;
* ``progression`` pour les récompenses et déblocages ;
* ``craft_service`` pour le craft et son cache.
"""

from datetime import datetime, timedelta, timezone
import uuid

from fastapi import APIRouter, HTTPException
from firebase_admin import firestore

try:
    from .content_store import find_entry, find_quest_givers, load_ordered_floors, load_static_zone_content, load_template
    from .craft_service import (
        build_recipe_key, generate_craft_item_via_ai, get_cached_craft,
        save_craft_cache, save_new_craft_item,
    )
    from .database import db
    from .game_rules import (
        advance_quest_objectives, apply_player_level_ups, apply_profession_level_ups, deplete_entry_state,
        get_profession, is_entry_unlocked, is_zone_accessible, public_entry_state, quest_progress_view,
        has_defeated_entry, is_shared_entry, reachable_floor_ids, require_entry_unlocked,
        resolve_entry_state, roll_loot_table, xp_to_next_level,
    )
    from .models import *
    from .player_store import get_player, get_zone_dynamic_state, save_player, save_zone_dynamic_state
    from .progression import check_and_apply_unlocks
except ImportError:
    from content_store import find_entry, find_quest_givers, load_ordered_floors, load_static_zone_content, load_template
    from craft_service import (
        build_recipe_key, generate_craft_item_via_ai, get_cached_craft,
        save_craft_cache, save_new_craft_item,
    )
    from database import db
    from game_rules import (
        advance_quest_objectives, apply_player_level_ups, apply_profession_level_ups, deplete_entry_state,
        get_profession, is_entry_unlocked, is_zone_accessible, public_entry_state, quest_progress_view,
        has_defeated_entry, is_shared_entry, reachable_floor_ids, require_entry_unlocked,
        resolve_entry_state, roll_loot_table, xp_to_next_level,
    )
    from models import *
    from player_store import get_player, get_zone_dynamic_state, save_player, save_zone_dynamic_state
    from progression import check_and_apply_unlocks


router = APIRouter()

# Paramètres d'équilibrage et limites de gameplay utilisés par les routes.
K_SELL = 0.35
VALID_STATS = {"hp", "atk", "def", "agi", "int", "sag"}
VALID_SLOTS = {"weapon", "head", "chest", "gloves", "boots", "ring", "amulet", "tool"}
MAX_ACTIVE_SKILL_SLOTS = 8
MAX_PASSIVE_SKILL_SLOTS = 4


# -----------------------------------------------------------------------------
# Déplacement
# -----------------------------------------------------------------------------

@router.post("/actions/travel")
def travel(req: TravelRequest):
    """Déplace le joueur dans une zone accessible d'un étage atteignable.

    ``current_zone_id`` sert à la carte (position du joueur, étage affiché).
    """
    floors = load_ordered_floors()
    for floor in floors:
        zone = next((z for z in floor.get("zones", []) if z["id"] == req.zone_id), None)
        if zone is not None:
            return _travel_tx(db.transaction(), req, floors, floor["id"], zone)
    raise HTTPException(404, f"Zone introuvable : {req.zone_id}")


@firestore.transactional
def _travel_tx(transaction, req: TravelRequest, floors: list[dict], floor_id: str, zone: dict) -> dict:
    player = get_player(req.player_id, transaction)
    if floor_id not in reachable_floor_ids(floors, player):
        raise HTTPException(403, "Cet étage n'est pas encore accessible : franchis d'abord le passage de l'étage précédent")
    if not is_zone_accessible(zone, player):
        raise HTTPException(403, "Cette zone n'est pas encore accessible")
    player["current_zone_id"] = zone["id"]
    save_player(req.player_id, player, transaction)
    return {"current_zone_id": zone["id"], "floor_id": floor_id}


# -----------------------------------------------------------------------------
# Progression : récolte et quêtes
#
# Les victoires en combat ne passent pas par une route REST : seul le
# WebSocket de combat (combat_routes.py) peut accorder les récompenses d'un kill.
# -----------------------------------------------------------------------------

@router.post("/actions/resource_gather")
def report_resource_gather(req: ResourceGatherRequest):
    """Récolte une ressource, gère son stock partagé et accorde l'XP de métier."""
    # La zone et l'entry sont lues depuis les fichiers statiques locaux :
    # Firestore ne contient ici que l'état dynamique, comme le stock restant.
    zone_static = load_static_zone_content(req.zone_id)
    entry = find_entry(zone_static, req.entry_id)
    if entry["template_type"] != "resource":
        raise HTTPException(400, "Cette entry n'est pas une ressource")
    resource = load_template("resource", entry["template_id"])

    result = _resource_gather_tx(db.transaction(), req, entry, resource)
    # Les déblocages dépendent du nouvel inventaire ou de la nouvelle
    # progression : ils sont recalculés après le commit.
    result["newly_unlocked"] = check_and_apply_unlocks(req.player_id)
    return result


@firestore.transactional
def _resource_gather_tx(transaction, req: ResourceGatherRequest, entry: dict, resource: dict) -> dict:
    # Le stock partagé et le joueur sont lus puis écrits dans la même
    # transaction : deux récoltes simultanées ne consomment pas la même charge.
    shared = entry["state_scope"] == "shared"
    zone_state = get_zone_dynamic_state(req.zone_id, transaction) if shared else None
    player = get_player(req.player_id, transaction)

    require_entry_unlocked(entry, player)
    prof_req = resource.get("profession_required")
    if prof_req:
        if get_profession(player, prof_req["profession_id"])["level"] < prof_req["min_level"]:
            raise HTTPException(403, f"Niveau {prof_req['min_level']} en {prof_req['profession_id']} requis")

    entry_state = None
    if shared:
        entry_state = resolve_entry_state(entry, zone_state["entries"].get(req.entry_id))
        if entry_state.get("status") != "available":
            raise HTTPException(409, "Ressource indisponible (épuisée, respawn en cours)")
        if entry["spawn"].get("stock"):
            # Avec un stock, l'entry reste disponible jusqu'à ce que la
            # dernière charge soit consommée.
            current_stock = entry_state["current_stock"] - 1
            entry_state = deplete_entry_state(entry) if current_stock <= 0 else {"status": "available", "current_stock": current_stock}
        else:
            entry_state = deplete_entry_state(entry)
        zone_state["entries"][req.entry_id] = entry_state
        save_zone_dynamic_state(req.zone_id, zone_state, transaction)

    # Les yields utilisent la même table pondérée que les loots de monstres.
    yields = roll_loot_table(resource.get("yields", []))
    if yields:
        item_id, qty = yields
        player["inventory"][item_id] = player["inventory"].get(item_id, 0) + qty

    profession = None
    if prof_req:
        # L'XP de récolte concerne la profession, pas le niveau général du joueur.
        profession_id = prof_req["profession_id"]
        player_prof = player["professions"].setdefault(profession_id, get_profession(player, profession_id))
        level_before = player_prof["level"]
        player_prof["xp"] += prof_req.get("xp_reward", 0)
        apply_profession_level_ups(player_prof)
        profession = {
            "id": profession_id,
            "xp_gained": prof_req.get("xp_reward", 0),
            "level": player_prof["level"],
            "levels_gained": player_prof["level"] - level_before,
            "xp": player_prof["xp"],
            "xp_to_next_level": xp_to_next_level(player_prof["level"]),
        }

    player["lifetime_counters"]["gathers"][entry["template_id"]] = player["lifetime_counters"]["gathers"].get(entry["template_id"], 0) + 1
    quest_progress = advance_quest_objectives(player, "gather_count", "resource_id", entry["template_id"])

    save_player(req.player_id, player, transaction)
    return {
        "quest_progress": quest_progress,
        "yields": yields,
        "profession": profession,
        "entry_state": public_entry_state(entry, entry_state) if shared else None,
        "player_inventory": player["inventory"],
    }


@router.get("/zones/{zone_id}/state")
def get_zone_state(zone_id: str, player_id: str | None = None):
    """Retourne la disponibilité des entries d'une zone.

    Pour chaque entry partagée : statut, stock restant et secondes avant
    réapparition. Les entries jamais touchées sont disponibles.

    Avec ``player_id``, les entries dont la condition d'apparition n'est pas
    remplie pour ce joueur portent ``locked: true``, et leur ``hint`` si leur
    ``visibility_when_locked`` vaut ``"hinted"`` (sinon elles sont à cacher).
    """
    zone_static = load_static_zone_content(zone_id)
    zone_state = get_zone_dynamic_state(zone_id)
    player = get_player(player_id) if player_id else None
    entries = {}
    for entry in zone_static.get("entries", []):
        result = {}
        monster = load_template("monster", entry["template_id"]) if entry.get("template_type") == "monster" else None
        if is_shared_entry(zone_id, entry, monster):
            state = resolve_entry_state(entry, zone_state["entries"].get(entry["id"]))
            result = public_entry_state(entry, state)
        if player is not None and has_defeated_entry(player, zone_id, entry["id"]):
            result["status"] = "defeated"
        if player is not None and not is_entry_unlocked(entry, player):
            spawn = entry.get("spawn", {})
            hinted = spawn.get("visibility_when_locked") == "hinted"
            result["locked"] = True
            result["hint"] = spawn.get("hint") if hinted else None
            result["hidden"] = not hinted
        if result:
            entries[entry["id"]] = result
    return {"zone_id": zone_id, "entries": entries}


@router.post("/actions/start_quest")
def start_quest(req: StartQuestRequest):
    """Démarre une quête ou relance une quête terminée et répétable."""
    # Charger la définition permet de vérifier l'existence de la quête et de
    # connaître le nombre exact d'objectifs à suivre.
    quest_def = load_template("quest", req.quest_id)
    givers = find_quest_givers(req.quest_id)
    if not givers:
        raise HTTPException(404, "Aucun personnage ne propose cette quête")
    return _start_quest_tx(db.transaction(), req, quest_def, givers)


@firestore.transactional
def _start_quest_tx(transaction, req: StartQuestRequest, quest_def: dict, givers: list[dict]) -> dict:
    player = get_player(req.player_id, transaction)
    # La quête doit être proposée par au moins une entry accessible au joueur
    # (une quête de suite n'apparaît qu'une fois la précédente terminée).
    if not any(is_entry_unlocked(giver["entry"], player) for giver in givers):
        raise HTTPException(403, "Cette quête n'est pas encore disponible")
    existing = player["quests"].get(req.quest_id)
    if existing:
        # Une quête active ne peut pas être démarrée deux fois. Une quête
        # terminée n'est relançable que si son template est répétable.
        if existing.get("state") == "active":
            raise HTTPException(409, "Quête déjà active")
        if existing.get("state") == "completed" and not quest_def.get("repeatable"):
            raise HTTPException(409, "Quête déjà terminée (non répétable)")
    # La progression est indexée comme les objectifs du template afin de garder
    # le même ordre lors de la validation de la remise de quête.
    player["quests"][req.quest_id] = {
        "state": "active",
        "objectives_progress": [{"objective_index": i, "current_count": 0} for i in range(len(quest_def["objectives"]))],
    }
    save_player(req.player_id, player, transaction)
    return {"quest_id": req.quest_id, **quest_progress_view(quest_def, player["quests"][req.quest_id], player)}


@router.post("/actions/abandon_quest")
def abandon_quest(req: AbandonQuestRequest):
    """Abandonne une quête active : sa progression est perdue."""
    return _abandon_quest_tx(db.transaction(), req)


@firestore.transactional
def _abandon_quest_tx(transaction, req: AbandonQuestRequest) -> dict:
    player = get_player(req.player_id, transaction)
    progress = player["quests"].get(req.quest_id)
    if not progress or progress.get("state") != "active":
        raise HTTPException(409, "Quête non active pour ce joueur")
    del player["quests"][req.quest_id]
    save_player(req.player_id, player, transaction)
    return {"abandoned": req.quest_id}


@router.post("/actions/turn_in_quest")
def turn_in_quest(req: TurnInQuestRequest):
    """Valide les objectifs d'une quête, consomme les livraisons et distribue les récompenses."""
    quest_def = load_template("quest", req.quest_id)
    result = _turn_in_quest_tx(db.transaction(), req, quest_def)
    result["newly_unlocked"] = check_and_apply_unlocks(req.player_id)
    return result


@firestore.transactional
def _turn_in_quest_tx(transaction, req: TurnInQuestRequest, quest_def: dict) -> dict:
    # La remise relit le template et l'état joueur pour vérifier la progression
    # réelle, plutôt que de faire confiance aux données envoyées par le client.
    player = get_player(req.player_id, transaction)
    progress = player["quests"].get(req.quest_id)
    if not progress or progress.get("state") != "active":
        raise HTTPException(409, "Quête non active pour ce joueur")
    view = quest_progress_view(quest_def, progress, player)
    for index, objective in enumerate(view["objectives"]):
        if not objective["done"]:
            raise HTTPException(400, f"Objectif {index + 1} non rempli ({objective['current']}/{objective['count']})")
    for obj in quest_def["objectives"]:
        if obj["type"] == "deliver_item":
            # Les items livrés sont retirés uniquement après que tous les
            # objectifs ont été validés, évitant une consommation partielle.
            player["inventory"][obj["item_id"]] -= obj["count"]
            if player["inventory"][obj["item_id"]] <= 0:
                del player["inventory"][obj["item_id"]]
            player["lifetime_counters"]["deliveries"][obj["item_id"]] = player["lifetime_counters"]["deliveries"].get(obj["item_id"], 0) + obj["count"]
    # Les récompenses sont appliquées au même état joueur que les consommations.
    rewards = quest_def.get("rewards", {})
    player["xp"] += rewards.get("xp", 0)
    levels_gained = apply_player_level_ups(player)
    player["currency"]["or"] = player["currency"].get("or", 0) + rewards.get("gold", 0)
    for item in rewards.get("items", []):
        player["inventory"][item["item_id"]] = player["inventory"].get(item["item_id"], 0) + item["quantity"]
    progress["state"] = "completed"
    save_player(req.player_id, player, transaction)
    return {
        "quest_completed": req.quest_id,
        "rewards_given": rewards,
        "levels_gained": levels_gained,
        "player_level": player["level"],
        "player_xp_total": player["xp"],
        "player_stat_points": player["stat_points"],
        "player_currency": player["currency"],
    }


# -----------------------------------------------------------------------------
# Craft et progression des statistiques
# -----------------------------------------------------------------------------

@router.post("/actions/turn_in_quest")
def turn_in_quest(req: TurnInQuestRequest):
    """Valide les objectifs d'une quête, consomme les livraisons et distribue les récompenses."""
    # La remise relit le template et l'état joueur pour vérifier la progression
    # réelle, plutôt que de faire confiance aux données envoyées par le client.
    quest_def = load_template("quest", req.quest_id)
    player = get_player(req.player_id)
    progress = player["quests"].get(req.quest_id)
    if not progress or progress.get("state") != "active":
        raise HTTPException(409, "Quête non active pour ce joueur")
    for i, obj in enumerate(quest_def["objectives"]):
        p = progress["objectives_progress"][i]
        # Les objectifs de collecte et de combat utilisent leur compteur dédié;
        # les livraisons vérifient directement la quantité présente en inventaire.
        if obj["type"] in ("kill_count", "gather_count") and p["current_count"] < obj["count"]:
            raise HTTPException(400, f"Objectif {i} non rempli")
        if obj["type"] == "deliver_item" and player["inventory"].get(obj["item_id"], 0) < obj["count"]:
            raise HTTPException(400, f"Objets manquants pour l'objectif {i} (deliver_item)")
    for obj in quest_def["objectives"]:
        if obj["type"] == "deliver_item":
            # Les items livrés sont retirés uniquement après que tous les
            # objectifs ont été validés, évitant une consommation partielle.
            player["inventory"][obj["item_id"]] -= obj["count"]
            player["lifetime_counters"]["deliveries"][obj["item_id"]] = player["lifetime_counters"]["deliveries"].get(obj["item_id"], 0) + obj["count"]
    # Les récompenses sont appliquées au même état joueur que les consommations.
    rewards = quest_def.get("rewards", {})
    player["xp"] += rewards.get("xp", 0)
    levels_gained = apply_player_level_ups(player)
    player["currency"]["or"] = player["currency"].get("or", 0) + rewards.get("gold", 0)
    for item in rewards.get("items", []):
        player["inventory"][item["item_id"]] = player["inventory"].get(item["item_id"], 0) + item["quantity"]
    progress["state"] = "completed"
    save_player(req.player_id, player)
    newly_unlocked = check_and_apply_unlocks(req.player_id)
    return {"quest_completed": req.quest_id, "rewards_given": rewards, "levels_gained": levels_gained, "player_level": player["level"], "player_xp_total": player["xp"], "player_stat_points": player["stat_points"], "player_currency": player["currency"], "newly_unlocked": newly_unlocked}


# Nombre maximal d'ingrédients différents dans un craft.
MAX_CRAFT_INPUTS = 4


@router.post("/actions/craft")
def craft(req: CraftRequest):
    """Fabrique un item après validation de l'atelier, des paramètres et de l'inventaire.

    Une recette connue est servie depuis le cache Firestore ; une recette
    inédite appelle Claude, sauvegarde le nouvel item et enregistre le résultat
    dans le cache. Les ingrédients ne sont consommés qu'une fois le résultat
    connu, dans une transaction.
    """
    # L'atelier fournit à la fois la profession requise et les paramètres
    # autorisés pour cette recette.
    workshop = load_template("workshop", req.workshop_id)
    declared = {p["id"]: p.get("options", []) for p in workshop.get("parameters", [])}
    for key in declared:
        # Chaque paramètre déclaré est obligatoire et doit utiliser une valeur
        # prévue par le template de l'atelier.
        if key not in req.parameters:
            raise HTTPException(400, f"Paramètre manquant : '{key}'")
        if req.parameters[key] not in declared[key]:
            raise HTTPException(400, f"Valeur invalide pour '{key}' : options {declared[key]}")
    unknown = set(req.parameters) - set(declared)
    if unknown:
        raise HTTPException(400, f"Paramètre(s) inconnu(s) : {sorted(unknown)}")

    item_ids = [inp.item_id for inp in req.inputs]
    if not req.inputs or len(req.inputs) > MAX_CRAFT_INPUTS:
        raise HTTPException(400, f"Choisis entre 1 et {MAX_CRAFT_INPUTS} ingrédients")
    if len(set(item_ids)) != len(item_ids):
        raise HTTPException(400, "Un même ingrédient ne peut apparaître qu'une fois (augmente sa quantité)")
    if any(inp.quantity < 1 for inp in req.inputs):
        raise HTTPException(400, "Quantité invalide")

    # Vérifications avant tout appel IA : on ne paie pas une génération pour un
    # craft que le joueur ne pourrait de toute façon pas réaliser.
    player = get_player(req.player_id)
    profession_id = workshop["profession_required"]["profession_id"]
    profession_level = get_profession(player, profession_id)["level"]
    inputs_full = []
    for inp in req.inputs:
        if player["inventory"].get(inp.item_id, 0) < inp.quantity:
            raise HTTPException(400, f"Pas assez de '{inp.item_id}' en inventaire")
        item = load_template("item", inp.item_id)
        if item.get("level", 1) > profession_level:
            raise HTTPException(403, f"'{item.get('name', inp.item_id)}' (niveau {item.get('level', 1)}) dépasse ton niveau en {profession_id} ({profession_level})")
        inputs_full.append({**item, "quantity": inp.quantity})

    # La clé canonique garantit que l'ordre des inputs/paramètres ne crée pas
    # artificiellement plusieurs recettes équivalentes.
    inputs_for_key = [{"item_id": i.item_id, "quantity": i.quantity} for i in req.inputs]
    recipe_key = build_recipe_key(req.workshop_id, inputs_for_key, req.parameters)
    from_cache = True
    output_item_id = get_cached_craft(recipe_key)
    if output_item_id is None:
        # Une recette inconnue déclenche l'IA une seule fois. Le résultat est
        # sauvegardé avant de consommer les ingrédients du joueur.
        from_cache = False
        new_item = generate_craft_item_via_ai(workshop, req.parameters, inputs_full)
        output_item_id = save_new_craft_item(new_item)
        save_craft_cache(recipe_key, output_item_id)

    output_item = load_template("item", output_item_id)
    result = _craft_consume_tx(db.transaction(), req, profession_id, output_item)
    result.update({"output_item_id": output_item_id, "output_item": output_item, "from_cache": from_cache})
    result["newly_unlocked"] = check_and_apply_unlocks(req.player_id)
    return result


@firestore.transactional
def _craft_consume_tx(transaction, req: CraftRequest, profession_id: str, output_item: dict) -> dict:
    # L'inventaire est revérifié ici : il a pu changer pendant l'appel IA.
    player = get_player(req.player_id, transaction)
    for inp in req.inputs:
        if player["inventory"].get(inp.item_id, 0) < inp.quantity:
            raise HTTPException(409, f"'{inp.item_id}' n'est plus en quantité suffisante dans ton inventaire")
    for inp in req.inputs:
        player["inventory"][inp.item_id] -= inp.quantity
        if player["inventory"][inp.item_id] <= 0:
            del player["inventory"][inp.item_id]
    player["inventory"][output_item["id"]] = player["inventory"].get(output_item["id"], 0) + 1

    # L'XP de craft est calculée à partir du niveau de l'item produit.
    player_prof = player["professions"].setdefault(profession_id, get_profession(player, profession_id))
    level_before = player_prof["level"]
    craft_xp_gained = output_item.get("level", 1) * 10
    player_prof["xp"] += craft_xp_gained
    apply_profession_level_ups(player_prof)
    save_player(req.player_id, player, transaction)
    return {
        "profession": {
            "id": profession_id,
            "xp_gained": craft_xp_gained,
            "level": player_prof["level"],
            "levels_gained": player_prof["level"] - level_before,
            "xp": player_prof["xp"],
            "xp_to_next_level": xp_to_next_level(player_prof["level"]),
        },
        "player_inventory": player["inventory"],
    }


@router.post("/actions/allocate_stat_points")
def allocate_stat_points(req: AllocateStatPointsRequest):
    """Dépense les points de statistiques disponibles du joueur."""
    # Les clés et les valeurs sont contrôlées avant de toucher à l'état
    # Firestore afin de rejeter toute allocation invalide atomiquement côté code.
    invalid_stats = set(req.allocations) - VALID_STATS
    if invalid_stats:
        raise HTTPException(400, f"Stat(s) inconnue(s) : {invalid_stats}")
    if any(v < 0 for v in req.allocations.values()):
        raise HTTPException(400, "Les valeurs doivent être positives")
    return _allocate_stat_points_tx(db.transaction(), req)


@firestore.transactional
def _allocate_stat_points_tx(transaction, req: AllocateStatPointsRequest) -> dict:
    # Deux validations simultanées ne doivent pas dépenser deux fois le même
    # pool de points.
    player = get_player(req.player_id, transaction)
    total_requested = sum(req.allocations.values())
    # Le total demandé est comparé au pool disponible, puis réparti stat par stat.
    if total_requested > player["stat_points"]["available"]:
        raise HTTPException(400, f"Pas assez de points disponibles ({player['stat_points']['available']} restants)")
    for stat, amount in req.allocations.items():
        player["stat_points"]["spent"][stat] += amount
    player["stat_points"]["available"] -= total_requested
    save_player(req.player_id, player, transaction)
    return {"stat_points": player["stat_points"]}


# -----------------------------------------------------------------------------
# Équipement du joueur
# -----------------------------------------------------------------------------

@router.post("/actions/equip_item")
def equip_item(req: EquipItemRequest):
    """Équipe un item et remet l'équipement remplacé dans l'inventaire."""
    return _equip_item_tx(db.transaction(), req)


@firestore.transactional
def _equip_item_tx(transaction, req: EquipItemRequest) -> dict:
    player = get_player(req.player_id, transaction)
    # L'équipement doit appartenir au joueur : le serveur ne doit jamais créer
    # un item à partir d'un simple identifiant envoyé par le client.
    if player["inventory"].get(req.item_id, 0) < 1:
        raise HTTPException(400, f"'{req.item_id}' absent de l'inventaire")
    item = load_template("item", req.item_id)
    if item.get("category") != "equipment":
        raise HTTPException(400, f"'{req.item_id}' n'est pas un équipement")
    equip_info = item.get("equipment", {})
    slot = equip_info.get("slot")
    # Le slot vient du template, jamais de la requête, ce qui empêche d'équiper
    # un item dans une position incompatible avec sa définition.
    if slot not in VALID_SLOTS:
        raise HTTPException(500, f"Slot invalide sur le template '{req.item_id}' : {slot}")
    level_required = equip_info.get("level_required", 1)
    if player["level"] < level_required:
        raise HTTPException(403, f"Niveau {level_required} requis pour équiper '{req.item_id}'")
    previous_item_id = player["equipment"].get(slot)
    # L'item équipé est retiré de l'inventaire et l'ancien est rendu au joueur.
    # Cette permutation évite toute perte lors du remplacement d'équipement.
    player["inventory"][req.item_id] -= 1
    if player["inventory"][req.item_id] <= 0:
        del player["inventory"][req.item_id]
    if previous_item_id:
        player["inventory"][previous_item_id] = player["inventory"].get(previous_item_id, 0) + 1
    player["equipment"][slot] = req.item_id
    save_player(req.player_id, player, transaction)
    return {"slot": slot, "equipped": req.item_id, "previous": previous_item_id, "equipment": player["equipment"]}


@router.post("/actions/unequip_item")
def unequip_item(req: UnequipItemRequest):
    """Retire l'item d'un slot et le rend à l'inventaire du joueur."""
    # Le slot est validé avant la lecture Firestore pour distinguer une mauvaise
    # requête d'un slot valide mais vide.
    if req.slot not in VALID_SLOTS:
        raise HTTPException(400, f"Slot invalide : {req.slot}")
    return _unequip_item_tx(db.transaction(), req)


@firestore.transactional
def _unequip_item_tx(transaction, req: UnequipItemRequest) -> dict:
    player = get_player(req.player_id, transaction)
    current_item_id = player["equipment"].get(req.slot)
    if not current_item_id:
        raise HTTPException(400, f"Rien d'équipé dans le slot '{req.slot}'")
    # Retirer l'équipement est l'opération inverse de equip_item : l'item revient
    # dans l'inventaire et le slot est remis à None.
    player["inventory"][current_item_id] = player["inventory"].get(current_item_id, 0) + 1
    player["equipment"][req.slot] = None
    save_player(req.player_id, player, transaction)
    return {"slot": req.slot, "unequipped": current_item_id, "equipment": player["equipment"]}


@router.post("/actions/equip_skill")
def equip_skill(req: EquipSkillRequest):
    """Équipe une compétence débloquée dans un slot actif ou passif disponible."""
    skill = load_template("skill", req.skill_id)
    return _equip_skill_tx(db.transaction(), req, skill)


@firestore.transactional
def _equip_skill_tx(transaction, req: EquipSkillRequest, skill: dict) -> dict:
    player = get_player(req.player_id, transaction)
    # Le déblocage est vérifié dans l'état joueur, tandis que la nature active ou
    # passive est lue depuis le template de compétence.
    if req.skill_id not in player["skills"]["unlocked"]:
        raise HTTPException(403, f"Compétence '{req.skill_id}' non débloquée")
    kind = skill["kind"]
    slot_list_key = "equipped_active" if kind == "active" else "equipped_passive"
    max_slots = MAX_ACTIVE_SKILL_SLOTS if kind == "active" else MAX_PASSIVE_SKILL_SLOTS
    # Une compétence ne peut apparaître qu'une fois et chaque catégorie possède
    # sa propre limite de slots.
    if req.skill_id in player["skills"][slot_list_key]:
        raise HTTPException(409, f"'{req.skill_id}' déjà équipée")
    if len(player["skills"][slot_list_key]) >= max_slots:
        raise HTTPException(400, f"Plus de slot {kind} disponible ({max_slots} max)")
    player["skills"][slot_list_key].append(req.skill_id)
    save_player(req.player_id, player, transaction)
    return {"equipped": req.skill_id, "kind": kind, "skills": player["skills"]}


@router.post("/actions/unequip_skill")
def unequip_skill(req: UnequipSkillRequest):
    """Retire une compétence de la liste des compétences équipées."""
    return _unequip_skill_tx(db.transaction(), req)


@firestore.transactional
def _unequip_skill_tx(transaction, req: UnequipSkillRequest) -> dict:
    player = get_player(req.player_id, transaction)
    # Les compétences actives et passives sont parcourues car le client ne
    # fournit pas le type de slot, seulement l'identifiant de la compétence.
    for slot_list_key in ("equipped_active", "equipped_passive"):
        if req.skill_id in player["skills"][slot_list_key]:
            player["skills"][slot_list_key].remove(req.skill_id)
            save_player(req.player_id, player, transaction)
            return {"unequipped": req.skill_id, "skills": player["skills"]}
    raise HTTPException(400, f"'{req.skill_id}' n'est équipée dans aucun slot")


# -----------------------------------------------------------------------------
# Titres
# -----------------------------------------------------------------------------

@router.post("/actions/equip_title")
def equip_title(req: EquipTitleRequest):
    """Équipe un titre déjà débloqué par le joueur."""
    # Le chargement vérifie que le fichier du titre existe avant de vérifier son
    # déblocage dans l'état joueur.
    load_template("title", req.title_id)
    player = get_player(req.player_id)
    if req.title_id not in player["titles"]["unlocked"]:
        raise HTTPException(403, f"Titre '{req.title_id}' non débloqué")
    previous_title_id = player["titles"]["equipped"]
    # Le titre précédent est conservé dans la réponse pour permettre au client
    # de mettre à jour son interface sans relire tout le profil.
    player["titles"]["equipped"] = req.title_id
    save_player(req.player_id, player)
    return {"equipped": req.title_id, "previous": previous_title_id, "titles": player["titles"]}


@router.post("/actions/unequip_title")
def unequip_title(req: UnequipTitleRequest):
    """Retire le titre actuellement équipé."""
    player = get_player(req.player_id)
    if not player["titles"]["equipped"]:
        raise HTTPException(400, "Aucun titre équipé")
    # L'identifiant est sauvegardé avant de vider le slot afin de pouvoir le
    # retourner dans la réponse.
    previous_title_id = player["titles"]["equipped"]
    player["titles"]["equipped"] = None
    save_player(req.player_id, player)
    return {"unequipped": previous_title_id, "titles": player["titles"]}


# -----------------------------------------------------------------------------
# Boutiques
# -----------------------------------------------------------------------------

def sell_unit_price(item: dict) -> int:
    """Prix de rachat d'un objet par n'importe quelle boutique (règle globale)."""
    return round(item.get("base_value", 0) * K_SELL)


def load_shop_entry(zone_id: str, entry_id: str) -> tuple[dict, dict]:
    """Retourne l'entry et le template d'une boutique de zone."""
    entry = find_entry(load_static_zone_content(zone_id), entry_id)
    if entry["template_type"] != "shop":
        raise HTTPException(400, "Cette entry n'est pas une boutique")
    return entry, load_template("shop", entry["template_id"])


@router.post("/actions/shop_buy")
def shop_buy(req: ShopBuyRequest):
    """Achète une offre de boutique, en tenant compte d'un éventuel stock partagé."""
    if req.quantity < 1:
        raise HTTPException(400, "Quantité invalide")
    entry, shop = load_shop_entry(req.zone_id, req.entry_id)
    offer = next((o for o in shop["sell_offers"] if o["item_id"] == req.item_id), None)
    if offer is None:
        raise HTTPException(404, f"'{req.item_id}' n'est pas vendu ici")
    return _shop_buy_tx(db.transaction(), req, entry, shop, offer)


@firestore.transactional
def _shop_buy_tx(transaction, req: ShopBuyRequest, entry: dict, shop: dict, offer: dict) -> dict:
    # Le stock n'est consommé que si le paiement est possible : tout est lu,
    # vérifié, puis écrit dans la même transaction.
    limited = offer.get("stock") is not None and entry["state_scope"] == "shared"
    zone_state = get_zone_dynamic_state(req.zone_id, transaction) if limited else None
    player = get_player(req.player_id, transaction)
    require_entry_unlocked(entry, player)

    total_cost = offer["price"] * req.quantity
    currency = shop.get("currency", "or")
    if player["currency"].get(currency, 0) < total_cost:
        raise HTTPException(400, f"Pas assez d'{currency} ({total_cost} requis)")

    if limited:
        # Le stock limité est conservé par entry et par item dans l'état de zone.
        entry_state = zone_state["entries"].setdefault(req.entry_id, {"offers": {}})
        offer_state = entry_state.setdefault("offers", {}).get(req.item_id, {"status": "available", "current_stock": offer["stock"]})
        if offer_state.get("status") == "depleted":
            # Le réapprovisionnement est évalué à la demande, comme les ressources.
            available_again_at = offer_state.get("available_again_at")
            if available_again_at and datetime.now(timezone.utc) >= available_again_at:
                offer_state = {"status": "available", "current_stock": offer["stock"]}
        if offer_state.get("status") != "available" or offer_state.get("current_stock", 0) < req.quantity:
            raise HTTPException(409, f"Stock insuffisant pour '{req.item_id}'")
        offer_state["current_stock"] -= req.quantity
        if offer_state["current_stock"] <= 0:
            restock = offer.get("restock_seconds")
            offer_state = {"status": "depleted", "available_again_at": datetime.now(timezone.utc) + timedelta(seconds=restock) if restock else None}
        entry_state["offers"][req.item_id] = offer_state
        save_zone_dynamic_state(req.zone_id, zone_state, transaction)

    player["currency"][currency] -= total_cost
    player["inventory"][req.item_id] = player["inventory"].get(req.item_id, 0) + req.quantity
    save_player(req.player_id, player, transaction)
    return {"bought": req.item_id, "quantity": req.quantity, "total_cost": total_cost, "player_currency": player["currency"], "player_inventory": player["inventory"]}


@router.post("/actions/shop_sell")
def shop_sell(req: ShopSellRequest):
    """Revend un objet à une boutique au taux global de rachat (K_SELL)."""
    if req.quantity < 1:
        raise HTTPException(400, "Quantité invalide")
    entry, shop = load_shop_entry(req.zone_id, req.entry_id)
    item = load_template("item", req.item_id)
    return _shop_sell_tx(db.transaction(), req, entry, shop, item)


@firestore.transactional
def _shop_sell_tx(transaction, req: ShopSellRequest, entry: dict, shop: dict, item: dict) -> dict:
    player = get_player(req.player_id, transaction)
    require_entry_unlocked(entry, player)
    if player["inventory"].get(req.item_id, 0) < req.quantity:
        raise HTTPException(400, f"Pas assez de '{req.item_id}' en inventaire")
    total_earned = sell_unit_price(item) * req.quantity
    player["inventory"][req.item_id] -= req.quantity
    if player["inventory"][req.item_id] <= 0:
        del player["inventory"][req.item_id]
    currency = shop.get("currency", "or")
    player["currency"][currency] = player["currency"].get(currency, 0) + total_earned
    save_player(req.player_id, player, transaction)
    return {"sold": req.item_id, "quantity": req.quantity, "total_earned": total_earned, "player_currency": player["currency"], "player_inventory": player["inventory"]}


@router.get("/shops/sell_prices")
def shop_sell_prices(player_id: str):
    """Prix de rachat unitaire de chaque objet du sac du joueur."""
    player = get_player(player_id)
    prices = {}
    for item_id in player["inventory"]:
        try:
            prices[item_id] = sell_unit_price(load_template("item", item_id))
        except HTTPException:
            # Un objet sans template ne peut pas être évalué ni revendu.
            continue
    return {"prices": prices}


# -----------------------------------------------------------------------------
# Autel des Ventes (marché entre joueurs)
# -----------------------------------------------------------------------------
#
# Tous les Autels des Ventes partagent les mêmes annonces : un objet déposé à
# un autel s'achète depuis n'importe quel autre. ``market_id`` garde seulement
# l'autel d'origine de l'annonce.

# Nombre de ventes conservées dans l'historique d'un vendeur.
MAX_MARKET_SALES = 20


@router.post("/actions/market_list_item")
def market_list_item(req: ListItemForSaleRequest):
    """Dépose un objet à un Autel des Ventes et crée son annonce."""
    entry = find_entry(load_static_zone_content(req.zone_id), req.entry_id)
    if entry["template_type"] != "player_market":
        raise HTTPException(400, "Cette entry n'est pas un Autel des Ventes")
    market = load_template("player_market", entry["template_id"])
    if req.quantity < 1 or req.price < 0:
        raise HTTPException(400, "Quantité ou prix invalide")
    listing_id = f"listing_{uuid.uuid4().hex[:12]}"
    # L'identifiant aléatoire garantit une clé Firestore distincte pour chaque
    # annonce, même si deux joueurs vendent le même item au même prix.
    listing = {
        "listing_id": listing_id,
        "market_id": entry["template_id"],
        "market_name": market.get("name", entry["template_id"]),
        "seller_player_id": req.player_id,
        "item_id": req.item_id,
        "quantity": req.quantity,
        "price": req.price,
        "currency": market.get("currency", "or"),
        "listed_at": datetime.now(timezone.utc),
    }
    _market_list_item_tx(db.transaction(), req, entry, listing)
    return listing


@firestore.transactional
def _market_list_item_tx(transaction, req: ListItemForSaleRequest, entry: dict, listing: dict) -> None:
    # Le retrait de l'inventaire et la création de l'annonce sont atomiques :
    # deux dépôts simultanés ne peuvent pas lister le même exemplaire.
    player = get_player(req.player_id, transaction)
    require_entry_unlocked(entry, player)
    if player["inventory"].get(req.item_id, 0) < req.quantity:
        raise HTTPException(400, f"Pas assez de '{req.item_id}' en inventaire")
    # Les items sont placés en dépôt immédiatement : ils ne peuvent donc pas
    # être vendus ou listés plusieurs fois pendant la durée de l'annonce.
    player["inventory"][req.item_id] -= req.quantity
    if player["inventory"][req.item_id] <= 0:
        del player["inventory"][req.item_id]
    save_player(req.player_id, player, transaction)
    transaction.set(db.collection("market_listings").document(listing["listing_id"]), listing)


@router.get("/market/listings")
def market_browse():
    """Retourne toutes les annonces actives, des plus récentes aux plus anciennes."""
    # Une annonce active est simplement un document encore présent dans cette
    # collection : l'achat et l'annulation suppriment le document.
    listings = [d.to_dict() for d in db.collection("market_listings").stream()]
    listings.sort(key=lambda listing: listing.get("listed_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return {"listings": listings}


@router.post("/actions/market_buy")
def market_buy(req: BuyListingRequest):
    """Achète une annonce, crédite le vendeur et supprime l'annonce."""
    return _market_buy_tx(db.transaction(), req)


@firestore.transactional
def _market_buy_tx(transaction, req: BuyListingRequest) -> dict:
    # L'annonce, l'acheteur et le vendeur sont lus dans la transaction : si deux
    # achats visent la même annonce, le second est rejoué et la voit supprimée.
    listing_ref = db.collection("market_listings").document(req.listing_id)
    listing_doc = listing_ref.get(transaction=transaction)
    if not listing_doc.exists:
        raise HTTPException(404, "Annonce introuvable (déjà vendue ou annulée)")
    listing = listing_doc.to_dict()
    if listing["seller_player_id"] == req.player_id:
        raise HTTPException(400, "Impossible d'acheter sa propre annonce")
    buyer = get_player(req.player_id, transaction)
    seller = get_player(listing["seller_player_id"], transaction)
    currency = listing["currency"]
    if buyer["currency"].get(currency, 0) < listing["price"]:
        raise HTTPException(400, f"Pas assez d'{currency}")
    # L'acheteur paie et reçoit les items, le vendeur est crédité et l'annonce
    # disparaît : les trois écritures sont validées ensemble au commit.
    buyer["currency"][currency] -= listing["price"]
    buyer["inventory"][listing["item_id"]] = buyer["inventory"].get(listing["item_id"], 0) + listing["quantity"]
    save_player(req.player_id, buyer, transaction)
    seller["currency"][currency] = seller["currency"].get(currency, 0) + listing["price"]
    # Le vendeur n'est pas forcément connecté : la vente est notée dans son
    # historique pour qu'il la retrouve dans ses annonces.
    sales = seller.setdefault("market_sales", [])
    sales.insert(0, {
        "item_id": listing["item_id"],
        "quantity": listing["quantity"],
        "price": listing["price"],
        "currency": currency,
        "sold_at": datetime.now(timezone.utc),
    })
    del sales[MAX_MARKET_SALES:]
    save_player(listing["seller_player_id"], seller, transaction)
    transaction.delete(listing_ref)
    return {"bought": listing["item_id"], "quantity": listing["quantity"], "price_paid": listing["price"], "player_currency": buyer["currency"], "player_inventory": buyer["inventory"]}


@router.post("/actions/market_cancel_listing")
def market_cancel_listing(req: CancelListingRequest):
    """Annule une annonce appartenant au joueur et restitue les items déposés."""
    return _market_cancel_listing_tx(db.transaction(), req)


@firestore.transactional
def _market_cancel_listing_tx(transaction, req: CancelListingRequest) -> dict:
    # L'identité du vendeur est comparée côté serveur : un client ne peut pas
    # annuler l'annonce d'un autre joueur en connaissant seulement son ID.
    # La transaction empêche aussi qu'une annulation et un achat simultanés
    # rendent l'item au vendeur tout en le livrant à l'acheteur.
    listing_ref = db.collection("market_listings").document(req.listing_id)
    listing_doc = listing_ref.get(transaction=transaction)
    if not listing_doc.exists:
        raise HTTPException(404, "Annonce introuvable")
    listing = listing_doc.to_dict()
    if listing["seller_player_id"] != req.player_id:
        raise HTTPException(403, "Seul le vendeur peut annuler cette annonce")
    seller = get_player(req.player_id, transaction)
    # La restitution et la suppression de l'annonce sont validées ensemble.
    seller["inventory"][listing["item_id"]] = seller["inventory"].get(listing["item_id"], 0) + listing["quantity"]
    save_player(req.player_id, seller, transaction)
    transaction.delete(listing_ref)
    return {"cancelled": req.listing_id, "item_returned": listing["item_id"], "quantity": listing["quantity"]}