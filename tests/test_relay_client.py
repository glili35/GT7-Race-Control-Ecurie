"""Clients réellement reliés en WS/WSS local ; aucune PS5 n'est simulée comme réelle."""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from racecontrol.fleet import FleetMonitor
from racecontrol.relay_server import RelayServer
from racecontrol.relay_protocol import (
    RelayProtocolError, new_secret, parse_invitation, encrypt_sample, make_invitation,
)
from racecontrol.relay_client import (
    RelayClientError, RelayEngineerSession, RelayPilotLink, check_relay_invitation,
    _Channel, _open, _frame,
)


def wait_for(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError('Résultat client relais non obtenu dans le délai prévu')


def test_certificate(directory):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'GT7 relay test CA')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .sign(key, hashes.SHA256()))
    certificate, private = directory / 'test-ca.pem', directory / 'test-key.pem'
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, private)
    return context, certificate


class RelayClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bootstrap = new_secret()
        self.server = RelayServer(self.root / 'server', self.bootstrap, host='127.0.0.1', port=0)
        self.server.start()
        self.url = f'ws://127.0.0.1:{self.server.port}/relay/v1'
        self.fleet = FleetMonitor()
        self.statuses = []
        self.engineer = RelayEngineerSession(self.url, self.bootstrap, self.root / 'engineer', self.fleet,
                                             on_status=self.statuses.append)
        self.engineers = [self.engineer]
        self.pilots = []
        self.addCleanup(self.cleanup)
        self.engineer.start()
        self.assertTrue(self.engineer.wait_ready(), self.statuses)

    def cleanup(self):
        for pilot in self.pilots:
            pilot.stop()
        for engineer in self.engineers:
            engineer.stop()
        self.server.stop()
        self.temp.cleanup()

    def pilot(self, car, invitation=None):
        connected, statuses = threading.Event(), []
        def status(text):
            statuses.append(text)
            if text.startswith('Relais connecté'):
                connected.set()
        pilot = RelayPilotLink(invitation or self.engineer.invitation(car), on_status=status)
        self.pilots.append(pilot)
        pilot.start()
        self.assertTrue(connected.wait(timeout=5))
        return pilot, statuses

    def drive(self, pilots, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for car, pilot in pilots.items():
                pilot.publish(SimpleNamespace(speed_kmh=50. * car, fuel_level=10. * car,
                                              fuel_capacity=100., current_lap=2,
                                              position_x=car * 100., position_z=car * -25.))
            if predicate():
                return
            time.sleep(0.025)
        raise AssertionError('Les mesures des pilotes ne sont pas arrivées')

    def test_four_clients_are_isolated_and_server_never_receives_encryption_keys(self):
        pilots = {car: self.pilot(car)[0] for car in range(1, 5)}
        self.drive(pilots, lambda: all(car.connected for car in self.fleet.snapshots()))
        for car in self.fleet.snapshots():
            self.assertEqual(car.speed_kmh, 50. * car.number)
            self.assertEqual(car.fuel_percent, 10. * car.number)
            self.assertEqual(car.position_x, car.number * 100.)
        server_files = b''.join(path.read_bytes() for path in (self.root / 'server').rglob('*') if path.is_file())
        for car in range(1, 5):
            invitation = parse_invitation(self.engineer.invitation(car))
            self.assertNotIn(invitation.key.encode(), server_files)
            self.assertFalse(any(invitation.key in status or invitation.token in status for status in self.statuses))

    def test_check_authenticates_without_telemetry_or_sequence_and_preserves_busy_pilot(self):
        invitation = self.engineer.invitation(1)
        result = check_relay_invitation(invitation)
        self.assertTrue(result.ok)
        self.assertEqual(result.car_number, 1)
        self.assertFalse(any(car.has_data for car in self.fleet.snapshots()))
        self.assertTrue(all(not entries for entries in self.engineer._seen.values()))
        pilot, statuses = self.pilot(1)
        result = check_relay_invitation(invitation)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'slot_busy')
        self.drive({1: pilot}, lambda: self.fleet.snapshots()[0].connected)
        self.assertFalse(any('injoignable' in status for status in statuses))

    def test_rotation_changes_only_one_token_and_key_and_invalidates_old_invitation(self):
        before = {car: self.engineer.invitation(car) for car in range(1, 5)}
        pilot, _ = self.pilot(1)
        self.drive({1: pilot}, lambda: self.fleet.snapshots()[0].connected)
        rotated = self.engineer.rotate_token(1)
        old, new = parse_invitation(before[1]), parse_invitation(rotated)
        self.assertNotEqual(old.token, new.token)
        self.assertNotEqual(old.key, new.key)
        for car in (2, 3, 4):
            self.assertEqual(self.engineer.invitation(car), before[car])
        pilot.stop()
        self.assertEqual(check_relay_invitation(before[1]).code, 'unauthorized')
        self.assertFalse(self.fleet.snapshots()[0].has_data)
        replacement, _ = self.pilot(1, invitation=rotated)
        self.drive({1: replacement}, lambda: self.fleet.snapshots()[0].connected)

    def test_engineer_restart_joins_private_room_and_preserves_invitations(self):
        invitations = [self.engineer.invitation(car) for car in range(1, 5)]
        self.engineer.stop()
        replacement = RelayEngineerSession(self.url, None, self.root / 'engineer', self.fleet)
        self.engineers.append(replacement)
        replacement.start()
        self.assertTrue(replacement.wait_ready())
        self.assertEqual([replacement.invitation(car) for car in range(1, 5)], invitations)
        profiles = list((self.root / 'engineer').glob('relay-profile-*.json'))
        self.assertEqual(len(profiles), 1)
        if os.name != 'nt':
            self.assertEqual(profiles[0].stat().st_mode & 0o777, 0o600)

    def test_single_sample_is_not_republished_by_challenges_or_keepalive(self):
        pilot, _ = self.pilot(1)
        pilot.publish(SimpleNamespace(speed_kmh=120.))
        wait_for(lambda: self.fleet.snapshots()[0].has_data)
        observed = self.fleet._cars[1].last_seen
        time.sleep(1.1)
        self.assertTrue(self.engineer.connected)
        self.assertEqual(self.fleet._cars[1].last_seen, observed)
        self.assertFalse(self.fleet.snapshots(now=observed + 3.1)[0].connected)

    def test_one_challenge_updates_ten_samples_without_advancing_freshness_to_receive_time(self):
        # Instance non démarrée pour un contrôle déterministe de l'horloge.
        fleet = FleetMonitor()
        engineer = RelayEngineerSession(self.url, None, self.root / 'unit', fleet)
        engineer._profile = json.loads(next((self.root / 'engineer').glob('*.json')).read_text())
        challenge, session = uuid.uuid4().hex, uuid.uuid4().hex
        engineer._challenges[challenge] = 100.
        key, room = engineer._profile['keys']['1'], engineer._profile['room']
        for sequence in range(1, 11):
            frame = encrypt_sample(room, 1, key, session, sequence, challenge,
                                   SimpleNamespace(speed_kmh=sequence * 10.), 20 + (sequence % 3) * 10)
            frame['car'] = 1
            now = 100. + sequence * .08
            with patch('racecontrol.relay_client.time.monotonic', return_value=now):
                engineer._receive_sample(frame, now)
            self.assertEqual(fleet.snapshots(now=now)[0].speed_kmh, sequence * 10.)
            self.assertLessEqual(fleet._cars[1].last_seen, 100.)
        self.assertFalse(fleet.snapshots(now=103.1)[0].connected)
        self.assertEqual(len(engineer._seen[1]), 1)

    def test_old_challenge_duplicate_sequence_and_ciphertext_tamper_cannot_refresh_fleet(self):
        engineer = RelayEngineerSession(self.url, None, self.root / 'unit', FleetMonitor())
        engineer._profile = json.loads(next((self.root / 'engineer').glob('*.json')).read_text())
        old, fresh, session = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
        engineer._challenges = OrderedDict([(old, 99.), (fresh, 100.)])
        room, key = engineer._profile['room'], engineer._profile['keys']['1']
        def sample(challenge, seq, speed):
            frame = encrypt_sample(room, 1, key, session, seq, challenge, SimpleNamespace(speed_kmh=speed), 0.)
            return {**frame, 'car': 1}
        with patch('racecontrol.relay_client.time.monotonic', return_value=100.5):
            good = sample(fresh, 1, 100.)
            engineer._receive_sample(good, 100.5)
            engineer._receive_sample(good, 100.5)
            engineer._receive_sample(sample(old, 2, 200.), 100.5)
            invalid = sample(fresh, 2, 250.)
            invalid['payload'] = invalid['payload'][:-2] + 'AA'
            with self.assertRaises(RelayProtocolError):
                engineer._receive_sample(invalid, 100.5)
        self.assertEqual(engineer.fleet.snapshots(now=100.5)[0].speed_kmh, 100.)
        self.assertEqual(engineer.fleet._cars[1].last_seen, 100.)
        with patch('racecontrol.relay_client.time.monotonic', return_value=103.):
            engineer._receive_sample(sample(fresh, 3, 300.), 103.)
        self.assertFalse(engineer.fleet.snapshots(now=103.1)[0].connected)

    def test_retired_session_cannot_return_after_a_new_session(self):
        engineer = RelayEngineerSession(self.url, None, self.root / 'unit', FleetMonitor())
        engineer._profile = json.loads(next((self.root / 'engineer').glob('*.json')).read_text())
        challenge, first, second = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
        engineer._challenges[challenge] = 100.
        room, key = engineer._profile['room'], engineer._profile['keys']['1']
        def sample(session, sequence, speed):
            return {**encrypt_sample(room, 1, key, session, sequence, challenge,
                                     SimpleNamespace(speed_kmh=speed), 0.), 'car': 1}
        with patch('racecontrol.relay_client.time.monotonic', return_value=100.5):
            engineer._receive_sample(sample(first, 1, 100.), 100.5)
            engineer._receive_sample(sample(second, 1, 200.), 100.5)
            engineer._receive_sample(sample(first, 2, 50.), 100.5)
        self.assertEqual(engineer.fleet.snapshots(now=100.5)[0].speed_kmh, 200.)
        self.assertEqual(engineer.fleet._cars[1].last_seen, 100.)

    def test_quarantined_slot_cannot_ingest_until_rotation_is_confirmed(self):
        engineer = RelayEngineerSession(self.url, None, self.root / 'unit', FleetMonitor())
        engineer._profile = json.loads(next((self.root / 'engineer').glob('*.json')).read_text())
        challenge = uuid.uuid4().hex
        engineer._challenges[challenge] = 100.
        room, key = engineer._profile['room'], engineer._profile['keys']['1']
        frame = {**encrypt_sample(room, 1, key, uuid.uuid4().hex, 1, challenge,
                                  SimpleNamespace(speed_kmh=300.), 0.), 'car': 1}
        engineer._invalid_slots.add(1)
        engineer._profile['quarantined'] = [1]
        engineer._save_profile(engineer._profile)
        with patch('racecontrol.relay_client.time.monotonic', return_value=100.5):
            engineer._receive_sample(frame, 100.5)
        self.assertFalse(engineer.fleet.snapshots(now=100.5)[0].has_data)
        replacement = RelayEngineerSession(self.url, None, self.root / 'unit', FleetMonitor())
        replacement._load_profile()
        self.assertIn(1, replacement._invalid_slots)

    def test_lost_rotation_ack_is_persisted_as_quarantine_and_can_be_recovered(self):
        original = self.server._message
        old_invitation = self.engineer.invitation(1)
        def lose_ack(peer, frame):
            if frame.get('op') != 'rotate':
                return original(peer, frame)
            original_control = peer.control
            def control(value, sent=None):
                if value.get('op') == 'rotated':
                    peer.abort()
                else:
                    original_control(value, sent)
            with patch.object(peer, 'control', side_effect=control):
                return original(peer, frame)
        with patch.object(self.server, '_message', side_effect=lose_ack):
            with self.assertRaises(RelayClientError):
                self.engineer.rotate_token(1)
        self.assertTrue(self.engineer.wait_ready())
        with self.assertRaises(RelayClientError):
            self.engineer.invitation(1)
        profile = json.loads(next((self.root / 'engineer').glob('*.json')).read_text())
        self.assertIn(1, profile['quarantined'])
        self.assertEqual(check_relay_invitation(old_invitation).code, 'unauthorized')
        new_invitation = self.engineer.rotate_token(1)
        self.assertTrue(check_relay_invitation(new_invitation).ok)
        profile = json.loads(next((self.root / 'engineer').glob('*.json')).read_text())
        self.assertNotIn(1, profile['quarantined'])

    def test_restart_server_never_replays_old_sample_and_reconnects_on_new_capture(self):
        pilot, _ = self.pilot(1)
        self.drive({1: pilot}, lambda: self.fleet.snapshots()[0].connected)
        old_port = self.server.port
        self.server.stop()
        wait_for(lambda: not self.engineer.connected)
        self.fleet.reset()
        self.server = RelayServer(self.root / 'server', self.bootstrap, host='127.0.0.1', port=old_port)
        self.server.start()
        self.assertTrue(self.engineer.wait_ready())
        time.sleep(.4)
        self.assertFalse(self.fleet.snapshots()[0].has_data)
        self.drive({1: pilot}, lambda: self.fleet.snapshots()[0].connected)

    def test_ws_is_local_only_and_wss_uses_certificate_and_hostname_verification(self):
        with self.assertRaises(RelayProtocolError):
            RelayEngineerSession('ws://192.168.1.1:8766/relay/v1', None, self.root, self.fleet)
        with patch('racecontrol.relay_client.connect', side_effect=OSError()) as connect_mock:
            with self.assertRaises(OSError):
                _open('wss://relay.example/relay/v1', threading.Event())
        options = connect_mock.call_args.kwargs
        self.assertEqual(options['ssl'].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(options['ssl'].check_hostname)
        self.assertIsNone(options['compression'])
        self.assertEqual(options['max_size'], 32768)

    def test_wss_rejects_unknown_certificate_then_accepts_explicitly_trusted_test_ca(self):
        self.engineer.stop()
        self.server.stop()
        tls, certificate = test_certificate(self.root)
        self.server = RelayServer(self.root / 'tls-server', self.bootstrap, host='127.0.0.1', port=0, ssl_context=tls)
        self.server.start()
        url = f'wss://127.0.0.1:{self.server.port}/relay/v1'
        # Le chemin normal refuse un certificat inconnu sans envoyer le bootstrap.
        with self.assertRaises(ssl.SSLCertVerificationError):
            _open(url, threading.Event())
        create_default_context = ssl.create_default_context
        def trusted_context():
            # Comme en production, chaque connexion reçoit son propre contexte.
            # Le mock précédent partageait un SSLContext déjà utilisé alors que
            # _open règle sa version minimale pour chaque nouvelle connexion.
            context = create_default_context(cafile=str(certificate))
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            return context
        with patch('racecontrol.relay_client.ssl.create_default_context', side_effect=trusted_context):
            engineer = RelayEngineerSession(url, self.bootstrap, self.root / 'tls-engineer', self.fleet)
            self.engineers.append(engineer)
            engineer.start()
            self.assertTrue(engineer.wait_ready())
            negotiated = engineer._channel.connection.socket.version()
            self.assertEqual(negotiated, 'TLSv1.3' if ssl.HAS_TLSv1_3 else 'TLSv1.2')
            result = check_relay_invitation(engineer.invitation(1))
            self.assertTrue(result.ok, f'{result!r}; engineer_connected={engineer.connected}; '
                                      f'accept_alive={self.server._accept_thread.is_alive()}')
            pilot, statuses = self.pilot(1, invitation=engineer.invitation(1))
            self.drive({1: pilot}, lambda: self.fleet.snapshots()[0].connected)
            self.assertEqual(self.fleet.snapshots()[0].speed_kmh, 50.)
            self.assertFalse(any('key' in value or 'bootstrap' in value for value in statuses))

    def test_blocked_dns_stop_start_cannot_duplicate_worker_or_send_late_auth(self):
        invitation = self.engineer.invitation(1)
        pilot = RelayPilotLink(invitation)
        self.pilots.append(pilot)
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        connection = Mock()
        connection.close.side_effect = closed.set
        def blocked_connect(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=3)
            return connection
        with patch('racecontrol.relay_client.connect', side_effect=blocked_connect) as connect_mock, \
                patch('racecontrol.relay_client.STOP_TIMEOUT_S', .05):
            pilot.start()
            self.assertTrue(entered.wait(timeout=1))
            worker = pilot._worker
            pilot.stop()
            self.assertIs(pilot._worker, worker)
            pilot.start()
            self.assertIs(pilot._worker, worker)
            self.assertEqual(connect_mock.call_count, 1)
            release.set()
            self.assertTrue(closed.wait(timeout=1))
            worker.join(timeout=1)
            connection.send.assert_not_called()

    def test_check_deadline_never_sends_hello_after_delayed_dns(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        connection = Mock()
        connection.close.side_effect = closed.set
        def blocked_connect(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=3)
            return connection
        with patch('racecontrol.relay_client.connect', side_effect=blocked_connect), \
                patch('racecontrol.relay_client.CHECK_TIMEOUT_S', .05):
            try:
                result = check_relay_invitation(self.engineer.invitation(1))
                self.assertTrue(entered.is_set())
                self.assertEqual(result.code, 'unreachable')
            finally:
                release.set()
                self.assertTrue(closed.wait(timeout=1))
        connection.send.assert_not_called()

    def test_blocked_writer_has_deadline_and_no_second_sender(self):
        stop, entered, release = threading.Event(), threading.Event(), threading.Event()
        connection = Mock()
        def blocked_send(_):
            entered.set()
            release.wait(timeout=3)
        connection.send.side_effect = blocked_send
        channel = _Channel(connection, stop)
        with patch('racecontrol.relay_client.SEND_TIMEOUT_S', .05):
            try:
                with self.assertRaises(RelayClientError):
                    channel.send({'v': 1, 'op': 'challenge', 'challenge': uuid.uuid4().hex})
                self.assertTrue(entered.is_set())
                self.assertEqual(connection.send.call_count, 1)
            finally:
                release.set()
                channel.close()

    def test_malformed_error_is_refused_without_echoing_server_text(self):
        with self.assertRaises(RelayProtocolError):
            _frame(json.dumps({'v': 1, 'op': 'error', 'code': [], 'message': new_secret()}))
        result = check_relay_invitation('not-an-invitation')
        self.assertEqual(result.code, 'invalid_invitation')
        self.assertIsNone(result.car_number)


if __name__ == '__main__':
    unittest.main()
