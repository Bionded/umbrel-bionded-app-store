"""Minimal OCI registry client: tags, digests and platforms (Docker Hub, GHCR, lscr.io, quay.io)."""

from __future__ import annotations

import re
from dataclasses import dataclass

import requests

ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    ]
)
REQUIRED_PLATFORMS = ("linux/amd64", "linux/arm64")
TIMEOUT = 30


@dataclass(frozen=True)
class ImageRef:
    registry: str
    repository: str
    tag: str | None
    digest: str | None

    @property
    def name(self) -> str:
        """Image name as written in compose (without tag/digest)."""
        if self.registry == "registry-1.docker.io":
            repo = self.repository.removeprefix("library/")
            return repo
        return f"{self.registry}/{self.repository}"


def parse_image(ref: str) -> ImageRef:
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    tag = None
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        ref, tag = ref.rsplit(":", 1)
    parts = ref.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry, repository = parts[0], "/".join(parts[1:])
    else:
        registry, repository = "registry-1.docker.io", ref
        if len(parts) == 1:
            repository = f"library/{ref}"
    return ImageRef(registry, repository, tag, digest)


class Registry:
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self._tokens: dict[tuple[str, str], str | None] = {}

    def _api_host(self, registry: str) -> str:
        # lscr.io is a redirector in front of ghcr.io
        return "ghcr.io" if registry == "lscr.io" else registry

    def _token(self, registry: str, repository: str, challenge: str | None = None) -> str | None:
        key = (registry, repository)
        if key in self._tokens:
            return self._tokens[key]
        token = None
        if registry == "registry-1.docker.io":
            url = f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repository}:pull"
        elif challenge:
            realm = re.search(r'realm="([^"]+)"', challenge)
            service = re.search(r'service="([^"]+)"', challenge)
            url = f"{realm.group(1)}?scope=repository:{repository}:pull" if realm else None
            if url and service:
                url += f"&service={service.group(1)}"
        else:
            url = f"https://{registry}/token?scope=repository:{repository}:pull"
        if url:
            r = self.session.get(url, timeout=TIMEOUT)
            if r.ok:
                token = r.json().get("token") or r.json().get("access_token")
        self._tokens[key] = token
        return token

    def _get(self, registry: str, repository: str, path: str, method: str = "GET") -> requests.Response:
        host = self._api_host(registry)
        url = f"https://{host}/v2/{repository}/{path}"
        headers = {"Accept": ACCEPT}
        token = self._token(host, repository)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = self.session.request(method, url, headers=headers, timeout=TIMEOUT)
        if r.status_code == 401 and "Www-Authenticate" in r.headers:
            self._tokens.pop((host, repository), None)
            token = self._token(host, repository, r.headers["Www-Authenticate"])
            if token:
                headers["Authorization"] = f"Bearer {token}"
                r = self.session.request(method, url, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        return r

    def tags(self, image: str) -> list[str]:
        ref = parse_image(image)
        out: list[str] = []
        path: str | None = "tags/list?n=1000"
        while path:
            r = self._get(ref.registry, ref.repository, path)
            out += r.json().get("tags") or []
            link = r.headers.get("Link")
            m = re.search(r"<[^>]*/v2/.+?/(tags/list[^>]*)>", link) if link else None
            path = m.group(1) if m else None
        return out

    def manifest(self, image: str) -> tuple[str, dict]:
        """Return (index digest, manifest json) for tag or digest reference."""
        ref = parse_image(image)
        r = self._get(ref.registry, ref.repository, f"manifests/{ref.digest or ref.tag or 'latest'}")
        return r.headers.get("Docker-Content-Digest", ""), r.json()

    def platforms(self, image: str) -> set[str]:
        _, m = self.manifest(image)
        plats = set()
        for entry in m.get("manifests", []):
            p = entry.get("platform") or {}
            if p.get("os") in (None, "unknown"):
                continue
            plats.add(f"{p['os']}/{p['architecture']}")
        if not plats and "config" in m:
            # Single-arch image: read the config blob to find its platform
            ref = parse_image(image)
            cfg = self._get(ref.registry, ref.repository, f"blobs/{m['config']['digest']}").json()
            plats.add(f"{cfg.get('os')}/{cfg.get('architecture')}")
        return plats

    def missing_platforms(self, image: str) -> list[str]:
        have = self.platforms(image)
        return [p for p in REQUIRED_PLATFORMS if p not in have]
