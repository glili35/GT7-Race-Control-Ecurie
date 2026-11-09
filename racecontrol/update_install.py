"""Lancement volontaire d'un installateur Windows téléchargé et vérifié.

Ce module n'effectue ni recherche de version ni téléchargement et ne ferme pas
l'application. L'appelant doit demander ce lancement depuis son bouton Installer.
La vérification du contenu est refaite juste avant Popen. Un descripteur garde
le fichier ouvert pendant cette étape et son identité est contrôlée ; une
substitution locale malveillante entre la dernière vérification et CreateProcess
ne peut toutefois pas être éliminée par une API Windows qui lance un chemin.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .updates import UpdateClient, UpdateInfo


class InstallerError(RuntimeError):
    """L'installation ne peut pas être lancée dans les conditions demandées."""


class InstallerFileError(InstallerError):
    """Le fichier doit être téléchargé à nouveau avant toute installation."""


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _check_path_components(path: Path) -> None:
    """Refuse les liens et jonctions avant de traverser chaque composant."""
    current = Path(path.anchor)
    components = path.parts[1:] if path.anchor else path.parts
    for index, part in enumerate(components):
        current = current / part
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise InstallerFileError("Le chemin de l'installateur contient un lien ou une redirection de dossier.")
        if index < len(components) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise InstallerFileError("Le dossier de l'installateur est invalide.")


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns)


def _unchanged(before: os.stat_result, after: os.stat_result) -> bool:
    # Comparer ctime uniquement entre deux appels de la même API. Sous CPython
    # Windows 3.12, lstat utilise la date de création et fstat le ChangeTime :
    # https://github.com/python/cpython/issues/157671
    return (_identity(before) == _identity(after)
            and before.st_ctime_ns == after.st_ctime_ns)


def _installer_path(client: UpdateClient, info: UpdateInfo, path: Path) -> Path:
    try:
        supplied = Path(path)
        directory = Path(client.data_dir)
        if ".." in supplied.parts or ".." in directory.parts:
            raise InstallerFileError("Le chemin de l'installateur est invalide.")
        installer = supplied.absolute()
        updates = (directory / "updates").absolute()
        expected = f"GT7-Race-Control-Ecurie-Setup-{info.version}-test.exe"
        if not re.fullmatch(r"GT7-Race-Control-Ecurie-Setup-[0-9]+\.[0-9]+\.[0-9]+-test\.exe", expected):
            raise InstallerFileError("Le nom de l'installateur est invalide.")
        if (info.asset_name != expected or installer.name != expected
                or installer.parent.parent != updates
                or re.fullmatch(r"[0-9a-f]{32}", installer.parent.name) is None):
            raise InstallerFileError("Utilise l'installateur téléchargé par ce logiciel.")
        _check_path_components(installer)
        metadata = installer.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise InstallerFileError("L'installateur doit être un fichier normal, sans lien.")
        return installer
    except InstallerError:
        raise
    except (OSError, TypeError, ValueError, AttributeError) as exc:
        raise InstallerFileError("Le fichier de mise à jour n'est plus disponible. Télécharge-le à nouveau.") from exc


def launch_installer(client: UpdateClient, info: UpdateInfo, path: Path) -> subprocess.Popen:
    """Vérifie puis ouvre l'assistant interactif Windows, sans fermer l'app.

    Aucun argument silencieux n'est utilisé. Une exception laisse à l'appelant
    la possibilité de conserver son interface et sa session en cours.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        raise InstallerError("L'installation est disponible dans le logiciel Windows installé.")
    installer = _installer_path(client, info, path)
    descriptor = None
    try:
        before = installer.lstat()
        descriptor = os.open(installer, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
        original = os.fstat(descriptor)
        if (not stat.S_ISREG(original.st_mode) or original.st_nlink != 1
                or _identity(before) != _identity(original)):
            raise InstallerFileError("L'installateur n'est plus un fichier normal ou a été remplacé.")
        try:
            verified = client.verify_download(info, installer)
        except Exception as exc:
            raise InstallerFileError("La vérification de l'installateur a échoué. Télécharge-le à nouveau.") from exc
        if verified is not True:
            raise InstallerFileError("Le contenu de l'installateur a changé. Télécharge-le à nouveau.")
        # Recontrôler également les parents : le vérificateur peut avoir ouvert
        # un autre fichier si un dossier a été remplacé entre les deux lectures.
        _check_path_components(installer)
        current = installer.lstat()
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or opened.st_nlink != 1
                or not _unchanged(original, opened) or not _unchanged(before, current)
                or _identity(original) != _identity(current)):
            raise InstallerFileError("L'installateur a changé pendant sa vérification. Télécharge-le à nouveau.")
        try:
            return subprocess.Popen(
                [str(installer), "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"],
                shell=False,
                cwd=str(installer.parent),
                close_fds=True,
            )
        except OSError as exc:
            raise InstallerError("Impossible de démarrer l'installateur Windows. Le logiciel reste ouvert.") from exc
    except InstallerError:
        raise
    except OSError as exc:
        raise InstallerFileError("Le fichier de mise à jour n'est plus disponible. Télécharge-le à nouveau.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
