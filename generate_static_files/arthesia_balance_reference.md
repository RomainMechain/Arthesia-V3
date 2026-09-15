# Arthesia — Référence d'équilibrage

> Point de départ chiffré, pas une vérité gravée dans le marbre — à calibrer en testant. Sert d'ancrage
> commun pour **tous** les prompts de génération (monstres, items, compétences, craft) afin qu'ils
> cessent d'improviser des nombres chacun dans leur coin. Extraits copiés directement dans les prompts
> concernés (pas juste référencés) puisque chaque appel API doit rester autonome.

---

## Système de points de stat (joueur)

Chaque niveau, le joueur gagne **6 points** à répartir librement entre `hp`/`atk`/`def`/`agi`/`int`/`sag`.
Un point n'a pas la même valeur selon la stat (sinon un point en HP et un point en AGI se vaudraient) :

| Stat | Valeur d'1 point |
|---|---|
| `hp` | +25 |
| `atk` | +2 |
| `def` | +2 |
| `agi` | +1 |
| `int` | +2 |
| `sag` | +1 |

**Stats de base** (niveau "0", avant tout point dépensé — le plancher du personnage) :
`hp: 80, atk: 8, def: 5, agi: 5, int: 6, sag: 5`.

Un joueur **généraliste** (1 point/stat/niveau, les 6 points bien répartis) suit la **courbe de
référence** ci-dessous. Un joueur **spécialisé** (ex. 6 points en HP à chaque niveau) s'en écarte
fortement dans un sens — c'est voulu, c'est la diversité de builds, pas quelque chose à corriger. Toute
la calibration monstres/items ci-dessous se fait contre le profil généraliste, comme ancrage neutre.

### Courbe de référence (généraliste)

| Niveau | HP | ATK | DEF | AGI | INT | SAG |
|---|---|---|---|---|---|---|
| 1 | 105 | 10 | 7 | 6 | 8 | 6 |
| 3 | 155 | 14 | 11 | 8 | 12 | 8 |
| 5 | 205 | 18 | 15 | 10 | 16 | 10 |
| 7 | 255 | 22 | 19 | 12 | 20 | 12 |
| 10 | 330 | 28 | 25 | 15 | 26 | 15 |

Formule générale (extensible à n'importe quel niveau, utile au-delà de l'étage 1) :
```
hp  = 80 + 25 × niveau
atk = 8  + 2  × niveau
def = 5  + 2  × niveau
agi = 5  + 1  × niveau
int = 6  + 2  × niveau
sag = 5  + 1  × niveau
```

---

## Constantes des formules de combat

| Constante | Valeur | Effet à def/int/sag/agi typiques |
|---|---|---|
| `K_def` | 20 | def=7 → 26% mitigation, def=25 → 56%, def=60 → 75% (rendements décroissants) |
| `K_int` (avec `base_mana: 50`) | 4 | int=8 → 82 mana, int=26 → 154 mana |
| `K_sag` (avec `base_regen: 1`) | 0.15 | sag=6 → 1,9/s, sag=15 → 3,25/s |
| `K_agi` (avec `base_cooldown: 2.0s`) | 0.03 | agi=6 → 1,69s, agi=15 → 1,38s |
| `K_sell` | 0.35 | Rachat au serveur = 35% du `base_value` (toujours < prix de vente, jamais d'argent infini) |

---

## Monstres : multiplicateurs par rôle

Stats d'un monstre = stats du joueur généraliste **au même niveau**, multipliées selon son `role` :

| `role` | HP × | ATK × | DEF × |
|---|---|---|---|
| `normal` | 1.5 – 2.2 | 0.9 – 1.7 | 0.8 – 1.3 |
| `elite` (mini-boss, `group_required`) | 5 – 9 | 1.5 – 2.2 | 1.2 – 1.8 |
| `boss` | 15 – 25 | 2 – 3 | 1.3 – 2.2 |

**Vérifié contre le contenu déjà généré :** Loup Alpha (lvl4, `normal`) → HP ×1.89, ATK ×1.67, DEF ×0.98
— dans la fourchette. Gardien de la Clairière (lvl6, `boss`) → HP ×18.3, ATK ×2.83, DEF ×1.58 — dans la
fourchette. Les deux exemples déjà écrits dans le modèle de données tombent naturellement dedans, bon
signe que ces plages sont réalistes plutôt qu'arbitraires.

---

## Équipement : budget de stats en "points équivalents"

Pour rester comparable au système de points du joueur, le total des `stat_bonuses` d'un équipement se
raisonne en **points équivalents** (même table de valeur par point que plus haut) plutôt qu'en chiffres
bruts mélangés entre stats différentes :

```
points_equivalents = hp_bonus/25 + atk_bonus/2 + def_bonus/2 + agi_bonus/1 + int_bonus/2 + sag_bonus/1
```

| Cas | Budget |
|---|---|
| Équipement normal (cohérence normale, voir prompt craft) | ≈ 1 niveau d'équivalent (6 points) |
| Craft "cohérence rare" (voir prompt craft) | Jusqu'à ≈ 2-3 niveaux d'équivalent (12-18 points) |
| Jamais | Au-delà de ~3 niveaux d'équivalent, quelle que soit la justification |

**Vérifié contre le contenu déjà généré :** Sceau du Cœur Scellé (craft "cohérence rare", lvl9) → 18,8
points ≈ 3,1 niveaux d'équivalent — pile dans la fourchette haute autorisée, alors que cette règle
n'avait pas encore été chiffrée au moment de sa génération. Épée Rouillée (lvl1, exemple du doc de
modèle) → 2,5 points ≈ 0,4 niveau — nettement en dessous, cohérent avec un objet volontairement
modeste ("elle a connu de meilleurs jours").

## Items : `base_value` indicatif par niveau

```
material   ≈ 5  × niveau
consumable ≈ 15 × niveau
equipment  ≈ 30 × niveau
```
Cas exceptionnel (craft cohérence rare) : jusqu'à ×2-3 ce montant, cohérent avec la règle déjà posée
dans le prompt de craft ("au-dessus de la somme des ingrédients").

---

## Compétences : rappel des coefficients (déjà posé dans le prompt dédié)

`power`/`amount_power` sont des coefficients multiplicateurs de la stat du lanceur (`atk` ou `int` selon
`damage_type`), jamais une valeur brute :

| Palier | `power` |
|---|---|
| Simple | 0.8 – 1.5 |
| Difficile | 1.5 – 2.5 |
| Exceptionnelle | 2.5 – 4, jamais au-delà |

Exemple concret avec la courbe ci-dessus : un joueur niveau 5 (généraliste, `atk: 18`) avec une
compétence `power: 2.0` (palier Difficile) inflige `2.0 × 18 = 36` dégâts avant mitigation adverse.

---

## Ce que ça ne couvre pas encore

- Aucune vraie simulation/playtest — ces chiffres sont dérivés par recoupement logique, pas éprouvés en
  jeu. À ajuster dès que des combats réels (ou simulés) sont possibles.
- Le comportement de la courbe **au-delà de l'étage 1** (niveaux 11+) — la formule est linéaire et
  extensible, mais rien ne garantit qu'une progression purement linéaire reste satisfaisante à très haut
  niveau (les jeux typiques ralentissent ou incurvent la progression). À revisiter.
- L'agrégation des bonus de **plusieurs équipements simultanés** (le budget ci-dessus est pièce par
  pièce — un joueur full équipé cumule plusieurs items, jamais vérifié si le total cumulé reste sain).
- L'impact des **titres et passifs** sur cette courbe (ils s'additionnent par-dessus, non pris en compte
  ici).
