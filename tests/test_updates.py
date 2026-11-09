"""Tests hors réseau du canal Test et de l'intégrité des installateurs."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request

from racecontrol.updates import (
    UpdateClient, UpdateInfo, UpdateError, UpdateCancelled, _RedirectPolicy,
    MAX_ASSET_BYTES, MAX_FEED_BYTES, CHUNK_BYTES,
)

REPO = 'glili35/GT7-Race-Control-Ecurie'
BASE = f'https://github.com/{REPO}/releases'
API = f'https://api.github.com/repos/{REPO}/releases?per_page=20'
PAYLOAD = b'MZ' + b'GT7 verified installer test bytes\n' * 5000


def info(version='0.8.0', payload=PAYLOAD):
    name = f'GT7-Race-Control-Ecurie-Setup-{version}-test.exe'
    return UpdateInfo(version, f'test-v{version}', name, f'{BASE}/download/test-v{version}/{name}',
                      len(payload), hashlib.sha256(payload).hexdigest(), f'{BASE}/tag/test-v{version}', 'Notes de version')


def release(version='0.8.0', payload=PAYLOAD):
    value = info(version, payload)
    return {'tag_name': value.tag, 'draft': False, 'prerelease': True, 'html_url': value.release_url,
            'body': value.release_notes,
            'assets': [{'name': 'GT7-portable.zip', 'size': 100, 'digest': None},
                       {'name': value.asset_name, 'size': value.size_bytes,
                        'browser_download_url': value.download_url, 'digest': 'sha256:' + value.sha256},
                       {'name': 'build.log', 'size': 500, 'digest': None}]}


class Response:
    def __init__(self, content, url, *, headers=None, fail_after=None, error=None):
        self.body = io.BytesIO(content)
        self.url = url
        self.status = 200
        self.headers = {'Content-Length': str(len(content))} if headers is None else headers
        self.closed = threading.Event()
        self.fail_after, self.error = fail_after, error
        self.reads = 0

    def geturl(self):
        return self.url

    def read(self, size):
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            raise self.error or OSError('Réseau interrompu')
        return self.body.read(size)

    read1 = read

    def close(self):
        self.body.close()
        self.closed.set()


class Opener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return self.response


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name) / 'private-app'
        self.client = UpdateClient(current_version='0.7.0', data_dir=self.data)

    def tearDown(self):
        self.temp.cleanup()

    @contextmanager
    def response(self, response):
        opener = Opener(response)
        with patch('racecontrol.updates.build_opener', return_value=opener):
            try:
                yield opener
            finally:
                # Un worker annulé peut terminer juste après le retour public.
                response.closed.wait(timeout=1)

    def feed(self, releases):
        return Response(json.dumps(releases).encode(), API)

    def assert_no_installer(self):
        self.assertFalse(list(self.data.rglob('*.exe')) if self.data.exists() else [])
        self.assertFalse(list(self.data.rglob('*.part')) if self.data.exists() else [])

    def test_selects_latest_test_version_numerically_without_touching_disk(self):
        releases = [release('0.9.0'), release('0.10.0'), release('0.8.0')]
        with self.response(self.feed(releases)) as opener:
            result = self.client.check()
        self.assertEqual(result, info('0.10.0'))
        self.assertFalse(self.data.exists())
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, API)
        self.assertEqual(timeout, 10)
        self.assertNotIn('Authorization', request.headers)

    def test_drafts_other_channels_invalid_tags_and_rollbacks_are_ignored(self):
        releases = [release('0.7.0'), release('0.6.0')]
        draft = release('0.9.0')
        draft['draft'] = True
        releases.append(draft)
        for tag in ('v0.9.0', 'stable-v0.9.0', 'test-v0.8.0-beta', 'test-v0.08.0', 'test-v0.8', 'test-v1.2.3.4'):
            other = release('0.9.0')
            other['tag_name'] = tag
            releases.append(other)
        with self.response(self.feed(releases)):
            self.assertIsNone(self.client.check())

    def test_prerelease_and_non_prerelease_test_tags_are_both_supported(self):
        for prerelease in (True, False):
            value = release()
            value['prerelease'] = prerelease
            with self.response(self.feed([value])):
                self.assertEqual(self.client.check().version, '0.8.0')

    def test_malformed_feed_is_reported(self):
        for content in (b'not json', b'\xff', b'{}', b'null', b'[NaN]',
                        b'[{"draft":true,"draft":false}]', json.dumps([release()] * 21).encode()):
            with self.subTest(content=content[:10]):
                with self.response(Response(content, API)), self.assertRaises(UpdateError):
                    self.client.check()

    def test_missing_digest_fails_clearly_instead_of_silently_choosing_old_release(self):
        latest = release('0.9.0')
        del latest['assets'][1]['digest']
        with self.response(self.feed([release('0.8.0'), latest])), self.assertRaisesRegex(UpdateError, 'SHA256'):
            self.client.check()

    def test_wrong_digest_missing_exe_and_duplicate_assets_are_rejected(self):
        cases = []
        for digest in ('md5:' + 'a' * 32, 'sha256:abc', None):
            value = release()
            value['assets'][1]['digest'] = digest
            cases.append(value)
        value = release()
        del value['assets'][1]
        cases.append(value)
        value = release()
        value['assets'].append(dict(value['assets'][1]))
        cases.append(value)
        for value in cases:
            with self.response(self.feed([value])), self.assertRaises(UpdateError):
                self.client.check()

    def test_untrusted_asset_and_release_addresses_are_rejected(self):
        for address in ('http://github.com/x', 'https://evil.example/setup.exe',
                        'https://user:pass@github.com/x',
                        info().download_url.replace(REPO, 'attacker/other'),
                        info().download_url + '?extra=1', info().download_url + '#fragment',
                        info().download_url.replace('test-v0.8.0/', 'test-v0.9.0/')):
            value = release()
            value['assets'][1]['browser_download_url'] = address
            with self.response(self.feed([value])), self.assertRaises(UpdateError):
                self.client.check()
        value = release()
        value['html_url'] = 'https://evil.example/release'
        with self.response(self.feed([value])), self.assertRaises(UpdateError):
            self.client.check()

    def test_metadata_size_is_positive_integer_and_at_most_150_mib(self):
        for size in (0, -1, True, 1.5, MAX_ASSET_BYTES + 1, '100'):
            value = release()
            value['assets'][1]['size'] = size
            with self.response(self.feed([value])), self.assertRaises(UpdateError):
                self.client.check()

    def test_api_and_asset_redirect_policies_reject_foreign_sources(self):
        api_policy = _RedirectPolicy(self.client._api_address)
        for target in ('http://api.github.com/x', 'https://evil.example/feed',
                       API.replace(REPO, 'attacker/other'), 'https://api.github.com/user'):
            with self.assertRaises(UpdateError):
                api_policy.redirect_request(Request(API), None, 302, 'Moved', {}, target)
        asset_policy = _RedirectPolicy(lambda url: self.client._asset_address(url, info(), redirect=True))
        for target in ('https://raw.githubusercontent.com/attacker/x/main/evil.exe',
                       'https://release-assets.githubusercontent.com.evil.example/file',
                       'https://user:pass@objects.githubusercontent.com/github-production-release-asset/x',
                       info().download_url.replace(REPO, 'attacker/other')):
            with self.assertRaises(UpdateError):
                asset_policy.redirect_request(Request(info().download_url), None, 302, 'Moved', {}, target)
        for host in ('release-assets.githubusercontent.com', 'objects.githubusercontent.com'):
            url = f'https://{host}/github-production-release-asset/x/y?signature=public-download'
            redirected = asset_policy.redirect_request(Request(info().download_url), None, 302, 'Moved', {}, url)
            self.assertEqual(redirected.full_url, url)

    def test_download_rechecks_final_url_even_if_opener_skipped_redirect_handler(self):
        with self.response(Response(PAYLOAD, 'https://evil.example/file')), self.assertRaises(UpdateError):
            self.client.download(info())
        self.assert_no_installer()

    def test_feed_body_is_bounded(self):
        response = Response(b'', API, headers={'Content-Length': str(MAX_FEED_BYTES + 1)})
        with self.response(response), self.assertRaises(UpdateError):
            self.client.check()
        self.assertEqual(response.reads, 0)

    def test_download_is_streamed_verified_private_and_progress_is_bounded(self):
        value = info()
        progress = []
        with self.response(Response(PAYLOAD, value.download_url)):
            path = self.client.download(value, on_progress=lambda received, total: progress.append((received, total)))
        self.assertEqual(path.parent.parent, self.data / 'updates')
        self.assertRegex(path.parent.name, r'^[0-9a-f]{32}$')
        self.assertEqual(path.name, value.asset_name)
        self.assertEqual(path.read_bytes(), PAYLOAD)
        self.assertTrue(self.client.verify_download(value, path))
        self.assertEqual(progress[0], (0, len(PAYLOAD)))
        self.assertEqual(progress[-1], (len(PAYLOAD), len(PAYLOAD)))
        self.assertTrue(all(total == len(PAYLOAD) and 0 <= received <= total for received, total in progress))
        self.assertEqual([received for received, _ in progress], sorted(received for received, _ in progress))
        self.assertFalse(list(self.data.rglob('*.part')))
        if os.name != 'nt':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_download_rejects_rollback_and_tampered_info_before_network(self):
        for value in (info('0.7.0'), info('0.6.0'), replace(info(), tag='test-v9.9.9'),
                      replace(info(), sha256='bad'), replace(info(), asset_name='../evil.exe'),
                      replace(info(), download_url='https://evil.example/setup.exe')):
            with patch('racecontrol.updates.build_opener') as opener, self.assertRaises(UpdateError):
                self.client.download(value)
            opener.assert_not_called()
        self.assertFalse(self.data.exists())

    def test_digest_mismatch_removes_partial_and_completed_installers(self):
        value = replace(info(), sha256='0' * 64)
        with self.response(Response(PAYLOAD, value.download_url)), self.assertRaisesRegex(UpdateError, 'SHA256'):
            self.client.download(value)
        self.assert_no_installer()

    def test_declared_size_short_read_overflow_and_encoding_fail_cleanly(self):
        value = info()
        cases = [Response(PAYLOAD, value.download_url, headers={'Content-Length': '1'}),
                 Response(PAYLOAD[:-10], value.download_url, headers={}),
                 Response(PAYLOAD + b'extra', value.download_url, headers={}),
                 Response(PAYLOAD, value.download_url, headers={'Content-Encoding': 'gzip'})]
        for response in cases:
            with self.response(response), self.assertRaises(UpdateError):
                self.client.download(value)
            self.assert_no_installer()

    def test_cancel_before_download_creates_no_folder_or_network_request(self):
        cancel = threading.Event()
        cancel.set()
        with patch('racecontrol.updates.build_opener') as opener, self.assertRaises(UpdateCancelled):
            self.client.download(info(), cancel=cancel)
        opener.assert_not_called()
        self.assertFalse(self.data.exists())

    def test_cancel_after_first_chunk_cleans_up(self):
        value = info()
        cancel = threading.Event()
        def progress(received, total):
            if received:
                cancel.set()
        with self.response(Response(PAYLOAD, value.download_url)), self.assertRaises(UpdateCancelled):
            self.client.download(value, on_progress=progress, cancel=cancel)
        self.assert_no_installer()

    def test_cancellation_during_final_checksum_still_removes_installer(self):
        value = info()
        cancel = threading.Event()
        verify = self.client.verify_download
        def cancel_while_verifying(update, path):
            valid = verify(update, path)
            cancel.set()
            return valid
        with self.response(Response(PAYLOAD, value.download_url)), \
                patch.object(self.client, 'verify_download', side_effect=cancel_while_verifying), \
                self.assertRaises(UpdateCancelled):
            self.client.download(value, cancel=cancel)
        self.assert_no_installer()

    def test_directory_permission_error_has_public_update_error(self):
        with patch.object(Path, 'mkdir', side_effect=PermissionError('no access')), \
                self.assertRaisesRegex(UpdateError, 'permissions'):
            self.client.download(info())
        self.assert_no_installer()

    def test_existing_download_directory_is_never_cleaned_as_if_new(self):
        folder = self.data / 'updates' / ('a' * 32)
        folder.mkdir(parents=True)
        existing = folder / info().asset_name
        existing.write_bytes(b'keep existing file')
        class ExistingUUID:
            hex = 'a' * 32
        with patch('racecontrol.updates.uuid.uuid4', return_value=ExistingUUID()), self.assertRaises(UpdateError):
            self.client.download(info())
        self.assertEqual(existing.read_bytes(), b'keep existing file')

    def test_network_failure_or_truncated_http_body_cleans_up(self):
        value = info()
        for error in (OSError('network error'), http.client.IncompleteRead(b'partial')):
            response = Response(PAYLOAD, value.download_url, fail_after=1, error=error)
            with self.response(response), self.assertRaises(UpdateError):
                self.client.download(value)
            self.assert_no_installer()

    def test_total_budget_includes_stalled_open_without_late_file_write(self):
        value = info()
        release_gate = threading.Event()
        response = Response(PAYLOAD, value.download_url)
        class StalledOpener:
            def open(self, *_args, **_kwargs):
                release_gate.wait(timeout=2)
                return response
        with patch('racecontrol.updates.build_opener', return_value=StalledOpener()), \
                patch('racecontrol.updates.DOWNLOAD_BUDGET_S', 0.05):
            try:
                started = time.monotonic()
                with self.assertRaises(UpdateError):
                    self.client.download(value)
                self.assertLess(time.monotonic() - started, 0.8)
                self.assert_no_installer()
            finally:
                release_gate.set()
                self.assertTrue(response.closed.wait(timeout=1))
        self.assert_no_installer()

    def test_verify_rejects_changed_content_missing_file_and_outside_path(self):
        value = info()
        with self.response(Response(PAYLOAD, value.download_url)):
            path = self.client.download(value)
        outside = Path(self.temp.name) / value.asset_name
        outside.write_bytes(PAYLOAD)
        self.assertFalse(self.client.verify_download(value, outside))
        path.write_bytes(b'x' * len(PAYLOAD))
        self.assertFalse(self.client.verify_download(value, path))
        path.unlink()
        self.assertFalse(self.client.verify_download(value, path))

    @unittest.skipIf(os.name == 'nt', 'La création de liens Windows nécessite des privilèges spécifiques.')
    def test_symlinked_parent_is_rejected_without_touching_external_directory(self):
        self.data.mkdir()
        external = Path(self.temp.name) / 'external'
        external.mkdir()
        (self.data / 'updates').symlink_to(external, target_is_directory=True)
        with self.assertRaises(UpdateError):
            self.client.download(info())
        self.assertFalse(list(external.iterdir()))

    @unittest.skipIf(os.name == 'nt', 'La création de liens Windows nécessite des privilèges spécifiques.')
    def test_partial_file_replaced_by_symlink_never_overwrites_external_target(self):
        value = info()
        external = Path(self.temp.name) / 'keep.txt'
        external.write_bytes(b'unchanged')
        replaced = False
        def progress(received, total):
            nonlocal replaced
            if received and not replaced:
                temporary = next(self.data.rglob('*.part'))
                temporary.unlink()
                temporary.symlink_to(external)
                replaced = True
        with self.response(Response(PAYLOAD, value.download_url)), self.assertRaises(UpdateError):
            self.client.download(value, on_progress=progress)
        self.assertEqual(external.read_bytes(), b'unchanged')
        self.assert_no_installer()

    @unittest.skipIf(os.name == 'nt', 'La création de liens Windows nécessite des privilèges spécifiques.')
    def test_verify_rejects_symlinks_and_hardlinks(self):
        value = info()
        with self.response(Response(PAYLOAD, value.download_url)):
            path = self.client.download(value)
        external = Path(self.temp.name) / 'copy.exe'
        os.link(path, external)
        self.assertFalse(self.client.verify_download(value, path))
        external.unlink()
        path.rename(external)
        path.symlink_to(external)
        self.assertFalse(self.client.verify_download(value, path))

    @unittest.skipIf(os.name == 'nt', 'Les FIFO ne sont pas disponibles sous Windows.')
    def test_verify_rejects_fifo_without_waiting_for_a_writer(self):
        value = info()
        folder = self.data / 'updates' / ('a' * 32)
        folder.mkdir(parents=True)
        path = folder / value.asset_name
        os.mkfifo(path)
        started = time.monotonic()
        self.assertFalse(self.client.verify_download(value, path))
        self.assertLess(time.monotonic() - started, 0.5)


if __name__ == '__main__':
    unittest.main()
