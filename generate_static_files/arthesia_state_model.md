# Arthesia — Modèle de l'état dynamique (serveur)

> Complète `arthesia_data_model.md` (contenu statique) et `arthesia_balance_reference.md` (calibrage).
> Ce document couvre ce qui **change en jouant** : progression joueur, état partagé par zone, état
> global serveur. Volontairement **hors périmètre ici** : la techno serveur/DB elle-même (à explorer
> ensemble séparément) et la synchronisation temps réel des combats de groupe (catégorie D, remise à
> plus tard).

---

## Principe : déblocage permanent vs état vivant

Point de conception qui traverse tout ce document :

- **Un déblocage/accomplissement, une fois vrai, reste vrai pour toujours** — même si la condition qui
  l'a permis cesse de l'être plus tard (revendre l'item, perdre le niveau n'existe pas mais par
  principe). Concerne : zones débloquées, entrées conditionnelles révélées, compétences/titres
  débloqués, quêtes terminées.
- **Un état "vivant" reste réévalué en direct**, jamais figé : conditions "temps réel combat"
  (`hp_below_percent`...), `quest` avec `state: "active"` (une quête active peut redevenir inactive/être
  abandonnée), et bien sûr tout ce qui décrit une ressource/un mob actuellement disponible ou non
  (catégorie B plus bas).

Concrètement : dès qu'une `Condition` de type déblocage (`level`, `item`, `quest: completed`, `zone`,
`boss`, `kill_count`, `gather_count`, `deliver_item`) devient vraie pour un joueur, le serveur
**enregistre l'événement** plutôt que de re-tester la condition à chaque fois qu'il faut savoir si
c'est débloqué.

---

## A. État par joueur

Un document par joueur (`PlayerState`), regroupant tout ce qui lui est propre :

```json
{
  "player_id": "player_ab12",
  "level": 5,
  "xp": 3400,

  "stat_points": {
    "spent": { "hp": 3, "atk": 8, "def": 5, "agi": 4, "int": 4, "sag": 2 },
    "available": 4
  },

  "inventory": {
    "bois_brut": 12,
    "potion_mineure": 3,
    "cle_runique": 1
  },

  "equipment": {
    "weapon": "epee_rouille",
    "head": null,
    "chest": null,
    "gloves": null,
    "boots": null,
    "ring": null,
    "amulet": "sceau_coeur_architecte_scelle",
    "tool": null
  },

  "currency": { "or": 245 },

  "skills": {
    "unlocked": ["coup_puissant", "armure_des_architectes", "veille_du_pecheur"],
    "equipped_active": ["coup_puissant"],
    "equipped_passive": ["armure_des_architectes", "veille_du_pecheur"]
  },

  "titles": {
    "unlocked": [],
    "equipped": null
  },

  "professions": {
    "bucheronnage": { "level": 3, "xp": 210 },
    "forgeron": { "level": 1, "xp": 0 }
  },

  "quests": {
    "premiers_pas_roseaux": {
      "state": "completed"
    },
    "silence_du_lac": {
      "state": "active",
      "objectives_progress": [
        { "objective_index": 0, "current_count": 2 }
      ]
    }
  },

  "unlocks": {
    "zones": ["valclair", "foret_sombre", "rive_miroir"],
    "entries": ["foret_sombre_secret_x"],
    "skills": ["coup_puissant", "armure_des_architectes", "veille_du_pecheur"],
    "titles": []
  },

  "current_zone_id": "rive_miroir"
}
```

**Notes :**
- `stat_points.spent` stocke des **points dépensés par stat**, pas la stat finale calculée — permet de
  recalibrer les valeurs par point (`arthesia_balance_reference.md`) sans casser les personnages
  existants, et ouvre la porte à un respec plus tard sans changer le modèle.
- `unlocks` est **séparé** de `skills.unlocked`/`quests`/etc. même si ça duplique un peu l'info :
  `unlocks` est le **journal des accomplissements permanents** (répond à "cette condition a-t-elle déjà
  été remplie un jour ?"), les autres champs décrivent l'**état actuel utile au jeu** (quel est mon
  niveau de compétence équipé maintenant). Dans une vraie implémentation, `unlocks` pourrait n'être
  qu'un sous-ensemble consulté uniquement au moment d'évaluer une `Condition` — détail d'implémentation,
  pas figé ici.
- `inventory` en `{item_id: quantity}` plutôt qu'une liste d'instances : aucun item n'a de variance
  aléatoire dans ce modèle (un `item_id` donné a toujours les mêmes stats), donc pas besoin d'identifiants
  d'instance uniques par objet possédé.
- `equipment` : un slot vide = `null`. Référence des `item_id`, pas des instances.

---

## B. État partagé par zone

Un document par zone **active** (ou par `(zone_id)` tout court si un seul monde partagé, pas
d'instances) — couvre uniquement les `Entry` à `state_scope: "shared"` :

```json
{
  "zone_id": "foret_sombre",
  "entries": {
    "foret_sombre_loup_alpha": {
      "status": "available",
      "available_again_at": null
    },
    "foret_sombre_loup_commun_01": {
      "status": "depleted",
      "available_again_at": "2026-08-19T14:32:00Z"
    },
    "foret_sombre_fibre_roseau_01": {
      "status": "available",
      "current_stock": 2,
      "max_stock": 3,
      "available_again_at": null
    },
    "foret_sombre_boss_gardien": {
      "status": "defeated"
    }
  }
}
```

**Notes :**
- Seules les `Entry` `shared` apparaissent ici — les `per_player` (coffre trouvé une fois, achat
  individuel...) vivent dans le `PlayerState` du joueur concerné (probablement un `unlocks.entries` ou
  équivalent, pas un nouveau champ ici).
- `status: "defeated"` sur un boss est **permanent** (cohérent avec la décision "tout le serveur, une
  fois battu par n'importe qui" prise plus tôt) — jamais de `available_again_at` pour un boss.
- `current_stock`/`max_stock` uniquement présents si l'`Entry` a un `stock` défini dans son contenu
  statique (sinon `respawn_timer` classique à charge unique, `status` suffit).

---

## C. État global serveur

Pas rattaché à une zone ni à un joueur précis :

### Autel des Ventes (annonces joueur-à-joueur)

```json
{
  "listing_id": "listing_8f2e",
  "seller_player_id": "player_ab12",
  "item_id": "fragment_pierre_taillee",
  "quantity": 1,
  "price": 40,
  "currency": "or",
  "listed_at": "2026-08-19T10:00:00Z"
}
```
Une collection de ces annonces, filtrable par joueur/item. Achat = transaction qui déplace l'item vers
l'acheteur, l'argent vers le vendeur, et supprime (ou décrémente) l'annonce.

### Cache de craft

```json
{ "forge|coeur_architecte:1,fragment_pierre_taillee:1|temperature=moyenne": "noyau_grave_stabilise" }
```
Déjà conçu plus tôt (`generate_craft_result.py` le gère en local via `craft_cache.json`) — à migrer
tel quel vers un stockage serveur partagé le jour où plusieurs joueurs doivent y accéder en même temps
(actuellement un fichier local, donc un seul "joueur" — toi — peut l'utiliser à la fois).

---

## Ce que ça ne couvre pas encore

- La techno serveur/DB réelle (base de données, API, hébergement) — à explorer ensemble séparément.
- La synchronisation temps réel des combats de groupe (catégorie D) — remise à plus tard, comme convenu.
- Le détail de **comment** une condition de déblocage est évaluée et écrite dans `unlocks` (déclenché
  par quel événement précis — un kill, une remise de quête, un achat... — et à quel moment exactement).
- La sécurité/autorité (empêcher un client de mentir sur son propre état — hors périmètre design pur).

**Tranché :** un monde **entièrement partagé**, pas d'instances par groupe/moment — un seul document par
`zone_id` en catégorie B, cohérent avec un vrai MMO plutôt que des donjons à la Diablo. Suffisant pour
le nombre de joueurs visé dans un premier temps ; à revisiter uniquement si la fréquentation
justifie un jour de sharder une zone trop peuplée.