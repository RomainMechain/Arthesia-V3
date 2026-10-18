"""Règles communes de progression, statistiques et conditions du jeu.

Ce module contient uniquement des calculs et des lectures de templates. Il ne
sauvegarde pas directement le joueur : les routes et services appelants restent
responsables de persister les changements dans Firestore.

Les fonctions sont utilisées par plusieurs domaines du serveur, notamment les
actions REST, les déblocages et le combat WebSocket. Les constantes ci-dessous
forment donc le point central des formules de base du gameplay.
"""

import random
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

try:
    from .content_store import load_ordered_floors, load_static_zone_content, load_template
    from .player_store import get_zone_dynamic_state
    from .balance import (
        BASE_STATS, HP_REGEN_FULL_SECONDS, POINT_VALUE, STAT_POINTS_PER_LEVEL, TOWN_REGEN_MULTIPLIER, kill_xp,
        level_gap_xp_multiplier, profession_xp_to_next_level, xp_to_next_level,
    )
except ImportError:
    from content_store import load_ordered_floors, load_static_zone_content, load_template
    from player_store import get_zone_dynamic_state
    from balance import (
        BASE_STATS, HP_REGEN_FULL_SECONDS, POINT_VALUE, STAT_POINTS_PER_LEVEL, TOWN_REGEN_MULTIPLIER, kill_xp,
        level_gap_xp_multiplier, profession_xp_to_next_level, xp_to_next_level,
    )



def roll_loot_table(loot_table: list[dict]) -> tuple[str, int] | None:
    """Tire au plus une récompense dans une table de loot pondérée.

    ``weight`` détermine la probabilité relative de chaque entrée. Une fois
    l'entrée choisie, la quantité est tirée uniformément entre
    ``quantity_min`` et ``quantity_max``. Une table vide ne donne aucune
    récompense et retourne ``None``.
    """
    if not loot_table:
        return None

    # Le poids total sert à définir un intervalle continu dans lequel chaque
    # entrée occupe une portion proportionnelle à son poids.
    total_weight = sum(entry["weight"] for entry in loot_table)
    roll = random.uniform(0, total_weight)
    cumulative = 0
    for entry in loot_table:
        cumulative += entry["weight"]
        if roll <= cumulative:
            # Le tirage de quantité intervient seulement après le choix de
            # l'item, afin que le poids ne soit pas influencé par la quantité.
            quantity = random.randint(entry["quantity_min"], entry["quantity_max"])
            return entry["item_id"], quantity

    # Ce cas ne devrait pas arriver avec des poids positifs, mais garder un
    # retour explicite rend la fonction sûre si une table mal formée est reçue.
    return None


# Délai de réapparition par défaut d'une entry partagée épuisée.
DEFAULT_RESPAWN_SECONDS = 60

# Niveau d'un métier jamais pratiqué. Les ressources demandent au minimum le
# niveau 1 : un niveau de départ à 0 empêcherait toute première récolte.
STARTING_PROFESSION_LEVEL = 1


def get_profession(player: dict, profession_id: str) -> dict:
    """Retourne l'état d'un métier du joueur, sans le créer s'il est absent."""
    return player["professions"].get(profession_id, {"level": STARTING_PROFESSION_LEVEL, "xp": 0})


def resolve_entry_state(entry: dict, raw_state: dict | None) -> dict:
    """Retourne l'état courant d'une entry partagée, réapparition appliquée.

    La réapparition est évaluée à la demande : une entry épuisée dont le délai
    est écoulé redevient disponible, avec son stock complet si elle en a un.
    """
    state = dict(raw_state or {"status": "available"})
    if state.get("status") == "depleted":
        available_again_at = state.get("available_again_at")
        if available_again_at and datetime.now(timezone.utc) >= available_again_at:
            state = {"status": "available"}
    max_stock = entry.get("spawn", {}).get("stock")
    if state.get("status") == "available" and max_stock and "current_stock" not in state:
        state["current_stock"] = max_stock
    return state


def public_entry_state(entry: dict, state: dict) -> dict:
    """Version envoyée au client : statut, stock et secondes avant réapparition."""
    result = {"status": state.get("status", "available")}
    max_stock = entry.get("spawn", {}).get("stock")
    if max_stock:
        result["max_stock"] = max_stock
        result["current_stock"] = state.get("current_stock", 0) if result["status"] == "available" else 0
    available_again_at = state.get("available_again_at")
    if available_again_at:
        remaining = (available_again_at - datetime.now(timezone.utc)).total_seconds()
        result["seconds_until_available"] = max(0, round(remaining, 1))
    return result


def deplete_entry_state(entry: dict) -> dict:
    """État d'une entry partagée qui vient d'être épuisée."""
    return {
        "status": "depleted",
        "available_again_at": datetime.now(timezone.utc) + timedelta(seconds=respawn_cooldown_seconds(entry)),
    }


def respawn_cooldown_seconds(entry: dict) -> float:
    """Retourne le délai de réapparition d'une entry de zone.

    Les modes de spawn autres que ``respawn_timer`` (``conditional`` par
    exemple) écrivent ``cooldown_seconds: null`` : la clé existe, donc
    ``dict.get`` avec une valeur par défaut renverrait ``None``.
    """
    return entry.get("spawn", {}).get("cooldown_seconds") or DEFAULT_RESPAWN_SECONDS


def compute_kill_xp(monster_level: int, role: str, player_level: int) -> int:
    """XP d'un monstre : son niveau et son rôle (``balance.kill_xp``), puis
    l'écart de niveau avec le joueur (moins d'XP pour un monstre plus faible,
    plus pour un monstre plus fort)."""
    return max(1, round(kill_xp(monster_level, role) * level_gap_xp_multiplier(monster_level, player_level)))


def apply_profession_level_ups(profession: dict) -> None:
    """Consomme l'XP en attente d'une profession et applique ses niveaux.

    Une boucle est nécessaire car une grosse récompense peut faire franchir
    plusieurs seuils en une seule action. L'XP restante est conservée après
    chaque montée de niveau.
    """
    while profession["xp"] >= profession_xp_to_next_level(profession["level"]):
        profession["xp"] -= profession_xp_to_next_level(profession["level"])
        profession["level"] += 1


def apply_player_level_ups(player: dict) -> int:
    """Applique les niveaux gagnés par le joueur et retourne leur nombre.

    Chaque niveau ajoute six points de statistiques disponibles. L'XP
    excédentaire reste dans ``player['xp']`` pour le seuil suivant.
    """
    levels_gained = 0
    while player["xp"] >= xp_to_next_level(player["level"]):
        player["xp"] -= xp_to_next_level(player["level"])
        player["level"] += 1
        player["stat_points"]["available"] += STAT_POINTS_PER_LEVEL
        levels_gained += 1
    return levels_gained


# -----------------------------------------------------------------------------
# Points de vie hors combat
#
# Les PV perdus en combat sont conservés et remontent avec le temps.
# ``player["health"]`` vaut {"current": PV, "at": horodatage} ; absent ou
# ``current`` à None, le joueur est en pleine forme. La valeur courante est
# calculée à la lecture (comme le respawn des entries) : rien n'est écrit
# pendant la régénération.
# -----------------------------------------------------------------------------

def zone_type(zone_id: str | None) -> str | None:
    for floor in load_ordered_floors():
        for zone in floor.get("zones", []):
            if zone["id"] == zone_id:
                return zone.get("type")
    return None


def hp_regen_per_second(player: dict, max_hp: float) -> float:
    rate = max_hp / HP_REGEN_FULL_SECONDS
    return rate * TOWN_REGEN_MULTIPLIER if zone_type(player.get("current_zone_id")) == "town" else rate


def current_hp(player: dict, max_hp: float, now: datetime | None = None) -> float:
    """PV actuels hors combat, régénération depuis la dernière mise à jour
    comprise. Le taux est celui de la zone actuelle : changer de zone fige
    d'abord les PV (``settle_hp``)."""
    health = player.get("health") or {}
    if health.get("current") is None:
        return float(max_hp)
    now = now or datetime.now(timezone.utc)
    elapsed = max(0.0, (now - health["at"]).total_seconds()) if health.get("at") else 0.0
    return round(min(float(max_hp), health["current"] + elapsed * hp_regen_per_second(player, max_hp)), 1)


def set_hp(player: dict, hp: float, max_hp: float) -> None:
    """Enregistre les PV actuels ; à PV pleins, l'état redevient « en forme »."""
    if hp >= max_hp:
        player["health"] = {"current": None, "at": None}
    else:
        player["health"] = {"current": round(max(hp, 0.0), 1), "at": datetime.now(timezone.utc)}


def settle_hp(player: dict) -> None:
    """Fige la régénération acquise, avant un changement de taux (voyage)."""
    max_hp = compute_final_stats(player)["hp"]
    set_hp(player, current_hp(player, max_hp), max_hp)


def health_view(player: dict) -> dict:
    """PV pour le client : actuels, max et vitesse de régénération."""
    max_hp = compute_final_stats(player)["hp"]
    hp = current_hp(player, max_hp)
    regen = hp_regen_per_second(player, max_hp)
    return {
        "current": hp,
        "max": max_hp,
        "regen_per_second": round(regen, 3),
        "full_in_seconds": round((max_hp - hp) / regen) if regen > 0 and hp < max_hp else 0,
    }


def is_floor_boss(zone_id: str, monster: dict) -> bool:
    """Boss de la zone ``boss_zone_id`` d'un étage."""
    if monster.get("role") != "boss":
        return False
    return any(floor.get("boss_zone_id") == zone_id for floor in load_ordered_floors())


def is_shared_entry(zone_id: str, entry: dict, monster: dict | None = None) -> bool:
    """Indique si l'état d'une entry est commun à tous les joueurs.

    Un boss d'étage est toujours partagé, quel que soit son ``state_scope`` :
    vaincu une fois, il l'est pour tout le serveur, ouvre le passage pour tout
    le monde et ne peut plus être combattu.
    """
    if entry.get("state_scope") == "shared":
        return True
    return monster is not None and is_floor_boss(zone_id, monster)


def is_one_time_per_player(zone_id: str, entry: dict, monster: dict) -> bool:
    """Entry ``per_player`` qu'un joueur ne bat qu'une fois : un boss (autre
    qu'un boss d'étage), ou un spawn ``unique_per_player``. Sa défaite est
    notée sur le profil du joueur."""
    if is_shared_entry(zone_id, entry, monster):
        return False
    return monster.get("role") == "boss" or entry.get("spawn", {}).get("mode") == "unique_per_player"


def defeated_entry_key(zone_id: str, entry_id: str) -> str:
    return f"{zone_id}/{entry_id}"


def has_defeated_entry(player: dict, zone_id: str, entry_id: str) -> bool:
    return defeated_entry_key(zone_id, entry_id) in player.get("defeated_entries", [])


def is_boss_defeated(zone_id: str, player: dict | None = None) -> bool:
    """Indique si un boss de la zone est vaincu.

    Un boss ``shared`` est vaincu pour tout le serveur (état de zone Firestore).
    Un boss ``per_player`` l'est pour chaque joueur qui l'a battu (profil du
    joueur). Une zone ou un contenu introuvable est traité comme non vaincu,
    ce qui évite de débloquer une condition sur une donnée invalide.
    """
    try:
        zone_static = load_static_zone_content(zone_id)
    except HTTPException:
        return False
    zone_state = get_zone_dynamic_state(zone_id)
    for entry in zone_static.get("entries", []):
        if entry["template_type"] != "monster":
            continue
        # Plusieurs monstres peuvent être présents dans une zone : seul un
        # template marqué role="boss" peut satisfaire cette condition.
        monster = load_template("monster", entry["template_id"])
        if monster.get("role") == "boss":
            entry_state = zone_state["entries"].get(entry["id"], {})
            if entry_state.get("status") == "defeated":
                return True
            if player is not None and has_defeated_entry(player, zone_id, entry["id"]):
                return True
    return False


def evaluate_condition(condition: dict | None, player: dict) -> bool:
    """Évalue récursivement une condition de déblocage pour un joueur.

    Les conditions feuilles consultent directement l'état joueur : niveau,
    quête, inventaire, zone débloquée ou compteurs de progression. Une
    condition ``group`` combine ses enfants avec ``all`` ou ``any`` et réutilise
    cette même fonction pour les niveaux imbriqués.

    Une condition absente est considérée comme satisfaite ; un type inconnu est
    refusé par défaut pour éviter un déblocage involontaire.
    """
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
        return is_boss_defeated(condition["zone_id"], player)
    if condition_type == "kill_count":
        return player["lifetime_counters"]["kills"].get(condition["monster_id"], 0) >= condition["count"]
    if condition_type == "gather_count":
        return player["lifetime_counters"]["gathers"].get(condition["resource_id"], 0) >= condition["count"]
    if condition_type == "deliver_item":
        return player["lifetime_counters"]["deliveries"].get(condition["item_id"], 0) >= condition["count"]
    if condition_type == "group":
        # L'appel récursif permet de composer des groupes de groupes sans
        # multiplier les branches spécifiques dans cette fonction.
        results = [evaluate_condition(item, player) for item in condition["conditions"]]
        return all(results) if condition["mode"] == "all" else any(results)
    return False


def is_zone_accessible(zone: dict, player: dict) -> bool:
    """Une zone est accessible si elle n'a pas de condition, ou si sa condition
    est remplie (évaluée en direct, pas seulement via ``unlocks.zones``)."""
    if zone.get("unlock_condition") is None or zone["id"] in player["unlocks"]["zones"]:
        return True
    return evaluate_condition(zone["unlock_condition"], player)


def reachable_floor_ids(floors: list[dict], player: dict) -> list[str]:
    """Étages où le joueur peut se rendre, dans l'ordre.

    Le premier étage est toujours accessible ; l'étage suivant le devient quand
    la zone ``passage`` de l'étage courant est accessible (son déblocage suit la
    défaite du boss de l'étage).
    """
    reachable = []
    for index, floor in enumerate(floors):
        if index > 0:
            passages = [z for z in floors[index - 1].get("zones", []) if z.get("type") == "passage"]
            if not any(is_zone_accessible(z, player) for z in passages):
                break
        reachable.append(floor["id"])
    return reachable


def require_player_in_zone(player: dict, zone_id: str) -> None:
    """Refuse une action faite depuis une autre zone que celle du joueur."""
    if player.get("current_zone_id") != zone_id:
        raise HTTPException(403, "Tu dois être dans cette zone pour faire ça")


def is_entry_unlocked(entry: dict, player: dict) -> bool:
    """Indique si une entry de zone est accessible au joueur.

    Seul le mode de spawn ``conditional`` porte une condition ; les autres
    modes sont toujours accessibles.
    """
    spawn = entry.get("spawn", {})
    if spawn.get("mode") != "conditional":
        return True
    return evaluate_condition(spawn.get("condition"), player)


def require_entry_unlocked(entry: dict, player: dict) -> None:
    """Refuse l'accès à une entry dont la condition d'apparition n'est pas remplie."""
    if not is_entry_unlocked(entry, player):
        raise HTTPException(403, "Pas encore accessible : une condition n'est pas remplie")


def quest_progress_view(quest_def: dict, progress: dict, player: dict) -> dict:
    """Progression lisible d'une quête : objectifs (actuel / requis) et ``ready``.

    Les objectifs ``kill_count`` et ``gather_count`` utilisent les compteurs de
    la quête ; ``deliver_item`` compte les objets présents dans l'inventaire ;
    ``level`` et ``zone`` se lisent sur l'état du joueur.
    ``ready`` vaut vrai quand la quête active peut être rendue.
    """
    counters = progress.get("objectives_progress", [])
    objectives = []
    for index, objective in enumerate(quest_def.get("objectives", [])):
        required = objective.get("count", 1)
        if objective["type"] == "deliver_item":
            current = player["inventory"].get(objective["item_id"], 0)
        elif objective["type"] == "level":
            required = objective.get("min", 1)
            current = player.get("level", 1)
        elif objective["type"] == "zone":
            current = 1 if objective.get("zone_id") in player["unlocks"]["zones"] else 0
        else:
            current = counters[index]["current_count"] if index < len(counters) else 0
        objectives.append({"current": min(current, required), "count": required, "done": current >= required})
    ready = progress.get("state") == "active" and all(objective["done"] for objective in objectives)
    return {"state": progress.get("state"), "objectives": objectives, "ready": ready}


def advance_quest_objectives(player: dict, objective_type: str, target_key: str, target_id: str) -> list[dict]:
    """Fait avancer les objectifs ``kill_count``/``gather_count`` des quêtes actives.

    Retourne, pour chaque objectif qui a avancé, la quête, l'objectif et la
    nouvelle valeur, ainsi que ``quest_ready`` si la quête peut être rendue.
    """
    updates = []
    for quest_id, progress in player["quests"].items():
        if progress.get("state") != "active":
            continue
        quest_def = load_template("quest", quest_id)
        for index, objective in enumerate(quest_def["objectives"]):
            if objective["type"] != objective_type or objective[target_key] != target_id:
                continue
            counter = progress["objectives_progress"][index]
            if counter["current_count"] >= objective["count"]:
                continue
            counter["current_count"] += 1
            updates.append({
                "quest_id": quest_id,
                "quest_name": quest_def.get("name", quest_id),
                "objective_index": index,
                "current": counter["current_count"],
                "count": objective["count"],
                "quest_ready": quest_progress_view(quest_def, progress, player)["ready"],
            })
    return updates


def _accumulate_stat_effects(effects: list[dict], stats: dict, percent_bonuses: dict) -> None:
    """Ajoute les effets permanents d'un template aux statistiques accumulées.

    Les bonus fixes sont appliqués immédiatement. Les bonus en pourcentage sont
    mis de côté pour être appliqués après tous les bonus fixes, afin que leur
    base de calcul soit complète.
    """
    for effect in effects:
        if effect["type"] == "stat_bonus":
            stats[effect["stat"]] = stats.get(effect["stat"], 0) + effect["amount"]
        elif effect["type"] == "stat_bonus_percent":
            percent_bonuses[effect["stat"]] = percent_bonuses.get(effect["stat"], 0) + effect["amount_percent"]


def compute_final_stats(player: dict) -> dict:
    """Calcule les statistiques finales persistantes d'un joueur.

    L'ordre du calcul est volontaire :

    1. statistiques de base ;
    2. points dépensés ;
    3. bonus fixes de l'équipement ;
    4. bonus fixes et pourcentages des passifs permanents ;
    5. bonus fixes et pourcentages du titre équipé ;
    6. application finale des pourcentages.

    Les passifs conditionnels ne sont pas intégrés ici : ils dépendent du
    contexte du combat et doivent être traités par le moteur temps réel.
    """
    # On copie les valeurs de base pour ne jamais modifier le dictionnaire
    # global BASE_STATS partagé entre les joueurs.
    stats = dict(BASE_STATS)
    for stat, points in player["stat_points"]["spent"].items():
        # Les points dépensés sont convertis selon POINT_VALUE.
        stats[stat] += points * POINT_VALUE[stat]

    for item_id in player["equipment"].values():
        if not item_id:
            continue
        # Un slot vide ne déclenche aucune lecture de fichier. Les bonus sont
        # lus depuis la définition complète de l'item équipé.
        item = load_template("item", item_id)
        for stat, amount in item.get("equipment", {}).get("stat_bonuses", {}).items():
            stats[stat] = stats.get(stat, 0) + amount

    percent_bonuses: dict = {}
    for skill_id in player["skills"]["equipped_passive"]:
        skill = load_template("skill", skill_id)
        if skill.get("trigger") == "always":
            # Seuls les passifs permanents participent aux stats hors combat.
            _accumulate_stat_effects(skill.get("effects", []), stats, percent_bonuses)

    if player["titles"]["equipped"]:
        # Le titre est traité comme une source supplémentaire d'effets, après
        # l'équipement et les passifs.
        title = load_template("title", player["titles"]["equipped"])
        _accumulate_stat_effects(title.get("effects", []), stats, percent_bonuses)

    for stat, percent in percent_bonuses.items():
        # Les pourcentages s'appliquent en dernier sur le total fixe complet.
        stats[stat] = round(stats.get(stat, 0) * (1 + percent / 100), 1)
    return stats
