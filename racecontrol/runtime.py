"""Capture locale d'une PS5, transmission et enregistrement sans bloquer l'UDP.

Une session contient une seule source réelle. Le serveur d'ingénieur utilise
les invitations pour regrouper quatre sessions indépendantes.
"""
from __future__ import annotations

import queue
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .telemetry import GT7Receiver, TelemetryDiagnostics
from .telemetry.csv_recorder import GT7CSVRecorder


class PilotSession:
    """Gère le cycle de vie d'une capture et de son CSV à pleine fréquence.

    ``on_status`` peut être appelé depuis un thread de fond. Une interface Tk
    doit donc passer ses messages par une file avant de modifier ses widgets.
    Les invitations ne sont jamais incluses dans les messages de diagnostic.
    """

    RECORD_QUEUE_SIZE = 4096
    WRITER_STOP_TIMEOUT = 5.0

    def __init__(
        self,
        fleet: Any,
        data_dir: Path,
        car_number: int = 1,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        if isinstance(car_number, bool) or car_number not in range(1, 5):
            raise ValueError("Le numéro de voiture doit être compris entre 1 et 4.")
        self.fleet = fleet
        self.data_dir = Path(data_dir)
        self.car_number = car_number
        self.on_status = on_status
        self.diagnostics = TelemetryDiagnostics()
        self._lifecycle_lock = threading.RLock()
        self._stats_lock = threading.Lock()
        self._accepting = threading.Event()
        self._running = False
        self._generation = 0
        self._receiver: GT7Receiver | None = None
        self._link: Any = None
        self._writer: threading.Thread | None = None
        self._writer_stop = threading.Event()
        self._queue: queue.Queue[Any] | None = None
        self._recorder: GT7CSVRecorder | None = None
        self._file_path: Path | None = None
        self._rows_written = 0
        self._rows_dropped = 0
        self._recording_error: str | None = None
        self._callback_error_reported = False

    @property
    def running(self) -> bool:
        # Ne pas prendre le verrou de cycle de vie ici : un callback de statut
        # peut lire cet état pendant que stop() attend son thread émetteur.
        return self._running

    @property
    def recording_active(self) -> bool:
        """Indique si le worker possède encore un CSV, même après stop().

        Un disque bloqué peut dépasser le délai de fermeture de stop().
        Une mise à jour doit attendre la fin réelle de ce worker avant de
        fermer l'application. Cette lecture ne bloque pas le thread graphique.
        """
        writer = self._writer
        return writer is not None and writer.is_alive()

    @property
    def rows_written(self) -> int:
        with self._stats_lock:
            return self._rows_written

    @property
    def rows_dropped(self) -> int:
        with self._stats_lock:
            return self._rows_dropped

    @property
    def file_path(self) -> Path | None:
        with self._stats_lock:
            return self._file_path

    @property
    def recording_error(self) -> str | None:
        with self._stats_lock:
            return self._recording_error

    def _emit(self, message: str) -> None:
        if self.on_status is not None:
            try:
                self.on_status(message)
            except Exception:
                # Une erreur d'affichage ne doit pas interrompre la capture.
                pass

    def start(
        self,
        ps5_ip: str | None,
        invitation: str | None = None,
        record: bool = True,
    ) -> None:
        """Démarre une capture ; ``None`` autorise la découverte UDP locale.

        À appeler hors du thread graphique : ouvrir les fichiers et démarrer
        le réseau peut prendre un peu de temps. Un deuxième appel pendant une
        session active est refusé au lieu de changer sa configuration en vol.
        """
        with self._lifecycle_lock:
            if self._running:
                raise RuntimeError("Une capture PS5 est déjà en cours.")
            if self._writer is not None and self._writer.is_alive():
                raise RuntimeError("L'enregistrement précédent est encore en cours de fermeture.")
            self._generation += 1
            generation = self._generation
            self.diagnostics = TelemetryDiagnostics()
            self._writer_stop = threading.Event()
            self._queue = None
            self._recorder = None
            self._writer = None
            self._link = None
            self._callback_error_reported = False
            with self._stats_lock:
                self._file_path = None
                self._rows_written = 0
                self._rows_dropped = 0
                self._recording_error = None
            try:
                reset = getattr(self.fleet, "reset", None)
                if reset is not None:
                    reset(self.car_number)
                if invitation:
                    try:
                        if invitation.startswith("gt7relay://"):
                            from .relay_client import RelayPilotLink

                            self._link = RelayPilotLink(invitation, on_status=self._emit)
                        else:
                            from .transport import PilotLink

                            self._link = PilotLink(invitation, on_status=self._emit)
                    except Exception:
                        raise ValueError("L'invitation du poste ingénieur est invalide.") from None
                    self._link.start()
                if record:
                    self._recorder = GT7CSVRecorder(self.data_dir / "sessions")
                    with self._stats_lock:
                        self._file_path = self._recorder.file_path
                    self._queue = queue.Queue(maxsize=self.RECORD_QUEUE_SIZE)
                    self._writer = threading.Thread(
                        target=self._write_loop,
                        args=(self._queue, self._writer_stop, self._recorder),
                        name="GT7-CSV-Writer",
                        daemon=True,
                    )
                    self._writer.start()
                self._receiver = GT7Receiver(
                    ps5_ip=ps5_ip,
                    diagnostics=self.diagnostics,
                    on_telemetry=lambda telemetry: self._on_telemetry(telemetry, generation),
                    on_status=self._emit,
                )
                self._accepting.set()
                self._running = True
                self._receiver.start()
            except Exception:
                self.stop()
                raise

    def _on_telemetry(self, telemetry: Any, generation: int) -> None:
        if not self._accepting.is_set() or generation != self._generation:
            return
        received_at = datetime.now()
        # Le décodeur fournit une dataclass immuable. On conserve sa référence
        # sans copie coûteuse et sans envoyer le CSV à travers Internet.
        try:
            self.fleet.ingest(self.car_number, telemetry)
            if self._link is not None:
                self._link.publish(telemetry)
        except Exception:
            if not self._callback_error_reported:
                self._callback_error_reported = True
                self._emit("Une mesure n'a pas pu être transmise au tableau de bord.")
        items = self._queue
        if items is not None and self.recording_error is None:
            try:
                items.put_nowait((telemetry, received_at))
            except queue.Full:
                with self._stats_lock:
                    self._rows_dropped += 1
                    dropped = self._rows_dropped
                if dropped == 1 or dropped % 600 == 0:
                    self._emit("Le disque ne suit pas : des mesures CSV ont été perdues.")

    def _write_loop(
        self,
        items: queue.Queue[Any],
        stop_event: threading.Event,
        recorder: GT7CSVRecorder,
    ) -> None:
        try:
            while not stop_event.is_set() or not items.empty():
                try:
                    telemetry, received_at = items.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    recorder.record(telemetry, timestamp=received_at)
                    with self._stats_lock:
                        self._rows_written = recorder.rows_written
                finally:
                    items.task_done()
        except Exception:
            with self._stats_lock:
                self._recording_error = "Écriture du fichier CSV impossible. Vérifie l'espace disque et les autorisations."
                self._rows_dropped += 1
            self._emit(self.recording_error)
            # La capture et la transmission restent disponibles même si le
            # disque échoue. Le CSV est explicitement signalé incomplet.
            while True:
                try:
                    items.get_nowait()
                    items.task_done()
                    with self._stats_lock:
                        self._rows_dropped += 1
                except queue.Empty:
                    break
        finally:
            try:
                recorder.close()
            except Exception:
                with self._stats_lock:
                    self._recording_error = "La fermeture du CSV a échoué ; le fichier peut être incomplet."
                self._emit(self.recording_error)

    def stop(self) -> None:
        """Arrête l'UDP puis vide le CSV avant de fermer la transmission.

        Le délai de fermeture du disque est borné. S'il est dépassé, le thread
        conserve la propriété de son fichier et un redémarrage est refusé
        jusqu'à sa terminaison, pour éviter de mélanger deux sessions.
        """
        with self._lifecycle_lock:
            self._accepting.clear()
            self._running = False
            self._generation += 1
            receiver, self._receiver = self._receiver, None
            link, self._link = self._link, None
            if receiver is not None:
                try:
                    receiver.stop()
                except Exception:
                    self._emit("Le récepteur PS5 n'a pas pu être arrêté normalement.")
            self._writer_stop.set()
            writer = self._writer
            if writer is not None and writer.ident is not None and writer is not threading.current_thread():
                writer.join(timeout=self.WRITER_STOP_TIMEOUT)
                if writer.is_alive():
                    self._emit("Le disque tarde à terminer le CSV. Patiente avant de relancer une capture.")
            elif self._recorder is not None and (writer is None or writer.ident is None):
                # File created but thread start failed.
                try:
                    self._recorder.close()
                except Exception:
                    self._emit("La fermeture du CSV a échoué.")
            if link is not None:
                try:
                    link.stop()
                except Exception:
                    self._emit("La liaison ingénieur n'a pas pu être arrêtée normalement.")
            self._queue = None
            self._recorder = None
