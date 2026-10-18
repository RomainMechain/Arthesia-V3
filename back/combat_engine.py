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
K_DEF = 20
BASE_ATTACK_COOLDOWN = 2.0
K_AGI = 0.03
BASIC_ATTACK_POWER = 1.0
BASE_MANA = 50
K_INT = 4
BASE_MANA_REGEN = 1
K_SAG = 0.15
# Recharge commune à tous les consommables utilisés en combat.
ITEM_COOLDOWN_SECONDS = 5.0

STAT_LABELS = {"hp": "PV", "atk": "ATK", "def": "DEF", "agi": "AGI", "int": "INT", "sag": "SAG"}


def now() -> float:
    return time.monotonic()


# -----------------------------------------------------------------------------
# Formules de base
# -----------------------------------------------------------------------------

def compute_basic_attack_cooldown(agi: float) -> float:
    """Convertit l'agilité en délai entre deux attaques basiques.

    Plus l'agilité augmente, plus le délai diminue, sans tomber à zéro :
    ``BASE_ATTACK_COOLDOWN`` reste le délai de référence.
    """
    return BASE_ATTACK_COOLDOWN / (1 + max(agi, 0) * K_AGI)


def compute_max_mana(int_stat: float) -> float:
    """Calcule le mana maximal à partir de l'intelligence du joueur."""
    return BASE_MANA + int_stat * K_INT


def compute_mana_regen(sag_stat: float) -> float:
    """Calcule la régénération de mana par seconde à partir de la sagesse."""
    return BASE_MANA_REGEN + max(sag_stat, 0) * K_SAG


def compute_mitigation(defense: float) -> float:
    """Retourne la part des dégâts absorbée par la défense.

    La formule asymptotique ``def / (def + K_DEF)`` évite qu'une défense
    élevée rende les dégâts totalement nuls.
    """
    defense = max(defense, 0)
    return defense / (defense + K_DEF)


# -----------------------------------------------------------------------------
# Session
# -----------------------------------------------------------------------------

def new_session(player_id: str, zone_id: str, entry_id: str, monster: dict, player_stats: dict,
                equipped_active: list[str], conditional_passives: list[dict]) -> dict:
    """Crée l'état en mémoire d'un combat."""
    max_mana = compute_max_mana(player_stats.get("int", 0))
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
        "player_current_hp": player_stats["hp"],
        "player_max_hp": player_stats["hp"],
        "player_current_mana": max_mana,
        "player_max_mana": max_mana,
        "player_effects": [],
        "player_shield": 0.0,
        "player_shield_expires_at": None,
        "player_basic_attack_ready_at": now(),
        "player_skill_cooldowns": {},
        "item_ready_at": now(),
        "equipped_active": list(equipped_active),
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

def damage_monster(session: dict, raw_damage: float) -> float:
    """Applique des dégâts au monstre après sa défense effective."""
    final = round(raw_damage * (1 - compute_mitigation(monster_stat(session, "def"))), 1)
    session["monster_current_hp"] = max(0.0, session["monster_current_hp"] - final)
    return final


def damage_player(session: dict, raw_damage: float) -> dict:
    """Applique des dégâts au joueur : défense, puis bouclier, puis PV."""
    mitigated = round(raw_damage * (1 - compute_mitigation(player_stat(session, "def"))), 1)
    absorbed = min(session["player_shield"], mitigated)
    session["player_shield"] = round(session["player_shield"] - absorbed, 1)
    if session["player_shield"] <= 0:
        session["player_shield"] = 0.0
        session["player_shield_expires_at"] = None
    final = round(mitigated - absorbed, 1)
    session["player_current_hp"] = max(0.0, round(session["player_current_hp"] - final, 1))
    return {"final_damage": final, "absorbed": round(absorbed, 1)}


def heal_player(session: dict, amount: float) -> float:
    """Soigne le joueur sans dépasser ses PV max ; renvoie les PV réellement rendus."""
    before = session["player_current_hp"]
    session["player_current_hp"] = min(session["player_max_hp"], round(before + amount, 1))
    return round(session["player_current_hp"] - before, 1)


def scaling_stat(skill: dict) -> str:
    """Stat qui fait évoluer une compétence : INT en magique, sinon ATK pour
    les dégâts ; les soins et boucliers non physiques utilisent INT."""
    return "atk" if skill.get("damage_type") == "physical" else "int"


def apply_shield(session: dict, skill: dict, effect: dict) -> float:
    amount = round(effect.get("amount_power", 0) * player_stat(session, scaling_stat(skill)), 1)
    session["player_shield"] = round(max(session["player_shield"], amount), 1)
    duration = effect.get("duration_seconds")
    session["player_shield_expires_at"] = now() + duration if duration else None
    return amount


def apply_skill_effects(session: dict, skill: dict) -> list[dict]:
    """Applique les effets d'une compétence active et décrit le résultat."""
    results = []
    for effect in skill.get("effects", []):
        effect_type = effect["type"]
        if effect_type == "damage":
            stat = "int" if skill.get("damage_type") == "magical" else "atk"
            final = damage_monster(session, effect["power"] * player_stat(session, stat))
            results.append({"effect": "damage", "final_damage": final})
        elif effect_type == "heal":
            amount = heal_player(session, effect["power"] * player_stat(session, "int"))
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
    compétence est prête. Renvoie l'attaque réalisée, le cas échéant."""
    # Un debuff d'agilité ralentit la recharge des attaques du monstre.
    speed = max(0.2, 1 + sum(e["percent"] for e in session["monster_effects"] if e["stat"] == "agi") / 100)
    cooldowns = session["monster_cooldowns"]
    moveset = available_moveset(session)
    for move in moveset:
        cooldowns[move["id"]] = max(0.0, cooldowns.get(move["id"], 0.0) - TICK_INTERVAL_SECONDS * speed)

    ready = [move for move in moveset if cooldowns.get(move["id"], 0.0) <= 0]
    if not ready:
        return None
    # Le poids permet au template du monstre de contrôler la fréquence
    # relative de ses attaques sans coder cette préférence ici.
    chosen = random.choices(ready, weights=[move.get("weight", 1) for move in ready], k=1)[0]
    cooldowns[chosen["id"]] = chosen["cooldown_seconds"]
    # Un debuff d'attaque réduit les dégâts fixes des attaques du monstre.
    attack_factor = max(0.0, 1 + sum(e["percent"] for e in session["monster_effects"] if e["stat"] == "atk") / 100)
    raw = chosen["damage"] * attack_factor
    result = damage_player(session, raw)
    return {"skill_id": chosen["id"], "raw_damage": round(raw, 1), **result}


def regen_mana(session: dict, seconds: float) -> None:
    regen = compute_mana_regen(player_stat(session, "sag")) * seconds
    session["player_current_mana"] = min(session["player_max_mana"], session["player_current_mana"] + regen)


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
        "basic_attack_cooldown": round(compute_basic_attack_cooldown(player_stat(session, "agi")), 2),
        "player_mana_regen": round(compute_mana_regen(player_stat(session, "sag")), 2),
    }


def status_signature(session: dict) -> tuple:
    """Résumé de l'état des effets, pour n'envoyer une mise à jour au client
    que lorsqu'il change."""
    return (
        session["player_shield"],
        tuple((e["source"], e["stat"]) for e in session["player_effects"]),
        tuple((e["source"], e["stat"]) for e in session["monster_effects"]),
        tuple(s["id"] for s in session["conditional_passives"] if evaluate_combat_condition(s.get("condition"), session)),
    )
