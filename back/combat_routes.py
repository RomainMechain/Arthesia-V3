"""WebSocket du combat temps réel.

Le combat est volontairement maintenu en mémoire dans ``active_combats`` :
les ticks fréquents ne doivent pas écrire dans Firestore. Firestore n'est
sollicité que pour lire l'état du joueur au début, pour retirer un
consommable utilisé, et pour enregistrer la récompense finale via
``apply_monster_kill_rewards``. Les règles elles-mêmes (stats effectives,
effets, boucliers, passifs, phases) sont dans ``combat_engine.py``.

Le protocole WebSocket repose sur quatre messages clients :

* ``start_combat`` initialise une session ;
* ``basic_attack`` applique une attaque avec cooldown ;
* ``use_skill`` consomme du mana et applique les effets de la compétence ;
* ``use_item`` utilise un consommable du sac (recharge commune).

Les messages serveur sont des événements JSON tels que ``combat_started``,
``monster_attack``, ``player_basic_attack``, ``player_skill_used``,
``player_item_used``, ``passive_triggered``, ``monster_phase``, ``status``,
``combat_won`` et ``combat_lost``. Chaque événement de combat porte un champ
``status`` (bouclier, effets actifs, cadences) décrit par
``combat_engine.status``.
"""

import asyncio
import traceback
import uuid

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from firebase_admin import firestore

try:
    from . import combat_engine as engine
    from .auth import AUTH_REQUIRED, check_player, verify_id_token
    from .content_store import find_entry, load_static_zone_content, load_template
    from .database import db
    from .game_rules import (
        compute_final_stats, has_defeated_entry, is_entry_unlocked, is_one_time_per_player, is_shared_entry,
        resolve_entry_state,
    )
    from .player_store import get_player, get_zone_dynamic_state, save_player
    from .progression import apply_monster_kill_rewards
except ImportError:
    import combat_engine as engine
    from auth import AUTH_REQUIRED, check_player, verify_id_token
    from content_store import find_entry, load_static_zone_content, load_template
    from database import db
    from game_rules import (
        compute_final_stats, has_defeated_entry, is_entry_unlocked, is_one_time_per_player, is_shared_entry,
        resolve_entry_state,
    )
    from player_store import get_player, get_zone_dynamic_state, save_player
    from progression import apply_monster_kill_rewards

router = APIRouter()
active_combats: dict[str, dict] = {}


async def send_event(websocket: WebSocket, session: dict, event: dict) -> None:
    """Envoie un événement de combat accompagné de l'état courant des effets."""
    session["last_status"] = engine.status_signature(session)
    await websocket.send_json({**event, "status": engine.status(session)})


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


async def after_player_action(websocket: WebSocket, combat_id: str, session: dict) -> None:
    """Suites communes d'une action du joueur : changement de phase du
    monstre, passifs déclenchés, puis victoire si le monstre est à terre."""
    if session["monster_current_hp"] <= 0:
        # La session est supprimée avant la récompense pour empêcher qu'une
        # seconde commande ne gagne deux fois le même combat.
        active_combats.pop(combat_id, None)
        await send_victory(websocket, session)
        return
    phase = engine.check_phase_change(session)
    if phase:
        await send_event(websocket, session, {"type": "monster_phase", **phase})
    for passive in engine.trigger_passives(session):
        await send_event(websocket, session, {"type": "passive_triggered", **passive})


async def combat_tick_loop(websocket: WebSocket, combat_id: str):
    """Exécute la boucle autonome d'une session de combat.

    À chaque tick, la fonction :

    1. récupère la session encore active ;
    2. retire les effets expirés et régénère le mana du joueur ;
    3. fait avancer les recharges du monstre et l'attaque si possible ;
    4. déclenche les passifs conditionnels devenus actifs ;
    5. termine le combat si les points de vie du joueur atteignent zéro.

    La tâche s'arrête quand la session est supprimée après une victoire, une
    défaite ou une déconnexion. Les exceptions sont journalisées ici afin de
    ne pas laisser une tâche asyncio silencieuse.
    """
    try:
        while combat_id in active_combats:
            await asyncio.sleep(engine.TICK_INTERVAL_SECONDS)
            session = active_combats.get(combat_id)
            if session is None:
                break

            engine.expire_effects(session)
            engine.regen_mana(session, engine.TICK_INTERVAL_SECONDS)

            attack = engine.monster_tick(session)
            if attack:
                await send_event(websocket, session, {
                    "type": "monster_attack",
                    **attack,
                    "player_hp": session["player_current_hp"],
                    "player_max_hp": session["player_max_hp"],
                    "player_mana": session["player_current_mana"],
                })
                if session["player_current_hp"] <= 0:
                    await websocket.send_json({"type": "combat_lost"})
                    # Retirer la session empêche la boucle de repartir au tick suivant.
                    active_combats.pop(combat_id, None)
                    break

            for passive in engine.trigger_passives(session):
                await send_event(websocket, session, {"type": "passive_triggered", **passive})

            # Un effet a expiré ou un passif s'est activé sans autre événement :
            # le client reçoit un état à jour.
            if engine.status_signature(session) != session.get("last_status"):
                await send_event(websocket, session, {
                    "type": "status",
                    "player_hp": session["player_current_hp"],
                    "player_mana": session["player_current_mana"],
                })
    except Exception:
        traceback.print_exc()


@firestore.transactional
def _consume_item_tx(transaction, player_id: str, item_id: str) -> int:
    """Retire un exemplaire d'un consommable du sac ; renvoie le reste."""
    player = get_player(player_id, transaction)
    owned = player["inventory"].get(item_id, 0)
    if owned < 1:
        raise HTTPException(400, "Tu n'as plus cet objet")
    player["inventory"][item_id] = owned - 1
    if player["inventory"][item_id] <= 0:
        del player["inventory"][item_id]
    save_player(player_id, player, transaction)
    return owned - 1


async def start_combat(websocket: WebSocket, message: dict) -> tuple[str, dict] | None:
    """Valide et crée une session de combat ; renvoie (combat_id, session)."""
    player_id = message.get("player_id", "")
    zone_id = message.get("zone_id", "")
    entry_id = message.get("entry_id", "")
    try:
        if AUTH_REQUIRED:
            # Le WebSocket n'a pas d'en-tête par message : le jeton voyage
            # dans start_combat.
            check_player(verify_id_token(message.get("id_token", "")), player_id)
        entry = find_entry(load_static_zone_content(zone_id), entry_id)
    except HTTPException as exc:
        await websocket.send_json({"type": "error", "message": exc.detail})
        return None
    if entry["template_type"] != "monster":
        await websocket.send_json({"type": "error", "message": "Cette entry n'est pas un monstre"})
        return None
    monster = load_template("monster", entry["template_id"])
    # Le joueur est lu une seule fois : ses stats et ses compétences
    # équipées restent figées pour toute la durée du combat.
    player = get_player(player_id)
    if player.get("current_zone_id") != zone_id:
        await websocket.send_json({"type": "error", "message": "Tu dois être dans cette zone pour combattre"})
        return None
    if not is_entry_unlocked(entry, player):
        await websocket.send_json({"type": "error", "message": "Ce monstre n'est pas encore accessible"})
        return None
    if is_one_time_per_player(zone_id, entry, monster) and has_defeated_entry(player, zone_id, entry_id):
        await websocket.send_json({"type": "error", "message": "Tu as déjà vaincu ce monstre"})
        return None
    if is_shared_entry(zone_id, entry, monster):
        # Inutile de combattre un monstre partagé déjà tué ou un boss d'étage
        # déjà vaincu : la victoire ne rapporterait rien.
        state = resolve_entry_state(entry, get_zone_dynamic_state(zone_id)["entries"].get(entry_id))
        if state.get("status") == "defeated":
            await websocket.send_json({"type": "error", "message": "Ce gardien a déjà été vaincu"})
            return None
        if state.get("status") != "available":
            await websocket.send_json({"type": "error", "message": "Ce monstre n'est pas là pour l'instant"})
            return None

    # Les passifs permanents sont déjà dans compute_final_stats ; les passifs
    # conditionnels sont évalués en direct pendant le combat.
    conditional_passives = []
    for skill_id in player["skills"].get("equipped_passive", []):
        skill = load_template("skill", skill_id)
        if skill.get("trigger") == "conditional":
            conditional_passives.append(skill)

    session = engine.new_session(
        player_id, zone_id, entry_id, monster, compute_final_stats(player),
        player["skills"]["equipped_active"], conditional_passives,
    )
    combat_id = str(uuid.uuid4())
    active_combats[combat_id] = session
    phase_count = len(monster.get("phases") or [])
    await send_event(websocket, session, {
        "type": "combat_started",
        "combat_id": combat_id,
        "monster_name": monster["name"],
        "monster_hp": session["monster_max_hp"],
        "monster_level": monster.get("level", 1),
        "phase": session["phase_index"] + 1 if session["phase_index"] is not None else None,
        "phase_count": phase_count or None,
        "player_hp": session["player_max_hp"],
        "player_mana": session["player_max_mana"],
        # Le client anime la régénération et les recharges entre deux
        # messages ; chaque événement resynchronise les valeurs exactes.
        "player_mana_regen": round(engine.compute_mana_regen(engine.player_stat(session, "sag")), 2),
        "basic_attack_cooldown": round(engine.compute_basic_attack_cooldown(engine.player_stat(session, "agi")), 2),
        "item_cooldown": engine.ITEM_COOLDOWN_SECONDS,
    })
    return combat_id, session


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
            action = message.get("action")

            if action == "start_combat":
                started = await start_combat(websocket, message)
                if started:
                    combat_id, _session = started
                    tick_task = asyncio.create_task(combat_tick_loop(websocket, combat_id))
                continue

            session = active_combats.get(combat_id)
            if session is None:
                await websocket.send_json({"type": "error", "message": "Aucun combat en cours"})
                continue
            t = engine.now()

            if action == "basic_attack":
                # L'attaque basique est limitée par un timestamp monotonic :
                # celui-ci n'est pas affecté par les changements d'heure système.
                if t < session["player_basic_attack_ready_at"]:
                    await websocket.send_json({"type": "error", "message": "Attaque pas encore prête"})
                    continue
                raw_damage = engine.BASIC_ATTACK_POWER * engine.player_stat(session, "atk")
                final_damage = engine.damage_monster(session, raw_damage)
                cooldown = engine.compute_basic_attack_cooldown(engine.player_stat(session, "agi"))
                session["player_basic_attack_ready_at"] = t + cooldown
                await send_event(websocket, session, {
                    "type": "player_basic_attack", "raw_damage": round(raw_damage, 1),
                    "final_damage": final_damage, "monster_hp": session["monster_current_hp"],
                    "monster_max_hp": session["monster_max_hp"], "next_ready_in": round(cooldown, 2),
                })
                await after_player_action(websocket, combat_id, session)

            elif action == "use_skill":
                # Une compétence doit être équipée, disponible et abordable en
                # mana avant que ses effets ne modifient la session.
                skill_id = message.get("skill_id", "")
                if skill_id not in session["equipped_active"]:
                    await websocket.send_json({"type": "error", "message": "Compétence non équipée"})
                    continue
                skill = load_template("skill", skill_id)
                cooldowns = session["player_skill_cooldowns"]
                if t < cooldowns.get(skill_id, 0):
                    await websocket.send_json({"type": "error", "message": "Compétence pas encore prête"})
                    continue
                if session["player_current_mana"] < skill["mana_cost"]:
                    await websocket.send_json({"type": "error", "message": "Pas assez de mana"})
                    continue
                session["player_current_mana"] -= skill["mana_cost"]
                cooldowns[skill_id] = t + skill["cooldown_seconds"]
                results = engine.apply_skill_effects(session, skill)
                await send_event(websocket, session, {
                    "type": "player_skill_used", "skill_id": skill_id, "results": results,
                    "monster_hp": session["monster_current_hp"], "player_hp": session["player_current_hp"],
                    "player_mana": session["player_current_mana"],
                    "next_ready_in": round(skill["cooldown_seconds"], 2),
                })
                await after_player_action(websocket, combat_id, session)

            elif action == "use_item":
                # Un consommable est retiré du sac tout de suite (une écriture
                # par utilisation, jamais pendant les ticks).
                item_id = message.get("item_id", "")
                if t < session["item_ready_at"]:
                    await websocket.send_json({"type": "error", "message": "Objet pas encore utilisable"})
                    continue
                try:
                    item = load_template("item", item_id)
                except HTTPException:
                    await websocket.send_json({"type": "error", "message": "Objet inconnu"})
                    continue
                if not engine.is_usable_consumable(item):
                    await websocket.send_json({"type": "error", "message": "Cet objet ne s'utilise pas en combat"})
                    continue
                refusal = engine.consumable_refusal(session, item)
                if refusal:
                    await websocket.send_json({"type": "error", "message": refusal})
                    continue
                try:
                    remaining = _consume_item_tx(db.transaction(), session["player_id"], item_id)
                except HTTPException as exc:
                    await websocket.send_json({"type": "error", "message": exc.detail})
                    continue
                session["item_ready_at"] = t + engine.ITEM_COOLDOWN_SECONDS
                results = engine.apply_consumable(session, item)
                await send_event(websocket, session, {
                    "type": "player_item_used", "item_id": item_id, "item_name": item.get("name", item_id),
                    "remaining": remaining, "results": results,
                    "player_hp": session["player_current_hp"], "player_mana": session["player_current_mana"],
                    "next_ready_in": engine.ITEM_COOLDOWN_SECONDS,
                })
                await after_player_action(websocket, combat_id, session)

            else:
                await websocket.send_json({"type": "error", "message": "Action inconnue"})
    except WebSocketDisconnect:
        # Une fermeture réseau doit libérer la tâche et la session en mémoire.
        if tick_task:
            tick_task.cancel()
        if combat_id and combat_id in active_combats:
            del active_combats[combat_id]
