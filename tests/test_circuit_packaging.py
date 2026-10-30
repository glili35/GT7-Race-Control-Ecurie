"""Vérification du catalogue en mode source et mode PyInstaller simulé."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from racecontrol import circuits


class TestCatalogueEmbarque(unittest.TestCase):
    def test_index_source(self):
        self.assertTrue(circuits.INDEX.is_file())
        with circuits.INDEX.open(encoding="utf-8") as f:
            self.assertIn("configurations", json.load(f))

    def test_catalogue(self):
        library = circuits.CircuitLibrary()
        self.assertGreater(len(library.noms), 0)


if __name__ == "__main__":
    unittest.main()
