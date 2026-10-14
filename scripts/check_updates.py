#!/usr/bin/env python3
"""Check upstream versions and update the app files in place (run by hand).

For every app in updates.yml:
  1. find the newest stable upstream version (GitHub release, registry tag, or a
     moving nightly tag resolved by digest); pre-releases are ignored
  2. skip it unless the image index publishes linux/amd64 AND linux/arm64
  3. pin the new `tag@sha256:digest` in docker-compose.yml, bump `version` in
     umbrel-app.yml and write short `releaseNotes` from the upstream release body

Nothing is committed or pushed: review with `git diff`, then commit and push yourself.

Usage:
  python3 scripts/check_updates.py --dry-run          # only show what is new
  python3 scripts/check_updates.py                    # update every app that has a new version
  python3 scripts/check_updates.py --app bionded-koito  # one app (also needed for `hold` apps)

Environment: GITHUB_TOKEN (optional, avoids GitHub API rate limits).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import Registry, parse_image  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GITHUB_API = "https://api.github.com"
NOTES_MAX_CHARS = 700
NOTES_MAX_ITEMS = 6
NOISE = re.compile(r"^(bump|chore|ci|build|deps?|docs?)\b|dependabot|renovate|^merge (pull request|branch)|"
                   r"^(new contributors|what's changed|full changelog)", re.I)


@dataclass
class Update:
    app: str
    current_version: str
    new_version: str
    image: str
    tag: str
    digest: str
    notes: str
    changelog: str
    services: list[str]
    skipped: str | None = None


# ---------------------------------------------------------------- helpers

def version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v.split("-")[0]))


def strip_patch(v: str) -> str:
    return re.sub(r"-patch\.\d+$", "", v)


def shorten_notes(body: str, version: str, url: str) -> str:
    """Turn a markdown release body into a few plain lines for umbrelOS."""
    items: list[str] = []
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "<", "---", "|", "```", "![", "**Full Changelog")):
            continue
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)          # links -> text
        line = re.sub(r"(\*\*|__|`)", "", line)                       # emphasis/code
        line = re.sub(r"\s*\(?#\d+\)?|\s+by @\S+.*$|\s+in https://\S+", "", line)  # PR refs
        line = re.sub(r"^[-*+]\s+|^\d+\.\s+", "", line)
        line = re.sub(r"^[0-9a-f]{7,40}\s+", "", line)                 # leading commit sha
        if len(line) < 4 or NOISE.match(line):
            continue
        if len(line) > 160:
            line = line[:157].rsplit(" ", 1)[0] + "…"
        items.append(line)
        if len(items) >= NOTES_MAX_ITEMS:
            break
    text = f"Update to {version}."
    if items:
        text += "\n\n\nHighlights:\n" + "\n".join(f"  - {i}" for i in items)
    if len(text) > NOTES_MAX_CHARS:
        text = text[:NOTES_MAX_CHARS].rsplit("\n", 1)[0]
    return f"{text}\n\n\nFull release notes: {url}"


def compose_image(app: str, service: str) -> str:
    compose = yaml.safe_load((ROOT / app / "docker-compose.yml").read_text())
    return compose["services"][service]["image"]


def manifest_version(app: str) -> str:
    return str(yaml.safe_load((ROOT / app / "umbrel-app.yml").read_text())["version"])


# ---------------------------------------------------------------- upstream lookups

class Checker:
    def __init__(self, cfg: dict):
        self.defaults = cfg.get("defaults", {})
        self.reg = Registry()
        self.gh = requests.Session()
        self.gh.headers["Accept"] = "application/vnd.github+json"
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            self.gh.headers["Authorization"] = f"Bearer {token}"

    def _github(self, path: str, **params):
        r = self.gh.get(f"{GITHUB_API}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def latest_release(self, app_cfg: dict) -> tuple[str, str, str] | None:
        rx = re.compile(app_cfg.get("release_regex", self.defaults.get("release_regex")))
        best = None
        for rel in self._github(f"/repos/{app_cfg['repo']}/releases", per_page=30):
            if rel["draft"] or rel["prerelease"]:
                continue
            m = rx.match(rel["tag_name"])
            if m and (best is None or version_key(m.group(1)) > version_key(best[0])):
                best = (m.group(1), rel.get("body") or "", rel["html_url"])
        return best

    def release_for(self, repo: str, version: str) -> tuple[str, str]:
        for tag in (version, f"v{version}"):
            r = self.gh.get(f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}", timeout=30)
            if r.ok:
                return r.json().get("body") or "", r.json()["html_url"]
        return "", f"https://github.com/{repo}/releases"

    def check(self, app: str, app_cfg: dict, today: dt.date, force: bool) -> Update | None:
        services = app_cfg["services"]
        current_image = compose_image(app, services[0])
        current_version = manifest_version(app)
        source = app_cfg["source"]

        if source == "nightly_digest":
            return self.check_nightly(app, app_cfg, current_image, current_version, today, force)
        if source == "github_release":
            found = self.latest_release(app_cfg)
            if not found:
                return None
            version, body, url = found
        elif source == "registry_tags":
            rx = re.compile(app_cfg["tag_regex"])
            versions = [m.group(1) for t in self.reg.tags(app_cfg["image"]) if (m := rx.match(t))]
            if not versions:
                return None
            version = max(versions, key=version_key)
            body, url = self.release_for(app_cfg["repo"], version) if app_cfg.get("repo") else ("", "")
        else:
            raise ValueError(f"{app}: unknown source {source}")

        if version_key(version) <= version_key(strip_patch(current_version)):
            return None
        tag = app_cfg.get("image_tag", "{version}").format(version=version)
        image = f"{app_cfg['image']}:{tag}"
        upd = Update(app, current_version, version, app_cfg["image"], tag, "", "", url, services)
        try:
            upd.digest, _ = self.reg.manifest(image)
            missing = self.reg.missing_platforms(image)
        except requests.HTTPError as exc:
            upd.skipped = f"image {image} not published yet ({exc.response.status_code})"
            return upd
        if missing:
            upd.skipped = f"image {image} lacks {', '.join(missing)}"
            return upd
        upd.notes = shorten_notes(body, version, url)
        return upd

    def check_nightly(self, app, app_cfg, current_image, current_version, today, force) -> Update | None:
        image = app_cfg["image"]
        nightly_digest, _ = self.reg.manifest(f"{image}:{app_cfg['track_tag']}")
        if nightly_digest == parse_image(current_image).digest:
            return None
        m = re.search(r"nightly\.(\d{8})", current_version)
        if m and not force:
            last = dt.datetime.strptime(m.group(1), "%Y%m%d").date()
            wait = int(app_cfg.get("min_interval_days", 7)) - (today - last).days
            if wait > 0:
                upd = Update(app, current_version, "newer nightly", image, "", "", "", "", app_cfg["services"])
                upd.skipped = f"last nightly bump is recent; next in {wait} day(s) (use --force)"
                return upd
        rx = re.compile(app_cfg["tag_regex"])
        candidates = sorted((t for t in self.reg.tags(image) if rx.match(t)),
                            key=lambda t: tuple(int(x) for x in re.findall(r"\d+", t)), reverse=True)
        for t in candidates[:10]:
            if self.reg.manifest(f"{image}:{t}")[0] == nightly_digest:
                upstream = rx.match(t).group(1)
                url = app_cfg.get("changelog", "")
                upd = Update(app, current_version, f"{upstream}-nightly.{today:%Y%m%d}", image, t, nightly_digest,
                             f"Nightly build {upstream} ({t}).\n\n\nChanges: {url}", url, app_cfg["services"])
                missing = self.reg.missing_platforms(f"{image}:{t}")
                if missing:
                    upd.skipped = f"image {image}:{t} lacks {', '.join(missing)}"
                return upd
        return None


# ---------------------------------------------------------------- applying

def apply_update(u: Update) -> None:
    compose_path = ROOT / u.app / "docker-compose.yml"
    text = compose_path.read_text()
    new_ref = f"{u.image}:{u.tag}@{u.digest}"
    text, n = re.subn(rf"(image:\s*){re.escape(u.image)}:[^\s@]+@sha256:[0-9a-f]{{64}}", rf"\g<1>{new_ref}", text)
    if n != len(u.services):
        raise RuntimeError(f"{u.app}: expected {len(u.services)} image line(s) for {u.image}, replaced {n}")
    compose_path.write_text(text)

    manifest_path = ROOT / u.app / "umbrel-app.yml"
    text = manifest_path.read_text()
    text, n = re.subn(r'^version:\s*"[^"]*"', f'version: "{u.new_version}"', text, count=1, flags=re.M)
    if n != 1:
        raise RuntimeError(f"{u.app}: could not update version")
    notes = "\n".join(("  " + line) if line else "" for line in u.notes.splitlines())
    text, n = re.subn(r"^releaseNotes:.*?(?=^\S)", lambda _: f"releaseNotes: >-\n{notes}\n", text,
                      count=1, flags=re.M | re.S)
    if n != 1:
        raise RuntimeError(f"{u.app}: could not update releaseNotes")
    manifest_path.write_text(text)


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="only report, don't change files")
    ap.add_argument("--app", action="append", help="limit to these app ids (repeatable)")
    ap.add_argument("--force", action="store_true", help="ignore min_interval_days (Lidarr nightly)")
    ap.add_argument("--today", help="override today's date (YYYY-MM-DD), for testing")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "updates.yml").read_text())
    checker = Checker(cfg)
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    rows, changed, failures = [], [], 0

    for app, app_cfg in cfg["apps"].items():
        if args.app and app not in args.app:
            continue
        try:
            u = checker.check(app, app_cfg, today, args.force)
            if u is None:
                rows.append((app, "up to date", manifest_version(app)))
            elif u.skipped:
                rows.append((app, f"skipped {u.new_version}", u.skipped))
            elif app_cfg.get("hold") and not args.app:
                rows.append((app, f"held {u.current_version} → {u.new_version}",
                             f"review upstream first, then run with --app {app}"))
            elif args.dry_run:
                rows.append((app, f"new {u.current_version} → {u.new_version}", f"{u.image}:{u.tag}"))
            else:
                apply_update(u)
                changed.append(app)
                rows.append((app, f"UPDATED {u.current_version} → {u.new_version}", u.changelog))
        except Exception as exc:  # keep going with the other apps
            failures += 1
            rows.append((app, "error", str(exc)))

    width = max(len(r[0]) for r in rows) if rows else 0
    for app, status, detail in rows:
        print(f"{app:<{width}}  {status:<45}  {detail}")
    if changed:
        print(f"\nUpdated {len(changed)} app(s). Review with `git diff`, then commit and push.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
