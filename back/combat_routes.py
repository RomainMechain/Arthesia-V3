"""WebSocket et regles de combat temps reel."""

import asyncio
import random
import time
import uuid

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

try:
    from .content_store import find_entry, load_static_zone_content, load_template
    from .game_rules import compute_final_stats
    from .progression import apply_monster_kill_rewards
    from .player_store import get_player
except ImportError:
    from content_store import find_entry, load_static_zone_content, load_template
    from game_rules import compute_final_stats
    from progression import apply_monster_kill_rewards
    from player_store import get_player

router = APIRouter()
TICK_INTERVAL_SECONDS = 0.5
K_DEF = 20
BASE_ATTACK_COOLDOWN = 2.0
K_AGI = 0.03
BASIC_ATTACK_POWER = 1.0
BASE_MANA = 50
K_INT = 4
BASE_MANA_REGEN = 1
K_SAG = 0.15
active_combats: dict[str, dict] = {}


def compute_basic_attack_cooldown(agi: float) -> float:
    return BASE_ATTACK_COOLDOWN / (1 + agi * K_AGI)


def compute_max_mana(int_stat: float) -> float:
    return BASE_MANA + int_stat * K_INT


def compute_mana_regen(sag_stat: float) -> float:
    return BASE_MANA_REGEN + sag_stat * K_SAG


def compute_mitigation(defense: float) -> float:
    return defense / (defense + K_DEF)


async def combat_tick_loop(websocket: WebSocket, combat_id: str):
    try:
        while combat_id in active_combats:
            await asyncio.sleep(TICK_INTERVAL_SECONDS)
            session = active_combats.get(combat_id)
            if session is None:
                break

            player = get_player(session["player_id"])
            player_stats = compute_final_stats(player)
            regen = compute_mana_regen(player_stats.get("sag", 0)) * TICK_INTERVAL_SECONDS
            session["player_current_mana"] = min(
                session["player_max_mana"], session["player_current_mana"] + regen
            )

            monster = load_template("monster", session["monster_template_id"])
            moveset = monster.get("moveset", [])
            cooldowns = session["monster_cooldowns"]
            for skill in moveset:
                sid = skill["id"]
                cooldowns[sid] = max(0.0, cooldowns.get(sid, 0.0) - TICK_INTERVAL_SECONDS)

            ready_skills = [skill for skill in moveset if cooldowns.get(skill["id"], 0.0) <= 0]
            if not ready_skills:
                continue
            chosen = random.choices(ready_skills, weights=[skill["weight"] for skill in ready_skills], k=1)[0]
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
    except Exception as exc:
        print(f"⚠️  Erreur dans la boucle de combat {combat_id} : {exc}")


@router.websocket("/ws/combat")
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
                player_stats = compute_final_stats(get_player(player_id))
                player_hp = player_stats["hp"]
                player_max_mana = compute_max_mana(player_stats.get("int", 0))
                combat_id = str(uuid.uuid4())
                active_combats[combat_id] = {
                    "player_id": player_id, "zone_id": zone_id, "entry_id": entry_id,
                    "monster_template_id": entry["template_id"],
                    "monster_current_hp": monster["stats"]["hp"], "monster_max_hp": monster["stats"]["hp"],
                    "monster_cooldowns": {}, "player_current_hp": player_hp, "player_max_hp": player_hp,
                    "player_basic_attack_ready_at": time.monotonic(),
                    "player_current_mana": player_max_mana, "player_max_mana": player_max_mana,
                    "player_skill_cooldowns": {},
                }
                await websocket.send_json({
                    "type": "combat_started", "combat_id": combat_id,
                    "monster_name": monster["name"], "monster_hp": monster["stats"]["hp"],
                    "player_hp": player_hp, "player_mana": player_max_mana,
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
                player_stats = compute_final_stats(get_player(session["player_id"]))
                monster = load_template("monster", session["monster_template_id"])
                raw_damage = BASIC_ATTACK_POWER * player_stats["atk"]
                final_damage = round(raw_damage * (1 - compute_mitigation(monster["stats"]["def"])), 1)
                session["monster_current_hp"] = max(0.0, session["monster_current_hp"] - final_damage)
                cooldown = compute_basic_attack_cooldown(player_stats["agi"])
                session["player_basic_attack_ready_at"] = now + cooldown
                await websocket.send_json({
                    "type": "player_basic_attack", "raw_damage": round(raw_damage, 1),
                    "final_damage": final_damage, "monster_hp": session["monster_current_hp"],
                    "monster_max_hp": session["monster_max_hp"], "next_ready_in": round(cooldown, 2),
                })
                if session["monster_current_hp"] <= 0:
                    del active_combats[combat_id]
                    try:
                        await websocket.send_json({"type": "combat_won", **apply_monster_kill_rewards(session["player_id"], session["zone_id"], session["entry_id"])})
                    except HTTPException as exc:
                        await websocket.send_json({"type": "combat_won_no_reward", "reason": exc.detail})

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
                    effect_type = effect["type"]
                    if effect_type == "damage":
                        stat = "int" if skill.get("damage_type") == "magical" else "atk"
                        raw = effect["power"] * player_stats.get(stat, 0)
                        final = round(raw * (1 - compute_mitigation(monster["stats"].get("def", 0))), 1)
                        session["monster_current_hp"] = max(0.0, session["monster_current_hp"] - final)
                        results.append({"effect": "damage", "final_damage": final})
                    elif effect_type == "heal":
                        raw = round(effect["power"] * player_stats.get("int", 0), 1)
                        session["player_current_hp"] = min(session["player_max_hp"], session["player_current_hp"] + raw)
                        results.append({"effect": "heal", "amount": raw})
                    else:
                        results.append({"effect": effect_type, "note": "pas encore implémenté mécaniquement"})
                await websocket.send_json({
                    "type": "player_skill_used", "skill_id": skill_id, "results": results,
                    "monster_hp": session["monster_current_hp"], "player_hp": session["player_current_hp"],
                    "player_mana": session["player_current_mana"],
                })
                if session["monster_current_hp"] <= 0:
                    del active_combats[combat_id]
                    try:
                        await websocket.send_json({"type": "combat_won", **apply_monster_kill_rewards(session["player_id"], session["zone_id"], session["entry_id"])})
                    except HTTPException as exc:
                        await websocket.send_json({"type": "combat_won_no_reward", "reason": exc.detail})
            else:
                await websocket.send_json({"type": "error", "message": "Action pas encore gérée à cette étape"})
    except WebSocketDisconnect:
        if tick_task:
            tick_task.cancel()
        if combat_id and combat_id in active_combats:
            del active_combats[combat_id]
