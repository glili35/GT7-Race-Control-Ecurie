"""Publication immuable du canal Test après les validations Windows du CI.

Le jeton GitHub est limité à l'étape de publication. La vérification suivante
utilise le vrai client de mises à jour sans authentification et n'exécute pas
l'installateur téléchargé. Aucun tag, fichier ou version existants ne sont
supprimés ou remplacés.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPOSITORY = "glili35/GT7-Race-Control-Ecurie"
PUBLISH_BRANCH = "refs/heads/main"
API_ROOT = f"https://api.github.com/repos/{REPOSITORY}"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
VERSION_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")


class PublicationError(RuntimeError):
    pass


class GitHubError(PublicationError):
    def __init__(self, status, message):
        super().__init__(f"GitHub refuse l'opération (HTTP {status}) : {message}")
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Une redirection ne doit jamais transmettre le jeton à un autre hôte.
        return None


class GitHub:
    def __init__(self, token=""):
        self.token = token
        self.opener = build_opener(_NoRedirect())

    def request(self, method, path, *, body=None, file=None, missing_ok=False):
        url = path if path.startswith("https://") else API_ROOT + path
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname not in ("api.github.com", "uploads.github.com")
                or parts.username or parts.password or parts.port not in (None, 443)):
            raise PublicationError("Destination GitHub de publication incorrecte.")
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "GT7-Race-Control-Publication",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        with ExitStack() as stack:
            data = None
            if file is not None:
                data = stack.enter_context(file.open("rb"))
                headers["Content-Type"] = "application/octet-stream"
                headers["Content-Length"] = str(file.stat().st_size)
            elif body is not None:
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            try:
                response = stack.enter_context(self.opener.open(Request(url, data=data, headers=headers, method=method), timeout=45))
                raw = response.read(4_000_001)
            except HTTPError as exc:
                if missing_ok and exc.code == 404:
                    return None
                try:
                    detail = json.loads(exc.read(4096)).get("message", "Requête refusée")
                except (ValueError, AttributeError):
                    detail = "Requête refusée"
                raise GitHubError(exc.code, str(detail)[:300]) from None
        if len(raw) > 4_000_000:
            raise PublicationError("Réponse GitHub trop volumineuse.")
        return json.loads(raw) if raw else None


@dataclass(frozen=True)
class Asset:
    path: Path
    sha256: str
    size: int

    @property
    def name(self):
        return self.path.name


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            result.update(chunk)
    return result.hexdigest()


def release_notes(version):
    """Présente les évolutions utiles dans le panneau de mise à jour."""
    changelog = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
    lines = changelog.read_text(encoding="utf-8").splitlines()
    section = []
    active = False
    for line in lines:
        if line.startswith("## "):
            if active:
                break
            active = line == f"## {version} Test"
        elif active:
            section.append(line)
    changes = "\n".join(section).strip()
    if not changes:
        raise PublicationError("Les évolutions de cette version sont absentes du fichier CHANGELOG.md.")
    return (f"GT7 Race Control Écurie {version} — Canal Test\n\n" + changes
            + "\n\nL'installateur conserve les réglages, circuits et sessions. "
              "Cette publication reste une version de test.")


def local_assets(directory, version):
    filenames = (f"GT7-Race-Control-Ecurie-Setup-{version}-test.exe",
                 f"GT7-Race-Control-Ecurie-Portable-{version}-test.zip", "SHA256SUMS.txt")
    assets = []
    for name in filenames:
        path = directory / name
        if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
            raise PublicationError(f"Fichier de construction absent ou incorrect : {name}")
        assets.append(Asset(path, digest(path), path.stat().st_size))
    manifest = {}
    for line in assets[-1].path.read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line)
        if not match or match[2] in manifest:
            raise PublicationError("Le fichier SHA256SUMS.txt est incorrect.")
        manifest[match[2]] = match[1]
    if manifest != {asset.name: asset.sha256 for asset in assets[:-1]}:
        raise PublicationError("Les fichiers ne correspondent pas à leurs empreintes de construction.")
    return tuple(assets)


def tag_sha(api, tag):
    reference = api.request("GET", "/git/ref/tags/" + quote(tag, safe=""), missing_ok=True)
    if reference is None:
        return None
    obj = reference["object"]
    for _ in range(5):
        if obj.get("type") == "commit" and SHA_PATTERN.fullmatch(str(obj.get("sha", ""))):
            return obj["sha"]
        if obj.get("type") != "tag" or not SHA_PATTERN.fullmatch(str(obj.get("sha", ""))):
            break
        obj = api.request("GET", "/git/tags/" + obj["sha"])["object"]
    raise PublicationError("Le tag existant ne désigne pas un commit vérifiable.")


def release_assets(api, release):
    result = api.request("GET", f"/releases/{release['id']}/assets?per_page=100")
    if not isinstance(result, list):
        raise PublicationError("Liste de fichiers GitHub incorrecte.")
    names = [asset.get("name") for asset in result]
    if len(names) != len(set(names)):
        raise PublicationError("La version contient des noms de fichiers en double.")
    return {asset["name"]: asset for asset in result}


def verify_remote_assets(api, release, assets, *, attempts=15):
    for attempt in range(attempts):
        remote = release_assets(api, release)
        if set(remote) != {asset.name for asset in assets}:
            raise PublicationError("Les fichiers de cette version diffèrent de la construction validée.")
        pending = False
        for asset in assets:
            item = remote[asset.name]
            if item.get("size") != asset.size:
                raise PublicationError(f"La taille du fichier publié diffère : {asset.name}")
            if not item.get("digest"):
                pending = True
            elif item["digest"] != "sha256:" + asset.sha256:
                raise PublicationError(f"L'empreinte du fichier publié diffère : {asset.name}")
        if not pending:
            return
        if attempt + 1 < attempts:
            time.sleep(2)
    raise PublicationError("GitHub n'a pas fourni les empreintes officielles des fichiers dans le délai prévu.")


def publish(api, version, sha, assets):
    tag = "test-v" + version
    existing_sha = tag_sha(api, tag)
    if existing_sha is not None and existing_sha != sha:
        raise PublicationError("Version immuable : ce tag existe déjà sur un autre commit. Augmenter le numéro de version.")
    if existing_sha is None:
        try:
            api.request("POST", "/git/refs", body={"ref": "refs/tags/" + tag, "sha": sha})
        except GitHubError as exc:
            if exc.status not in (409, 422):
                raise
        if tag_sha(api, tag) != sha:
            raise PublicationError("Le tag ne correspond pas au commit validé. Aucune publication effectuée.")

    release = api.request("GET", "/releases/tags/" + tag, missing_ok=True)
    if release is None:
        # Les brouillons ne sont pas toujours retournés par l'URL par tag.
        drafts = api.request("GET", "/releases?per_page=100")
        release = next((item for item in drafts if item.get("tag_name") == tag), None)
    if release is not None:
        if release.get("prerelease") is not True or release.get("tag_name") != tag:
            raise PublicationError("Le tag correspond à une version qui n'appartient pas au canal Test.")
        if not release.get("draft"):
            verify_remote_assets(api, release, assets)
            print("Préversion déjà publiée, commit et fichiers identiques : " + release["html_url"])
            return release
    else:
        release = api.request("POST", "/releases", body={
            "tag_name": tag, "target_commitish": sha,
            "name": f"GT7 Race Control Écurie {version} — Test",
            "body": release_notes(version),
            "draft": True, "prerelease": True, "make_latest": "false",
        })
    remote = release_assets(api, release)
    if set(remote) - {asset.name for asset in assets}:
        raise PublicationError("Le brouillon contient d'autres fichiers. Rien ne sera remplacé.")
    for asset in assets:
        if asset.name in remote:
            item = remote[asset.name]
            if item.get("size") != asset.size or (item.get("digest") and item["digest"] != "sha256:" + asset.sha256):
                raise PublicationError("Le brouillon contient un fichier différent : " + asset.name)
            continue
        upload_url = release["upload_url"].split("{", 1)[0]
        expected_path = f"/repos/{REPOSITORY}/releases/{release['id']}/assets"
        if urlsplit(upload_url).netloc != "uploads.github.com" or urlsplit(upload_url).path != expected_path:
            raise PublicationError("URL de dépôt de fichiers GitHub incorrecte.")
        api.request("POST", upload_url + "?name=" + quote(asset.name, safe=""), file=asset.path)
    verify_remote_assets(api, release, assets)
    if tag_sha(api, tag) != sha:
        raise PublicationError("Le tag a changé pendant la préparation. Le brouillon reste non publié.")
    published = api.request("PATCH", f"/releases/{release['id']}", body={
        "draft": False, "prerelease": True, "make_latest": "false",
    })
    if published.get("draft") or published.get("prerelease") is not True:
        raise PublicationError("L'état de la préversion publiée n'a pas été confirmé.")
    print("Préversion Test publiée : " + published["html_url"])
    return published


def verify_public(version, previous_version, sha, assets):
    # Cette étape du workflow n'a pas de jeton. Le supprimer aussi ici évite
    # qu'une configuration future transforme ce test public en test connecté.
    os.environ.pop("GH_TOKEN", None)
    os.environ.pop("GITHUB_TOKEN", None)
    api = GitHub()
    tag = "test-v" + version
    release = api.request("GET", "/releases/tags/" + tag)
    if release.get("draft") or release.get("prerelease") is not True or tag_sha(api, tag) != sha:
        raise PublicationError("La préversion publique ne correspond pas au commit validé.")
    verify_remote_assets(api, release, assets)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from racecontrol.updates import UpdateClient, UpdateError

    expected = assets[0]
    with tempfile.TemporaryDirectory(prefix="gt7-update-public-") as directory:
        client = UpdateClient(repository=REPOSITORY, current_version=previous_version, data_dir=Path(directory))
        info = None
        last_error = "Aucune mise à jour proposée"
        for attempt in range(15):
            try:
                info = client.check()
                if info is not None and info.version == version and info.tag == tag:
                    break
                last_error = "La nouvelle préversion n'apparaît pas encore dans la liste publique"
            except UpdateError as exc:
                last_error = str(exc)
            if attempt < 14:
                time.sleep(2)
        else:
            raise PublicationError(last_error)
        if info.asset_name != expected.name or info.sha256 != expected.sha256 or info.size_bytes != expected.size:
            raise PublicationError("Le client ne propose pas le fichier et l'empreinte qui viennent d'être publiés.")
        path = client.download(info)
        if not client.verify_download(info, path) or digest(path) != expected.sha256:
            raise PublicationError("Le téléchargement public ne correspond pas à la construction validée.")
    print(f"Mise à jour publique {previous_version} → {version} téléchargée et vérifiée, sans authentification ni exécution.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--verify-public", action="store_true")
    parser.add_argument("--previous-version", default="0.7.0")
    args = parser.parse_args()
    try:
        if not VERSION_PATTERN.fullmatch(args.version) or not SHA_PATTERN.fullmatch(args.target_sha):
            raise PublicationError("Version ou commit cible incorrect.")
        assets = local_assets(args.artifact_dir, args.version)
        if args.verify_public:
            verify_public(args.version, args.previous_version, args.target_sha, assets)
        else:
            if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
                    or os.environ.get("GITHUB_REF") != PUBLISH_BRANCH
                    or os.environ.get("GITHUB_EVENT_NAME") not in ("push", "workflow_dispatch")
                    or os.environ.get("GITHUB_SHA") != args.target_sha):
                raise PublicationError("La publication est réservée au workflow de la branche de test sur son commit exact.")
            token = os.environ.get("GH_TOKEN", "")
            if not token:
                raise PublicationError("Jeton GitHub absent : la publication nécessite contents: write dans ce job.")
            publish(GitHub(token), args.version, args.target_sha, assets)
        return 0
    except Exception as exc:
        print("Publication/validation interrompue : " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
