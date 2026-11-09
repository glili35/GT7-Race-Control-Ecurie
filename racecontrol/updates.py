"""Mises à jour du canal Test depuis les publications publiques GitHub.

Ce module vérifie et télécharge un installateur ; il ne l'exécute jamais.
L'empreinte SHA256 est celle publiée par l'API GitHub pour l'asset exact.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import queue
import re
import stat
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

DEFAULT_REPOSITORY = 'glili35/GT7-Race-Control-Ecurie'
MAX_ASSET_BYTES = 150 * 1024 * 1024
MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_RELEASES = 20
MAX_NOTES_CHARS = 8000
CHUNK_BYTES = 64 * 1024
REQUEST_TIMEOUT_S = 10.0
DOWNLOAD_BUDGET_S = 180.0
CHECK_BUDGET_S = 20.0
_NETWORK_SLOTS = threading.BoundedSemaphore(4)
_VERSION = re.compile(r'(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\Z', re.ASCII)
_DIGEST = re.compile(r'sha256:([0-9a-fA-F]{64})\Z', re.ASCII)
_CDN_HOSTS = {'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}


class UpdateError(ValueError):
    """Mise à jour refusée ou téléchargement indisponible."""


class UpdateCancelled(UpdateError):
    """Téléchargement annulé volontairement, aucun installateur conservé."""


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    tag: str
    asset_name: str
    download_url: str
    size_bytes: int
    sha256: str
    release_url: str
    release_notes: str


def _version(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise UpdateError('Version incorrecte : trois nombres attendus, par exemple 0.7.0.')
    return tuple(int(part) for part in value.split('.'))


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Clé JSON dupliquée')
        result[key] = value
    return result


def _reject_json_constant(_):
    raise ValueError('Constante JSON invalide')


def _url(value: str):
    if (not isinstance(value, str) or len(value) > 8192
            or any(ord(character) < 33 or ord(character) == 127 for character in value)
            or '\\' in value):
        raise UpdateError('Adresse de publication ou de téléchargement non autorisée.')
    try:
        parts = urlsplit(value)
        if (parts.scheme != 'https' or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443) or parts.fragment):
            raise ValueError()
    except ValueError:
        raise UpdateError('Seules les adresses HTTPS GitHub sans identifiants sont autorisées.') from None
    return parts


def _no_aliases(path: Path) -> None:
    """Refuse liens et jonctions sur tous les composants existants du chemin."""
    for item in (*reversed(path.parents), path):
        try:
            metadata = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or (hasattr(item, 'is_junction') and item.is_junction()):
            raise UpdateError('Le dossier de mises à jour ne doit contenir aucun lien ou jonction.')


def _regular(metadata) -> bool:
    return stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1


class _RedirectPolicy(HTTPRedirectHandler):
    def __init__(self, validate: Callable[[str], None]):
        self.validate = validate

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        self.validate(target)
        count = getattr(req, '_gt7_redirects', 0) + 1
        if count > 5:
            raise UpdateError('Trop de redirections pendant le téléchargement GitHub.')
        redirected = super().redirect_request(req, fp, code, msg, headers, target)
        if redirected is not None:
            redirected._gt7_redirects = count
        return redirected


def _active(deadline: float, cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise UpdateCancelled('Téléchargement annulé.')
    if time.monotonic() >= deadline:
        raise UpdateError('Le délai total de téléchargement a expiré. Réessaie plus tard.')


def _chunks(url: str, *, validate: Callable[[str], None], limit: int,
            deadline: float, cancel: threading.Event | None = None,
            expected_bytes: int | None = None, user_agent: str) -> Iterator[bytes]:
    """I/O isolée et bornée, y compris si le DNS système reste bloqué.

    Seul l'appelant écrit les fichiers. Un worker réseau retardé ne peut donc
    rien écrire ni renommer après une annulation ou l'expiration du délai.
    """
    validate(url)
    _active(deadline, cancel)
    if not _NETWORK_SLOTS.acquire(blocking=False):
        raise UpdateError('Des téléchargements sont déjà en cours. Réessaie dans quelques instants.')
    messages = queue.Queue(maxsize=2)
    stopped = threading.Event()

    def emit(kind, value):
        while not stopped.is_set():
            try:
                messages.put((kind, value), timeout=0.05)
                return
            except queue.Full:
                pass

    def worker():
        response = None
        try:
            request = Request(url, headers={'Accept': 'application/vnd.github+json' if urlsplit(url).hostname == 'api.github.com' else 'application/octet-stream',
                                            'Accept-Encoding': 'identity', 'User-Agent': user_agent,
                                            'X-GitHub-Api-Version': '2022-11-28'})
            opener = build_opener(_RedirectPolicy(validate))
            response = opener.open(request, timeout=REQUEST_TIMEOUT_S)
            validate(response.geturl())
            if response.status != 200:
                raise UpdateError('GitHub n’a pas fourni le fichier attendu.')
            encoding = response.headers.get('Content-Encoding', 'identity').lower()
            if encoding not in ('identity', ''):
                raise UpdateError('Le serveur a fourni un encodage de fichier non pris en charge.')
            length = response.headers.get('Content-Length')
            if length is not None:
                if not re.fullmatch(r'[0-9]{1,12}', length) or int(length) > limit:
                    raise UpdateError('La taille annoncée du fichier est incorrecte ou trop grande.')
                if expected_bytes is not None and int(length) != expected_bytes:
                    raise UpdateError('La taille annoncée ne correspond pas à celle de la publication GitHub.')
            emit('headers', None)
            received = 0
            reader = getattr(response, 'read1', response.read)
            while not stopped.is_set():
                data = reader(CHUNK_BYTES)
                if not data:
                    emit('done', None)
                    break
                received += len(data)
                if received > limit:
                    raise UpdateError('Le fichier dépasse la taille autorisée.')
                emit('chunk', data)
        except HTTPError as exc:
            emit('error', UpdateError('GitHub limite temporairement les demandes. Réessaie plus tard.'
                                     if exc.code in (403, 429) else
                                     'La publication GitHub est inaccessible ou son fichier n’est plus disponible.'))
        except UpdateError as exc:
            emit('error', exc)
        except (OSError, URLError, ValueError, TypeError, http.client.HTTPException):
            emit('error', UpdateError('Connexion à GitHub interrompue. Vérifie ta connexion et réessaie.'))
        finally:
            try:
                if response is not None:
                    response.close()
            except Exception:
                pass
            finally:
                _NETWORK_SLOTS.release()

    try:
        threading.Thread(target=worker, name='gt7-update-network', daemon=True).start()
    except Exception:
        _NETWORK_SLOTS.release()
        raise UpdateError('Impossible de démarrer la connexion à GitHub.') from None
    last_event = time.monotonic()
    try:
        while True:
            _active(deadline, cancel)
            if time.monotonic() - last_event >= REQUEST_TIMEOUT_S:
                raise UpdateError('GitHub ne répond pas dans le délai prévu. Réessaie plus tard.')
            try:
                kind, value = messages.get(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            last_event = time.monotonic()
            if kind == 'chunk':
                yield value
            elif kind == 'done':
                return
            elif kind == 'error':
                raise value
    finally:
        stopped.set()


class UpdateClient:
    def __init__(self, repository: str = DEFAULT_REPOSITORY, current_version: str = '0.7.0', *, data_dir: Path):
        if (not isinstance(repository, str) or len(repository) > 200
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', repository)
                or any(part in ('.', '..') for part in repository.split('/'))):
            raise UpdateError('Nom de dépôt GitHub incorrect.')
        self.repository = repository
        self.current_version = current_version
        self._current = _version(current_version)
        self.data_dir = Path(data_dir).absolute()
        self._api_url = f'https://api.github.com/repos/{repository}/releases?per_page={MAX_RELEASES}'
        self._user_agent = f'GT7-Race-Control-Ecurie/{current_version}'

    def _api_address(self, url):
        parts = _url(url)
        expected = urlsplit(self._api_url)
        if parts.hostname != 'api.github.com' or parts.path != expected.path or parts.query != expected.query:
            raise UpdateError('La liste des publications doit provenir de l’API officielle GitHub du projet.')

    def _asset_address(self, url, info: UpdateInfo, *, redirect=False):
        parts = _url(url)
        expected_path = f'/{self.repository}/releases/download/{info.tag}/{info.asset_name}'
        if parts.hostname == 'github.com' and parts.path == expected_path and not parts.query:
            return
        if (redirect and parts.hostname in _CDN_HOSTS
                and parts.path.startswith('/github-production-release-asset')
                and '..' not in parts.path.split('/')):
            return
        raise UpdateError('L’installateur doit provenir des publications officielles de ce dépôt GitHub.')

    def _validate_info(self, info: UpdateInfo) -> None:
        if not isinstance(info, UpdateInfo) or _version(info.version) <= self._current:
            raise UpdateError('Cette mise à jour n’est pas plus récente que la version installée.')
        if info.tag != f'test-v{info.version}' or info.asset_name != f'GT7-Race-Control-Ecurie-Setup-{info.version}-test.exe':
            raise UpdateError('Le nom de la publication ou de son installateur est incorrect.')
        if type(info.size_bytes) is not int or not 0 < info.size_bytes <= MAX_ASSET_BYTES:
            raise UpdateError('La taille de l’installateur est incorrecte ou dépasse 150 Mio.')
        if not isinstance(info.sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', info.sha256):
            raise UpdateError('La publication ne fournit pas une empreinte SHA256 officielle valide.')
        if not isinstance(info.release_notes, str) or len(info.release_notes) > MAX_NOTES_CHARS:
            raise UpdateError('Les notes de publication sont incorrectes.')
        release = _url(info.release_url)
        if (release.hostname != 'github.com' or release.path != f'/{self.repository}/releases/tag/{info.tag}'
                or release.query):
            raise UpdateError('Adresse de publication GitHub incorrecte.')
        self._asset_address(info.download_url, info)

    def check(self) -> UpdateInfo | None:
        """Retourne la dernière version Test strictement supérieure, ou None."""
        content = b''.join(_chunks(self._api_url, validate=self._api_address, limit=MAX_FEED_BYTES,
                                  deadline=time.monotonic() + CHECK_BUDGET_S, user_agent=self._user_agent))
        try:
            releases = json.loads(content, object_pairs_hook=_json_object, parse_constant=_reject_json_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise UpdateError('La liste des publications GitHub est illisible.') from None
        if not isinstance(releases, list) or len(releases) > MAX_RELEASES:
            raise UpdateError('La liste des publications GitHub possède un format incorrect.')
        candidates = []
        for release in releases:
            if not isinstance(release, dict) or release.get('draft') is not False:
                continue
            tag = release.get('tag_name')
            if not isinstance(tag, str) or not tag.startswith('test-v'):
                continue
            try:
                version = _version(tag[6:])
            except UpdateError:
                continue
            if version > self._current:
                candidates.append((version, release))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            raise UpdateError('Plusieurs publications décrivent la même version Test.')
        numbers, release = candidates[0]
        version = '.'.join(str(number) for number in numbers)
        asset_name = f'GT7-Race-Control-Ecurie-Setup-{version}-test.exe'
        assets = release.get('assets')
        if not isinstance(assets, list) or len(assets) > 100:
            raise UpdateError('La liste des fichiers de la publication GitHub est incorrecte.')
        installers = [asset for asset in assets if isinstance(asset, dict) and asset.get('name') == asset_name]
        if len(installers) != 1:
            raise UpdateError('L’installateur Windows attendu est absent ou ambigu dans cette publication Test.')
        asset = installers[0]
        digest = asset.get('digest')
        match = _DIGEST.fullmatch(digest) if isinstance(digest, str) else None
        if match is None:
            raise UpdateError('GitHub ne fournit pas encore l’empreinte SHA256 de cet installateur. Réessaie plus tard.')
        notes = release.get('body')
        if notes is None:
            notes = ''
        if not isinstance(notes, str):
            raise UpdateError('Les notes de publication GitHub sont incorrectes.')
        info = UpdateInfo(version, release['tag_name'], asset_name, asset.get('browser_download_url'),
                          asset.get('size'), match.group(1).lower(), release.get('html_url'), notes[:MAX_NOTES_CHARS])
        self._validate_info(info)
        return info

    def _download_path(self, info: UpdateInfo, path: Path) -> Path:
        self._validate_info(info)
        path = Path(path).absolute()
        root = self.data_dir / 'updates'
        _no_aliases(path)
        if (path.name != info.asset_name or path.parent.parent != root
                or not re.fullmatch(r'[0-9a-f]{32}', path.parent.name)):
            raise UpdateError('Le fichier ne se trouve pas dans le dossier privé des mises à jour.')
        return path

    def verify_download(self, info: UpdateInfo, path: Path) -> bool:
        """Revérifie provenance, chemin local, taille et SHA256 avant utilisation."""
        fd = None
        try:
            path = self._download_path(info, path)
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            before = os.fstat(fd)
            if not _regular(before) or before.st_size != info.size_bytes:
                return False
            digest = hashlib.sha256()
            received = 0
            with os.fdopen(fd, 'rb') as stream:
                fd = None
                while data := stream.read(min(CHUNK_BYTES, info.size_bytes - received + 1)):
                    received += len(data)
                    if received > info.size_bytes:
                        return False
                    digest.update(data)
                after = os.fstat(stream.fileno())
            _no_aliases(path)
            current = path.lstat()
            identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
            return (identity(before) == identity(after) == identity(current) and _regular(current)
                    and hmac.compare_digest(digest.hexdigest(), info.sha256))
        except (OSError, UpdateError, ValueError, TypeError):
            return False
        finally:
            if fd is not None:
                os.close(fd)

    def download(self, info: UpdateInfo, on_progress: Callable[[int, int], None] | None = None,
                 cancel: threading.Event | None = None) -> Path:
        self._validate_info(info)
        deadline = time.monotonic() + DOWNLOAD_BUDGET_S
        _active(deadline, cancel)
        root = self.data_dir / 'updates'
        folder = root / uuid.uuid4().hex
        temporary, destination = folder / (info.asset_name + '.part'), folder / info.asset_name
        directory_fd = None
        success = False
        folder_created = False
        try:
            _no_aliases(root)
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            _no_aliases(root)
            folder.mkdir(mode=0o700)
            folder_created = True
            _no_aliases(folder)
            if os.name != 'nt':
                directory_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
            fd = (os.open(temporary.name, flags, 0o600, dir_fd=directory_fd) if directory_fd is not None
                  else os.open(temporary, flags, 0o600))
            received, digest = 0, hashlib.sha256()
            with os.fdopen(fd, 'wb') as stream:
                if on_progress:
                    on_progress(0, info.size_bytes)
                chunks = _chunks(info.download_url, validate=lambda url: self._asset_address(url, info, redirect=True),
                                 limit=info.size_bytes, expected_bytes=info.size_bytes, deadline=deadline,
                                 cancel=cancel, user_agent=self._user_agent)
                try:
                    for data in chunks:
                        _active(deadline, cancel)
                        stream.write(data)
                        received += len(data)
                        digest.update(data)
                        if on_progress:
                            on_progress(received, info.size_bytes)
                finally:
                    chunks.close()
                _active(deadline, cancel)
                if received != info.size_bytes:
                    raise UpdateError('Le téléchargement est incomplet ; aucun installateur n’a été conservé.')
                if not hmac.compare_digest(digest.hexdigest(), info.sha256):
                    raise UpdateError('L’empreinte SHA256 ne correspond pas. L’installateur a été supprimé.')
                stream.flush()
                os.fsync(stream.fileno())
                written = os.fstat(stream.fileno())
            _active(deadline, cancel)
            _no_aliases(folder)
            current = temporary.lstat()
            if not _regular(current) or (current.st_dev, current.st_ino) != (written.st_dev, written.st_ino):
                raise UpdateError('Le fichier téléchargé a été remplacé pendant la vérification.')
            if directory_fd is not None:
                os.replace(temporary.name, destination.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            else:
                os.replace(temporary, destination)
            if not self.verify_download(info, destination):
                raise UpdateError('La vérification finale de l’installateur a échoué.')
            _active(deadline, cancel)
            success = True
            return destination
        except UpdateError:
            raise
        except OSError:
            raise UpdateError('Impossible d’enregistrer la mise à jour. Vérifie l’espace disque et les permissions.') from None
        except Exception:
            raise UpdateError('Le téléchargement de la mise à jour a été interrompu.') from None
        finally:
            if not success and folder_created:
                for name in (temporary.name, destination.name):
                    try:
                        if directory_fd is not None:
                            os.unlink(name, dir_fd=directory_fd)
                        else:
                            (folder / name).unlink(missing_ok=True)
                    except OSError:
                        pass
                try:
                    folder.rmdir()
                except OSError:
                    pass
            if directory_fd is not None:
                os.close(directory_fd)
