"""Vérification du catalogue source et de sa résolution dans un EXE gelé."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from racecontrol import circuits

RESOURCES = (
    'index.json', 'corners.json', 'track_paths.json',
    'LICENSE-CC0.txt', 'LICENSE-MIT-reference-laps.txt',
)


class TestCatalogueEmbarque(unittest.TestCase):
    def test_index_source(self):
        self.assertTrue(circuits.INDEX.is_file())
        with circuits.INDEX.open(encoding="utf-8") as stream:
            self.assertIn("configurations", json.load(stream))

    def test_catalogue(self):
        library = circuits.CircuitLibrary()
        self.assertIsNone(library.erreur_catalogue)
        self.assertGreater(len(library.noms), 0)

    def test_source_data_and_license_resources_are_present(self):
        directory = circuits.INDEX.parent
        for filename in RESOURCES:
            with self.subTest(resource=filename):
                self.assertTrue((directory / filename).is_file())
                self.assertGreater((directory / filename).stat().st_size, 0)
        for filename in ('corners.json', 'track_paths.json'):
            with self.subTest(resource=filename):
                self.assertIsInstance(json.loads((directory / filename).read_bytes()), dict)

    def test_frozen_resource_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index = root / "circuits_gt7" / "index.json"
            index.parent.mkdir()
            for filename in RESOURCES:
                shutil.copyfile(circuits.INDEX.parent / filename, index.parent / filename)
            result = subprocess.run(
                [sys.executable, "-c", (
                    "import sys; from pathlib import Path; "
                    "sys._MEIPASS=sys.argv[1]; "
                    "from racecontrol.circuits import CircuitLibrary, INDEX; "
                    "from racecontrol.track_paths import TrackPaths, PATHS; "
                    "assert INDEX == Path(sys.argv[1]) / 'circuits_gt7' / 'index.json'; "
                    "assert PATHS == INDEX.with_name('track_paths.json'); "
                    "cache=Path(sys.argv[1]) / 'cache-absent'; "
                    "library=CircuitLibrary(cache_dir=cache); "
                    "assert library.erreur_catalogue is None; "
                    "assert len(library.noms)>0; "
                    "corners=library.annotations_virages('Circuit de Spa-Francorchamps'); "
                    "assert len(corners.points)==19; "
                    "assert corners.source and corners.revision; "
                    "paths=TrackPaths(); "
                    "assert paths.error is None; "
                    "assert len(paths.load('High Speed Ring'))>10; "
                    "assert not cache.exists()"
                ), str(root)],
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
