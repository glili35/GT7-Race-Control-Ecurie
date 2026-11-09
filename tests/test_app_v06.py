"""Intégration Tk de l'essai local, des téléchargements et des suggestions.

Les fenêtres sont réelles ; la console, la liaison et les téléchargements sont
contrôlés par des doubles sans socket. Sans bureau, la classe est explicitement
ignorée. Les tests Windows et les exécutions Xvfb l'exercent normalement.
"""
from __future__ import annotations

import gc
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class FakeLocalTrial:
    """Deux flux distincts : recevoir une PS5 ne simule pas un envoi HTTPS."""

    def __init__(self, engineer_fleet, data_dir, car_number=1, on_status=None):
        from racecontrol.fleet import FleetMonitor
        from racecontrol.telemetry import TelemetryDiagnostics

        self.engineer_fleet = engineer_fleet
        self.local_fleet = FleetMonitor(timeout_s=engineer_fleet.timeout_s)
        self.data_dir = Path(data_dir)
        self.car_number = car_number
        self.on_status = on_status
        self.running = False
        self.pilot = SimpleNamespace(
            fleet=self.local_fleet, car_number=car_number, running=False,
            diagnostics=TelemetryDiagnostics(), rows_written=0, stopped=False,
        )
        self.hub = SimpleNamespace(running=False, stopped=False)
        self.started_with = None

    def start(self, ps5_ip, record=True):
        self.started_with = (ps5_ip, record)
        self.running = self.pilot.running = self.hub.running = True

    def stop(self):
        self.running = False
        if self.pilot is not None:
            self.pilot.running = False
            self.pilot.stopped = True
        if self.hub is not None:
            self.hub.running = False
            self.hub.stopped = True
        self.local_fleet.reset()
        self.engineer_fleet.reset()
        self.pilot = self.hub = None
        self.on_status = None


class AppV06IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
            del probe
            gc.collect()
        except tk.TclError as exc:
            raise unittest.SkipTest(
                "Affichage Tk indisponible : lancer sous Xvfb ou sur un bureau Windows."
            ) from exc
        from racecontrol.app import RaceControlApp
        from racecontrol.demo import sample
        from racecontrol.circuits import BulkInstallResult
        from racecontrol.track_matcher import MatchResult
        from racecontrol.transport import ConnectionCheck

        cls.App = RaceControlApp
        cls.sample = staticmethod(sample)
        cls.BulkResult = BulkInstallResult
        cls.MatchResult = MatchResult
        cls.ConnectionCheck = ConnectionCheck

    def setUp(self):
        self.main_thread = threading.get_ident()
        self.initial_threads = set(threading.enumerate())
        self.temp = tempfile.TemporaryDirectory()
        self.profile = Path(self.temp.name)
        self.patches = []
        self.releases = []
        self.callback_errors = []
        self.root = self.app = None
        self._patch(patch.dict(os.environ, {"GT7RC_DATA_DIR": str(self.profile)}))
        self._patch(patch("racecontrol.app.local_address", return_value="127.0.0.1"))
        self._patch(patch("racecontrol.app.LocalTrial", FakeLocalTrial))
        for name in ("showerror", "showinfo", "showwarning"):
            self._patch(patch("racecontrol.app.messagebox." + name))
        # Toute activation d'un vrai service par erreur fait échouer le test.
        self._patch(patch("racecontrol.app.EngineerHub", side_effect=AssertionError("Réseau interdit dans le test Tk")))
        self._patch(patch("racecontrol.app.PilotSession", side_effect=AssertionError("UDP interdit dans le test Tk")))
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.report_callback_exception = lambda kind, value, trace: self.callback_errors.append(value)
        self.app = self.App(self.root)
        self.root.update_idletasks()

    def _patch(self, guard):
        result = guard.start()
        self.patches.append(guard)
        return result

    def tearDown(self):
        # Terminer les workers avant de libérer les cycles Python/Tcl. Un
        # worker qui abandonne la dernière référence à Tk provoquerait sinon
        # Tcl_AsyncDelete dans un test réseau ultérieur.
        for release in self.releases:
            release.set()
        if self.root is not None:
            deadline = time.monotonic() + 4
            while self.app is not None and (self.app.busy or self.app.catalog_busy) and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.005)
        workers = [worker for worker in threading.enumerate()
                   if worker not in self.initial_threads and worker.name == "GT7-Action"]
        for worker in workers:
            worker.join(timeout=4)
        if self.root is not None:
            try:
                for timer in self.root.tk.call("after", "info"):
                    self.root.after_cancel(timer)
                if self.app is not None and not self.app.closed:
                    self.app.busy = self.app.catalog_busy = False
                    self.app.close()
                elif self.root.winfo_exists():
                    self.root.destroy()
            except tk.TclError:
                pass
        self.app = self.root = None
        for guard in reversed(self.patches):
            guard.stop()
        self.patches.clear()
        self.callback_errors.clear()
        self.releases.clear()
        self.temp.cleanup()
        gc.collect()

    def _until(self, predicate, message="L'action graphique ne s'est pas terminée"):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.005)
        self.root.update_idletasks()
        self.assertFalse(self.callback_errors, str(self.callback_errors))
        self.assertTrue(predicate(), message)

    def _start_trial(self, car=2):
        self.app.ps5_ip.set("192.0.2.44")
        self.app.local_car.set(str(car))
        self.app.record.set(False)
        self.app.trial_button.invoke()
        self._until(lambda: self.app.local_trial is not None and not self.app.busy)
        return self.app.local_trial

    def _refresh(self):
        self.app.tick()
        self.root.update_idletasks()
        self.assertFalse(self.callback_errors, str(self.callback_errors))

    def test_trial_button_preserves_team_invitation_and_separates_capture_from_engineer(self):
        invitation = "invitation-équipe-déjà-enregistrée"
        self.app.invitation.set(invitation)
        self.app.save_settings(quiet=True)
        trial = self._start_trial(car=3)
        self.assertEqual(trial.started_with, ("192.0.2.44", False))
        self.assertEqual(trial.car_number, 3)
        self.assertIs(self.app.fleet, trial.engineer_fleet)
        self.assertIsNot(self.app.fleet, self.app.pilot.fleet)
        self.assertFalse(any(snapshot.has_data for snapshot in self.app.fleet.snapshots()))
        self.assertEqual(self.app.invitation.get(), invitation)
        saved = json.loads(self.app.config_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["invitation"], invitation)
        self.assertEqual(self.app.banner.winfo_manager(), "pack")
        self.assertIn("VRAIE PS5", self.app.banner.cget("text"))
        self.assertIn("Arrêter", self.app.trial_button.cget("text"))

    def test_local_ps5_data_does_not_claim_engineer_transport_is_valid(self):
        trial = self._start_trial()
        trial.local_fleet.ingest(2, self.sample(2, 3))
        trial.pilot.diagnostics.record_decoded()
        self._refresh()
        self.assertEqual(self.app.pilot_status.get(), "PS5 en direct")
        self.assertIn("en attente de la transmission", self.app.trial_status.get())
        self.assertIn("0 / 4", self.app.summary.get())
        self.assertFalse(self.app.fleet.snapshots()[1].connected)
        self.assertEqual(self.app.cards[2].speed.cget("text"), "—")

        trial.engineer_fleet.ingest(2, self.sample(2, 4))
        self._refresh()
        self.assertIn("Chaîne locale complète en direct", self.app.trial_status.get())
        self.assertIn("1 / 4", self.app.summary.get())
        self.assertEqual(self.app.cards[2].status.cget("text"), "EN DIRECT")

    def _assert_trial_stop_from(self, button_name):
        trial = self._start_trial()
        pilot, hub = trial.pilot, trial.hub
        trial.local_fleet.ingest(2, self.sample(2, 3))
        trial.engineer_fleet.ingest(2, self.sample(2, 3))
        self._refresh()
        getattr(self.app, button_name).invoke()
        self._until(lambda: self.app.local_trial is None and not self.app.busy)
        self.assertFalse(trial.running)
        self.assertTrue(pilot.stopped)
        self.assertTrue(hub.stopped)
        self.assertIsNone(self.app.pilot)
        self.assertIsNone(self.app.hub)
        self.assertFalse(any(snapshot.has_data for snapshot in self.app.fleet.snapshots()))
        self.assertFalse(any(snapshot.has_data for snapshot in trial.local_fleet.snapshots()))
        self.assertEqual(self.app.banner.winfo_manager(), "")
        self.assertEqual(self.app.hub_status.get(), "Réception fermée")
        self.assertIn("arrêtée", self.app.pilot_status.get())
        self.assertEqual(self.app.trial_button.cget("text"), "Démarrer l'essai complet")

    def test_pilot_stop_button_closes_both_sides_of_local_trial(self):
        self._assert_trial_stop_from("pilot_button")

    def test_engineer_stop_button_closes_both_sides_of_local_trial(self):
        self._assert_trial_stop_from("hub_button")

    def test_invitation_check_neither_creates_telemetry_nor_refreshes_its_timestamp(self):
        seen_at = time.monotonic() - 10
        self.app.fleet.ingest(1, self.sample(1, 2), now=seen_at)
        before = self.app.fleet.snapshots(now=seen_at + 1)
        for result in (
            self.ConnectionCheck(True, "ok", "Ingénieur joignable · invitation acceptée"),
            self.ConnectionCheck(False, "unreachable", "Ingénieur injoignable"),
        ):
            with self.subTest(ok=result.ok), patch("racecontrol.app.check_invitation", return_value=result):
                self.app.test_connection()
                self._until(lambda: not self.app.busy and self.app.connection_status.get() == result.message)
                self.assertEqual(self.app.fleet.snapshots(now=seen_at + 1), before)
                self.assertFalse(self.app.fleet.snapshots(now=seen_at + 4)[0].connected)
                self.assertIn("0 / 4", self.app.summary.get())

    def test_bulk_download_updates_tk_from_main_thread_and_reloads_map_and_matcher(self):
        selected, failed = self.app.library.noms[:2]
        self.app.circuit.set(selected)
        self.app.recognition.set(True)
        old_matcher = SimpleNamespace(reset=lambda **kwargs: None)
        self.app.matcher = old_matcher
        self.app.suggested.set(selected)
        release = threading.Event()
        self.releases.append(release)
        progress_sent = threading.Event()
        installed = threading.Event()
        points = [(0., 0., "L"), (50., 10., "L"), (20., 50., "R")]
        mutation_threads = []
        original_set = self.app.circuit_status.set

        def set_status(value):
            mutation_threads.append(threading.get_ident())
            original_set(value)

        def install_all(*, progress, cancel):
            progress(1, 2, selected, "installé")
            progress_sent.set()
            if not release.wait(3):
                raise RuntimeError("Téléchargement de test non libéré")
            installed.set()
            return self.BulkResult((selected,), (), {failed: "Source indisponible"}, False)

        def new_matcher(library, **kwargs):
            return SimpleNamespace(
                loaded_points=library.charger_points(selected),
                reset=lambda **kwargs: None,
                observe=lambda x, z: self.MatchResult("waiting", message="Attente de coordonnées"),
            )

        with patch.object(self.app.library, "installer_tous", side_effect=install_all), \
             patch.object(self.app.library, "charger_points", side_effect=lambda name: points if installed.is_set() else []), \
             patch.object(self.app.circuit_status, "set", side_effect=set_status), \
             patch("racecontrol.app.TrackMatcher", side_effect=new_matcher):
            self.app.download_all_circuits()
            self._until(lambda: progress_sent.is_set() and "1/2" in self.app.circuit_status.get())
            self.assertTrue(self.app.catalog_busy)
            self.assertEqual(set(mutation_threads), {self.main_thread})
            release.set()
            self._until(lambda: not self.app.catalog_busy and not self.app.busy and self.app.matcher is not old_matcher)
            self.assertEqual(self.app.points, points)
            self.assertEqual(self.app.matcher.loaded_points, points)
            self.assertEqual(self.app.suggested.get(), "")
            self.assertIn("1 tracés prêts", self.app.circuit_status.get())
            self.assertIn("1 échecs", self.app.circuit_status.get())
            self.assertEqual(set(mutation_threads), {self.main_thread})

    def test_catalog_download_does_not_block_stopping_local_capture(self):
        trial = self._start_trial()
        release = threading.Event()
        self.releases.append(release)

        def install_all(*, progress, cancel):
            if not release.wait(3):
                raise RuntimeError("Téléchargement de test non libéré")
            return self.BulkResult((), (), {}, False)

        with patch.object(self.app.library, "installer_tous", side_effect=install_all):
            self.app.download_all_circuits()
            self.assertTrue(self.app.catalog_busy)
            self.app.pilot_button.invoke()
            self._until(lambda: self.app.local_trial is None and not self.app.busy)
            self.assertFalse(trial.running)
            self.assertTrue(self.app.catalog_busy, "L'arrêt de capture doit aboutir avant le téléchargement.")
            self.assertEqual(self.app.banner.winfo_manager(), "")
            release.set()
            self._until(lambda: not self.app.catalog_busy)

    def test_ambiguous_track_requires_explicit_selection_and_apply(self):
        current, first, second = self.app.library.noms[:3]
        self.app.circuit.set(current)
        self.app.recognition.set(True)
        self.app.fleet.ingest(1, self.sample(1, 1))
        self.app.matcher = SimpleNamespace(
            reset=lambda **kwargs: None,
            observe=lambda x, z: self.MatchResult(
                "ambiguous", candidates=(first, second), message="Deux configurations possibles"),
        )
        self.app.update_recognition(self.app.fleet.snapshots())
        self.assertEqual(tuple(self.app.suggestion_combo.cget("values")), (first, second))
        self.assertEqual(self.app.suggested.get(), "")
        self.assertEqual(self.app.circuit.get(), current)
        self.app.apply_suggestion()
        self.assertEqual(self.app.circuit.get(), current)
        self.app.suggested.set(second)
        self.app.apply_suggestion()
        self.assertEqual(self.app.circuit.get(), second)

    def test_single_track_proposal_and_offline_result_never_change_selected_circuit(self):
        current, candidate = self.app.library.noms[:2]
        self.app.circuit.set(current)
        self.app.recognition.set(True)
        self.app.fleet.ingest(1, self.sample(1, 1))
        self.app.matcher = SimpleNamespace(
            reset=lambda **kwargs: None,
            observe=lambda x, z: self.MatchResult(
                "candidate", candidate=candidate, candidates=(candidate,), message="Proposition à confirmer"),
        )
        self.app.update_recognition(self.app.fleet.snapshots())
        self.assertEqual(self.app.suggested.get(), candidate)
        self.assertEqual(self.app.circuit.get(), current)
        self.app.matcher.observe = lambda x, z: self.MatchResult("unavailable", message="Aucun tracé installé")
        self.app.update_recognition(self.app.fleet.snapshots())
        self.assertEqual(self.app.suggested.get(), "")
        self.assertEqual(tuple(self.app.suggestion_combo.cget("values")), ())
        self.assertEqual(self.app.circuit.get(), current)

    def test_window_is_destroyed_even_when_trial_stop_raises(self):
        trial = self._start_trial()
        stop = trial.stop

        def failing_stop():
            stop()
            raise RuntimeError("Échec de fermeture contrôlé")

        trial.stop = failing_stop
        with self.assertRaisesRegex(RuntimeError, "Échec de fermeture contrôlé"):
            self.app.close()
        self.assertTrue(self.app.closed)
        with self.assertRaises(tk.TclError):
            self.root.winfo_exists()

    def test_legacy_window_is_not_opened_while_an_action_is_in_progress(self):
        for flag in ("busy", "catalog_busy"):
            with self.subTest(action=flag), patch("racecontrol.app.tk.Toplevel", side_effect=AssertionError("Fenêtre interdite pendant une action")):
                setattr(self.app, flag, True)
                try:
                    self.app.open_legacy()
                    self.assertEqual(self.app.legacy_windows, [])
                finally:
                    setattr(self.app, flag, False)


if __name__ == "__main__":
    unittest.main()
