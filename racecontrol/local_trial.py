"""Essai réel PS5 → pilote → HTTPS → ingénieur sur un seul ordinateur.

Le tableau de bord ingénieur ne reçoit aucune copie directe de la capture.
Il reçoit exclusivement les requêtes HTTPS de PilotSession, exactement comme
sur deux postes. Seule l'adresse du serveur est forcée à la boucle locale.
Ce module n'invente aucune télémétrie et ne modifie aucune invitation sauvegardée.
"""
from __future__ import annotations

from pathlib import Path
import threading
from typing import Callable

from .fleet import FleetMonitor
from .runtime import PilotSession
from .transport import EngineerHub


class LocalTrial:
    """Cycle de vie d'un essai local, à démarrer/arrêter hors du thread Tk."""

    def __init__(
        self,
        engineer_fleet: FleetMonitor,
        data_dir: Path,
        car_number: int = 1,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        if type(car_number) is not int or not 1 <= car_number <= 4:
            raise ValueError("Le numéro de voiture doit être compris entre 1 et 4.")
        self.engineer_fleet = engineer_fleet
        self.data_dir = Path(data_dir)
        self.car_number = car_number
        self.on_status = on_status
        self.local_fleet = FleetMonitor(timeout_s=engineer_fleet.timeout_s)
        self.hub: EngineerHub | None = None
        self.pilot: PilotSession | None = None
        self._lock = threading.RLock()
        self._running = False

    @property
    def running(self) -> bool:
        # Les callbacks de statut peuvent lire cet état pendant stop(), qui
        # attend ses workers. Ne pas prendre ici le verrou de cycle de vie.
        return self._running

    def _emit(self, message: str) -> None:
        if self.on_status is not None:
            try:
                self.on_status(message)
            except Exception:
                pass

    def start(self, ps5_ip: str | None, record: bool = True) -> None:
        """Écoute une vraie PS5 et transmet au serveur HTTPS local privé.

        ``None`` conserve la découverte locale du récepteur existant. Les
        fichiers CSV restent dans le dossier utilisateur habituel des sessions.
        Une seconde capture active n'est jamais créée par cet objet.
        """
        with self._lock:
            if self._running or self.hub is not None or self.pilot is not None:
                raise RuntimeError("Un essai local est déjà en cours.")
            self.engineer_fleet.reset()
            self.local_fleet.reset()
            try:
                self.hub = EngineerHub(
                    self.engineer_fleet.ingest,
                    self.data_dir / "essai-local",
                    host="127.0.0.1",
                    port=0,
                )
                self.hub.start()
                invitation = self.hub.invitation(self.car_number, "127.0.0.1")
                self.pilot = PilotSession(
                    self.local_fleet,
                    self.data_dir,
                    car_number=self.car_number,
                    on_status=self._emit,
                )
                self.pilot.start(ps5_ip, invitation=invitation, record=record)
                self._running = True
            except Exception:
                # Fermer également un serveur déjà ouvert si la PS5, le CSV,
                # ou la création du pilote échoue. Garder l'erreur initiale.
                self._cleanup(raise_errors=False)
                raise
            self._emit("Essai local ouvert : PS5 réelle → pilote → HTTPS local → ingénieur.")

    def _cleanup(self, *, raise_errors: bool) -> None:
        self._running = False
        pilot, hub = self.pilot, self.hub
        failures = []
        try:
            if pilot is not None:
                try:
                    # PilotSession.stop() arrête l'UDP, ferme le CSV, puis
                    # ferme son PilotLink ; le hub reste disponible jusque-là.
                    pilot.stop()
                except Exception as exc:
                    failures.append(exc)
            if hub is not None:
                try:
                    hub.stop()
                except Exception as exc:
                    failures.append(exc)
        finally:
            self.pilot = None
            self.hub = None
            self.local_fleet.reset()
            self.engineer_fleet.reset()
        if failures:
            self._emit("L'arrêt de l'essai local a rencontré une erreur.")
            if raise_errors:
                raise RuntimeError("L'essai local n'a pas pu être arrêté complètement.") from failures[0]

    def stop(self) -> None:
        """Ferme capture, liaison et serveur ; efface les valeurs arrêtées."""
        with self._lock:
            self._cleanup(raise_errors=True)
