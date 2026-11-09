"""Garde d'installation et fermeture au niveau de l'application Tk complète."""
from __future__ import annotations

import csv
import gc
import os
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class FakeUpdateLifecycle:
    """Une tâche téléchargée doit accuser réception de l'annulation avant close."""
    def __init__(self):
        self.busy = True
        self.installing = False
        self.cancel_requested = threading.Event()
        self.finished = threading.Event()

    def request_close(self):
        self.cancel_requested.set()

    def poll(self):
        if self.finished.is_set():
            self.busy = False


class UpdateAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
        except tk.TclError as exc:
            raise unittest.SkipTest("Affichage Tk indisponible : lancer avec Xvfb ou sur un bureau Windows.") from exc
        from racecontrol.app import RaceControlApp
        from racecontrol.runtime import PilotSession
        from racecontrol.telemetry.csv_recorder import GT7CSVRecorder
        try:
            from test_runtime import FakeReceiver, sample
        except ModuleNotFoundError:
            from tests.test_runtime import FakeReceiver, sample
        cls.App = RaceControlApp
        cls.PilotSession = PilotSession
        cls.Recorder = GT7CSVRecorder
        cls.Receiver = FakeReceiver
        cls.sample = staticmethod(sample)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.profile = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"GT7RC_DATA_DIR": str(self.profile)})
        self.environment.start()
        self.errors = patch("racecontrol.app.messagebox.showerror")
        self.errors.start()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = self.App(self.root)
        self.root.update_idletasks()

    def tearDown(self):
        try:
            if not self.app.closed:
                self.app.busy = self.app.catalog_busy = False
                self._cancel_timers()
                self.app.close()
        finally:
            self.app = None
            self.root = None
            # Les variables et widgets Tk doivent être libérés sur le thread
            # principal, avant qu'un prochain worker réseau ne déclenche le GC.
            gc.collect()
            self.errors.stop()
            self.environment.stop()
            self.temp.cleanup()

    def _cancel_timers(self):
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("La tâche de l'application n'a pas terminé dans le délai de test.")

    def test_retired_local_trial_csv_blocks_install_until_actual_close(self):
        entered, release = threading.Event(), threading.Event()
        original_record = self.Recorder.record

        def blocked_record(recorder, telemetry):
            entered.set()
            release.wait(3)
            original_record(recorder, telemetry)

        self.Receiver.instances = []
        self.Receiver.fail_start = False
        pilot = self.PilotSession(self.app.fleet, self.profile, on_status=self.app.pilot_message)
        pilot.WRITER_STOP_TIMEOUT = 0.01

        class StoppedTrial:
            def __init__(self):
                self.pilot = pilot
                self.hub = object()

            def stop(self):
                pilot.stop()
                self.pilot = self.hub = None

        with patch("racecontrol.runtime.GT7Receiver", self.Receiver), \
             patch("racecontrol.runtime.GT7CSVRecorder.record", new=blocked_record), \
             patch("racecontrol.update_ui.launch_installer") as launch:
            try:
                pilot.start("192.168.0.44")
                self.Receiver.instances[-1].emit(self.sample(123))
                self.assertTrue(entered.wait(1))
                trial = StoppedTrial()
                self.app.local_trial, self.app.pilot, self.app.hub = trial, pilot, trial.hub
                self.app.toggle_local_trial()
                self.wait_for(lambda: not self.app.busy)
                self.assertIsNone(self.app.pilot)
                self.assertIsNone(self.app.local_trial)
                self.assertIsNone(self.app.hub)
                self.assertFalse(pilot.running)
                self.assertTrue(pilot.recording_active)
                self.assertIn(pilot, self.app._retired_pilots)

                # Le bouton passe par la vraie garde de l'application, même
                # lorsque ses références de capture ont déjà été effacées.
                self.app.updates.info = SimpleNamespace(version="9.9.9")
                self.app.updates.update()
                self.assertIn("session est encore en cours de fermeture", self.app.updates.status.get())
                launch.assert_not_called()
                self.assertFalse(self.app.updates.busy)

                release.set()
                pilot._writer.join(timeout=1)
                self.assertFalse(pilot.recording_active)
                self.assertIsNone(self.app.updates.can_install())
                self.assertEqual(self.app._retired_pilots, [])
                with pilot.file_path.open(encoding="utf-8", newline="") as stream:
                    rows = list(csv.DictReader(stream, delimiter=";"))
                self.assertEqual([float(row["speed_kmh"]) for row in rows], [123])
            finally:
                release.set()
                pilot.stop()

    def test_active_capture_or_hub_blocks_installation(self):
        self.app.updates.info = SimpleNamespace(version="9.9.9")
        with patch("racecontrol.update_ui.launch_installer") as launch:
            self.app.hub = SimpleNamespace(stop=lambda: None)
            self.app.updates.update()
            self.assertIn("Arrête la capture", self.app.updates.status.get())
            self.app.hub = None
            self.app.pilot = SimpleNamespace(running=True, recording_active=False, stop=lambda: None)
            self.app.updates.update()
            self.assertIn("Arrête la capture", self.app.updates.status.get())
            self.app.pilot = None
            launch.assert_not_called()
        self.assertIsNone(self.app.update_install_blocker())

    def test_close_cancels_update_and_waits_for_worker_completion(self):
        updates = FakeUpdateLifecycle()
        self.app.updates = updates
        executed = threading.Event()
        self.app.close()
        self.assertTrue(self.app._closing)
        self.assertTrue(updates.cancel_requested.is_set())
        self.assertFalse(self.app.closed)
        self.assertTrue(self.root.winfo_exists())
        self.app.work(executed.set, lambda _: None)
        self.assertFalse(executed.is_set())
        self.assertFalse(self.app.busy)
        updates.finished.set()
        self.app.tick()
        self.assertFalse(updates.busy)
        self._cancel_timers()
        self.app.close()
        self.assertTrue(self.app.closed)


if __name__ == "__main__":
    unittest.main()
