#!/usr/bin/env python3
"""Validate the Music Stack community store.

Checks every app folder against the umbrelOS manifest schema and this store's
conventions:

* manifest fields/types (mirrors umbreld's AppManifestSchema), id == folder,
  store prefix, semver-ish manifestVersion
* docker-compose: app_proxy APP_HOST/APP_PORT point at a real service,
  images pinned as tag@sha256, restart: on-failure, no `platform:`,
  `docker compose config` passes
* host ports unique inside the store and against the official Umbrel store
* icon 256x256 PNG and gallery 1440x900 JPG committed and referenced by URL
* every image index lists linux/amd64 and linux/arm64 (--check-images)
* changed images come with a bumped `version` (--base REF)

Usage:
  python3 scripts/validate_store.py [--check-images] [--compose] [--base origin/master]
                                    [--official-store PATH] [APP ...]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import Registry  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW_BASE = "https://raw.githubusercontent.com/Bionded/umbrel-bionded-app-store/master"
REQUIRED = ["manifestVersion", "id", "name", "tagline", "category", "version", "port",
            "description", "website", "support", "gallery"]
NOTE_MAX = 300
RESERVED_PORTS = {80, 443, 22, 53}
IMAGE_RE = re.compile(r"^[^@\s]+:[^@\s]+@sha256:[0-9a-f]{64}$")

errors: list[str] = []
warnings: list[str] = []


def err(app: str, msg: str) -> None:
    errors.append(f"{app}: {msg}")


def warn(app: str, msg: str) -> None:
    warnings.append(f"{app}: {msg}")


def load_yaml(path: Path):
    with path.open() as fh:
        return yaml.safe_load(fh)


def semver_like(v) -> bool:
    return bool(re.fullmatch(r"\d+(\.\d+){0,2}", str(v)))


def png_size(path: Path) -> tuple[int, int] | None:
    data = path.read_bytes()[:32]
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def jpeg_size(path: Path) -> tuple[int, int] | None:
    data = path.read_bytes()
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        seg = int.from_bytes(data[i + 2:i + 4], "big")
        i += 2 + seg
    return None


def official_ports(store: Path | None) -> dict[int, str]:
    ports: dict[int, str] = {}
    if not store or not store.is_dir():
        return ports
    for manifest in store.glob("*/umbrel-app.yml"):
        try:
            m = load_yaml(manifest)
            ports[int(m["port"])] = m["id"]
        except Exception:
            continue
        compose = manifest.parent / "docker-compose.yml"
        if compose.exists():
            for p in published_ports(load_yaml(compose) or {}):
                ports.setdefault(p, m["id"])
    return ports


def published_ports(compose: dict) -> list[int]:
    out = []
    for svc in (compose.get("services") or {}).values():
        for entry in svc.get("ports") or []:
            s = str(entry.get("published") if isinstance(entry, dict) else entry)
            m = re.match(r"^(?:[\d.]+:)?(\d+)(?::\d+)?(?:/\w+)?$", s.strip('"'))
            if m:
                out.append(int(m.group(1)))
    return out


def images(compose: dict) -> dict[str, str]:
    return {name: svc["image"] for name, svc in (compose.get("services") or {}).items()
            if isinstance(svc, dict) and svc.get("image")}


def check_app(app_dir: Path, store_id: str) -> tuple[dict, dict]:
    app = app_dir.name
    m = load_yaml(app_dir / "umbrel-app.yml") or {}
    c = load_yaml(app_dir / "docker-compose.yml") or {}

    # --- manifest
    for key in REQUIRED:
        if key not in m:
            err(app, f"umbrel-app.yml missing `{key}`")
    if m.get("id") != app:
        err(app, f"id `{m.get('id')}` must equal folder name")
    if not app.startswith(f"{store_id}-"):
        err(app, f"app id must start with `{store_id}-`")
    if not semver_like(m.get("manifestVersion")):
        err(app, "manifestVersion must be a version like 1 or 1.1")
    if not isinstance(m.get("version"), str):
        err(app, "version must be a quoted string")
    if not isinstance(m.get("port"), int):
        err(app, "port must be an integer")
    elif m["port"] in RESERVED_PORTS or m["port"] < 1024:
        err(app, f"port {m['port']} is reserved")
    for key in ("website", "support", "repo"):
        if m.get(key) and not str(m[key]).startswith("http"):
            err(app, f"{key} must be a URL")
    if m.get("storage") not in (None, {"dataRoot": "data"}):
        err(app, "storage must be exactly {dataRoot: data}")
    if not m.get("releaseNotes"):
        warn(app, "releaseNotes is empty")
    if m.get("deterministicPassword") and m.get("defaultPassword"):
        err(app, "deterministicPassword and defaultPassword are mutually exclusive")
    services = set((c.get("services") or {}).keys())
    for folder in m.get("folderAccess") or []:
        if len(str(folder.get("note", ""))) > NOTE_MAX:
            err(app, f"folderAccess `{folder.get('id')}` note longer than {NOTE_MAX}")
        for mount in folder.get("mounts") or []:
            if mount.get("service") not in services:
                err(app, f"folderAccess `{folder.get('id')}` targets unknown service `{mount.get('service')}`")
    for env in m.get("environment") or []:
        if len(str(env.get("note", ""))) > NOTE_MAX:
            err(app, f"environment `{env.get('name')}` note longer than {NOTE_MAX}")
        for svc in env.get("services") or []:
            if svc not in services:
                err(app, f"environment `{env.get('name')}` targets unknown service `{svc}`")

    # --- assets
    icon = app_dir / "icon.png"
    if m.get("icon") != f"{RAW_BASE}/{app}/icon.png":
        err(app, f"icon must be {RAW_BASE}/{app}/icon.png")
    if not icon.exists():
        err(app, "icon.png missing")
    elif png_size(icon) != (256, 256):
        err(app, f"icon.png must be a 256x256 PNG, got {png_size(icon)}")
    gallery = m.get("gallery") or []
    if not 2 <= len(gallery) <= 5:
        warn(app, f"gallery has {len(gallery)} images (aim for 3-5)")
    for url in gallery:
        prefix = f"{RAW_BASE}/{app}/"
        if not str(url).startswith(prefix):
            err(app, f"gallery URL not hosted in this repo: {url}")
            continue
        f = app_dir / str(url)[len(prefix):]
        if not f.exists():
            err(app, f"gallery file missing: {f.relative_to(ROOT)}")
        elif jpeg_size(f) != (1440, 900):
            err(app, f"{f.name} must be 1440x900 JPEG, got {jpeg_size(f)}")

    # --- compose
    proxy = (c.get("services") or {}).get("app_proxy") or {}
    env = proxy.get("environment") or {}
    host = str(env.get("APP_HOST", ""))
    if not host.startswith(f"{app}_") or not host.endswith("_1"):
        err(app, f"app_proxy APP_HOST `{host}` must be `{app}_<service>_1`")
    elif host[len(app) + 1:-2] not in services:
        err(app, f"app_proxy APP_HOST points at unknown service `{host}`")
    if not str(env.get("APP_PORT", "")).isdigit():
        err(app, "app_proxy APP_PORT missing")
    for name, svc in (c.get("services") or {}).items():
        if name == "app_proxy":
            continue
        image = svc.get("image", "")
        if not IMAGE_RE.match(image):
            err(app, f"service `{name}` image must be pinned as tag@sha256:<digest> (got `{image}`)")
        elif re.search(r":latest@", image):
            err(app, f"service `{name}` must not track `latest`")
        if svc.get("restart") != "on-failure":
            err(app, f"service `{name}` must use `restart: on-failure`")
        if "platform" in svc:
            err(app, f"service `{name}` must not set `platform:` (rely on multi-arch manifests)")
        if "container_name" in svc:
            err(app, f"service `{name}` must not set container_name (umbreld names containers)")
        for vol in svc.get("volumes") or []:
            src = str(vol).split(":", 1)[0]
            if src.startswith("${APP_DATA_DIR}/data/"):
                rel = src[len("${APP_DATA_DIR}/"):]
                path = app_dir / rel
                if not path.exists() and not (path.parent.exists() and "." in path.name):
                    err(app, f"bind source `{src}` not committed (add {rel}/.gitkeep)")
            elif src.startswith("${UMBREL_ROOT}/") and not src.startswith(
                ("${UMBREL_ROOT}/data/storage/downloads", "${UMBREL_ROOT}/data/storage/Music")
            ):
                err(app, f"unexpected host path `{src}` (use Downloads or Music)")
    return m, c


def compose_config(app_dir: Path) -> None:
    override = {"services": {"app_proxy": {"image": "getumbrel/app-proxy:latest"}}}
    env = dict(os.environ, APP_DATA_DIR=f"/tmp/umbrel/app-data/{app_dir.name}", UMBREL_ROOT="/tmp/umbrel",
               APP_SEED="0" * 64, APP_PASSWORD="0" * 64, DEVICE_DOMAIN_NAME="umbrel.local",
               DEVICE_HOSTNAME="umbrel")
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
        yaml.safe_dump(override, fh)
    try:
        r = subprocess.run(["docker", "compose", "-p", app_dir.name, "-f", str(app_dir / "docker-compose.yml"),
                            "-f", fh.name, "config", "-q"], env=env, capture_output=True, text=True)
        if r.returncode:
            err(app_dir.name, f"docker compose config failed: {r.stderr.strip()}")
    finally:
        os.unlink(fh.name)


def changed_without_bump(app_dir: Path, base: str) -> None:
    def show(path: str) -> str | None:
        r = subprocess.run(["git", "show", f"{base}:{path}"], cwd=ROOT, capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None

    rel = app_dir.name
    old_c, old_m = show(f"{rel}/docker-compose.yml"), show(f"{rel}/umbrel-app.yml")
    if old_c is None or old_m is None:
        return
    new_c = load_yaml(app_dir / "docker-compose.yml")
    new_m = load_yaml(app_dir / "umbrel-app.yml")
    files_changed = old_c != (app_dir / "docker-compose.yml").read_text()
    if (images(yaml.safe_load(old_c)) != images(new_c) or files_changed) and \
            yaml.safe_load(old_m).get("version") == new_m.get("version"):
        err(rel, "docker-compose.yml changed but `version` was not bumped (existing installs would not update)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("apps", nargs="*")
    ap.add_argument("--check-images", action="store_true", help="verify amd64+arm64 in every image index")
    ap.add_argument("--compose", action="store_true", help="run `docker compose config` per app")
    ap.add_argument("--base", help="git ref to compare versions against (PRs)")
    ap.add_argument("--official-store", type=Path, help="path to a getumbrel/umbrel-apps checkout for port checks")
    args = ap.parse_args()

    store = load_yaml(ROOT / "umbrel-app-store.yml")
    store_id = store.get("id")
    if not store_id or store_id == "sparkles":
        errors.append(f"umbrel-app-store.yml: set a real store id (got `{store_id}`)")
    app_dirs = sorted(p for p in ROOT.iterdir() if p.is_dir() and (p / "umbrel-app.yml").exists())
    selected = [d for d in app_dirs if not args.apps or d.name in args.apps]

    ports: dict[int, str] = {}
    for d in app_dirs:
        m, c = check_app(d, store_id) if d in selected else (load_yaml(d / "umbrel-app.yml"),
                                                                load_yaml(d / "docker-compose.yml"))
        for p in [m.get("port")] + published_ports(c or {}):
            if p in ports and ports[p] != d.name:
                err(d.name, f"port {p} already used by {ports[p]}")
            ports.setdefault(p, d.name)
    for p, owner in official_ports(args.official_store).items():
        if p in ports:
            err(ports[p], f"port {p} collides with official app `{owner}`")

    if args.compose:
        for d in selected:
            compose_config(d)
    if args.base:
        for d in selected:
            changed_without_bump(d, args.base)
    if args.check_images:
        reg = Registry()
        for d in selected:
            for svc, image in images(load_yaml(d / "docker-compose.yml")).items():
                try:
                    missing = reg.missing_platforms(image)
                except Exception as exc:  # network/registry errors are failures, not passes
                    err(d.name, f"{svc}: cannot inspect {image}: {exc}")
                    continue
                if missing:
                    err(d.name, f"{svc}: {image} lacks {', '.join(missing)}")
                else:
                    print(f"ok  {d.name}/{svc}: {image.split('@')[0]} amd64+arm64")

    for w in warnings:
        print(f"WARNING {w}")
    for e in errors:
        print(f"ERROR   {e}")
    print(f"{len(selected)} app(s) checked, {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
