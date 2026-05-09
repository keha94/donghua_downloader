# Donghua Downloader 📥

A **FastAPI** web app to manage donghua shows and pull new episodes into a library folder. Two show types:

1. **animexin** (default) — scrape [animexin.dev](https://animexin.dev), resolve Mediafire links, download with HTTP + Playwright.
2. **rss_qbittorrent** — poll an RSS feed, add matching magnets/torrents to [qBittorrent](https://www.qbittorrent.org/) via [qbittorrent-api](https://github.com/rmartin16/qbittorrent-api), then move finished files into the same library layout as type (1).

Core logic lives in `downloader.py` and `rss_qbittorrent.py`; the UI and HTTP API live under `web_gui/`.

---

## 🧰 Requirements

- Python 3.9+ (see `pyproject.toml`)
- [Poetry](https://python-poetry.org/docs/#installation)
- Optional: `xvfb` on headless Linux if you ever disable headless Playwright in `config.json`

---

## 📦 Installation

1. **Clone the project**

```bash
git clone https://github.com/sharingiscaring42/donghua-downloader.git
cd donghua-downloader
```

2. **Install dependencies** (creates `.venv` in the project — see `poetry.toml`)

```bash
poetry install
poetry run playwright install
```

The repo includes `poetry.lock`. After you change `pyproject.toml`, run `poetry lock` and commit the updated lock file.

3. **Optional:** `poetry shell` so you can omit the `poetry run` prefix.

4. **Optional: headless Linux** — if Playwright misbehaves without a display:

```bash
sudo apt install xvfb
```

Or set `"headless": false` in `config.json` and use a real display.

5. **`config.json`** — at minimum set where finished library files live, and optionally qBittorrent:

```json
{
  "base_folder_download": "/downloads",
  "qbittorrent": {
    "host": "127.0.0.1",
    "port": 8080,
    "username": "",
    "password": "",
    "tag_prefix": "donghua"
  }
}
```

Add **`"VERIFY_WEBUI_CERTIFICATE": false`** under **`qbittorrent`** if the Web UI uses HTTPS with a certificate your machine does not trust.

### Show types quick reference

| Kind | Purpose | Main config fields |
|------|---------|--------------------|
| `animexin` | Site scrape + direct download | `link` (series page), optional `thetvdb_id` |
| `rss_qbittorrent` | Feed + qBittorrent | `rss_url`, **`thetvdb_id`** (required), optional `show_name_regex`, `episode_regex`, `season` |

Omit **`type`** (or use `animexin`) for the classic animexin flow. For RSS entries, **`type`** must be **`rss_qbittorrent`** (or **`rss`**). In the web UI, click **Add show**, then switch to the **RSS · qBittorrent** tab in the modal, or call **`POST /api/shows/rss`**.

**RSS matching:** Optional regexes pinpoint feed titles; if **`show_name_regex`** is omitted, titles are matched when any Skyhook-derived name (title, clean/sort titles, alternate titles/aliases when present in the payload) occurs as a substring (case-insensitive). **`episode_regex`** should expose the episode number in its **first capturing group**; if omitted, common patterns (`S01E02`, `Episode 3`, etc.) are tried.

**qBittorrent connection:** Use **`qbittorrent`** in `config.json`, or override host/port/credentials with the **`QBITTORRENT_*`** variables (see [Environment variables](#environment-variables)). Torrents added for RSS shows get tags like **`{tag_prefix}_{show_index}`** (default **`tag_prefix`**: `donghua`). The client downloads to its own folders; when a job completes, this app finds the largest video file and **moves** it into **`base_folder_download`** using **TheTVDB** naming when **`thetvdb_id`** resolves via Skyhook (same behaviour as animexin with an ID).

**qBittorrent save path (NAS / remote client):** The Web API returns paths as **qBittorrent’s host** sees them (e.g. `/volume1/Downloads` on a NAS). This app runs elsewhere and must read and move files via **your** mount (e.g. `/Volumes/MyNAS/Downloads`). Map prefixes under **`qbittorrent`** using either a list (longest `from` wins) or a single pair:

```json
"qbittorrent": {
  "host": "192.168.1.10",
  "port": 8080,
  "save_path_rewrite": [
    { "from": "/volume1/Downloads", "to": "/Volumes/MyNAS/Downloads" }
  ]
}
```

Alternatively: **`save_path_from`** and **`save_path_to`**, or environment variables **`QBITTORRENT_SAVE_PATH_FROM`** / **`QBITTORRENT_SAVE_PATH_TO`** (see [Environment variables](#environment-variables)). Set `from` to the prefix exactly as the API reports (check a torrent’s save path in the Web UI if unsure).

Use a **normal filesystem path** for `to` (e.g. **`/Volumes/YourNAS/Movies/Others`** on macOS where the share is mounted). Do **not** use **`afp://`** or **`smb://`** URLs — Python’s path APIs cannot open those, so imports will fail even if Finder shows the file.

**Library layout** (Skyhook/TheTVDB when metadata loads; otherwise scraped slugs from config):

Example layout:

```text
├── Donghua/
│   ├── Battle.Through.The.Heavens/
│   │   └── Season.05/
│   │       ├── Battle.Through.The.Heavens.S05E138.mp4
│   │       └── ...
│   └── Renegade.Immortal/
│       └── Season.01/
│           └── Renegade.Immortal.S01E072.mp4
```

Add shows (via **Add show** → modal), edit `last_ep`, and remove entries **in the web UI**. Use **Run full downloader** to run `run_downloads()` (RSS poll + site scraping + downloads) in a background thread. Use **RSS import pending** to run only the pending-import pass (`run_rss_pending_import_pass`) — same logic as the periodic RSS import job. Logs go to the **Activity log**. At most one full downloader job and one RSS import job at a time (they may run together). Enable **Debug logging** in the UI (or set **`debug`** in `config.json`) for extra RSS diagnostics when nothing matches.

---

## Environment variables

Variables are read with `os.environ` where noted. If a variable is **unset**, behaviour falls through to **`config.json`** (when that key exists) and then to the **effective default** in the table.

| Variable | Effective default when unset | Purpose |
|----------|------------------------------|---------|
| `CONFIG_PATH` | `<repo>/config.json` (repository root; override with this variable) | JSON config file for the web app and downloader. |
| `DOWNLOAD_SCHEDULE_SECONDS` | `3600` | Seconds between automatic **full downloader** passes (`run_downloads`) while the server runs and that job is idle. **`0`** disables. The loop waits this long **before the first** scheduled run, then repeats. |
| `RSS_IMPORT_SCHEDULE_SECONDS` | `1800` | Seconds between **RSS pending-import** passes (finished qBittorrent jobs → library via `run_rss_pending_import_pass`; no RSS feed poll). **`0`** disables. The first pass runs after an initial ~**45s** delay, then every interval. |
| `TORRENT_PROGRESS_POLL_SECONDS` | `2` | Interval for polling qBittorrent so the **Download progress** bar updates after the downloader thread has exited. **`0`** disables background polling. |
| `SKYHOOK_BASE_URL` | `config.json` → `skyhook_base_url`, else **`https://skyhook.sonarr.tv/v1/tvdb`** | Base URL for Sonarr Skyhook (TheTVDB metadata, no API key). |
| `SKYHOOK_LANGUAGE` | `config.json` → `skyhook_language`, else **`en`** | Skyhook language segment (e.g. `en`, `zh`). |
| `QBITTORRENT_HOST` | `config.json` → `qbittorrent.host`, else **`127.0.0.1`** | qBittorrent Web UI host (RSS show type). |
| `QBITTORRENT_PORT` | `config.json` → `qbittorrent.port`, else **`8080`** | qBittorrent Web UI port (integer). Invalid values are ignored. |
| `QBITTORRENT_USERNAME` | `config.json` → `qbittorrent.username`, else **empty** | Web UI username. If the variable is **set** in the environment (even to an empty string), that value overrides `config.json`. |
| `QBITTORRENT_PASSWORD` | `config.json` → `qbittorrent.password`, else **empty** | Web UI password. If the variable is **set** in the environment, that value overrides `config.json` (including empty string). |
| `QBITTORRENT_VERIFY_CERT` | TLS verification follows **`qbittorrent.VERIFY_WEBUI_CERTIFICATE`** in `config.json` if present; otherwise **HTTPS verification is on** (qbittorrent-api default). | When `VERIFY_WEBUI_CERTIFICATE` is omitted from `config.json`, set this to **`0`**, **`false`**, or **`no`** (case-insensitive) to disable certificate verification for the Web UI. |
| `QBITTORRENT_SAVE_PATH_FROM` | *(none)* — use **`qbittorrent.save_path_rewrite`** or **`save_path_from`** / **`save_path_to`** in `config.json` | Path prefix as reported by qBittorrent (e.g. NAS filesystem). Used with **`QBITTORRENT_SAVE_PATH_TO`** to rewrite API paths before import. |
| `QBITTORRENT_SAVE_PATH_TO` | *(none)* | Local **filesystem** path for the same tree (mount point, bind mount). Must not be an **`afp://`** or **`smb://`** URL — use **`/Volumes/...`** (macOS) or the path inside Docker after `-v` mounting the share. |

**Not read by application code** but relevant when running under Docker or systemd:

| Variable | Typical value | Purpose |
|----------|-----------------|---------|
| `PYTHONUNBUFFERED` | *(Python default)*; Docker image sets **`1`** | When `1`, stdout/stderr are unbuffered (snappier logs). |

---

## ▶️ Run the app

From the project root:

```bash
poetry run uvicorn web_gui.app:app --reload --host 127.0.0.1 --port 8765
```

RSS shows need a **reachable qBittorrent Web UI** from wherever this process runs (e.g. localhost, or **`host.docker.internal`** from Docker if qBittorrent is on the host). Adjust **`qbittorrent.host`** accordingly.

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Use `--host 127.0.0.1` unless you intend to expose the app on your LAN.

**Config path** — same as `CONFIG_PATH` in [Environment variables](#environment-variables); example:

```bash
CONFIG_PATH=/path/to/config.json poetry run uvicorn web_gui.app:app --host 127.0.0.1 --port 8765
```

**Built-in schedules** — see **`DOWNLOAD_SCHEDULE_SECONDS`** and **`RSS_IMPORT_SCHEDULE_SECONDS`** in [Environment variables](#environment-variables). The download scheduler starts a pass only when no full downloader job is already running.

**Manual triggers** (same as the UI buttons):

```bash
curl -sS -X POST http://127.0.0.1:8765/api/download
curl -sS -X POST http://127.0.0.1:8765/api/rss-import
```

Returns **`202`** when started, **`409`** if that job type is already running. Optional: `GET /api/download/status` and `GET /api/rss-import/status` for JSON state.

The Activity log mirrors server logging. When using Docker, **`PYTHONUNBUFFERED=1`** is set for snappier output (see [Environment variables](#environment-variables)).

---

## 🐳 Docker

Playwright + Chromium are in the image; dependencies are installed with **Poetry** during the build.

**Requirements:** [Docker](https://docs.docker.com/get-docker/) with Compose v2.

1. In `config.json`, set `"base_folder_download": "/downloads"` to match the volume below.

2. Create the host download directory:

```bash
mkdir -p downloads
```

3. Build and run:

```bash
docker compose build
docker compose up
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The `app` service mounts `./config.json` and `./downloads`, and sets **`DOWNLOAD_SCHEDULE_SECONDS`** in `docker-compose.yml` (default **`3600`**; see [Environment variables](#environment-variables)); adjust as needed. Other variables (**`RSS_IMPORT_SCHEDULE_SECONDS`**, **`TORRENT_PROGRESS_POLL_SECONDS`**, **`QBITTORRENT_*`**, save-path mapping, etc.) use code defaults unless you add them under **`environment:`** or use a [Compose override file](https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/) (e.g. `docker compose -f docker-compose.yml -f docker-compose.override.yml up`).

**Without Compose:**

```bash
docker build -t donghua-downloader .
docker run --rm -p 127.0.0.1:8765:8765 \
  -e DOWNLOAD_SCHEDULE_SECONDS=3600 \
  -e RSS_IMPORT_SCHEDULE_SECONDS=1800 \
  -v "$(pwd)/config.json:/app/config.json" \
  -v "$(pwd)/downloads:/downloads" \
  donghua-downloader
```

If you change the downloads mount path, update `base_folder_download` in `config.json` to the path **inside** the container.

---

## 📝 Example log (downloader)

```text
---------------------------

📺 Processing: Throne of Seal

📋 Episodes to download (EP > 150):
  - Episode 151

➡️ Episode 151 - Page: https://animexin.dev/throne-of-seal-episode-151-indonesia-english-sub/
🔍 Navigating to: https://www.mediafire.com/file/.../file
✅ Found direct link: https://download....mp4
.../Throne.of.Seal/Season.01/Throne.of.Seal.S01E151.mp4: 100%|████| 490M/490M [00:40<00:00, 12.2MB/s]
✅ Downloaded: Throne.of.Seal.S01E151.mp4

💾 Config updated!
```

RSS/qBittorrent passes log different lines (feed match, add to qBittorrent, relocation). Inspect the **Activity log** after **Run full downloader**, after **RSS import pending**, or after a scheduled import pass.
