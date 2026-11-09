"""Protocole relais : invitations, JSON borné et télémétrie chiffrée de bout en bout.

Le relais ne reçoit jamais les clés de chiffrement. Une sous-clé HKDF est propre
à la session, à la salle et à la voiture ; le nonce contient la séquence uint64.
L'émetteur doit utiliser une nouvelle session aléatoire à chaque redémarrage et
ne jamais réutiliser une séquence avec cette session. L'ingénieur applique la
fraîcheur des challenges et la protection contre le rejeu au-dessus de ce module.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import ipaddress
import json
import math
import re
import secrets
from types import SimpleNamespace
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .transport import _FIELDS, _validate_telemetry

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 32768
MAX_MESSAGE_BYTES = MAX_FRAME_BYTES
MAX_CIPHERTEXT_BYTES = 24000
MAX_SEQUENCE = 2**63 - 1
_INVITATION_PREFIX = "gt7relay://"


class RelayProtocolError(ValueError):
    """Entrée relais invalide ; les messages n'incluent jamais les secrets."""


@dataclass(frozen=True)
class RelayInvitation:
    url: str
    room: str
    car: int
    token: str = field(repr=False)
    key: str = field(repr=False)


def validate_id(value: str) -> str:
    """Vérifie la représentation d'un identifiant UUID hexadécimal minuscule."""
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise RelayProtocolError("Identifiant de relais invalide.")
    return value


def validate_car(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 4:
        raise RelayProtocolError("Numéro de voiture attendu : 1 à 4.")
    return value


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str, *, minimum: int, maximum: int) -> bytes:
    if (not isinstance(value, str) or not value
            or len(value) > (maximum * 4 + 2) // 3
            or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None):
        raise RelayProtocolError("Valeur encodée de relais invalide.")
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, UnicodeError):
        raise RelayProtocolError("Valeur encodée de relais invalide.") from None
    if not minimum <= len(decoded) <= maximum or _b64encode(decoded) != value:
        raise RelayProtocolError("Valeur encodée de relais invalide.")
    return decoded


def validate_secret(value: str) -> str:
    try:
        if not isinstance(value, str) or len(value) != 43:
            raise RelayProtocolError("Secret de relais invalide.")
        _b64decode(value, minimum=32, maximum=32)
    except RelayProtocolError:
        raise RelayProtocolError("Secret de relais invalide.") from None
    return value


def new_secret() -> str:
    return _b64encode(secrets.token_bytes(32))


def validate_url(value: str) -> str:
    """Autorise WSS, ou WS exclusivement vers 127.0.0.1 / ::1."""
    invalid = "Adresse de relais invalide : utilise WSS, ou la boucle locale pour un essai."
    if (not isinstance(value, str) or not 1 <= len(value) <= 1024
            or any(ord(char) < 33 or ord(char) == 127 for char in value)
            or any(char in value for char in "\\@?#%")):
        raise RelayProtocolError(invalid)
    try:
        parts = urlsplit(value)
        if (parts.scheme not in ("ws", "wss") or not value.startswith(parts.scheme + "://")
                or parts.username is not None or parts.password is not None
                or parts.path != "/relay/v1" or parts.query or parts.fragment
                or parts.hostname is None):
            raise ValueError()
        host = parts.hostname
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
        if parts.netloc.startswith("["):
            if re.fullmatch(r"\[[0-9A-Fa-f:]+\](?::[0-9]{1,5})?", parts.netloc) is None:
                raise ValueError()
            ipaddress.IPv6Address(host)
        else:
            if re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?", parts.netloc) is None:
                raise ValueError()
            if len(host) > 253 or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                                      for label in host.split(".")):
                raise ValueError()
        if parts.scheme == "ws" and host not in ("127.0.0.1", "::1"):
            raise ValueError()
    except (ValueError, TypeError):
        raise RelayProtocolError(invalid) from None
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RelayProtocolError("Message JSON de relais invalide.")
        result[key] = value
    return result


def _json_values(data) -> None:
    pending = [(data, 0)]
    seen = 0
    while pending:
        value, depth = pending.pop()
        seen += 1
        if depth > 16 or seen > 8192:
            raise RelayProtocolError("Message JSON de relais invalide.")
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise RelayProtocolError("Message JSON de relais invalide.")
            for key in value:
                key.encode("utf-8")
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
        elif type(value) is str:
            value.encode("utf-8")
        elif value is None or type(value) in (bool, int):
            continue
        elif type(value) is float and math.isfinite(value):
            continue
        else:
            raise RelayProtocolError("Message JSON de relais invalide.")


def dumps_frame(frame: dict) -> str:
    if not isinstance(frame, dict):
        raise RelayProtocolError("Objet JSON de relais attendu.")
    try:
        _json_values(frame)
        text = json.dumps(frame, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
        if len(text.encode("utf-8")) > MAX_FRAME_BYTES:
            raise RelayProtocolError("Message de relais trop volumineux.")
        return text
    except RelayProtocolError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise RelayProtocolError("Message JSON de relais invalide.") from None


def loads_frame(message: str) -> dict:
    if not isinstance(message, str):
        raise RelayProtocolError("Message texte de relais attendu.")
    try:
        if len(message.encode("utf-8")) > MAX_FRAME_BYTES:
            raise RelayProtocolError("Message de relais trop volumineux.")

        def reject_constant(_):
            raise RelayProtocolError("Message JSON de relais invalide.")

        frame = json.loads(message, object_pairs_hook=_unique_object, parse_constant=reject_constant)
        if not isinstance(frame, dict):
            raise RelayProtocolError("Objet JSON de relais attendu.")
        _json_values(frame)
        return frame
    except RelayProtocolError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise RelayProtocolError("Message JSON de relais invalide.") from None


def make_invitation(url: str, room: str, car: int, token: str, key: str) -> str:
    data = {"v": PROTOCOL_VERSION, "url": validate_url(url), "room": validate_id(room),
            "car": validate_car(car), "token": validate_secret(token), "key": validate_secret(key)}
    return _INVITATION_PREFIX + _b64encode(dumps_frame(data).encode("utf-8"))


def parse_invitation(invitation: str) -> RelayInvitation:
    try:
        if not isinstance(invitation, str):
            raise RelayProtocolError("Invitation de relais invalide.")
        invitation = invitation.strip()
        if not invitation.startswith(_INVITATION_PREFIX) or len(invitation) > 4096:
            raise RelayProtocolError("Invitation de relais invalide.")
        raw = _b64decode(invitation[len(_INVITATION_PREFIX):], minimum=1, maximum=3000)
        data = loads_frame(raw.decode("utf-8"))
        if (set(data) != {"v", "url", "room", "car", "token", "key"}
                or type(data["v"]) is not int or data["v"] != PROTOCOL_VERSION):
            raise RelayProtocolError("Invitation de relais invalide.")
        return RelayInvitation(validate_url(data["url"]), validate_id(data["room"]),
                               validate_car(data["car"]), validate_secret(data["token"]),
                               validate_secret(data["key"]))
    except (ValueError, TypeError, UnicodeError, KeyError, RecursionError, OverflowError):
        raise RelayProtocolError("Invitation de relais invalide ou incompatible.") from None


def _sequence(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_SEQUENCE:
        raise RelayProtocolError("Séquence de relais invalide.")
    return value


def _age(value) -> float:
    if type(value) not in (int, float) or not 0 <= value <= 1000 or not math.isfinite(value):
        raise RelayProtocolError("Âge de télémétrie invalide.")
    return float(value)


def validate_sample_frame(frame: dict, with_car: bool = False) -> dict:
    expected = {"v", "op", "session", "sequence", "challenge", "payload"}
    if with_car:
        expected.add("car")
    if (not isinstance(frame, dict) or set(frame) != expected
            or type(frame.get("v")) is not int or frame["v"] != PROTOCOL_VERSION
            or frame.get("op") != "sample"):
        raise RelayProtocolError("Trame de télémétrie relais invalide.")
    validate_id(frame["session"])
    _sequence(frame["sequence"])
    validate_id(frame["challenge"])
    if with_car:
        validate_car(frame["car"])
    _b64decode(frame["payload"], minimum=16, maximum=MAX_CIPHERTEXT_BYTES)
    dumps_frame(frame)  # Le serveur valide les bornes sans déchiffrer.
    return dict(frame)


def _context(room: str, car: int, key: str, session: str) -> bytes:
    room, car, session = validate_id(room), validate_car(car), validate_id(session)
    secret = _b64decode(validate_secret(key), minimum=32, maximum=32)
    info = dumps_frame({"v": PROTOCOL_VERSION, "room": room, "car": car,
                        "purpose": "GT7RC relay session key"}).encode("utf-8")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=bytes.fromhex(session), info=info).derive(secret)


def _aad(room: str, car: int, session: str, sequence: int, challenge: str) -> bytes:
    return dumps_frame({"v": PROTOCOL_VERSION, "room": room, "car": car,
                        "session": session, "sequence": sequence, "challenge": challenge}).encode("utf-8")


def _telemetry(data) -> dict:
    try:
        if not isinstance(data, dict):
            data = {name: value for name in (*_FIELDS, "tyre_temperatures")
                    if (value := getattr(data, name, None)) is not None}
        return _validate_telemetry(data)
    except Exception:
        raise RelayProtocolError("Télémétrie de relais invalide.") from None


def encrypt_sample(room: str, car: int, key: str, session: str, sequence: int,
                   challenge: str, telemetry, age_ms) -> dict:
    """Chiffre un échantillon ; une session/séquence ne doit jamais être réutilisée."""
    room, car = validate_id(room), validate_car(car)
    session, challenge = validate_id(session), validate_id(challenge)
    sequence = _sequence(sequence)
    age = _age(age_ms)
    body = {"telemetry": _telemetry(telemetry), "age_ms": age}
    # JSON ne connaît pas les tuples ; le validateur GT7 renvoie ceux des pneus.
    if "tyre_temperatures" in body["telemetry"]:
        body["telemetry"]["tyre_temperatures"] = list(body["telemetry"]["tyre_temperatures"])
    plaintext = dumps_frame(body).encode("utf-8")
    nonce = PROTOCOL_VERSION.to_bytes(4, "big") + sequence.to_bytes(8, "big")
    encrypted = AESGCM(_context(room, car, key, session)).encrypt(
        nonce, plaintext, _aad(room, car, session, sequence, challenge))
    return validate_sample_frame({"v": PROTOCOL_VERSION, "op": "sample", "session": session,
                                  "sequence": sequence, "challenge": challenge,
                                  "payload": _b64encode(encrypted)})


def decrypt_sample(room: str, car: int, key: str, frame: dict) -> tuple[SimpleNamespace, float]:
    """Authentifie et déchiffre ; challenges frais et rejeux se contrôlent au-dessus."""
    try:
        room, car = validate_id(room), validate_car(car)
        frame = validate_sample_frame(frame, with_car=isinstance(frame, dict) and "car" in frame)
        if frame.get("car", car) != car:
            raise RelayProtocolError("Contexte de voiture incorrect.")
        sequence, session, challenge = frame["sequence"], frame["session"], frame["challenge"]
        nonce = PROTOCOL_VERSION.to_bytes(4, "big") + sequence.to_bytes(8, "big")
        raw = AESGCM(_context(room, car, key, session)).decrypt(
            nonce, _b64decode(frame["payload"], minimum=16, maximum=MAX_CIPHERTEXT_BYTES),
            _aad(room, car, session, sequence, challenge))
        data = loads_frame(raw.decode("utf-8"))
        if set(data) != {"telemetry", "age_ms"}:
            raise RelayProtocolError("Contenu de télémétrie incorrect.")
        return SimpleNamespace(**_telemetry(data["telemetry"])), _age(data["age_ms"])
    except (ValueError, TypeError, KeyError, AttributeError, InvalidTag, UnicodeError, RecursionError, OverflowError):
        raise RelayProtocolError("Trame chiffrée invalide ou non authentifiée.") from None
