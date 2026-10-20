"""L'équilibrage calibré tient toujours (simulateur, quelques secondes).

À relancer après tout changement de formule ou de constante : si un de ces
tests échoue, relancer ``simulate_combat.py calibrate`` et reporter les
valeurs dans ``balance.ROLE_PROFILES``.
"""

import json
import random
import statistics
import sys
import unittest

from fakes import ROOT

sys.path.insert(0, str(ROOT / "generate_static_files" / "balance"))
import simulate_combat as sim  # noqa: E402

COUNT = 120


def template(folder, template_id):
    return json.loads((ROOT / "templates" / folder / f"{template_id}.json").read_text(encoding="utf-8"))


class EquilibrageTest(unittest.TestCase):
    def setUp(self):
        random.seed(11)

    def fights(self, monster, level, potions, **kwargs):
        return [sim.fight(monster, level, potions=potions, **kwargs) for _ in range(COUNT)]

    def test_monstre_normal_une_minute_et_la_moitie_des_pv(self):
        for level in (5, 30):
            results = self.fights(sim.reference_monster(level, "normal"), level, 0)
            self.assertTrue(all(r["won"] for r in results))
            self.assertAlmostEqual(statistics.median(r["duration"] for r in results), 65, delta=12)
            self.assertAlmostEqual(statistics.mean(r["hp_lost"] for r in results), 0.5, delta=0.1)

    def test_elite_et_boss_pour_un_joueur_moyen(self):
        for role, potions, target in (("elite", 2, 0.55), ("boss", 5, 0.40)):
            for level in (10, 30):
                wins = sum(r["won"] for r in self.fights(sim.reference_monster(level, role), level, potions))
                self.assertAlmostEqual(wins / COUNT, target, delta=0.2, msg=f"{role} niveau {level}")

    def test_l_habilete_compte_contre_un_boss(self):
        boss = sim.reference_monster(10, "boss")
        careless = sum(r["won"] for r in self.fights(boss, 10, 5, avoid_rate=0.0)) / COUNT
        skilled = sum(r["won"] for r in self.fights(boss, 10, 5, avoid_rate=0.8)) / COUNT
        self.assertLess(careless, 0.1)
        self.assertGreater(skilled, 0.8)

    def test_un_nouveau_personnage_gagne_ses_premiers_combats(self):
        starter = {skill_id: template("skills", skill_id) for skill_id in ("frappe", "trait_de_lumiere")}
        for monster_id in ("lapin_bondissant", "loup_dore", "crabe_des_galets"):
            results = self.fights(template("monsters", monster_id), 1, 0, skills=starter)
            self.assertGreater(sum(r["won"] for r in results) / COUNT, 0.95, monster_id)


if __name__ == "__main__":
    unittest.main()
