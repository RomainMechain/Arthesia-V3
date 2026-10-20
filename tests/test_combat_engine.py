"""Règles du moteur de combat (module pur, horloge simulée)."""

import random
import unittest
from unittest import mock

from fakes import ROOT  # noqa: F401  (ajoute la racine du dépôt au chemin)
from back import combat_engine as engine
from back.content_store import load_template


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


STATS = {"hp": 650, "atk": 62, "def": 50, "agi": 19, "int": 24, "sag": 32}


class CombatEngineTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.original_now = engine.now
        engine.now = self.clock
        random.seed(7)

    def tearDown(self):
        engine.now = self.original_now

    def session(self, monster_id="gardien_de_l_aube", skills=("frappe", "premiers_soins"), stats=None, level=10):
        templates = {skill_id: load_template("skill", skill_id) for skill_id in skills}
        return engine.new_session("p", "z", "e", load_template("monster", monster_id), stats or dict(STATS),
                                  list(skills), [], templates, level)

    def announce(self, session, move_id):
        move = next(m for m in session["monster"]["moveset"] if m["id"] == move_id)
        session["monster_pending"] = {"move": move, "resolves_at": self.clock.t + move["telegraph_seconds"], "dodged": False}
        return move

    # --- Formules ---------------------------------------------------------

    def test_formules_stables_avec_le_niveau(self):
        # Le joueur de référence garde la même réduction des dégâts et la même
        # vitesse d'attaque à tous les niveaux : l'équilibrage ne dérive pas.
        from back import balance
        mitigations, cooldowns = [], []
        for level in (1, 10, 50, 100):
            stats = balance.reference_build_stats(level)
            mitigations.append(engine.compute_mitigation(stats["def"], level))
            cooldowns.append(engine.compute_basic_attack_cooldown(stats["agi"], level))
        self.assertLess(max(mitigations) - min(mitigations), 0.01)
        self.assertLess(max(cooldowns) - min(cooldowns), 0.02)
        self.assertAlmostEqual(mitigations[0], 0.43, delta=0.01)

    def test_roll_hit_variation_esquive_et_critique(self):
        hits = [engine.roll_hit(100, 10, 10) for _ in range(20000)]
        landed = [h["damage"] for h in hits if not h["dodged"] and not h["critical"]]
        self.assertGreaterEqual(min(landed), 92)
        self.assertLessEqual(max(landed), 108)
        dodge_rate = sum(h["dodged"] for h in hits) / len(hits)
        self.assertAlmostEqual(dodge_rate, 0.05, delta=0.01)

    # --- Compétences --------------------------------------------------------

    def test_degats_base_plus_stat(self):
        session = self.session(monster_id="sanglier_des_fougeres")
        effect = session["skills"]["frappe"]["effects"][0]
        expected = effect["base"] + effect["power"] * STATS["atk"]
        no_luck = lambda raw, attacker_agi, defender_agi: {"damage": raw, "critical": False, "dodged": False}
        with mock.patch.object(engine, "roll_hit", no_luck):
            before = session["monster_current_hp"]
            engine.use_skill(session, "frappe")
        mitigation = engine.compute_mitigation(session["monster"]["stats"]["def"], 10)
        self.assertAlmostEqual(before - session["monster_current_hp"], expected * (1 - mitigation), delta=0.2)

    def test_stat_hybride_est_deux_fois_la_plus_faible(self):
        session = self.session()
        hybrid = load_template("skill", "estoc_runique")
        self.assertEqual(engine.skill_damage_stat(session, hybrid), 2 * min(STATS["atk"], STATS["int"]))
        self.assertEqual(engine.skill_damage_stat(session, {"damage_type": "magical"}), STATS["int"])

    def test_competence_refusee_sans_mana_ou_en_recharge(self):
        session = self.session()
        engine.use_skill(session, "frappe")
        with self.assertRaises(engine.CombatRefusal):
            engine.use_skill(session, "frappe")
        self.clock.t += 100
        session["player_current_mana"] = 0
        with self.assertRaises(engine.CombatRefusal):
            engine.use_skill(session, "frappe")

    # --- Attaques télégraphiées --------------------------------------------

    def test_esquive_reussie(self):
        session = self.session()
        self.announce(session, "aube_eternelle")
        hp = session["player_current_hp"]
        self.assertTrue(engine.dodge(session)["useful"])
        self.clock.t += 2.1
        result = engine.monster_tick(session)
        self.assertEqual(result["avoided"], "esquive")
        self.assertEqual(session["player_current_hp"], hp)

    def test_esquive_en_recharge_et_hors_annonce(self):
        session = self.session()
        self.assertFalse(engine.dodge(session)["useful"])          # hors annonce : perdue
        self.announce(session, "aube_eternelle")
        with self.assertRaises(engine.CombatRefusal):             # recharge de 8 s
            engine.dodge(session)
        self.clock.t += engine.DODGE_COOLDOWN_SECONDS
        self.assertTrue(engine.dodge(session)["useful"])

    def test_esquive_inutile_contre_une_attaque_a_interrompre(self):
        session = self.session()
        self.announce(session, "ecrasement_des_anneaux")
        self.assertFalse(engine.dodge(session)["useful"])

    def test_interruption_par_une_competence_de_degats(self):
        session = self.session()
        self.announce(session, "ecrasement_des_anneaux")
        self.assertEqual(engine.use_skill(session, "frappe")["interrupted"], "ecrasement_des_anneaux")
        self.assertIsNone(session["monster_pending"])

    def test_un_soin_n_interrompt_pas(self):
        session = self.session()
        self.announce(session, "ecrasement_des_anneaux")
        session["player_current_hp"] = 100
        self.assertIsNone(engine.use_skill(session, "premiers_soins")["interrupted"])
        self.assertIsNotNone(session["monster_pending"])

    def test_rien_d_autre_pendant_une_annonce(self):
        session = self.session()
        session["phase_index"] = len(session["monster"]["phases"]) - 1
        for _ in range(60):
            self.clock.t += engine.TICK_INTERVAL_SECONDS
            event = engine.monster_tick(session)
            if event and event.get("telegraph"):
                self.assertEqual(engine.status(session)["monster_pending"]["skill_id"], event["skill_id"])
                self.clock.t += engine.TICK_INTERVAL_SECONDS
                self.assertIsNone(engine.monster_tick(session))
                return
        self.fail("aucune attaque annoncée en 30 s")

    # --- Consommables -------------------------------------------------------

    def test_soin_refuse_a_pv_pleins(self):
        session = self.session()
        soin = {"category": "consumable", "consumable": {"effect_type": "heal", "amount": 50}}
        self.assertIsNotNone(engine.consumable_refusal(session, soin))
        session["player_current_hp"] = 100
        self.assertIsNone(engine.consumable_refusal(session, soin))


if __name__ == "__main__":
    unittest.main()
