"""Carte Tk : mêmes coordonnées pour tracé, voitures et ancres de virage."""
from __future__ import annotations

import gc
import json
import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class FakeMapLibrary:
    """Géométrie volontairement asymétrique pour révéler un miroir manquant."""

    def __init__(self):
        from racecontrol.circuits import CornerPoint

        self.erreur_catalogue = None
        self.noms = ["Circuit asymétrique de test"]
        self.entrees = {self.noms[0]: {"bundle": {"file": "tracks/test.json"}}}
        self.points = [(-100., -50., "L"), (200., -50., "L"),
                       (185., 110., "R"), (-30., 95., "R"),
                       (-70., -20., "L"), (140., 80., "R")]
        self.corners = (CornerPoint(1, "Première courbe", -70., -20.),
                        CornerPoint(2, "Deuxième courbe", 140., 80.))

    def est_disponible(self, name):
        return name in self.entrees

    def charger_points(self, name):
        return list(self.points) if name in self.entrees else []

    def charger_virages(self, name):
        return self.corners if name in self.entrees else ()


class MapDisplayIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
            del probe
            gc.collect()
        except tk.TclError as exc:
            raise unittest.SkipTest("Affichage Tk indisponible : utiliser Xvfb ou un bureau Windows.") from exc
        from racecontrol.app import RaceControlApp
        from racecontrol.fleet import FleetMonitor

        cls.App = RaceControlApp
        cls.Fleet = FleetMonitor

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.profile = Path(self.temp.name)
        self.windows = []
        self.patches = []
        self.callback_errors = []
        self.app = self.root = None
        self.addCleanup(self._cleanup)
        self._patch(patch.dict(os.environ, {"GT7RC_DATA_DIR": str(self.profile)}))
        self._patch(patch("racecontrol.app.local_address", return_value="127.0.0.1"))
        self.library = FakeMapLibrary()
        self._patch(patch("racecontrol.app.CircuitLibrary", return_value=self.library))
        for name in ("showerror", "showinfo", "showwarning"):
            self._patch(patch("racecontrol.app.messagebox." + name))
        self.root, self.app = self._new_window()
        self.app.circuit.set(self.library.noms[0])
        self.app.select_circuit()
        self.app.fleet = self.Fleet(timeout_s=60)
        self.sample_time = time.monotonic()
        for corner in self.library.corners:
            self.app.fleet.ingest(corner.n, SimpleNamespace(
                position_x=corner.x, position_z=corner.z, speed_kmh=100 + corner.n,
                fuel_level=50, fuel_capacity=100, current_lap=1,
            ), now=self.sample_time)
        self._render()

    def _patch(self, guard):
        result = guard.start()
        self.patches.append(guard)
        return result

    def _new_window(self):
        root = tk.Tk()
        root.withdraw()
        self.windows.append((root, None))
        root.report_callback_exception = lambda kind, value, trace: self.callback_errors.append(value)
        app = self.App(root)
        self.windows[-1] = (root, app)
        root.geometry("1280x900")
        root.deiconify()
        root.update()
        return root, app

    @staticmethod
    def _close_window(root, app):
        try:
            for timer in root.tk.call("after", "info"):
                root.after_cancel(timer)
            if app is not None and not app.closed:
                app.close()
            elif root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass

    def _cleanup(self):
        for root, app in reversed(self.windows):
            self._close_window(root, app)
        self.windows.clear()
        self.app = self.root = None
        for guard in reversed(self.patches):
            guard.stop()
        self.patches.clear()
        self.callback_errors.clear()
        self.library = None
        self.temp.cleanup()
        gc.collect()

    def _render(self):
        self.root.update_idletasks()
        self.app.draw_track()
        self.app.draw_cars(self.app.fleet.snapshots())
        self.root.update_idletasks()
        self.assertFalse(self.callback_errors, str(self.callback_errors))
        self.assertIsNotNone(self.app.transform, "Le canevas doit afficher une vraie géométrie de test.")

    def _car_center(self, number):
        canvas = self.app.canvas
        labels = [item for item in canvas.find_withtag("cars")
                  if canvas.type(item) == "text" and canvas.itemcget(item, "text") == str(number)]
        self.assertEqual(len(labels), 1, f"La voiture {number} doit être affichée une seule fois.")
        center = canvas.coords(labels[0])
        circles = [canvas.coords(item) for item in canvas.find_withtag("cars") if canvas.type(item) == "oval"]
        self.assertTrue(any(abs((box[0] + box[2]) / 2 - center[0]) < 1e-6 and
                            abs((box[1] + box[3]) / 2 - center[1]) < 1e-6 for box in circles),
                        "Le numéro de voiture doit rester centré sur son marqueur.")
        return center

    def _track_centers(self):
        canvas = self.app.canvas
        return [((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
                for item in canvas.find_withtag("track") if canvas.type(item) == "oval"
                for box in [canvas.coords(item)]]

    def test_rotations_and_mirror_keep_car_and_corner_anchor_on_same_track_point(self):
        for rotation in ("0", "90", "180", "270"):
            for mirrored in (False, True):
                with self.subTest(rotation=rotation, mirrored=mirrored):
                    self.app.rotation.set(rotation)
                    self.app.mirror_horizontal.set(mirrored)
                    self._render()
                    track = self._track_centers()
                    for corner in self.library.corners:
                        center = self._car_center(corner.n)
                        anchor = self.app.canvas.coords(f"turn-anchor-{corner.n}")
                        self.assertEqual(len(anchor), 4)
                        self.assertAlmostEqual(anchor[0], center[0], places=6)
                        self.assertAlmostEqual(anchor[1], center[1], places=6)
                        self.assertTrue(any(abs(x - center[0]) < 1e-6 and abs(y - center[1]) < 1e-6
                                            for x, y in track))
                        self.assertEqual(self.app.canvas.itemcget(f"turn-label-{corner.n}", "text"), str(corner.n))
                        self.assertNotEqual(anchor[:2], anchor[2:], "Le numéro doit être décalé de son ancre.")

    def test_horizontal_mirror_reverses_screen_x_after_rotation_without_mutating_telemetry(self):
        self.app.rotation.set("90")
        self.app.mirror_horizontal.set(False)
        self._render()
        before = {number: self._car_center(number) for number in (1, 2)}
        track_before = self._track_centers()
        raw_before = self.app.fleet.snapshots(now=self.sample_time + 1)
        width = self.app.canvas.winfo_width()

        self.app.mirror_horizontal.set(True)
        self._render()
        after = {number: self._car_center(number) for number in (1, 2)}
        for number in (1, 2):
            self.assertAlmostEqual(after[number][0], width - before[number][0], places=6)
            self.assertAlmostEqual(after[number][1], before[number][1], places=6)
            self.assertNotAlmostEqual(after[number][0], before[number][0], places=2)
        expected_track = sorted((round(width - x, 6), round(y, 6)) for x, y in track_before)
        actual_track = sorted((round(x, 6), round(y, 6)) for x, y in self._track_centers())
        self.assertEqual(actual_track, expected_track)
        self.assertEqual(self.app.fleet.snapshots(now=self.sample_time + 1), raw_before)

    def test_hiding_corner_numbers_preserves_track_and_cars(self):
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        toggle = next(widget for widget in descendants(self.root)
                      if isinstance(widget, tk.Checkbutton) and widget.cget("text") == "Virages")
        self.assertTrue(self.app.show_corners.get())
        track_before = self._track_centers()
        cars_before = {number: self._car_center(number) for number in (1, 2)}
        self.assertTrue(self.app.canvas.find_withtag("corners"))

        toggle.invoke()
        self._render()
        self.assertFalse(self.app.show_corners.get())
        self.assertFalse(self.app.canvas.find_withtag("corners"))
        self.assertEqual(self._track_centers(), track_before)
        self.assertEqual({number: self._car_center(number) for number in (1, 2)}, cars_before)

        toggle.invoke()
        self._render()
        self.assertTrue(self.app.show_corners.get())
        self.assertTrue(self.app.canvas.find_withtag("turn-label-1"))
        self.assertTrue(self.app.canvas.find_withtag("turn-label-2"))
        self.assertEqual({number: self._car_center(number) for number in (1, 2)}, cars_before)

    def test_mirror_and_corner_preferences_are_saved_and_reloaded(self):
        self.assertTrue(self.app.mirror_horizontal.get())
        self.assertTrue(self.app.show_corners.get())
        self.app.mirror_horizontal.set(False)
        self.app.show_corners.set(False)
        self.app.rotation.set("270")
        self.app.save_settings(quiet=True)
        saved = json.loads(self.app.config_file.read_text(encoding="utf-8"))
        self.assertIs(saved["mirror_horizontal"], False)
        self.assertIs(saved["show_corners"], False)
        self._close_window(self.root, self.app)
        self.root, self.app = self._new_window()
        self.assertFalse(self.app.mirror_horizontal.get())
        self.assertFalse(self.app.show_corners.get())
        self.assertEqual(self.app.rotation.get(), "270")


if __name__ == "__main__":
    unittest.main()
