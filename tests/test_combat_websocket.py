"""Protocole du WebSocket de combat, de bout en bout, sur la base factice."""

import time
import unittest
from unittest import mock

import fakes
from fakes import STORE

fakes.install()

from fastapi.testclient import TestClient  # noqa: E402

from back import combat_routes as cr, main  # noqa: E402

PLAYER = {
    "level": 10, "current_zone_id": "chambre_gardien",
    "skills": {"unlocked": ["frappe", "premiers_soins"], "equipped_active": ["frappe", "premiers_soins"], "equipped_passive": []},
    "inventory": {"pain_du_pelerin": 2},
}
START = {"action": "start_combat", "player_id": "joueur", "zone_id": "chambre_gardien", "entry_id": "gardien_aube_boss"}


def receive(ws, *types, timeout=10.0):
    """Lit les messages jusqu'au premier d'un des types voulus (les ``status``
    envoyés par la boucle de combat sont ignorés). Échoue au bout de
    ``timeout`` secondes plutôt que d'attendre indéfiniment."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = ws.receive_json()
        if message["type"] in types:
            return message
    raise AssertionError(f"aucun message {types} en {timeout} s")


# Coup qui touche toujours, sans critique ni variation.
SURE_HIT = lambda raw, attacker_agi, defender_agi: {"damage": raw, "critical": False, "dodged": False}


class CombatWebsocketTest(unittest.TestCase):
    def setUp(self):
        fakes.reset()
        fakes.new_player(**PLAYER)
        # Le Gardien a déjà été vaincu par quelqu'un d'autre : il reste son écho.
        STORE["zones/chambre_gardien"] = {"zone_id": "chambre_gardien", "entries": {"gardien_aube_boss": {"status": "defeated"}}}
        cr.AUTH_REQUIRED = False
        self.client = TestClient(main.app)

    def session(self, started):
        return cr.active_combats[started["combat_id"]]

    def announce(self, session, move_id):
        move = next(m for m in session["monster"]["moveset"] if m["id"] == move_id)
        session["monster_pending"] = {"move": move, "resolves_at": cr.engine.now() + 30, "dodged": False}

    def test_connexion_requise(self):
        cr.AUTH_REQUIRED = True
        with self.client.websocket_connect("/ws/combat") as ws:
            ws.send_json(START)
            self.assertEqual(ws.receive_json(), {"type": "error", "message": "Connexion requise"})

    def test_echo_esquive_interruption_et_victoire_sans_butin(self):
        with self.client.websocket_connect("/ws/combat") as ws:
            ws.send_json(START)
            started = receive(ws, "combat_started", "error")
            self.assertEqual(started["type"], "combat_started", started)
            self.assertTrue(started["echo"])
            self.assertTrue(started["monster_name"].startswith("Écho"))
            session = self.session(started)

            # Esquive pendant l'annonce d'une attaque à esquiver.
            self.announce(session, "aube_eternelle")
            ws.send_json({"action": "dodge"})
            self.assertTrue(receive(ws, "player_dodge")["useful"])

            # Interruption par une compétence de dégâts.
            self.announce(session, "ecrasement_des_anneaux")
            ws.send_json({"action": "use_skill", "skill_id": "frappe"})
            self.assertEqual(receive(ws, "player_skill_used")["interrupted"], "ecrasement_des_anneaux")

            # Victoire : l'XP du boss, mais aucun butin.
            session["monster_current_hp"] = 1
            with mock.patch.object(cr.engine, "roll_hit", SURE_HIT):
                ws.send_json({"action": "basic_attack"})
                won = receive(ws, "combat_won", "combat_won_no_reward")
            self.assertEqual(won["type"], "combat_won", won)
            self.assertTrue(won["echo"])
            self.assertIsNone(won["loot"])
        player = fakes.player()
        self.assertEqual(player["defeated_echoes"], ["chambre_gardien/gardien_aube_boss"])
        self.assertNotIn("couronne_du_gardien", player["inventory"])

        # Un second écho est refusé dès le début du combat.
        with self.client.websocket_connect("/ws/combat") as ws:
            ws.send_json(START)
            self.assertIn("écho", receive(ws, "error", "combat_started")["message"])

    def test_pv_enregistres_a_la_deconnexion_et_un_combat_a_la_fois(self):
        with self.client.websocket_connect("/ws/combat") as ws:
            ws.send_json(START)
            started = receive(ws, "combat_started")
            self.session(started)["player_current_hp"] = 40
            ws.send_json(START)
            self.assertIn("déjà en cours", receive(ws, "error")["message"])
        time.sleep(0.3)
        self.assertEqual(fakes.player()["health"]["current"], 40)


if __name__ == "__main__":
    unittest.main()
