"""WebSocket et règles du combat temps réel.

Le combat est volontairement maintenu en mémoire dans ``active_combats`` :
les ticks fréquents ne doivent pas écrire dans Firestore. Firestore n'est
sollicité que pour lire l'état du joueur et pour enregistrer la récompense
finale via ``apply_monster_kill_rewards``.

Le protocole WebSocket repose sur trois messages clients :

* ``start_combat`` initialise une session ;
* ``basic_attack`` applique une attaque avec cooldown ;
* ``use_skill`` consomme du mana et applique les effets pris en charge.

Les messages serveur sont des événements JSON tels que ``combat_started``,
``monster_attack``, ``player_basic_attack``, ``player_skill_used``,
``combat_won`` et ``combat_lost``.
"""

import asyncio
import random
import time
import traceback
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

# Le tick est partagé par la régénération de mana et les attaques automatiques
# du monstre. Les constantes ci-dessous centralisent les formules provisoires
# du modèle de combat.
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
    """Convertit l'agilité en délai entre deux attaques basiques.

    Plus l'agilité augmente, plus le délai diminue, sans tomber à zéro :
    ``BASE_ATTACK_COOLDOWN`` reste le délai de référence.
    """
    return BASE_ATTACK_COOLDOWN / (1 + agi * K_AGI)


def compute_max_mana(int_stat: float) -> float:
    """Calcule le mana maximal à partir de l'intelligence du joueur."""
    return BASE_MANA + int_stat * K_INT


def compute_mana_regen(sag_stat: float) -> float:
    """Calcule la régénération de mana par seconde à partir de la sagesse."""
    return BASE_MANA_REGEN + sag_stat * K_SAG


def compute_mitigation(defense: float) -> float:
    """Retourne la part des dégâts absorbée par la défense.

    La formule asymptotique ``def / (def + K_DEF)`` évite qu'une défense
    élevée rende les dégâts totalement nuls.
    """
    return defense / (defense + K_DEF)


async def send_victory(websocket: WebSocket, session: dict) -> None:
    """Applique les récompenses d'une victoire et en informe le client.

    Un refus métier (spawn déjà vaincu...) donne ``combat_won_no_reward``.
    Une erreur inattendue est journalisée et signalée au client sans fermer
    la connexion : le joueur peut relancer un combat.
    """
    try:
        rewards = apply_monster_kill_rewards(session["player_id"], session["zone_id"], session["entry_id"])
    except HTTPException as exc:
        await websocket.send_json({"type": "combat_won_no_reward", "reason": exc.detail})
        return
    except Exception:
        traceback.print_exc()
        await websocket.send_json({
            "type": "combat_won_no_reward",
            "reason": "Erreur serveur pendant l'attribution des récompenses.",
        })
        return
    await websocket.send_json({"type": "combat_won", **rewards})


async def combat_tick_loop(websocket: WebSocket, combat_id: str):
    """Exécute la boucle autonome d'une session de combat.

    À chaque tick, la fonction :

    1. récupère la session encore active ;
    2. régénère le mana du joueur ;
    3. réduit les cooldowns des compétences du monstre ;
    4. choisit aléatoirement une compétence prête, pondérée par ``weight`` ;
    5. applique les dégâts et diffuse l'événement au client ;
    6. termine le combat si les points de vie du joueur atteignent zéro.

    La tâche s'arrête quand la session est supprimée après une victoire, une
    défaite ou une déconnexion. Les exceptions sont journalisées ici afin de
    ne pas laisser une tâche asyncio silencieuse.
    """
    try:
        while combat_id in active_combats:
            await asyncio.sleep(TICK_INTERVAL_SECONDS)
            session = active_combats.get(combat_id)
            if session is None:
                break

            # Les statistiques sont figées au début du combat : aucun accès
            # Firestore n'a lieu pendant les ticks.
            player_stats = session["player_stats"]
            regen = compute_mana_regen(player_stats.get("sag", 0)) * TICK_INTERVAL_SECONDS
            session["player_current_mana"] = min(
                session["player_max_mana"], session["player_current_mana"] + regen
            )

            monster = load_template("monster", session["monster_template_id"])
            moveset = monster.get("moveset", [])
            cooldowns = session["monster_cooldowns"]

            # Chaque compétence possède son propre cooldown. La valeur stockée
            # représente le nombre de secondes restantes avant disponibilité.
            for skill in moveset:
                sid = skill["id"]
                cooldowns[sid] = max(0.0, cooldowns.get(sid, 0.0) - TICK_INTERVAL_SECONDS)

            ready_skills = [skill for skill in moveset if cooldowns.get(skill["id"], 0.0) <= 0]
            if not ready_skills:
                continue

            # Le poids permet au template du monstre de contrôler la fréquence
            # relative de ses attaques sans coder cette préférence ici.
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
                # Retirer la session empêche la boucle de repartir au tick suivant.
                del active_combats[combat_id]
                break
    except Exception as exc:
        print(f"⚠️  Erreur dans la boucle de combat {combat_id} : {exc}")


@router.websocket("/ws/combat")
async def combat_websocket(websocket: WebSocket):
    """Gère les commandes et événements d'une session WebSocket de combat.

    Une connexion ne peut contrôler qu'un combat courant identifié par
    ``combat_id``. La boucle de tick tourne en parallèle de cette fonction,
    tandis que les messages entrants traitent les actions du joueur.
    """
    await websocket.accept()
    combat_id = None
    tick_task = None
    try:
        while True:
            message = await websocket.receive_json()
            if message.get("action") == "start_combat":
                # Le client fournit une zone et une entry ; on vérifie que cette
                # entry correspond bien à un monstre avant de créer la session.
                player_id = message["player_id"]
                zone_id = message["zone_id"]
                entry_id = message["entry_id"]
                zone_static = load_static_zone_content(zone_id)
                entry = find_entry(zone_static, entry_id)
                if entry["template_type"] != "monster":
                    await websocket.send_json({"type": "error", "message": "Cette entry n'est pas un monstre"})
                    continue
                monster = load_template("monster", entry["template_id"])
                # Le joueur est lu une seule fois : ses stats et ses compétences
                # équipées restent figées pour toute la durée du combat.
                player = get_player(player_id)
                player_stats = compute_final_stats(player)
                player_hp = player_stats["hp"]
                player_max_mana = compute_max_mana(player_stats.get("int", 0))
                combat_id = str(uuid.uuid4())

                # Tout l'état volatile du combat reste en mémoire. Les valeurs
                # initiales sont envoyées au client dans l'événement suivant.
                active_combats[combat_id] = {
                    "player_id": player_id, "zone_id": zone_id, "entry_id": entry_id,
                    "monster_template_id": entry["template_id"],
                    "monster_current_hp": monster["stats"]["hp"], "monster_max_hp": monster["stats"]["hp"],
                    "monster_cooldowns": {}, "player_current_hp": player_hp, "player_max_hp": player_hp,
                    "player_basic_attack_ready_at": time.monotonic(),
                    "player_current_mana": player_max_mana, "player_max_mana": player_max_mana,
                    "player_skill_cooldowns": {},
                    "player_stats": player_stats,
                    "equipped_active": list(player["skills"]["equipped_active"]),
                }
                await websocket.send_json({
                    "type": "combat_started", "combat_id": combat_id,
                    "monster_name": monster["name"], "monster_hp": monster["stats"]["hp"],
                    "player_hp": player_hp, "player_mana": player_max_mana,
                    "monster_level": monster.get("level", 1),
                    # Le client anime la régénération entre deux messages du
                    # serveur ; chaque événement resynchronise la valeur exacte.
                    "player_mana_regen": round(compute_mana_regen(player_stats.get("sag", 0)), 2),
                    "basic_attack_cooldown": round(compute_basic_attack_cooldown(player_stats.get("agi", 0)), 2),
                })
                tick_task = asyncio.create_task(combat_tick_loop(websocket, combat_id))

            elif message.get("action") == "basic_attack":
                # L'attaque basique est limitée par un timestamp monotonic :
                # celui-ci n'est pas affecté par les changements d'heure système.
                session = active_combats.get(combat_id)
                if session is None:
                    await websocket.send_json({"type": "error", "message": "Aucun combat en cours"})
                    continue
                now = time.monotonic()
                if now < session["player_basic_attack_ready_at"]:
                    await websocket.send_json({"type": "error", "message": "Attaque pas encore prête"})
                    continue
                player_stats = session["player_stats"]
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
                    # La session est supprimée avant la récompense pour empêcher
                    # qu'une seconde commande ne gagne deux fois le même combat.
                    del active_combats[combat_id]
                    await send_victory(websocket, session)

            elif message.get("action") == "use_skill":
                # Une compétence doit être équipée, disponible et abordable en
                # mana avant que ses effets ne modifient la session.
                skill_id = message["skill_id"]
                session = active_combats.get(combat_id)
                if session is None:
                    await websocket.send_json({"type": "error", "message": "Aucun combat en cours"})
                    continue
                if skill_id not in session["equipped_active"]:
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
                player_stats = session["player_stats"]
                monster = load_template("monster", session["monster_template_id"])
                results = []
                for effect in skill.get("effects", []):
                    effect_type = effect["type"]
                    if effect_type == "damage":
                        # Les dégâts magiques utilisent l'intelligence ; les
                        # dégâts physiques utilisent l'attaque.
                        stat = "int" if skill.get("damage_type") == "magical" else "atk"
                        raw = effect["power"] * player_stats.get(stat, 0)
                        final = round(raw * (1 - compute_mitigation(monster["stats"].get("def", 0))), 1)
                        session["monster_current_hp"] = max(0.0, session["monster_current_hp"] - final)
                        results.append({"effect": "damage", "final_damage": final})
                    elif effect_type == "heal":
                        # Les soins ne peuvent pas dépasser les PV maximums de
                        # la session, même si l'effet est très puissant.
                        raw = round(effect["power"] * player_stats.get("int", 0), 1)
                        session["player_current_hp"] = min(session["player_max_hp"], session["player_current_hp"] + raw)
                        results.append({"effect": "heal", "amount": raw})
                    else:
                        # Les effets sont renvoyés au client pour rester visibles,
                        # mais leur mécanique complète n'est pas encore implémentée.
                        results.append({"effect": effect_type, "note": "pas encore implémenté mécaniquement"})
                await websocket.send_json({
                    "type": "player_skill_used", "skill_id": skill_id, "results": results,
                    "monster_hp": session["monster_current_hp"], "player_hp": session["player_current_hp"],
                    "player_mana": session["player_current_mana"],
                    "next_ready_in": round(skill["cooldown_seconds"], 2),
                })
                if session["monster_current_hp"] <= 0:
                    del active_combats[combat_id]
                    await send_victory(websocket, session)
            else:
                await websocket.send_json({"type": "error", "message": "Action pas encore gérée à cette étape"})
    except WebSocketDisconnect:
        # Une fermeture réseau doit libérer la tâche et la session en mémoire.
        if tick_task:
            tick_task.cancel()
        if combat_id and combat_id in active_combats:
            del active_combats[combat_id]
