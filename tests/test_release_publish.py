"""Publication Test : API simulée, aucun réseau, jeton ou binaire exécuté."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from scripts import publish_test as publication


TARGET_SHA = "a" * 40
VERSION = "0.8.1"


class FakeGitHub:
    """Métadonnées GitHub contrôlées et journal des seules mutations tentées."""

    def __init__(self, *, sha=None, draft=None, assets=()):
        self.sha = sha
        self.assets = [dict(asset) for asset in assets]
        self.mutations = []
        self.publication_assets = None
        self.release = None if draft is None else self._release(draft)

    @staticmethod
    def _release(draft):
        return {
            "id": 10,
            "tag_name": "test-v" + VERSION,
            "prerelease": True,
            "draft": draft,
            "html_url": f"https://github.com/{publication.REPOSITORY}/releases/tag/test-v{VERSION}",
            "upload_url": f"https://uploads.github.com/repos/{publication.REPOSITORY}/releases/10/assets{{?name,label}}",
        }

    def request(self, method, path, *, body=None, file=None, missing_ok=False):
        if method != "GET":
            self.mutations.append((method, path, body))
        if method == "GET" and path.startswith("/git/ref/tags/"):
            return None if self.sha is None else {"object": {"type": "commit", "sha": self.sha}}
        if method == "POST" and path == "/git/refs":
            self.sha = body["sha"]
            return {"object": {"type": "commit", "sha": self.sha}}
        if method == "GET" and path.startswith("/releases/tags/"):
            return self.release
        if method == "GET" and path == "/releases?per_page=100":
            return [self.release] if self.release is not None else []
        if method == "POST" and path == "/releases":
            self.release = self._release(True)
            self.release.update(body)
            return self.release
        if method == "GET" and path == "/releases/10/assets?per_page=100":
            return [dict(asset) for asset in self.assets]
        if method == "POST" and path.startswith("https://uploads.github.com/"):
            item = {
                "name": parse_qs(urlsplit(path).query)["name"][0],
                "size": file.stat().st_size,
                "digest": "sha256:" + publication.digest(file),
            }
            self.assets.append(item)
            return item
        if method == "PATCH" and path == "/releases/10":
            self.publication_assets = [dict(asset) for asset in self.assets]
            self.release.update(body)
            return self.release
        raise AssertionError(f"Requête GitHub non prévue dans le test : {method} {path}")


class ReleasePublicationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        files = {
            f"GT7-Race-Control-Ecurie-Setup-{VERSION}-test.exe": b"MZ-fixture-not-an-executable",
            f"GT7-Race-Control-Ecurie-Portable-{VERSION}-test.zip": b"PK-fixture-not-an-archive",
        }
        for name, content in files.items():
            (self.directory / name).write_bytes(content)
        manifest = "".join(publication.digest(self.directory / name) + "  " + name + "\r\n"
                           for name in sorted(files))
        (self.directory / "SHA256SUMS.txt").write_bytes(manifest.encode("ascii"))
        self.assets = publication.local_assets(self.directory, VERSION)
        self.remote = [{"name": asset.name, "size": asset.size, "digest": "sha256:" + asset.sha256}
                       for asset in self.assets]
        network_guard = patch.object(publication.GitHub, "request", side_effect=AssertionError("Réseau interdit dans les tests de publication"))
        network_guard.start()
        self.addCleanup(network_guard.stop)
        output_guard = patch.object(publication, "print", create=True)
        output_guard.start()
        self.addCleanup(output_guard.stop)

    def test_new_release_uses_exact_commit_and_publishes_complete_prerelease(self):
        api = FakeGitHub()
        result = publication.publish(api, VERSION, TARGET_SHA, self.assets)
        self.assertEqual(api.sha, TARGET_SHA)
        self.assertEqual(result["target_commitish"], TARGET_SHA)
        self.assertFalse(result["draft"])
        self.assertTrue(result["prerelease"])
        self.assertEqual(result["make_latest"], "false")
        self.assertEqual(api.assets, self.remote)
        self.assertEqual(api.publication_assets, self.remote,
                         "La publication doit intervenir après l'envoi des trois fichiers.")

    def test_identical_existing_publication_performs_no_mutation(self):
        api = FakeGitHub(sha=TARGET_SHA, draft=False, assets=self.remote)
        publication.publish(api, VERSION, TARGET_SHA, self.assets)
        self.assertEqual(api.mutations, [])

    def test_tag_on_another_commit_is_never_moved(self):
        api = FakeGitHub(sha="b" * 40)
        with self.assertRaisesRegex(publication.PublicationError, "immuable"):
            publication.publish(api, VERSION, TARGET_SHA, self.assets)
        self.assertEqual(api.sha, "b" * 40)
        self.assertEqual(api.mutations, [])

    def test_published_asset_with_different_hash_is_never_replaced(self):
        remote = [dict(asset) for asset in self.remote]
        remote[0]["digest"] = "sha256:" + "0" * 64
        api = FakeGitHub(sha=TARGET_SHA, draft=False, assets=remote)
        with self.assertRaisesRegex(publication.PublicationError, "empreinte"):
            publication.publish(api, VERSION, TARGET_SHA, self.assets)
        self.assertEqual(api.mutations, [])
        self.assertEqual(api.assets, remote)

    def test_extra_file_in_published_release_is_refused_without_mutation(self):
        api = FakeGitHub(sha=TARGET_SHA, draft=False, assets=self.remote + [{"name": "other.exe"}])
        with self.assertRaisesRegex(publication.PublicationError, "fichiers"):
            publication.publish(api, VERSION, TARGET_SHA, self.assets)
        self.assertEqual(api.mutations, [])

    def test_matching_partial_draft_uploads_only_missing_files(self):
        api = FakeGitHub(sha=TARGET_SHA, draft=True, assets=self.remote[:1])
        publication.publish(api, VERSION, TARGET_SHA, self.assets)
        uploaded_names = {
            parse_qs(urlsplit(path).query)["name"][0]
            for method, path, _ in api.mutations
            if method == "POST" and path.startswith("https://uploads.github.com/")
        }
        self.assertEqual(uploaded_names, {asset.name for asset in self.assets[1:]})
        self.assertEqual(api.assets, self.remote)
        self.assertFalse(api.release["draft"])

    def test_upload_url_for_another_host_is_refused_before_any_upload(self):
        api = FakeGitHub(sha=TARGET_SHA, draft=True)
        api.release["upload_url"] = "https://example.invalid/assets{?name}"
        with self.assertRaisesRegex(publication.PublicationError, "URL"):
            publication.publish(api, VERSION, TARGET_SHA, self.assets)
        self.assertEqual(api.mutations, [])
        self.assertTrue(api.release["draft"])

    def test_changed_build_is_rejected_by_checksum_manifest(self):
        self.assets[0].path.write_bytes(b"changed-after-build")
        with self.assertRaisesRegex(publication.PublicationError, "empreintes"):
            publication.local_assets(self.directory, VERSION)


if __name__ == "__main__":
    unittest.main()
