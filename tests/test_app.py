"""Régressions d'intégration de l'interface Tk, sans PS5 ni serveur extérieur.

Sous Linux ces tests utilisent un affichage X11, par exemple Xvfb. Sans
affichage utilisable la classe entière est ignorée avec un motif explicite.
Les profils et secrets éventuels sont exclusivement dans un dossier temporaire.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class AppIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
        except tk.TclError as exc:
            raise unittest.SkipTest("Affichage Tk indisponible : lancer avec Xvfb ou sur un bureau Windows.") from exc
        from racecontrol.app import RaceControlApp
        from racecontrol.demo import sample
        from racecontrol.telemetry import TelemetryDiagnostics

        cls.App = RaceControlApp
        cls.sample = staticmethod(sample)
        cls.Diagnostics = TelemetryDiagnostics

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name)
        env = patch.dict(os.environ, {"GT7RC_DATA_DIR": str(self.profile)})
        env.start()
        self.addCleanup(env.stop)
        for target in ("showerror", "showinfo", "showwarning"):
            guard = patch("racecontrol.app.messagebox." + target)
            guard.start()
            self.addCleanup(guard.stop)
        self.windows = []
        self.addCleanup(self._close_windows)
        self.root, self.app = self._new_app()

    def _new_app(self):
        root = tk.Tk()
        root.withdraw()
        self.windows.append((root, None))
        app = self.App(root)
        self.windows[-1] = (root, app)
        root.update_idletasks()
        return root, app

    @staticmethod
    def _close_window(root, app):
        try:
            # Ne pas laisser un ancien after() se déclencher dans un autre
            # interpréteur Tk créé par le test suivant.
            for timer in root.tk.call("after", "info"):
                root.after_cancel(timer)
            if app is not None and not app.closed:
                app.busy = False
                app.close()
            elif root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass

    def _close_windows(self):
        for root, app in reversed(self.windows):
            self._close_window(root, app)

    def _attach_local_pilot(self):
        diagnostics = self.Diagnostics()
        self.app.pilot = SimpleNamespace(
            running=True,
            car_number=1,
            diagnostics=diagnostics,
            rows_written=0,
            stop=lambda: None,
        )
        return diagnostics

    def test_demo_populates_four_cards_without_starting_network_or_capture(self):
        with patch("racecontrol.app.EngineerHub") as hub, \
             patch("racecontrol.app.PilotSession") as pilot, \
             patch("socket.socket") as socket_factory:
            self.app.toggle_demo()
            self.app.tick()
            self.root.update_idletasks()
        self.assertTrue(self.app.demo)
        self.assertEqual(sum(car.connected for car in self.app.fleet.snapshots()), 4)
        self.assertIn("4 / 4", self.app.summary.get())
        self.assertIn("simulées", self.app.summary.get())
        self.assertIn("SIMULÉES", self.app.banner.cget("text"))
        self.assertEqual(self.app.banner.winfo_manager(), "pack")
        for card in self.app.cards.values():
            self.assertNotEqual(card.speed.cget("text"), "—")
            self.assertTrue(card.grid_info())
        hub.assert_not_called()
        pilot.assert_not_called()
        socket_factory.assert_not_called()
        self.assertIsNone(self.app.hub)
        self.assertIsNone(self.app.pilot)
        self.assertFalse((self.profile / "sessions").exists())
        self.assertFalse((self.profile / "reception").exists())

    def test_leaving_demo_clears_simulated_data_before_real_session(self):
        self.app.toggle_demo()
        self.app.tick()
        self.assertTrue(any(car.has_data for car in self.app.fleet.snapshots()))
        self.app.toggle_demo()
        self.app.tick()
        self.assertFalse(self.app.demo)
        self.assertEqual(self.app.banner.winfo_manager(), "")
        self.assertTrue(all(not car.has_data for car in self.app.fleet.snapshots()))
        self.assertIn("0 / 4 voitures en direct", self.app.summary.get())
        for card in self.app.cards.values():
            self.assertEqual(card.speed.cget("text"), "—")
            self.assertEqual(card.status.cget("text"), "EN ATTENTE")

    def test_preferences_are_written_and_reloaded_by_a_new_window(self):
        self.app.names[0].set("Écurie bleue / Léa")
        self.app.names[3].set("Voiture endurance")
        self.app.ps5_ip.set("192.168.0.44")
        self.app.host.set("ingenieur.example.test")
        self.app.port.set("8877")
        self.app.local_car.set("4")
        self.app.rotation.set("270")
        self.app.circuit.set(self.app.library.noms[0])
        self.app.invitation.set("invitation-de-test-conservee-dans-le-profil")
        self.app.save_settings(quiet=True)
        saved = json.loads((self.profile / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["driver_1"], "Écurie bleue / Léa")
        self.assertEqual(saved["rotation"], "270")
        if os.name != "nt":
            self.assertEqual((self.profile / "settings.json").stat().st_mode & 0o777, 0o600)
        self._close_window(self.root, self.app)
        _, restored = self._new_app()
        self.assertEqual(restored.names[0].get(), "Écurie bleue / Léa")
        self.assertEqual(restored.names[3].get(), "Voiture endurance")
        self.assertEqual(restored.ps5_ip.get(), "192.168.0.44")
        self.assertEqual(restored.host.get(), "ingenieur.example.test")
        self.assertEqual(restored.port.get(), "8877")
        self.assertEqual(restored.local_car.get(), "4")
        self.assertEqual(restored.rotation.get(), "270")
        self.assertEqual(restored.circuit.get(), saved["circuit"])
        self.assertEqual(restored.invitation.get(), saved["invitation"])

    def test_completion_reaches_widgets_even_when_diagnostic_queue_is_full(self):
        for i in range(400):
            self.app.log(f"Diagnostic fréquent {i}")
        self.assertTrue(self.app.events.full())
        self.app.work(lambda: "Terminé et visible", self.app.hub_status.set)
        deadline = time.monotonic() + 2
        while self.app.actions.empty() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(self.app.actions.empty(), "Le thread de travail n'a pas envoyé sa fin.")
        self.assertTrue(self.app.events.full(), "Le journal doit encore être saturé avant le traitement Tk.")
        self.app.tick()
        self.assertEqual(self.app.hub_status.get(), "Terminé et visible")
        self.assertFalse(self.app.busy)

    def test_recent_invalid_datagrams_do_not_hide_loss_of_valid_ps5_data(self):
        diagnostics = self._attach_local_pilot()
        self.app.fleet.ingest(1, self.sample(1, 5), now=time.monotonic() - 4)
        diagnostics.record_datagram("192.168.0.44")
        diagnostics.record_packet_format_error("Paquet de test invalide")
        self.app.tick()
        self.assertLess(diagnostics.snapshot().last_packet_age_seconds, 1)
        self.assertIn("Signal PS5 interrompu", self.app.pilot_status.get())
        self.assertEqual(self.app.cards[1].status.cget("text"), "SIGNAL PERDU")
        self.assertEqual(self.app.cards[1].speed.cget("text"), "—")
        self.assertIn("1 erreurs", self.app.diagnostic.get())
        self.app.fleet.ingest(1, self.sample(1, 6))
        diagnostics.record_decoded()
        self.app.tick()
        self.assertEqual(self.app.pilot_status.get(), "PS5 en direct")
        self.assertEqual(self.app.cards[1].status.cget("text"), "EN DIRECT")

    def test_engineer_link_status_is_separate_from_ps5_and_general_logs(self):
        self._attach_local_pilot()
        self.app.fleet.ingest(1, self.sample(1, 5))
        self.app.pilot_message("Télémétrie transmise au poste ingénieur")
        self.app.pilot_message("Récepteur GT7 démarré sur le réseau local")
        self.app.tick()
        self.assertEqual(self.app.pilot_status.get(), "PS5 en direct")
        self.assertEqual(self.app.link_status.get(), "Télémétrie transmise au poste ingénieur")
        self.app.pilot_message("Ingénieur injoignable · nouvelle tentative")
        self.app.pilot_message("Enregistrement CSV poursuivi")
        self.app.tick()
        self.assertEqual(self.app.pilot_status.get(), "PS5 en direct")
        self.assertEqual(self.app.link_status.get(), "Ingénieur injoignable · nouvelle tentative")
        journal = self.app.journal.get("1.0", "end")
        self.assertIn("Récepteur GT7 démarré", journal)
        self.assertIn("Enregistrement CSV poursuivi", journal)


if __name__ == "__main__":
    unittest.main()
