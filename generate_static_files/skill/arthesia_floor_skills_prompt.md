# Prompt : génération des compétences liées à un étage — Arthesia

> À utiliser comme system prompt pour générer un lot de compétences (`Skill`) rattachées à un étage
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
| **Exceptionnelle** | `group` combinant plusieurs conditions dures, ou `boss` vaincu | Effet build-defining — réserve ça à 1-2 compétences maximum sur tout le lot, jamais plus |

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
{ "type": "boss", "zone_id": "...", "state": "defeated" }
{ "type": "kill_count", "monster_id": "...", "count": 10 }
{ "type": "gather_count", "resource_id": "...", "count": 10 }
{ "type": "deliver_item", "item_id": "...", "count": 3 }
{ "type": "group", "mode": "all" | "any", "conditions": [ ... ] }
```
Chaque `monster_id`/`resource_id`/`item_id`/`quest_id`/`zone_id` doit exister dans
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
| `damage` | Dégâts — scale sur ATK (physical) ou INT (magical) | `power` |
| `heal` | Soin — scale sur SAG | `power` |
| `shield` | Absorbe des dégâts pendant une durée — scale sur SAG (ATK si la compétence est physical) | `amount_power`, `duration_seconds` |
| `buff` | Augmente une stat temporairement | `stat`, `amount_percent`, `duration_seconds`, `target` |
| `debuff` | Réduit une stat d'une cible temporairement | `stat`, `amount_percent`, `duration_seconds`, `target` |
| `stat_bonus` | Bonus de stat permanent (passif uniquement) | `stat`, `amount` |
| `stat_bonus_percent` | Bonus de stat % permanent (passif uniquement) | `stat`, `amount_percent` |

`target` : `"self"` / `"enemy"` / `"ally"`.

**⚠️ `power` et `amount_power` sont des COEFFICIENTS MULTIPLICATEURS de la stat du lanceur, jamais une
valeur brute.** `dégâts = power × ATK` (physique) ou `power × INT` (magique), `soin = power × SAG`.
Exemple : avec 44 ATK, `"power": 1.5` inflige `1.5 × 44 = 66` dégâts avant réduction, pas 1,5 et
surtout pas 260.

### Chiffres : prends-les dans `balance_targets`

Les monstres ont été calibrés par simulation contre un kit de compétences de référence. Pour que les
compétences générées restent dans cet équilibre, le bloc `balance_targets` fourni donne les valeurs à
utiliser ; tu n'as rien à calculer. Le niveau d'une compétence est celui du contenu que référence sa
condition (le niveau du monstre, de la ressource, de la quête ou du boss).

- **Dégâts** : `power_by_cooldown[palier][recharge]`. Plus la recharge est longue, plus le coup est
  fort ; le rythme de dégâts reste le même. Pour une recharge intermédiaire, prends une valeur entre
  les deux colonnes voisines.
- **Soin et bouclier** : `support_power_by_cooldown[palier][recharge]` (la moitié d'une attaque de
  même recharge), pour `power` comme pour `amount_power`. Un bouclier dure 6 à 12 s.
- **Coût en mana** : `levels[niveau].mana_cost_by_cooldown[recharge]`. C'est une valeur absolue, déjà
  à l'échelle du mana du joueur de ce niveau.
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

Recharges conseillées : 3 à 20 s. Un joueur équipe plusieurs compétences actives : une attaque rapide
(3-6 s), une attaque forte (10-15 s) et une compétence de soutien forment un kit typique.

Un `power` à deux ou trois chiffres est toujours une erreur. Si une compétence doit être « grosse »,
monte d'un palier, allonge la recharge, ou ajoute un second effet (un debuff, un bouclier), sans
dépasser les valeurs de `balance_targets`.

### Skill — format attendu

```json
// Passif
{
  "id": "identifiant_court_unique",
  "name": "...",
  "description": "...",
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
  "kind": "active",
  "unlock_condition": { /* Condition */ },
  "damage_type": "physical | magical",
  "cooldown_seconds": 6,
  "mana_cost": 20,
  "telegraph_seconds": 0,
  "avoidable_by": null,
  "effects": [ /* Effects[] */ ]
}
```

`telegraph_seconds`/`avoidable_by` : toujours `0`/`null` en V1 (non fonctionnels, placeholders V2).

## FORMAT DE SORTIE (JSON strict, rien d'autre)

```json
{ "skills": [ /* Skill[] */ ] }
```

## CHECKLIST AVANT DE RÉPONDRE

- [ ] Chaque `unlock_condition` référence un id qui existe réellement dans `floor_content_inventory`.
- [ ] Puissance de l'effet cohérente avec la difficulté de la condition (jamais inversée).
- [ ] `power`, `amount_power`, `mana_cost` et bonus de passifs repris de `balance_targets` pour le
      palier, la recharge et le niveau de chaque compétence.
- [ ] Au maximum 1-2 compétences de palier "Exceptionnelle" (conditions composites ou liées au boss).
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
- `balance_targets` : `power_by_cooldown` et `support_power_by_cooldown` par palier, `passive_percent`,
  et pour chaque niveau de l'étage : ATK, INT et SAG du joueur de référence, `max_mana`,
  `mana_cost_by_cooldown`, `passive_points` par palier.