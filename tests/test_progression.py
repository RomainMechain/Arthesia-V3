"""Actions du joueur et progression, sur la base Firestore factice."""

import unittest
from datetime import datetime, timedelta, timezone

import fakes
from fakes import STORE

fakes.install()

from fastapi import HTTPException  # noqa: E402

from back import action_routes as ar, player_routes as pr_routes, progression  # noqa: E402
from back import balance, models  # noqa: E402
from back.content_store import find_entry, load_static_zone_content, load_template  # noqa: E402
from back.craft_service import build_recipe_key, recipe_key_doc_id  # noqa: E402
from back.game_rules import compute_final_stats, current_hp, set_hp  # noqa: E402


def kill(player_id, zone_id, entry_id, hp=None, echo=False):
    return progression.apply_monster_kill_rewards(player_id, zone_id, entry_id, hp, echo)


class ProgressionTest(unittest.TestCase):
    def setUp(self):
        fakes.reset()

    # --- Création ---------------------------------------------------------

    def test_creation_debloque_la_ville_et_equipe_les_competences_de_depart(self):
        pr_routes.create_player(models.CreatePlayerRequest(player_id="neuf", display_name="Nouveau Venu"))
        player = fakes.player("neuf")
        self.assertEqual(player["current_zone_id"], "village_aube")
        self.assertIn("village_aube", player["unlocks"]["zones"])
        self.assertEqual(sorted(player["skills"]["equipped_active"]), ["frappe", "trait_de_lumiere"])
        with self.assertRaises(HTTPException):   # pseudo déjà pris
            pr_routes.create_player(models.CreatePlayerRequest(player_id="autre", display_name="nouveau venu"))

    # --- Combat : récompenses ---------------------------------------------

    def test_victoire_xp_butin_compteur_et_respawn(self):
        fakes.new_player(level=1)
        result = kill("joueur", "sentier_lisiere", "loup_dore_spawn", hp=40)
        player = fakes.player()
        self.assertEqual(result["xp_gagne"], balance.kill_xp(1))
        self.assertEqual(player["lifetime_counters"]["kills"]["loup_dore"], 1)
        self.assertEqual(STORE["zones/sentier_lisiere"]["entries"]["loup_dore_spawn"]["status"], "depleted")
        self.assertEqual(player["health"]["current"], 40)   # PV de fin de combat gardés
        with self.assertRaises(HTTPException):   # le même loup ne paie pas deux fois
            kill("joueur", "sentier_lisiere", "loup_dore_spawn")

    def test_montee_de_niveau_rend_tous_les_pv(self):
        fakes.new_player(level=1, xp=balance.xp_to_next_level(1) - 1)
        result = kill("joueur", "sentier_lisiere", "lapin_spawn", hp=10)
        self.assertEqual(result["levels_gained"], 1)
        self.assertIsNone(fakes.player()["health"]["current"])
        self.assertEqual(result["xp_to_next_level"], balance.xp_to_next_level(2))

    def test_boss_premier_vainqueur_puis_echo_sans_butin(self):
        fakes.new_player("premier", level=10)
        fakes.new_player("second", level=10)
        first = kill("premier", "chambre_gardien", "gardien_aube_boss")
        self.assertIsNotNone(first["loot"])
        self.assertEqual(STORE["zones/chambre_gardien"]["entries"]["gardien_aube_boss"]["status"], "defeated")
        with self.assertRaises(HTTPException):   # le vrai boss ne se rebat pas
            kill("second", "chambre_gardien", "gardien_aube_boss")

        state = ar.get_zone_state("chambre_gardien", "second")["entries"]["gardien_aube_boss"]
        self.assertTrue(state["echo_available"])
        echo = kill("second", "chambre_gardien", "gardien_aube_boss", echo=True)
        self.assertIsNone(echo["loot"])
        self.assertEqual(echo["xp_gagne"], balance.kill_xp(10, "boss"))
        self.assertEqual(fakes.player("second")["inventory"], {})
        self.assertFalse(ar.get_zone_state("chambre_gardien", "second")["entries"]["gardien_aube_boss"]["echo_available"])
        with self.assertRaises(HTTPException):   # un seul écho par joueur
            kill("second", "chambre_gardien", "gardien_aube_boss", echo=True)

    def test_defaite_sans_perte_de_niveau(self):
        fakes.new_player(level=10, xp=20, currency={"or": 1000})
        penalty = progression.apply_defeat("joueur")
        player = fakes.player()
        self.assertEqual(penalty, {"xp_lost": 20, "gold_lost": 30})
        self.assertEqual((player["level"], player["xp"], player["currency"]["or"]), (10, 0, 970))
        max_hp = compute_final_stats(player)["hp"]
        self.assertAlmostEqual(current_hp(player, max_hp), max_hp * balance.DEFEAT_HP_SHARE, delta=1)

    # --- Quêtes -----------------------------------------------------------

    def test_quete_active_ouvre_la_rive_tout_de_suite(self):
        fakes.new_player(current_zone_id="village_aube", unlocks={"zones": ["village_aube"], "entries": [], "skills": [], "titles": []})
        result = ar.start_quest(models.StartQuestRequest(player_id="joueur", quest_id="intro_peche"))
        self.assertIn("zone:rive_paisible", result["newly_unlocked"])
        fakes.player()["inventory"]["truite_argentee_aube"] = 3
        result = ar.turn_in_quest(models.TurnInQuestRequest(player_id="joueur", quest_id="intro_peche"))
        self.assertEqual(result["quest_completed"], "intro_peche")
        self.assertIn("rive_paisible", fakes.player()["unlocks"]["zones"])   # reste ouverte après la quête

    def test_objectif_de_niveau(self):
        quest = load_template("quest", "lumiere_qui_ne_vacille_jamais")
        progress = {"state": "active", "objectives_progress": [{"current_count": 15}, {"current_count": 0}]}
        from back.game_rules import quest_progress_view
        player = fakes.new_player(level=1)
        self.assertFalse(quest_progress_view(quest, progress, player)["ready"])
        player["level"] = 2
        self.assertTrue(quest_progress_view(quest, progress, player)["ready"])

    # --- Carnet d'aventure ---------------------------------------------------

    def test_carnet_ne_montre_que_les_decouvertes(self):
        counters = {"kills": {"loup_dore": 12}, "gathers": {"bois_mort_de_lisiere": 3}, "deliveries": {}}
        fakes.new_player(lifetime_counters=counters,
                         unlocks={"zones": ["village_aube"], "entries": [], "skills": [], "titles": []})
        page = pr_routes.get_player_carnet("joueur")["floors"][0]
        self.assertEqual([m["id"] for m in page["bestiary"]], ["loup_dore"])
        self.assertEqual(page["bestiary"][0]["kills"], 12)
        self.assertGreater(page["unknown_monsters"], 20)
        self.assertEqual([r["id"] for r in page["harvests"]], ["bois_mort_de_lisiere"])
        # Les habitants des zones fermées restent inconnus.
        zones = {n["zone_name"] for n in page["inhabitants"]}
        self.assertIn("Val-de-l'Aube", zones)
        self.assertNotIn("Camp des Pêcheurs du Soir", zones)
        self.assertGreater(page["unknown_inhabitants"], 0)

    def test_carnet_vide_pour_un_nouveau_venu(self):
        fakes.new_player(unlocks={"zones": [], "entries": [], "skills": [], "titles": []}, current_zone_id="chambre_gardien")
        floors = pr_routes.get_player_carnet("joueur")["floors"]
        self.assertTrue(all(not f["bestiary"] and not f["harvests"] for f in floors))

    # --- Titres -----------------------------------------------------------

    def test_titre_debloque_au_seuil(self):
        counters = {"kills": {"loup_dore": 99}, "gathers": {}, "deliveries": {}}
        fakes.new_player(level=3, lifetime_counters=counters)
        progression.check_and_apply_unlocks("joueur")
        self.assertNotIn("fleau_des_loups_dores", fakes.player()["titles"]["unlocked"])
        fakes.player()["lifetime_counters"]["kills"]["loup_dore"] = 100
        self.assertIn("title:fleau_des_loups_dores", progression.check_and_apply_unlocks("joueur"))

    # --- Réinitialisation des points ---------------------------------------

    def test_reinitialisation_des_points(self):
        spent = {"hp": 13, "atk": 15, "def": 12, "agi": 7, "int": 5, "sag": 8}
        fakes.new_player(level=10, current_zone_id="sentier_lisiere", currency={"or": 400},
                         stat_points={"spent": dict(spent), "available": 0})
        request = models.ResetStatPointsRequest(player_id="joueur")
        with self.assertRaises(HTTPException):   # hors de la ville
            ar.reset_stat_points(request)
        fakes.player()["current_zone_id"] = "village_aube"
        self.assertEqual(ar.reset_stat_points(request)["cost"], 0)        # la première est gratuite
        self.assertEqual(fakes.player()["stat_points"]["available"], 60)
        fakes.player()["stat_points"] = {"spent": dict(spent), "available": 0}
        self.assertEqual(ar.reset_stat_points(request)["cost"], 300)      # puis 30 or × niveau
        self.assertEqual(fakes.player()["currency"]["or"], 100)

    # --- Craft ------------------------------------------------------------

    def test_carnet_de_recettes(self):
        fakes.new_player(inventory={"branche_morte": 5})
        inputs = [{"item_id": "branche_morte", "quantity": 2}]
        parameters = {p["id"]: p["options"][0] for p in load_template("workshop", "forge").get("parameters", [])}
        # Recette déjà connue : pas d'appel à l'IA.
        key = build_recipe_key("forge", inputs, parameters)
        STORE[f"craft_cache/{recipe_key_doc_id(key)}"] = {"recipe_key": key, "output_item_id": "pain_de_miel"}
        request = models.CraftRequest(player_id="joueur", workshop_id="forge", inputs=inputs, parameters=parameters)
        ar.craft(request)
        ar.craft(request)
        book = fakes.player()["craft_book"]
        self.assertEqual(len(book), 1)
        self.assertEqual((book[0]["output_item_id"], book[0]["times"]), ("pain_de_miel", 2))
        self.assertEqual(fakes.player()["inventory"], {"branche_morte": 1, "pain_de_miel": 2})

    def test_objets_craftes_restaures_au_demarrage(self):
        from back.config import TEMPLATES_DIR
        from back.craft_service import restore_crafted_items
        path = TEMPLATES_DIR / "items" / "objet_de_test_restaure.json"
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        # Seulement dans Firestore (disque effacé par un redémarrage) : réécrit.
        STORE["crafted_items/objet_de_test_restaure"] = {"name": "Test", "category": "material", "level": 1}
        # Seulement sur le disque (crafté avant la sauvegarde) : envoyé dans Firestore.
        STORE["craft_cache/recette"] = {"output_item_id": "pain_de_miel"}
        self.assertEqual(restore_crafted_items(), {"restored": 1, "uploaded": 1})
        self.assertTrue(path.exists())
        self.assertEqual(STORE["crafted_items/pain_de_miel"]["id"], "pain_de_miel")
        self.assertEqual(restore_crafted_items(), {"restored": 0, "uploaded": 0})

    def test_craft_limite_a_4_objets_et_serie_d_une_recette_connue(self):
        fakes.new_player(inventory={"branche_morte": 20})
        parameters = {p["id"]: p["options"][0] for p in load_template("workshop", "forge").get("parameters", [])}

        def request(quantity, times=1):
            return models.CraftRequest(player_id="joueur", workshop_id="forge", parameters=parameters, times=times,
                                       inputs=[{"item_id": "branche_morte", "quantity": quantity}])

        with self.assertRaises(HTTPException):   # 5 objets : au-delà de 4, quantités comprises
            ar.craft(request(5))
        key = build_recipe_key("forge", [{"item_id": "branche_morte", "quantity": 2}], parameters)
        with self.assertRaises(HTTPException):   # série refusée tant que la recette est inédite
            ar.craft(request(2, times=3))
        STORE[f"craft_cache/{recipe_key_doc_id(key)}"] = {"recipe_key": key, "output_item_id": "pain_de_miel"}
        result = ar.craft(request(2, times=3))
        self.assertEqual(result["times"], 3)
        self.assertEqual(fakes.player()["inventory"], {"branche_morte": 14, "pain_de_miel": 3})
        self.assertEqual(fakes.player()["craft_book"][0]["times"], 3)
        with self.assertRaises(HTTPException):   # pas assez pour 8 séries de 2
            ar.craft(request(2, times=8))

    # --- Récolte et soin hors combat ----------------------------------------

    def test_recolte_xp_de_metier(self):
        fakes.new_player(current_zone_id="sentier_lisiere")
        request = models.ResourceGatherRequest(player_id="joueur", zone_id="sentier_lisiere", entry_id="bois_mort_de_lisiere_noeud")
        ar.gather_start(models.GatherStartRequest(**request.model_dump()))
        fakes.player()["gathering"]["ready_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        ar.report_resource_gather(request)
        profession = fakes.player()["professions"]["bucheronnage"]
        self.assertEqual(profession["xp"], balance.profession_action_xp(1, "gather", 1))

    def test_xp_de_metier_suit_le_niveau_de_l_objet(self):
        xp = [balance.profession_action_xp(level, "gather", 1) for level in (1, 2)]
        self.assertLess(xp[0], xp[1])                           # niveau 2 > niveau 1
        # Un objet sous le niveau du métier rapporte moins, sans tomber à rien.
        self.assertLess(balance.profession_action_xp(1, "gather", 3), xp[0])
        self.assertGreater(balance.profession_action_xp(1, "gather", 10), 0)
        # Environ 50 actions de sa tranche par niveau de métier.
        for level in (1, 5, 10):
            top = balance.profession_action_xp(balance.max_item_level_for(level), "gather", level)
            self.assertAlmostEqual(balance.profession_xp_to_next_level(level) / top, 50, delta=10)

    def test_soin_hors_combat(self):
        player = fakes.new_player(inventory={"pain_de_miel": 1})
        max_hp = compute_final_stats(player)["hp"]
        with self.assertRaises(HTTPException):   # PV pleins : refusé
            ar.use_item(models.UseItemRequest(player_id="joueur", item_id="pain_de_miel"))
        set_hp(fakes.player(), 10, max_hp)
        result = ar.use_item(models.UseItemRequest(player_id="joueur", item_id="pain_de_miel"))
        self.assertGreater(result["healed"], 0)
        self.assertNotIn("pain_de_miel", fakes.player()["inventory"])


if __name__ == "__main__":
    unittest.main()
