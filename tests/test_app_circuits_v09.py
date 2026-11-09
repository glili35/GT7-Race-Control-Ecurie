"""Cartes et repères embarqués dans une vraie fenêtre Tk, sans réseau ni PS5.

Les ressources du logiciel sont réelles ; le profil et l'ancien cache de bordures
sont exclusivement temporaires. Ces contrôles ne certifient pas une numérotation
officielle des circuits et ne déduisent aucun virage de leur géométrie.
"""
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


SPA = "Circuit de Spa-Francorchamps"
SUGO = "SUGO International Racing Course"
SARDEGNA = "Sardegna - Road Track - A"
AUTOPOLIS_SHORT = "Autopolis International Racing Course - Short Course"


class AppCircuitsV09Tests(unittest.TestCase):
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
        cls.App, cls.Fleet = RaceControlApp, FleetMonitor

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.profile = Path(self.directory.name) / "profile"
        self.patchers = []
        self.errors = []
        self.boxes = {}
        self.root = self.app = None
        self.addCleanup(self.cleanup)
        self.guard(patch.dict(os.environ, {"GT7RC_DATA_DIR": str(self.profile)}))
        self.guard(patch("racecontrol.app.local_address", return_value="127.0.0.1"))
        self.download = self.guard(patch("racecontrol.circuits.urlopen",
                                        side_effect=AssertionError("Aucun téléchargement attendu pour ces cartes")))
        for method in ("showerror", "showwarning", "showinfo"):
            self.boxes[method] = self.guard(patch("racecontrol.app.messagebox." + method))
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.report_callback_exception = lambda kind, value, trace: self.errors.append(value)
        self.app = self.App(self.root)
        self.app.fleet = self.Fleet(timeout_s=60)
        self.root.geometry("1280x900")
        self.root.deiconify()
        self.wait_for(lambda: self.app.canvas.winfo_ismapped()
                      and self.app.canvas.winfo_width() > 20
                      and self.app.canvas.winfo_height() > 20)

    def guard(self, patcher):
        value = patcher.start()
        self.patchers.append(patcher)
        return value

    def cleanup(self):
        try:
            if self.root is not None:
                for timer in self.root.tk.call("after", "info"):
                    self.root.after_cancel(timer)
                if self.app is not None and not self.app.closed:
                    self.app.close()
                elif self.root.winfo_exists():
                    self.root.destroy()
        except tk.TclError:
            pass
        finally:
            self.app = self.root = None
            for patcher in reversed(self.patchers):
                patcher.stop()
            self.patchers.clear()
            self.boxes.clear()
            self.errors.clear()
            self.directory.cleanup()
            gc.collect()

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            self.assertFalse(self.errors, str(self.errors))
            if predicate():
                return
            time.sleep(.01)
        self.fail("La fenêtre de carte n'a pas atteint son état attendu")

    def render(self):
        self.root.update()
        self.app.draw_track()
        self.app.draw_cars(self.app.fleet.snapshots())
        self.root.update_idletasks()
        self.assertFalse(self.errors, str(self.errors))

    def select(self, name):
        self.app.circuit.set(name)
        self.app.select_circuit()
        self.render()

    def labels(self):
        return sorted(int(self.app.canvas.itemcget(item, "text"))
                      for item in self.app.canvas.find_withtag("corners")
                      if self.app.canvas.type(item) == "text")

    def track_lines(self):
        return [item for item in self.app.canvas.find_withtag("track")
                if self.app.canvas.type(item) == "line"]

    def test_spa_uses_new_numbers_on_an_old_valid_cornerless_cache_without_rewriting_it(self):
        anchors = self.app.library.annotations_virages(SPA).points
        cached = self.app.library.chemin(SPA)
        cached.parent.mkdir(parents=True)
        # Petite géométrie de test, autour des vraies ancres embarquées ; il ne
        # s'agit pas de nouvelles bordures prétendument mesurées dans GT7.
        edges = [{"x": corner.x + offset, "z": corner.z, "side": side}
                 for corner in anchors for offset, side in ((-2., "L"), (2., "R"))]
        cached.write_text(json.dumps({"edges": edges}), encoding="utf-8")
        original = cached.read_bytes()
        self.select(SPA)
        self.assertTrue(self.app.points)
        self.assertFalse(self.app.centerline)
        self.assertEqual(self.labels(), list(range(1, 20)))
        self.assertEqual(len(self.app.corners), 19)
        self.app.library.installer(SPA)
        self.assertEqual(cached.read_bytes(), original)
        self.download.assert_not_called()

    def test_spa_renders_its_embedded_line_and_nineteen_numbers_with_no_downloaded_cache(self):
        self.select(SPA)
        self.assertFalse(self.app.points)
        self.assertTrue(self.app.centerline)
        self.assertEqual(self.labels(), list(range(1, 20)))
        self.assertIn("Carte embarquée", self.app.circuit_status.get())
        self.assertEqual(str(self.app.download_circuit_button.cget("state")), "normal")
        lines = self.track_lines()
        self.assertEqual(len(lines), 1)
        pixels = self.app.canvas.coords(lines[0])
        self.assertEqual(len(pixels), len(self.app.centerline) * 2)
        # Les extrémités correspondent exactement à la source, sans ajout de
        # segment fermant artificiellement un relevé ouvert.
        for raw, pixel in ((self.app.centerline[0], pixels[:2]),
                           (self.app.centerline[-1], pixels[-2:])):
            expected = self.app.transform(*self.app._rotate(*raw))
            self.assertAlmostEqual(pixel[0], expected[0], places=6)
            self.assertAlmostEqual(pixel[1], expected[1], places=6)
        self.assertFalse(self.app.library.cache_dir.exists())
        self.download.assert_not_called()

    def test_rotation_and_mirror_transform_embedded_line_corner_anchor_and_car_together(self):
        self.select(SPA)
        corner = self.app.corners[0]
        observed = time.monotonic()
        self.app.fleet.ingest(1, SimpleNamespace(
            position_x=corner.x, position_z=corner.z, speed_kmh=120.,
            fuel_level=50., fuel_capacity=100., current_lap=2, flags=1,
        ), now=observed)
        # Le passage de quatre cartes en attente à une voiture reçue élargit
        # le canevas. Appliquer ce layout avant les deux captures évite qu'un
        # tick programmé le change au milieu de la comparaison du miroir.
        self.app.tick()
        self.app.rotation.set("90")
        self.app.mirror_horizontal.set(False)
        self.render()
        original_line = self.app.canvas.coords(self.track_lines()[0])
        raw_before = self.app.fleet.snapshots(now=observed + 1)
        width, height = self.app.canvas.winfo_width(), self.app.canvas.winfo_height()
        self.app.mirror_horizontal.set(True)
        self.render()
        self.assertEqual((self.app.canvas.winfo_width(), self.app.canvas.winfo_height()),
                         (width, height), "Comparer le miroir sur un canevas de taille identique")
        mirrored_line = self.app.canvas.coords(self.track_lines()[0])
        self.assertEqual(len(original_line), len(mirrored_line))
        for index in range(0, len(original_line), 2):
            self.assertAlmostEqual(mirrored_line[index], width - original_line[index], places=6)
            self.assertAlmostEqual(mirrored_line[index + 1], original_line[index + 1], places=6)
        anchor = self.app.canvas.coords(f"turn-anchor-{corner.n}")
        car = next(item for item in self.app.canvas.find_withtag("cars")
                   if self.app.canvas.type(item) == "text")
        center = self.app.canvas.coords(car)
        self.assertAlmostEqual(anchor[0], center[0], places=6)
        self.assertAlmostEqual(anchor[1], center[1], places=6)
        self.assertEqual(self.app.fleet.snapshots(now=observed + 1), raw_before)

    def test_configuration_without_a_map_or_annotations_does_not_reuse_spa_numbers(self):
        self.select(SPA)
        self.assertTrue(self.labels())
        self.select(AUTOPOLIS_SHORT)
        self.assertFalse(self.app.points)
        self.assertFalse(self.app.centerline)
        self.assertFalse(self.app.corners)
        self.assertFalse(self.labels())
        self.assertFalse(self.track_lines())
        self.assertIsNone(self.app.transform)
        self.assertIn("Carte non disponible", self.app.circuit_status.get())
        self.assertIn("Virages non documentés", self.app.map_info.get())
        self.assertEqual(str(self.app.download_circuit_button.cget("state")), "disabled")
        self.download.assert_not_called()

    def test_reverse_configuration_keeps_its_line_without_inheriting_forward_numbers(self):
        self.select(SARDEGNA)
        self.assertEqual(self.labels(), list(range(1, 16)))
        self.select(SARDEGNA + " (Reverse)")
        self.assertTrue(self.app.centerline)
        self.assertTrue(self.track_lines())
        self.assertFalse(self.app.corners)
        self.assertFalse(self.labels())
        self.assertIn("Virages non documentés", self.app.map_info.get())
        self.assertEqual(str(self.app.download_circuit_button.cget("state")), "disabled")
        self.download.assert_not_called()

    def test_sugo_keeps_fourteen_documented_anchors_and_displays_unconfirmed_coverage(self):
        self.select(SUGO)
        self.assertEqual(len(self.app.corners), 14)
        self.assertEqual(self.labels(), list(range(1, 15)))
        self.assertIn("14 repères de virage documentés", self.app.map_info.get())
        self.assertIn("couverture à confirmer", self.app.map_info.get())
        self.assertNotIn("officiel", self.app.map_info.get().lower())
        self.download.assert_not_called()

    def test_demo_removes_real_centerline_and_numbers_then_restores_them(self):
        self.select(SPA)
        original_line, original_corners = self.app.centerline, self.app.corners
        self.app.toggle_demo()
        self.render()
        self.assertTrue(self.app.demo)
        self.assertFalse(self.app.centerline)
        self.assertFalse(self.app.corners)
        self.assertFalse(self.labels())
        self.assertTrue(self.app.points)
        self.assertIn("aucune numérotation de circuit réel", self.app.map_info.get())
        self.assertEqual(str(self.app.download_circuit_button.cget("state")), "disabled")
        self.app.toggle_demo()
        self.render()
        self.assertFalse(self.app.demo)
        self.assertEqual(self.app.centerline, original_line)
        self.assertEqual(self.app.corners, original_corners)
        self.assertEqual(self.labels(), list(range(1, 20)))
        self.assertEqual(str(self.app.download_circuit_button.cget("state")), "normal")
        self.assertIsNone(self.app.pilot)
        self.assertIsNone(self.app.hub)
        self.download.assert_not_called()

    def test_hover_identifies_spa_turn_one_and_restores_the_coverage_message(self):
        self.select(SPA)
        footer = self.app.map_info.get()
        x, y = self.app.canvas.coords("turn-label-1")
        self.app.canvas.event_generate("<Motion>", x=0, y=0)
        self.root.update()
        self.app.canvas.event_generate("<Motion>", x=round(x), y=round(y))
        self.wait_for(lambda: "La Source" in self.app.map_info.get())
        self.assertIn("Virage 1", self.app.map_info.get())
        self.app.canvas.event_generate("<Motion>", x=0, y=0)
        self.wait_for(lambda: self.app.map_info.get() == footer)


if __name__ == "__main__":
    unittest.main()
