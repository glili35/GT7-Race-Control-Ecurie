"""Relais WebSocket optionnel : authentification et transit de trames opaques.

Le serveur ne reçoit aucune clé de télémétrie. Seuls les accès des salles sont
persistés ; les sorties gardent au plus une trame en transit par voiture.
L'interface CLI n'installe ni hébergement ni règle réseau.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
import argparse
import hmac
import json
import logging
import os
from pathlib import Path
import signal
import socket
import ssl
import tempfile
import threading
from time import monotonic
from uuid import uuid4

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from .relay_protocol import (
    MAX_FRAME_BYTES, RelayProtocolError, dumps_frame, loads_frame, new_secret,
    validate_car, validate_id, validate_sample_frame, validate_secret,
)

MAX_ROOMS = 16
MAX_CONNECTIONS = 64
MAX_CONTROL_QUEUE = 8
HELLO_TIMEOUT = 3.0
CHALLENGE_MAX_AGE = 2.5
SAMPLE_RATE = 10.0
SAMPLE_BURST = 20
CONTROL_RATE = 10.0
CONTROL_BURST = 20

# Le logger de websockets peut inclure des trames/entêtes en mode debug.
# Cet objet indépendant ne propage aucun événement au logger de l'application.
_QUIET = logging.Logger("GT7RelayPrivate")
_QUIET.disabled = True
_QUIET.propagate = False


class _Rejected(Exception):
    def __init__(self, code="invalid_message"):
        self.code = code


_MESSAGES = {
    "unauthorized": "Accès refusé.", "room_unknown": "Salle inconnue.",
    "slot_busy": "Cet accès est déjà connecté.", "capacity": "Capacité du relais atteinte.",
    "invalid_message": "Message ou débit non autorisé.",
}


def _abort_socket(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


class _Bucket:
    def __init__(self, rate, burst):
        self.rate, self.burst, self.tokens, self.updated = rate, burst, float(burst), monotonic()

    def take(self):
        now = monotonic()
        self.tokens = min(self.burst, self.tokens + max(0., now - self.updated) * self.rate)
        self.updated = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


@dataclass
class _Room:
    room: str
    owner: str
    writers: dict[int, str]
    engineer: object = None
    pilots: dict[int, object] = field(default_factory=dict)
    challenge: str | None = None
    challenge_at: float | None = None


class _Peer:
    """Un writer indépendant ; contrôle borné et dernier sample par slot."""

    def __init__(self, connection, room, role, car=None):
        self.connection, self.room, self.role, self.car = connection, room, role, car
        self.closed = threading.Event()
        self.condition = threading.Condition()
        self.controls = deque()
        self.challenge = None
        self.samples = OrderedDict()
        self.sequences = OrderedDict()
        self.rate = _Bucket(CONTROL_RATE, CONTROL_BURST)
        self.sample_rate = _Bucket(SAMPLE_RATE, SAMPLE_BURST)
        self.writer = threading.Thread(target=self._write, name="GT7Relay-Writer", daemon=True)

    def control(self, frame, sent=None):
        with self.condition:
            if self.closed.is_set():
                return False
            if frame["op"] == "challenge":
                self.challenge = (frame, monotonic())
            elif len(self.controls) >= MAX_CONTROL_QUEUE:
                self.abort()
                return False
            else:
                self.controls.append((frame, sent))
            self.condition.notify()
            return True

    def sample(self, car, frame):
        with self.condition:
            if not self.closed.is_set():
                self.samples[car] = (frame, monotonic())
                self.condition.notify()

    def _write(self):
        try:
            while not self.closed.is_set():
                sent = None
                with self.condition:
                    while not self.closed.is_set() and not (self.controls or self.challenge or self.samples):
                        self.condition.wait(.5)
                    if self.closed.is_set():
                        return
                    if self.controls:
                        frame, sent = self.controls.popleft()
                    elif self.challenge is not None:
                        frame, stamp = self.challenge
                        self.challenge = None
                        if monotonic() - stamp > CHALLENGE_MAX_AGE:
                            continue
                    else:
                        _, (frame, stamp) = self.samples.popitem(last=False)
                        if monotonic() - stamp > CHALLENGE_MAX_AGE:
                            continue
                self.connection.send(dumps_frame(frame))
                if sent is not None:
                    sent.set()
        except (ConnectionClosed, OSError, RelayProtocolError):
            pass
        finally:
            self.abort()

    def abort(self):
        self.closed.set()
        with self.condition:
            self.controls.clear()
            self.samples.clear()
            self.challenge = None
            self.condition.notify_all()
        _abort_socket(self.connection.socket)


class RelayServer:
    """Serveur local ou placé explicitement derrière un frontal TLS public."""

    def __init__(self, data_dir, bootstrap, host="127.0.0.1", port=8766, ssl_context=None):
        self.bootstrap = validate_secret(bootstrap)
        if not isinstance(host, str) or not host or type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Adresse ou port de relais incorrect.")
        if ssl_context is not None and not isinstance(ssl_context, ssl.SSLContext):
            raise ValueError("Contexte TLS serveur incorrect.")
        self.data_dir, self.host, self._requested_port = Path(data_dir), host, port
        self.ssl_context, self._port = ssl_context, port
        self._lock = threading.RLock()
        self._lifecycle = threading.Lock()
        self._stop = threading.Event()
        self._rooms = {}
        self._peers = set()
        self._raw_sockets = set()
        self._workers = set()
        self._server = self._accept_thread = None
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    @property
    def port(self):
        return self._port

    def _load(self):
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.data_dir.chmod(0o700)
        path = self.data_dir / "rooms.json"
        if not path.exists():
            return {}
        if path.is_symlink() or path.stat().st_size > MAX_FRAME_BYTES:
            raise ValueError("Métadonnées de relais incorrectes.")
        if os.name != "nt":
            path.chmod(0o600)
        data = loads_frame(path.read_text(encoding="utf-8"))
        if set(data) != {"v", "rooms"} or type(data["v"]) is not int or data["v"] != 1:
            raise ValueError("Métadonnées de relais incorrectes.")
        entries = data["rooms"]
        if not isinstance(entries, list) or len(entries) > MAX_ROOMS:
            raise ValueError("Métadonnées de relais incorrectes.")
        rooms = {}
        for item in entries:
            if not isinstance(item, dict) or set(item) != {"room", "owner", "writers"}:
                raise ValueError("Métadonnées de relais incorrectes.")
            room = validate_id(item["room"])
            owner = validate_secret(item["owner"])
            writers = item["writers"]
            if room in rooms or not isinstance(writers, dict) or set(writers) != {"1", "2", "3", "4"}:
                raise ValueError("Métadonnées de relais incorrectes.")
            rooms[room] = _Room(room, owner, {int(n): validate_secret(token) for n, token in writers.items()})
        return rooms

    def _persist(self):
        data = {"v": 1, "rooms": [{"room": room.room, "owner": room.owner,
                 "writers": {str(n): token for n, token in room.writers.items()}} for room in self._rooms.values()]}
        temporary = None
        try:
            fd, name = tempfile.mkstemp(prefix=".rooms-", suffix=".tmp", dir=self.data_dir)
            temporary = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.data_dir / "rooms.json")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def start(self):
        with self._lifecycle:
            if self._server is not None or self._workers:
                raise RuntimeError("Le relais est déjà ouvert ou termine sa fermeture.")
            self._rooms = self._load()
            self._stop.clear()
            self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
            if self.ssl_context is not None:
                # Éviter les NewSessionTicket TLS 1.3 post-handshake pendant
                # les premières lectures/écritures SSL du transport synchrone.
                # Les essais ont isolé un blocage HTTP initial absent avec
                # ces tickets désactivés ; TLS 1.3 et les certificats restent
                # disponibles, sans plafonnement de version TLS.
                self.ssl_context.num_tickets = 0
            server = serve(self._handle, self.host, self._requested_port, ssl=self.ssl_context,
                           compression=None, origins=[None], process_request=self._request,
                           max_size=MAX_FRAME_BYTES, max_queue=4, open_timeout=HELLO_TIMEOUT,
                           close_timeout=.5, ping_interval=20, ping_timeout=10, server_header=None,
                           logger=_QUIET, backlog=MAX_CONNECTIONS)
            server.socket.settimeout(.2)
            self._server, self._port = server, server.socket.getsockname()[1]
            self._accept_thread = threading.Thread(target=self._accept, args=(server,), name="GT7Relay-Accept", daemon=True)
            self._accept_thread.start()

    @staticmethod
    def _request(connection, request):
        if request.path != "/relay/v1":
            return connection.respond(404, "Relais indisponible.\n")
        return None

    def _accept(self, server):
        # sync.Server.serve_forever crée sinon un thread par accept() avant
        # toute authentification. L'admission est bornée avant cette création.
        while not self._stop.is_set():
            try:
                sock, address = server.socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                _abort_socket(sock)
                continue
            with self._lock:
                if self._stop.is_set():
                    self._slots.release()
                    _abort_socket(sock)
                    return
                worker = threading.Thread(target=self._raw_worker, args=(server, sock, address), name="GT7Relay-Client", daemon=True)
                self._raw_sockets.add(sock)
                self._workers.add(worker)
                try:
                    # Inscription et démarrage atomiques vis-à-vis de stop(),
                    # qui ne doit jamais joindre un thread non démarré.
                    worker.start()
                except Exception:
                    self._raw_sockets.discard(sock)
                    self._workers.discard(worker)
                    self._slots.release()
                    _abort_socket(sock)

    def _raw_worker(self, server, sock, address):
        try:
            server.handler(sock, address)
        finally:
            _abort_socket(sock)
            with self._lock:
                self._raw_sockets.discard(sock)
                self._workers.discard(threading.current_thread())
            self._slots.release()

    def _register(self, connection, frame):
        if type(frame.get("v")) is not int or frame["v"] != 1:
            raise _Rejected()
        with self._lock:
            if self._stop.is_set():
                raise _Rejected("capacity")
            if frame.get("op") == "create":
                if set(frame) != {"v", "op", "bootstrap"}:
                    raise _Rejected()
                token = validate_secret(frame["bootstrap"])
                if not hmac.compare_digest(token, self.bootstrap):
                    raise _Rejected("unauthorized")
                if len(self._rooms) >= MAX_ROOMS:
                    raise _Rejected("capacity")
                room = _Room(uuid4().hex, new_secret(), {n: new_secret() for n in range(1, 5)})
                self._rooms[room.room] = room
                try:
                    self._persist()
                except Exception:
                    del self._rooms[room.room]
                    raise
                peer = _Peer(connection, room, "engineer")
                answer = {"v": 1, "op": "created", "room": room.room, "owner": room.owner,
                          "writers": {str(n): t for n, t in room.writers.items()}}
            elif frame.get("op") == "join":
                role = frame.get("role")
                fields = {"v", "op", "room", "role", "token"}
                if role == "pilot":
                    fields.add("car")
                if role not in ("pilot", "engineer") or set(frame) != fields:
                    raise _Rejected()
                room_id, token = validate_id(frame["room"]), validate_secret(frame["token"])
                room = self._rooms.get(room_id)
                if room is None:
                    raise _Rejected("room_unknown")
                car = validate_car(frame["car"]) if role == "pilot" else None
                expected = room.writers[car] if car is not None else room.owner
                if not hmac.compare_digest(token, expected):
                    raise _Rejected("unauthorized")
                active = room.pilots.get(car) if car is not None else room.engineer
                if active is not None and not active.closed.is_set():
                    raise _Rejected("slot_busy")
                peer = _Peer(connection, room, role, car)
                answer = {"v": 1, "op": "joined", "room": room.room, "role": role}
                if car is not None:
                    answer["car"] = car
            else:
                raise _Rejected()
            if peer.role == "engineer":
                room.engineer = peer
                room.challenge = room.challenge_at = None
            else:
                room.pilots[peer.car] = peer
            self._peers.add(peer)
            peer.control(answer)
            if peer.role == "pilot" and room.challenge and monotonic() - room.challenge_at <= CHALLENGE_MAX_AGE:
                peer.control({"v": 1, "op": "challenge", "challenge": room.challenge})
            peer.writer.start()
            return peer

    def _message(self, peer, frame):
        if not peer.rate.take():
            raise _Rejected()
        room = peer.room
        if peer.role == "pilot":
            if not peer.sample_rate.take():
                raise _Rejected()
            sample = validate_sample_frame(frame)
            session, sequence = sample["session"], sample["sequence"]
            with self._lock:
                if room.pilots.get(peer.car) is not peer:
                    raise _Rejected("unauthorized")
                previous = peer.sequences.get(session, 0)
                if sequence <= previous:
                    return
                peer.sequences[session] = sequence
                peer.sequences.move_to_end(session)
                while len(peer.sequences) > 8:
                    peer.sequences.popitem(last=False)
                engineer = room.engineer
                if (engineer is None or engineer.closed.is_set() or sample["challenge"] != room.challenge
                        or room.challenge_at is None or monotonic() - room.challenge_at > CHALLENGE_MAX_AGE):
                    return
                engineer.sample(peer.car, {**sample, "car": peer.car})
            return
        if type(frame.get("v")) is not int or frame["v"] != 1:
            raise _Rejected()
        if frame.get("op") == "challenge" and set(frame) == {"v", "op", "challenge"}:
            challenge = validate_id(frame["challenge"])
            with self._lock:
                if room.engineer is not peer:
                    raise _Rejected("unauthorized")
                room.challenge, room.challenge_at = challenge, monotonic()
                pilots = tuple(room.pilots.values())
            for pilot in pilots:
                pilot.control({"v": 1, "op": "challenge", "challenge": challenge})
        elif frame.get("op") == "rotate" and set(frame) == {"v", "op", "car"}:
            car = validate_car(frame["car"])
            token = new_secret()
            with self._lock:
                if room.engineer is not peer:
                    raise _Rejected("unauthorized")
                previous = room.writers[car]
                room.writers[car] = token
                try:
                    self._persist()
                except Exception:
                    room.writers[car] = previous
                    raise
                pilot = room.pilots.pop(car, None)
            if pilot is not None:
                pilot.abort()
            peer.control({"v": 1, "op": "rotated", "car": car, "token": token})
        else:
            raise _Rejected()

    def _handle(self, connection):
        peer = None
        try:
            peer = self._register(connection, loads_frame(connection.recv(timeout=HELLO_TIMEOUT)))
            while not self._stop.is_set() and not peer.closed.is_set():
                try:
                    raw = connection.recv(timeout=.5)
                except TimeoutError:
                    continue
                self._message(peer, loads_frame(raw))
        except (ConnectionClosed, OSError):
            pass
        except Exception as exc:
            code = exc.code if isinstance(exc, _Rejected) else "invalid_message"
            error = {"v": 1, "op": "error", "code": code, "message": _MESSAGES[code]}
            if peer is None:
                try:
                    connection.send(dumps_frame(error))
                except Exception:
                    pass
            else:
                sent = threading.Event()
                peer.control(error, sent)
                sent.wait(.25)
        finally:
            if peer is not None:
                peer.abort()
                peer.writer.join(timeout=1)
                with self._lock:
                    self._peers.discard(peer)
                    if peer.role == "engineer" and peer.room.engineer is peer:
                        peer.room.engineer = None
                        peer.room.challenge = peer.room.challenge_at = None
                    elif peer.room.pilots.get(peer.car) is peer:
                        del peer.room.pilots[peer.car]

    def stop(self):
        with self._lifecycle:
            server = self._server
            if server is None:
                return
            self._stop.set()
            with self._lock:
                peers, sockets = tuple(self._peers), tuple(self._raw_sockets)
                workers = tuple(self._workers)
            server.shutdown()
            for peer in peers:
                peer.abort()
            for sock in sockets:
                _abort_socket(sock)
            deadline = monotonic() + 4
            for worker in (self._accept_thread, *workers):
                if worker is not None and worker is not threading.current_thread():
                    worker.join(timeout=max(0., deadline - monotonic()))
            for name in ("shutdown_watcher", "shutdown_notifier"):
                fd = getattr(server, name, None)
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            self._server = self._accept_thread = None
            if any(worker.is_alive() for worker in workers):
                raise RuntimeError("Le relais termine encore la fermeture de ses connexions.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Relais GT7 optionnel, sans historique de télémétrie.")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1", help="Écoute locale par défaut ; frontal TLS requis pour un accès public.")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--bootstrap-file", type=Path, help="Fichier privé contenant le secret d'administration.")
    parser.add_argument("--certfile", type=Path)
    parser.add_argument("--keyfile", type=Path)
    args = parser.parse_args(argv)
    server = None
    try:
        bootstrap = os.environ.get("GT7_RELAY_BOOTSTRAP", "")
        if args.bootstrap_file:
            if args.bootstrap_file.stat().st_size > 128:
                raise ValueError
            bootstrap = args.bootstrap_file.read_text(encoding="ascii").strip()
        if not bootstrap:
            parser.error("Définir GT7_RELAY_BOOTSTRAP ou --bootstrap-file ; aucun secret ne doit figurer dans les arguments.")
        context = None
        if args.certfile or args.keyfile:
            if not (args.certfile and args.keyfile):
                parser.error("--certfile et --keyfile sont nécessaires ensemble.")
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(str(args.certfile), str(args.keyfile))
        server = RelayServer(args.data_dir, bootstrap, host=args.host, port=args.port, ssl_context=context)
        stop = threading.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
        server.start()
        print(f"Relais actif sur le port {server.port}. Aucun historique de télémétrie.", flush=True)
        while not stop.wait(.5):
            pass
        return 0
    except (Exception, KeyboardInterrupt):
        print("Le relais n'a pas pu fonctionner ; vérifier la configuration et les accès aux fichiers.")
        return 1
    finally:
        if server is not None:
            server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
