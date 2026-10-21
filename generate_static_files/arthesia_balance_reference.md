# Arthesia — Référence d'équilibrage

> Ce document décrit les règles et les intentions. Les chiffres font foi dans le code :
> - `back/balance.py` : stats, XP, métiers, PV, défaite, profils des monstres ;
> - `back/content_targets.py` : chiffres cibles du contenu généré ;
> - `back/combat_engine.py` : formules de combat.
>
> Les générateurs (squelette excepté) joignent à chaque appel un bloc `balance_targets` calculé par
> `content_targets.py` pour les niveaux concernés. Les prompts expliquent les règles et disent au
> modèle de prendre les valeurs dans ce bloc ; il n'a aucun calcul à faire. Après génération,
> `content_targets.check_template` signale les monstres et objets trop loin de leurs cibles.
>
> Pour changer l'équilibrage : modifier les constantes, relancer le simulateur
> (`generate_static_files/balance/simulate_combat.py`), puis mettre ce document à jour.

---

## Intentions de jeu

- Une tour façon SAO : des étages ajoutés au fil du temps, **environ 10 niveaux par étage**
  (étage N : niveaux 10(N−1)+1 à 10N, le boss au niveau le plus haut).
- Un étage dure plusieurs jours ou semaines en jouant un peu chaque jour.
- Un monstre normal n'est pas très dur, mais on peut perdre. La difficulté monte quand on s'enfonce
  dans l'étage. Les élites et les boss sont très durs : tout le monde ne les bat pas.
- Durée d'un combat : environ 1 min contre un normal, 1-2 min contre une élite, 3-5 min contre un boss.
- Jeu solo pour l'instant ; le jeu en groupe viendra plus tard.
- L'équipement fait 40-50 % de la puissance. Les builds hybrides restent viables.
- XP : 70 % en combat, 30 % en quêtes. Les métiers montent plus lentement, pour pousser à se
  spécialiser et à commercer. L'or reste modéré, avec des dépenses.
- Les élites et les boss ont des attaques télégraphiées : l'habileté du joueur compte autant que son
  build. Une attaque ne peut pas toujours être parée.

---

## Personnage

**Stats de base** (avant tout point) : `hp 80, atk 8, def 5, agi 5, int 6, sag 5`.
**6 points par niveau**, à répartir librement. Valeur d'un point :

| Stat | Valeur d'un point | Rôle |
|---|---|---|
| `hp` | +25 | PV |
| `atk` | +2 | dégâts physiques |
| `def` | +2 | réduction des dégâts |
| `agi` | +1 | vitesse de l'attaque de base, critiques, esquive |
| `int` | +2 | dégâts magiques, mana max |
| `sag` | +2 | soins, boucliers, régénération de mana |

**Points équivalents** : `hp/25 + atk/2 + def/2 + agi + int/2 + sag/2`. Toute comparaison de puissance
(équipement, passifs) se fait dans cette unité.

### Joueur de référence

Les monstres sont calibrés contre un joueur de référence **équipé**. Ses points de niveau suivent
`balance.REFERENCE_WEIGHTS`, un hybride un peu plus porté sur l'ATK, les PV et la DEF (le build
« aventurier » du simulateur). Son équipement vaut 45 % de sa puissance totale
(`balance.GEAR_SHARE`), soit 0,45 / 0,55 fois ses points de niveau.

| Niveau | PV | ATK | DEF | AGI | INT | SAG | Mana |
|---|---|---|---|---|---|---|---|
| 1 | 137 | 14 | 10 | 6 | 8 | 8 | 76 |
| 5 | 364 | 35 | 28 | 12 | 15 | 19 | 130 |
| 10 | 648 | 62 | 50 | 19 | 24 | 32 | 198 |
| 20 | 1 216 | 117 | 96 | 32 | 42 | 60 | 335 |
| 30 | 1 784 | 172 | 141 | 46 | 60 | 87 | 471 |
| 50 | 2 921 | 281 | 232 | 73 | 97 | 141 | 744 |

Les monstres sont calqués sur ce build et non sur un généraliste. Au niveau 1, les stats de base
dominent et tous les builds se ressemblent ; l'écart entre deux builds grandit ensuite avec le niveau.
Avec des monstres calqués sur un autre build que celui de calibration, l'équilibrage dériverait :
un boss passait de 0 % de victoires au niveau 10 à 95 % au niveau 50.

Contre un monstre normal, tous les builds gagnent. Contre une élite ou un boss au même niveau, le
résultat dépend surtout du build :

| Build | Élite | Boss |
|---|---|---|
| aventurier (référence) | ~60 % | ~45-65 % |
| guerrier, mage, tank | ~95-100 % | ~100 % |
| assassin | ~15-20 % | ~2 % |
| généraliste, lame-sort (ATK = INT) | 0 % | 0 % |

Les builds ATK = INT perdent parce que leurs dégâts se répartissent sur deux stats. Il leur faudra
des compétences hybrides.

---

## Combat

Toutes les formules sont dans `combat_engine.py`. Celles qui dériveraient avec des stats linéaires
sont relatives au niveau. Leurs constantes sont proportionnelles aux stats du joueur de référence
(DEF ≈ 5 + 4,5 × niveau, AGI ≈ 5 + 1,4 × niveau) : il garde ~43 % de réduction et ~1,42 s par
attaque à tous les niveaux.

| Règle | Formule |
|---|---|
| Réduction des dégâts | `def / (def + 6,6 + 6 × niveau de l'attaquant)` |
| Recharge de l'attaque de base | `2,0 s / (1 + agi / (agi + 7,4 + 2 × niveau))` |
| Attaque de base | `0,4 × max(ATK, INT)` |
| Mana max | `50 + 10 × niveau + 2 × INT` |
| Régénération de mana | `1 + 0,2 × niveau + 0,05 × SAG` par seconde |
| Compétence de dégâts | `power × ATK` (physique) ou `power × INT` (magique) |
| Soin | `power × SAG` |
| Bouclier | `amount_power × SAG` (`× ATK` si la compétence est physique) |
| Attaque de monstre | `power × ATK du monstre` (`× INT` si magique) |

Chaque coup passe par `roll_hit` : esquive, puis variance de ±8 %, puis critique (×1,5).
Avec `edge = (agi_att − agi_déf) / (agi_att + agi_déf)` :
critique `0,05 + 0,15 × edge`, esquive `0,05 + 0,10 × edge`.

---

## Monstres

Stats d'un monstre = stats du joueur de référence **de son niveau**, avec une INT égale à son ATK
(`balance.monster_base_stats`), × le profil de son rôle (`balance.ROLE_PROFILES`, calibré par le
simulateur aux niveaux 5 et 30) :

| Rôle | PV × | ATK × | DEF × | AGI/INT × | Puissance P de l'attaque principale | XP |
|---|---|---|---|---|---|---|
| `normal` | 2,15 | 1,0 | 1,0 | 1,0 | 0,21 (× profondeur) | ×1 |
| `elite` | 2,99 | 1,1 | 1,2 | 1,1 | 0,252 | ×4 |
| `boss` | 6,0 | 1,2 | 1,4 | 1,2 | 0,13 | ×15 |

Cibles qui ont servi à la calibration (joueur de référence au même niveau) :

| Rôle | Durée d'une victoire | Résultat visé |
|---|---|---|
| `normal` | ~65 s | perd ~50 % de ses PV, sans potion |
| `elite` | ~100 s | gagne ~55 % du temps, avec 2 potions, en parant la moitié des annonces |
| `boss` | ~240 s | gagne ~40 % du temps, avec 5 potions, en parant la moitié des annonces |

**Attaques.** L'attaque principale a la puissance P et une recharge de 2,5 s. Une attaque plus lente
peut frapper plus fort, au même rythme : `power ≈ P × recharge / 3,5`. Par exemple, `2P` à 7 s,
`1,5P` à 5 s, ou `3P` à 10 s pour la furie d'un boss. Un boss a 2 ou 3 phases
(`hp_above_percent`), et sa dernière phase débloque sa grosse attaque.

**Attaques télégraphiées** (élites et boss, `balance.TELEGRAPHED_MOVES`). La grosse attaque
s'annonce pendant `telegraph_seconds`, puis frappe. Sa parade (`avoidable_by`) :
- `esquive` : le bouton Esquiver, pendant l'annonce. Il a sa propre recharge de 8 s : deux annonces
  rapprochées ne s'esquivent pas toutes les deux, et une esquive lancée hors annonce est perdue ;
- `interruption` : une compétence de dégâts lancée pendant l'annonce, ce qui demande d'en garder une
  prête et le mana pour la lancer.

Une élite a une attaque à esquiver (son assaut, 1,5 s d'annonce). Un boss a un assaut à interrompre
(1,5 s) et une furie à esquiver (2 s, dernière phase). Ces attaques frappent 2,5 fois plus fort
(`TELEGRAPH_POWER_MULTIPLIER`), et la calibration suppose un joueur moyen qui pare la moitié des
annonces (`AVERAGE_AVOID_RATE`). Joueur de référence contre le boss de niveau 10 :

| Annonces parées | 0 % | 50 % | 80 % | 100 % |
|---|---|---|---|---|
| Victoires | 0 % | ~40 % | ~90 % | 100 % |

Elles réduisent aussi l'écart entre builds : l'assassin passe de ~25 % à ~65 % de victoires contre un
boss quand il pare 80 % des annonces. Les builds mixtes (ATK = INT) restent à 0 % : il leur faut des
compétences hybrides.

**Profondeur de la zone** (`DIFFICULTY_TIERS`). Elle ne joue que sur la puissance des attaques des
monstres **normaux**. Les PV ne changent pas, donc la durée des combats non plus.

| Profondeur | Puissance × | PV perdus par victoire (référence) |
|---|---|---|
| `entree` (premier tiers de l'étage) | 0,7 | ~36 % |
| `coeur` | 1,0 | ~50 % |
| `profondeurs` (dernier tiers) | 1,15 | ~58 % |

La profondeur vient de `generation_brief.difficulty_tier` dans le squelette. Sans ce champ, elle
est déduite du nombre de sauts depuis la ville de départ.

Le résultat contre un boss reste très sensible : 1 % de puissance en plus ou en moins déplace la
part de victoires de 20 points. Pour un monstre généré, on ajuste ses PV au simulateur
(`template --monster <id> --potions …`) plutôt que de les deviner.

---

## Progression

### XP du personnage (`balance.py`)

La courbe est pensée en temps de jeu. Un niveau dure environ 15 min au niveau 1, 1 h au niveau 11,
puis +1,2 % par niveau. On compte 12 monstres normaux par heure, régénération comprise.

| Niveau | XP pour monter | XP d'un normal | Durée du niveau |
|---|---|---|---|
| 1 | 43 | 10 | ~15 min |
| 10 | 298 | 17 | ~1 h |
| 50 | 5 345 | 174 | ~1 h 45 |
| 100 | 178 745 | 3 201 | ~3 h 15 |

Un étage complet prend environ 6 h de jeu (étage 1), 17 h (étage 5) et 31 h (étage 10).

- **Écart de niveau** : un monstre plus fort rapporte +10 % par niveau d'écart (au plus +50 %) ; un
  plus faible rapporte −20 % par niveau (au moins 10 %).
- **Quêtes** (`quest_xp`) : une part du seuil du niveau de la quête. Une petite quête vaut 3 %, une
  normale 6 %, une majeure 12 %. Ces parts sont faites pour **~30 quêtes par étage** (2 à 4 par zone
  ordinaire, 4 à 6 en ville, ampleurs mêlées) : l'étage 1 en compte 35, qui apportent 29 % de l'XP.

### Métiers

Les métiers ont une courbe à part : environ 50 récoltes de sa tranche par niveau de métier, à tous
les niveaux. Une fabrication vaut 4 récoltes. **Le serveur calcule l'XP d'une action** à partir du
niveau de la ressource ou de l'objet fabriqué (+10 % par niveau d'objet), le `xp_reward` des
templates n'est plus lu. Un objet sous le niveau du métier rapporte 25 % de moins par niveau de
métier d'écart, 25 % au minimum : récolter sans fin la ressource la plus facile ne paie pas.

### PV hors combat et défaite

- Les PV perdus restent perdus d'un combat à l'autre. Ils remontent entièrement en 10 min, 3 fois
  plus vite en ville. Monter de niveau remet les PV au maximum.
- **Défaite** : perte de 10 % de l'XP du niveau, sans jamais perdre de niveau. Perte de 3 % de l'or,
  au plus 20 × le niveau. On se relève à 20 % des PV.

---

## Objets et économie (`content_targets.py`)

### Équipement

Les sept emplacements de combat (`weapon`, `head`, `chest`, `gloves`, `boots`, `ring`, `amulet`) se
partagent la part de l'équipement. Une pièce de niveau L vaut donc environ **0,7 × L points
équivalents** : 0,7 au niveau 1, 3,5 au niveau 5, 7 au niveau 10, 21 au niveau 30. Une panoplie
complète au niveau du joueur donne les 45 % prévus.

- Un objet ordinaire (butin, boutique) suit ce budget.
- Un craft très cohérent avec le lore peut aller de ×1,5 à ×2, jamais au-delà.
- L'outil (`tool`) ne donne pas de stats de combat.

### Valeur de base

En or : `material 5 × niveau`, `consumable 15 × niveau`, `equipment 30 × niveau`. Le serveur rachète
à 35 % (`K_SELL`). Une boutique vend toujours au moins au `base_value`, donc on ne peut pas faire
d'or en boucle.

### Consommables

- Un soin rend 30 % des PV du joueur de référence de son niveau : 41 au niveau 1, 194 au niveau 10.
- Une potion de mana rend 35 % de son mana.
- Un bonus de stat donne 10 à 20 % pendant 30 à 60 s.

### Quêtes

| Ampleur | XP | Or | Objectifs (monstres ou récoltes) |
|---|---|---|---|
| `small` | 3 % du niveau | 3 × niveau | 3-5 |
| `normal` | 6 % du niveau | 6 × niveau | 6-10 |
| `major` | 12 % du niveau | 15 × niveau | 12-20 |

L'or vient surtout des quêtes et de la revente du butin. Il se dépense en boutique, à l'Autel des Ventes,
lors des défaites, et pour réinitialiser ses points de stat (gratuit la première fois, puis
30 or × niveau, en ville).

---

## Compétences (`content_targets.py`)

Un joueur équipe au plus **4 compétences actives** et 4 passives (`balance.MAX_ACTIVE_SKILL_SLOTS`). Le
simulateur calibre les monstres avec un kit de 4 compétences (rapide, moyenne, lente, soin). Le mana
limite déjà le rythme, mais au-delà de 4, les affaiblissements et les compétences en plus rendraient le
contenu trivial.

**Compétences de départ** (débloquées par le niveau seul, équipées d'office tant qu'il reste un
emplacement) : Frappe et Trait de Lumière au niveau 1, Premiers Soins au niveau 2, Coup Puissant et
Éclat d'Aube au niveau 3. Avec la seule attaque de base, un personnage de niveau 1 perd tous ses
combats.

**Effet d'une compétence = `base` + `power` × stat**, avec un **coût en mana fixe**. Chaque compétence a
un niveau (`level`, celui de son déblocage). À ce niveau, pour le joueur de référence, l'effet total
vaut `P × stat`, et la `base` en fait 60 % (`balance.SKILL_BASE_SHARE`) : `base = 0,6 × P × stat de
référence`, `power = 0,4 × P`. La stat de référence est l'ATK pour les dégâts (un mage a autant d'INT
qu'un guerrier d'ATK) et pour un bouclier physique, la SAG pour un soin ou un bouclier magique.

Comme la base ne grandit pas, une compétence ancienne perd de son importance : au niveau 10, Frappe
(niveau 1) fait ~40 dégâts quand une compétence du niveau 10 de même recharge en fait ~75 ; au
niveau 20, 66 contre 140. Les compétences des étages suivants prennent le relais, et les stats
gardent leur intérêt par la part `power`, l'attaque de base, la défense et l'AGI. Le simulateur
calibre les monstres avec un kit débloqué 3 niveaux plus tôt (`SKILL_KIT_LEVEL_LAG`), coûts fixes
compris. Contre les monstres normaux, ce kit est de palier simple ; contre les élites et les boss, la
moitié est de palier difficile (`REFERENCE_KIT_TIERS`), car le joueur a déjà gagné des compétences
d'étage quand il les affronte.

**Compétences hybrides** (`damage_type: "hybrid"`) : leur stat vaut 2 × la plus faible d'ATK et d'INT
(`combat_engine.HYBRID_STAT_FACTOR`). Pour un spécialiste, elle reste sous sa meilleure stat (aventurier
48 contre 62 d'ATK au niveau 10) : il garde ses compétences mono-stat, et la calibration n'en dépend
pas. Un build ATK = INT en profite pleinement : au niveau 10, avec trois hybrides et un soin, le
lame-sort bat le Héraut de l'Aube 93 % du temps en jouant moyennement (59 % avec des compétences
mono-stat). Le « 1 point partout » reste le build le plus faible.

Le total `P` d'une compétence de dégâts vaut **`0,31 × recharge^0,75` × son palier** :

| Recharge | Simple (×1) | Difficile (×1,25) | Exceptionnelle (×1,6) |
|---|---|---|---|
| 3 s | 0,71 | 0,88 | 1,13 |
| 6 s | 1,19 | 1,49 | 1,90 |
| 10 s | 1,74 | 2,18 | 2,79 |
| 15 s | 2,36 | 2,95 | 3,78 |
| 20 s | 2,93 | 3,66 | 4,69 |

- **Soins et boucliers** : la moitié de la puissance d'une attaque de même recharge.
- **Coût en mana** : 2 % du mana du joueur de référence par seconde de recharge, entre 8 % et 25 %.
  Il est donné en valeur absolue par niveau dans `balance_targets`.
- **Buffs et debuffs** : 10 à 30 % pendant 8 à 20 s. Une relance rafraîchit l'effet, elle ne le
  cumule pas.
- **Passifs permanents** (`stat_bonus`) : une part de la puissance totale du joueur de référence au
  niveau de déblocage. Simple 4 %, difficile 8 %, exceptionnelle 15 % ; au moins 1,5 / 3 / 6 points
  équivalents.
- **Passifs en pourcentage** (`stat_bonus_percent`) : 3-5 %, 6-10 % ou 12-20 % selon le palier.
- **Titres** : un seul porté à la fois, des effets permanents de 6 à 12 % sur une ou deux stats, entre une
  passive difficile et une exceptionnelle. Leur condition est un accomplissement composé et secret, jamais
  un boss d'étage (vaincu une fois pour tout le serveur).

---

## Ce qui reste à faire

- Le jeu en groupe : les élites `group_required` se battent encore en solo.
- Les templates actuels ont été générés avec les anciennes règles ; ils seront régénérés.
