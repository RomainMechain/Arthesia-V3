"""Mécanique pure du combat temps réel.

Ce module ne touche ni au réseau ni à Firestore : il modifie la session de
combat (un dictionnaire en mémoire) et renvoie ce qu'il s'est passé. Les
routes WebSocket (``combat_routes.py``) s'occupent des messages et de la
persistance.

Les statistiques effectives combinent les stats figées au début du combat et
les effets temporaires :

* buffs du joueur (compétences, consommables) et passifs conditionnels ;
* debuffs appliqués au monstre ;
* bouclier du joueur, qui absorbe les dégâts avant les PV.

Les durées sont mesurées avec ``time.monotonic()``.
"""

import random
import time

# Le tick est partagé par la régénération de mana et les attaques automatiques
# du monstre. Les constantes ci-dessous centralisent les formules provisoires
# du modèle de combat (voir arthesia_balance_reference.md).
TICK_INTERVAL_SECONDS = 0.5
# La défense et l'agilité se comparent à une référence qui grandit avec le
# niveau : sans cela, les stats linéaires rendraient les hauts niveaux
# quasi invulnérables et ultra-rapides. Les constantes sont proportionnelles
# aux stats du joueur de référence (DEF ≈ 5 + 4,5 × niveau, AGI ≈ 5 + 1,4 ×
# niveau) : le joueur de référence garde ~38 % de réduction et ~1,35 s par
# attaque à tous les niveaux, et l'équilibrage calibré ne dérive pas.
K_DEF = 6.6
K_DEF_PER_LEVEL = 6
BASE_ATTACK_COOLDOWN = 2.0
K_AGI = 7.4
K_AGI_PER_LEVEL = 2
# L'attaque basique est un appoint : les compétences font l'essentiel des
# dégâts, ce qui rend le mana (INT, SAG) utile à tous les builds. Elle
# utilise la meilleure stat offensive (ATK ou INT) pour que les mages en aient
# une aussi.
BASIC_ATTACK_POWER = 0.4
# Le mana grandit avec le niveau pour tous : un guerrier peut aussi lancer
# ses compétences. INT et SAG en ajoutent par-dessus.
BASE_MANA = 50
MANA_PER_LEVEL = 10
K_INT = 2
BASE_MANA_REGEN = 1
MANA_REGEN_PER_LEVEL = 0.2
K_SAG = 0.05
# Une compétence hybride s'appuie sur ATK et INT à la fois : sa stat vaut
# HYBRID_STAT_FACTOR × la plus faible des deux. Un build qui investit autant
# dans l'une que dans l'autre en profite pleinement ; pour un spécialiste, elle
# reste toujours sous sa meilleure stat, donc ses compétences mono-stat restent
# meilleures (et la calibration des monstres n'en dépend pas).
HYBRID_STAT_FACTOR = 2.0
# Recharge commune à tous les consommables utilisés en combat.
ITEM_COOLDOWN_SECONDS = 5.0
# Attaques télégraphiées : le monstre annonce une grosse attaque pendant
# ``telegraph_seconds`` avant de frapper. Sa parade (``avoidable_by``) est soit
# l'esquive (bouton dédié, avec sa recharge : deux annonces rapprochées ne
# s'esquivent pas toutes les deux), soit l'interruption (une compétence de
# dégâts lancée pendant l'annonce). Une esquive hors annonce est gaspillée.
DODGE_COOLDOWN_SECONDS = 8.0
COUNTERS = {"esquive", "interruption"}

# Chaque coup de dégâts varie de ±DAMAGE_VARIANCE autour de sa valeur.
DAMAGE_VARIANCE = 0.08
# Critique et esquive dépendent de l'écart d'AGI entre attaquant et défenseur
# (agility_edge, entre -1 et 1) : chance = BASE + K × écart, donc entre 0 et
# BASE + K (20 % de critique, 15 % d'esquive au plus). À AGI égale : 5 %.
BASE_CRIT_CHANCE = 0.05
K_CRIT = 0.15
CRIT_MULTIPLIER = 1.5
BASE_DODGE_CHANCE = 0.05
K_DODGE = 0.10


class CombatRefusal(Exception):
    """Action du joueur refusée (recharge, mana...) ; le message est pour le joueur."""

STAT_LABELS = {"hp": "PV", "atk": "ATK", "def": "DEF", "agi": "AGI", "int": "INT", "sag": "SAG"}


def now() -> float:
    return time.monotonic()


# -----------------------------------------------------------------------------
# Formules de base
# -----------------------------------------------------------------------------

def compute_basic_attack_cooldown(agi: float, level: int) -> float:
    """Convertit l'agilité en délai entre deux attaques basiques.

    La vitesse vaut 1 + agi / (agi + K_AGI + K_AGI_PER_LEVEL × niveau) : entre
    1 et 2, le délai reste donc entre ``BASE_ATTACK_COOLDOWN`` et sa moitié.
    """
    agi = max(agi, 0)
    return BASE_ATTACK_COOLDOWN / (1 + agi / (agi + K_AGI + K_AGI_PER_LEVEL * max(level, 1)))


def compute_max_mana(int_stat: float, level: int) -> float:
    """Mana maximal : une part liée au niveau, plus l'intelligence."""
    return BASE_MANA + MANA_PER_LEVEL * max(level, 1) + max(int_stat, 0) * K_INT


def compute_mana_regen(sag_stat: float, level: int) -> float:
    """Régénération de mana par seconde : une part liée au niveau, plus la
    sagesse."""
    return BASE_MANA_REGEN + MANA_REGEN_PER_LEVEL * max(level, 1) + max(sag_stat, 0) * K_SAG


def agility_edge(attacker_agi: float, defender_agi: float) -> float:
    """Avantage d'agilité de l'attaquant, entre -1 et 1. Relatif, il garde le
    même sens à tous les niveaux."""
    total = max(attacker_agi, 0) + max(defender_agi, 0)
    return (max(attacker_agi, 0) - max(defender_agi, 0)) / total if total > 0 else 0.0


def crit_chance(attacker_agi: float, defender_agi: float) -> float:
    edge = agility_edge(attacker_agi, defender_agi)
    return max(0.0, BASE_CRIT_CHANCE + K_CRIT * edge)


def dodge_chance(attacker_agi: float, defender_agi: float) -> float:
    edge = agility_edge(defender_agi, attacker_agi)
    return max(0.0, BASE_DODGE_CHANCE + K_DODGE * edge)


def roll_hit(raw_damage: float, attacker_agi: float, defender_agi: float) -> dict:
    """Tire l'issue d'un coup avant la défense : esquive, critique et
    variation aléatoire."""
    if random.random() < dodge_chance(attacker_agi, defender_agi):
        return {"damage": 0.0, "critical": False, "dodged": True}
    damage = raw_damage * random.uniform(1 - DAMAGE_VARIANCE, 1 + DAMAGE_VARIANCE)
    critical = random.random() < crit_chance(attacker_agi, defender_agi)
    if critical:
        damage *= CRIT_MULTIPLIER
    return {"damage": damage, "critical": critical, "dodged": False}


def compute_mitigation(defense: float, attacker_level: int) -> float:
    """Retourne la part des dégâts absorbée par la défense.

    ``def / (def + K_DEF + K_DEF_PER_LEVEL × niveau de l'attaquant)`` : la
    défense n'annule jamais tous les dégâts, et un attaquant de plus haut
    niveau la perce mieux.
    """
    defense = max(defense, 0)
    return defense / (defense + K_DEF + K_DEF_PER_LEVEL * max(attacker_level, 1))


# -----------------------------------------------------------------------------
# Session
# -----------------------------------------------------------------------------

def new_session(player_id: str, zone_id: str, entry_id: str, monster: dict, player_stats: dict,
                equipped_active: list[str], conditional_passives: list[dict],
                skills: dict[str, dict] | None = None, player_level: int = 1,
                player_current_hp: float | None = None) -> dict:
    """Crée l'état en mémoire d'un combat. ``skills`` donne les templates des
    compétences actives équipées, par identifiant ; ``player_current_hp`` les
    PV du joueur en début de combat (pleins par défaut)."""
    max_mana = compute_max_mana(player_stats.get("int", 0), player_level)
    return {
        "player_id": player_id,
        "zone_id": zone_id,
        "entry_id": entry_id,
        "monster": monster,
        "monster_template_id": monster["id"],
        "monster_current_hp": monster["stats"]["hp"],
        "monster_max_hp": monster["stats"]["hp"],
        "monster_cooldowns": {},
        "monster_effects": [],
        "phase_index": current_phase_index(monster, 1.0),
        "player_stats": player_stats,
        "player_level": player_level,
        "player_current_hp": player_stats["hp"] if player_current_hp is None else min(player_current_hp, player_stats["hp"]),
        "player_max_hp": player_stats["hp"],
        "player_current_mana": max_mana,
        "player_max_mana": max_mana,
        "player_effects": [],
        "player_shield": 0.0,
        "player_shield_expires_at": None,
        "player_basic_attack_ready_at": now(),
        "player_dodge_ready_at": now(),
        # Attaque télégraphiée en cours d'annonce : {move, resolves_at, dodged}.
        "monster_pending": None,
        "player_skill_cooldowns": {},
        "item_ready_at": now(),
        "equipped_active": list(equipped_active),
        "skills": dict(skills or {}),
        "conditional_passives": conditional_passives,
        # Passifs dont l'effet ponctuel (bouclier) a déjà été déclenché.
        "triggered_passives": [],
    }


# -----------------------------------------------------------------------------
# Effets temporaires et statistiques effectives
# -----------------------------------------------------------------------------

def add_effect(effects: list[dict], source: str, label: str, stat: str, duration: float,
               percent: float = 0.0, flat: float = 0.0) -> dict:
    """Ajoute un effet temporaire. Relancer la même source rafraîchit l'effet
    au lieu de le cumuler."""
    effects[:] = [e for e in effects if not (e["source"] == source and e["stat"] == stat)]
    effect = {
        "source": source,
        "label": label,
        "stat": stat,
        "percent": percent,
        "flat": flat,
        "expires_at": now() + duration,
        "duration": duration,
    }
    effects.append(effect)
    return effect


def expire_effects(session: dict) -> bool:
    """Retire les effets et le bouclier expirés. Renvoie vrai si quelque chose
    a changé."""
    t = now()
    changed = False
    for key in ("player_effects", "monster_effects"):
        kept = [e for e in session[key] if e["expires_at"] > t]
        changed = changed or len(kept) != len(session[key])
        session[key] = kept
    expires_at = session["player_shield_expires_at"]
    if session["player_shield"] > 0 and expires_at is not None and expires_at <= t:
        session["player_shield"] = 0.0
        session["player_shield_expires_at"] = None
        changed = True
    return changed


def _modified(base: float, effects: list[dict], stat: str) -> float:
    percent = sum(e["percent"] for e in effects if e["stat"] == stat)
    flat = sum(e["flat"] for e in effects if e["stat"] == stat)
    return max(0.0, (base + flat) * (1 + percent / 100))


def player_stat(session: dict, stat: str) -> float:
    """Stat effective du joueur : base du combat, buffs et passifs actifs."""
    effects = session["player_effects"] + active_passive_effects(session)
    return _modified(session["player_stats"].get(stat, 0), effects, stat)


def monster_stat(session: dict, stat: str) -> float:
    """Stat effective du monstre, debuffs compris."""
    return _modified(session["monster"]["stats"].get(stat, 0), session["monster_effects"], stat)


# -----------------------------------------------------------------------------
# Passifs conditionnels
# -----------------------------------------------------------------------------

def evaluate_combat_condition(condition: dict | None, session: dict) -> bool:
    """Conditions « temps réel combat » du modèle de données."""
    if not condition:
        return True
    hp_percent = session["player_current_hp"] / max(session["player_max_hp"], 1) * 100
    mana_percent = session["player_current_mana"] / max(session["player_max_mana"], 1) * 100
    condition_type = condition.get("type")
    if condition_type == "hp_below_percent":
        return hp_percent < condition["value"]
    if condition_type == "hp_above_percent":
        return hp_percent > condition["value"]
    if condition_type == "mana_below_percent":
        return mana_percent < condition["value"]
    if condition_type == "group":
        results = [evaluate_combat_condition(c, session) for c in condition.get("conditions", [])]
        return all(results) if condition.get("mode") == "all" else any(results)
    return False


def active_passive_effects(session: dict) -> list[dict]:
    """Bonus de stats des passifs conditionnels dont la condition est remplie.
    Ils durent tant que la condition reste vraie."""
    effects = []
    for skill in session["conditional_passives"]:
        if not evaluate_combat_condition(skill.get("condition"), session):
            continue
        for effect in skill.get("effects", []):
            if effect["type"] == "stat_bonus":
                effects.append({"stat": effect["stat"], "percent": 0.0, "flat": effect["amount"]})
            elif effect["type"] == "stat_bonus_percent":
                effects.append({"stat": effect["stat"], "percent": effect["amount_percent"], "flat": 0.0})
    return effects


def trigger_passives(session: dict) -> list[dict]:
    """Déclenche les effets ponctuels des passifs conditionnels (bouclier) au
    moment où leur condition devient vraie, une fois par combat."""
    triggered = []
    for skill in session["conditional_passives"]:
        if skill["id"] in session["triggered_passives"]:
            continue
        if not evaluate_combat_condition(skill.get("condition"), session):
            continue
        shields = [e for e in skill.get("effects", []) if e["type"] == "shield"]
        if not shields:
            continue
        session["triggered_passives"].append(skill["id"])
        for effect in shields:
            amount = apply_shield(session, skill, effect)
            triggered.append({"skill_id": skill["id"], "skill_name": skill.get("name", skill["id"]), "effect": "shield", "amount": amount})
    return triggered


# -----------------------------------------------------------------------------
# Dégâts, soins, boucliers
# -----------------------------------------------------------------------------

def damage_monster(session: dict, raw_damage: float) -> dict:
    """Frappe le monstre : esquive, critique, variation, puis sa défense."""
    hit = roll_hit(raw_damage, player_stat(session, "agi"), monster_stat(session, "agi"))
    final = round(hit["damage"] * (1 - compute_mitigation(monster_stat(session, "def"), session["player_level"])), 1)
    session["monster_current_hp"] = max(0.0, round(session["monster_current_hp"] - final, 1))
    return {"final_damage": final, "critical": hit["critical"], "dodged": hit["dodged"]}


def damage_player(session: dict, raw_damage: float) -> dict:
    """Frappe le joueur : esquive, critique, variation, défense, puis
    bouclier et PV."""
    hit = roll_hit(raw_damage, monster_stat(session, "agi"), player_stat(session, "agi"))
    mitigated = round(hit["damage"] * (1 - compute_mitigation(player_stat(session, "def"), session["monster"].get("level", 1))), 1)
    absorbed = min(session["player_shield"], mitigated)
    session["player_shield"] = round(session["player_shield"] - absorbed, 1)
    if session["player_shield"] <= 0:
        session["player_shield"] = 0.0
        session["player_shield_expires_at"] = None
    final = round(mitigated - absorbed, 1)
    session["player_current_hp"] = max(0.0, round(session["player_current_hp"] - final, 1))
    return {"final_damage": final, "absorbed": round(absorbed, 1), "critical": hit["critical"], "dodged": hit["dodged"]}


def heal_player(session: dict, amount: float) -> float:
    """Soigne le joueur sans dépasser ses PV max ; renvoie les PV réellement rendus."""
    before = session["player_current_hp"]
    session["player_current_hp"] = min(session["player_max_hp"], round(before + amount, 1))
    return round(session["player_current_hp"] - before, 1)


def apply_shield(session: dict, skill: dict, effect: dict) -> float:
    """Bouclier : ``base`` + ``amount_power`` × SAG (× ATK pour une
    compétence physique)."""
    stat = "atk" if skill.get("damage_type") == "physical" else "sag"
    amount = round(effect.get("base", 0) + effect.get("amount_power", 0) * player_stat(session, stat), 1)
    session["player_shield"] = round(max(session["player_shield"], amount), 1)
    duration = effect.get("duration_seconds")
    session["player_shield_expires_at"] = now() + duration if duration else None
    return amount


def skill_damage_stat(session: dict, skill: dict) -> float:
    """Stat des dégâts d'une compétence : ATK (physique), INT (magique) ou
    HYBRID_STAT_FACTOR × la plus faible des deux (hybride)."""
    damage_type = skill.get("damage_type")
    if damage_type == "hybrid":
        return HYBRID_STAT_FACTOR * min(player_stat(session, "atk"), player_stat(session, "int"))
    return player_stat(session, "int" if damage_type == "magical" else "atk")


def apply_skill_effects(session: dict, skill: dict) -> list[dict]:
    """Applique les effets d'une compétence active et décrit le résultat.

    Dégâts et soins valent ``base`` + ``power`` × stat : la base est fixée au
    niveau de la compétence, donc une compétence ancienne perd peu à peu de
    son importance face aux nouvelles."""
    results = []
    for effect in skill.get("effects", []):
        effect_type = effect["type"]
        if effect_type == "damage":
            raw = effect.get("base", 0) + effect.get("power", 0) * skill_damage_stat(session, skill)
            results.append({"effect": "damage", **damage_monster(session, raw)})
        elif effect_type == "heal":
            # Les soins dépendent de la SAG : l'INT fait les dégâts magiques.
            amount = heal_player(session, effect.get("base", 0) + effect.get("power", 0) * player_stat(session, "sag"))
            results.append({"effect": "heal", "amount": amount})
        elif effect_type == "shield":
            results.append({"effect": "shield", "amount": apply_shield(session, skill, effect)})
        elif effect_type in ("buff", "debuff"):
            # Un buff vise le joueur, un debuff le monstre (sauf cible explicite).
            target_self = effect.get("target", "self" if effect_type == "buff" else "enemy") == "self"
            percent = effect.get("amount_percent", 0) * (1 if effect_type == "buff" else -1)
            effects = session["player_effects"] if target_self else session["monster_effects"]
            add_effect(effects, skill["id"], skill.get("name", skill["id"]), effect["stat"],
                       effect.get("duration_seconds", 0), percent=percent)
            results.append({
                "effect": effect_type,
                "target": "self" if target_self else "enemy",
                "stat": effect["stat"],
                "percent": percent,
                "duration": effect.get("duration_seconds", 0),
            })
    return results


def apply_consumable(session: dict, item: dict) -> list[dict]:
    """Applique l'effet d'un consommable utilisé en combat."""
    consumable = item.get("consumable", {})
    effect_type = consumable.get("effect_type")
    amount = consumable.get("amount", 0)
    if effect_type == "heal":
        return [{"effect": "heal", "amount": heal_player(session, amount)}]
    if effect_type == "restore_mana":
        before = session["player_current_mana"]
        session["player_current_mana"] = min(session["player_max_mana"], before + amount)
        return [{"effect": "mana", "amount": round(session["player_current_mana"] - before, 1)}]
    if effect_type == "buff_stat":
        add_effect(session["player_effects"], item["id"], item.get("name", item["id"]), consumable.get("stat", "atk"),
                   consumable.get("duration_seconds", 0), flat=amount)
        return [{"effect": "buff", "target": "self", "stat": consumable.get("stat"), "flat": amount,
                 "duration": consumable.get("duration_seconds", 0)}]
    return []


def is_usable_consumable(item: dict) -> bool:
    return item.get("category") == "consumable" and item.get("consumable", {}).get("effect_type") in ("heal", "restore_mana", "buff_stat")


def consumable_refusal(session: dict, item: dict) -> str | None:
    """Raison de ne pas utiliser un consommable maintenant (il serait gâché),
    ou ``None`` s'il peut servir."""
    effect_type = item.get("consumable", {}).get("effect_type")
    if effect_type == "heal" and session["player_current_hp"] >= session["player_max_hp"]:
        return "Tes PV sont déjà au maximum"
    if effect_type == "restore_mana" and session["player_current_mana"] >= session["player_max_mana"]:
        return "Ton mana est déjà au maximum"
    return None


# -----------------------------------------------------------------------------
# Monstre : phases et attaques
# -----------------------------------------------------------------------------

def current_phase_index(monster: dict, hp_ratio: float) -> int | None:
    """Phase active d'un boss : la première dont le seuil de PV est dépassé.
    ``None`` pour un monstre sans phases."""
    phases = monster.get("phases") or []
    for index, phase in enumerate(phases):
        if hp_ratio * 100 > phase.get("hp_above_percent", 0):
            return index
    return len(phases) - 1 if phases else None


def check_phase_change(session: dict) -> dict | None:
    """Met à jour la phase du monstre après des dégâts ; renvoie la nouvelle
    phase si elle a changé."""
    monster = session["monster"]
    index = current_phase_index(monster, session["monster_current_hp"] / max(session["monster_max_hp"], 1))
    if index is None or index == session["phase_index"]:
        return None
    session["phase_index"] = index
    phase = monster["phases"][index]
    return {"phase": index + 1, "phase_count": len(monster["phases"]), "moveset_ids": phase.get("moveset_ids", [])}


def available_moveset(session: dict) -> list[dict]:
    moveset = session["monster"].get("moveset", [])
    if session["phase_index"] is None:
        return moveset
    allowed = session["monster"]["phases"][session["phase_index"]].get("moveset_ids", [])
    return [move for move in moveset if move["id"] in allowed] or moveset


def monster_tick(session: dict) -> dict | None:
    """Fait avancer d'un tick les recharges du monstre et l'attaque si une
    compétence est prête.

    Renvoie l'attaque réalisée, l'annonce d'une attaque télégraphiée
    (``{"telegraph": True, ...}``) ou ``None``. Pendant une annonce, le
    monstre ne lance rien d'autre ; l'attaque frappe quand l'annonce se
    termine, sauf si elle a été esquivée ou interrompue.
    """
    # Un debuff d'agilité ralentit la recharge des attaques du monstre.
    speed = max(0.2, 1 + sum(e["percent"] for e in session["monster_effects"] if e["stat"] == "agi") / 100)
    cooldowns = session["monster_cooldowns"]
    moveset = available_moveset(session)
    for move in moveset:
        cooldowns[move["id"]] = max(0.0, cooldowns.get(move["id"], 0.0) - TICK_INTERVAL_SECONDS * speed)

    pending = session["monster_pending"]
    if pending is not None:
        if now() < pending["resolves_at"]:
            return None
        return resolve_telegraph(session)

    ready = [move for move in moveset if cooldowns.get(move["id"], 0.0) <= 0]
    if not ready:
        return None
    # Le poids permet au template du monstre de contrôler la fréquence
    # relative de ses attaques sans coder cette préférence ici.
    chosen = random.choices(ready, weights=[move.get("weight", 1) for move in ready], k=1)[0]
    cooldowns[chosen["id"]] = chosen["cooldown_seconds"]
    if chosen.get("telegraph_seconds", 0) > 0:
        session["monster_pending"] = {
            "move": chosen,
            "resolves_at": now() + chosen["telegraph_seconds"],
            "dodged": False,
        }
        return {"telegraph": True, **pending_view(session)}
    raw = monster_move_damage(session, chosen)
    result = damage_player(session, raw)
    return {"skill_id": chosen["id"], "raw_damage": round(raw, 1), **result}


def counter_of(move: dict) -> str | None:
    """Parade d'une attaque télégraphiée : « esquive », « interruption » ou
    ``None`` (inévitable : l'annonce sert à se préparer)."""
    counter = move.get("avoidable_by")
    return counter if counter in COUNTERS else None


def pending_view(session: dict) -> dict | None:
    pending = session["monster_pending"]
    if pending is None:
        return None
    move = pending["move"]
    return {
        "skill_id": move["id"],
        "skill_name": move.get("name", move["id"]),
        "avoidable_by": counter_of(move),
        "seconds": move.get("telegraph_seconds", 0),
        "remaining": round(max(0.0, pending["resolves_at"] - now()), 2),
        "dodged": pending["dodged"],
    }


def resolve_telegraph(session: dict) -> dict:
    """Fin de l'annonce : l'attaque frappe, sauf si elle a été esquivée."""
    pending = session["monster_pending"]
    session["monster_pending"] = None
    move = pending["move"]
    if pending["dodged"]:
        return {"skill_id": move["id"], "telegraphed": True, "avoided": "esquive", "raw_damage": 0.0,
                "final_damage": 0.0, "absorbed": 0.0, "critical": False, "dodged": True}
    raw = monster_move_damage(session, move)
    return {"skill_id": move["id"], "telegraphed": True, "avoided": None, "raw_damage": round(raw, 1),
            **damage_player(session, raw)}


def monster_move_damage(session: dict, move: dict) -> float:
    """Dégâts bruts d'une attaque du monstre : ``power`` × son ATK (INT si
    l'attaque est magique), comme les compétences du joueur. Un debuff de la
    stat réduit donc l'attaque. Les anciens templates à dégâts fixes
    (``damage``) restent lus, réduits par un debuff d'ATK."""
    if "power" in move:
        stat = "int" if move.get("damage_type") == "magical" else "atk"
        return move["power"] * monster_stat(session, stat)
    attack_factor = max(0.0, 1 + sum(e["percent"] for e in session["monster_effects"] if e["stat"] == "atk") / 100)
    return move.get("damage", 0) * attack_factor


def regen_mana(session: dict, seconds: float) -> None:
    regen = player_mana_regen(session) * seconds
    session["player_current_mana"] = min(session["player_max_mana"], session["player_current_mana"] + regen)


# -----------------------------------------------------------------------------
# Actions du joueur et déroulement
# -----------------------------------------------------------------------------

def player_mana_regen(session: dict) -> float:
    return compute_mana_regen(player_stat(session, "sag"), session["player_level"])


def player_attack_cooldown(session: dict) -> float:
    return compute_basic_attack_cooldown(player_stat(session, "agi"), session["player_level"])


def basic_attack(session: dict) -> dict:
    """Attaque basique du joueur ; lève CombatRefusal si elle n'est pas prête."""
    t = now()
    if t < session["player_basic_attack_ready_at"]:
        raise CombatRefusal("Attaque pas encore prête")
    raw_damage = BASIC_ATTACK_POWER * max(player_stat(session, "atk"), player_stat(session, "int"))
    hit = damage_monster(session, raw_damage)
    cooldown = player_attack_cooldown(session)
    session["player_basic_attack_ready_at"] = t + cooldown
    return {"raw_damage": round(raw_damage, 1), **hit, "next_ready_in": round(cooldown, 2)}


def dodge(session: dict) -> dict:
    """Esquive du joueur ; lève CombatRefusal pendant sa recharge.

    Elle n'évite que l'attaque en cours d'annonce dont la parade est
    l'esquive. Lancée à un autre moment, elle est perdue (et sa recharge
    court quand même) : on ne peut pas esquiver par réflexe en continu."""
    t = now()
    if t < session["player_dodge_ready_at"]:
        raise CombatRefusal("Esquive pas encore prête")
    session["player_dodge_ready_at"] = t + DODGE_COOLDOWN_SECONDS
    pending = session["monster_pending"]
    useful = pending is not None and not pending["dodged"] and counter_of(pending["move"]) == "esquive"
    if useful:
        pending["dodged"] = True
    return {"useful": useful, "against": pending["move"]["id"] if pending else None,
            "next_ready_in": DODGE_COOLDOWN_SECONDS}


def interrupt_pending(session: dict, skill: dict) -> str | None:
    """Une compétence de dégâts lancée pendant l'annonce d'une attaque à
    interrompre l'annule. Renvoie l'attaque interrompue, le cas échéant."""
    pending = session["monster_pending"]
    if pending is None or counter_of(pending["move"]) != "interruption":
        return None
    if not any(effect["type"] == "damage" for effect in skill.get("effects", [])):
        return None
    session["monster_pending"] = None
    return pending["move"]["id"]


def use_skill(session: dict, skill_id: str) -> dict:
    """Lance une compétence active équipée ; lève CombatRefusal si elle n'est
    pas équipée, pas prête ou trop chère en mana."""
    skill = session["skills"].get(skill_id)
    if skill_id not in session["equipped_active"] or skill is None:
        raise CombatRefusal("Compétence non équipée")
    t = now()
    cooldowns = session["player_skill_cooldowns"]
    if t < cooldowns.get(skill_id, 0):
        raise CombatRefusal("Compétence pas encore prête")
    if session["player_current_mana"] < skill["mana_cost"]:
        raise CombatRefusal("Pas assez de mana")
    session["player_current_mana"] -= skill["mana_cost"]
    cooldowns[skill_id] = t + skill["cooldown_seconds"]
    interrupted = interrupt_pending(session, skill)
    return {"results": apply_skill_effects(session, skill), "next_ready_in": round(skill["cooldown_seconds"], 2),
            "interrupted": interrupted}


def tick(session: dict) -> dict | None:
    """Un tick de combat : effets expirés, régénération du mana, puis attaque
    du monstre si l'une est prête (renvoyée)."""
    expire_effects(session)
    regen_mana(session, TICK_INTERVAL_SECONDS)
    return monster_tick(session)


# -----------------------------------------------------------------------------
# Vue client
# -----------------------------------------------------------------------------

def _public_effects(effects: list[dict]) -> list[dict]:
    t = now()
    return [{
        "source": e["source"],
        "label": e["label"],
        "stat": e["stat"],
        "stat_label": STAT_LABELS.get(e["stat"], e["stat"]),
        "percent": e["percent"],
        "flat": e["flat"],
        "remaining": round(max(0.0, e["expires_at"] - t), 1),
    } for e in effects]


def status(session: dict) -> dict:
    """État des effets envoyé au client avec chaque événement."""
    t = now()
    expires_at = session["player_shield_expires_at"]
    return {
        "player_shield": session["player_shield"],
        "player_shield_remaining": round(max(0.0, expires_at - t), 1) if expires_at else None,
        "player_effects": _public_effects(session["player_effects"]),
        "monster_effects": _public_effects(session["monster_effects"]),
        "active_passives": [
            skill.get("name", skill["id"]) for skill in session["conditional_passives"]
            if evaluate_combat_condition(skill.get("condition"), session)
            and any(e["type"] in ("stat_bonus", "stat_bonus_percent") for e in skill.get("effects", []))
        ],
        "basic_attack_cooldown": round(player_attack_cooldown(session), 2),
        "player_mana_regen": round(player_mana_regen(session), 2),
        "monster_pending": pending_view(session),
        "dodge_ready_in": round(max(0.0, session["player_dodge_ready_at"] - t), 2),
        "dodge_cooldown": DODGE_COOLDOWN_SECONDS,
    }


def status_signature(session: dict) -> tuple:
    """Résumé de l'état des effets, pour n'envoyer une mise à jour au client
    que lorsqu'il change."""
    return (
        session["player_shield"],
        tuple((e["source"], e["stat"]) for e in session["player_effects"]),
        tuple((e["source"], e["stat"]) for e in session["monster_effects"]),
        tuple(s["id"] for s in session["conditional_passives"] if evaluate_combat_condition(s.get("condition"), session)),
        (session["monster_pending"] or {}).get("dodged"),
    )
