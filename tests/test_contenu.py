"""Cohérence du contenu généré : à relancer après chaque génération d'étage."""

import importlib.util
import json
import unittest
from collections import defaultdict

from fakes import ROOT
from back import balance
from back.content_targets import check_skill, check_template, check_title, zone_difficulty_tier
from back.content_routes import get_floor, get_template

TEMPLATES = ROOT / "templates"
FOLDER = {"monster": "monsters", "resource": "resources", "npc": "npcs", "quest_giver": "quests",
          "shop": "shops", "player_market": "player_markets"}


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def template(kind_folder, template_id):
    path = TEMPLATES / kind_folder / f"{template_id}.json"
    return load_json(path) if path.exists() else None


def floors():
    """(numéro, squelette, {zone_id: contenu}) de chaque étage généré."""
    for path in sorted((ROOT / "zone_skeletons").glob("floor_*_skeleton.json")):
        skeleton = load_json(path)
        number = int(skeleton["id"].split("_")[1])
        zones = {p.stem: load_json(p) for p in (ROOT / "zones" / f"floor_{number}").glob("*.json")}
        yield number, skeleton, zones


def zone_generator():
    path = ROOT / "generate_static_files" / "zone_content" / "generate_zone_content.py"
    spec = importlib.util.spec_from_file_location("generate_zone_content", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def condition_leaves(condition):
    if not isinstance(condition, dict):
        return []
    if condition.get("type") == "group":
        return [leaf for sub in condition.get("conditions", []) for leaf in condition_leaves(sub)]
    return [condition]


class ContenuTest(unittest.TestCase):
    def test_chaque_zone_du_squelette_est_generee(self):
        for number, skeleton, zones in floors():
            missing = {z["id"] for z in skeleton["zones"]} - set(zones)
            self.assertFalse(missing, f"étage {number} : zones sans contenu {missing}")

    def test_templates_references_existent(self):
        for number, _, zones in floors():
            for zone_id, zone in zones.items():
                for entry in zone["entries"]:
                    self.assertIsNotNone(template(FOLDER[entry["template_type"]], entry["template_id"]),
                                         f"{zone_id} : {entry['template_id']} introuvable")

    def test_rien_n_attend_un_contenu_absent(self):
        generator = zone_generator()
        for number, _, _ in floors():
            missing, _ = generator.floor_dependencies(number)
            self.assertFalse(any(missing.values()), f"étage {number} : placés nulle part {missing}")

    def test_conditions_du_squelette_tenues(self):
        for number, skeleton, zones in floors():
            quests = {e["template_id"] for z in zones.values() for e in z["entries"] if e["template_type"] == "quest_giver"}
            for item in skeleton.get("condition_manifest", []):
                condition = item["condition"]
                if condition["type"] == "quest":
                    self.assertIn(condition["quest_id"], quests, f"étage {number} : quête {condition['quest_id']} absente")
                elif condition["type"] == "item":
                    self.assertTrue(self._item_obtainable(condition["item_id"], zones), f"objet {condition['item_id']} introuvable")
                elif condition["type"] == "boss":
                    boss_entries = [e for e in zones[condition["zone_id"]]["entries"]
                                    if (template("monsters", e["template_id"]) or {}).get("role") == "boss"]
                    self.assertTrue(boss_entries, f"pas de boss dans {condition['zone_id']}")

    def _item_obtainable(self, item_id, zones):
        for zone in zones.values():
            for entry in zone["entries"]:
                data = template(FOLDER[entry["template_type"]], entry["template_id"]) or {}
                sources = (data.get("loot_table") or []) + (data.get("yields") or []) + (data.get("sell_offers") or [])
                sources += (data.get("rewards") or {}).get("items", [])
                if any(source.get("item_id") == item_id for source in sources):
                    return True
        return False

    def test_monstres_et_objets_dans_leurs_cibles(self):
        tiers = {}
        for _, skeleton, zones in floors():
            for zone_id, zone in zones.items():
                for entry in zone["entries"]:
                    tiers.setdefault(entry["template_id"], zone_difficulty_tier(skeleton, zone_id))
        warnings = []
        for folder, kind in (("monsters", "monster"), ("items", "item")):
            for path in (TEMPLATES / folder).glob("*.json"):
                data = load_json(path)
                warnings += check_template({**data, "kind": kind}, tiers.get(data["id"], "coeur"))
        self.assertFalse(warnings, "\n".join(warnings))

    def test_competences_dans_leurs_cibles(self):
        warnings = []
        for path in (TEMPLATES / "skills").glob("*.json"):
            warnings += check_skill(load_json(path))
        self.assertFalse(warnings, "\n".join(warnings))

    def test_titres_dans_leurs_regles(self):
        warnings = []
        for path in (TEMPLATES / "titles").glob("*.json"):
            warnings += check_title(load_json(path))
        self.assertFalse(warnings, "\n".join(warnings))

    def test_conditions_des_competences_et_titres_visent_du_contenu_reel(self):
        targets = {"kill_count": ("monster_id", "monsters"), "gather_count": ("resource_id", "resources"),
                   "quest": ("quest_id", "quests"), "item": ("item_id", "items"), "deliver_item": ("item_id", "items")}
        for folder in ("skills", "titles"):
            for path in (TEMPLATES / folder).glob("*.json"):
                data = load_json(path)
                for leaf in condition_leaves(data.get("unlock_condition")):
                    if leaf["type"] in targets:
                        key, target_folder = targets[leaf["type"]]
                        self.assertIsNotNone(template(target_folder, leaf[key]), f"{data['id']} : {leaf[key]} introuvable")
                    # Un boss d'étage est vaincu une fois pour tout le serveur : jamais en condition.
                    self.assertNotEqual(leaf["type"], "boss", f"{data['id']} dépend d'un boss d'étage")

    def test_chaque_metier_progresse_depuis_le_niveau_1(self):
        for number, _, zones in floors():
            levels = defaultdict(set)
            for zone in zones.values():
                for entry in zone["entries"]:
                    if entry["template_type"] == "resource":
                        required = template("resources", entry["template_id"])["profession_required"]
                        levels[required["profession_id"]].add(required["min_level"])
            for profession, found in levels.items():
                self.assertEqual(found, set(range(1, max(found) + 1)), f"étage {number}, {profession} : niveaux {sorted(found)}")

    def test_les_quetes_apportent_environ_30_pourcent_de_l_xp(self):
        for number, _, zones in floors():
            quest_xp = sum(template("quests", e["template_id"])["rewards"]["xp"]
                           for z in zones.values() for e in z["entries"] if e["template_type"] == "quest_giver")
            low, high = (number - 1) * 10 + 1, number * 10
            needed = sum(balance.xp_to_next_level(level) for level in range(low, high))
            self.assertTrue(0.2 <= quest_xp / needed <= 0.4, f"étage {number} : {quest_xp / needed:.0%} de l'XP en quêtes")

    def test_conditions_secretes_jamais_servies(self):
        for folder, kind in (("skills", "skill"), ("titles", "title")):
            for path in (TEMPLATES / folder).glob("*.json"):
                self.assertNotIn("unlock_condition", get_template(kind, path.stem))

    def test_etage_servi_avec_les_niveaux_sans_les_consignes(self):
        for number, skeleton, _ in floors():
            for zone in get_floor(skeleton["id"])["zones"]:
                self.assertNotIn("generation_brief", zone)
                self.assertEqual(len(zone["level_range"]), 2, zone["id"])

    def test_fiches_chargees_d_un_coup(self):
        from fastapi.testclient import TestClient
        from back.main import app
        client = TestClient(app)
        response = client.post("/content/templates/item/batch", json={"ids": ["branche_morte", "pain_de_miel~rare", "n_existe_pas", "branche_morte"]})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(sorted(data["templates"]), ["branche_morte", "pain_de_miel~rare"])
        self.assertEqual(data["templates"]["pain_de_miel~rare"]["rarity"], "rare")
        self.assertEqual(data["missing"], ["n_existe_pas"])
        # Les conditions secrètes restent cachées, comme fiche par fiche.
        skills = client.post("/content/templates/skill/batch", json={"ids": ["frappe"]}).json()["templates"]
        self.assertNotIn("unlock_condition", skills["frappe"])


if __name__ == "__main__":
    unittest.main()
