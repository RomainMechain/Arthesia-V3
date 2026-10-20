"""Constantes d'équilibrage partagées par le serveur et les outils.

Module pur (aucun accès à Firestore ni aux fichiers) : le simulateur de
combat (``generate_static_files/balance/simulate_combat.py``) l'importe sans démarrer le serveur. Les
valeurs sont documentées dans ``generate_static_files/arthesia_balance_reference.md``.
"""

# Statistiques d'un personnage avant tout point dépensé.
BASE_STATS = {"hp": 80, "atk": 8, "def": 5, "agi": 5, "int": 6, "sag": 5}

# Valeur d'un point de statistique dépensé.
# La SAG vaut 2 par point comme l'INT : elle porte les soins et boucliers, dont
# la puissance se lit comme celle des dégâts (power × stat).
POINT_VALUE = {"hp": 25, "atk": 2, "def": 2, "agi": 1, "int": 2, "sag": 2}

# Points gagnés à chaque niveau.
STAT_POINTS_PER_LEVEL = 6

# Réinitialisation des points de stat, en ville : la première est gratuite
# (pour essayer un autre build), les suivantes coûtent RESPEC_GOLD_PER_LEVEL ×
# niveau, soit le prix d'une pièce d'équipement de ce niveau.
RESPEC_GOLD_PER_LEVEL = 30


def respec_cost(level: int, resets_done: int) -> int:
    return 0 if resets_done == 0 else RESPEC_GOLD_PER_LEVEL * max(level, 1)

# Compétences équipables. Les monstres sont calibrés pour un joueur qui utilise
# MAX_ACTIVE_SKILL_SLOTS compétences actives : chaque compétence en plus
# s'ajouterait au rythme de dégâts et rendrait le contenu trivial.
MAX_ACTIVE_SKILL_SLOTS = 4
MAX_PASSIVE_SKILL_SLOTS = 4

# Effet d'une compétence = base + power × stat. Au niveau où la compétence se
# débloque, la base fait SKILL_BASE_SHARE de l'effet pour le joueur de
# référence. Le coût en mana est fixe : une compétence de début de jeu reste
# bon marché mais perd de son importance, puisque sa base ne grandit pas.
SKILL_BASE_SHARE = 0.6
# Le kit du joueur de référence a été débloqué SKILL_KIT_LEVEL_LAG niveaux
# plus tôt, en moyenne : la calibration des monstres en tient compte.
SKILL_KIT_LEVEL_LAG = 3
# Palier des compétences du kit de référence selon le rôle du monstre. Contre
# les monstres normaux, le joueur n'a souvent que des compétences simples (de
# départ ou d'étage) ; face aux élites et aux boss, il a déjà gagné des
# compétences d'étage : la moitié de son kit est de palier difficile.
REFERENCE_KIT_TIERS = {
    "normal": {"frappe": "simple", "technique": "simple", "assaut": "simple", "soin": "simple"},
    "elite": {"frappe": "simple", "technique": "difficile", "assaut": "difficile", "soin": "simple"},
    "boss": {"frappe": "simple", "technique": "difficile", "assaut": "difficile", "soin": "simple"},
}

STATS = ("hp", "atk", "def", "agi", "int", "sag")


def reference_player_stats(level: int, gear_share: float = 0.0, weights: dict | None = None) -> dict:
    """Stats d'un personnage de référence au niveau ``level``.

    ``weights`` répartit les points de chaque niveau (par défaut un point par
    stat : le généraliste). ``gear_share`` est la part de la puissance totale
    apportée par l'équipement : 0.45 signifie que l'équipement vaut 45 % de la
    puissance, soit 0.45 / 0.55 fois les points de niveau.
    """
    weights = weights or {stat: 1 for stat in STATS}
    total_weight = sum(weights.values())
    level_points = STAT_POINTS_PER_LEVEL * level
    gear_points = level_points * gear_share / (1 - gear_share) if gear_share < 1 else 0
    stats = dict(BASE_STATS)
    for stat in STATS:
        share = weights.get(stat, 0) / total_weight
        stats[stat] += (level_points + gear_points) * share * POINT_VALUE[stat]
    return {stat: round(value, 1) for stat, value in stats.items()}


# -----------------------------------------------------------------------------
# Monstres (calibrés par generate_static_files/balance/simulate_combat.py)
# -----------------------------------------------------------------------------

# Part de la puissance apportée par l'équipement pour le joueur de référence.
GEAR_SHARE = 0.45

# Répartition des points du joueur de référence : un hybride raisonnable, un
# peu plus porté sur l'ATK, les PV et la DEF. Les monstres sont calqués sur
# lui ; s'ils l'étaient sur un généraliste, l'écart entre les deux (faible au
# niveau 1, où les stats de base dominent) grandirait avec le niveau et
# l'équilibrage dériverait.
REFERENCE_WEIGHTS = {"hp": 1.25, "atk": 1.5, "def": 1.25, "agi": 0.75, "int": 0.5, "sag": 0.75}


def reference_build_stats(level: int) -> dict:
    """Joueur de référence équipé, contre lequel les monstres sont calibrés."""
    return reference_player_stats(level, GEAR_SHARE, REFERENCE_WEIGHTS)


def monster_base_stats(level: int) -> dict:
    """Base des stats d'un monstre (avant le profil de son rôle) : celles du
    joueur de référence, avec une INT égale à son ATK pour que les attaques
    magiques frappent comme les physiques."""
    stats = reference_build_stats(level)
    stats["int"] = stats["atk"]
    return stats

# Valeurs retenues après calibration : multiplicateurs des stats du joueur de
# référence (équipé) et puissance de l'attaque de base du monstre.
# Issues de ``calibrate`` (moyenne des niveaux 5 et 30).
ROLE_PROFILES = {
    "normal": {"hp": 2.15, "atk": 1.0, "def": 1.0, "agi": 1.0, "int": 1.0, "sag": 1.0, "power": 0.21},
    "elite": {"hp": 2.99, "atk": 1.1, "def": 1.2, "agi": 1.1, "int": 1.1, "sag": 1.0, "power": 0.252},
    "boss": {"hp": 6.0, "atk": 1.2, "def": 1.4, "agi": 1.2, "int": 1.2, "sag": 1.0, "power": 0.13},
}

# Attaques télégraphiées (élites et boss) : la grosse attaque s'annonce
# pendant quelques secondes et peut être esquivée ou interrompue. Elle frappe
# TELEGRAPH_POWER_MULTIPLIER fois plus fort qu'une attaque lente ordinaire : la
# calibration suppose un joueur moyen qui pare AVERAGE_AVOID_RATE des annonces,
# donc un joueur attentif bat ce qu'un joueur distrait ne bat pas.
TELEGRAPH_POWER_MULTIPLIER = 2.5
AVERAGE_AVOID_RATE = 0.5
# Attaques télégraphiées du moveset de référence : durée de l'annonce et parade.
TELEGRAPHED_MOVES = {
    "elite": {"assaut": (1.5, "esquive")},
    "boss": {"assaut": (1.5, "interruption"), "furie": (2.0, "esquive")},
}

# Montée de la difficulté dans un étage : la puissance des attaques des
# monstres est multipliée selon la profondeur de la zone (distance à la zone
# de départ). Les PV ne changent pas : la durée des combats reste la même.
DIFFICULTY_TIERS = {"entree": 0.7, "coeur": 1.0, "profondeurs": 1.15}

# -----------------------------------------------------------------------------
# Expérience du personnage
#
# La courbe est pensée en temps de jeu : un niveau demande environ
# hours_per_level(niveau) heures, dont 70 % d'XP gagnée en combat (à
# KILLS_PER_HOUR monstres normaux de son niveau par heure, régénération
# comprise) et 30 % en quêtes. L'XP d'un monstre et le seuil d'un niveau
# grandissent tous deux exponentiellement ; le seuil un peu plus vite, donc
# chaque niveau prend un peu plus de temps que le précédent.
# -----------------------------------------------------------------------------

KILL_XP_BASE = 10
KILL_XP_GROWTH = 1.06
ROLE_XP_MULTIPLIER = {"normal": 1, "elite": 4, "boss": 15}
KILLS_PER_HOUR = 12
COMBAT_XP_SHARE = 0.7
# Heures par niveau : 1 h au niveau 11, +1,2 % par niveau ensuite. Les dix
# premiers niveaux vont plus vite (15 min pour le premier).
HOURS_PER_LEVEL_GROWTH = 1.012
EARLY_LEVELS = 10
# Part du seuil de niveau apportée par une quête, selon son ampleur. Un étage
# compte ~30 quêtes (2 à 4 par zone, 4 à 6 en ville), mêlant les trois
# ampleurs : ces parts leur font apporter ~30 % de l'XP de l'étage.
QUEST_XP_SHARE = {"small": 0.03, "normal": 0.06, "major": 0.12}


def hours_per_level(level: int) -> float:
    level = max(level, 1)
    early = min(1.0, 0.25 + 0.075 * (level - 1)) if level <= EARLY_LEVELS else 1.0
    return early * HOURS_PER_LEVEL_GROWTH ** (level - 1)


def kill_xp(monster_level: int, role: str = "normal") -> int:
    """XP d'un monstre, avant l'effet de l'écart de niveau."""
    base = KILL_XP_BASE * KILL_XP_GROWTH ** (max(monster_level, 1) - 1)
    return max(1, round(base * ROLE_XP_MULTIPLIER.get(role, 1)))


def xp_to_next_level(level: int) -> int:
    """Seuil d'XP pour passer du niveau ``level`` au suivant."""
    level = max(level, 1)
    per_kill = KILL_XP_BASE * KILL_XP_GROWTH ** (level - 1)
    return round(per_kill * KILLS_PER_HOUR * hours_per_level(level) / COMBAT_XP_SHARE)


def level_gap_xp_multiplier(monster_level: int, player_level: int) -> float:
    """Un monstre plus fort rapporte jusqu'à +50 % ; un monstre plus faible
    rapporte 20 % de moins par niveau d'écart, jamais moins de 10 %."""
    gap = monster_level - player_level
    if gap >= 0:
        return 1 + 0.1 * min(gap, 5)
    return max(0.1, 1 + 0.2 * gap)


def quest_xp(level: int, size: str = "normal") -> int:
    """XP conseillée pour une quête de ce niveau (utilisée par les prompts)."""
    return round(xp_to_next_level(level) * QUEST_XP_SHARE.get(size, QUEST_XP_SHARE["normal"]))


# -----------------------------------------------------------------------------
# Métiers
#
# Un métier progresse environ deux fois moins vite que le personnage : on ne
# peut pas tout monter, ce qui nourrit le commerce entre joueurs. L'XP d'une
# action dépend du niveau de la ressource récoltée ou de l'objet fabriqué ; une
# fabrication vaut plusieurs récoltes, puisqu'elle consomme des ingrédients.
# -----------------------------------------------------------------------------

# Les objets et les ressources ont un niveau à l'échelle du personnage ; un
# niveau de métier en couvre ITEM_LEVELS_PER_PROFESSION_LEVEL. Récolter une
# ressource de niveau 10 ou fabriquer avec un matériau de niveau 10 demande
# donc le niveau 5 dans le métier.
ITEM_LEVELS_PER_PROFESSION_LEVEL = 2

PROFESSION_XP_BASE = 5
PROFESSION_XP_GROWTH = 1.06
PROFESSION_ACTIONS_PER_LEVEL = 120
CRAFT_XP_MULTIPLIER = 4


def profession_level_for(item_level: int) -> int:
    """Niveau de métier requis pour un objet ou une ressource de ce niveau."""
    return max(1, -(-max(item_level, 1) // ITEM_LEVELS_PER_PROFESSION_LEVEL))


def max_item_level_for(profession_level: int) -> int:
    """Niveau d'objet le plus haut accessible à ce niveau de métier."""
    return max(profession_level, 1) * ITEM_LEVELS_PER_PROFESSION_LEVEL


def profession_action_xp(content_level: int, action: str = "gather") -> int:
    """XP d'une action ; ``content_level`` est à l'échelle du métier
    (``profession_level_for`` du niveau de l'objet)."""
    base = PROFESSION_XP_BASE * PROFESSION_XP_GROWTH ** (max(content_level, 1) - 1)
    return max(1, round(base * (CRAFT_XP_MULTIPLIER if action == "craft" else 1)))


def profession_xp_to_next_level(level: int) -> int:
    level = max(level, 1)
    per_action = PROFESSION_XP_BASE * PROFESSION_XP_GROWTH ** (level - 1)
    return round(per_action * PROFESSION_ACTIONS_PER_LEVEL * HOURS_PER_LEVEL_GROWTH ** (level - 1))


# -----------------------------------------------------------------------------
# Points de vie hors combat et défaite
# -----------------------------------------------------------------------------

# Les PV perdus se régénèrent entièrement en HP_REGEN_FULL_SECONDS, trois fois
# plus vite dans une zone de type ``town``.
HP_REGEN_FULL_SECONDS = 600
TOWN_REGEN_MULTIPLIER = 3

# Défaite : perte d'une part de l'XP du niveau (sans perdre de niveau) et d'une
# part de l'or (plafonnée selon le niveau), retour à une fraction des PV.
DEFEAT_XP_SHARE = 0.10
DEFEAT_GOLD_SHARE = 0.03
DEFEAT_GOLD_CAP_PER_LEVEL = 20
DEFEAT_HP_SHARE = 0.20


def defeat_penalty(level: int, xp: int, gold: int) -> dict:
    """Pertes d'une défaite : ``xp_lost`` ne dépasse pas l'XP du niveau en cours."""
    xp_lost = min(xp, round(xp_to_next_level(level) * DEFEAT_XP_SHARE))
    gold_lost = min(gold, round(gold * DEFEAT_GOLD_SHARE), DEFEAT_GOLD_CAP_PER_LEVEL * max(level, 1))
    return {"xp_lost": max(0, xp_lost), "gold_lost": max(0, gold_lost)}
