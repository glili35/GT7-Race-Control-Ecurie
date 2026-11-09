"""Contrôle du lancement volontaire ; aucun exécutable n'est réellement lancé."""
from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from racecontrol.update_install import InstallerError, launch_installer
from racecontrol.updates import UpdateClient, UpdateInfo


class InstallUpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data_dir = Path(self.directory.name)
        self.client = UpdateClient(current_version="0.7.0", data_dir=self.data_dir)
        self.client.verify_download = Mock(wraps=self.client.verify_download)
        self.content = b'Fichier inerte pour test de mise a jour, pas un executable.'
        name = 'GT7-Race-Control-Ecurie-Setup-0.7.1-test.exe'
        self.info = UpdateInfo(
            version='0.7.1', tag='test-v0.7.1', asset_name=name,
            download_url=f'https://github.com/glili35/GT7-Race-Control-Ecurie/releases/download/test-v0.7.1/{name}',
            size_bytes=len(self.content), sha256=sha256(self.content).hexdigest(),
            release_url='https://github.com/glili35/GT7-Race-Control-Ecurie/releases/tag/test-v0.7.1',
            release_notes='Version de test',
        )
        self.installer = self.data_dir / 'updates' / ('a' * 32) / name
        self.installer.parent.mkdir(parents=True)
        self.installer.write_bytes(self.content)
        self.process = Mock(name='installer-process')
        process_patch = patch('racecontrol.update_install.subprocess.Popen', return_value=self.process)
        self.popen = process_patch.start()
        self.addCleanup(process_patch.stop)
        platform_patch = patch.object(sys, 'platform', 'win32')
        platform_patch.start()
        self.addCleanup(platform_patch.stop)
        frozen_patch = patch.object(sys, 'frozen', True, create=True)
        frozen_patch.start()
        self.addCleanup(frozen_patch.stop)

    def test_verified_installer_opens_interactive_assistant_without_shell(self):
        result = launch_installer(self.client, self.info, self.installer)
        self.assertIs(result, self.process)
        self.assertEqual(self.client.verify_download.call_count, 1)
        args, kwargs = self.popen.call_args
        self.assertEqual(args[0], [str(self.installer), '/CLOSEAPPLICATIONS', '/RESTARTAPPLICATIONS'])
        self.assertFalse(kwargs['shell'])
        self.assertEqual(kwargs['cwd'], str(self.installer.parent))
        self.assertTrue(kwargs['close_fds'])
        self.assertFalse(any('SILENT' in arg.upper() for arg in args[0]))
        self.assertEqual(self.installer.read_bytes(), self.content)

    def test_source_and_other_platforms_are_rejected_before_file_access(self):
        for platform, frozen in [('linux', True), ('darwin', True), ('win32', False)]:
            with self.subTest(platform=platform, frozen=frozen), patch.object(sys, 'platform', platform), patch.object(sys, 'frozen', frozen):
                with self.assertRaises(InstallerError):
                    launch_installer(self.client, self.info, self.installer)
        self.assertEqual(self.client.verify_download.call_count, 0)
        self.popen.assert_not_called()

    def test_tampering_after_download_is_rejected_without_starting_process(self):
        self.installer.write_bytes(self.content.replace(b'inerte', b'altere'))
        self.assertEqual(self.installer.stat().st_size, self.info.size_bytes)
        with self.assertRaisesRegex(InstallerError, 'contenu'):
            launch_installer(self.client, self.info, self.installer)
        self.assertEqual(self.client.verify_download.call_count, 1)
        self.popen.assert_not_called()

    def test_missing_installer_is_not_started(self):
        self.installer.unlink()
        with self.assertRaises(InstallerError):
            launch_installer(self.client, self.info, self.installer)
        self.popen.assert_not_called()

    def test_other_files_and_locations_are_not_treated_as_downloaded_installer(self):
        for relative in [
            Path('elsewhere') / ('a' * 32) / self.info.asset_name,
            Path('updates') / self.info.asset_name,
            Path('updates') / 'not-a-download-id' / self.info.asset_name,
            Path('updates') / ('b' * 32) / 'cmd.exe',
            Path('updates') / ('c' * 32) / self.info.asset_name.replace('0.7.1', '0.7.2'),
        ]:
            with self.subTest(path=relative):
                path = self.data_dir / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self.content)
                with self.assertRaises(InstallerError):
                    launch_installer(self.client, self.info, path)
        self.assertEqual(self.client.verify_download.call_count, 0)
        self.popen.assert_not_called()

    def test_file_symlink_is_rejected_even_with_correct_content(self):
        target = self.data_dir / 'real-installer.exe'
        self.installer.replace(target)
        try:
            self.installer.symlink_to(target)
        except OSError as exc:
            self.skipTest(f'Création de liens non disponible : {exc}')
        with self.assertRaises(InstallerError):
            launch_installer(self.client, self.info, self.installer)
        self.assertEqual(self.client.verify_download.call_count, 0)
        self.popen.assert_not_called()

    def test_redirected_download_directory_is_rejected(self):
        destination = self.data_dir / 'redirected-download'
        self.installer.parent.replace(destination)
        try:
            self.installer.parent.symlink_to(destination, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f'Création de liens non disponible : {exc}')
        with self.assertRaises(InstallerError):
            launch_installer(self.client, self.info, self.installer)
        self.popen.assert_not_called()

    def test_redirected_updates_root_is_rejected(self):
        original = self.data_dir / 'updates'
        redirected = self.data_dir / 'other-updates'
        original.replace(redirected)
        try:
            original.symlink_to(redirected, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f'Création de liens non disponible : {exc}')
        with self.assertRaises(InstallerError):
            launch_installer(self.client, self.info, self.installer)
        self.popen.assert_not_called()

    def test_hardlink_is_rejected(self):
        try:
            os.link(self.installer, self.data_dir / 'other-link.exe')
        except OSError as exc:
            self.skipTest(f'Liens physiques non disponibles : {exc}')
        with self.assertRaises(InstallerError):
            launch_installer(self.client, self.info, self.installer)
        self.popen.assert_not_called()

    def test_file_changed_during_verification_is_not_started(self):
        real_verify = self.client.verify_download

        def verify_then_modify(info, path):
            verified = real_verify(info, path)
            path.write_bytes(self.content + b' modification tardive')
            return verified

        with patch.object(self.client, 'verify_download', side_effect=verify_then_modify):
            with self.assertRaisesRegex(InstallerError, 'pendant sa vérification'):
                launch_installer(self.client, self.info, self.installer)
        self.popen.assert_not_called()

    def test_replaced_file_during_verification_is_not_started(self):
        real_verify = self.client.verify_download

        def verify_then_replace(info, path):
            verified = real_verify(info, path)
            replacement = path.with_suffix('.replacement')
            replacement.write_bytes(self.content)
            try:
                replacement.replace(path)
            except PermissionError:
                # Windows peut déjà refuser le remplacement du fichier ouvert.
                return False
            return verified

        with patch.object(self.client, 'verify_download', side_effect=verify_then_replace):
            with self.assertRaises(InstallerError):
                launch_installer(self.client, self.info, self.installer)
        self.popen.assert_not_called()

    def test_verification_error_never_starts_process(self):
        with patch.object(self.client, 'verify_download', side_effect=OSError('Lecture impossible')):
            with self.assertRaisesRegex(InstallerError, 'vérification'):
                launch_installer(self.client, self.info, self.installer)
        self.popen.assert_not_called()

    def test_launch_failure_is_reported_without_mutating_download(self):
        self.popen.side_effect = OSError('Accès refusé')
        with self.assertRaisesRegex(InstallerError, 'logiciel reste ouvert'):
            launch_installer(self.client, self.info, self.installer)
        self.assertEqual(self.installer.read_bytes(), self.content)
        # Le handle ouvert pour la vérification est bien libéré après échec.
        self.installer.unlink()


if __name__ == '__main__':
    unittest.main()
