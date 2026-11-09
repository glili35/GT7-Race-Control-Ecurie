"""Vrais WebSockets en boucle locale ; aucun hébergement ni console extérieure."""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.sync.client import connect

from racecontrol import relay_server
from racecontrol.relay_protocol import dumps_frame, loads_frame, new_secret


_QUIET_CLIENT = logging.Logger("GT7RelayTestClient")
_QUIET_CLIENT.disabled = True
OPAQUE = base64.urlsafe_b64encode(b"opaque-ciphertext-not-plaintext-telemetry").rstrip(b"=").decode("ascii")


class RelayServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "server"
        self.bootstrap = new_secret()
        self.connections = []
        self.releases = []
        self.server = relay_server.RelayServer(self.directory, self.bootstrap, port=0)
        self.server.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for release in self.releases:
            release.set()
        try:
            self.server.stop()
        finally:
            for connection in self.connections:
                connection.close()
            self.connections.clear()
            self.temp.cleanup()

    def _connect(self, suffix="/relay/v1", **kwargs):
        connection = connect(f"ws://127.0.0.1:{self.server.port}" + suffix, proxy=None,
                             compression=None, open_timeout=2, close_timeout=.2,
                             ping_interval=None, max_size=32768, logger=_QUIET_CLIENT, **kwargs)
        self.connections.append(connection)
        return connection

    def _receive(self, connection):
        return loads_frame(connection.recv(timeout=2))

    def _create(self):
        engineer = self._connect()
        engineer.send(dumps_frame({"v": 1, "op": "create", "bootstrap": self.bootstrap}))
        room = self._receive(engineer)
        self.assertEqual(room["op"], "created")
        self.assertEqual(set(room["writers"]), {"1", "2", "3", "4"})
        self.assertNotIn("key", room)
        return engineer, room

    def _join_frame(self, room, *, role="pilot", car=1, token=None):
        frame = {"v": 1, "op": "join", "room": room["room"], "role": role,
                 "token": token or (room["owner"] if role == "engineer" else room["writers"][str(car)])}
        if role == "pilot":
            frame["car"] = car
        return frame

    def _join(self, room, **kwargs):
        connection = self._connect()
        connection.send(dumps_frame(self._join_frame(room, **kwargs)))
        frame = self._receive(connection)
        self.assertEqual(frame["op"], "joined")
        return connection

    def _join_engineer_after_close(self, room):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            connection = self._connect()
            connection.send(dumps_frame(self._join_frame(room, role="engineer")))
            frame = self._receive(connection)
            if frame["op"] == "joined":
                return connection
            self.assertEqual(frame.get("code"), "slot_busy")
            connection.close()
            time.sleep(.01)
        self.fail("Le poste ingénieur ne peut pas se reconnecter après sa fermeture.")

    def _challenge(self, engineer, *pilots):
        challenge = uuid4().hex
        engineer.send(dumps_frame({"v": 1, "op": "challenge", "challenge": challenge}))
        for pilot in pilots:
            frame = self._receive(pilot)
            self.assertEqual(frame["op"], "challenge")
            self.assertEqual(frame["challenge"], challenge)
        return challenge

    @staticmethod
    def _sample(challenge, sequence=1, session=None):
        return {"v": 1, "op": "sample", "session": session or uuid4().hex,
                "sequence": sequence, "challenge": challenge, "payload": OPAQUE}

    def _empty(self, connection):
        with self.assertRaises(TimeoutError):
            connection.recv(timeout=.12)

    def _error(self, connection, code):
        frame = self._receive(connection)
        self.assertEqual(frame["op"], "error")
        self.assertEqual(frame["code"], code)
        with self.assertRaises(ConnectionClosed):
            connection.recv(timeout=2)

    def test_bootstrap_and_room_role_tokens_are_required(self):
        connection = self._connect()
        connection.send(dumps_frame({"v": 1, "op": "create", "bootstrap": new_secret()}))
        self._error(connection, "unauthorized")
        engineer, room = self._create()
        for role in ("pilot", "engineer"):
            with self.subTest(role=role):
                connection = self._connect()
                connection.send(dumps_frame(self._join_frame(room, role=role, token=new_secret())))
                self._error(connection, "unauthorized")
        unknown = self._connect()
        frame = self._join_frame(room)
        frame["room"] = uuid4().hex
        unknown.send(dumps_frame(frame))
        self._error(unknown, "room_unknown")
        self._empty(engineer)

    def test_salles_are_isolated_and_server_assigns_authenticated_car(self):
        engineer_a, room_a = self._create()
        engineer_b, room_b = self._create()
        pilot_a = self._join(room_a, car=4)
        pilot_b = self._join(room_b, car=1)
        challenge_a = self._challenge(engineer_a, pilot_a)
        challenge_b = self._challenge(engineer_b, pilot_b)
        pilot_a.send(dumps_frame(self._sample(challenge_a)))
        frame_a = self._receive(engineer_a)
        self.assertEqual(frame_a["car"], 4)
        self.assertEqual(frame_a["payload"], OPAQUE)
        self._empty(engineer_b)
        pilot_b.send(dumps_frame(self._sample(challenge_b)))
        self.assertEqual(self._receive(engineer_b)["car"], 1)
        self._empty(engineer_a)
        wrong_room = self._connect()
        wrong_room.send(dumps_frame(self._join_frame(room_b, token=room_a["writers"]["1"])))
        self._error(wrong_room, "unauthorized")

    def test_one_engineer_and_one_pilot_per_slot(self):
        engineer, room = self._create()
        duplicate = self._connect()
        duplicate.send(dumps_frame(self._join_frame(room, role="engineer")))
        self._error(duplicate, "slot_busy")
        self._join(room, car=2)
        duplicate = self._connect()
        duplicate.send(dumps_frame(self._join_frame(room, car=2)))
        self._error(duplicate, "slot_busy")
        self._empty(engineer)

    def test_rotation_disconnects_old_pilot_and_persists_new_token(self):
        engineer, room = self._create()
        old_pilot = self._join(room)
        engineer.send(dumps_frame({"v": 1, "op": "rotate", "car": 1}))
        rotation = self._receive(engineer)
        self.assertEqual(rotation["op"], "rotated")
        self.assertTrue(rotation["token"] != room["writers"]["1"])
        with self.assertRaises(ConnectionClosed):
            old_pilot.recv(timeout=2)
        rejected = self._connect()
        rejected.send(dumps_frame(self._join_frame(room)))
        self._error(rejected, "unauthorized")
        pilot = self._join(room, token=rotation["token"])
        challenge = self._challenge(engineer, pilot)
        pilot.send(dumps_frame(self._sample(challenge)))
        self.assertEqual(self._receive(engineer)["car"], 1)
        stored = json.loads((self.directory / "rooms.json").read_text(encoding="utf-8"))
        self.assertTrue(stored["rooms"][0]["writers"]["1"] == rotation["token"])

    def test_reconnection_never_replays_samples_and_disk_contains_only_access_metadata(self):
        engineer, room = self._create()
        pilot = self._join(room)
        challenge = self._challenge(engineer, pilot)
        pilot.send(dumps_frame(self._sample(challenge)))
        self.assertEqual(self._receive(engineer)["op"], "sample")
        engineer.close()
        engineer = self._join_engineer_after_close(room)
        self._empty(engineer)
        pilot.send(dumps_frame(self._sample(challenge, sequence=2)))
        self._empty(engineer)
        self.server.stop()
        data = json.loads((self.directory / "rooms.json").read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"v", "rooms"})
        self.assertEqual(set(data["rooms"][0]), {"room", "owner", "writers"})
        if os.name != "nt":
            self.assertEqual((self.directory / "rooms.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.server.start()
        connection = self._join(room, role="engineer")
        self._empty(connection)

    def test_expired_challenge_is_not_replayed_or_used_for_forwarding(self):
        engineer, room = self._create()
        pilot = self._join(room)
        with patch.object(relay_server, "CHALLENGE_MAX_AGE", .08):
            challenge = self._challenge(engineer, pilot)
            time.sleep(.12)
            late_pilot = self._join(room, car=2)
            self._empty(late_pilot)
            pilot.send(dumps_frame(self._sample(challenge)))
            self._empty(engineer)
        fresh = self._challenge(engineer, pilot, late_pilot)
        pilot.send(dumps_frame(self._sample(fresh, sequence=2)))
        self.assertEqual(self._receive(engineer)["challenge"], fresh)

    def test_pilot_cannot_override_car_or_send_control_operations(self):
        engineer, room = self._create()
        pilot = self._join(room)
        challenge = self._challenge(engineer, pilot)
        sample = {**self._sample(challenge), "car": 4}
        pilot.send(dumps_frame(sample))
        self._error(pilot, "invalid_message")
        self._empty(engineer)
        pilot = self._join(room, car=2)
        pilot.send(dumps_frame({"v": 1, "op": "rotate", "car": 1}))
        # Un challenge frais peut précéder l'erreur dans cette connexion.
        frame = self._receive(pilot)
        if frame["op"] == "challenge":
            frame = self._receive(pilot)
        self.assertEqual(frame.get("code"), "invalid_message")

    def test_sample_rate_is_bounded(self):
        engineer, room = self._create()
        with patch.object(relay_server, "SAMPLE_RATE", .01), patch.object(relay_server, "SAMPLE_BURST", 3):
            pilot = self._join(room)
        challenge = self._challenge(engineer, pilot)
        session = uuid4().hex
        for sequence in range(1, 8):
            try:
                pilot.send(dumps_frame(self._sample(challenge, sequence, session)))
            except ConnectionClosed:
                break
        self._error(pilot, "invalid_message")

    def test_room_limit_and_invalid_handshake_path(self):
        with patch.object(relay_server, "MAX_ROOMS", 2):
            self._create()
            self._create()
            rejected = self._connect()
            rejected.send(dumps_frame({"v": 1, "op": "create", "bootstrap": self.bootstrap}))
            self._error(rejected, "capacity")
        data = json.loads((self.directory / "rooms.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["rooms"]), 2)
        with self.assertRaises(InvalidHandshake):
            self._connect("/relay/v1?unexpected=1")
        with self.assertRaises(InvalidHandshake):
            self._connect(origin="https://example.invalid")

    def test_raw_connection_admission_is_bounded_and_stop_interrupts_handshake(self):
        self.server.stop()
        with patch.object(relay_server, "MAX_CONNECTIONS", 2):
            self.server.start()
        raw_a = socket.create_connection(("127.0.0.1", self.server.port), timeout=1)
        raw_b = socket.create_connection(("127.0.0.1", self.server.port), timeout=1)
        try:
            deadline = time.monotonic() + 1
            while len(self.server._raw_sockets) < 2 and time.monotonic() < deadline:
                time.sleep(.005)
            # Le refus TCP précède volontairement le handshake. Selon le
            # moment où le client observe la fermeture, websockets expose une
            # erreur HTTP, socket, ou ConnectionClosed avant sa requête HTTP.
            with self.assertRaises((InvalidHandshake, ConnectionClosed, OSError)):
                self._connect()
            started = time.monotonic()
            self.server.stop()
            self.assertLess(time.monotonic() - started, 2)
            self.assertFalse(self.server._workers)
        finally:
            raw_a.close()
            raw_b.close()

    def test_stop_racing_with_admission_never_joins_an_unstarted_thread(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original_start = threading.Thread.start
        failures = []

        def gated_start(thread):
            if thread.name == "GT7Relay-Client":
                entered.set()
                release.wait(2)
            return original_start(thread)

        def stop():
            try:
                self.server.stop()
            except Exception as exc:
                failures.append(exc)

        with patch.object(threading.Thread, "start", gated_start):
            raw = socket.create_connection(("127.0.0.1", self.server.port), timeout=1)
            try:
                self.assertTrue(entered.wait(1))
                stopper = threading.Thread(target=stop)
                original_start(stopper)
                time.sleep(.03)
                release.set()
                stopper.join(timeout=3)
                self.assertFalse(stopper.is_alive())
                self.assertFalse(failures, "Une admission en cours doit pouvoir être arrêtée proprement.")
            finally:
                raw.close()

    def test_slow_engineer_has_latest_only_queue_and_does_not_block_another_room(self):
        engineer, room = self._create()
        pilots = [self._join(room, car=n) for n in range(1, 5)]
        challenge = self._challenge(engineer, *pilots)
        release, blocked = threading.Event(), threading.Event()
        self.releases.append(release)
        with self.server._lock:
            peer = next(peer for peer in self.server._peers if peer.role == "engineer")
        original_send = peer.connection.send

        def slow_send(message):
            if loads_frame(message)["op"] == "sample":
                blocked.set()
                release.wait(3)
            return original_send(message)

        with patch.object(peer.connection, "send", side_effect=slow_send):
            first_session = uuid4().hex
            pilots[0].send(dumps_frame(self._sample(challenge, 1, first_session)))
            self.assertTrue(blocked.wait(1))
            for car, pilot in enumerate(pilots, 1):
                session = first_session if car == 1 else uuid4().hex
                for sequence in range(2 if car == 1 else 1, 9):
                    pilot.send(dumps_frame(self._sample(challenge, sequence, session)))
            second_engineer, second_room = self._create()
            second_pilot = self._join(second_room)
            second_challenge = self._challenge(second_engineer, second_pilot)
            second_pilot.send(dumps_frame(self._sample(second_challenge)))
            self.assertEqual(self._receive(second_engineer)["op"], "sample")
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                with peer.condition:
                    ready = set(peer.samples) == {1, 2, 3, 4} and all(frame["sequence"] == 8 for frame, _ in peer.samples.values())
                if ready:
                    break
                time.sleep(.005)
            self.assertTrue(ready, "La sortie bloquée doit conserver seulement la dernière trame de chaque voiture.")
            release.set()
            seen = {}
            for _ in range(6):
                frame = self._receive(engineer)
                seen[frame["car"]] = frame["sequence"]
                if seen == {1: 8, 2: 8, 3: 8, 4: 8}:
                    break
            self.assertEqual(seen, {1: 8, 2: 8, 3: 8, 4: 8})


if __name__ == "__main__":
    unittest.main()
