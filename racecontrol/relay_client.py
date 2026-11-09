"""Clients du relais optionnel : connexions sortantes et télémétrie chiffrée.

La clé de chaque voiture reste dans son invitation et dans le profil privé de
l'ingénieur. Le relais reçoit uniquement les jetons d'accès et les chiffrés.
Aucun état de connexion ou message de contrôle ne rafraîchit la flotte.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import queue
import socket
import ssl
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
import uuid

from websockets.sync.client import connect
from websockets.exceptions import WebSocketException

from .relay_protocol import (
    RelayProtocolError, RelayInvitation, validate_url, validate_id, validate_secret,
    validate_car, parse_invitation, make_invitation, new_secret, loads_frame,
    dumps_frame, encrypt_sample, decrypt_sample, validate_sample_frame,
)
from .transport import ConnectionCheck, _private_write, _validate_telemetry

OPEN_TIMEOUT_S = 3.0
HELLO_TIMEOUT_S = 3.0
SEND_TIMEOUT_S = 2.0
CHECK_TIMEOUT_S = 6.0
STOP_TIMEOUT_S = 2.0
CHALLENGE_INTERVAL_S = 1.0
CHALLENGE_TTL_S = 2.5
SAMPLE_TTL_S = 1.0
SEND_INTERVAL_S = 0.1
ROTATE_TIMEOUT_S = 5.0
_MAX_PROFILE_BYTES = 16_384
_CHECK_SLOTS = threading.BoundedSemaphore(2)
_LOGGER = logging.Logger('gt7-relay-private', level=logging.CRITICAL + 1)
_LOGGER.addHandler(logging.NullHandler())
_LOGGER.propagate = False


class RelayClientError(ValueError):
    """Erreur publique exploitable sans exposer les secrets de connexion."""


class _Stopped(Exception):
    pass


class _RemoteError(RelayClientError):
    def __init__(self, code):
        self.code = code
        super().__init__('La connexion au relais a été refusée.')


def _abort(connection):
    if connection is None:
        return
    sock = getattr(connection, 'socket', None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


def _schema(frame, operation, fields):
    if (not isinstance(frame, dict) or set(frame) != {'v', 'op', *fields}
            or type(frame.get('v')) is not int or frame['v'] != 1 or frame.get('op') != operation):
        raise RelayProtocolError('Réponse du relais invalide.')
    return frame


def _frame(value):
    frame = loads_frame(value)
    if frame.get('op') == 'error':
        _schema(frame, 'error', {'code', 'message'})
        code = frame.get('code')
        if not isinstance(code, str) or code not in {'unauthorized', 'room_unknown', 'slot_busy', 'capacity', 'invalid_message'}:
            raise RelayProtocolError('Erreur du relais invalide.')
        # Ne jamais réafficher le texte arbitraire envoyé par le serveur.
        raise _RemoteError(code)
    return frame


class _Channel:
    """Un writer borné par connexion ; une écriture bloquée est interrompue."""
    def __init__(self, connection, stop):
        self.connection = connection
        self.stop = stop
        self.closed = threading.Event()
        self.items = queue.Queue(maxsize=2)
        self.writer = threading.Thread(target=self._write, name='gt7-relay-writer', daemon=True)
        self.writer.start()

    def _write(self):
        while not self.closed.is_set() and not self.stop.is_set():
            try:
                text, done, error = self.items.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if self.closed.is_set() or self.stop.is_set():
                    raise _Stopped()
                self.connection.send(text)
            except Exception:
                error.append(True)
            finally:
                done.set()

    def send(self, frame):
        if self.closed.is_set() or self.stop.is_set():
            raise _Stopped()
        done, errors = threading.Event(), []
        try:
            self.items.put_nowait((dumps_frame(frame), done, errors))
        except queue.Full:
            raise RelayClientError('La liaison au relais est saturée.') from None
        deadline = time.monotonic() + SEND_TIMEOUT_S
        while not done.wait(timeout=0.05):
            if self.stop.is_set() or self.closed.is_set():
                _abort(self.connection)
                raise _Stopped()
            if time.monotonic() >= deadline:
                self.closed.set()
                _abort(self.connection)
                raise RelayClientError('Le relais ne reçoit plus les données.')
        if errors:
            raise RelayClientError('Écriture vers le relais interrompue.')

    def receive(self, timeout=0.1):
        if self.stop.is_set() or self.closed.is_set():
            raise _Stopped()
        return _frame(self.connection.recv(timeout=timeout))

    def close(self):
        self.closed.set()
        _abort(self.connection)
        if self.writer is not threading.current_thread():
            self.writer.join(timeout=0.5)
        try:
            self.connection.close()
        except Exception:
            pass


def _open(url, stop):
    validate_url(url)
    if stop.is_set():
        raise _Stopped()
    local = urlsplit(url).scheme == 'ws'
    options = dict(compression=None, open_timeout=OPEN_TIMEOUT_S, close_timeout=0.5,
                   max_size=32_768, max_queue=8, ping_interval=10, ping_timeout=10,
                   logger=_LOGGER, proxy=None if local else True)
    if not local:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        options['ssl'] = context
    connection = connect(url, **options)
    if stop.is_set():
        _abort(connection)
        try:
            connection.close()
        except Exception:
            pass
        raise _Stopped()
    return _Channel(connection, stop)


def _pilot_join(channel, invitation):
    channel.send({'v': 1, 'op': 'join', 'room': invitation.room, 'role': 'pilot',
                  'car': invitation.car, 'token': invitation.token})
    frame = channel.receive(HELLO_TIMEOUT_S)
    _schema(frame, 'joined', {'room', 'role', 'car'})
    if (validate_id(frame['room']) != invitation.room or frame['role'] != 'pilot'
            or validate_car(frame['car']) != invitation.car):
        raise RelayProtocolError('Le relais a rejoint une autre voiture.')


class _Lifecycle:
    def _initialize(self, on_status):
        self._on_status = on_status
        self._last_status = None
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._terminated = threading.Event()
        self._worker = None
        self._channel = None

    def _status(self, text):
        if self._last_status != text:
            self._last_status = text
            if self._on_status is not None:
                try:
                    self._on_status(text)
                except Exception:
                    pass

    def start(self):
        with self._lifecycle_lock:
            # Un ancien DNS système peut dépasser le délai socket. Ne jamais
            # effacer son événement d'arrêt tant que le worker existe encore.
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop.clear()
            self._terminated.clear()
            self._worker = threading.Thread(target=self._run, name=self._thread_name, daemon=True)
            self._worker.start()

    def stop(self):
        with self._lifecycle_lock:
            self._stop.set()
            worker, channel = self._worker, self._channel
        if channel is not None:
            _abort(channel.connection)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=STOP_TIMEOUT_S)
        with self._lifecycle_lock:
            if self._worker is worker and (worker is None or not worker.is_alive()):
                self._worker = None


class RelayPilotLink(_Lifecycle):
    """Publication des données réelles uniquement, au plus dix fois par seconde."""
    def __init__(self, invitation: str, on_status=None):
        self._invite = parse_invitation(invitation)
        self.car_number = self._invite.car
        self._initialize(on_status)
        self._pending = None
        self._sequence = 0
        self._session = uuid.uuid4().hex
        self._thread_name = f'gt7-relay-pilot-{self.car_number}'

    def publish(self, telemetry):
        if self._stop.is_set():
            return
        try:
            fields = ('speed_kmh', 'rpm', 'current_gear', 'throttle_percent', 'brake_percent',
                      'fuel_level', 'fuel_capacity', 'current_lap', 'total_laps', 'best_lap_ms',
                      'last_lap_ms', 'race_position', 'race_position_total', 'position_x',
                      'position_y', 'position_z', 'flags', 'car_id', 'tyre_temperatures')
            values = {name: value for name in fields if (value := getattr(telemetry, name, None)) is not None}
            values = _validate_telemetry(values)
        except (ValueError, TypeError, AttributeError):
            self._status('Mesure PS5 invalide : non transmise au relais.')
            return
        with self._lock:
            if self._stop.is_set():
                return
            self._sequence += 1
            self._pending = (self._sequence, time.monotonic(), SimpleNamespace(**values))

    def _connected_loop(self, channel):
        _pilot_join(channel, self._invite)
        with self._lock:
            self._pending = None  # Attendre une nouvelle capture après reconnexion.
        self._status('Relais connecté : en attente des données PS5 et de l’ingénieur.')
        challenge, received_challenge = None, 0.
        next_send = 0.
        while not self._stop.is_set():
            try:
                frame = channel.receive(timeout=0.05)
            except TimeoutError:
                frame = None
            now = time.monotonic()
            if frame is not None:
                _schema(frame, 'challenge', {'challenge'})
                challenge = validate_id(frame['challenge'])
                received_challenge = now
            if challenge is None or now - received_challenge > CHALLENGE_TTL_S or now < next_send:
                continue
            with self._lock:
                sample, self._pending = self._pending, None
            if sample is None:
                continue
            sequence, produced, telemetry = sample
            age_ms = (now - produced) * 1000
            if age_ms < 0 or age_ms > SAMPLE_TTL_S * 1000:
                continue
            frame = encrypt_sample(self._invite.room, self.car_number, self._invite.key,
                                   self._session, sequence, challenge, telemetry, age_ms)
            if self._stop.is_set() or time.monotonic() - produced > SAMPLE_TTL_S:
                continue
            channel.send(frame)
            next_send = time.monotonic() + SEND_INTERVAL_S
            self._status('Télémétrie chiffrée transmise au relais.')

    def _run(self):
        backoff = 0.25
        # Nouveau sous-contexte cryptographique pour chaque nouveau worker.
        with self._lock:
            self._session = uuid.uuid4().hex
            self._sequence = 0
            self._pending = None
        try:
            while not self._stop.is_set():
                channel = None
                try:
                    channel = _open(self._invite.url, self._stop)
                    self._channel = channel
                    self._connected_loop(channel)
                    backoff = 0.25
                except _Stopped:
                    break
                except _RemoteError as exc:
                    if exc.code in {'unauthorized', 'room_unknown'}:
                        self._status('Invitation relais refusée. Demande une nouvelle invitation à l’ingénieur.')
                        break
                    self._status('Emplacement relais indisponible. Nouvelle tentative automatique.')
                except (RelayProtocolError, RelayClientError, OSError, WebSocketException, TimeoutError):
                    if not self._stop.is_set():
                        self._status('Relais injoignable : reconnexion automatique.')
                finally:
                    self._channel = None
                    if channel is not None:
                        channel.close()
                    with self._lock:
                        self._pending = None
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 5.)
        finally:
            self._terminated.set()
            if self._stop.is_set():
                self._status('Transmission relais arrêtée.')

    def stop(self):
        super().stop()
        with self._lock:
            self._pending = None


@dataclass
class _Rotation:
    car: int
    done: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)
    answer: str | None = None
    error: str | None = None


class RelayEngineerSession(_Lifecycle):
    """Réception déchiffrée des quatre emplacements et profil ingénieur privé."""
    def __init__(self, url, bootstrap, data_dir, fleet, on_status=None):
        self.url = validate_url(url)
        self._bootstrap = validate_secret(bootstrap) if bootstrap else None
        self.data_dir = Path(data_dir)
        self.fleet = fleet
        self._initialize(on_status)
        self._ready = threading.Event()
        self._profile = None
        self._profile_path = self.data_dir / ('relay-profile-' + hashlib.sha256(self.url.encode()).hexdigest() + '.json')
        self._thread_name = 'gt7-relay-engineer'
        self._rotations = queue.Queue(maxsize=4)
        self._pending_rotation = None
        self._invalid_slots = set()
        self._challenges = OrderedDict()
        self._seen = {number: OrderedDict() for number in range(1, 5)}
        self._observed = {}
        self._active_sessions = {}
        self._retired_sessions = {number: OrderedDict() for number in range(1, 5)}

    @property
    def connected(self):
        return self._ready.is_set() and not self._stop.is_set()

    def wait_ready(self, timeout=6):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout):
            raise ValueError('Délai de connexion invalide.')
        deadline = time.monotonic() + max(0., min(float(timeout), 120.))
        while not self.connected:
            if self._stop.is_set() or self._terminated.is_set() or time.monotonic() >= deadline:
                return False
            self._ready.wait(timeout=min(0.05, max(0., deadline - time.monotonic())))
        return True

    def _profile_valid(self, profile):
        required = {'v', 'url', 'room', 'owner', 'writers', 'keys'}
        if (not isinstance(profile, dict) or not required.issubset(profile)
                or set(profile) - required - {'quarantined'}
                or type(profile['v']) is not int or profile['v'] != 1 or profile['url'] != self.url):
            raise RelayClientError('Le profil privé du relais est incorrect.')
        validate_id(profile['room'])
        validate_secret(profile['owner'])
        for kind in ('writers', 'keys'):
            if not isinstance(profile[kind], dict) or set(profile[kind]) != {'1', '2', '3', '4'}:
                raise RelayClientError('Le profil privé du relais est incomplet.')
            for value in profile[kind].values():
                validate_secret(value)
        quarantined = profile.get('quarantined', [])
        if (not isinstance(quarantined, list) or len(quarantined) > 4
                or any(type(car) is not int or not 1 <= car <= 4 for car in quarantined)
                or len(set(quarantined)) != len(quarantined)):
            raise RelayClientError('Le profil privé du relais est incorrect.')
        return profile

    def _load_profile(self):
        if self._profile is not None:
            return
        try:
            if self._profile_path.is_symlink():
                raise RelayClientError('Le profil privé du relais ne doit pas être un lien.')
            if not self._profile_path.exists():
                return
            with self._profile_path.open('rb') as stream:
                content = stream.read(_MAX_PROFILE_BYTES + 1)
            if len(content) > _MAX_PROFILE_BYTES:
                raise RelayClientError('Le profil privé du relais est trop volumineux.')
            profile = self._profile_valid(json.loads(content))
            if os.name != 'nt':
                os.chmod(self._profile_path, 0o600)
            with self._lock:
                self._profile = profile
                self._invalid_slots.update(profile.get('quarantined', []))
        except (OSError, ValueError, TypeError, UnicodeError):
            raise RelayClientError('Impossible de lire le profil privé du relais. Conserve une sauvegarde avant de le réinitialiser.') from None

    def _save_profile(self, profile):
        self._profile_valid(profile)
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.data_dir.is_symlink() or self._profile_path.is_symlink():
                raise OSError()
            if os.name != 'nt':
                os.chmod(self.data_dir, 0o700)
            _private_write(self._profile_path, json.dumps(profile, separators=(',', ':')).encode())
        except OSError:
            raise RelayClientError('Impossible d’enregistrer les accès privés du relais.') from None

    def _join(self, channel):
        with self._lock:
            profile = self._profile
        if profile is None:
            if self._bootstrap is None:
                raise RelayClientError('Un code d’accès au relais est nécessaire pour créer une salle.')
            channel.send({'v': 1, 'op': 'create', 'bootstrap': self._bootstrap})
            frame = channel.receive(HELLO_TIMEOUT_S)
            _schema(frame, 'created', {'room', 'owner', 'writers'})
            profile = {'v': 1, 'url': self.url, 'room': frame['room'], 'owner': frame['owner'],
                       'writers': frame['writers'], 'keys': {str(n): new_secret() for n in range(1, 5)},
                       'quarantined': []}
            self._profile_valid(profile)
            # Garder en mémoire la salle créée si l'écriture échoue : ne pas
            # créer automatiquement d'autres salles lors d'une reconnexion.
            with self._lock:
                self._profile = profile
            self._save_profile(profile)
        else:
            channel.send({'v': 1, 'op': 'join', 'room': profile['room'], 'role': 'engineer', 'token': profile['owner']})
            frame = channel.receive(HELLO_TIMEOUT_S)
            _schema(frame, 'joined', {'room', 'role'})
            if frame['room'] != profile['room'] or frame['role'] != 'engineer':
                raise RelayProtocolError('Le relais a rejoint une autre salle.')
        self._challenges.clear()
        self._observed.clear()
        self._seen = {number: OrderedDict() for number in range(1, 5)}
        self._active_sessions.clear()
        self._retired_sessions = {number: OrderedDict() for number in range(1, 5)}
        self._ready.set()
        self._status('Poste ingénieur connecté au relais.')

    def invitation(self, car):
        car = validate_car(car)
        with self._lock:
            profile = self._profile
            if not self.connected or profile is None:
                raise RelayClientError('Le poste ingénieur doit être connecté au relais.')
            if car in self._invalid_slots:
                raise RelayClientError('Renouvelle l’accès de cette voiture avant de copier son invitation.')
            return make_invitation(self.url, profile['room'], car, profile['writers'][str(car)], profile['keys'][str(car)])

    def rotate_token(self, car):
        request = _Rotation(validate_car(car))
        if not self.connected:
            raise RelayClientError('Le poste ingénieur doit être connecté au relais.')
        try:
            self._rotations.put_nowait(request)
        except queue.Full:
            raise RelayClientError('Un renouvellement d’accès est déjà en cours.') from None
        deadline = time.monotonic() + ROTATE_TIMEOUT_S + 1
        while not request.done.wait(timeout=0.05):
            if self._stop.is_set() or time.monotonic() >= deadline:
                request.cancelled.set()
                with self._lock:
                    self._invalid_slots.add(request.car)
                reset = getattr(self.fleet, 'reset', None)
                if reset is not None:
                    reset(request.car)
                channel = self._channel
                if channel is not None:
                    _abort(channel.connection)
                raise RelayClientError('Le renouvellement n’a pas pu être confirmé. Réessaie une fois reconnecté.')
        if request.error is not None:
            raise RelayClientError(request.error)
        return request.answer

    def _fail_rotations(self):
        with self._lock:
            pending, self._pending_rotation = self._pending_rotation, None
        if pending is not None:
            request, _ = pending
            with self._lock:
                self._invalid_slots.add(request.car)
            request.error = 'Le renouvellement n’a pas été confirmé. Renouvelle de nouveau cet accès.'
            request.done.set()
        while True:
            try:
                request = self._rotations.get_nowait()
            except queue.Empty:
                break
            request.error = 'Le relais est déconnecté. Réessaie une fois reconnecté.'
            request.done.set()

    def _receive_sample(self, frame, now):
        validate_sample_frame(frame, with_car=True)
        car, challenge = frame['car'], frame['challenge']
        with self._lock:
            if car in self._invalid_slots:
                return
        issued = self._challenges.get(challenge)
        if issued is None or not 0 <= now - issued <= CHALLENGE_TTL_S:
            return
        previous = self._observed.get(car)
        if previous is not None and issued < previous[0]:
            return  # Aucun retour vers un challenge plus ancien.
        seen = self._seen[car]
        if frame['sequence'] <= seen.get(frame['session'], 0):
            return
        with self._lock:
            profile = self._profile
            key = profile['keys'][str(car)]
            room = profile['room']
        telemetry, age_ms = decrypt_sample(room, car, key, frame)
        if self._stop.is_set() or time.monotonic() - issued > CHALLENGE_TTL_S:
            return
        retired = self._retired_sessions[car]
        for session, expires in list(retired.items()):
            if expires < now:
                del retired[session]
        session = frame['session']
        active = self._active_sessions.get(car)
        if session != active:
            if session in retired or (active is not None and len(retired) >= 16):
                return
            if active is not None:
                retired[active] = now + CHALLENGE_TTL_S
            self._active_sessions[car] = session
        observed = issued - age_ms / 1000
        if previous is not None and issued == previous[0]:
            observed = max(previous[1], observed)
        # Égalité permise : nouveaux chiffres sans avancer artificiellement la
        # fraîcheur sur l'heure de réception ou sur un simple message contrôle.
        self.fleet.ingest(car, telemetry, now=observed)
        self._observed[car] = (issued, observed)
        if frame['session'] not in seen and len(seen) >= 16:
            seen.popitem(last=False)
        seen[frame['session']] = frame['sequence']

    def _rotate_response(self, frame):
        _schema(frame, 'rotated', {'car', 'token'})
        car, token = validate_car(frame['car']), validate_secret(frame['token'])
        with self._lock:
            if self._pending_rotation is None or self._pending_rotation[0].car != car:
                if self._stop.is_set():
                    return
                raise RelayProtocolError('Réponse de renouvellement inattendue.')
            request, _ = self._pending_rotation
            previous = self._profile
            profile = {**previous, 'writers': dict(previous['writers']), 'keys': dict(previous['keys'])}
            profile['writers'][str(car)] = token
            profile['keys'][str(car)] = new_secret()
            profile['quarantined'] = [number for number in self._invalid_slots if number != car]
            self._profile = profile
        self._save_profile(profile)
        with self._lock:
            self._invalid_slots.discard(car)
        self._seen[car].clear()
        self._observed.pop(car, None)
        self._active_sessions.pop(car, None)
        self._retired_sessions[car].clear()
        reset = getattr(self.fleet, 'reset', None)
        if reset is not None:
            reset(car)
        self._pending_rotation = None
        request.answer = make_invitation(self.url, profile['room'], car, profile['writers'][str(car)], profile['keys'][str(car)])
        request.done.set()

    def _connected_loop(self, channel):
        self._join(channel)
        next_challenge = 0.
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_challenge:
                challenge = uuid.uuid4().hex
                self._challenges[challenge] = now
                while len(self._challenges) > 3:
                    self._challenges.popitem(last=False)
                channel.send({'v': 1, 'op': 'challenge', 'challenge': challenge})
                next_challenge = time.monotonic() + CHALLENGE_INTERVAL_S
            if self._pending_rotation is not None:
                if now >= self._pending_rotation[1]:
                    raise RelayClientError('Le renouvellement de l’accès a expiré.')
            else:
                try:
                    request = self._rotations.get_nowait()
                except queue.Empty:
                    request = None
                if request is not None:
                    if request.cancelled.is_set():
                        request.error = 'Renouvellement annulé.'
                        request.done.set()
                    else:
                        self._pending_rotation = (request, now + ROTATE_TIMEOUT_S)
                        # Persister l'état incertain AVANT d'envoyer la rotation.
                        # Après crash ou réponse perdue, aucune ancienne clé ne
                        # doit réalimenter cette voiture avant confirmation.
                        with self._lock:
                            self._invalid_slots.add(request.car)
                            profile = {**self._profile, 'quarantined': sorted(self._invalid_slots)}
                            self._profile = profile
                        reset = getattr(self.fleet, 'reset', None)
                        if reset is not None:
                            reset(request.car)
                        self._save_profile(profile)
                        channel.send({'v': 1, 'op': 'rotate', 'car': request.car})
            try:
                frame = channel.receive(timeout=0.1)
            except TimeoutError:
                continue
            if frame.get('op') == 'sample':
                try:
                    self._receive_sample(frame, time.monotonic())
                except RelayProtocolError:
                    # Une voiture ne doit pas interrompre les trois autres.
                    self._status('Une mesure relais invalide a été ignorée.')
            elif frame.get('op') == 'rotated':
                self._rotate_response(frame)
            else:
                raise RelayProtocolError('Message du relais inattendu.')

    def _run(self):
        backoff = 0.25
        try:
            self._load_profile()
            while not self._stop.is_set():
                channel = None
                try:
                    channel = _open(self.url, self._stop)
                    self._channel = channel
                    self._connected_loop(channel)
                    backoff = 0.25
                except _Stopped:
                    break
                except _RemoteError as exc:
                    if exc.code in {'unauthorized', 'room_unknown'}:
                        self._status('Accès ingénieur refusé. Vérifie la configuration privée du relais.')
                        break
                    self._status('Salle relais indisponible. Nouvelle tentative automatique.')
                except RelayClientError:
                    if self._profile is None or not self._profile_path.exists():
                        self._status('Impossible de préparer les accès privés du relais.')
                        break
                    self._status('Relais déconnecté : reconnexion automatique du poste ingénieur.')
                except (RelayProtocolError, OSError, WebSocketException, TimeoutError):
                    if not self._stop.is_set():
                        self._status('Relais injoignable : reconnexion automatique du poste ingénieur.')
                finally:
                    self._ready.clear()
                    self._channel = None
                    self._challenges.clear()
                    self._fail_rotations()
                    if channel is not None:
                        channel.close()
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 5.)
        except (RelayClientError, RelayProtocolError):
            self._status('Le profil privé du relais est indisponible. Vérifie sa configuration.')
        finally:
            self._ready.clear()
            self._fail_rotations()
            self._terminated.set()
            if self._stop.is_set():
                self._status('Réception relais arrêtée.')

    def stop(self):
        self._ready.clear()
        super().stop()
        self._fail_rotations()


def check_relay_invitation(invitation: str) -> ConnectionCheck:
    """Hello pilote authentifié puis fermeture, sans données ni clé chiffrante."""
    try:
        invite = parse_invitation(invitation)
    except RelayProtocolError:
        return ConnectionCheck(False, 'invalid_invitation', 'Invitation relais invalide. Copie une nouvelle invitation.')
    parts = urlsplit(invite.url)
    public = dict(host=parts.hostname, port=parts.port or (443 if parts.scheme == 'wss' else 80), car_number=invite.car)
    def result(ok, code, message, latency=None):
        return ConnectionCheck(ok, code, message, latency_ms=latency, **public)
    if not _CHECK_SLOTS.acquire(blocking=False):
        return result(False, 'unreachable', 'Des tests relais sont déjà en cours. Réessaie dans quelques instants.')
    stop, done = threading.Event(), threading.Event()
    active, outcome = [], []
    started = time.monotonic()
    def worker():
        channel = None
        try:
            channel = _open(invite.url, stop)
            active.append(channel)
            _pilot_join(channel, invite)
            outcome.append(result(True, 'ok', 'Accès au relais vérifié. Aucune donnée PS5 n’a été envoyée.',
                                  (time.monotonic() - started) * 1000))
        except _RemoteError as exc:
            if exc.code == 'slot_busy':
                outcome.append(result(False, 'slot_busy', 'Cette voiture utilise déjà le relais. Sa connexion est conservée.'))
            elif exc.code in {'unauthorized', 'room_unknown'}:
                outcome.append(result(False, 'unauthorized', 'Invitation relais refusée. Demande une nouvelle invitation.'))
            else:
                outcome.append(result(False, 'unreachable', 'Le relais ne peut pas accepter cette connexion.'))
        except ssl.SSLCertVerificationError:
            outcome.append(result(False, 'certificate_mismatch', 'Le certificat du relais n’a pas pu être vérifié.'))
        except RelayProtocolError:
            outcome.append(result(False, 'incompatible_server', 'Réponse du relais incompatible. Vérifie sa version.'))
        except (OSError, WebSocketException, TimeoutError, RelayClientError, _Stopped):
            outcome.append(result(False, 'unreachable', 'Relais injoignable. Vérifie son adresse et sa disponibilité.'))
        finally:
            if channel is not None:
                channel.close()
            _CHECK_SLOTS.release()
            done.set()
    try:
        threading.Thread(target=worker, name='gt7-relay-check', daemon=True).start()
    except Exception:
        _CHECK_SLOTS.release()
        return result(False, 'unreachable', 'Impossible de démarrer la vérification du relais.')
    if not done.wait(timeout=CHECK_TIMEOUT_S):
        stop.set()
        if active:
            _abort(active[0].connection)
        return result(False, 'unreachable', 'Le test du relais a expiré. Vérifie sa connexion réseau.')
    return outcome[0] if outcome else result(False, 'unreachable', 'Vérification du relais interrompue.')
