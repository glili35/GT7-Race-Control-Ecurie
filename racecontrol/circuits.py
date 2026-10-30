"""Catalogue des circuits GT7, téléchargements volontaires et cache utilisateur."""
import json
import os
import sys
from pathlib import Path
from urllib.request import urlopen

SOURCE = 'https://raw.githubusercontent.com/jbhoorasingh/gt7-datalogger-track-data/main/'
CACHE = Path(os.environ.get('LOCALAPPDATA') or (Path.home() / '.cache')) / 'GT7-Race-Control-Ecurie' / 'circuits'
INDEX = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent)) / 'circuits_gt7' / 'index.json'

class CircuitLibrary:
    def __init__(self):
        with INDEX.open(encoding='utf-8') as stream:
            data = json.load(stream)
        self.entrees = {item['official_name']: item for item in data['configurations']}
        self.noms = sorted(self.entrees)

    def est_disponible(self, nom):
        return bool(self.entrees[nom].get('bundle'))

    def chemin(self, nom):
        entry = self.entrees[nom]
        if not entry.get('bundle'):
            return None
        # Le chemin est issu de l'index embarqué, pas d'une entrée libre.
        file = entry['bundle']['file']
        if not file.startswith('tracks/') or '/' in file[7:] or not file.endswith('.json'):
            raise ValueError('Chemin de circuit non valide')
        return CACHE / file

    def est_installe(self, nom):
        path = self.chemin(nom)
        return bool(path and path.is_file())

    def installer(self, nom):
        path = self.chemin(nom)
        if path is None:
            raise ValueError('Tracé non disponible pour ce circuit')
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        relative = self.entrees[nom]['bundle']['file']
        with urlopen(SOURCE + relative, timeout=30) as response:
            content = response.read(12_000_001)
        if len(content) > 12_000_000:
            raise ValueError('Circuit trop volumineux')
        data = json.loads(content)
        if not isinstance(data.get('edges'), list):
            raise ValueError('Fichier de circuit incorrect')
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(content)
        temporary.replace(path)

    def charger(self, nom):
        path = self.chemin(nom)
        if not path or not path.is_file():
            return [], []
        try:
            with path.open(encoding='utf-8') as stream:
                data = json.load(stream)
            edges = data['edges']
            return ([(float(p['x']), float(p['z'])) for p in edges if p.get('side') == 'L'],
                    [(float(p['x']), float(p['z'])) for p in edges if p.get('side') == 'R'])
        except (OSError, ValueError, KeyError, TypeError):
            return [], []
