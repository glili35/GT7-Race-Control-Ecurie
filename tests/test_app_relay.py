"""Parcours Tk du relais et des essais locaux, sans véritable console PS5.

Les connexions WebSocket et les clients ingénieur sont réels en boucle locale.
La capture est contrôlée et les profils sont exclusivement temporaires. Ces
essais ne valident ni une connexion Internet ni quatre consoles physiques.
"""
from __future__ import annotations

from dataclasses import replace
import gc
import os
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from racecontrol.local_trial import LocalTrial
from racecontrol.relay_protocol import new_secret, parse_invitation
from racecontrol.relay_server import RelayServer
from racecontrol.relay_trial import RelayLocalTrial
from racecontrol.telemetry import TelemetryDiagnostics

try:
    from test_local_trial import ControlledReceiver, sample
except ModuleNotFoundError:
    from tests.test_local_trial import ControlledReceiver, sample


class AppRelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
        except tk.TclError as exc:
            raise unittest.SkipTest("Affichage Tk indisponible : lancer avec Xvfb ou sur un bureau Windows.") from exc
        from racecontrol.app import RaceControlApp
        cls.App = RaceControlApp

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.directory.name)
        self.profile = self.workspace / "application"
        self.patchers = []
        environment = patch.dict(os.environ, {"GT7RC_DATA_DIR": str(self.profile)})
        self.patchers.append(environment)
        environment.start()
        self.boxes = {}
        for name in ("showerror", "showwarning", "showinfo"):
            guard = patch("racecontrol.app.messagebox." + name)
            self.patchers.append(guard)
            self.boxes[name] = guard.start()
        ControlledReceiver.instances = []
        ControlledReceiver.fail_start = False
        receiver = patch("racecontrol.runtime.GT7Receiver", ControlledReceiver)
        self.patchers.append(receiver)
        receiver.start()
        self.servers = []
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = self.App(self.root)
        self.root.update_idletasks()

    def tearDown(self):
        # Un GT7-Action ne doit pas garder un callback/objet Tk vivant après la
        # destruction de l'interpréteur. Ses résultats sont traités avant close.
        deadline = time.monotonic() + 8
        while not self.app.closed and self.app.busy and time.monotonic() < deadline:
            self.app.tick()
            self.root.update_idletasks()
            time.sleep(0.005)
        for worker in tuple(threading.enumerate()):
            if worker.name == "GT7-Action" and worker is not threading.current_thread():
                worker.join(timeout=7)
        try:
            if not self.app.closed:
                self.app.tick()
            for timer in self.root.tk.call("after", "info"):
                self.root.after_cancel(timer)
            if not self.app.closed:
                self.app.close()
        except tk.TclError:
            pass
        finally:
            for server in reversed(self.servers):
                server.stop()
            self.servers.clear()
            try:
                if self.root.winfo_exists():
                    self.root.destroy()
            except tk.TclError:
                pass
        self.app = None
        self.root = None
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.patchers.clear()
        self.boxes.clear()
        self.directory.cleanup()
        gc.collect()

    def wait_for(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.tick()
            self.root.update_idletasks()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("L'interface relais n'a pas atteint l'état attendu")

    def idle(self):
        self.wait_for(lambda: not self.app.busy)

    def connect_relay(self):
        bootstrap = new_secret()
        server = RelayServer(self.workspace / f"backend-{len(self.servers)}", bootstrap,
                             host="127.0.0.1", port=0)
        server.start()
        self.servers.append(server)
        self.app.connection_mode.set("Relais hébergé")
        self.app.change_connection_mode()
        self.app.relay_url.set(f"ws://127.0.0.1:{server.port}/relay/v1")
        self.app.relay_access.set(bootstrap)
        self.app.relay_button.invoke()
        self.wait_for(lambda: not self.app.busy and self.app.relay_engineer is not None)
        self.assertTrue(self.app.relay_engineer.connected)
        return server

    def attach_local_capture_surface(self):
        pilot = SimpleNamespace(running=True, car_number=1, fleet=self.app.fleet,
                                diagnostics=TelemetryDiagnostics(), rows_written=0,
                                recording_active=False, recording_error=None, rows_dropped=0)
        pilot.stop = lambda: setattr(pilot, "running", False)
        self.app.pilot = pilot
        return pilot

    def prepare_visible_map(self):
        self.root.deiconify()
        self.app.points = [(100.0, 430.0, "L"), (150.0, 430.0, "R"),
                           (100.0, 480.0, "L"), (150.0, 480.0, "R")]
        self.root.update_idletasks()
        self.app.draw_track()
        self.assertIsNotNone(self.app.transform)
        self.assertGreater(self.app.canvas.winfo_width(), 20)

    def test_default_is_direct_and_no_relay_or_capture_starts_implicitly(self):
        self.assertEqual(self.app.connection_mode.get(), "Connexion directe")
        self.assertEqual(self.app.trial_transport.get(), "Direct")
        self.assertEqual(self.app.direct_controls.winfo_manager(), "pack")
        self.assertEqual(self.app.relay_controls.winfo_manager(), "")
        self.assertIsNone(self.app.relay_engineer)
        self.assertIsNone(self.app.hub)
        self.assertIsNone(self.app.pilot)
        self.assertFalse(ControlledReceiver.instances)
        self.assertIsNone(self.app.update_install_blocker())

    def test_mode_change_and_update_install_are_blocked_by_an_active_relay(self):
        self.connect_relay()
        self.app.connection_mode.set("Connexion directe")
        self.app.change_connection_mode()
        self.assertEqual(self.app.connection_mode.get(), "Relais hébergé")
        self.assertEqual(self.app.relay_controls.winfo_manager(), "pack")
        self.boxes["showinfo"].assert_called_once()
        self.assertIn("ferme la réception", self.app.update_install_blocker())
        self.app.relay_button.invoke()
        self.wait_for(lambda: not self.app.busy and self.app.relay_engineer is None)
        self.assertIsNone(self.app.update_install_blocker())
        self.app.connection_mode.set("Connexion directe")
        self.app.change_connection_mode()
        self.assertEqual(self.app.connection_mode.get(), "Connexion directe")

    def test_engineer_connects_and_disconnects_via_real_loopback_websocket(self):
        self.connect_relay()
        self.assertIsNone(self.app.hub)
        self.assertIsNone(self.app.pilot)
        self.assertEqual(self.app.relay_button.cget("text"), "Déconnecter le relais")
        self.assertIn("Relais connecté", self.app.hub_status.get())
        self.assertTrue(all(not car.has_data for car in self.app.fleet.snapshots()))
        self.app.relay_button.invoke()
        self.wait_for(lambda: not self.app.busy and self.app.relay_engineer is None)
        self.assertEqual(self.app.relay_button.cget("text"), "Connecter le relais")
        self.assertIn("déconnecté", self.app.hub_status.get())
        self.assertFalse(ControlledReceiver.instances)
        self.boxes["showerror"].assert_not_called()

    def test_copy_and_rotate_select_the_correct_car_without_changing_other_accesses(self):
        self.connect_relay()
        session = self.app.relay_engineer
        before = {number: session.invitation(number) for number in range(1, 5)}
        saved_pilot_invitation = "invitation-du-pilote-conservee"
        self.app.invitation.set(saved_pilot_invitation)
        self.app.slot.set("3")
        self.app.copy_invitation()
        copied = self.root.clipboard_get()
        self.assertEqual(parse_invitation(copied).car, 3)
        self.assertEqual(copied, before[3])
        self.app.rotate_invitation()
        self.idle()
        after = {number: session.invitation(number) for number in range(1, 5)}
        copied_new = self.root.clipboard_get()
        self.assertEqual(copied_new, after[3])
        old, new = parse_invitation(before[3]), parse_invitation(after[3])
        self.assertEqual(new.car, 3)
        self.assertNotEqual(old.token, new.token)
        self.assertNotEqual(old.key, new.key)
        for number in (1, 2, 4):
            self.assertEqual(before[number], after[number])
        self.assertEqual(self.app.invitation.get(), saved_pilot_invitation)
        self.assertIn("voiture 3", self.app.network_hint.cget("text"))
        self.boxes["showerror"].assert_not_called()

    def test_local_relay_trial_button_starts_one_capture_and_other_button_stops_it(self):
        self.app.trial_transport.set("Relais local")
        self.app.ps5_ip.set("192.168.0.44")
        self.app.local_car.set("2")
        self.app.record.set(False)
        self.app.invitation.set("invitation-utilisateur-a-ne-pas-remplacer")
        self.app.trial_button.invoke()
        self.wait_for(lambda: not self.app.busy and self.app.local_trial is not None)
        self.assertIsInstance(self.app.local_trial, RelayLocalTrial)
        self.assertTrue(self.app.local_trial.running)
        self.assertTrue(self.app.local_trial.engineer.connected)
        self.assertEqual(len(ControlledReceiver.instances), 1)
        self.assertEqual(ControlledReceiver.instances[0].kwargs["ps5_ip"], "192.168.0.44")
        self.assertIs(self.app.pilot, self.app.local_trial.pilot)
        self.assertIs(self.app.hub, self.app.local_trial.hub)
        self.assertIn("ESSAI RELAIS LOCAL", self.app.banner.cget("text"))
        self.assertIsNotNone(self.app.update_install_blocker())
        self.assertTrue(all(not car.has_data for car in self.app.fleet.snapshots()))
        self.assertEqual(self.app.invitation.get(), "invitation-utilisateur-a-ne-pas-remplacer")
        self.app.pilot_button.invoke()
        self.wait_for(lambda: not self.app.busy and self.app.local_trial is None)
        self.assertTrue(ControlledReceiver.instances[0].stopped)
        self.assertIsNone(self.app.pilot)
        self.assertIsNone(self.app.hub)
        self.assertEqual(self.app.banner.winfo_manager(), "")
        self.assertFalse(tuple(self.profile.glob("essai-relais-*")))
        self.assertIsNone(self.app.update_install_blocker())

    def test_default_direct_trial_remains_available_through_the_same_button(self):
        self.app.ps5_ip.set("192.168.0.44")
        self.app.record.set(False)
        self.app.trial_button.invoke()
        self.wait_for(lambda: not self.app.busy and self.app.local_trial is not None)
        self.assertIsInstance(self.app.local_trial, LocalTrial)
        self.assertNotIsInstance(self.app.local_trial, RelayLocalTrial)
        self.assertEqual(len(ControlledReceiver.instances), 1)
        self.app.hub_button.invoke()
        self.wait_for(lambda: not self.app.busy and self.app.local_trial is None)
        self.assertTrue(ControlledReceiver.instances[0].stopped)
        self.assertIsNone(self.app.hub)
        self.assertEqual(self.app.trial_transport.get(), "Direct")

    def test_loading_and_inactive_flux_keep_connection_but_hide_driving_and_map_values(self):
        self.attach_local_capture_surface()
        self.prepare_visible_map()
        self.app.fleet.ingest(1, replace(sample(240.0), flags=1))
        self.app.tick()
        self.assertEqual(self.app.cards[1].status.cget("text"), "EN DIRECT")
        self.assertTrue(self.app.canvas.find_withtag("cars"))
        for flags, card_status, pilot_status in ((4, "CHARGEMENT", "chargement"), (0, "HORS PISTE", "hors piste")):
            with self.subTest(flags=flags):
                self.app.fleet.ingest(1, replace(sample(240.0), flags=flags))
                self.app.tick()
                self.assertEqual(self.app.cards[1].status.cget("text"), card_status)
                self.assertEqual(self.app.cards[1].speed.cget("text"), "—")
                self.assertEqual(self.app.cards[1].values["fuel"].cget("text"), "—")
                self.assertIn(pilot_status, self.app.pilot_status.get())
                self.assertFalse(self.app.canvas.find_withtag("cars"))
                self.assertTrue(self.app.fleet.snapshots()[0].connected)

    def test_paused_car_is_labelled_as_paused_instead_of_driving(self):
        self.attach_local_capture_surface()
        self.prepare_visible_map()
        self.app.fleet.ingest(1, replace(sample(120.0), flags=1))
        self.app.tick()
        self.app.fleet.ingest(1, replace(sample(120.0), flags=3))
        self.app.tick()
        self.assertEqual(self.app.cards[1].status.cget("text"), "EN PAUSE")
        self.assertNotEqual(self.app.cards[1].speed.cget("text"), "—")
        self.assertTrue(self.app.canvas.find_withtag("cars"))
        self.assertIn("jeu en pause", self.app.pilot_status.get())
        self.assertNotIn("PS5 en direct", self.app.pilot_status.get())
        self.assertTrue(self.app.fleet.snapshots()[0].connected)
        self.assertEqual(self.app.fleet.snapshots()[0].activity_state, "paused")


    def test_real_relay_pilot_statuses_reach_link_indicator_without_hiding_ps5_state(self):
        self.attach_local_capture_surface()
        self.app.fleet.ingest(1, sample())
        statuses = (
            "Télémétrie chiffrée transmise au relais.",
            "Invitation relais refusée. Demande une nouvelle invitation à l’ingénieur.",
            "Emplacement relais indisponible. Nouvelle tentative automatique.",
            "Transmission relais arrêtée.",
        )
        for status in statuses:
            with self.subTest(status=status):
                self.app.pilot_message(status)
                self.app.pilot_message("Enregistrement CSV poursuivi dans le profil utilisateur.")
                self.app.tick()
                self.assertEqual(self.app.link_status.get(), status)
                self.assertEqual(self.app.pilot_status.get(), "PS5 en direct")
                self.assertIn(status, self.app.journal.get("1.0", "end"))

    def test_unknown_lap_count_is_not_presented_as_a_free_practice_session(self):
        self.app.tick()
        race = self.app.cards[1].race.cget("text")
        self.assertIn(" / —", race)
        self.assertNotIn("LIBRE", race)
        self.app.fleet.ingest(1, replace(sample(), total_laps=0))
        self.app.tick()
        self.assertIn(" / LIBRE", self.app.cards[1].race.cget("text"))
        # Le chargement masque à nouveau le compteur, même si le paquet brut
        # contenait zéro : l'absence d'information ne signifie pas séance libre.
        self.app.fleet.ingest(1, replace(sample(), flags=4, total_laps=0))
        self.app.tick()
        race = self.app.cards[1].race.cget("text")
        self.assertIn(" / —", race)
        self.assertNotIn("LIBRE", race)

    def test_action_thread_start_failure_releases_both_busy_modes_and_allows_close(self):
        executed = []
        completed = []
        for catalog in (False, True):
            with self.subTest(catalog=catalog):
                with patch("racecontrol.app.threading.Thread.start", side_effect=RuntimeError("worker indisponible")):
                    self.app.work(lambda: executed.append("opération"),
                                  lambda _: completed.append("succès"), catalog=catalog)
                self.assertFalse(self.app.busy)
                self.assertFalse(self.app.catalog_busy)
                self.app.tick()
                self.assertFalse(self.app.busy)
                self.assertFalse(self.app.catalog_busy)
                self.assertFalse(executed)
                self.assertFalse(completed)
                self.assertIn("Impossible de démarrer l'action", self.app.journal.get("1.0", "end"))
        self.assertEqual(self.boxes["showerror"].call_count, 2)
        self.assertIsNone(self.app.update_install_blocker())
        self.app.close()
        self.assertTrue(self.app.closed, "Un faux état occupé ne doit pas différer indéfiniment la fermeture")


if __name__ == "__main__":
    unittest.main()
