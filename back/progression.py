"""Progression des joueurs : deblocages et recompenses de combat."""

import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

try:
    from .config import TEMPLATES_DIR
    from .content_store import find_entry, load_all_floor_skeletons, load_static_zone_content, load_template
    from .game_rules import apply_player_level_ups, compute_kill_xp, roll_loot_table, evaluate_condition
    from .player_store import get_player, get_zone_dynamic_state, save_player, save_zone_dynamic_state
except ImportError:
    from config import TEMPLATES_DIR
    from content_store import find_entry, load_all_floor_skeletons, load_static_zone_content, load_template
    from game_rules import apply_player_level_ups, compute_kill_xp, roll_loot_table, evaluate_condition
    from player_store import get_player, get_zone_dynamic_state, save_player, save_zone_dynamic_state


def check_and_apply_unlocks(player_id: str) -> list[str]:
    player = get_player(player_id)
    newly_unlocked = []

    for skeleton in load_all_floor_skeletons():
        for zone in skeleton.get("zones", []):
            if zone["id"] in player["unlocks"]["zones"]:
                continue
            if evaluate_condition(zone.get("unlock_condition"), player):
                player["unlocks"]["zones"].append(zone["id"])
                newly_unlocked.append(f"zone:{zone['id']}")

    for folder, unlock_key, player_key in (
        ("skills", "skills", "skills"),
        ("titles", "titles", "titles"),
    ):
        template_dir = TEMPLATES_DIR / folder
        if not template_dir.exists():
            continue
        for path in template_dir.glob("*.json"):
            template = json.loads(path.read_text(encoding="utf-8"))
            if template["id"] in player[player_key]["unlocked"]:
                continue
            if evaluate_condition(template.get("unlock_condition"), player):
                player[player_key]["unlocked"].append(template["id"])
                player["unlocks"][unlock_key].append(template["id"])
                newly_unlocked.append(f"{unlock_key[:-1]}:{template['id']}")

    if newly_unlocked:
        save_player(player_id, player)
    return newly_unlocked


def apply_monster_kill_rewards(player_id: str, zone_id: str, entry_id: str) -> dict:
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
        item_id, quantity = loot
        player["inventory"][item_id] = player["inventory"].get(item_id, 0) + quantity

    xp_gained = compute_kill_xp(monster["level"], monster.get("role", "normal"))
    player["xp"] += xp_gained
    levels_gained = apply_player_level_ups(player)
    player["lifetime_counters"]["kills"][entry["template_id"]] = player["lifetime_counters"]["kills"].get(entry["template_id"], 0) + 1

    for quest_id, progress in player["quests"].items():
        if progress.get("state") != "active":
            continue
        quest_def = load_template("quest", quest_id)
        for index, objective in enumerate(quest_def["objectives"]):
            if objective["type"] == "kill_count" and objective["monster_id"] == entry["template_id"]:
                progress["objectives_progress"][index]["current_count"] += 1

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
