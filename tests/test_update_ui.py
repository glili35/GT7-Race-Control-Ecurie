"""Parcours réels Tk des mises à jour, avec I/O et installation contrôlées.

Aucun réseau ni installateur n'est exécuté. Les workers et événements restent
réels ; sous Linux, exécuter ces tests sur un affichage X11 tel que Xvfb.
"""
from __future__ import annotations

import gc
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from racecontrol.update_install import InstallerError, InstallerFileError
from racecontrol.updates import UpdateCancelled, UpdateInfo


class ControlledClient:
    """Double I/O dont les barrières pilotent les courses entre clics et worker."""
    def __init__(self, destination):
        self.destination = destination
        self.check_result = None
        self.check_error = None
        self.download_error = None
        self.check_calls = 0
        self.download_calls = 0
        self.check_threads = []
        self.download_threads = []
        self.check_started = threading.Event()
        self.check_release = threading.Event()
        self.check_release.set()
        self.download_started = threading.Event()
        self.download_release = threading.Event()
        self.download_release.set()
        self.download_finished = threading.Event()
        self.cancel_seen = threading.Event()
        self.honour_cancel = True
        self.progress_threads = []

    def check(self):
        self.check_calls += 1
        self.check_threads.append(threading.get_ident())
        self.check_started.set()
        if not self.check_release.wait(timeout=3):
            raise AssertionError("La barrière de recherche n'a pas été libérée")
        if self.check_error:
            raise self.check_error
        return self.check_result

    def download(self, info, on_progress=None, cancel=None):
        self.download_calls += 1
        self.download_threads.append(threading.get_ident())
        self.download_started.set()
        try:
            if on_progress:
                self.progress_threads.append(threading.get_ident())
                on_progress(5, 10)
            deadline = time.monotonic() + 3
            while not self.download_release.wait(timeout=0.01):
                if self.honour_cancel and cancel is not None and cancel.is_set():
                    self.cancel_seen.set()
                    raise UpdateCancelled("Annulé pendant l'I/O contrôlée")
                if time.monotonic() >= deadline:
                    raise AssertionError("La barrière de téléchargement n'a pas été libérée")
            if self.honour_cancel and cancel is not None and cancel.is_set():
                self.cancel_seen.set()
                raise UpdateCancelled("Annulé à la fin du téléchargement")
            if self.download_error:
                raise self.download_error
            if on_progress:
                on_progress(10, 10)
            return self.destination
        finally:
            self.download_finished.set()


class UpdatePanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
        except tk.TclError as exc:
            raise unittest.SkipTest("Affichage Tk indisponible : lancer avec Xvfb ou sur un bureau Windows.") from exc
        from racecontrol.update_ui import UpdatePanel
        cls.Panel = UpdatePanel

    def setUp(self):
        self.main_thread = threading.get_ident()
        self.directory = tempfile.TemporaryDirectory()
        self.profile = Path(self.directory.name)
        self.installer_path = self.profile / "mise-a-jour-test.exe"
        self.fake = ControlledClient(self.installer_path)
        self.info = UpdateInfo(
            version="9.0.0", tag="v9.0.0-test", asset_name="mise-a-jour-test.exe",
            download_url="https://github.com/glili35/GT7-Race-Control-Ecurie/releases/download/v9.0.0-test/mise-a-jour-test.exe",
            size_bytes=10, sha256="0" * 64,
            release_url="https://github.com/glili35/GT7-Race-Control-Ecurie/releases/tag/v9.0.0-test",
            release_notes="Nouvelle carte et améliorations de stabilité.",
        )
        self.block_reason = None
        self.guards = []
        self.installed = []
        self.logs = []
        self.extra_gates = []
        self.patchers = []
        client_factory = patch("racecontrol.update_ui.UpdateClient", return_value=self.fake)
        self.patchers.append(client_factory)
        self.client_factory = client_factory.start()
        launch = patch("racecontrol.update_ui.launch_installer", return_value=object())
        self.patchers.append(launch)
        self.launch = launch.start()
        self.root = tk.Tk()
        self.root.withdraw()
        self.panel = self.Panel(self.root, data_dir=self.profile,
                                can_install=self.can_install,
                                on_installed=self.on_installed,
                                log=self.log)
        self.panel.pack()
        self.root.update_idletasks()

    def can_install(self):
        self.guards.append(threading.get_ident())
        return self.block_reason

    def on_installed(self):
        self.installed.append(threading.get_ident())

    def log(self, message):
        self.logs.append((threading.get_ident(), message))

    def tearDown(self):
        # Les doubles I/O doivent pouvoir finir même si une assertion échoue.
        self.fake.check_release.set()
        self.fake.download_release.set()
        for gate in self.extra_gates:
            gate.set()
        self.panel.request_close()
        worker = self.panel._worker
        if worker is not None:
            worker.join(timeout=4)
        try:
            self.panel.poll()
            for timer in self.root.tk.call("after", "info"):
                self.root.after_cancel(timer)
            self.root.destroy()
        except tk.TclError:
            pass
        self.panel = None
        self.root = None
        self.fake = None
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.client_factory = None
        self.launch = None
        self.patchers.clear()
        self.directory.cleanup()
        gc.collect()  # Les objets Tcl/Tk sont détruits sur le thread principal.

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.panel.poll()
            self.root.update_idletasks()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Le panneau n'a pas atteint l'état attendu avant expiration du délai")

    def idle(self):
        self.wait_for(lambda: not self.panel.busy)

    def offer_update(self):
        self.fake.check_result = self.info
        self.panel.check()
        self.idle()
        self.assertIs(self.panel.info, self.info)

    def click_update(self):
        # Modifier seulement les gardes des actions ; aucun Path Windows n'est
        # construit sur Linux et aucun installateur natif n'est lancé.
        with patch("racecontrol.update_ui.sys.platform", "win32"), \
             patch("racecontrol.update_ui.sys.frozen", True, create=True):
            self.panel.update()

    def test_constructor_starts_no_check_download_or_installer(self):
        self.assertEqual(self.fake.check_calls, 0)
        self.assertEqual(self.fake.download_calls, 0)
        self.launch.assert_not_called()
        self.assertFalse(self.panel.busy)
        self.assertIsNone(self.panel.info)
        self.assertEqual(str(self.panel.install_button.cget("state")), "disabled")
        self.assertFalse(self.installed)

    def test_check_current_version_does_not_download_or_install(self):
        self.panel.check()
        self.idle()
        self.assertIsNone(self.panel.info)
        self.assertIn("dernière version", self.panel.status.get())
        self.assertEqual(self.fake.check_calls, 1)
        self.assertNotEqual(self.fake.check_threads[0], self.main_thread)
        self.assertEqual(self.fake.download_calls, 0)
        self.launch.assert_not_called()
        self.assertEqual(str(self.panel.install_button.cget("state")), "disabled")

    def test_check_new_version_displays_notes_without_downloading_or_installing(self):
        self.offer_update()
        self.assertIn("9.0.0", self.panel.status.get())
        self.assertIn(self.info.release_notes, self.panel.notes.get("1.0", "end"))
        self.assertEqual(str(self.panel.install_button.cget("state")), "normal")
        self.assertEqual(self.fake.download_calls, 0)
        self.launch.assert_not_called()
        self.assertFalse(self.installed)

    def test_capture_running_prevents_download_and_install(self):
        self.offer_update()
        self.block_reason = "Arrête la capture avant l'installation."
        self.click_update()
        self.assertEqual(self.panel.status.get(), self.block_reason)
        self.assertFalse(self.panel.busy)
        self.assertEqual(self.fake.download_calls, 0)
        self.launch.assert_not_called()
        self.assertFalse(self.installed)
        self.assertEqual(self.guards, [self.main_thread])

    def test_source_or_non_windows_application_cannot_start_installation(self):
        self.offer_update()
        with patch("racecontrol.update_ui.sys.platform", "linux"), \
             patch("racecontrol.update_ui.sys.frozen", False, create=True):
            self.panel.update()
        self.assertIn("application Windows installée", self.panel.status.get())
        self.assertEqual(self.fake.download_calls, 0)
        self.launch.assert_not_called()

    def test_capture_started_during_download_keeps_file_ready_without_closing(self):
        self.offer_update()
        self.fake.download_release.clear()
        self.click_update()
        self.assertTrue(self.fake.download_started.wait(timeout=1))
        self.block_reason = "Une réception PS5 a commencé : arrête-la avant d'installer."
        self.fake.download_release.set()
        self.idle()
        self.assertEqual(self.panel.downloaded, self.installer_path)
        self.assertIn("Fichier prêt", self.panel.status.get())
        self.assertIn(self.block_reason, self.panel.status.get())
        self.launch.assert_not_called()
        self.assertFalse(self.installed)
        self.assertTrue(self.root.winfo_exists())
        self.assertEqual(set(self.guards), {self.main_thread})
        # Un second clic après arrêt de la capture réutilise le fichier vérifié.
        self.block_reason = None
        self.click_update()
        self.idle()
        self.assertEqual(self.fake.download_calls, 1)
        self.launch.assert_called_once_with(self.fake, self.info, self.installer_path)
        self.assertEqual(self.installed, [self.main_thread])

    def test_double_click_during_download_creates_one_job_and_one_install(self):
        self.offer_update()
        self.fake.download_release.clear()
        self.click_update()
        self.assertTrue(self.fake.download_started.wait(timeout=1))
        self.click_update()
        self.panel.check()
        self.assertEqual(self.fake.download_calls, 1)
        self.assertEqual(self.fake.check_calls, 1)
        self.fake.download_release.set()
        self.idle()
        self.assertEqual(self.fake.download_calls, 1)
        self.launch.assert_called_once()
        self.assertEqual(self.installed, [self.main_thread])

    def test_cancel_while_downloading_never_runs_installer(self):
        self.offer_update()
        self.fake.download_release.clear()
        self.click_update()
        self.assertTrue(self.fake.download_started.wait(timeout=1))
        self.panel.cancel()
        self.assertTrue(self.fake.cancel_seen.wait(timeout=1))
        self.idle()
        self.assertIn("annulé", self.panel.status.get())
        self.assertIsNone(self.panel.downloaded)
        self.launch.assert_not_called()
        self.assertFalse(self.installed)
        self.assertEqual(str(self.panel.install_button.cget("state")), "normal")

    def test_cancel_after_io_completes_but_before_poll_still_prevents_install(self):
        self.offer_update()
        self.click_update()
        self.assertTrue(self.fake.download_finished.wait(timeout=1))
        # Ne pas traiter l'événement 'downloaded' avant le clic Annuler : le
        # fichier peut déjà être vérifié, l'intention utilisateur prime encore.
        worker = self.panel._worker
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.panel.cancel()
        self.idle()
        self.launch.assert_not_called()
        self.assertFalse(self.installed)
        self.assertIn("annul", self.panel.status.get().lower())

    def test_request_close_ignores_a_late_successful_download(self):
        self.offer_update()
        self.fake.honour_cancel = False
        self.fake.download_release.clear()
        self.click_update()
        self.assertTrue(self.fake.download_started.wait(timeout=1))
        self.panel.request_close()
        self.fake.download_release.set()
        self.idle()
        self.launch.assert_not_called()
        self.assertFalse(self.installed)
        self.assertFalse(self.panel.installing)

    def test_failed_installer_launch_keeps_application_open_for_retry(self):
        self.offer_update()
        self.launch.side_effect = InstallerError("Windows n'a pas pu ouvrir l'installateur")
        self.click_update()
        self.idle()
        self.assertIn("Windows n'a pas pu ouvrir", self.panel.status.get())
        self.assertFalse(self.installed)
        self.assertTrue(self.root.winfo_exists())
        self.assertFalse(self.panel.installing)
        self.assertEqual(self.panel.downloaded, self.installer_path)
        self.assertEqual(str(self.panel.install_button.cget("state")), "normal")
        self.assertEqual(len(self.logs), 1)
        self.assertEqual(self.logs[0][0], self.main_thread)
        self.assertEqual(self.panel.install_button.cget("text"), "Réessayer l'installation")
        self.launch.side_effect = None
        self.click_update()
        self.idle()
        self.assertEqual(self.fake.download_calls, 1)
        self.assertEqual(self.launch.call_count, 2)
        self.assertEqual(self.installed, [self.main_thread])

    def test_invalid_installer_requires_new_download_before_another_install_attempt(self):
        self.offer_update()
        self.launch.side_effect = [InstallerFileError("Le fichier a changé. Télécharge-le à nouveau."), object()]
        self.click_update()
        self.idle()
        self.assertEqual(self.fake.download_calls, 1)
        self.launch.assert_called_once_with(self.fake, self.info, self.installer_path)
        self.assertIsNone(self.panel.downloaded)
        self.assertEqual(self.panel.progress.get(), 0)
        self.assertEqual(self.panel.install_button.cget("text"), "Télécharger à nouveau")
        self.assertEqual(str(self.panel.install_button.cget("state")), "normal")
        self.assertIn("Télécharge-le à nouveau", self.panel.status.get())
        self.assertFalse(self.installed)
        self.assertFalse(self.panel.installing)
        self.assertTrue(self.root.winfo_exists())
        self.assertIs(self.panel.info, self.info)
        self.assertEqual(len(self.logs), 1)
        self.assertEqual(self.logs[0][0], self.main_thread)
        # Le second clic utilise un nouveau téléchargement et son nouveau chemin.
        replacement = self.profile / "nouveau-telechargement.exe"
        self.fake.destination = replacement
        self.click_update()
        self.idle()
        self.assertEqual(self.fake.download_calls, 2)
        self.assertEqual(self.launch.call_count, 2)
        self.launch.assert_called_with(self.fake, self.info, replacement)
        self.assertEqual(self.panel.downloaded, replacement)
        self.assertEqual(self.installed, [self.main_thread])

    def test_success_notifies_main_thread_only_after_launch_helper_returns(self):
        self.offer_update()
        launch_started = threading.Event()
        launch_release = threading.Event()
        self.extra_gates.append(launch_release)
        stages = []
        def launch(*_):
            stages.append(("helper-start", threading.get_ident()))
            launch_started.set()
            if not launch_release.wait(timeout=3):
                raise AssertionError("La barrière d'installation n'a pas été libérée")
            stages.append(("helper-return", threading.get_ident()))
            return object()
        self.launch.side_effect = launch
        self.click_update()
        self.wait_for(launch_started.is_set)
        self.assertTrue(self.panel.installing)
        self.assertFalse(self.installed)
        self.assertEqual([name for name, _ in stages], ["helper-start"])
        launch_release.set()
        self.idle()
        self.assertEqual([name for name, _ in stages], ["helper-start", "helper-return"])
        self.assertTrue(all(thread != self.main_thread for _, thread in stages))
        self.assertEqual(self.installed, [self.main_thread])
        self.assertIn("Installateur lancé", self.panel.status.get())

    def test_progress_callbacks_only_enqueue_and_tk_changes_stay_on_main_thread(self):
        self.offer_update()
        status_set = self.panel.status.set
        progress_set = self.panel.progress.set
        def main_status(value):
            self.assertEqual(threading.get_ident(), self.main_thread)
            return status_set(value)
        def main_progress(value):
            self.assertEqual(threading.get_ident(), self.main_thread)
            return progress_set(value)
        with patch.object(self.panel.status, "set", side_effect=main_status), \
             patch.object(self.panel.progress, "set", side_effect=main_progress):
            self.click_update()
            self.idle()
        self.assertTrue(self.fake.progress_threads)
        self.assertTrue(all(thread != self.main_thread for thread in self.fake.progress_threads))
        self.assertEqual(self.panel.progress.get(), 100)
        self.assertEqual(self.installed, [self.main_thread])

    def test_check_error_is_visible_and_logged_without_installing(self):
        self.fake.check_error = ValueError("GitHub est temporairement indisponible")
        self.panel.check()
        self.idle()
        self.assertIn("GitHub est temporairement indisponible", self.panel.status.get())
        self.assertEqual(self.logs, [(self.main_thread, "Mise à jour : GitHub est temporairement indisponible")])
        self.assertFalse(self.panel.busy)
        self.assertEqual(self.fake.download_calls, 0)
        self.launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
