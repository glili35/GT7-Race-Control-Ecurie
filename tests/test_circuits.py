"""Validation des données circuit et des téléchargements, sans accès Internet."""
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from racecontrol import circuits


class Download(io.BytesIO):
    def __init__(self, payload, url=None, length=None):
        super().__init__(payload)
        self.url = url or circuits.SOURCE + "tracks/test.json"
        self.headers = {} if length is None else {"Content-Length": str(length)}

    def geturl(self):
        return self.url


class CircuitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.index = self.root / "index.json"
        self.write_index()
        self.library = circuits.CircuitLibrary(self.root / "cache", self.index)
        self.payload = json.dumps({"edges": [
            {"x": 12, "z": -4, "side": "L"},
            {"x": 18.5, "z": 5, "side": "R"},
            {"x": 11, "z": 100, "side": "L"},
        ]}).encode()

    def tearDown(self):
        self.temp.cleanup()

    def write_index(self, file="tracks/test.json"):
        self.index.write_text(json.dumps({"configurations": [
            {"official_name": "Test", "bundle": {"file": file}},
            {"official_name": "Sans bordures", "bundle": None},
        ]}), encoding="utf-8")

    def put_cache(self, payload=None):
        path = self.library.chemin("Test")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.payload if payload is None else payload)
        return path

    def test_constructor_is_offline_and_does_not_create_cache(self):
        with patch.object(circuits, "urlopen") as download:
            library = circuits.CircuitLibrary(self.root / "other", self.index)
            self.assertEqual(library.noms, ["Sans bordures", "Test"])
            self.assertFalse((self.root / "other").exists())
            download.assert_not_called()

    def test_missing_index_keeps_application_usable_with_visible_error(self):
        library = circuits.CircuitLibrary(self.root / "cache", self.root / "absent.json")
        self.assertEqual(library.noms, [])
        self.assertIn("Catalogue des circuits indisponible", library.erreur_catalogue)
        self.assertEqual(library.charger_points("Test"), [])
        with self.assertRaisesRegex(circuits.CircuitLibraryError, "Catalogue"):
            library.installer("Test")

    def test_unknown_name_has_clear_error(self):
        for method in (self.library.chemin, self.library.est_disponible, self.library.charger):
            with self.assertRaisesRegex(circuits.CircuitLibraryError, "Circuit inconnu"):
                method("Un circuit absent")

    def test_no_geometry_is_not_an_empty_download(self):
        self.assertIsNone(self.library.chemin("Sans bordures"))
        self.assertFalse(self.library.est_installe("Sans bordures"))
        with patch.object(circuits, "urlopen") as download:
            with self.assertRaisesRegex(circuits.CircuitLibraryError, "non disponible"):
                self.library.installer("Sans bordures")
            download.assert_not_called()

    def test_rejects_traversal_in_catalogue(self):
        for filename in ("tracks/../outside.json", "tracks/..\\outside.json", "tracks/C:evil.json", "tracks/test.json?x=1", "../test.json"):
            with self.subTest(filename=filename):
                self.write_index(filename)
                library = circuits.CircuitLibrary(self.root / "cache", self.index)
                self.assertIn("Chemin", library.erreur_catalogue)
                self.assertEqual(library.noms, [])

    def test_duplicate_or_malformed_catalogue_is_not_partially_loaded(self):
        self.index.write_text(json.dumps({"configurations": [{"official_name": "A"}, {"official_name": "A"}]}))
        library = circuits.CircuitLibrary(self.root / "cache", self.index)
        self.assertEqual(library.noms, [])
        self.assertIn("dupliqué", library.erreur_catalogue)

    def test_points_preserve_sides_without_inventing_order(self):
        self.put_cache()
        self.assertEqual(self.library.charger_points("Test"), [(12.0, -4.0, "L"), (18.5, 5.0, "R"), (11.0, 100.0, "L")])
        self.assertEqual(self.library.charger("Test"), ([(12.0, -4.0), (11.0, 100.0)], [(18.5, 5.0)]))
        self.assertTrue(self.library.est_installe("Test"))

    def test_malformed_cache_is_visible_and_not_considered_installed(self):
        for content in (b"invalid json", b'{"edges": []}', b'{"edges": [{"x": NaN,"z":0,"side":"L"}]}', b'{"edges":[{"x":true,"z":0,"side":"R"}]}', b'{"edges":[{"x":0,"z":0,"side":"unknown"}]}'):
            with self.subTest(content=content):
                self.put_cache(content)
                self.assertEqual(self.library.charger_points("Test"), [])
                self.assertFalse(self.library.est_installe("Test"))
                self.assertIn("incorrect", self.library.erreur_cache("Test"))

    def test_valid_cache_is_not_downloaded_again(self):
        self.put_cache()
        with patch.object(circuits, "urlopen") as download:
            self.library.installer("Test")
            download.assert_not_called()

    def test_download_commits_only_valid_geometry_to_injected_cache(self):
        with patch.object(circuits, "urlopen", return_value=Download(self.payload, length=len(self.payload))) as download:
            self.library.installer("Test")
            download.assert_called_once_with(circuits.SOURCE + "tracks/test.json", timeout=30)
        self.assertEqual(self.library.chemin("Test").read_bytes(), self.payload)
        self.assertEqual(list((self.root / "cache").rglob("*.tmp")), [])

    def test_invalid_download_does_not_replace_cache(self):
        path = self.put_cache(b"old invalid cache")
        with patch.object(circuits, "urlopen", return_value=Download(b'{"edges": [{"x":1,"z":2}]}')):
            with self.assertRaises(circuits.CircuitLibraryError):
                self.library.installer("Test")
        self.assertEqual(path.read_bytes(), b"old invalid cache")
        self.assertEqual(list((self.root / "cache").rglob("*.tmp")), [])

    def test_failed_atomic_write_removes_temporary_file(self):
        with patch.object(circuits, "urlopen", return_value=Download(self.payload)), patch.object(Path, "replace", side_effect=OSError("disque plein")):
            with self.assertRaises(OSError):
                self.library.installer("Test")
        self.assertFalse(self.library.chemin("Test").exists())
        self.assertEqual(list((self.root / "cache").rglob("*.tmp")), [])

    def test_oversized_download_is_rejected_without_cache(self):
        with patch.object(circuits, "urlopen", return_value=Download(self.payload, length=circuits.MAX_DOWNLOAD_BYTES + 1)):
            with self.assertRaisesRegex(circuits.CircuitLibraryError, "volumineux"):
                self.library.installer("Test")
        self.assertFalse((self.root / "cache").exists())
        with patch.object(circuits, "MAX_DOWNLOAD_BYTES", 3), patch.object(circuits, "urlopen", return_value=Download(self.payload)):
            with self.assertRaisesRegex(circuits.CircuitLibraryError, "volumineux"):
                self.library.installer("Test")

    def test_unexpected_redirect_is_rejected(self):
        for url in ("http://raw.githubusercontent.com/test.json", "https://other.example/test.json"):
            with self.subTest(url=url), patch.object(circuits, "urlopen", return_value=Download(self.payload, url=url)):
                with self.assertRaisesRegex(circuits.CircuitLibraryError, "redirigé"):
                    self.library.installer("Test")
        self.assertFalse((self.root / "cache").exists())

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "liens symboliques non disponibles")
    def test_cache_symlink_cannot_escape_chosen_directory(self):
        cache = self.root / "cache"
        cache.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (cache / "tracks").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("création des liens symboliques non autorisée")
        with self.assertRaisesRegex(circuits.CircuitLibraryError, "sort du dossier"):
            self.library.chemin("Test")


    def cache_with_corners(self, corners):
        data = json.loads(self.payload)
        data["corners"] = corners
        return self.put_cache(json.dumps(data).encode())

    def test_corner_anchors_are_read_in_number_order_without_changing_coordinates(self):
        self.cache_with_corners([
            {"n": 3, "name": "Raidillon", "apex": {"x": 83.1, "z": -462.1}},
            {"n": 1, "name": "La Source", "apex": {"x": -494.7, "z": -936.4}},
        ])
        with patch.object(circuits, "urlopen") as download:
            self.assertEqual(self.library.charger_virages("Test"), (
                circuits.CornerPoint(1, "La Source", -494.7, -936.4),
                circuits.CornerPoint(3, "Raidillon", 83.1, -462.1),
            ))
            download.assert_not_called()
        self.assertIsNone(self.library.erreur_cache("Test"))

    def test_old_cache_without_corner_metadata_remains_compatible(self):
        self.put_cache()
        self.assertEqual(self.library.charger_virages("Test"), ())
        self.assertIsNone(self.library.erreur_cache("Test"))
        self.assertTrue(self.library.est_installe("Test"))

    def test_absent_corner_cache_never_triggers_download(self):
        with patch.object(circuits, "urlopen") as download:
            self.assertEqual(self.library.charger_virages("Test"), ())
            self.assertEqual(self.library.charger_virages("Sans bordures"), ())
            download.assert_not_called()
        self.assertFalse((self.root / "cache").exists())

    def test_invalid_optional_corner_data_does_not_disable_valid_borders(self):
        valid = {"n": 1, "name": "Premier virage", "apex": {"x": 10.0, "z": 20.0}}
        malformed = [
            [valid, valid],
            [{**valid, "n": 0}],
            [{**valid, "n": -1}],
            [{**valid, "n": True}],
            [{**valid, "n": "1"}],
            [{**valid, "apex": None}],
            [{**valid, "apex": {"x": float("nan"), "z": 20}}],
            [{**valid, "apex": {"x": 10, "z": float("inf")}}],
            [{**valid, "apex": {"x": True, "z": 20}}],
            [{**valid, "apex": {"x": "10", "z": 20}}],
            [{**valid, "name": 12}],
            {"unexpected": "object"},
        ]
        for corners in malformed:
            with self.subTest(corners=corners):
                path = self.cache_with_corners(corners)
                before = path.read_bytes()
                self.assertEqual(self.library.charger_virages("Test"), ())
                self.assertIn("Annotations des virages", self.library.erreur_cache("Test"))
                self.assertTrue(self.library.charger_points("Test"))
                self.assertTrue(self.library.est_installe("Test"))
                self.assertEqual(path.read_bytes(), before)

    def bulk_library(self, entries=None):
        entries = entries or [
            {"official_name": name, "bundle": {"file": f"tracks/{name.lower()}.json"}}
            for name in ("A", "B", "C")
        ]
        self.index.write_text(json.dumps({"configurations": entries}), encoding="utf-8")
        return circuits.CircuitLibrary(self.root / "cache", self.index)

    def test_bulk_preserves_valid_cache_and_continues_after_one_failure(self):
        library = self.bulk_library()
        cached = library.chemin("A")
        cached.parent.mkdir(parents=True)
        cached.write_bytes(self.payload)
        events = []
        with patch.object(circuits, "urlopen", side_effect=[OSError("source indisponible"), Download(self.payload)]) as download:
            report = library.installer_tous(lambda *event: events.append(event))
        self.assertEqual(report.already_present, ("A",))
        self.assertEqual(report.installed, ("C",))
        self.assertEqual(report.failed, {"B": "source indisponible"})
        self.assertFalse(report.cancelled)
        self.assertEqual(download.call_count, 2)
        self.assertEqual(cached.read_bytes(), self.payload)
        self.assertEqual(events, [(1, 3, "A", "prêt"), (2, 3, "B", "échec"), (3, 3, "C", "installé")])

    def test_bulk_cancellation_is_honoured_between_files(self):
        library = self.bulk_library()
        cancel = threading.Event()
        events = []
        def progress(*event):
            events.append(event)
            cancel.set()
        with patch.object(circuits, "urlopen", side_effect=lambda *args, **kwargs: Download(self.payload)) as download:
            report = library.installer_tous(progress, cancel)
        self.assertEqual(report.installed, ("A",))
        self.assertTrue(report.cancelled)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(len(events), 1)
        self.assertFalse(library.chemin("B").exists())

    def test_bulk_already_cancelled_performs_no_network_or_cache_write(self):
        library = self.bulk_library()
        cancel = threading.Event()
        cancel.set()
        with patch.object(circuits, "urlopen") as download:
            report = library.installer_tous(cancel=cancel)
        self.assertTrue(report.cancelled)
        self.assertFalse(report.installed)
        self.assertFalse((self.root / "cache").exists())
        download.assert_not_called()

    def test_bulk_shared_bundle_downloads_once_for_all_configuration_names(self):
        library = self.bulk_library([
            {"official_name": "A", "bundle": {"file": "tracks/shared.json"}},
            {"official_name": "A Reverse", "bundle": {"file": "tracks/shared.json"}},
            {"official_name": "Sans géométrie", "bundle": None},
        ])
        with patch.object(circuits, "urlopen", side_effect=lambda *args, **kwargs: Download(self.payload)) as download:
            report = library.installer_tous()
            self.assertEqual(report.installed, ("A", "A Reverse"))
            self.assertEqual(download.call_count, 1)
            repeated = library.installer_tous()
            self.assertEqual(repeated.already_present, ("A", "A Reverse"))
            self.assertEqual(download.call_count, 1)
        self.assertFalse(report.failed)
        self.assertFalse(report.cancelled)

    def test_bulk_callback_failure_does_not_interrupt_other_downloads(self):
        library = self.bulk_library()
        def progress(*_):
            raise RuntimeError("la fenêtre a été fermée")
        with patch.object(circuits, "urlopen", side_effect=lambda *args, **kwargs: Download(self.payload)):
            report = library.installer_tous(progress)
        self.assertEqual(report.installed, ("A", "B", "C"))
        self.assertFalse(report.failed)

    def test_bulk_missing_catalogue_is_an_explicit_error(self):
        library = circuits.CircuitLibrary(self.root / "cache", self.root / "absent.json")
        with patch.object(circuits, "urlopen") as download:
            with self.assertRaisesRegex(circuits.CircuitLibraryError, "Catalogue"):
                library.installer_tous()
            download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
