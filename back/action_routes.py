"""Routes HTTP des actions métier du joueur."""

from datetime import datetime, timedelta, timezone
import uuid

from fastapi import APIRouter, HTTPException

try:
    from .content_store import find_entry, load_static_zone_content, load_template
    from .craft_service import (
        build_recipe_key, generate_craft_item_via_ai, get_cached_craft,
        save_craft_cache, save_new_craft_item,
    )
    from .database import db
    from .game_rules import apply_player_level_ups, apply_profession_level_ups, roll_loot_table
    from .models import *
    from .player_store import get_player, get_zone_dynamic_state, save_player, save_zone_dynamic_state
    from .progression import apply_monster_kill_rewards, check_and_apply_unlocks
except ImportError:
    from content_store import find_entry, load_static_zone_content, load_template
    from craft_service import (
        build_recipe_key, generate_craft_item_via_ai, get_cached_craft,
        save_craft_cache, save_new_craft_item,
    )
    from database import db
    from game_rules import apply_player_level_ups, apply_profession_level_ups, roll_loot_table
    from models import *
    from player_store import get_player, get_zone_dynamic_state, save_player, save_zone_dynamic_state
    from progression import apply_monster_kill_rewards, check_and_apply_unlocks


router = APIRouter()
K_SELL = 0.35
VALID_STATS = {"hp", "atk", "def", "agi", "int", "sag"}
VALID_SLOTS = {"weapon", "head", "chest", "gloves", "boots", "ring", "amulet", "tool"}
MAX_ACTIVE_SKILL_SLOTS = 8
MAX_PASSIVE_SKILL_SLOTS = 4


@router.post("/actions/monster_kill")
def report_monster_kill(req: MonsterKillRequest):
    return apply_monster_kill_rewards(req.player_id, req.zone_id, req.entry_id)


@router.post("/actions/resource_gather")
def report_resource_gather(req: ResourceGatherRequest):
    zone_static = load_static_zone_content(req.zone_id)
    entry = find_entry(zone_static, req.entry_id)
    if entry["template_type"] != "resource":
        raise HTTPException(400, "Cette entry n'est pas une ressource")
    resource = load_template("resource", entry["template_id"])

    player = get_player(req.player_id)
    prof_req = resource.get("profession_required")
    if prof_req:
        player_prof_check = player["professions"].get(prof_req["profession_id"], {"level": 0, "xp": 0})
        if player_prof_check["level"] < prof_req["min_level"]:
            raise HTTPException(403, f"Niveau {prof_req['min_level']} en {prof_req['profession_id']} requis")

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
                entry_state = {"status": "depleted", "available_again_at": datetime.now(timezone.utc) + timedelta(seconds=cooldown)}
            else:
                entry_state = {"status": "available", "current_stock": current_stock}
        else:
            entry_state = {"status": "depleted", "available_again_at": datetime.now(timezone.utc) + timedelta(seconds=cooldown)}
        zone_state["entries"][req.entry_id] = entry_state
        save_zone_dynamic_state(req.zone_id, zone_state)

    yields = roll_loot_table(resource.get("yields", []))
    if yields:
        item_id, qty = yields
        player["inventory"][item_id] = player["inventory"].get(item_id, 0) + qty

    profession_xp_gained = 0
    if prof_req:
        profession_id = prof_req["profession_id"]
        player_prof = player["professions"].setdefault(profession_id, {"level": 0, "xp": 0})
        profession_xp_gained = prof_req.get("xp_reward", 0)
        player_prof["xp"] += profession_xp_gained
        apply_profession_level_ups(player_prof)

    player["lifetime_counters"]["gathers"][entry["template_id"]] = player["lifetime_counters"]["gathers"].get(entry["template_id"], 0) + 1
    for quest_id, progress in player["quests"].items():
        if progress.get("state") != "active":
            continue
        quest_def = load_template("quest", quest_id)
        for i, obj in enumerate(quest_def["objectives"]):
            if obj["type"] == "gather_count" and obj["resource_id"] == entry["template_id"]:
                progress["objectives_progress"][i]["current_count"] += 1

    save_player(req.player_id, player)
    newly_unlocked = check_and_apply_unlocks(req.player_id)
    return {"yields": yields, "profession_xp_gained": profession_xp_gained, "player_professions": player["professions"], "player_inventory": player["inventory"], "newly_unlocked": newly_unlocked}


@router.post("/actions/start_quest")
def start_quest(req: StartQuestRequest):
    quest_def = load_template("quest", req.quest_id)
    player = get_player(req.player_id)
    existing = player["quests"].get(req.quest_id)
    if existing:
        if existing.get("state") == "active":
            raise HTTPException(409, "Quête déjà active")
        if existing.get("state") == "completed" and not quest_def.get("repeatable"):
            raise HTTPException(409, "Quête déjà terminée (non répétable)")
    player["quests"][req.quest_id] = {"state": "active", "objectives_progress": [{"objective_index": i, "current_count": 0} for i in range(len(quest_def["objectives"]))]}
    save_player(req.player_id, player)
    return player["quests"][req.quest_id]


@router.post("/actions/turn_in_quest")
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
            player["lifetime_counters"]["deliveries"][obj["item_id"]] = player["lifetime_counters"]["deliveries"].get(obj["item_id"], 0) + obj["count"]
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


@router.post("/actions/craft")
def craft(req: CraftRequest):
    workshop = load_template("workshop", req.workshop_id)
    declared = {p["id"]: p.get("options", []) for p in workshop.get("parameters", [])}
    for key in declared:
        if key not in req.parameters:
            raise HTTPException(400, f"Paramètre manquant : '{key}'")
        if req.parameters[key] not in declared[key]:
            raise HTTPException(400, f"Valeur invalide pour '{key}' : options {declared[key]}")
    player = get_player(req.player_id)
    profession_id = workshop["profession_required"]["profession_id"]
    player_prof = player["professions"].setdefault(profession_id, {"level": 0, "xp": 0})
    inputs_full = []
    for inp in req.inputs:
        if player["inventory"].get(inp.item_id, 0) < inp.quantity:
            raise HTTPException(400, f"Pas assez de '{inp.item_id}' en inventaire")
        item = load_template("item", inp.item_id)
        if item.get("level", 0) > player_prof["level"]:
            raise HTTPException(403, f"'{inp.item_id}' (niveau {item['level']}) dépasse ton niveau en {profession_id} ({player_prof['level']})")
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
    for inp in req.inputs:
        player["inventory"][inp.item_id] -= inp.quantity
        if player["inventory"][inp.item_id] <= 0:
            del player["inventory"][inp.item_id]
    player["inventory"][output_item_id] = player["inventory"].get(output_item_id, 0) + 1
    output_item = load_template("item", output_item_id)
    craft_xp_gained = output_item.get("level", 1) * 10
    player_prof["xp"] += craft_xp_gained
    apply_profession_level_ups(player_prof)
    save_player(req.player_id, player)
    newly_unlocked = check_and_apply_unlocks(req.player_id)
    return {"output_item_id": output_item_id, "from_cache": from_cache, "craft_xp_gained": craft_xp_gained, "player_professions": player["professions"], "player_inventory": player["inventory"], "newly_unlocked": newly_unlocked}


@router.post("/actions/allocate_stat_points")
def allocate_stat_points(req: AllocateStatPointsRequest):
    invalid_stats = set(req.allocations) - VALID_STATS
    if invalid_stats:
        raise HTTPException(400, f"Stat(s) inconnue(s) : {invalid_stats}")
    if any(v < 0 for v in req.allocations.values()):
        raise HTTPException(400, "Les valeurs doivent être positives")
    player = get_player(req.player_id)
    total_requested = sum(req.allocations.values())
    if total_requested > player["stat_points"]["available"]:
        raise HTTPException(400, f"Pas assez de points disponibles ({player['stat_points']['available']} restants)")
    for stat, amount in req.allocations.items():
        player["stat_points"]["spent"][stat] += amount
    player["stat_points"]["available"] -= total_requested
    save_player(req.player_id, player)
    return {"stat_points": player["stat_points"]}


@router.post("/actions/equip_item")
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
    player["inventory"][req.item_id] -= 1
    if player["inventory"][req.item_id] <= 0:
        del player["inventory"][req.item_id]
    if previous_item_id:
        player["inventory"][previous_item_id] = player["inventory"].get(previous_item_id, 0) + 1
    player["equipment"][slot] = req.item_id
    save_player(req.player_id, player)
    return {"slot": slot, "equipped": req.item_id, "previous": previous_item_id, "equipment": player["equipment"]}


@router.post("/actions/unequip_item")
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


@router.post("/actions/equip_skill")
def equip_skill(req: EquipSkillRequest):
    skill = load_template("skill", req.skill_id)
    player = get_player(req.player_id)
    if req.skill_id not in player["skills"]["unlocked"]:
        raise HTTPException(403, f"Compétence '{req.skill_id}' non débloquée")
    kind = skill["kind"]
    slot_list_key = "equipped_active" if kind == "active" else "equipped_passive"
    max_slots = MAX_ACTIVE_SKILL_SLOTS if kind == "active" else MAX_PASSIVE_SKILL_SLOTS
    if req.skill_id in player["skills"][slot_list_key]:
        raise HTTPException(409, f"'{req.skill_id}' déjà équipée")
    if len(player["skills"][slot_list_key]) >= max_slots:
        raise HTTPException(400, f"Plus de slot {kind} disponible ({max_slots} max)")
    player["skills"][slot_list_key].append(req.skill_id)
    save_player(req.player_id, player)
    return {"equipped": req.skill_id, "kind": kind, "skills": player["skills"]}


@router.post("/actions/unequip_skill")
def unequip_skill(req: UnequipSkillRequest):
    player = get_player(req.player_id)
    for slot_list_key in ("equipped_active", "equipped_passive"):
        if req.skill_id in player["skills"][slot_list_key]:
            player["skills"][slot_list_key].remove(req.skill_id)
            save_player(req.player_id, player)
            return {"unequipped": req.skill_id, "skills": player["skills"]}
    raise HTTPException(400, f"'{req.skill_id}' n'est équipée dans aucun slot")


@router.post("/actions/equip_title")
def equip_title(req: EquipTitleRequest):
    load_template("title", req.title_id)
    player = get_player(req.player_id)
    if req.title_id not in player["titles"]["unlocked"]:
        raise HTTPException(403, f"Titre '{req.title_id}' non débloqué")
    previous_title_id = player["titles"]["equipped"]
    player["titles"]["equipped"] = req.title_id
    save_player(req.player_id, player)
    return {"equipped": req.title_id, "previous": previous_title_id, "titles": player["titles"]}


@router.post("/actions/unequip_title")
def unequip_title(req: UnequipTitleRequest):
    player = get_player(req.player_id)
    if not player["titles"]["equipped"]:
        raise HTTPException(400, "Aucun titre équipé")
    previous_title_id = player["titles"]["equipped"]
    player["titles"]["equipped"] = None
    save_player(req.player_id, player)
    return {"unequipped": previous_title_id, "titles": player["titles"]}


@router.post("/actions/buy_from_shop")
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
    if entry["state_scope"] == "shared" and offer.get("stock") is not None:
        zone_state = get_zone_dynamic_state(req.zone_id)
        entry_state = zone_state["entries"].get(req.entry_id, {"offers": {}})
        offer_state = entry_state.get("offers", {}).get(req.item_id, {"status": "available", "current_stock": offer["stock"]})
        if offer_state.get("status") == "depleted":
            available_again_at = offer_state.get("available_again_at")
            if available_again_at and datetime.now(timezone.utc) >= available_again_at:
                offer_state = {"status": "available", "current_stock": offer["stock"]}
        if offer_state.get("status") != "available" or offer_state.get("current_stock", 0) < req.quantity:
            raise HTTPException(409, f"Stock insuffisant pour '{req.item_id}' (réapprovisionnement en cours)")
        offer_state["current_stock"] -= req.quantity
        if offer_state["current_stock"] <= 0:
            restock = offer.get("restock_seconds")
            offer_state = {"status": "depleted", "available_again_at": datetime.now(timezone.utc) + timedelta(seconds=restock) if restock else None}
        entry_state.setdefault("offers", {})[req.item_id] = offer_state
        zone_state["entries"][req.entry_id] = entry_state
        save_zone_dynamic_state(req.zone_id, zone_state)
    player["currency"][currency] = player["currency"].get(currency, 0) - total_price
    player["inventory"][req.item_id] = player["inventory"].get(req.item_id, 0) + req.quantity
    save_player(req.player_id, player)
    return {"bought": req.item_id, "quantity": req.quantity, "total_price": total_price, "player_currency": player["currency"], "player_inventory": player["inventory"]}


@router.post("/actions/sell_to_server")
def sell_to_server(req: SellToServerRequest):
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
    return {"sold": req.item_id, "quantity": req.quantity, "total_gained": total_gained, "player_currency": player["currency"], "player_inventory": player["inventory"]}


@router.post("/actions/shop_buy")
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
    if offer.get("stock") is not None and entry["state_scope"] == "shared":
        zone_state = get_zone_dynamic_state(req.zone_id)
        entry_state = zone_state["entries"].setdefault(req.entry_id, {"offers": {}})
        offer_state = entry_state["offers"].get(req.item_id, {"status": "available", "current_stock": offer["stock"]})
        if offer_state.get("status") == "depleted":
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
    return {"bought": req.item_id, "quantity": req.quantity, "total_cost": total_cost, "player_currency": player["currency"], "player_inventory": player["inventory"]}


@router.post("/actions/shop_sell")
def shop_sell(req: ShopSellRequest):
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
    return {"sold": req.item_id, "quantity": req.quantity, "total_earned": total_earned, "player_currency": player["currency"], "player_inventory": player["inventory"]}


@router.post("/actions/market_list_item")
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
    player["inventory"][req.item_id] -= req.quantity
    if player["inventory"][req.item_id] <= 0:
        del player["inventory"][req.item_id]
    save_player(req.player_id, player)
    listing_id = f"listing_{uuid.uuid4().hex[:12]}"
    listing = {"listing_id": listing_id, "market_id": entry["template_id"], "seller_player_id": req.player_id, "item_id": req.item_id, "quantity": req.quantity, "price": req.price, "currency": market.get("currency", "or"), "listed_at": datetime.now(timezone.utc)}
    db.collection("market_listings").document(listing_id).set(listing)
    return listing


@router.get("/market/{market_id}/listings")
def market_browse(market_id: str):
    docs = db.collection("market_listings").where("market_id", "==", market_id).stream()
    return [d.to_dict() for d in docs]


@router.post("/actions/market_buy")
def market_buy(req: BuyListingRequest):
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
    seller = get_player(listing["seller_player_id"])
    seller["currency"][currency] = seller["currency"].get(currency, 0) + listing["price"]
    save_player(listing["seller_player_id"], seller)
    listing_ref.delete()
    return {"bought": listing["item_id"], "quantity": listing["quantity"], "price_paid": listing["price"], "player_currency": buyer["currency"], "player_inventory": buyer["inventory"]}


@router.post("/actions/market_cancel_listing")
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