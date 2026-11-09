"""Mises à jour du canal Test, demandées explicitement depuis l'interface."""
from __future__ import annotations

from pathlib import Path
import queue
import sys
import threading
import time
from tkinter import ttk
import tkinter as tk
from typing import Callable

from . import __version__
from .update_install import InstallerFileError, launch_installer
from .updates import UpdateCancelled, UpdateClient


class UpdatePanel(ttk.Frame):
    def __init__(self, parent, *, data_dir: Path,
                 can_install: Callable[[], str | None],
                 on_installed: Callable[[], None], log: Callable[[str], None]):
        super().__init__(parent, padding=20)
        self.client = UpdateClient(current_version=__version__, data_dir=data_dir)
        self.can_install = can_install
        self.on_installed = on_installed
        self.log = log
        self.info = None
        self.downloaded = None
        self.busy = False
        self.installing = False
        self._worker = None
        self._events = queue.SimpleQueue()
        self._cancel = threading.Event()
        self._close_requested = False
        self._last_progress = 0.0
        self.status = tk.StringVar(value="Recherche à la demande · aucune installation automatique au démarrage")
        self.release = tk.StringVar(value="")
        self.progress = tk.DoubleVar(value=0)
        ttk.Label(self, text="MISES À JOUR", font=("Segoe UI", 18, "bold")).pack(anchor="w", pady=(0, 12))
        ttk.Label(self, text=f"Version installée : {__version__} · Canal Test").pack(anchor="w", pady=5)
        ttk.Label(self, text="Les prochaines versions se téléchargent ici, sans compte GitHub ni recherche de fichiers.",
                  wraplength=1000).pack(anchor="w", pady=6)
        row = ttk.Frame(self)
        row.pack(fill="x", pady=18)
        self.check_button = ttk.Button(row, text="Vérifier les mises à jour", command=self.check)
        self.check_button.pack(side="left")
        self.install_button = ttk.Button(row, text="Télécharger et installer", command=self.update, state="disabled")
        self.install_button.pack(side="left", padx=12)
        self.cancel_button = ttk.Button(row, text="Annuler le téléchargement", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left")
        ttk.Label(self, textvariable=self.status, wraplength=1050).pack(anchor="w", pady=8)
        ttk.Progressbar(self, variable=self.progress, maximum=100).pack(fill="x", pady=9)
        ttk.Label(self, textvariable=self.release, wraplength=1050).pack(anchor="w", pady=(12, 5))
        self.notes = tk.Text(self, height=10, bg="#152233", fg="#ecf3fa", relief="flat",
                             wrap="word", state="disabled", font=("Segoe UI", 10))
        self.notes.pack(fill="both", expand=True, pady=8)
        ttk.Label(self, text="Arrête la réception et la capture avant l'installation. Tes réglages, sessions et circuits sont conservés.",
                  wraplength=1050).pack(anchor="w", pady=10)

    def _spawn(self, action, kind):
        if self.busy or self._close_requested:
            return
        self.busy = True
        self._cancel.clear()
        self.check_button.config(state="disabled")
        self.install_button.config(state="disabled")

        def run():
            try:
                result = action()
                self._events.put((kind, result))
            except UpdateCancelled:
                self._events.put(("cancelled", None))
            except InstallerFileError as exc:
                self._events.put(("invalid_installer", str(exc)))
            except Exception as exc:
                self._events.put(("error", str(exc)))

        self._worker = threading.Thread(target=run, name="GT7-Update", daemon=True)
        try:
            self._worker.start()
        except RuntimeError:
            self._worker = None
            self.busy = False
            self.installing = False
            self.check_button.config(state="normal")
            self.status.set("Impossible de démarrer la mise à jour. Réessaie après avoir fermé les tâches en cours.")

    def check(self):
        if self.busy or self._close_requested:
            return
        self.info = None
        self.downloaded = None
        self.progress.set(0)
        self.status.set("Recherche d'une nouvelle version…")
        self.release.set("")
        self._set_notes("")
        self._spawn(self.client.check, "checked")

    def _set_notes(self, text):
        self.notes.config(state="normal")
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", text[:12000])
        self.notes.config(state="disabled")

    def _progress(self, downloaded, total):
        now = time.monotonic()
        if downloaded == total or now - self._last_progress >= 0.15:
            self._last_progress = now
            self._events.put(("progress", (downloaded, total)))

    def update(self):
        if self.busy or self._close_requested or self.info is None:
            return
        reason = self.can_install()
        if reason:
            self.status.set(reason)
            return
        if sys.platform != "win32" or not getattr(sys, "frozen", False):
            self.status.set("L'installation est disponible depuis l'application Windows installée.")
            return
        if self.downloaded is not None:
            self._install()
            return
        self.status.set(f"Téléchargement de la version {self.info.version}…")
        self.progress.set(0)
        self._last_progress = 0
        self.cancel_button.config(state="normal")
        info = self.info
        self._spawn(lambda: self.client.download(info, on_progress=self._progress, cancel=self._cancel), "downloaded")

    def _install(self):
        if self._close_requested:
            return
        reason = self.can_install()
        if reason:
            self.status.set("Fichier prêt. " + reason)
            self.install_button.config(text="Installer maintenant", state="normal")
            return
        self.installing = True
        self.status.set("Fichier vérifié · ouverture de l'installateur Windows…")
        info, path = self.info, self.downloaded
        self._spawn(lambda: launch_installer(self.client, info, path), "installed")

    def cancel(self):
        if self.busy and not self.installing:
            self._cancel.set()
            self.status.set("Annulation du téléchargement…")

    def request_close(self):
        self._close_requested = True
        self._cancel.set()

    def poll(self):
        """Appelé uniquement depuis la boucle Tk principale."""
        for _ in range(40):
            try:
                kind, result = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                done, total = result
                self.progress.set(min(100, done * 100 / total) if total else 0)
                if not self._cancel.is_set():
                    self.status.set(f"Téléchargement : {done / 1048576:.1f} / {total / 1048576:.1f} Mo")
                continue
            if self._worker is not None:
                self._worker.join(timeout=0.2)
                if self._worker.is_alive():
                    self._events.put((kind, result))
                    break
                self._worker = None
            self.busy = False
            self.installing = False
            self.cancel_button.config(state="disabled")
            if self._close_requested:
                continue
            if kind == "downloaded" and self._cancel.is_set():
                kind, result = "cancelled", None
            self.check_button.config(state="normal")
            if kind == "checked":
                self.info = result
                if result is None:
                    self.status.set(f"Tu utilises la dernière version disponible : {__version__}.")
                else:
                    self.status.set(f"Nouvelle version disponible : {result.version}")
                    self.release.set("Évolutions de cette version")
                    self._set_notes(result.release_notes)
                    self.install_button.config(text="Télécharger et installer", state="normal")
            elif kind == "downloaded":
                self.downloaded = result
                self.progress.set(100)
                self.status.set("Téléchargement terminé · fichier vérifié")
                self._install()
            elif kind == "installed":
                self.status.set("Installateur lancé · fermeture de l'application")
                self.on_installed()
                break
            elif kind == "cancelled":
                self.status.set("Téléchargement annulé. La version actuelle reste disponible.")
                if self.info is not None:
                    self.install_button.config(state="normal")
            elif kind == "invalid_installer":
                self.downloaded = None
                self.progress.set(0)
                self.status.set(result)
                self.log("Mise à jour : " + result)
                if self.info is not None:
                    self.install_button.config(text="Télécharger à nouveau", state="normal")
            elif kind == "error":
                self.status.set(result)
                self.log("Mise à jour : " + result)
                if self.info is not None:
                    self.install_button.config(text="Réessayer l'installation" if self.downloaded else "Réessayer le téléchargement", state="normal")
