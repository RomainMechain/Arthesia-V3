# Arthesia — Modèle de données (v2 : carte par nœuds)

> Document de référence évolutif. Statut : squelette validé (Floor / Zone / Entry / Template / Condition)
> + Effects, système de combat v1, Stats, et templates Monster / Resource / NPC / Shop / Player Market /
> Quest / Item / Skill / Title / Workshop définis, plus le système de Craft (troisième couche : contenu
> généré à la volée puis mis en cache) et le pipeline de génération d'étage (squelette global en un appel
> + `generation_brief`/`condition_manifest` pour garder la cohérence entre zones générées séparément
> ensuite). Tous les templates portent un champ `description` (lore/UI). Métiers de récolte et de craft
> listés (leveling non défini). `discovery` volontairement hors périmètre V1.
> Non couvert pour l'instant : contenu réel des compétences/titres/loot, esquive/parade (V2),
> état dynamique (progression de quête, stocks, respawns, annonces marché, loadout joueur, cache de craft...).

---

## Vue d'ensemble

Deux couches bien séparées :

- **Contenu statique** — généré par l'IA en amont, identique pour tous les joueurs, modifiable à la main.
  Floors, Zones, Entries, Templates, Conditions.
- **État dynamique** — vit sur le serveur, change en jouant (mob tué, respawn en cours, zone débloquée
  pour un joueur, boss serveur vaincu...). Hors périmètre de ce document.

```
Floor
 ├─ zones: Zone[]
 └─ connections: [zone_id, zone_id][]

Zone
 └─ categories (déduites automatiquement, voir table plus bas)
     └─ entries: Entry[]

Entry
 └─ référence un Template (monster / resource / discovery / npc / ...)
```

---

## Floor (étage)

```json
{
  "id": "floor_1",
  "name": "Les Terres d'Aube",
  "description": "...",
  "start_zone_id": "valclair",
  "boss_zone_id": "gardien_du_seuil",
  "zones": [ /* Zone[] */ ],
  "connections": [
    ["valclair", "foret_sombre"],
    ["foret_sombre", "camp_bucherons"]
  ]
}
```

**Notes :**
- `connections` est séparé des zones pour éviter la duplication (une connexion n'appartient à aucune
  des deux zones en particulier, elle relie les deux).
- `boss_zone_id` désigne la zone dont la victoire débloque le passage vers l'étage suivant
  (déblocage **serveur entier**, décidé précédemment).
- Le passage vers l'étage suivant est lui-même une zone (type `passage`), débloquée par
  `{ "type": "boss", "zone_id": boss_zone_id, "state": "defeated" }`.

---

## Zone

```json
{
  "id": "foret_sombre",
  "name": "Forêt Sombre",
  "type": "wilderness",
  "position": { "x": 24, "y": 64 },
  "visibility": "visible",
  "description": "...",
  "unlock_condition": null,
  "hint": null,
  "entries": [ /* Entry[] */ ]
}
```

| Champ | Valeurs / notes |
|---|---|
| `type` | `town`, `wilderness`, `water`, `camp`, `ruins`, `danger`, `boss`, `passage`, ... (libre, sert surtout à l'icône/DA sur la carte) |
| `visibility` | `"visible"` — toujours affichée sur la carte, même verrouillée (avec `hint`). `"hidden"` — n'existe pas sur la carte tant que `unlock_condition` n'est pas remplie. Pas de `hint` par définition. |
| `unlock_condition` | `null` = débloquée dès le départ. Sinon un objet `Condition` (voir plus bas). |
| `hint` | Uniquement pertinent si `visibility: "visible"` et verrouillée. Texte affiché à la place des actions. |
| `entries` | Liste d'`Entry`, groupées automatiquement par catégorie à l'affichage (voir plus bas). |

---

## Condition

Objet générique `{ "type": ..., ... }`, composable. Nouveau type de condition à l'avenir = un nouveau
`type` dans cette liste, rien d'autre à modifier ailleurs dans le modèle.

```json
{ "type": "level", "min": 5 }
{ "type": "quest", "quest_id": "murmure_du_lac", "state": "completed" }
{ "type": "quest", "quest_id": "les_arbres_qui_parlent", "state": "active" }
{ "type": "item", "item_id": "cle_runique", "state": "owned" }
{ "type": "zone", "zone_id": "ruines_architectes", "state": "unlocked" }
{ "type": "boss", "zone_id": "gardien_du_seuil", "state": "defeated" }
{ "type": "kill_count", "monster_id": "loup_alpha", "count": 5 }
{ "type": "gather_count", "resource_id": "bois_chene_runique", "count": 10 }
{ "type": "deliver_item", "item_id": "ecaille_luisante", "count": 3 }
```

Les trois derniers types sont pensés pour servir d'**objectifs de quête** (voir `Quest` plus bas) —
`kill_count`/`gather_count` suivent une progression, `deliver_item` consomme les objets à la remise.

**Conditions "temps réel combat"** — même objet réutilisé, mais évalué en direct pendant un combat
(déclencheurs de compétences passives) plutôt qu'au niveau progression/monde :

```json
{ "type": "hp_below_percent", "value": 30 }
{ "type": "hp_above_percent", "value": 80 }
{ "type": "mana_below_percent", "value": 20 }
```

Composition :

```json
{
  "type": "group",
  "mode": "all",
  "conditions": [
    { "type": "zone", "zone_id": "ruines_architectes", "state": "unlocked" },
    { "type": "zone", "zone_id": "marais_empoisonne", "state": "unlocked" }
  ]
}
```

`mode`: `"all"` (= ET, toutes requises) ou `"any"` (= OU, une seule suffit).

Utilisée à deux endroits : `Zone.unlock_condition` et `Entry.spawn.condition`.

---

## Entry (instance de spawn dans une zone)

Une Entry **ne contient pas** les stats/loot/comportement — elle référence un Template et décrit
uniquement *comment et quand* ce template apparaît **dans cette zone précise**. `rarity` et
`group_required` sont propres à l'instance : un même monstre peut être rare dans une zone bas niveau
et commun ailleurs.

```json
{
  "id": "foret_sombre_loup_alpha",
  "template_type": "monster",
  "template_id": "loup_alpha",
  "state_scope": "shared",
  "rarity": "rare",
  "group_required": false,
  "spawn": {
    "mode": "respawn_timer",
    "cooldown_seconds": 900
  }
}
```

Exemple conditionnel avec indice :

```json
{
  "id": "foret_sombre_eveilleur",
  "template_type": "monster",
  "template_id": "eveilleur_des_bois",
  "state_scope": "shared",
  "rarity": "very_rare",
  "group_required": false,
  "spawn": {
    "mode": "conditional",
    "condition": { "type": "quest", "quest_id": "les_arbres_qui_parlent", "state": "active" },
    "visibility_when_locked": "hinted",
    "hint": "Quelque chose dans les fourrés semble attendre que vous compreniez ce qu'il cherche."
  }
}
```

Exemple totalement invisible (pas de `visibility_when_locked` → absente de la liste tant que non
débloquée, aucun indice) :

```json
{
  "id": "foret_sombre_secret_x",
  "template_type": "discovery",
  "template_id": "on_definira_plus_tard",
  "state_scope": "per_player",
  "spawn": {
    "mode": "conditional",
    "condition": { "type": "item", "item_id": "carte_dechiree", "state": "owned" }
  }
}
```

### Champs

| Champ | Notes |
|---|---|
| `template_type` | `monster`, `resource`, `discovery`, `npc`, `quest_giver`, `shop`, `player_market`, ... — sert aussi à déduire la catégorie affichée (table plus bas). |
| `template_id` | Référence vers la fiche du template (voir section suivante). |
| `state_scope` | `"shared"` — état commun à tous les joueurs de la zone (mob tué = mort pour tout le monde jusqu'au respawn). `"per_player"` — état propre à chaque joueur (coffre trouvé une fois). |
| `rarity` | `common`, `rare`, `very_rare`, ... — pondération d'apparition dans le pool actif, **pas** une condition de déblocage. |
| `group_required` | Booléen — combat/récolte nécessitant plusieurs joueurs. |
| `spawn.mode` | Voir table ci-dessous. |

### `spawn.mode`

| Mode | Usage | Champs additionnels |
|---|---|---|
| `permanent` | Toujours présent (un PNJ, une échoppe) | — |
| `respawn_timer` | Cooldown après épuisement (mob, ressource) | `cooldown_seconds`, `stock` (optionnel, défaut `1` — nombre de charges disponibles avant passage en cooldown ; `stock: 3` = un nœud de ressource récoltable 3 fois avant de se reconstituer) |
| `conditional` | N'apparaît que si une `Condition` est remplie | `condition`, `visibility_when_locked` (`"hinted"` ou absent), `hint` (si hinted) |
| `unique_per_player` | Trouvable une fois par joueur (coffre, lore) | — |
| `admin_event` | Déclenché manuellement par un admin | `event_id` |

Liste ouverte — un nouveau mode (ex. horaire) s'ajoute ici sans toucher au reste du modèle.

---

## Template (référentiel, indépendant des zones)

Une fiche par créature/ressource/etc., définie une fois, réutilisée par autant d'`Entry` que
nécessaire dans autant de zones que nécessaire. Structure interne **à définir plus tard** — squelette
minimal pour l'instant :

```json
// monsters/loup_alpha.json
{
  "id": "loup_alpha",
  "name": "Loup Alpha",
  "level": 4,
  "stats": { /* à définir */ },
  "loot_table": [ /* à définir */ ],
  "skills": [ /* à définir */ ]
}
```

**Convention transversale :** tous les templates portent un champ `description` à but purement
narratif/UI (lore, flavor text, ou explication en langage courant d'un effet pour les compétences) —
indépendant de la mécanique portée par les autres champs. Ajouté rétroactivement aux exemples déjà
posés dans ce document.

Un référentiel par `template_type` : `monsters/`, `resources/`, `discoveries/`, `npcs/`, ...

---

## Catégories affichées (UI)

Déduites automatiquement de `template_type` via une table fixe, globale à tout le jeu (pas
d'override par zone — peu de catégories, cohérence pour le joueur) :

| `template_type` | Catégorie affichée |
|---|---|
| `monster` | Combat |
| `resource` | Ressources |
| `discovery` | Exploration |
| `npc` | Habitants |
| `quest_giver` | Quêtes |
| `shop` | Commerce |
| `player_market` | Commerce |

---

## Stats (joueur & monstre, symétriques)

Six stats, communes au joueur et aux monstres :

| Stat | Effet |
|---|---|
| `hp` | Points de vie max |
| `atk` | Scale les dégâts des attaques/compétences **physiques** |
| `def` | Réduit les dégâts entrants, **physiques et magiques** (pas de résistance magique séparée en v1) |
| `agi` | Réduit le cooldown de l'attaque classique (vitesse d'attaque) |
| `int` | Scale les dégâts des compétences **magiques** + augmente le mana max |
| `sag` | Augmente la régénération de mana (passive) |

### Progression : points de stat par niveau

Contrairement aux monstres (stats fixes par template), le joueur **monte en niveau et répartit des
points librement** : `6 points par niveau`, à investir dans n'importe quelle combinaison des 6 stats.
Valeur d'un point selon la stat (`hp` vaut plus qu'un point d'`agi`, par exemple) :

| Stat | Valeur d'1 point |
|---|---|
| `hp` | +25 |
| `atk` | +2 |
| `def` | +2 |
| `agi` | +1 |
| `int` | +2 |
| `sag` | +2 |

Stats de base (niveau "0", avant tout point dépensé) : `hp:80, atk:8, def:5, agi:5, int:6, sag:5`.

Un joueur **généraliste** (1 point/stat/niveau) suit une courbe de référence utilisée pour calibrer
monstres et équipement (voir `arthesia_balance_reference.md`). Un joueur **spécialisé** s'en écarte
fortement dans un sens — c'est la diversité de builds voulue, pas un déséquilibre à corriger. La
répartition des points elle-même est de l'**état par joueur** (comme les slots de compétences équipées),
pas du contenu généré — hors périmètre de ce document.

Chaque compétence (active joueur ou entrée de `moveset` monstre) porte un `damage_type`
(`"physical"` ou `"magical"`) qui détermine si elle scale sur `atk` ou `int`. Les soins scalent sur
`sag`, les boucliers sur `sag` (`atk` si la compétence est physique).

Les formules de combat (réduction des dégâts, mana, vitesse d'attaque, critiques et esquive) sont
relatives au niveau et calibrées par simulation : elles sont décrites dans
`arthesia_balance_reference.md` et font foi dans `back/combat_engine.py`.

---

## Combat (squelette v1)

Choix central : le combat est **temps réel simplifié**, avec une dimension de réaction (esquive/parade)
**reportée en V2**. Le joueur contrôle un seul personnage. Les combats de groupe (boss) sont
**synchronisés en direct** : tous les joueurs présents voient le même état du boss en temps réel.

### Côté joueur

| Élément | Comportement |
|---|---|
| **Attaque classique** | Déclenchée manuellement (tap), sans coût de mana, cooldown réduit par `agi` (voir formule dans Stats). Ne génère pas de mana. |
| **Compétences actives** | 4 à 8 slots équipables. Chacune a son propre cooldown, un coût en mana, et un `damage_type` (physical/magical). |
| **Passifs** | Toujours actifs, modifient les formules (dégâts %, régén, crit...). |
| **Titres** | Même principe que les passifs mais débloqués par accomplissement, équipés à part — plus rares/impactants. |
| **Mana** | `max_mana` et `mana_regen_per_second` dérivent de `int`/`sag` (voir Stats), plus bonus d'équipement/titres/passifs. Régénération **passive uniquement**. |
| **Esquive / parade** | **Reporté en V2.** Aucune mécanique d'évitement en V1. |

### Côté monstre

Le monstre est piloté par IA, donc "automatique" par nature — pas de distinction manuel/auto à faire
pour lui. Pas de ressource de mana côté monstre : uniquement des cooldowns.

- **`moveset`** : liste de compétences, chacune avec son cooldown. L'IA choisit, parmi les compétences
  dont le cooldown est écoulé, une compétence pondérée aléatoirement (`weight`).
- **`telegraph_seconds` / `avoidable_by`** : présents dans le schéma dès maintenant pour que la V2
  (esquive/parade) s'ajoute proprement plus tard, mais **non fonctionnels en V1** — l'attaque touche
  systématiquement, le télégraphe n'est qu'une info d'animation.
- **`phases`** (boss uniquement) : à des seuils de HP, le moveset actif change (`moveset_ids`), avec
  un flag `enrage` optionnel pour les phases finales.

### Combat de groupe

Le boss est une entité unique dont l'état (HP, phase, moveset actif) est partagé et synchronisé pour
tous les joueurs présents. Pas de système d'aggro/tank en V1 — cible aléatoire ou attaques en zone
touchant tout le monde.

---

## Template : Monster

```json
{
  "id": "loup_alpha",
  "name": "Loup Alpha",
  "description": "Meneur de meute à la fourrure striée de marques runiques. Plus intelligent et bien plus dangereux qu'un loup ordinaire.",
  "level": 4,
  "role": "normal",
  "stats": {
    "hp": 340,
    "atk": 28,
    "def": 12,
    "agi": 14,
    "int": 0,
    "sag": 0
  },
  "moveset": [
    {
      "id": "morsure",
      "damage_type": "physical",
      "cooldown_seconds": 2.5,
      "power": 0.21,
      "weight": 3,
      "telegraph_seconds": 0,
      "avoidable_by": null
    },
    {
      "id": "charge_feroce",
      "damage_type": "physical",
      "cooldown_seconds": 8,
      "power": 0.48,
      "weight": 1,
      "telegraph_seconds": 1.2,
      "avoidable_by": null
    }
  ],
  "phases": null,
  "loot_table": []
}
```

Boss avec phases (avec une attaque magique pour l'exemple) :

```json
{
  "id": "gardien_de_la_clairiere",
  "name": "Gardien de la Clairière",
  "description": "Une entité d'écorce et de pierre runique, éveillée pour protéger le cœur de la forêt.",
  "level": 6,
  "role": "boss",
  "stats": { "hp": 4200, "atk": 60, "def": 25, "agi": 10, "int": 40, "sag": 0 },
  "moveset": [
    { "id": "griffe", "damage_type": "physical", "cooldown_seconds": 2.5, "power": 0.14, "weight": 3, "telegraph_seconds": 0, "avoidable_by": null },
    { "id": "charge_feroce", "damage_type": "physical", "cooldown_seconds": 10, "power": 0.4, "weight": 1, "telegraph_seconds": 0, "avoidable_by": null },
    { "id": "onde_de_choc", "damage_type": "magical", "cooldown_seconds": 14, "power": 0.57, "weight": 1, "telegraph_seconds": 0, "avoidable_by": null }
  ],
  "phases": [
    { "hp_above_percent": 50, "moveset_ids": ["griffe", "charge_feroce"] },
    { "hp_above_percent": 0, "moveset_ids": ["griffe", "charge_feroce", "onde_de_choc"], "enrage": true }
  ],
  "loot_table": []
}
```

**Notes :**
- `role` : `normal`, `elite`, `boss` — sert à distinguer visuellement/mécaniquement, `phases` n'a de sens
  que pour `boss` (et potentiellement `elite` plus tard).
- `rarity` et `group_required` n'apparaissent **pas** ici : ce sont des propriétés de l'`Entry` (instance
  de spawn dans une zone précise), pas du template.
- `loot_table` reste un placeholder vide — structure à définir séparément (système d'items).

---

## Template : Resource

```json
{
  "id": "bois_chene_runique",
  "name": "Bois de Chêne Runique",
  "description": "Un bois dense, veiné de fines lignes lumineuses. Prisé des forgerons pour les manches d'outils enchantés.",
  "level": 3,
  "profession_required": { "profession_id": "bucheronnage", "min_level": 2, "xp_reward": 15 },
  "tool_required": null,
  "gather_time_seconds": 3,
  "yields": [
    { "item_id": "bois_brut", "quantity_min": 1, "quantity_max": 3, "weight": 10 },
    { "item_id": "eclat_runique", "quantity_min": 1, "quantity_max": 1, "weight": 1 }
  ]
}
```

**Notes :**
- `profession_required` : propriété **intrinsèque** de la ressource (contrairement à `rarity`/`group_required`
  qui vivent sur l'`Entry` et varient par zone, le niveau de métier requis pour récolter ne dépend pas
  d'où pousse la ressource). Porte à la fois le prérequis (`min_level`) et la récompense (`xp_reward`)
  pour ce métier. `null` si aucun métier requis.
- Comportement UI : la ressource reste **visible** dans la liste même sans le niveau requis, mais
  l'interaction affiche le prérequis au lieu de lancer la récolte — même logique que le `hint` d'une
  zone verrouillée, appliqué ici à une entrée plutôt qu'à une zone entière.
- `tool_required` : référence optionnelle à un item (hache, pioche...). `null` par défaut, à développer
  avec le système d'items.
- `gather_time_seconds` : la récolte n'est pas instantanée, le joueur reste "occupé" pendant ce délai.
- `yields` : même logique de loot table pondérée (item, quantité min/max, poids) que celle prévue pour
  `loot_table` sur les monstres — cohérence volontaire entre les deux systèmes.

---

## Métiers (liste v1)

Système de leveling (courbe d'XP, paliers, ce que chaque niveau débloque) **non défini** — seule la
liste des métiers et leur thématique sont posées pour l'instant, comme référentiel pour `profession_id`.

### Métiers de récolte

| `profession_id` | Nom | Récolte typique |
|---|---|---|
| `bucheronnage` | Bûcheron | Bois dans les zones sauvages |
| `minage` | Mineur | Minerais dans grottes/ruines |
| `peche` | Pêcheur | Poissons sur zones aquatiques |
| `herboristerie` | Herboriste | Plantes, champignons, fleurs |
| `chasse` | Chasseur | Peaux/matériaux **après un combat**, pas via une `resource` classique — mécanisme à préciser quand `loot_table` (monstre) sera défini |
| `runographie` | Runographe | Fragments runiques sur sites spécifiques — matière première pour un futur système d'enchantement |
| `sourcellerie` | Sourcier | Veines de mana — ressource magique plutôt que physique |
| `archeologie` | Archéologue | Fragments de technologie/relique pré-Fracture, dans les ruines |

### Métiers de craft

Distincts des métiers de récolte — transforment des `Item` en de nouveaux `Item` via un `Workshop`
(voir plus bas). C'est le niveau dans ces métiers-ci qui plafonne le `level` des items utilisables en
craft.

| `profession_id` | Nom | Atelier associé |
|---|---|---|
| `forgeron` | Forgeron | Forge |
| `alchimiste` | Alchimiste | Alchimie |
| `couturier` | Couturier | Couture |
| `enchanteur` | Enchanteur | Enchantement |
| `sculpteur` | Sculpteur | Sculpture |
| `cuisinier` | Cuisinier | Cuisine |

---

## Template : NPC

```json
{
  "id": "aldric_aubergiste",
  "name": "Aldric",
  "description": "Tient l'auberge de Valclair depuis vingt ans. Toujours au courant des rumeurs, jamais avare d'un conseil.",
  "role": "Aubergiste",
  "dialogues": [
    "Bienvenue à Valclair, Marqué ! Votre arrivée était attendue... enfin, d'une certaine façon.",
    "On raconte que les Architectes ont creusé les fondations de ce village bien avant la Fracture.",
    "Si vous cherchez du travail, essayez la vieille forge à l'est."
  ]
}
```

**Notes :**
- `role` : purement cosmétique (sous-titre affiché dans la fiche), aucun effet mécanique.
- `dialogues` : une réplique tirée **au hasard** à chaque interaction. Pas de branchement, pas de
  séquence fixe, pas de conditions en V1 — extension naturelle plus tard via le système `Condition`
  déjà en place (ex. nouvelle réplique débloquée après une quête complétée).
- `quest_giver` et `shop` sont des `template_type` **séparés** de `npc`, pas des variantes enrichies —
  un personnage qui parle, donne une quête et vend, ce sont trois entrées distinctes dans la zone,
  reliées seulement par la narration. Les templates `quest_giver`/`shop` restent à définir avec les
  systèmes de quêtes/items.

---

## Template : Shop

```json
{
  "id": "echoppe_sylvaine",
  "name": "Échoppe de Sylvaine",
  "description": "Runes, composants et curiosités en tout genre — Sylvaine trouve toujours ce qu'il vous faut.",
  "currency": "or",
  "sell_offers": [
    { "item_id": "potion_mineure", "price": 15, "stock": null, "restock_seconds": null },
    { "item_id": "rune_protection", "price": 120, "stock": 3, "restock_seconds": 3600 }
  ]
}
```

**Notes :**
- `currency` : id de monnaie, `"or"` par défaut en V1 — accueillera d'autres monnaies (jetons d'event...)
  une fois un vrai système économique posé.
- **Rachat au serveur** : ce n'est **pas** un champ du template — règle globale du jeu, tous les shops
  rachètent tous les objets. Formule du prix de rachat (probablement un % de la valeur de base de
  l'objet) reportée au système d'items.
- `sell_offers` : `stock: null` = infini. Chiffré = limité, se reconstitue via `restock_seconds`
  (`null` = ne se réapprovisionne jamais, utile pour un objet unique/événementiel).
- Pas de champ de verrouillage dans ce template : une boutique inaccessible tant qu'une condition n'est
  pas remplie réutilise `Entry.spawn.mode: "conditional"` + `Condition`, déjà existants.
- Si `stock` est limité, `Entry.state_scope` (déjà défini) tranche si c'est un stock de marché partagé
  entre tous les joueurs (`"shared"`) ou individuel (`"per_player"`).

---

## Template : Player Market (Autel des Ventes)

Marché **joueur-à-joueur** : contrairement aux autres templates, son contenu (les annonces en vente)
n'est pas généré par l'IA — il est entièrement **dynamique**, alimenté par les joueurs eux-mêmes. Le
template reste donc volontairement minimal, il ne décrit que le point d'accès :

```json
{
  "id": "autel_des_ventes_valclair",
  "name": "Autel des Ventes",
  "description": "Un vieil autel de pierre où les Marqués laissent leurs objets à la vue des autres, contre quelques pièces.",
  "currency": "or"
}
```

**Notes :**
- Pas de `sell_offers` ici : les annonces (qui vend quoi, à quel prix) sont de l'**état dynamique**,
  stocké et géré côté serveur — hors périmètre de ce document (voir "Ce qui reste à définir").
  Probablement une entité globale ou par-étage plutôt que par-zone, à trancher avec le système d'items.
- `currency` : même logique que le `shop` classique.

---

## Template : Quest (quest_giver)

```json
{
  "id": "murmure_du_lac",
  "name": "Le Murmure du Lac",
  "description": "Des lumières bleues montent parfois du fond du lac. Torven pense que ça n'a rien de naturel.",
  "objectives": [
    { "type": "kill_count", "monster_id": "loup_alpha", "count": 5 },
    { "type": "deliver_item", "item_id": "ecaille_luisante", "count": 3 }
  ],
  "rewards": {
    "xp": 200,
    "gold": 50,
    "items": [ { "item_id": "amulette_du_pecheur", "quantity": 1 } ]
  },
  "repeatable": false
}
```

**Notes :**
- `objectives` : liste de `Condition` (voir plus haut) — réutilisation directe, pas de schéma parallèle.
  En V1, **toutes** les conditions sont requises (équivalent d'un `group` en mode `"all"` implicite) —
  pas de branchement ni de choix d'objectifs alternatifs pour l'instant.
- `rewards` : structure provisoire, dépend du système d'items/monnaie pas encore défini.
- `repeatable` : booléen posé maintenant (utile pour distinguer une quête d'histoire d'une quête
  journalière plus tard), mais la mécanique de reset (quand/comment) n'est pas définie.
- Déblocage de la quête (apparition dans la liste du `quest_giver`) : réutilise
  `Entry.spawn.mode: "conditional"` + `Condition`, comme pour tous les autres templates.
- **Hors périmètre ici** : suivi de la progression par joueur (quête active, objectifs partiels,
  terminée) — c'est de l'état dynamique, pas du contenu généré.

---

## Template : Item

Un seul template pour les trois catégories (`material`, `equipment`, `consumable`) — champs communs à
la racine, bloc spécifique selon `category`. Référencé partout où `item_id` apparaît déjà dans le
modèle : `Resource.yields`, `Monster.loot_table`, `Shop.sell_offers`, `Quest.rewards`, l'objectif
`deliver_item`.

```json
// material
{
  "id": "bois_brut",
  "name": "Bois Brut",
  "description": "Du bois fraîchement coupé.",
  "category": "material",
  "level": 1,
  "base_value": 2,
  "stack_size": 99
}

// equipment
{
  "id": "epee_rouille",
  "name": "Épée Rouillée",
  "description": "Elle a connu de meilleurs jours, mais elle tranche encore.",
  "category": "equipment",
  "level": 1,
  "base_value": 25,
  "stack_size": 1,
  "equipment": {
    "slot": "weapon",
    "level_required": 1,
    "stat_bonuses": { "atk": 5 }
  }
}

// consumable
{
  "id": "potion_mineure",
  "name": "Potion de Soin Mineure",
  "description": "Un remède rudimentaire mais efficace.",
  "category": "consumable",
  "level": 1,
  "base_value": 8,
  "stack_size": 20,
  "consumable": {
    "effect_type": "heal",
    "amount": 60,
    "duration_seconds": 0
  }
}
```

**Notes :**
- Champs communs : `id`, `name`, `description`, `category`, `level`, `base_value`, `stack_size`.
- `level` : palier/tier de l'objet, **toutes catégories confondues** (un matériau a un niveau autant
  qu'une épée). Sert de garde-fou au craft (voir `Workshop`) : un item ne peut être utilisé en entrée
  d'un atelier que si son `level` est ≤ au niveau du joueur dans le métier requis par cet atelier.
  Distinct de `equipment.level_required` (niveau **du personnage** pour équiper l'objet une fois fini) —
  souvent corrélés mais pas nécessairement identiques.
- `base_value` : ferme un point resté ouvert — le prix de rachat au serveur (règle globale du Shop)
  se calcule désormais `base_value × K_sell` (constante à calibrer).
- `equipment.slot` : `weapon`, `head`, `chest`, `gloves`, `boots`, `ring`, `amulet`, `tool` — le slot
  `tool` est ce que référencera `Resource.tool_required`, ferme cet autre point ouvert.
- `equipment.stat_bonuses` : réutilise directement les clés des 6 stats (`hp`/`atk`/`def`/`agi`/`int`/`sag`)
  définies plus haut — s'additionnent à la base du joueur.
- `consumable.effect_type` : `heal`, `restore_mana`, `buff_stat` (temporaire, avec `stat` + `amount` +
  `duration_seconds`) pour démarrer — liste ouverte comme les autres enums du modèle.
  `duration_seconds: 0` = effet instantané.

---

## Effects (sous-système réutilisé par les compétences)

Comme `Condition`, un objet générique `{ "type": ..., ... }`. Utilisé dans `Skill.effects`.

| `type` | Usage | Champs |
|---|---|---|
| `damage` | Dégâts — scale sur ATK ou INT du lanceur selon `damage_type` du skill parent | `power` (coefficient, ex. `1.8` = 180% du stat) |
| `heal` | Soin — scale sur SAG du lanceur | `power` |
| `shield` | Absorbe des dégâts pendant une durée — scale sur SAG (ATK si le skill est physique) | `amount_power`, `duration_seconds` |
| `buff` | Augmente une stat temporairement | `stat`, `amount_percent`, `duration_seconds`, `target` |
| `debuff` | Réduit une stat d'une cible temporairement | `stat`, `amount_percent`, `duration_seconds`, `target` |
| `stat_bonus` | Bonus de stat permanent (**passif uniquement**) | `stat`, `amount` |
| `stat_bonus_percent` | Bonus de stat % permanent (**passif uniquement**) | `stat`, `amount_percent` |

`target` : `"self"` / `"enemy"` / `"ally"` (`ally` pertinent seulement en combat de groupe — solo en V1,
`self` par défaut).

---

## Template : Skill

```json
// Passif toujours actif
{
  "id": "passive_peau_epaisse",
  "name": "Peau Épaisse",
  "description": "Votre peau a durci au fil des combats. Vous encaissez mieux les coups.",
  "kind": "passive",
  "unlock_condition": { "type": "level", "min": 5 },
  "trigger": "always",
  "condition": null,
  "effects": [ { "type": "stat_bonus", "stat": "def", "amount": 8 } ]
}
```

```json
// Passif conditionnel (déclenché en combat)
{
  "id": "passive_dernier_souffle",
  "name": "Dernier Souffle",
  "description": "Acculé, vous puisez dans vos dernières forces pour frapper plus fort.",
  "kind": "passive",
  "unlock_condition": { "type": "level", "min": 12 },
  "trigger": "conditional",
  "condition": { "type": "hp_below_percent", "value": 30 },
  "effects": [ { "type": "stat_bonus_percent", "stat": "atk", "amount_percent": 20 } ]
}
```

```json
// Actif — attaque, soin ou protection : même structure
{
  "id": "coup_puissant",
  "name": "Coup Puissant",
  "description": "Un coup brutal qui percute l'ennemi de plein fouet.",
  "kind": "active",
  "unlock_condition": { "type": "level", "min": 3 },
  "damage_type": "physical",
  "cooldown_seconds": 6,
  "mana_cost": 20,
  "telegraph_seconds": 0,
  "avoidable_by": null,
  "effects": [ { "type": "damage", "power": 1.8 } ]
}
```

```json
// Actif — soin
{
  "id": "soin_lumineux",
  "name": "Soin Lumineux",
  "description": "Une lumière chaude referme les blessures les plus récentes.",
  "kind": "active",
  "unlock_condition": { "type": "level", "min": 4 },
  "damage_type": "magical",
  "cooldown_seconds": 10,
  "mana_cost": 30,
  "effects": [ { "type": "heal", "power": 1.2 } ]
}
```

```json
// Actif — protection
{
  "id": "bouclier_de_pierre",
  "name": "Bouclier de Pierre",
  "description": "Une croûte de pierre runique se forme sur votre peau, absorbant les coups à venir.",
  "kind": "active",
  "unlock_condition": { "type": "level", "min": 6 },
  "cooldown_seconds": 15,
  "mana_cost": 25,
  "effects": [ { "type": "shield", "amount_power": 2.0, "duration_seconds": 8 } ]
}
```

**Notes :**
- `kind: "passive"` : `trigger` = `"always"` (toujours actif) ou `"conditional"` (nécessite `condition`,
  une des conditions "temps réel combat" définies plus haut). Pas de `cooldown`/`mana_cost`.
- `kind: "active"` : porte `cooldown_seconds`, `mana_cost`, `damage_type` (si l'effet scale sur une stat),
  et `telegraph_seconds`/`avoidable_by` — repris tels quels du moveset monstre, non fonctionnels en V1
  (placeholders V2 pour l'esquive/parade), pour rester symétrique.
- `unlock_condition` réutilise `Condition` sans rien ajouter — cohérent avec zones/entrées/quêtes.
- Les attaques des monstres utilisent aussi `power` (× leur `atk`, ou `int` si magique), comme les
  compétences joueur : un debuff de stat les affecte de la même façon. Les anciens templates à dégâts
  fixes (`damage`) restent lus.
- Slots (4 à 8 actifs équipables, cf. section Combat) et emplacements de passifs équipés : c'est de la
  configuration de personnage (état par joueur), pas du contenu — hors périmètre ici.

---

## Template : Title

Réutilise directement la structure des compétences passives (`effects`, toujours actif une fois
équipé) — un titre est en quelque sorte une "grosse passive". La différence est ailleurs :
`unlock_condition` est en général **composite** (`group`/`all_of`, cf. `Condition`), et le joueur peut
en **posséder plusieurs mais n'en équiper qu'un seul à la fois** — cette contrainte est une règle de
configuration côté joueur (comme les slots de compétences équipées), pas quelque chose que le template
doit empêcher.

```json
{
  "id": "titre_dompteur_de_loups",
  "name": "Dompteur de Loups",
  "description": "Vous avez maté les meutes de la Forêt Sombre au point qu'elles vous évitent désormais.",
  "unlock_condition": {
    "type": "group",
    "mode": "all",
    "conditions": [
      { "type": "kill_count", "monster_id": "loup_alpha", "count": 50 },
      { "type": "level", "min": 15 }
    ]
  },
  "effects": [ { "type": "stat_bonus_percent", "stat": "atk", "amount_percent": 10 } ]
}
```

**Notes :**
- Pas de `trigger`/`condition` de déclenchement en V1 (contrairement aux passifs) — un titre équipé est
  actif en permanence. Extensible plus tard sur le même principe si besoin d'un titre conditionnel.
- `unlock_condition` composite typique : combine plusieurs `kill_count`/`level`/`boss`/`quest` — les
  titres sont pensés comme des accomplissements rares, pas des paliers de progression courants.

---

## Craft

Contrairement au reste du modèle, le craft n'est **ni statique (généré en amont) ni dynamique au sens
déjà défini (état qui change en jouant)** — c'est une **troisième catégorie** : du contenu généré par
l'IA **à la volée, une seule fois**, puis figé pour toujours et partagé par tous les joueurs. Le craft
n'est pas une "recette à étapes obligatoires" : chaque **atelier** fait une seule transformation
atomique (des items en entrée + des paramètres → un item en sortie). Le "multi-étapes" émerge
naturellement du fait qu'un résultat de craft est un `Item` normal, réutilisable comme ingrédient
ailleurs — aucune structure de recette à plusieurs étapes n'est nécessaire.

### Template : Workshop

```json
{
  "id": "forge",
  "name": "Forge",
  "description": "Fond et façonne les métaux et alliages.",
  "profession_required": { "profession_id": "forgeron" },
  "parameters": [
    { "id": "temperature", "label": "Température", "type": "choice", "options": ["basse", "moyenne", "haute", "extreme"] }
  ]
}
```

**Notes :**
- `parameters` : valeurs **discrètes** uniquement (`type: "choice"` + `options`), jamais de curseur
  continu — indispensable pour que "mêmes entrées + mêmes réglages" retombe de façon fiable sur la
  même clé de cache entre deux joueurs différents.
- `profession_required.profession_id` : quel métier de craft ouvre l'accès à cet atelier. Pas de
  `min_level` ici — l'accès à l'atelier est binaire (tu pratiques ce métier ou non), c'est le **niveau**
  dans ce métier qui plafonne quels items (`item.level`) tu peux y utiliser, pas l'accès à l'atelier
  lui-même.

### Les 6 ateliers (liste v1)

| Atelier | Métier | Paramètre (v1) |
|---|---|---|
| Forge | Forgeron | `temperature` : basse / moyenne / haute / extrême |
| Alchimie | Alchimiste | `methode` : macération froide / ébullition rapide / distillation lente |
| Couture | Couturier | `technique` : point serré / tissage lâche / matelassage |
| Enchantement | Enchanteur | `intensite_rune` : faible / stable / instable / profonde |
| Sculpture | Sculpteur | `finition` : brute / polie / gravée |
| Cuisine | Cuisinier | `cuisson` : cru-mariné / grillé / mijoté / flambé |

Un seul paramètre par atelier pour démarrer, comme convenu — rien n'empêche d'en ajouter (ex. une
durée, un second réactif) une fois le système testé.

### Le cache de craft

```json
{
  "recipe_key": "forge|bois_brut:2,minerai_fer:1|temperature=haute",
  "workshop_id": "forge",
  "inputs": [ { "item_id": "minerai_fer", "quantity": 1 }, { "item_id": "bois_brut", "quantity": 2 } ],
  "parameters": { "temperature": "haute" },
  "output_item_id": "alliage_incandescent"
}
```

**Déroulé d'un craft :**
1. Le joueur choisit un atelier, des items en inventaire (`item.level` ≤ son niveau dans le métier de
   l'atelier), et une valeur pour chaque paramètre.
2. Le serveur construit une `recipe_key` **canonique** : items triés par `item_id` (l'ordre de
   sélection n'a pas d'importance), paramètres triés par id.
3. **Clé déjà connue** → renvoie directement `output_item_id`, consomme les inputs, donne l'objet.
   Instantané, aucun appel IA.
4. **Clé inconnue** → appelle l'IA avec le contexte complet (atelier, items en entrée avec leurs
   descriptions/catégories/stats, paramètres choisis) pour générer un nouvel `Item` cohérent avec ces
   ingrédients. Le résultat est persisté à la fois comme nouveau template `Item` **et** comme nouvelle
   entrée de cache — cette combinaison exacte donnera désormais toujours le même résultat, à tout le
   monde, sans repasser par l'IA.
- Déterministe par construction : pas de tirage aléatoire sur le résultat, la variance vient du nombre
  quasi infini de combinaisons possibles, pas du hasard sur une combinaison donnée.

---

## Pipeline de génération : squelette d'étage

Deux couches distinctes pour générer un étage, décidées pour garder la cohérence sans figer le
contenu en un seul bloc monolithique :

1. **Bibliothèque de Templates** (Monster/Resource/Item/NPC/Skill...) — grandit au fil du temps,
   réutilisée d'une zone à l'autre et d'un étage à l'autre. Lors de la génération d'une zone, l'IA pioche
   en priorité dans les templates existants cohérents (thème/niveau) ; elle n'en crée un nouveau que si
   nécessaire (aucun monstre du thème/niveau adéquat, objet de quête spécifique...).
2. **Composition de zone** — quelles `Entry` dans cette zone, avec quels templates, quelle rareté,
   quelles conditions. Le vrai travail créatif à chaque génération.

**Portée d'un appel :** le **squelette de l'étage entier** (liste des zones, connexions, conditions de
déblocage) se génère **en un seul appel** — les connexions et les zones cachées à convergence n'ont de
sens que si tout le graphe est pensé d'un coup. Le **contenu de chaque zone** (ses `Entry`) peut ensuite
se générer **zone par zone**, tout de suite après ou à la demande plus tard.

Le risque de cette séparation en deux temps : une condition posée dans le squelette (ex. "il faut la
Clé Runique pour débloquer les Ruines") doit être **remplissable quelque part** une fois les zones
générées indépendamment — sans garde-fou, rien n'empêche qu'aucune zone ne finisse par fournir cette
clé. Le squelette produit donc, en plus de la liste des zones, deux artefacts pour combler ce risque :

### `generation_brief` (par zone)

Pas du texte joueur — des indications pour l'appel qui remplira cette zone plus tard.

```json
{
  "id": "foret_sombre",
  "name": "Forêt Sombre",
  "type": "wilderness",
  "generation_brief": {
    "level_range": [2, 5],
    "tags": ["foret", "loups", "runes"],
    "content_intent": "Zone de combat principale du début d'étage : mobs communs + un mob rare + un boss de groupe optionnel. Un PNJ isolé sert de couleur, pas de vendeur.",
    "provides": [
      { "type": "item", "item_id": "cle_runique", "via": "loot_table du boss de groupe de cette zone" }
    ]
  }
}
```

### `condition_manifest` (au niveau du Floor)

Trace chaque condition **non-triviale** (référence à un item/boss/quête devant exister quelque part)
jusqu'à la zone censée la fournir.

```json
{
  "condition_manifest": [
    {
      "condition": { "type": "item", "item_id": "cle_runique", "state": "owned" },
      "used_by": ["zone:ruines_architectes.unlock_condition"],
      "resolved_by": "zone:foret_sombre (loot du boss de groupe)"
    }
  ]
}
```

Les conditions "auto-suffisantes" (`level`, `zone` déjà débloquée) n'ont pas besoin d'entrée dans le
manifeste — elles ne dépendent d'aucun contenu à créer quelque part.

**Notes :**
- `generation_brief` et `condition_manifest` sont des artefacts de **génération**, distincts du contenu
  final joué (le `description` reste le texte joueur). À trancher plus tard : les garder après usage
  (utile pour enrichir une zone existante ou vérifier la cohérence de l'étage) ou les jeter une fois le
  contenu généré — je pencherais pour les garder, coût de stockage négligeable face à l'utilité.
- L'appel "contenu de zone" (phase 2) reçoit le `generation_brief` de sa zone **et** les entrées du
  `condition_manifest` qui le concernent (`resolved_by` pointant vers lui) — il sait précisément quoi
  produire pour que le reste de l'étage reste cohérent, sans avoir besoin du contexte complet des autres
  zones.

---

## Ce qui reste à définir

- Suivi de la progression de quête par joueur (état actif/objectifs partiels/terminé, comment
  `kill_count`/`gather_count` s'incrémentent en jouant) — état dynamique, hors périmètre du contenu statique.
- Template `discovery` : **volontairement hors périmètre V1** (mis de côté pour garder le socle simple
  au démarrage, à reprendre plus tard).
- État dynamique des annonces de l'Autel des Ventes (qui vend quoi, à quel prix, où ça vit côté serveur).
- Contenu réel des `loot_table` (monstres) / `yields` (ressources) : quels `item_id` précis, pondérations
  — le template Item existe, reste à remplir les tables avec de vrais objets.
- Où et comment le cache de craft est stocké/interrogé côté serveur (base de recettes globale) — posé
  côté design, pas côté infra.
- Mécanisme d'XP pour `chasse` (lié au combat/loot, pas à une `resource`) — à préciser avec `loot_table`.
- Système de compétences joueur : contenu réel de l'arbre/pool de sélection (quelles compétences existent
  précisément, courbe de puissance par niveau) — la structure `Skill`/`Effects` est posée, reste à
  remplir. Nombre exact de slots actifs (4 à 8) à trancher en testant.
- Esquive / parade — **V2**, schéma déjà prévu (`telegraph_seconds`, `avoidable_by`) mais non actif en V1.
- Système d'aggro/ciblage pour les combats de groupe — absent en V1, à envisager si besoin plus tard.
- État dynamique côté serveur (comment `shared` vs `per_player` est réellement stocké/synchronisé,
  et comment un combat de groupe synchronisé en direct est diffusé à tous les joueurs présents).
- Pipeline de génération IA (prompt, portée d'un appel — étage entier vs zone par zone).
