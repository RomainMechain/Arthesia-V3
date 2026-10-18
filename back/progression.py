"""Progression des joueurs : déblocages et récompenses de combat.

Ce module regroupe les opérations qui font progresser durablement un joueur :

* vérifier les conditions et enregistrer les nouveaux déblocages ;
* appliquer les récompenses d'une victoire contre un monstre ;
* mettre à jour les compteurs utilisés par les quêtes et conditions.

Le contenu statique est lu depuis les fichiers locaux. L'état évolutif du joueur
et des rencontres partagées est lu et écrit dans Firestore.
"""

import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from firebase_admin import firestore

try:
    from .config import TEMPLATES_DIR
    from .content_store import find_entry, load_all_floor_skeletons, load_static_zone_content, load_template
    from .database import db
    from .game_rules import (
        advance_quest_objectives, apply_player_level_ups, compute_kill_xp, defeated_entry_key, evaluate_condition,
        has_defeated_entry, is_one_time_per_player, is_shared_entry, respawn_cooldown_seconds, roll_loot_table,
    )
    from .player_store import get_player, get_zone_dynamic_state, save_player, save_zone_dynamic_state
except ImportError:
    from config import TEMPLATES_DIR
    from content_store import find_entry, load_all_floor_skeletons, load_static_zone_content, load_template
    from database import db
    from game_rules import (
        advance_quest_objectives, apply_player_level_ups, compute_kill_xp, defeated_entry_key, evaluate_condition,
        has_defeated_entry, is_one_time_per_player, is_shared_entry, respawn_cooldown_seconds, roll_loot_table,
    )
    from player_store import get_player, get_zone_dynamic_state, save_player, save_zone_dynamic_state


def check_and_apply_unlocks(player_id: str) -> list[str]:
    """Évalue toutes les conditions de déblocage connues pour un joueur.

    Les zones sont lues depuis les squelettes d'étage, puis les compétences et
    titres sont parcourus dans leurs dossiers de templates. Une condition est
    évaluée avec l'état courant du joueur ; chaque nouvel élément est ajouté à
    la fois dans sa liste détaillée et dans ``unlocks``, utilisée comme index
    rapide par le reste du serveur.

    La fonction est volontairement idempotente : un élément déjà débloqué est
    ignoré et un joueur sans nouveau déblocage ne provoque aucune écriture.
    Elle retourne des identifiants préfixés, par exemple ``zone:rivelune`` ou
    ``skill:dard_de_libellule``, afin d'informer directement le client.

    La lecture et l'écriture du joueur se font dans une transaction : une autre
    action concurrente ne peut pas être écrasée par cette sauvegarde.
    """
    return _check_and_apply_unlocks_tx(db.transaction(), player_id)


@firestore.transactional
def _check_and_apply_unlocks_tx(transaction, player_id: str) -> list[str]:
    player = get_player(player_id, transaction)
    newly_unlocked = []

    # Les conditions de zones vivent dans les squelettes, indépendamment des
    # fichiers de contenu détaillé générés pour chaque zone.
    for skeleton in load_all_floor_skeletons():
        for zone in skeleton.get("zones", []):
            if zone["id"] in player["unlocks"]["zones"]:
                continue
            # evaluate_condition prend en charge les conditions simples et les
            # groupes récursifs (niveau, quête, item, boss, etc.).
            if evaluate_condition(zone.get("unlock_condition"), player):
                player["unlocks"]["zones"].append(zone["id"])
                newly_unlocked.append(f"zone:{zone['id']}")

    # Les compétences et titres suivent la même règle de déblocage, mais leur
    # source est un dossier de templates plutôt qu'un squelette d'étage.
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
                # On conserve deux représentations : la liste spécialisée du
                # joueur et l'index générique des déblocages.
                player[player_key]["unlocked"].append(template["id"])
                player["unlocks"][unlock_key].append(template["id"])
                newly_unlocked.append(f"{unlock_key[:-1]}:{template['id']}")

    # Une seule écriture est faite après avoir évalué toutes les catégories.
    if newly_unlocked:
        save_player(player_id, player, transaction)
    return newly_unlocked


def apply_monster_kill_rewards(player_id: str, zone_id: str, entry_id: str) -> dict:
    """Valide une victoire et applique toutes ses conséquences persistantes.

    Le traitement suit cet ordre :

    1. charger la zone, l'entry et le template du monstre ;
    2. réserver ou épuiser l'entry si son état est partagé ;
    3. tirer le loot et ajouter l'XP au joueur ;
    4. mettre à jour les compteurs de kills et les quêtes actives ;
    5. sauvegarder le joueur puis recalculer ses déblocages.

    Pour un monstre normal, l'entry passe en respawn. Pour un boss, elle passe
    en état ``defeated`` et ne redevient pas disponible automatiquement.
    """
    # La zone et l'entry proviennent du contenu statique local. Cela permet de
    # vérifier que le client n'a pas envoyé un identifiant inventé.
    zone_static = load_static_zone_content(zone_id)
    entry = find_entry(zone_static, entry_id)
    if entry["template_type"] != "monster":
        raise HTTPException(400, "Cette entry n'est pas un monstre")
    monster = load_template("monster", entry["template_id"])

    # La réservation du spawn et les récompenses sont écrites dans la même
    # transaction : si deux victoires arrivent en même temps, Firestore rejoue
    # la seconde, qui voit alors le spawn déjà épuisé.
    result = _apply_monster_kill_rewards_tx(db.transaction(), player_id, zone_id, entry_id, entry, monster)
    result["newly_unlocked"] = check_and_apply_unlocks(player_id)
    return result


@firestore.transactional
def _apply_monster_kill_rewards_tx(transaction, player_id: str, zone_id: str, entry_id: str, entry: dict, monster: dict) -> dict:
    # Firestore impose de faire toutes les lectures avant la première écriture.
    shared = is_shared_entry(zone_id, entry, monster)
    zone_state = get_zone_dynamic_state(zone_id, transaction) if shared else None
    player = get_player(player_id, transaction)

    if shared:
        # Les monstres partagés doivent être réservés avant les récompenses :
        # deux appels concurrents ne doivent pas pouvoir payer deux fois le même
        # spawn déjà vaincu.
        entry_state = zone_state["entries"].get(entry_id, {"status": "available"})
        if entry_state.get("status") == "depleted":
            # Le respawn est vérifié à la demande, en comparant l'horodatage
            # Firestore à l'heure UTC actuelle.
            available_again_at = entry_state.get("available_again_at")
            if available_again_at and datetime.now(timezone.utc) >= available_again_at:
                entry_state = {"status": "available"}
        if entry_state.get("status") != "available":
            raise HTTPException(409, "Monstre indisponible (déjà tué/vaincu, respawn en cours ou permanent)")
        if monster.get("role") == "boss":
            # Un boss vaincu reste définitivement indisponible dans cet état.
            entry_state = {"status": "defeated"}
        else:
            # Les monstres ordinaires sont épuisés pendant leur cooldown.
            cooldown = respawn_cooldown_seconds(entry)
            entry_state = {
                "status": "depleted",
                "available_again_at": datetime.now(timezone.utc) + timedelta(seconds=cooldown),
            }
        zone_state["entries"][entry_id] = entry_state
        save_zone_dynamic_state(zone_id, zone_state, transaction)
    elif is_one_time_per_player(zone_id, entry, monster):
        # Un boss propre à chaque joueur ne se bat qu'une fois : sa défaite est
        # notée sur le profil (elle ouvre notamment le passage de l'étage).
        if has_defeated_entry(player, zone_id, entry_id):
            raise HTTPException(409, "Tu as déjà vaincu ce monstre")
        player.setdefault("defeated_entries", []).append(defeated_entry_key(zone_id, entry_id))

    # Une fois la rencontre validée, on applique les récompenses sur l'état
    # courant du joueur, lu plus haut dans la transaction.
    loot = roll_loot_table(monster.get("loot_table", []))
    if loot:
        # roll_loot_table renvoie au plus un item et une quantité pour ce kill.
        item_id, quantity = loot
        player["inventory"][item_id] = player["inventory"].get(item_id, 0) + quantity

    xp_gained = compute_kill_xp(monster["level"], monster.get("role", "normal"))
    player["xp"] += xp_gained
    # La montée de niveau peut augmenter les points de statistiques disponibles.
    levels_gained = apply_player_level_ups(player)
    player["lifetime_counters"]["kills"][entry["template_id"]] = player["lifetime_counters"]["kills"].get(entry["template_id"], 0) + 1

    # Seules les quêtes actives avancent ; les autres restent inchangées.
    quest_progress = advance_quest_objectives(player, "kill_count", "monster_id", entry["template_id"])

    # Le joueur est sauvegardé avant le recalcul des déblocages (fait après le
    # commit) afin que leurs conditions lisent bien les nouveaux XP, compteurs
    # et items.
    save_player(player_id, player, transaction)
    return {
        "loot": loot,
        "xp_gagne": xp_gained,
        "levels_gained": levels_gained,
        "player_level": player["level"],
        "player_xp_total": player["xp"],
        "player_stat_points": player["stat_points"],
        "player_inventory": player["inventory"],
        "quest_progress": quest_progress,
    }
