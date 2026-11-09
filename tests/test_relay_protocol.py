"""Protocole et chiffrement relais, sans réseau ni télémétrie matérielle."""
from __future__ import annotations

import base64
from dataclasses import FrozenInstanceError
import json
from types import SimpleNamespace
import unittest
from uuid import uuid4

from racecontrol.relay_protocol import (
    MAX_CIPHERTEXT_BYTES, MAX_FRAME_BYTES, MAX_SEQUENCE,
    RelayInvitation, RelayProtocolError,
    decrypt_sample, dumps_frame, encrypt_sample, loads_frame,
    make_invitation, new_secret, parse_invitation,
    validate_car, validate_id, validate_sample_frame, validate_secret, validate_url,
)


def b64(value):
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def unb64(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


class RelayProtocolTests(unittest.TestCase):
    def setUp(self):
        self.url = 'wss://relay.example.org/relay/v1'
        self.room = uuid4().hex
        self.session = uuid4().hex
        self.challenge = uuid4().hex
        self.key = new_secret()
        self.token = new_secret()
        self.telemetry = {
            'speed_kmh': 187.25, 'rpm': 6543.0, 'current_gear': 4,
            'fuel_level': 73.125, 'fuel_capacity': 100.0,
            'current_lap': 3, 'total_laps': 20,
            'last_lap_ms': 91000, 'best_lap_ms': 0xFFFFFFFF,
            'throttle_percent': 82.0, 'brake_percent': 0.0,
            'position_x': -123.5, 'position_z': 876.25,
            'tyre_temperatures': (63.0, 64.0, 65.0, 66.0), 'car_id': 42,
        }

    def frame(self, **overrides):
        options = dict(room=self.room, car=2, key=self.key, session=self.session,
                       sequence=1, challenge=self.challenge, telemetry=self.telemetry, age_ms=12.5)
        options.update(overrides)
        return encrypt_sample(**options)

    def test_invitation_round_trip_and_secrets_hidden_in_repr(self):
        encoded = make_invitation(self.url, self.room, 2, self.token, self.key)
        value = parse_invitation('  ' + encoded + '\n')
        self.assertIsInstance(value, RelayInvitation)
        self.assertEqual((value.url, value.room, value.car, value.token, value.key),
                         (self.url, self.room, 2, self.token, self.key))
        self.assertNotIn(self.key, repr(value))
        self.assertNotIn(self.token, repr(value))
        with self.assertRaises(FrozenInstanceError):
            value.car = 3

    def test_secret_is_32_random_bytes_and_canonical_unpadded(self):
        secrets = {new_secret() for _ in range(30)}
        self.assertEqual(len(secrets), 30)
        for secret in secrets:
            self.assertEqual(validate_secret(secret), secret)
            self.assertEqual(len(secret), 43)
            self.assertEqual(len(unb64(secret)), 32)
            self.assertNotIn('=', secret)
        # Same decoded length, but nonzero unused base64 bits are not canonical.
        for secret in ['', 'x' * 43, self.key + '=', b64(b'x' * 31), b64(b'x' * 33), None, 123]:
            with self.subTest(secret_type=type(secret)), self.assertRaises(RelayProtocolError):
                validate_secret(secret)

    def test_ids_and_car_slots_reject_bool_and_wrong_forms(self):
        self.assertEqual(validate_id(self.room), self.room)
        self.assertEqual(validate_car(4), 4)
        for value in [self.room.upper(), 'a' * 31, 'a' * 33, '../' + 'a' * 29, None, 1]:
            with self.subTest(id=value), self.assertRaises(RelayProtocolError):
                validate_id(value)
        for value in [True, False, 0, 5, 1.0, '1', None]:
            with self.subTest(car=value), self.assertRaises(RelayProtocolError):
                validate_car(value)

    def test_wss_and_explicit_loopback_ws_only(self):
        for url in [self.url, 'wss://relay.example.org:443/relay/v1',
                    'ws://127.0.0.1:8766/relay/v1', 'ws://127.0.0.1/relay/v1',
                    'ws://[::1]:8766/relay/v1', 'wss://[2001:db8::1]:443/relay/v1']:
            with self.subTest(url=url):
                self.assertEqual(validate_url(url), url)
        for url in [
            'ws://relay.example.org/relay/v1', 'ws://192.168.0.1/relay/v1',
            'ws://localhost/relay/v1', 'ws://0.0.0.0/relay/v1', 'ws://127.0.0.2/relay/v1',
            'https://relay.example.org/relay/v1', 'wss://relay.example.org/relay/v1/',
            'wss://relay.example.org/relay/v1?', 'wss://relay.example.org/relay/v1#',
            f'wss://user:{self.token}@relay.example.org/relay/v1',
            f'wss://relay.example.org/relay/v1?token={self.token}',
            'wss://relay.example.org:65536/relay/v1', 'wss://relay.example.org:0/relay/v1',
            'wss://relay.example.org:/relay/v1', 'wss://relay.example.org:/relay%2fv1',
            '\nwss://relay.example.org/relay/v1', 'wss://relay.example.org/relay/v1 ',
            'wss://relay..example.org/relay/v1', 'wss://-relay.example.org/relay/v1',
            'ws://[::1%25zone]:8766/relay/v1', None,
        ]:
            with self.subTest(url_type=type(url)), self.assertRaises(RelayProtocolError) as failure:
                validate_url(url)
            self.assertNotIn(self.token, str(failure.exception))

    def test_round_trip_preserves_gt7_whitelist_and_age(self):
        frame = self.frame(telemetry=SimpleNamespace(**self.telemetry))
        self.assertNotIn('car', frame)
        routed = {**frame, 'car': 2}
        value, age = decrypt_sample(self.room, 2, self.key, loads_frame(dumps_frame(routed)))
        self.assertEqual(vars(value), self.telemetry)
        self.assertEqual(age, 12.5)
        exposed = dumps_frame(routed)
        for private in [self.key, self.token, 'fuel_level', '73.125', 'speed_kmh']:
            self.assertNotIn(private, exposed)

    def test_payload_bit_flip_is_not_authenticated(self):
        frame = self.frame()
        payload = bytearray(unb64(frame['payload']))
        payload[3] ^= 1
        frame['payload'] = b64(payload)
        with self.assertRaises(RelayProtocolError):
            decrypt_sample(self.room, 2, self.key, frame)

    def test_authentication_binds_room_car_key_session_sequence_and_challenge(self):
        frame = self.frame()
        cases = [
            (uuid4().hex, 2, self.key, frame),
            (self.room, 3, self.key, frame),
            (self.room, 2, new_secret(), frame),
            (self.room, 2, self.key, {**frame, 'session': uuid4().hex}),
            (self.room, 2, self.key, {**frame, 'sequence': 2}),
            (self.room, 2, self.key, {**frame, 'challenge': uuid4().hex}),
            (self.room, 2, self.key, {**frame, 'car': 3}),
        ]
        for room, car, key, changed in cases:
            with self.subTest(car=car, changed_fields=tuple(changed)), self.assertRaises(RelayProtocolError) as failure:
                decrypt_sample(room, car, key, changed)
            self.assertNotIn(self.key, str(failure.exception))
            self.assertNotIn(key, str(failure.exception))

    def test_nonce_uses_upper_sequence_bits_and_sessions_derive_separate_keys(self):
        first = self.frame()
        high_sequence = self.frame(sequence=2**32 + 1)
        other_session = self.frame(session=uuid4().hex)
        # Compare ciphertext body, not just the tag: changed AAD alone would
        # change the tag even if a buggy implementation reused key and nonce.
        body = unb64(first['payload'])[:-16]
        self.assertNotEqual(body, unb64(high_sequence['payload'])[:-16])
        self.assertNotEqual(body, unb64(other_session['payload'])[:-16])
        for frame in [high_sequence, other_session, self.frame(sequence=MAX_SEQUENCE)]:
            self.assertEqual(decrypt_sample(self.room, 2, self.key, frame)[0].speed_kmh, 187.25)

    def test_challenge_changes_authentication_without_reusing_it_as_nonce(self):
        first = self.frame()
        second = self.frame(challenge=uuid4().hex)
        self.assertNotEqual(first['payload'], second['payload'])
        self.assertEqual(unb64(first['payload'])[:-16], unb64(second['payload'])[:-16])
        self.assertEqual(decrypt_sample(self.room, 2, self.key, second)[1], 12.5)

    def test_opaque_server_validation_does_not_require_a_decryption_key(self):
        opaque = self.frame()
        opaque['payload'] = b64(b'not a genuine encrypted payload')
        self.assertEqual(validate_sample_frame(opaque), opaque)
        self.assertEqual(validate_sample_frame({**opaque, 'car': 4}, with_car=True)['car'], 4)
        with self.assertRaises(RelayProtocolError):
            decrypt_sample(self.room, 2, self.key, opaque)

    def test_invalid_frame_fields_or_ciphertext_are_rejected(self):
        valid = self.frame()
        invalid = [
            {**valid, 'v': 2}, {**valid, 'v': True}, {**valid, 'op': 'challenge'},
            {**valid, 'sequence': True}, {**valid, 'sequence': 0},
            {**valid, 'sequence': MAX_SEQUENCE + 1}, {**valid, 'sequence': 1.0},
            {**valid, 'challenge': 'not-an-id'}, {**valid, 'session': self.session.upper()},
            {**valid, 'payload': valid['payload'] + '='},
            {**valid, 'payload': b64(b'x' * 15)},
            {**valid, 'payload': b64(b'x' * (MAX_CIPHERTEXT_BYTES + 1))},
            {**valid, 'payload': []}, {**valid, 'extra': self.key},
            {**valid, 'car': 1}, {}, None,
        ]
        for frame in invalid:
            with self.subTest(type=type(frame)), self.assertRaises(RelayProtocolError):
                validate_sample_frame(frame)
        with self.assertRaises(RelayProtocolError):
            validate_sample_frame({**valid, 'car': True}, with_car=True)

    def test_measurement_and_age_validation_is_the_same_as_direct_transport(self):
        for telemetry in [
            {}, {'speed_kmh': True}, {'speed_kmh': float('nan')},
            {'speed_kmh': float('inf')}, {'speed_kmh': 1000},
            {'speed_kmh': 10, 'fuel_level': -1},
            {'speed_kmh': 10, 'tyre_temperatures': [1, 2]},
            {'speed_kmh': 10, 'private_key': self.key},
        ]:
            with self.subTest(fields=tuple(telemetry)), self.assertRaises(RelayProtocolError) as failure:
                self.frame(telemetry=telemetry)
            self.assertNotIn(self.key, str(failure.exception))
        for age in [-1, 1001, float('nan'), float('inf'), True, '0', None, 10**500]:
            with self.subTest(age_type=type(age)), self.assertRaises(RelayProtocolError):
                self.frame(age_ms=age)
        for sequence in [0, -1, True, '1', MAX_SEQUENCE + 1]:
            with self.subTest(sequence=sequence), self.assertRaises(RelayProtocolError):
                self.frame(sequence=sequence)
        self.assertEqual(decrypt_sample(self.room, 2, self.key, self.frame(age_ms=1000))[1], 1000)

    def test_json_is_strict_and_bounded_by_utf8_bytes(self):
        for text in [
            '[]', 'null', '123', '{"v":1,"v":1}', '{"x":{"token":1,"token":2}}',
            '{"x":NaN}', '{"x":Infinity}', '{"x":1e9999}', '{broken}',
            '{"x":"\\ud800"}', b'{"v":1}',
        ]:
            with self.subTest(text_type=type(text)), self.assertRaises(RelayProtocolError):
                loads_frame(text)
        for value in [{'x': float('nan')}, {'x': float('inf')}, {1: 'value'}, {'x': object()}]:
            with self.subTest(type=type(value)), self.assertRaises(RelayProtocolError):
                dumps_frame(value)
        exact = {'x': 'a' * (MAX_FRAME_BYTES - 8)}
        encoded = dumps_frame(exact)
        self.assertEqual(len(encoded.encode('utf-8')), MAX_FRAME_BYTES)
        self.assertEqual(loads_frame(encoded), exact)
        with self.assertRaises(RelayProtocolError):
            dumps_frame({'x': 'a' * (MAX_FRAME_BYTES - 7)})
        for operation in [lambda: dumps_frame({'x': 'é' * 20000}),
                          lambda: loads_frame('{"x":"' + 'é' * 20000 + '"}')]:
            with self.assertRaises(RelayProtocolError):
                operation()
        recursive = {}
        recursive['x'] = recursive
        with self.assertRaises(RelayProtocolError):
            dumps_frame(recursive)

    def test_malformed_invitations_do_not_echo_token_or_key(self):
        data = {'v': 1, 'url': self.url, 'room': self.room, 'car': 2,
                'token': self.token, 'key': self.key}
        malformed = [self.key, None, 'gt7relay://' + self.key + '=', 'gt7rc://' + self.token]
        for change in [{'v': True}, {'v': 2}, {'car': True}, {'extra': self.key},
                       {'url': 'ws://relay.example.org/relay/v1'}, {'token': self.token + '='}]:
            modified = {**data, **change}
            malformed.append('gt7relay://' + b64(json.dumps(modified).encode('utf-8')))
        duplicate = json.dumps(data)[:-1] + ',"key":' + json.dumps(self.key) + '}'
        malformed.append('gt7relay://' + b64(duplicate.encode('utf-8')))
        for invitation in malformed:
            with self.subTest(type=type(invitation)), self.assertRaises(RelayProtocolError) as failure:
                parse_invitation(invitation)
            self.assertNotIn(self.token, str(failure.exception))
            self.assertNotIn(self.key, str(failure.exception))


if __name__ == '__main__':
    unittest.main()
