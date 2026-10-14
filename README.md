# Music Stack — Umbrel Community App Store

A self-hosted music stack for [umbrelOS](https://umbrel.com): library management, Soulseek
downloads, discovery, playlist mirroring, sonic analysis and listening stats. Every app runs on
**linux/amd64 and linux/arm64**.

**Add the store:** umbrelOS → App Store → ⋯ (top right) → Community App Stores → paste
`https://github.com/Bionded/umbrel-bionded-app-store` → Add.

## Apps

| App | Id | Port | What it does |
|---|---|---|---|
| Lidarr Nightly | `bionded-lidarr-nightly` | 8687 | Library manager (nightly build with the plugin system, e.g. Tubifarry). Coexists with the official Lidarr. |
| slskd | `bionded-slskd` | 5032 + **50310/tcp** | Soulseek client and API used by the other apps. Shares nothing until you set it up. Coexists with other slskd apps. |
| Aurral | `bionded-aurral` | 3041 | Artist/album requests and discovery. Sends requests to Lidarr; can download through slskd. |
| SoulSync | `bionded-soulsync` | 8018 | Mirrors Spotify/Tidal/Deezer playlists through slskd. |
| AudioMuse-AI | `bionded-audiomuse-ai` | 8044 | Sonic analysis and smart playlists for Navidrome (needs AVX2 on amd64). |
| Explo | `bionded-explo` | 7288 | Weekly ListenBrainz discovery playlists. |
| Multi-Scrobbler | `bionded-multi-scrobbler` | 9078 | Routes plays from Navidrome and others to Koito, ListenBrainz and Last.fm. |
| Koito | `bionded-koito` | 4110 | Private ListenBrainz-compatible scrobbler with stats. |
| Pinchflat-NGX | `bionded-pinchflat-ngx` | 8946 | YouTube channel/playlist downloader (enhanced Pinchflat fork, bundled PO-token provider). Coexists with the official Pinchflat. |

Navidrome is the official Umbrel app (`navidrome`) and is configured by you.

```
           requests / discovery                       listening history
 Aurral ──────────────┐                    Navidrome ──► Multi-Scrobbler ──► Koito
 Explo  ──┐           ▼                        ▲   ▲            └──► ListenBrainz ──► Explo / Aurral
 SoulSync ┼──► slskd  Lidarr Nightly ──► /music┘   └── AudioMuse-AI (Subsonic API, plugin)
          └──(Soulseek)──► Downloads/<app> ───────┘
```

## Shared path convention

Every app sees the same two folders at the same container paths:

| Umbrel Files | Container path | Who writes |
|---|---|---|
| **Home → Music** | **`/music`** | **Lidarr Nightly only** (its root folder). Others read it: Aurral (same path as Lidarr), SoulSync, Explo. |
| **Home → Downloads** | **`/downloads`** | Every downloader can read all downloads, but each writes only to its own folder: |
| ↳ `Downloads/slskd/complete`, `…/incomplete` | `/downloads/slskd/…` | slskd. Lidarr (Tubifarry), Aurral, SoulSync and Explo each move **only the transfers they started**. |
| ↳ `Downloads/lidarr` | `/downloads/lidarr` | Lidarr's non-Soulseek Tubifarry downloads (YouTube, Deezer…) before import |
| ↳ `Downloads/aurral` | `/downloads/aurral` | Aurral (flows, playlists, its own albums) |
| ↳ `Downloads/soulsync` | `/app/Transfer` in SoulSync | SoulSync (mirrored playlists) |
| ↳ `Downloads/explo` | `/data` in Explo | Explo (weekly playlists, rotated) |
| ↳ `Downloads/pinchflat` | `/downloads` in Pinchflat-NGX | Pinchflat-NGX (YouTube videos; it only sees this folder) |

Rules that keep apps from fighting:

- Only Lidarr writes the library. Album and artist requests from Aurral go **through Lidarr**, and Aurral reuses Lidarr's files instead of downloading copies.
- Playlist mirroring belongs to **SoulSync**, ListenBrainz discovery to **Explo**. Don't point Aurral's playlist import or flows at the same sources.
- Nothing treats `slskd/complete` as a watch folder.
- `Home/Music` doesn't exist on a fresh umbrelOS. Each app's `hooks/pre-start` creates it (and its download folder) owned by 1000:1000. Existing folders are never changed.
- Each app declares the library as a **Music** folder slot, so you can point it somewhere else (e.g. an external drive) in the app's settings.

## Recommended install order

1. **Navidrome** (official store). Point its library at Home → Music, and add `Downloads/aurral`, `Downloads/soulsync` and `Downloads/explo` as extra libraries if you want those playable.
2. **slskd**: set your Soulseek account under ⋯ → Settings → Advanced settings. Forward TCP 50310 on your router.
3. **Lidarr Nightly**: add root folder `/music`, install the Tubifarry plugin, add slskd as a download client.
4. **Koito**, then **Multi-Scrobbler** (Navidrome → Koito).
5. **Aurral**, **SoulSync**, **Explo**, **AudioMuse-AI**.

## Connecting apps

Use the internal container hostnames: they work between Umbrel apps without leaving the device.

| In app | Setting | Value |
|---|---|---|
| Lidarr Nightly | Root folder | `/music` |
| Lidarr Nightly | Download client "Slskd" (Tubifarry) | `http://bionded-slskd_server_1:5030`, slskd API key¹ (files are picked up from `/downloads/slskd/complete`) |
| Aurral | Lidarr | `http://bionded-lidarr-nightly_server_1:8686` + Lidarr API key² |
| Aurral | slskd | `http://bionded-slskd_server_1:5030` + slskd API key¹ |
| Aurral | Downloads Folder | `/downloads/aurral` (preset) |
| Aurral | Navidrome | `http://navidrome_server_1:4533` + Navidrome user/password |
| Aurral | Koito | `http://bionded-koito_server_1:4110` + Koito API key³ |
| SoulSync | Soulseek | `http://bionded-slskd_server_1:5030` + slskd API key¹; download folder `/app/downloads/slskd/complete`, Transfer `/app/Transfer` |
| SoulSync | Media server | `http://navidrome_server_1:4533` |
| SoulSync | Spotify redirect URI | `http://127.0.0.1:8888/callback` (see the app description for the one-time address swap) |
| Explo | Music system | Subsonic `http://navidrome_server_1:4533` (preset) |
| Explo | slskd / Lidarr | preset URLs + slskd API key¹ / Lidarr API key² |
| Explo | ListenBrainz | username + user token (listenbrainz.org → Settings) |
| AudioMuse-AI | Setup Wizard → Navidrome | `http://navidrome_server_1:4533` |
| Navidrome plugin | AudioMuse URL | `http://bionded-audiomuse-ai_flask_1:8000` + AudioMuse API token |
| Multi-Scrobbler | App settings (env) | `SUBSONIC_*` (Navidrome), `KOITO_*` (URL preset, token³), optional `LZ_*` |
| Any ListenBrainz scrobbler | Koito endpoint | `http://bionded-koito_server_1:4110/apis/listenbrainz/1` (LAN: `http://umbrel.local:4110/apis/listenbrainz/1`) |

¹ Add an API key in slskd: Options → web → authentication → api_keys (16+ characters). slskd's web login starts as slskd / slskd; change it there too.
² Lidarr → Settings → General → Security → API Key.
³ Koito → Settings → API Keys.

## Secrets

No secrets are committed. Per-install secrets come from umbrelOS: `${APP_PASSWORD}` (shown to you as the
app's password, `deterministicPassword: true`) for the Explo and Koito logins,
and `${APP_SEED}` for AudioMuse-AI's Postgres password.

## Development

```sh
pip install requests pyyaml
python3 scripts/validate_store.py --compose --check-images      # schema, compose, ports, assets, amd64+arm64
git clone --depth 1 https://github.com/getumbrel/umbrel-apps /tmp/umbrel-apps
python3 scripts/validate_store.py --official-store /tmp/umbrel-apps   # + port collisions with the official store
```

A packaging-only change (no new upstream version) must bump `version` with a `-patch.N` suffix,
or existing installs won't receive it. umbrelOS offers an update whenever the `version` string changes.

### Updating apps

`scripts/check_updates.py` is run by hand. It reads `updates.yml` (one entry per app: GitHub
releases, registry tags, or a moving nightly tag) and for every app with a newer **stable** version:

1. checks that the image publishes **linux/amd64 and linux/arm64** (otherwise the app is skipped and reported),
2. pins `tag@sha256:digest` in `docker-compose.yml`,
3. bumps `version` and writes short `releaseNotes` from the upstream release in `umbrel-app.yml`.

It only edits files. Review with `git diff`, then commit and push.

```sh
pip install requests pyyaml
python3 scripts/check_updates.py --dry-run              # what's new?
python3 scripts/check_updates.py                        # update every app that has a new version
python3 scripts/check_updates.py --app bionded-koito    # one app; Koito is `hold` and only updates this way
```

- **Koito** is marked `hold` (upstream warns it's unstable). It's reported but only updated when named with `--app`.
- **Lidarr Nightly** follows the `nightly` tag by digest, at most once per 7 days (`--force` overrides), with
  versions like `3.1.6.5102-nightly.20261014`. umbrelOS offers an update whenever the version string differs.
- Set `GITHUB_TOKEN` if you hit GitHub API rate limits.
