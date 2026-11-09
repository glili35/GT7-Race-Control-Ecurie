"""Vérification du catalogue source et de sa résolution dans un EXE gelé."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from racecontrol import circuits


class TestCatalogueEmbarque(unittest.TestCase):
    def test_index_source(self):
        self.assertTrue(circuits.INDEX.is_file())
        with circuits.INDEX.open(encoding="utf-8") as stream:
            self.assertIn("configurations", json.load(stream))

    def test_catalogue(self):
        library = circuits.CircuitLibrary()
        self.assertIsNone(library.erreur_catalogue)
        self.assertGreater(len(library.noms), 0)

    def test_frozen_resource_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index = root / "circuits_gt7" / "index.json"
            index.parent.mkdir()
            index.write_bytes(circuits.INDEX.read_bytes())
            result = subprocess.run(
                [sys.executable, "-c", (
                    "import sys; from pathlib import Path; "
                    "sys._MEIPASS=sys.argv[1]; "
                    "from racecontrol.circuits import CircuitLibrary, INDEX; "
                    "assert INDEX == Path(sys.argv[1]) / 'circuits_gt7' / 'index.json'; "
                    "library=CircuitLibrary(); "
                    "assert library.erreur_catalogue is None; "
                    "assert len(library.noms)>0"
                ), str(root)],
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
