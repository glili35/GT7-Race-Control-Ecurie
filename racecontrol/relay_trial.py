"""Essai PS5 → pilote → relais WebSocket → ingénieur sur un seul ordinateur.

Le mur ingénieur reçoit exclusivement les données déchiffrées par son client
relais : aucune copie directe depuis la capture n'est effectuée. Le relais et
l'identité ingénieur sont éphémères, dans un dossier privé de cet essai. Les CSV
restent dans le profil utilisateur habituel et les invitations sauvegardées ne
sont jamais modifiées. Aucun générateur de données n'est présent dans ce module.
"""
from __future__ import annotations

from pathlib import Path
import secrets
import tempfile
import threading
from typing import Callable

from .fleet import FleetMonitor
from .relay_client import RelayEngineerSession
from .relay_server import RelayServer
from .runtime import PilotSession


class RelayLocalTrial:
    """Cycle de vie d'un essai relais local, à exécuter hors du thread Tk."""

    READY_TIMEOUT = 6.0

    def __init__(self, engineer_fleet: FleetMonitor, data_dir: Path,
                 car_number: int = 1, on_status: Callable[[str], None] | None = None):
        if type(car_number) is not int or not 1 <= car_number <= 4:
            raise ValueError("Le numéro de voiture doit être compris entre 1 et 4.")
        self.engineer_fleet = engineer_fleet
        self.data_dir = Path(data_dir)
        self.car_number = car_number
        self.on_status = on_status
        self.local_fleet = FleetMonitor(timeout_s=engineer_fleet.timeout_s)
        # ``hub`` conserve le nom employé par le contrôleur d'essai direct.
        self.hub: RelayServer | None = None
        self.engineer: RelayEngineerSession | None = None
        self.pilot: PilotSession | None = None
        self._tempdir: tempfile.TemporaryDirectory | None = None
        self._lock = threading.RLock()
        self._running = False

    @property
    def running(self) -> bool:
        # Un callback de statut peut lire cet état pendant que stop() attend
        # son worker ; ne pas prendre ici le verrou de cycle de vie.
        return self._running

    def _emit(self, message: str) -> None:
        if self.on_status is not None:
            try:
                self.on_status(message)
            except Exception:
                pass

    def start(self, ps5_ip: str | None, record: bool = True) -> None:
        """Ouvre un relais privé loopback, son ingénieur, puis une capture PS5.

        Une nouvelle salle est créée dans le profil éphémère à chaque essai,
        sans remplir la limite des salles d'un relais distant. ``None`` conserve
        la découverte locale existante. Les données ne sont pas simulées.
        """
        with self._lock:
            if (self._running or self.hub is not None or self.engineer is not None
                    or self.pilot is not None or self._tempdir is not None):
                raise RuntimeError("Un essai relais local est déjà en cours.")
            self.engineer_fleet.reset()
            self.local_fleet.reset()
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                self._tempdir = tempfile.TemporaryDirectory(prefix="essai-relais-", dir=self.data_dir)
                workspace = Path(self._tempdir.name)
                bootstrap = secrets.token_urlsafe(32)
                self.hub = RelayServer(workspace / "server", bootstrap,
                                       host="127.0.0.1", port=0)
                self.hub.start()
                url = f"ws://127.0.0.1:{self.hub.port}/relay/v1"
                self.engineer = RelayEngineerSession(
                    url, bootstrap, workspace / "engineer", self.engineer_fleet,
                    on_status=self._emit,
                )
                self.engineer.start()
                if not self.engineer.wait_ready(timeout=self.READY_TIMEOUT):
                    raise TimeoutError("Le poste ingénieur n'a pas pu rejoindre le relais local dans le délai prévu.")
                invitation = self.engineer.invitation(self.car_number)
                self.pilot = PilotSession(self.local_fleet, self.data_dir,
                                          car_number=self.car_number, on_status=self._emit)
                self.pilot.start(ps5_ip, invitation=invitation, record=record)
                self._running = True
            except Exception:
                # Tenter chaque fermeture sans masquer l'erreur de démarrage.
                self._cleanup(raise_errors=False)
                raise
            self._emit("Essai relais ouvert : PS5 réelle → pilote → relais local → ingénieur.")

    def _cleanup(self, *, raise_errors: bool) -> None:
        self._running = False
        pilot, engineer, hub = self.pilot, self.engineer, self.hub
        failures = []
        try:
            # L'ordre garantit que le CSV est fermé dans son dossier persistant
            # avant d'enlever les identités du relais et de son ingénieur.
            for component in (pilot, engineer, hub):
                if component is not None:
                    try:
                        component.stop()
                    except Exception as exc:
                        failures.append(exc)
        finally:
            self.pilot = None
            self.engineer = None
            self.hub = None
            self.local_fleet.reset()
            self.engineer_fleet.reset()
            if self._tempdir is not None:
                try:
                    self._tempdir.cleanup()
                except Exception as exc:
                    # Conserver la référence permet à stop() de réessayer ; ne
                    # jamais supprimer d'autres fichiers du profil utilisateur.
                    failures.append(exc)
                else:
                    self._tempdir = None
        if failures:
            self._emit("L'arrêt de l'essai relais local a rencontré une erreur.")
            if raise_errors:
                raise RuntimeError("L'essai relais local n'a pas pu être arrêté complètement.") from failures[0]

    def stop(self) -> None:
        """Ferme capture, ingénieur, relais, puis seulement leur dossier privé."""
        with self._lock:
            self._cleanup(raise_errors=True)
