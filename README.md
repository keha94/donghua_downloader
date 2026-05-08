# Donghua Downloader 📥

A **FastAPI** web app to manage donghua shows and download episodes from [animexin.dev](https://animexin.dev) (Mediafire links, Playwright for direct URLs). Core logic lives in `downloader.py`; the UI and HTTP API live under `web_gui/`.

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

5. **`config.json`** — set where files are stored:

```json
{
  "base_folder_download": "/downloads"
}
```

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

Add shows, edit `last_ep`, and remove entries **in the web UI**. Use **Run downloader** on the same page to fetch new episodes (runs `run_downloads()` in a background thread; logs go to the **Activity log**). Only one downloader run at a time.

---

## ▶️ Run the app

From the project root:

```bash
poetry run uvicorn web_gui.app:app --reload --host 127.0.0.1 --port 8765
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Use `--host 127.0.0.1` unless you intend to expose the app on your LAN.

**Another config file:**

```bash
CONFIG_PATH=/path/to/config.json poetry run uvicorn web_gui.app:app --host 127.0.0.1 --port 8765
```

**Built-in schedule:** with the server running, an asyncio task starts a download pass on a fixed interval **when the downloader is idle** (default **3600** seconds = 1 hour). The first tick is **one hour after startup**, then every hour. Override with `DOWNLOAD_SCHEDULE_SECONDS` (any positive integer, in seconds), or set **`DOWNLOAD_SCHEDULE_SECONDS=0`** to turn the schedule off.

**Manual trigger** (same as the “Run downloader” button in the UI):

```bash
curl -sS -X POST http://127.0.0.1:8765/api/download
```

The Activity log mirrors server logging; use `PYTHONUNBUFFERED=1` under Docker or systemd if you want line-buffered output.

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

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The `app` service mounts `./config.json` and `./downloads`, and sets `DOWNLOAD_SCHEDULE_SECONDS` (see **Built-in schedule** above); adjust in `docker-compose.yml` if needed.

**Without Compose:**

```bash
docker build -t donghua-downloader .
docker run --rm -p 127.0.0.1:8765:8765 \
  -e DOWNLOAD_SCHEDULE_SECONDS=3600 \
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
