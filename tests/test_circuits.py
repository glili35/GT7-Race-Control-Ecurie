"""Validation des données circuit et des téléchargements, sans accès Internet."""
import io
import json
import tempfile
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


if __name__ == "__main__":
    unittest.main()
