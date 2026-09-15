"""Regles de progression, statistiques et conditions du jeu."""

import random
from datetime import datetime, timezone

from fastapi import HTTPException

try:
    from .content_store import load_static_zone_content, load_template
    from .player_store import get_zone_dynamic_state
except ImportError:
    from content_store import load_static_zone_content, load_template
    from player_store import get_zone_dynamic_state

BASE_STATS = {"hp": 80, "atk": 8, "def": 5, "agi": 5, "int": 6, "sag": 5}
POINT_VALUE = {"hp": 25, "atk": 2, "def": 2, "agi": 1, "int": 2, "sag": 1}


def roll_loot_table(loot_table: list[dict]) -> tuple[str, int] | None:
    if not loot_table:
        return None
    total_weight = sum(entry["weight"] for entry in loot_table)
    roll = random.uniform(0, total_weight)
    cumulative = 0
    for entry in loot_table:
        cumulative += entry["weight"]
        if roll <= cumulative:
            quantity = random.randint(entry["quantity_min"], entry["quantity_max"])
            return entry["item_id"], quantity
    return None


def compute_kill_xp(monster_level: int, role: str) -> int:
    multiplier = {"normal": 10, "elite": 30, "boss": 100}.get(role, 10)
    return monster_level * multiplier


def xp_to_next_level(level: int) -> int:
    return 50 * max(level, 1)


def apply_profession_level_ups(profession: dict) -> None:
    while profession["xp"] >= xp_to_next_level(profession["level"]):
        profession["xp"] -= xp_to_next_level(profession["level"])
        profession["level"] += 1


def apply_player_level_ups(player: dict) -> int:
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
    condition_type = condition["type"]
    if condition_type == "level":
        return player["level"] >= condition["min"]
    if condition_type == "quest":
        quest = player["quests"].get(condition["quest_id"])
        return bool(quest) and quest.get("state") == condition["state"]
    if condition_type == "item":
        return player["inventory"].get(condition["item_id"], 0) > 0
    if condition_type == "zone":
        return condition["zone_id"] in player["unlocks"]["zones"]
    if condition_type == "boss":
        return is_boss_defeated(condition["zone_id"])
    if condition_type == "kill_count":
        return player["lifetime_counters"]["kills"].get(condition["monster_id"], 0) >= condition["count"]
    if condition_type == "gather_count":
        return player["lifetime_counters"]["gathers"].get(condition["resource_id"], 0) >= condition["count"]
    if condition_type == "deliver_item":
        return player["lifetime_counters"]["deliveries"].get(condition["item_id"], 0) >= condition["count"]
    if condition_type == "group":
        results = [evaluate_condition(item, player) for item in condition["conditions"]]
        return all(results) if condition["mode"] == "all" else any(results)
    return False


def _accumulate_stat_effects(effects: list[dict], stats: dict, percent_bonuses: dict) -> None:
    for effect in effects:
        if effect["type"] == "stat_bonus":
            stats[effect["stat"]] = stats.get(effect["stat"], 0) + effect["amount"]
        elif effect["type"] == "stat_bonus_percent":
            percent_bonuses[effect["stat"]] = percent_bonuses.get(effect["stat"], 0) + effect["amount_percent"]


def compute_final_stats(player: dict) -> dict:
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
