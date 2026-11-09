"""Tests du vrai transport HTTPS local, sans console ni données PS5 inventées."""
from __future__ import annotations

import base64
import http.client
import json
from pathlib import Path
import ssl
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from racecontrol.transport import (
    EngineerHub, PilotLink, MAX_BODY, invitation_details, _parse_invitation,
)


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError('Résultat réseau non obtenu avant expiration du délai')


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.events = []
        self.lock = threading.Lock()
        self.links = []
        self.hub = EngineerHub(self.receive, Path(self.directory.name), host='127.0.0.1', port=0)
        self.hub.start()

    def receive(self, number, telemetry):
        with self.lock:
            self.events.append((number, telemetry))

    def tearDown(self):
        for link in self.links:
            link.stop()
        self.hub.stop()
        self.directory.cleanup()

    def link(self, invitation=None, statuses=None):
        value = PilotLink(invitation or self.hub.invitation(1, '127.0.0.1'),
                          on_status=None if statuses is None else statuses.append)
        self.links.append(value)
        value.start()
        return value

    def post(self, payload=None, *, car=1, token_car=1, raw=None, length=None):
        invitation = _parse_invitation(self.hub.invitation(token_car, '127.0.0.1'))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # Tests de requêtes volontairement malformées.
        connection = http.client.HTTPSConnection('127.0.0.1', self.hub.port, context=context, timeout=2)
        if raw is None:
            envelope = {'v': 1, 'session': uuid.uuid4().hex, 'sequence': 1,
                        'age_ms': 0, 'telemetry': {'speed_kmh': 123.0}}
            envelope.update(payload or {})
            raw = json.dumps(envelope).encode()
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + invitation['token']}
        if length is not None:
            headers['Content-Length'] = str(length)
        try:
            connection.request('POST', f'/v1/telemetry/{car}', body=raw, headers=headers)
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    def test_four_real_tls_clients_have_independent_car_slots(self):
        for number in range(1, 5):
            link = self.link(self.hub.invitation(number, '127.0.0.1'))
            link.publish(SimpleNamespace(speed_kmh=number * 40.0, fuel_level=number * 10.,
                                         fuel_capacity=100., tyre_temperatures=(65., 66., 67., 68.),
                                         car_id=100 + number))
        wait_for(lambda: len(self.events) == 4)
        by_car = {n: t for n, t in self.events}
        self.assertEqual(set(by_car), {1, 2, 3, 4})
        for number, telemetry in by_car.items():
            self.assertEqual(telemetry.speed_kmh, number * 40)
            self.assertEqual(telemetry.fuel_level, number * 10)
            self.assertEqual(telemetry.car_id, 100 + number)
            self.assertEqual(telemetry.tyre_temperatures, (65., 66., 67., 68.))

    def test_one_invitation_cannot_publish_another_car(self):
        self.assertEqual(self.post(car=2, token_car=1), 401)
        self.assertFalse(self.events)
        self.assertEqual(self.post(car=2, token_car=2), 204)
        self.assertEqual(self.events[0][0], 2)

    def test_wrong_certificate_pin_sends_no_http_request_or_token(self):
        data = _parse_invitation(self.hub.invitation(1, '127.0.0.1'))
        data['fingerprint'] = '0' * 64
        invitation = 'gt7rc://' + base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
        statuses = []
        with patch.object(http.client.HTTPSConnection, 'request') as request:
            link = self.link(invitation, statuses)
            link.publish(SimpleNamespace(speed_kmh=150))
            wait_for(lambda: any('Certificat' in x for x in statuses))
            request.assert_not_called()
        self.assertFalse(self.events)
        self.assertTrue(all(data['token'] not in x for x in statuses))

    def test_revoking_one_invitation_keeps_other_cars_valid(self):
        statuses = []
        old_invitation = self.hub.invitation(1, '127.0.0.1')
        self.hub.rotate_token(1)
        link = self.link(old_invitation, statuses)
        link.publish(SimpleNamespace(speed_kmh=90))
        wait_for(lambda: any('refusée' in x for x in statuses))
        self.assertFalse(self.events)
        self.assertEqual(self.post(car=2, token_car=2), 204)
        self.assertEqual(self.post(car=1, token_car=1), 204)
        self.assertEqual({n for n, _ in self.events}, {1, 2})

    def test_identity_and_car_invitations_persist_across_restart(self):
        invitation = self.hub.invitation(4, '127.0.0.1')
        previous_port = self.hub.port
        self.hub.stop()
        self.hub = EngineerHub(self.receive, Path(self.directory.name), host='127.0.0.1', port=previous_port)
        self.hub.start()
        self.assertEqual(self.hub.invitation(4, '127.0.0.1'), invitation)
        self.assertEqual(invitation_details(invitation), ('127.0.0.1', previous_port, 4))

    def test_invalid_protocol_payloads_do_not_reach_callback(self):
        for payload in [
            {'v': 2}, {'v': True}, {'age_ms': 1001}, {'age_ms': -1}, {'age_ms': 10**400},
            {'sequence': 0}, {'sequence': True}, {'session': '../../session'},
            {'telemetry': {'speed_kmh': float('nan')}},
            {'telemetry': {'speed_kmh': float('inf')}},
            {'telemetry': {'speed_kmh': True}},
            {'telemetry': {'speed_kmh': 900}},
            {'telemetry': {'speed_kmh': 10**400}},
            {'telemetry': {'speed_kmh': 100, 'fuel_level': -1}},
            {'telemetry': {'speed_kmh': 100, 'tyre_temperatures': [20, 20]}},
            {'telemetry': {'speed_kmh': 100, 'name': 'untrusted'}},
            {'telemetry': {}},
        ]:
            with self.subTest(payload=payload):
                self.assertEqual(self.post(payload), 400)
        self.assertEqual(self.post(raw=b'not json'), 400)
        self.assertEqual(self.post(raw=b'{"v":1,"v":1}'), 400)
        self.assertFalse(self.events)

    def test_oversized_request_is_rejected_before_body_read(self):
        self.assertEqual(self.post(raw=b'', length=MAX_BODY + 1), 413)
        self.assertFalse(self.events)

    def test_duplicate_sample_does_not_refresh_signal(self):
        payload = {'session': uuid.uuid4().hex, 'sequence': 42}
        self.assertEqual(self.post(payload), 204)
        self.assertEqual(self.post(payload), 409)
        self.assertEqual(self.post({**payload, 'sequence': 41}), 409)
        self.assertEqual(len(self.events), 1)

    def test_worker_sends_only_fresh_samples_and_recovers_on_new_data(self):
        statuses = []
        link = self.link(statuses=statuses)
        link.publish(SimpleNamespace(speed_kmh=100))
        wait_for(lambda: len(self.events) == 1)
        time.sleep(0.35)
        self.assertEqual(len(self.events), 1, 'Un paquet unique ne doit pas être republie comme frais')
        previous_port = self.hub.port
        self.hub.stop()
        link.publish(SimpleNamespace(speed_kmh=111))
        wait_for(lambda: any('injoignable' in x for x in statuses))
        self.hub = EngineerHub(self.receive, Path(self.directory.name), host='127.0.0.1', port=previous_port)
        self.hub.start()
        time.sleep(0.35)
        self.assertEqual(len(self.events), 1, 'Ne pas rejouer le paquet ancien lors de la reconnexion')
        link.publish(SimpleNamespace(speed_kmh=222))
        wait_for(lambda: len(self.events) == 2)
        self.assertEqual(self.events[-1][1].speed_kmh, 222)

    def test_handshake_delay_discards_expired_sample(self):
        statuses = []
        link = self.link(statuses=statuses)
        real_connect = link._connect
        def slow_connect():
            time.sleep(1.05)
            return real_connect()
        with patch.object(link, '_connect', slow_connect):
            link.publish(SimpleNamespace(speed_kmh=180))
            wait_for(lambda: any('récentes' in x for x in statuses))
        self.assertFalse(self.events)

    def test_stop_closes_partial_requests_and_prevents_late_ingestion(self):
        data = _parse_invitation(self.hub.invitation(1, '127.0.0.1'))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection('127.0.0.1', self.hub.port, context=context, timeout=2)
        body = json.dumps({'v': 1, 'session': uuid.uuid4().hex, 'sequence': 1,
                           'age_ms': 0, 'telemetry': {'speed_kmh': 150}}).encode()
        try:
            connection.putrequest('POST', '/v1/telemetry/1')
            connection.putheader('Content-Type', 'application/json')
            connection.putheader('Authorization', 'Bearer ' + data['token'])
            connection.putheader('Content-Length', str(len(body)))
            connection.endheaders(body[:10])
            time.sleep(0.05)
            self.hub.stop()
            try:
                connection.send(body[10:])
                response = connection.getresponse()
                self.assertNotEqual(response.status, 204)
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.05)
            self.assertFalse(self.events)
        finally:
            connection.close()

    def test_publish_is_nonblocking_and_latest_value_wins(self):
        link = self.link()
        for i in range(300):
            link.publish(SimpleNamespace(speed_kmh=float(i)))
        wait_for(lambda: any(t.speed_kmh == 299 for _, t in self.events))
        # La rafale de 300 messages ne crée pas une file de 300 envois.
        self.assertLess(len(self.events), 10)

    def test_stopping_a_slow_worker_cannot_start_a_second_worker(self):
        link = PilotLink(self.hub.invitation(1, '127.0.0.1'))
        self.links.append(link)
        entered, release = threading.Event(), threading.Event()
        workers = []
        def blocked_worker():
            workers.append(threading.current_thread())
            entered.set()
            release.wait(timeout=5)
        with patch.object(link, '_run', blocked_worker):
            link.start()
            self.assertTrue(entered.wait(timeout=1))
            worker = link._thread
            # Simule un DNS restant bloqué au-delà du délai de join de stop.
            with patch.object(worker, 'join', return_value=None):
                link.stop()
            self.assertIs(link._thread, worker)
            self.assertTrue(link._stop.is_set())
            link.start()
            self.assertIs(link._thread, worker)
            self.assertEqual(len(workers), 1)
            link.publish(SimpleNamespace(speed_kmh=120))
            self.assertIsNone(link._pending)
            release.set()
            worker.join(timeout=1)
        self.assertFalse(self.events)

    def test_invalid_invitation_rejected_before_any_connection(self):
        valid = _parse_invitation(self.hub.invitation(1, '127.0.0.1'))
        invalid = ['https://example.com', 'gt7rc://%%%', 'gt7rc://e30']
        for changes in [{'host': '127.0.0.1/path'}, {'port': 0}, {'car': 5}, {'car': True},
                        {'v': 2}, {'token': 'short'}, {'fingerprint': 'bad'}]:
            data = {**valid, **changes}
            invalid.append('gt7rc://' + base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('='))
        for invitation in invalid:
            with self.subTest(invitation=invitation[:20]):
                with self.assertRaises(ValueError):
                    PilotLink(invitation)


if __name__ == '__main__':
    unittest.main()
