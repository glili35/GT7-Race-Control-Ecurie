"""Liaison pilote → ingénieur par HTTPS, avec invitations propres à chaque voiture.

Le certificat de l'ingénieur est épinglé dans l'invitation. Son empreinte est
vérifiée avant tout envoi du secret. Aucun service Internet tiers n'est utilisé :
l'adresse de l'ingénieur doit être joignable depuis le réseau du pilote.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import http.server
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import threading
import time
from types import SimpleNamespace
from typing import Callable
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

PROTOCOL_VERSION = 1
MAX_BODY = 16_384
SAMPLE_TTL_S = 1.0
SEND_INTERVAL_S = 0.1
NETWORK_TIMEOUT_S = 1.5
CHECK_TIMEOUT_S = 4.0
_CHECK_SLOTS = threading.BoundedSemaphore(2)
_FIELDS = {
    'speed_kmh': (-18, 720, False), 'rpm': (0, 30_000, False),
    'current_gear': (0, 15, True), 'throttle_percent': (0, 100, False),
    'brake_percent': (0, 100, False), 'fuel_level': (0, 100_000, False),
    'fuel_capacity': (0, 100_000, False), 'current_lap': (0, 32_767, True),
    'total_laps': (0, 32_767, True), 'best_lap_ms': (0, 0xFFFFFFFF, True),
    'last_lap_ms': (0, 0xFFFFFFFF, True), 'race_position': (-32_768, 32_767, True),
    'race_position_total': (-32_768, 32_767, True),
    'position_x': (-10_000_000, 10_000_000, False),
    'position_y': (-10_000_000, 10_000_000, False),
    'position_z': (-10_000_000, 10_000_000, False),
    'flags': (0, 65_535, True), 'car_id': (0, 0xFFFFFFFF, True),
}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Clé JSON dupliquée')
        result[key] = value
    return result


def _load_json(content: bytes | str):
    def reject(_):
        raise ValueError('Nombre JSON non fini')
    return json.loads(content, object_pairs_hook=_object, parse_constant=reject)


def _car(number):
    if type(number) is not int or not 1 <= number <= 4:
        raise ValueError('Numéro de voiture attendu : 1 à 4')
    return number


def _host(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 253:
        raise ValueError('Adresse ingénieur invalide')
    if value != value.strip() or any(ch in value for ch in '/\\@?#% \t\r\n\x00'):
        raise ValueError('Adresse ingénieur invalide')
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', value):
            raise ValueError('Adresse ingénieur invalide') from None
        if any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-')
               for label in value.split('.')):
            raise ValueError('Adresse ingénieur invalide')
    return value


def _parse_invitation(invitation):
    if not isinstance(invitation, str):
        raise ValueError('Invitation ingénieur invalide')
    invitation = invitation.strip()
    if not invitation.startswith('gt7rc://'):
        raise ValueError('Invitation attendue : gt7rc://…')
    encoded = invitation[8:]
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,2000}={0,2}', encoded):
        raise ValueError('Invitation ingénieur invalide')
    try:
        data = _load_json(base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True))
        if not isinstance(data, dict) or set(data) != {'v', 'host', 'port', 'car', 'token', 'fingerprint'}:
            raise ValueError()
        if type(data['v']) is not int or data['v'] != PROTOCOL_VERSION:
            raise ValueError('Version de liaison incompatible')
        _host(data['host'])
        if type(data['port']) is not int or not 1 <= data['port'] <= 65535:
            raise ValueError()
        _car(data['car'])
        if not isinstance(data['token'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', data['token']):
            raise ValueError()
        if not isinstance(data['fingerprint'], str) or not re.fullmatch(r'[0-9a-f]{64}', data['fingerprint']):
            raise ValueError()
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ValueError('Invitation ingénieur invalide ou incompatible') from None
    return data


def invitation_details(invitation: str) -> tuple[str, int, int]:
    """Informations publiques pour l'interface, sans révéler le secret."""
    data = _parse_invitation(invitation)
    return data['host'], data['port'], data['car']


@dataclass(frozen=True)
class ConnectionCheck:
    """Résultat public d'un test de liaison, sans invitation ni jeton secret."""
    ok: bool
    code: str
    message: str
    host: str | None = None
    port: int | None = None
    car_number: int | None = None
    latency_ms: float | None = None


def _validate_telemetry(data):
    if not isinstance(data, dict) or not data or len(data) > len(_FIELDS) + 1:
        raise ValueError('Télémétrie invalide')
    if 'speed_kmh' not in data or set(data) - (set(_FIELDS) | {'tyre_temperatures'}):
        raise ValueError('Champs de télémétrie invalides')
    result = {}
    for name, value in data.items():
        if name == 'tyre_temperatures':
            if not isinstance(value, (list, tuple)) or len(value) != 4:
                raise ValueError('Quatre températures de pneus attendues')
            if any(type(v) not in (int, float) or not -100 <= v <= 300 or not math.isfinite(v) for v in value):
                raise ValueError('Température de pneu invalide')
            result[name] = tuple(value)
            continue
        lower, upper, integer = _FIELDS[name]
        if type(value) not in (int, float) or (integer and type(value) is not int):
            raise ValueError('Type de mesure invalide')
        if not lower <= value <= upper or not math.isfinite(value):
            raise ValueError('Mesure hors limites')
        result[name] = value
    return result


def _private_write(path: Path, content: bytes):
    temporary = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _identity(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cert_file, key_file = data_dir / 'engineer-cert.pem', data_dir / 'engineer-key.pem'
    token_file = data_dir / 'engineer-tokens.json'
    if cert_file.exists() != key_file.exists():
        raise ValueError('Identité réseau incomplète : certificat ou clé manquant')
    if not cert_file.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'GT7 Race Control Écurie')])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        _private_write(key_file, key.private_bytes(serialization.Encoding.PEM,
                       serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        _private_write(cert_file, cert.public_bytes(serialization.Encoding.PEM))
    try:
        cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
        fingerprint = cert.fingerprint(hashes.SHA256()).hex()
        if token_file.exists():
            tokens = _load_json(token_file.read_bytes())
            if not isinstance(tokens, dict) or set(tokens) != {'1', '2', '3', '4'}:
                raise ValueError()
            if any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', v) for v in tokens.values()):
                raise ValueError()
        else:
            tokens = {str(n): secrets.token_urlsafe(32) for n in range(1, 5)}
            _private_write(token_file, json.dumps(tokens).encode('utf-8'))
    except (OSError, ValueError, TypeError):
        raise ValueError('Identité réseau illisible ; conserve une sauvegarde avant de la réinitialiser') from None
    if os.name != 'nt':
        os.chmod(data_dir, 0o700)
        for path in (cert_file, key_file, token_file):
            os.chmod(path, 0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file)
    return context, tokens, fingerprint


class _BoundedServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 12
    allow_reuse_address = True

    def __init__(self, address, handler, context):
        self.context = context
        self._slots = threading.BoundedSemaphore(12)
        self._connections = set()
        self._connections_lock = threading.Lock()
        super().__init__(address, handler)

    def get_request(self):
        connection, address = self.socket.accept()
        connection.settimeout(NETWORK_TIMEOUT_S)
        try:
            wrapped = self.context.wrap_socket(connection, server_side=True, do_handshake_on_connect=False)
            with self._connections_lock:
                self._connections.add(wrapped)
            return wrapped, address
        except Exception:
            connection.close()
            raise

    def process_request(self, request, address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            request.do_handshake()
            super().process_request_thread(request, address)
        except (OSError, ssl.SSLError):
            self.shutdown_request(request)
        finally:
            self._slots.release()

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            with self._connections_lock:
                self._connections.discard(request)

    def server_close(self):
        super().server_close()
        with self._connections_lock:
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def handle_error(self, request, client_address):
        # Ne jamais imprimer des objets de requête contenant une invitation.
        pass


class EngineerHub:
    """Récepteur HTTPS embarqué ; chaque invitation ne donne accès qu'à une voiture."""
    def __init__(self, callback: Callable[[int, SimpleNamespace], None], data_dir: Path,
                 host: str = '0.0.0.0', port: int = 8765):
        if not callable(callback):
            raise TypeError('Callback de télémétrie attendu')
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError('Port ingénieur invalide')
        self._callback, self._data_dir = callback, Path(data_dir)
        self._host, self.port = host, port
        self._context, self._tokens, self.fingerprint = _identity(self._data_dir)
        self._server = None
        self._thread = None
        self._running = False
        self._lock = threading.RLock()
        self._seen = {n: {} for n in range(1, 5)}

    def start(self):
        if self._server is not None:
            return
        hub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            server_version = 'GT7RC'
            sys_version = ''

            def log_message(self, *args):
                pass

            def _reply(self, status):
                self.send_response(status)
                self.send_header('Content-Length', '0')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True

            def do_POST(self):
                match = re.fullmatch(r'/v1/(telemetry|check)/([1-4])', self.path)
                if match is None:
                    self._reply(404)
                    return
                operation, number = match.group(1), int(match.group(2))
                expected = 'Bearer ' + hub._tokens[str(number)]
                supplied = self.headers.get('Authorization', '')
                if len(supplied) != len(expected) or not hmac.compare_digest(supplied, expected):
                    self._reply(401)
                    return
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) != 1 or self.headers.get('Transfer-Encoding'):
                    self._reply(400)
                    return
                try:
                    length = int(lengths[0])
                except (ValueError, TypeError):
                    self._reply(400)
                    return
                if operation == 'check':
                    # Un test ne transporte aucune mesure et ne modifie ni la
                    # fraîcheur de la flotte, ni les séquences de télémétrie.
                    if lengths[0] != '0':
                        self._reply(400)
                        return
                    with hub._lock:
                        if not hub._running:
                            status = 503
                        elif not hmac.compare_digest(supplied, 'Bearer ' + hub._tokens[str(number)]):
                            status = 401
                        else:
                            status = 204
                    self._reply(status)
                    return
                if not 0 < length <= MAX_BODY:
                    self._reply(413)
                    return
                if self.headers.get_content_type() != 'application/json':
                    self._reply(415)
                    return
                try:
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError('Corps incomplet')
                    data = _load_json(raw)
                    if not isinstance(data, dict) or set(data) != {'v', 'session', 'sequence', 'age_ms', 'telemetry'}:
                        raise ValueError('Enveloppe invalide')
                    if type(data['v']) is not int or data['v'] != PROTOCOL_VERSION:
                        raise ValueError('Version incompatible')
                    session, sequence, age = data['session'], data['sequence'], data['age_ms']
                    if not isinstance(session, str) or not re.fullmatch(r'[a-f0-9]{32}', session):
                        raise ValueError('Session invalide')
                    if type(sequence) is not int or not 1 <= sequence <= 2**63 - 1:
                        raise ValueError('Séquence invalide')
                    if type(age) not in (int, float) or not 0 <= age <= SAMPLE_TTL_S * 1000 or not math.isfinite(age):
                        raise ValueError('Mesure périmée')
                    telemetry = SimpleNamespace(**_validate_telemetry(data['telemetry']))
                except (ValueError, TypeError, KeyError, UnicodeError, OSError, RecursionError, OverflowError):
                    self._reply(400)
                    return
                with hub._lock:
                    if not hub._running:
                        self._reply(503)
                        return
                    # Une invitation peut avoir été révoquée pendant la lecture.
                    if not hmac.compare_digest(supplied, 'Bearer ' + hub._tokens[str(number)]):
                        self._reply(401)
                        return
                    seen = hub._seen[number]
                    if sequence <= seen.get(session, 0):
                        self._reply(409)
                        return
                    try:
                        hub._callback(number, telemetry)
                    except Exception:
                        self._reply(503)
                        return
                    if session not in seen and len(seen) >= 16:
                        del seen[next(iter(seen))]
                    seen[session] = sequence
                self._reply(204)

        self._server = _BoundedServer((self._host, self.port), Handler, self._context)
        self.port = self._server.server_address[1]
        with self._lock:
            self._running = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={'poll_interval': 0.1}, name='gt7-engineer', daemon=True)
        self._thread.start()

    def stop(self):
        with self._lock:
            self._running = False
            server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def invitation(self, car_number: int, advertised_host: str) -> str:
        number, host = _car(car_number), _host(advertised_host)
        if self._server is None:
            raise ValueError("Démarre d'abord le poste ingénieur")
        data = {'v': PROTOCOL_VERSION, 'host': host, 'port': self.port, 'car': number,
                'token': self._tokens[str(number)], 'fingerprint': self.fingerprint}
        encoded = base64.urlsafe_b64encode(json.dumps(data, separators=(',', ':')).encode()).decode().rstrip('=')
        return 'gt7rc://' + encoded

    def rotate_token(self, car_number: int):
        number = _car(car_number)
        with self._lock:
            tokens = dict(self._tokens)
            tokens[str(number)] = secrets.token_urlsafe(32)
            _private_write(self._data_dir / 'engineer-tokens.json', json.dumps(tokens).encode())
            self._tokens = tokens
            self._seen[number].clear()


class _PinError(OSError):
    pass


def _pinned_connection(address):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # Autorité privée : la confiance est l'empreinte fournie hors bande dans
    # l'invitation, pas un certificat public. Vérification AVANT le secret.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(address['host'], address['port'],
                                           timeout=NETWORK_TIMEOUT_S, context=context)
    try:
        connection.connect()
        cert = connection.sock.getpeercert(binary_form=True)
        actual = hashlib.sha256(cert).hexdigest()
        if not hmac.compare_digest(actual, address['fingerprint']):
            raise _PinError('Certificat ingénieur différent')
        # Une fermeture concurrente ne doit jamais déclencher la reconnexion
        # automatique de http.client sans nouvelle vérification d'empreinte.
        connection.auto_open = 0
    except Exception:
        connection.close()
        raise
    return connection


def check_invitation(invitation: str) -> ConnectionCheck:
    """Teste TLS et l'autorisation d'une invitation, sans simuler une voiture.

    Cet appel synchrone doit être exécuté hors du thread graphique. Son délai
    total est borné, y compris si le résolveur DNS système reste bloqué. Deux
    workers maximum sont conservés ; un test expiré n'envoie plus de secret
    après une résolution tardive. Aucun état de pilote n'est créé.
    """
    try:
        address = _parse_invitation(invitation)
    except ValueError:
        return ConnectionCheck(False, 'invalid_invitation',
                               'Invitation invalide ou incompatible. Copie une nouvelle invitation depuis le poste ingénieur.')
    public = {'host': address['host'], 'port': address['port'], 'car_number': address['car']}

    def result(ok, code, message, latency_ms=None):
        return ConnectionCheck(ok, code, message, latency_ms=latency_ms, **public)

    if not _CHECK_SLOTS.acquire(blocking=False):
        return result(False, 'unreachable',
                      'Des tests de connexion sont déjà en cours. Réessaie dans quelques instants.')
    done, cancelled = threading.Event(), threading.Event()
    outcome = []
    active = []
    active_lock = threading.Lock()
    started = time.monotonic()

    def worker():
        connection = None
        try:
            connection = _pinned_connection(address)
            with active_lock:
                if cancelled.is_set():
                    return
                active.append(connection)
            # Vérifier une dernière fois avant toute donnée d'authentification.
            if cancelled.is_set():
                return
            connection.request('POST', f'/v1/check/{address["car"]}', body=b'',
                               headers={'Content-Length': '0',
                                        'Authorization': 'Bearer ' + address['token']})
            response = connection.getresponse()
            status = response.status
            if status == 204:
                elapsed = (time.monotonic() - started) * 1000
                outcome.append(result(True, 'ok',
                                      'Connexion au poste ingénieur vérifiée. Aucune donnée PS5 n’a été envoyée.', elapsed))
            elif status in (401, 403):
                outcome.append(result(False, 'unauthorized',
                                      'Invitation refusée. Demande une nouvelle invitation au poste ingénieur.'))
            elif status in (408, 429, 500, 502, 503, 504):
                outcome.append(result(False, 'unreachable',
                                      'Le poste ingénieur est momentanément indisponible. Vérifie qu’il est démarré, puis réessaie.'))
            else:
                message = ('Le poste ingénieur ne prend pas en charge ce test. Mets à jour son logiciel, puis réessaie.'
                           if status in (404, 405, 501) else
                           'Réponse du poste ingénieur incompatible. Vérifie sa version et mets-le à jour.')
                outcome.append(result(False, 'incompatible_server', message))
        except _PinError:
            outcome.append(result(False, 'certificate_mismatch',
                                  'Le certificat du poste ingénieur a changé. Demande une nouvelle invitation.'))
        except (OSError, ValueError, http.client.HTTPException):
            outcome.append(result(False, 'unreachable',
                                  'Poste ingénieur injoignable. Vérifie qu’il est démarré et que son adresse est accessible.'))
        finally:
            if connection is not None:
                connection.close()
            _CHECK_SLOTS.release()
            done.set()

    try:
        threading.Thread(target=worker, name='gt7-connection-check', daemon=True).start()
    except Exception:
        _CHECK_SLOTS.release()
        return result(False, 'unreachable', 'Impossible de démarrer le test de connexion. Réessaie.')
    if not done.wait(timeout=CHECK_TIMEOUT_S):
        cancelled.set()
        with active_lock:
            connection = active[0] if active else None
        if connection is not None:
            connection.close()
        return result(False, 'unreachable',
                      'Le test de connexion a expiré. Vérifie l’adresse du poste ingénieur et sa connexion réseau.')
    return outcome[0] if outcome else result(False, 'unreachable', 'Test de connexion interrompu. Réessaie.')


class PilotLink:
    """Envoie le dernier échantillon à 10 Hz maximum, sans bloquer le thread UDP."""
    def __init__(self, invitation: str, on_status: Callable[[str], None] | None = None):
        self._address = _parse_invitation(invitation)
        self.car_number = self._address['car']
        self._on_status = on_status
        self._status = None
        self._lock = threading.Lock()
        self._pending = None
        self._sequence = 0
        self._session = uuid.uuid4().hex
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._connection = None

    def _notify(self, text):
        if text != self._status:
            self._status = text
            if self._on_status is not None:
                try:
                    self._on_status(text)
                except Exception:
                    pass

    def start(self):
        with self._lock:
            # Une résolution DNS système peut dépasser le timeout socket. Ne
            # jamais réactiver l'événement partagé tant que ce worker vit.
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name=f'gt7-pilot-{self.car_number}', daemon=True)
            self._thread.start()

    def publish(self, telemetry):
        if self._stop.is_set():
            return
        try:
            payload = {name: value for name in (*_FIELDS, 'tyre_temperatures')
                       if (value := getattr(telemetry, name, None)) is not None}
            payload = _validate_telemetry(payload)
        except (TypeError, ValueError, AttributeError):
            self._notify('Mesure PS5 invalide : non transmise')
            return
        with self._lock:
            if self._stop.is_set():
                return
            self._sequence += 1
            self._pending = (self._sequence, time.monotonic(), payload)
        self._wake.set()

    def _connect(self):
        connection = _pinned_connection(self._address)
        self._connection = connection
        return connection

    def _send(self, sample):
        sequence, produced, telemetry = sample
        connection = self._connect()
        try:
            age = time.monotonic() - produced
            if age > SAMPLE_TTL_S or self._stop.is_set():
                return False
            data = {'v': PROTOCOL_VERSION, 'session': self._session, 'sequence': sequence,
                    'age_ms': max(0., age * 1000), 'telemetry': telemetry}
            body = json.dumps(data, separators=(',', ':'), allow_nan=False).encode('utf-8')
            connection.request('POST', f'/v1/telemetry/{self.car_number}', body=body,
                               headers={'Content-Type': 'application/json',
                                        'Authorization': 'Bearer ' + self._address['token']})
            response = connection.getresponse()
            status = response.status
            response.read(1024)
            if status == 204:
                return True
            if status == 401:
                raise PermissionError('Invitation refusée')
            raise OSError('Mesure refusée par le poste ingénieur')
        finally:
            connection.close()
            self._connection = None

    def _run(self):
        self._notify('En attente des données PS5')
        next_send = 0.
        backoff = 0.25
        while not self._stop.is_set():
            self._wake.wait(timeout=0.25)
            self._wake.clear()
            if self._stop.is_set():
                break
            delay = next_send - time.monotonic()
            if delay > 0 and self._stop.wait(delay):
                break
            with self._lock:
                sample, self._pending = self._pending, None
            if sample is None:
                continue
            if time.monotonic() - sample[1] > SAMPLE_TTL_S:
                self._notify('En attente de données PS5 récentes')
                continue
            try:
                if self._send(sample):
                    self._notify('Télémétrie transmise à l’ingénieur')
                else:
                    self._notify('En attente de données PS5 récentes')
                backoff = 0.25
                next_send = time.monotonic() + SEND_INTERVAL_S
            except _PinError:
                self._notify('Certificat ingénieur différent : renouvelle l’invitation')
                next_send = time.monotonic() + 2.0
            except PermissionError:
                self._notify('Invitation refusée : demande une nouvelle invitation')
                next_send = time.monotonic() + 2.0
            except (OSError, ValueError, http.client.HTTPException):
                self._notify('Ingénieur injoignable : reconnexion automatique')
                next_send = time.monotonic() + backoff
                backoff = min(backoff * 2, 5.0)
            # Une nouvelle publication pendant l'envoi doit être traitée ensuite.
            with self._lock:
                if self._pending is not None:
                    self._wake.set()
        self._notify('Transmission arrêtée')

    def stop(self):
        with self._lock:
            self._stop.set()
            self._wake.set()
            worker = self._thread
            self._pending = None
        connection = self._connection
        if connection is not None:
            connection.close()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=NETWORK_TIMEOUT_S + 1)
        with self._lock:
            if self._thread is worker and (worker is None or not worker.is_alive()):
                self._thread = None
            self._pending = None
