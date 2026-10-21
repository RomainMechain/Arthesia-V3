"""Craft : type d'objet choisi, générations, rareté tirée à chaque fabrication,
enchantement unique, et chaîne d'IA (Gemini, Claude) avec le générateur sans IA
en secours.

Aucun appel réseau : Gemini est simulé avec ``mock``. Les objets de base créés
pendant un test sont écrits dans templates/items puis supprimés.
"""

import json
import random
import unittest
from collections import Counter
from unittest import mock

import fakes
from fakes import STORE

fakes.install()

from fastapi import HTTPException  # noqa: E402

from back import action_routes as ar, craft_service as cs, models  # noqa: E402
from back.balance import RARITIES  # noqa: E402
from back.config import TEMPLATES_DIR  # noqa: E402
from back.content_store import load_template  # noqa: E402
from back.content_targets import check_template, equipment_points, equipment_points_of  # noqa: E402
from back.craft_procedural import BASE_BUDGET_CAP, procedural_craft_item  # noqa: E402
from back.craft_rules import rarity_odds, roll_rarity  # noqa: E402
from back.craft_service import build_recipe_key, recipe_key_doc_id  # noqa: E402
from back.rarity import split_variant_id  # noqa: E402

FORGE = load_template("workshop", "forge")
WEAPON = next(t for t in FORGE["item_types"] if t["id"] == "arme")
ALLOY = next(t for t in FORGE["item_types"] if t["id"] == "alliage")
PARAMETERS = {"temperature": "haute"}
INPUTS = [{**load_template("item", "branche_morte"), "quantity": 2},
          {**load_template("item", "poil_de_loup_dore"), "quantity": 1}]


def gemini_reply(item: dict, status: int = 200) -> mock.Mock:
    payload = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps({"item": item})}]}}]}
    return mock.Mock(status_code=status, text=json.dumps(payload), json=lambda: payload)


def base_count(inventory: dict, base_id: str) -> int:
    """Exemplaires d'un objet de base, tous rangs confondus."""
    return sum(count for item_id, count in inventory.items() if split_variant_id(item_id)[0] == base_id)


def request(inputs: dict, item_type: str | None = "arme", times: int = 1, workshop: str = "forge", parameters=None):
    return models.CraftRequest(player_id="joueur", workshop_id=workshop, item_type=item_type, times=times,
                               parameters=parameters or PARAMETERS,
                               inputs=[{"item_id": item_id, "quantity": quantity} for item_id, quantity in inputs.items()])


class CraftTest(unittest.TestCase):
    def setUp(self):
        fakes.reset()
        # Aucune clé par défaut : chaque test active ce qu'il simule.
        patcher = mock.patch.multiple(cs, GEMINI_API_KEY=None, ANTHROPIC_API_KEY=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def forget_item(self, item_id: str) -> None:
        self.addCleanup(lambda: (TEMPLATES_DIR / "items" / f"{split_variant_id(item_id)[0]}.json").unlink(missing_ok=True))

    def known_recipe(self, inputs: dict, item_type: str, output_id: str, workshop: str = "forge", parameters=None) -> None:
        key = build_recipe_key(workshop, [{"item_id": i, "quantity": q} for i, q in inputs.items()],
                               parameters or PARAMETERS, item_type)
        STORE[f"craft_cache/{recipe_key_doc_id(key)}"] = {"recipe_key": key, "output_item_id": output_id}

    # --- Chaîne d'IA et objet de base ------------------------------------------

    def test_sans_cle_le_generateur_sans_ia_suit_le_type(self):
        item = cs.generate_craft_item(FORGE, WEAPON, PARAMETERS, INPUTS, "recette", 1)
        self.assertEqual((item["origin"], item["category"], item["equipment"]["slot"]), ("procedural", "equipment", "weapon"))
        self.assertEqual((item["generation"], item["rarity"]), (1, "commun"))
        self.assertFalse(check_template({**item, "kind": "item"}))
        self.assertEqual(item, cs.generate_craft_item(FORGE, WEAPON, PARAMETERS, INPUTS, "recette", 1))   # même recette, même objet
        # Il ne cite que l'ingrédient dominant, pas toute la liste.
        self.assertNotIn("Poil de Loup", item["description"])

    def test_gemini_ne_peut_ni_changer_le_type_ni_depasser_les_bornes(self):
        greedy = {"id": "lame_divine", "name": "Lame divine", "category": "consumable", "level": 40, "base_value": 99999,
                  "coherence": "rare", "equipment": {"slot": "ring", "stat_bonuses": {"atk": 500, "agi": 80}}}
        with mock.patch.object(cs, "GEMINI_API_KEY", "cle-de-test"), \
                mock.patch.object(cs.httpx, "post", return_value=gemini_reply(greedy)) as post:
            item = cs.generate_craft_item(FORGE, WEAPON, PARAMETERS, INPUTS, "recette", 1)
        self.assertIn('"slot": "weapon"', post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"])
        self.assertEqual((item["origin"], item["category"], item["equipment"]["slot"]), ("gemini", "equipment", "weapon"))
        self.assertEqual(item["level"], 1)
        self.assertLessEqual(equipment_points_of(item["equipment"]["stat_bonuses"]), equipment_points(1) * BASE_BUDGET_CAP)
        self.assertEqual(item["coherence"], "rare")
        self.assertLess(item["base_value"], 100)

    def test_quota_ou_reponse_invalide_de_gemini(self):
        broken = mock.Mock(status_code=200, json=lambda: {"candidates": [{"content": {"parts": [{"text": "pas du json"}]}}]})
        for reply in (gemini_reply({}, status=429), broken):
            with mock.patch.object(cs, "GEMINI_API_KEY", "cle-de-test"), mock.patch.object(cs.httpx, "post", return_value=reply):
                self.assertEqual(cs.generate_craft_item(FORGE, WEAPON, PARAMETERS, INPUTS, "r", 1)["origin"], "procedural")

    def test_un_craft_commun_ne_rapporte_pas(self):
        rich = [{**i, "base_value": 40} for i in INPUTS]   # 120 or d'ingrédients
        item = cs.generate_craft_item(FORGE, ALLOY, PARAMETERS, rich, "r", 1)
        self.assertLess(item["base_value"], 120)

    def test_noms_accordes(self):
        names = set()
        for workshop_id in ("forge", "couture", "alchimie", "cuisine", "sculpture"):
            workshop = load_template("workshop", workshop_id)
            for item_type in workshop["item_types"]:
                for option in workshop["parameters"][0]["options"]:
                    for n in range(3):
                        names.add(procedural_craft_item(workshop, item_type, {"p": option}, INPUTS, f"{option}{n}")["name"])
        # Le qualificatif est suivi de la parenthèse de l'ingrédient dominant.
        for wrong in ("Potion vif (", "Philtre distillée (", "Alliage incandescente (", "vife (", "Infusion grillé (", "Tisane mijoté ("):
            self.assertFalse([n for n in names if wrong in n], wrong)

    # --- Fabrication ------------------------------------------------------------

    def test_craft_inedit_de_bout_en_bout(self):
        fakes.new_player(inventory={"branche_morte": 3})
        result = ar.craft(request({"branche_morte": 3}))
        produced = result["output_item_id"]
        base_id = split_variant_id(produced)[0]
        self.forget_item(base_id)
        self.assertFalse(result["from_cache"])
        self.assertEqual(STORE[f"crafted_items/{base_id}"]["generation"], 1)
        self.assertEqual(result["output_item"]["equipment"]["slot"], "weapon")
        self.assertEqual(fakes.player()["inventory"], {produced: 1})
        self.assertEqual(fakes.player()["craft_book"][0]["item_type"], "arme")

    def test_type_obligatoire_et_type_inconnu(self):
        fakes.new_player(inventory={"branche_morte": 3})
        for item_type in (None, "potion_soin"):
            with self.assertRaises(HTTPException) as refused:
                ar.craft(request({"branche_morte": 2}, item_type=item_type))
            self.assertIn("Choisis", refused.exception.detail)

    def test_serie_d_une_recette_connue_et_limites(self):
        fakes.new_player(inventory={"branche_morte": 20})
        with self.assertRaises(HTTPException):   # 5 objets : au-delà de 4, quantités comprises
            ar.craft(request({"branche_morte": 5}))
        with self.assertRaises(HTTPException):   # série refusée tant que la recette est inédite
            ar.craft(request({"branche_morte": 2}, times=3))
        self.known_recipe({"branche_morte": 2}, "arme", "pain_de_miel")
        result = ar.craft(request({"branche_morte": 2}, times=3))
        self.assertEqual((result["times"], sum(result["produced"].values())), (3, 3))
        inventory = fakes.player()["inventory"]
        self.assertEqual((inventory["branche_morte"], base_count(inventory, "pain_de_miel")), (14, 3))
        self.assertEqual(fakes.player()["craft_book"][0]["times"], 3)
        with self.assertRaises(HTTPException):   # pas assez pour 8 séries de 2
            ar.craft(request({"branche_morte": 2}, times=8))

    # --- Générations ---------------------------------------------------------------

    def with_items(self, **items):
        """Remplace load_template des routes pour des ingrédients factices."""
        real = ar.load_template
        fake = {item_id: {"id": item_id, "name": item_id, "category": "material", "level": 1, "base_value": 5, **fields}
                for item_id, fields in items.items()}
        patcher = mock.patch.object(ar, "load_template", lambda kind, i: fake[i] if i in fake else real(kind, i))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_generations(self):
        self.with_items(alliage_maison={"generation": 1}, lame_maison={"category": "equipment", "generation": 2,
                                                                       "equipment": {"slot": "weapon", "stat_bonuses": {"atk": 2}}})
        fakes.new_player(inventory={"branche_morte": 4, "alliage_maison": 2, "lame_maison": 1})
        # Un ingrédient de génération 1 : plus de matériau, mais une arme reste possible.
        with self.assertRaises(HTTPException) as refused:
            ar.craft(request({"branche_morte": 1, "alliage_maison": 1}, item_type="alliage"))
        self.assertIn("équipement ou un consommable", refused.exception.detail)
        result = ar.craft(request({"branche_morte": 1, "alliage_maison": 1}, item_type="arme"))
        self.forget_item(result["output_item_id"])
        self.assertEqual(result["output_item"]["generation"], 2)
        # Un ingrédient de génération 2 ne sert plus.
        with self.assertRaises(HTTPException) as refused:
            ar.craft(request({"branche_morte": 1, "lame_maison": 1}, item_type="arme"))
        self.assertIn("trop travaillé", refused.exception.detail)

    # --- Rareté ----------------------------------------------------------------------

    def test_rarete_tiree_a_chaque_fabrication(self):
        rng = random.Random(4)
        inputs = [{"rarity": "commun", "quantity": 2}]
        rolls = Counter(roll_rarity(rarity_odds("normale", inputs, 6, 3), rng) for _ in range(20000))
        self.assertAlmostEqual(rolls["commun"] / 20000, 0.70, delta=0.02)
        self.assertAlmostEqual(rolls["rare"] / 20000, 0.07, delta=0.01)
        # La cohérence, les ingrédients rares et l'avance du métier font monter les chances.
        top = lambda odds: sum(odds[2:])
        self.assertGreater(top(rarity_odds("rare", inputs, 6, 3)), top(rarity_odds("normale", inputs, 6, 3)))
        self.assertGreater(top(rarity_odds("normale", [{"rarity": "rare", "quantity": 2}], 6, 3)), top(rarity_odds("normale", inputs, 6, 3)))
        self.assertGreater(top(rarity_odds("normale", inputs, 2, 6)), top(rarity_odds("normale", inputs, 6, 3)))
        # Un ingrédient épique garantit au moins un rare.
        self.assertEqual(sum(rarity_odds("normale", [{"rarity": "epique", "quantity": 1}], 6, 3)[:2]), 0)

    def test_une_serie_donne_des_rangs_varies_plus_forts(self):
        fakes.new_player(inventory={"branche_morte": 40}, professions={"forgeron": {"level": 10, "xp": 0}})
        self.known_recipe({"branche_morte": 2}, "arme", "amulette_chance_gravee")
        result = ar.craft(request({"branche_morte": 2}, times=20))
        self.assertGreater(len(result["produced"]), 1)
        ranks = {RARITIES.index(item.get("rarity", "commun")): item for item in result["output_items"].values()}
        weakest, strongest = ranks[min(ranks)], ranks[max(ranks)]
        self.assertGreater(equipment_points_of(strongest["equipment"]["stat_bonuses"]), equipment_points_of(weakest["equipment"]["stat_bonuses"]))
        self.assertGreater(strongest["base_value"], weakest["base_value"])

    # --- Enchantement ---------------------------------------------------------------

    def test_enchantement_unique(self):
        fakes.new_player(inventory={"amulette_chance_gravee": 1, "branche_morte": 4})
        enchant = lambda inputs, orientation="force": ar.craft(request(inputs, item_type=orientation, workshop="enchantement",
                                                                       parameters={"intensite_rune": "stable"}))
        with self.assertRaises(HTTPException):   # il faut un équipement
            enchant({"branche_morte": 2})
        result = enchant({"amulette_chance_gravee": 1, "branche_morte": 2})
        enchanted = result["output_item"]
        self.forget_item(enchanted["id"])
        self.assertTrue(enchanted["enchanted"])
        self.assertTrue(enchanted["name"].endswith("✦ Force"))
        before = load_template("item", "amulette_chance_gravee")["equipment"]["stat_bonuses"]
        self.assertGreater(enchanted["equipment"]["stat_bonuses"].get("atk", 0), before.get("atk", 0))
        self.assertEqual(fakes.player()["inventory"], {enchanted["id"]: 1, "branche_morte": 2})
        # Un objet enchanté ne s'enchante plus et ne sert plus dans aucun craft.
        with self.assertRaises(HTTPException) as refused:
            enchant({enchanted["id"]: 1, "branche_morte": 1})
        self.assertIn("une fois", refused.exception.detail)
        with self.assertRaises(HTTPException) as refused:
            ar.craft(request({enchanted["id"]: 1, "branche_morte": 1}, item_type="arme"))
        self.assertIn("enchanté", refused.exception.detail)


if __name__ == "__main__":
    unittest.main()
