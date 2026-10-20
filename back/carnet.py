"""Carnet d'aventure : ce que le joueur a découvert de chaque étage.

Le carnet se déduit de l'état du joueur, sans rien stocker de plus : les
créatures vaincues (``lifetime_counters.kills``), les ressources récoltées
(``lifetime_counters.gathers``) et les habitants des zones qu'il peut visiter.
Ce qui n'a pas encore été découvert n'est que compté, jamais décrit.
"""

from fastapi import HTTPException

try:
    from .content_store import load_ordered_floors, load_static_zone_content, load_template
    from .game_rules import is_entry_unlocked, is_zone_accessible
except ImportError:
    from content_store import load_ordered_floors, load_static_zone_content, load_template
    from game_rules import is_entry_unlocked, is_zone_accessible


def floor_entries(floor: dict) -> list[tuple[dict, dict]]:
    """(zone du squelette, entry) de chaque entry des zones générées de l'étage."""
    entries = []
    for zone in floor.get("zones", []):
        try:
            content = load_static_zone_content(zone["id"])
        except HTTPException:
            continue
        entries += [(zone, entry) for entry in content.get("entries", [])]
    return entries


def item_names(item_ids) -> list[str]:
    names = []
    for item_id in dict.fromkeys(item_ids):
        try:
            names.append(load_template("item", item_id).get("name", item_id))
        except HTTPException:
            continue
    return names


def floor_carnet(floor: dict, player: dict) -> dict:
    kills = player["lifetime_counters"].get("kills", {})
    gathers = player["lifetime_counters"].get("gathers", {})
    monsters, resources, npcs = {}, {}, {}

    for zone, entry in floor_entries(floor):
        template_id = entry.get("template_id")
        kind = entry.get("template_type")
        if kind == "monster":
            monsters.setdefault(template_id, zone)
        elif kind == "resource":
            resources.setdefault(template_id, zone)
        elif kind == "npc":
            # Un habitant se rencontre dans une zone ouverte, une fois son
            # éventuelle condition d'apparition remplie.
            met = is_zone_accessible(zone, player) and is_entry_unlocked(entry, player)
            if met or template_id not in npcs:
                npcs[template_id] = (zone, met)

    bestiary = []
    for monster_id in monsters:
        if kills.get(monster_id, 0) <= 0:
            continue
        monster = load_template("monster", monster_id)
        bestiary.append({
            "id": monster_id,
            "name": monster.get("name", monster_id),
            "level": monster.get("level", 1),
            "role": monster.get("role", "normal"),
            "description": monster.get("description", ""),
            "kills": kills[monster_id],
            "loot": item_names(line["item_id"] for line in monster.get("loot_table") or []),
        })
    bestiary.sort(key=lambda m: (m["level"], m["name"]))

    harvests = []
    for resource_id in resources:
        if gathers.get(resource_id, 0) <= 0:
            continue
        resource = load_template("resource", resource_id)
        harvests.append({
            "id": resource_id,
            "name": resource.get("name", resource_id),
            "level": resource.get("level", 1),
            "description": resource.get("description", ""),
            "profession_id": (resource.get("profession_required") or {}).get("profession_id", ""),
            "gathers": gathers[resource_id],
            "yields": item_names(line["item_id"] for line in resource.get("yields") or []),
        })
    harvests.sort(key=lambda r: (r["profession_id"], r["level"], r["name"]))

    inhabitants = []
    for npc_id, (zone, met) in npcs.items():
        if not met:
            continue
        npc = load_template("npc", npc_id)
        inhabitants.append({
            "id": npc_id,
            "name": npc.get("name", npc_id),
            "role": npc.get("role", ""),
            "description": npc.get("description", ""),
            "dialogues": npc.get("dialogues") or [],
            "zone_name": zone.get("name", zone["id"]),
        })

    return {
        "id": floor["id"],
        "name": floor.get("name"),
        "bestiary": bestiary,
        "unknown_monsters": len(monsters) - len(bestiary),
        "harvests": harvests,
        "unknown_resources": len(resources) - len(harvests),
        "inhabitants": inhabitants,
        "unknown_inhabitants": len(npcs) - len(inhabitants),
    }


def build_carnet(player: dict) -> list[dict]:
    """Carnet de chaque étage où le joueur a déjà mis les pieds."""
    carnet = []
    for floor in load_ordered_floors():
        page = floor_carnet(floor, player)
        if page["bestiary"] or page["harvests"] or page["inhabitants"]:
            carnet.append(page)
    return carnet
