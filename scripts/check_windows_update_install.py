"""Installe l'artifact local via le vrai lanceur de mises à jour, pour le CI.

Aucun téléchargement : le SHA256 et les métadonnées sont vérifiés sur une copie
écrite puis renommée comme un téléchargement. Seul le Popen final ajoute les
options silencieuses nécessaires au runner ; le lanceur du logiciel reste
interactif. L'installation obtenue est conservée pour son test de démarrage.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
import uuid

# ``python scripts/...`` place scripts/ et non la racine dans sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from racecontrol.update_install import launch_installer
from racecontrol.updates import DEFAULT_REPOSITORY, UpdateClient, UpdateInfo


def check_install(installer: Path, destination: Path, version: str) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Ce contrôle d'installation doit être exécuté sous Windows.")
    if re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version) is None:
        raise ValueError("Version attendue : trois nombres séparés par des points.")
    source = installer.resolve(strict=True)
    destination = destination.absolute()
    name = f"GT7-Race-Control-Ecurie-Setup-{version}-test.exe"
    if source.name != name or not source.is_file():
        raise ValueError("L'artifact local ne porte pas le nom attendu pour cette version.")
    if destination == Path(destination.anchor):
        raise ValueError("Choisir un sous-dossier temporaire pour cette installation de test.")
    size = source.stat().st_size
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    release = f"https://github.com/{DEFAULT_REPOSITORY}/releases"
    info = UpdateInfo(
        version=version, tag="test-v" + version, asset_name=name,
        download_url=f"{release}/download/test-v{version}/{name}",
        size_bytes=size, sha256=digest,
        release_url=f"{release}/tag/test-v{version}",
        release_notes="Artifact local du CI, non téléchargé.",
    )
    real_popen = subprocess.Popen
    with tempfile.TemporaryDirectory(prefix="gt7-update-install-ci-") as directory:
        profile = Path(directory)
        folder = profile / "updates" / uuid.uuid4().hex
        folder.mkdir(parents=True)
        copied = folder / name
        partial = folder / (name + ".part")
        with partial.open("xb") as output:
            # Séparer création et modification reproduit le cas Windows où
            # lstat().ctime (création) diffère de fstat().ctime (ChangeTime).
            time.sleep(.03)
            with source.open("rb") as incoming:
                shutil.copyfileobj(incoming, output, length=64 * 1024)
            output.flush()
            os.fsync(output.fileno())
        partial.replace(copied)
        client = UpdateClient(current_version="0.8.0", data_dir=profile)
        launches = []

        def launch_in_ci(arguments, **options):
            expected = [str(copied), "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"]
            if arguments != expected or any("SILENT" in argument.upper() for argument in arguments[1:]):
                raise RuntimeError("Le lanceur du logiciel doit ouvrir un assistant interactif.")
            if options != {"shell": False, "cwd": str(folder), "close_fds": True}:
                raise RuntimeError("Les options de lancement de l'installateur ont changé.")
            if launches:
                raise RuntimeError("Une seule installation doit être exécutée par ce contrôle.")
            process = real_popen(arguments + [
                "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-",
                "/DIR=" + str(destination),
            ], **options)
            launches.append(process)
            return process

        with patch.object(sys, "frozen", True, create=True), \
                patch("racecontrol.update_install.subprocess.Popen", side_effect=launch_in_ci):
            process = launch_installer(client, info, copied)
        if len(launches) != 1 or process is not launches[0]:
            raise RuntimeError("Le vrai lanceur n'a pas exécuté l'installateur une seule fois.")
        try:
            code = process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            # Inno Setup peut avoir un processus enfant : ne pas laisser cette
            # installation de test continuer après l'échec du contrôle.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, timeout=10)
            process.wait(timeout=10)
            raise RuntimeError("L'installation de test a dépassé son délai de 120 secondes.") from None
        if code != 0:
            raise RuntimeError(f"L'installation de test a échoué (code {code}).")
        if not (destination / "GT7-Race-Control-Ecurie.exe").is_file():
            raise RuntimeError("L'installation n'a pas créé l'exécutable Windows attendu.")
    print(f"Installateur {version} vérifié et exécuté par le vrai lanceur de mises à jour.")
    print(f"Application installée pour son contrôle de démarrage : {destination}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    check_install(args.installer, args.destination, args.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
