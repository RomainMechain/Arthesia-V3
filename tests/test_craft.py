"""Craft inédit : chaîne d'IA (Gemini, Claude) et générateur sans IA en secours.

Aucun appel réseau : Gemini est simulé avec ``mock``.
"""

import json
import unittest
from unittest import mock

import fakes
from fakes import STORE

fakes.install()

from back import craft_service as cs, models  # noqa: E402
from back import action_routes as ar  # noqa: E402
from back.content_store import load_template  # noqa: E402
from back.content_targets import RARE_CRAFT_MULTIPLIER, check_template, equipment_points, equipment_points_of  # noqa: E402
from back.craft_procedural import procedural_craft_item  # noqa: E402
from back.config import TEMPLATES_DIR  # noqa: E402

FORGE = load_template("workshop", "forge")
INPUTS = [{**load_template("item", "branche_morte"), "quantity": 2},
          {**load_template("item", "poil_de_loup_dore"), "quantity": 1}]


def gemini_reply(item: dict, status: int = 200) -> mock.Mock:
    payload = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps({"item": item})}]}}]}
    return mock.Mock(status_code=status, text=json.dumps(payload), json=lambda: payload)


class CraftTest(unittest.TestCase):
    def setUp(self):
        fakes.reset()
        # Aucune clé par défaut : chaque test active ce qu'il simule.
        patcher = mock.patch.multiple(cs, GEMINI_API_KEY=None, ANTHROPIC_API_KEY=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sans_cle_le_generateur_sans_ia_prend_le_relais(self):
        item = cs.generate_craft_item(FORGE, {"temperature": "haute"}, INPUTS, "recette")
        self.assertEqual(item["origin"], "procedural")
        self.assertFalse(check_template({**item, "kind": "item"}))
        # Même recette, même objet.
        self.assertEqual(item, cs.generate_craft_item(FORGE, {"temperature": "haute"}, INPUTS, "recette"))

    def test_gemini_invente_mais_reste_dans_les_bornes(self):
        greedy = {"id": "lame_divine", "name": "Lame divine", "category": "equipment", "level": 40, "base_value": 99999,
                  "equipment": {"slot": "weapon", "level_required": 40, "stat_bonuses": {"atk": 500, "agi": 80}}}
        with mock.patch.object(cs, "GEMINI_API_KEY", "cle-de-test"), \
                mock.patch.object(cs.httpx, "post", return_value=gemini_reply(greedy)) as post:
            item = cs.generate_craft_item(FORGE, {"temperature": "extreme"}, INPUTS, "recette")
        self.assertEqual(post.call_args.kwargs["headers"], {"x-goog-api-key": "cle-de-test"})
        self.assertEqual(item["origin"], "gemini")
        level = max(i["level"] for i in INPUTS)
        self.assertEqual((item["level"], item["equipment"]["level_required"]), (level, level))
        self.assertLessEqual(equipment_points_of(item["equipment"]["stat_bonuses"]), equipment_points(level) * RARE_CRAFT_MULTIPLIER[1])
        self.assertLess(item["base_value"], 1000)

    def test_quota_gemini_atteint(self):
        with mock.patch.object(cs, "GEMINI_API_KEY", "cle-de-test"), \
                mock.patch.object(cs.httpx, "post", return_value=gemini_reply({}, status=429)):
            item = cs.generate_craft_item(FORGE, {"temperature": "basse"}, INPUTS, "recette")
        self.assertEqual(item["origin"], "procedural")

    def test_reponse_gemini_invalide(self):
        broken = mock.Mock(status_code=200, json=lambda: {"candidates": [{"content": {"parts": [{"text": "pas du json"}]}}]})
        with mock.patch.object(cs, "GEMINI_API_KEY", "cle-de-test"), mock.patch.object(cs.httpx, "post", return_value=broken):
            self.assertEqual(cs.generate_craft_item(FORGE, {"temperature": "basse"}, INPUTS, "r")["origin"], "procedural")

    def test_craft_inedit_de_bout_en_bout_sans_ia(self):
        fakes.new_player(inventory={"branche_morte": 3})
        request = models.CraftRequest(player_id="joueur", workshop_id="forge",
                                      inputs=[{"item_id": "branche_morte", "quantity": 3}], parameters={"temperature": "moyenne"})
        result = ar.craft(request)
        item_id = result["output_item_id"]
        self.addCleanup(lambda: (TEMPLATES_DIR / "items" / f"{item_id}.json").unlink(missing_ok=True))
        self.assertFalse(result["from_cache"])
        self.assertEqual(STORE[f"crafted_items/{item_id}"]["origin"], "procedural")
        self.assertEqual(fakes.player()["inventory"], {item_id: 1})

    def test_noms_accordes(self):
        names = {procedural_craft_item(load_template("workshop", w), {p["id"]: o}, INPUTS, f"{w}{o}{n}")["name"]
                 for w in ("forge", "couture", "alchimie", "cuisine", "enchantement", "sculpture")
                 for p in load_template("workshop", w)["parameters"] for o in p["options"] for n in range(4)}
        for wrong in ("Potion vif", "Philtre distillée", "Alliage incandescente", "vife"):
            self.assertFalse([n for n in names if wrong in n], wrong)


if __name__ == "__main__":
    unittest.main()
