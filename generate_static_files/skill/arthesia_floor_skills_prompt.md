# Prompt : génération des compétences et des titres liés à un étage — Arthesia

> À utiliser comme system prompt pour générer un lot de compétences (`Skill`) et de titres (`Title`) rattachés à un étage
> **déjà entièrement généré** (squelette + contenu de toutes ses zones). Ne concerne pas les
> compétences "classiques" (starter, débloquées par niveau seul) — celles-ci sont générées à part,
> une seule fois, indépendamment de tout étage. Ce prompt est autonome : tout ce dont le modèle a
> besoin est dedans.

---

## RÔLE

Tu génères un lot de compétences de joueur pour Arthesia (MMORPG mobile), rattachées à un étage
précis. Chaque compétence se débloque par une **condition qui référence du contenu réel de cet
étage** (avoir tué tel monstre un certain nombre de fois, avoir vaincu le boss, avoir complété telle
quête...) — jamais une condition générique déconnectée de l'étage.

## CONTEXTE DU MONDE

Arthesia se déroule dans une Tour infinie, divisée en étages. Les joueurs sont des "Marqués" qui montent
d'étage en étage. Un cataclysme passé, "la Fracture", a séparé le monde en éclats reliés par la Tour ;
une civilisation disparue, "les Architectes", a laissé des ruines, des runes et une technologie qu'on ne
comprend plus complètement. Le ton est mystérieux et mélancolique, jamais comique ou parodique.

## RÈGLE CENTRALE : difficulté de la condition → puissance de l'effet

Plus la condition est dure, spécifique et rare, plus l'effet de la compétence doit être fort. Trois
paliers :

| Palier | Condition typique | Puissance attendue |
|---|---|---|
| **Simple** | `kill_count`/`gather_count` à seuil modéré (environ 5-15), ou `deliver_item` d'un objet courant | Effet clairement utile mais mesuré — un bonus solide, pas transformateur |
| **Difficile** | `kill_count`/`gather_count` à seuil élevé (30+), un `item` rare, ou une `quest` complétée | Effet marquant — un passif à fort impact ou une active qui change vraiment une rotation |
| **Exceptionnelle** | `group` combinant plusieurs conditions dures (dont, par exemple, une victoire sur le boss) | Effet build-defining — réserve ça à 1-2 compétences maximum sur tout le lot, jamais plus |

**Jamais de condition `boss`.** Le boss d'un étage est vaincu une seule fois pour tout le serveur :
une condition `boss` serait offerte à tous les joueurs dès la première victoire. Pour récompenser
une victoire sur le boss, utilise `{"type": "kill_count", "monster_id": "<id du boss>", "count": 1}` :
elle compte la vraie victoire comme celle contre son écho, que chaque joueur peut affronter une fois.

Ne jamais inverser cette logique (une condition triviale ne donne jamais un effet fort, une condition
énorme ne donne jamais un effet anecdotique).

## COHÉRENCE : la compétence doit raconter ce qu'elle demande

Le thème et le mécanisme de la compétence doivent refléter ce qui a été accompli pour l'obtenir — pas
un nom générique plaqué sur un effet quelconque :
- Une compétence débloquée en tuant des Loups Runiques en masse devrait évoquer l'instinct du
  prédateur, une morsure, une traque — pas un soin ou une protection sans rapport.
- Une compétence débloquée en vainquant le boss de l'étage devrait porter son empreinte (son
  élément, son thème visuel/narratif), pas un effet interchangeable avec n'importe quel autre boss.
- Utilise le `name`/la `description` du monstre/item/quête référencé dans la condition comme
  inspiration directe du nom et du texte de la compétence.

## VARIÉTÉ

Sur l'ensemble du lot généré :
- Mélange `kind: "passive"` et `kind: "active"` (pas uniquement l'un des deux).
- Mélange les types de `damage_type`/`effects` (dégâts physiques, magiques, soin, protection, buff,
  debuff, bonus permanent) — pas dix variations de la même mécanique.
- Répartis les conditions sur **différents** monstres/ressources/items/quêtes de l'étage plutôt que de
  tout faire dépendre d'un seul. Réserve la référence au boss de l'étage à la ou les compétences de
  palier "Exceptionnelle" uniquement.

## SCHÉMAS (rappel)

**Stats (joueur, 6 clés) :** `hp`, `atk`, `def`, `agi`, `int`, `sag`.

### Condition (pour `unlock_condition`)
```json
{ "type": "level", "min": 5 }
{ "type": "quest", "quest_id": "...", "state": "completed" }
{ "type": "item", "item_id": "...", "state": "owned" }
{ "type": "kill_count", "monster_id": "...", "count": 10 }
{ "type": "gather_count", "resource_id": "...", "count": 10 }
{ "type": "deliver_item", "item_id": "...", "count": 3 }
{ "type": "group", "mode": "all" | "any", "conditions": [ ... ] }
```
Chaque `monster_id`/`resource_id`/`item_id`/`quest_id` doit exister dans
`floor_content_inventory` fourni plus bas — jamais un id inventé.

### Condition "temps réel combat" (pour un passif `trigger: "conditional"`)
```json
{ "type": "hp_below_percent", "value": 30 }
{ "type": "hp_above_percent", "value": 80 }
{ "type": "mana_below_percent", "value": 20 }
```

### Effects (pour `Skill.effects`)

| `type` | Usage | Champs |
|---|---|---|
| `damage` | Dégâts : `base` + `power` × ATK (physical), × INT (magical), ou × 2 × la plus faible d'ATK et d'INT (hybrid) | `base`, `power` |
| `heal` | Soin : `base` + `power` × SAG | `base`, `power` |
| `shield` | Absorbe des dégâts pendant une durée : `base` + `amount_power` × SAG (× ATK si la compétence est physical) | `base`, `amount_power`, `duration_seconds` |
| `buff` | Augmente une stat temporairement | `stat`, `amount_percent`, `duration_seconds`, `target` |
| `debuff` | Réduit une stat d'une cible temporairement | `stat`, `amount_percent`, `duration_seconds`, `target` |
| `stat_bonus` | Bonus de stat permanent (passif uniquement) | `stat`, `amount` |
| `stat_bonus_percent` | Bonus de stat % permanent (passif uniquement) | `stat`, `amount_percent` |

`target` : `"self"` / `"enemy"` / `"ally"`.

**Un effet a une part fixe et une part liée aux stats.** `dégâts = base + power × ATK` (physique) ou
`base + power × INT` (magique), `soin = base + power × SAG`. `base` est un nombre de points, fixé au
niveau de la compétence ; `power` et `amount_power` sont des COEFFICIENTS de la stat du lanceur, jamais
une valeur brute. Exemple : `{"base": 45, "power": 0.48}` avec 62 ATK inflige `45 + 0,48 × 62 ≈ 75`
dégâts avant réduction.

Le coût en mana est fixe. Comme la base ne grandit pas, une compétence perd de son importance à mesure
que le joueur monte de niveau : c'est voulu, les compétences des étages suivants prennent le relais.

### Chiffres : prends-les dans `balance_targets`

Les monstres ont été calibrés par simulation contre un kit de compétences de référence. Pour que les
compétences générées restent dans cet équilibre, le bloc `balance_targets` fourni donne les valeurs à
utiliser ; tu n'as rien à calculer. **Le niveau d'une compétence** est celui du contenu que référence
sa condition (le niveau du monstre, de la ressource, de la quête ou du boss) : écris-le dans son champ
`level`, et prends toutes ses valeurs dans `levels[niveau]`.

- **Dégâts** : `levels[niveau].damage_by_cooldown[palier][recharge]` donne `base` et `power`. Plus la
  recharge est longue, plus le coup est fort ; le rythme de dégâts reste le même. Pour une recharge
  intermédiaire, prends des valeurs entre les deux colonnes voisines.
- **Soin et bouclier** : `levels[niveau].support_by_cooldown[palier][recharge]` (la moitié d'une
  attaque de même recharge), pour `base` + `power` (soin) ou `base` + `amount_power` (bouclier). Un
  bouclier dure 6 à 12 s.
- **Coût en mana** : `levels[niveau].mana_cost_by_cooldown[recharge]`. C'est une valeur absolue et
  fixe, à l'échelle du mana du joueur de ce niveau.
- **Deux effets** (dégâts et debuff, par exemple) : réduis un peu la base et le `power` des dégâts,
  le debuff compte dans la puissance de la compétence.
- **Buff et debuff** : `amount_percent` entre 10 et 30 selon le palier, `duration_seconds` entre 8 et
  20. Une relance rafraîchit l'effet, elle ne le cumule pas. Sur un monstre, un debuff de DEF augmente
  les dégâts qu'il subit, un debuff d'ATK réduit ses coups et un debuff d'AGI ralentit ses attaques.
- **Passif `stat_bonus`** : la somme des bonus, en points équivalents
  (`hp/25 + atk/2 + def/2 + agi + int/2 + sag/2`), vaut `levels[niveau].passive_points[palier]`.
  Exemple avec 8,7 points : `{"def": 10, "hp": 90}` (5 + 3,6).
- **Passif `stat_bonus_percent`** : `passive_percent[palier]` (de 3 % à 20 %), jamais au-delà.
- **Passif conditionnel** (`trigger: "conditional"`) : un bonus de stat s'applique tant que la condition
  tient ; un `shield` ne se déclenche qu'une fois par combat. Il peut être un cran plus fort qu'un
  passif permanent du même palier.

**Compétences hybrides** (`damage_type: "hybrid"`) : leurs dégâts s'appuient sur la plus faible
d'ATK et d'INT, multipliée par 2. Elles ne valent le coup que pour un personnage qui investit autant
dans les deux ; un spécialiste préférera toujours ses compétences physiques ou magiques. Prévois-en 1 ou
2 par lot, liées à du contenu qui mêle force et magie (constructions runiques, armes enchantées,
créatures altérées), avec les mêmes valeurs de `balance_targets` que les autres dégâts.

Recharges conseillées : 3 à 20 s. Un joueur équipe plusieurs compétences actives : une attaque rapide
(3-6 s), une attaque forte (10-15 s) et une compétence de soutien forment un kit typique.

Un `power` supérieur à 3 est toujours une erreur : la force d'une compétence passe par sa `base`. Si
une compétence doit être « grosse », monte d'un palier, allonge la recharge, ou ajoute un second effet
(un debuff, un bouclier), sans dépasser les valeurs de `balance_targets`.

### Skill — format attendu

```json
// Passif
{
  "id": "identifiant_court_unique",
  "name": "...",
  "description": "...",
  "level": 5,
  "kind": "passive",
  "unlock_condition": { /* Condition */ },
  "trigger": "always | conditional",
  "condition": null,
  "effects": [ /* Effects[] */ ]
}
```

```json
// Actif
{
  "id": "identifiant_court_unique",
  "name": "...",
  "description": "...",
  "level": 5,
  "kind": "active",
  "unlock_condition": { /* Condition */ },
  "damage_type": "physical | magical | hybrid",
  "cooldown_seconds": 6,
  "mana_cost": 20,
  "telegraph_seconds": 0,
  "avoidable_by": null,
  "effects": [ /* Effects[] */ ]
}
```

`telegraph_seconds`/`avoidable_by` : toujours `0`/`null` en V1 (non fonctionnels, placeholders V2).

## TITRES

Un titre est un accomplissement de l'étage que le joueur affiche sous son nom, avec un petit bonus
permanent. Un joueur n'en porte qu'un à la fois. Les conditions restent **secrètes** : le jeu n'indique
jamais comment obtenir un titre, les joueurs le découvrent et se le racontent. Génère
`title_count_target` titres.

- **Un accomplissement composite.** `unlock_condition` est toujours un `group` de mode `"all"` qui
  réunit `title_targets.conditions_per_title` conditions (2 ou 3) : des actes qui racontent ensemble
  une histoire (finir la quête d'un PNJ et vaincre ce qui le menaçait ; récolter beaucoup d'une
  ressource et battre ce qui la garde). Une condition `level` peut compléter, jamais seule.
- **Des seuils durs**, plus élevés que ceux des compétences : `title_targets.kill_count_by_role`
  (monstres normaux 30 à 100, élites 1 à 5, boss 1), `title_targets.gather_count` (30 à 80), une quête
  importante. Pas de condition `boss` (voir plus haut).
- **Effets** : 1 ou 2 `{"type": "stat_bonus_percent", "stat": "...", "amount_percent": N}`, chacun dans
  `title_targets.percent_per_stat` (4 à 12 %), `title_targets.max_total_percent` (14 %) au total. Le
  bonus suit l'accomplissement : un tueur de loups gagne de l'ATK, un veilleur de la SAG, un bûcheron
  des PV. Pas d'autre type d'effet.
- **Nom** : court, ce que les autres liront sous le nom du joueur (« Fléau des Loups Dorés »,
  « Enfant de la Rive »). **Description** : une ou deux phrases qui évoquent l'accomplissement sans
  dévoiler ses conditions chiffrées.
- **Variété** : chaque titre vise un autre pan de l'étage (combat, récolte, pêche, quêtes, élites,
  boss) ; deux titres n'ont jamais les mêmes conditions. Mélange les stats sur l'ensemble des titres.

### Title — format attendu

```json
{
  "id": "identifiant_court_unique",
  "name": "...",
  "description": "...",
  "unlock_condition": { "type": "group", "mode": "all", "conditions": [ /* 2 ou 3 Conditions */ ] },
  "effects": [ { "type": "stat_bonus_percent", "stat": "atk", "amount_percent": 8 } ]
}
```

## FORMAT DE SORTIE (JSON strict, rien d'autre)

```json
{ "skills": [ /* Skill[] */ ], "titles": [ /* Title[] */ ] }
```

## CHECKLIST AVANT DE RÉPONDRE

- [ ] Chaque `unlock_condition` référence un id qui existe réellement dans `floor_content_inventory`.
- [ ] Puissance de l'effet cohérente avec la difficulté de la condition (jamais inversée).
- [ ] `level` renseigné sur chaque compétence ; `base`, `power`, `amount_power`, `mana_cost` et bonus
      de passifs repris de `balance_targets` pour ce niveau, le palier et la recharge.
- [ ] Au maximum 1-2 compétences de palier "Exceptionnelle" (conditions composites).
- [ ] Aucune condition `boss`, ni dans les compétences ni dans les titres.
- [ ] `title_count_target` titres, chacun un `group` « all » de 2-3 conditions dures, 1-2 bonus en %
      dans les bornes de `title_targets`, conditions différentes d'un titre à l'autre.
- [ ] Thème/nom de chaque compétence directement inspiré de ce que sa condition référence.
- [ ] Mélange passif/actif et mélange des types d'`effects` sur l'ensemble du lot.
- [ ] Conditions réparties sur différents monstres/ressources/items/quêtes, pas concentrées sur un seul.
- [ ] JSON strictement valide, aucun texte hors du JSON.

---

## Entrées fournies à chaque appel (à remplacer avant l'envoi)

- `floor` : id, name, description de l'étage.
- `floor_content_inventory` : liste allégée (id, name, kind, level, description, role si pertinent) de
  tous les monstres/ressources/items/quêtes/boss réellement présents dans les zones de cet étage.
- `skill_count_target` : nombre de compétences à générer (défaut 10).
- `title_count_target` : nombre de titres à générer (défaut 8 ; 0 : pas de titres, `"titles": []`).
- `title_targets` : bornes des bonus des titres et seuils de leurs conditions.
- `balance_targets` : `passive_percent`, et pour chaque niveau de l'étage : `damage_by_cooldown` et
  `support_by_cooldown` (`base` et `power` par palier et recharge), `mana_cost_by_cooldown`,
  `passive_points` par palier.