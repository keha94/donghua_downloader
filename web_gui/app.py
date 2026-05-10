"""Local web UI to manage donghua shows in config.json."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from downloader import (  # noqa: E402
    create_config_from_link,
    create_rss_show_entry,
    load_config as load_config_file,
    normalize_legacy_show_entry,
    run_downloads,
    save_config as save_config_file,
    set_download_progress_hook,
    set_torrent_progress_hook,
)
from rss_qbittorrent import (
    poll_all_shows_rss_torrent_gui_progress,
    run_rss_pending_import_pass,
)
from tvdb_naming import (  # noqa: E402
    episode_slug_prefix,
    folder_series_for_show,
    normalize_series_slug,
)

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", REPO_ROOT / "config.json")).resolve()

_log_lines: deque[str] = deque(maxlen=500)
_log_lock = threading.Lock()

T = TypeVar("T")


def _init_logging() -> logging.Logger:
    logger = logging.getLogger("web_gui")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    class _MemoryHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            line = self.format(record)
            with _log_lock:
                _log_lines.append(line)

    mem = _MemoryHandler()
    mem.setFormatter(fmt)
    logger.addHandler(mem)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


log = _init_logging()

_download_lock = threading.Lock()
_download_state: dict[str, Any] = {
    "running": False,
    "error": None,
    "started_at": None,
    "finished_at": None,
}
_download_thread: Optional[threading.Thread] = None

_rss_import_lock = threading.Lock()
_rss_import_state: dict[str, Any] = {
    "running": False,
    "error": None,
    "started_at": None,
    "finished_at": None,
}
_rss_import_thread: Optional[threading.Thread] = None

_episode_progress_lock = threading.Lock()
_episode_progress_items: dict[str, dict[str, Any]] = {}


def _http_progress_key(ev: dict[str, Any]) -> str:
    show = str(
        ev.get("show_link")
        or ev.get("show_name")
        or ev.get("series")
        or "show"
    ).strip()
    ep = str(ev.get("episode") or "")
    fn = str(ev.get("filename") or "")
    return f"http:{show}:{ep}:{fn}".lower()


def _torrent_progress_key(ev: dict[str, Any]) -> str:
    h = str(ev.get("info_hash") or "").strip().lower()
    if h:
        return f"torrent:{h}"
    show = str(ev.get("show_name") or "show").strip()
    ep = str(ev.get("episode") or "")
    fn = str(ev.get("filename") or "")
    return f"torrent:{show}:{ep}:{fn}".lower()


def _serialize_progress_item(item: dict[str, Any]) -> dict[str, Any]:
    active = bool(item.get("active"))
    source = item.get("source")
    downloaded = int(item.get("downloaded") or 0)
    total = int(item.get("total") or 0)
    qbit_dl = int(item.get("dlspeed") or 0)
    t0 = item.get("_t0")

    speed_bps = 0
    if active:
        if source == "torrent" and qbit_dl > 0:
            speed_bps = qbit_dl
        elif t0 is not None and downloaded > 0:
            speed_bps = int(downloaded / max(time.monotonic() - float(t0), 1e-6))

    body: dict[str, Any] = {
        "id": str(item.get("id") or ""),
        "active": active,
        "source": source,
        "show_link": item.get("show_link"),
        "show_name": item.get("show_name"),
        "series": item.get("series"),
        "episode": item.get("episode"),
        "filename": item.get("filename"),
        "downloaded": downloaded,
        "total": total,
        "speed_bps": speed_bps,
        "torrent_state": item.get("torrent_state"),
        "torrent_eta": item.get("torrent_eta"),
    }
    if active:
        if total > 0:
            body["percent"] = min(100.0, 100.0 * downloaded / total)
            body["indeterminate"] = False
        else:
            body["percent"] = None
            body["indeterminate"] = True
    else:
        body["percent"] = None
        body["indeterminate"] = False
    return body


def _reset_episode_download_progress() -> None:
    with _episode_progress_lock:
        _episode_progress_items.clear()


def _on_download_file_event(ev: dict[str, Any]) -> None:
    kind = ev.get("kind")
    key = _http_progress_key(ev)
    with _episode_progress_lock:
        if kind == "file_start":
            _episode_progress_items[key] = {
                "id": key,
                "active": True,
                "source": "http",
                "show_link": ev.get("show_link"),
                "show_name": ev.get("show_name"),
                "series": ev.get("series"),
                "episode": ev.get("episode"),
                "filename": ev.get("filename"),
                "downloaded": 0,
                "total": int(ev.get("total") or 0),
                "_t0": time.monotonic(),
                "dlspeed": 0,
                "torrent_state": None,
                "torrent_eta": None,
            }
        elif kind == "file_progress":
            item = _episode_progress_items.get(key)
            if not item:
                return
            item["downloaded"] = int(ev.get("downloaded") or 0)
            item["total"] = int(ev.get("total") or 0)
        elif kind == "file_complete":
            _episode_progress_items.pop(key, None)
        elif kind == "file_error":
            _episode_progress_items.pop(key, None)

    if kind == "file_start":
        log.info(
            "[download] Downloading EP%s — %s",
            ev.get("episode"),
            ev.get("filename"),
        )


def _on_torrent_progress_event(ev: dict[str, Any]) -> None:
    kind = str(ev.get("kind") or "")
    if kind == "torrent_snapshot":
        active_hashes = {
            str(h).strip().lower()
            for h in (ev.get("active_hashes") or [])
            if str(h).strip()
        }
        with _episode_progress_lock:
            stale = [
                key
                for key, item in _episode_progress_items.items()
                if item.get("source") == "torrent"
                and not (
                    key.startswith("torrent:")
                    and key.removeprefix("torrent:") in active_hashes
                )
            ]
            for key in stale:
                _episode_progress_items.pop(key, None)
        return
    if kind != "torrent_progress":
        return
    key = _torrent_progress_key(ev)
    with _episode_progress_lock:
        existing = _episode_progress_items.get(key) or {}
        _episode_progress_items[key] = {
            "id": key,
            "active": True,
            "source": "torrent",
            "show_link": None,
            "show_name": ev.get("show_name"),
            "series": ev.get("show_name"),
            "episode": ev.get("episode"),
            "filename": ev.get("filename"),
            "downloaded": int(ev.get("downloaded") or 0),
            "total": int(ev.get("total") or 0),
            "_t0": existing.get("_t0") or time.monotonic(),
            "dlspeed": int(ev.get("dlspeed") or 0),
            "torrent_state": ev.get("state"),
            "torrent_eta": ev.get("eta"),
        }
    h = str(ev.get("info_hash") or "")
    log.debug(
        "[torrent-ui] hook %s EP%s %.1f%% state=%s bytes=%s/%s hash=%s…",
        ev.get("show_name"),
        ev.get("episode"),
        float(ev.get("percent") or 0),
        ev.get("state"),
        ev.get("downloaded"),
        ev.get("total"),
        h[:8] if h else "?",
    )


def _clear_torrent_progress_ui_state() -> bool:
    """Hide torrent row when nothing is actively downloading (poller found no jobs)."""
    with _episode_progress_lock:
        torrent_keys = [
            key
            for key, item in _episode_progress_items.items()
            if item.get("source") == "torrent"
        ]
        if not torrent_keys:
            return False
        for key in torrent_keys:
            _episode_progress_items.pop(key, None)
        return True


_torrent_poll_last_qbit_warn_ts: float = 0.0
_torrent_poll_idle_log_counter: int = 0
_torrent_poll_logged_loop_start: bool = False


async def _torrent_gui_poll_loop(interval_seconds: float) -> None:
    """Poll qBittorrent for RSS jobs so the progress bar updates after run_downloads() exits."""
    global _torrent_poll_last_qbit_warn_ts, _torrent_poll_idle_log_counter
    global _torrent_poll_logged_loop_start
    await asyncio.sleep(0.3)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            cfg = load_config()
            dbg = bool(cfg.get("debug"))
            if dbg and not _torrent_poll_logged_loop_start:
                _torrent_poll_logged_loop_start = True
                log.info("[torrent-poll] loop started (every %ss)", interval_seconds)

            polled, detail = poll_all_shows_rss_torrent_gui_progress(cfg)
            if polled is True:
                _torrent_poll_idle_log_counter = 0
                if dbg:
                    log.info("[torrent-poll] %s", detail)
            elif polled is False:
                _torrent_poll_idle_log_counter += 1
                if dbg and (
                    _torrent_poll_idle_log_counter <= 3
                    or _torrent_poll_idle_log_counter % 12 == 0
                ):
                    log.info("[torrent-poll] %s", detail)
                cleared = _clear_torrent_progress_ui_state()
                if dbg and cleared:
                    log.info(
                        "[torrent-poll] cleared torrent progress card (nothing actively downloading)"
                    )
            else:
                now = time.monotonic()
                if dbg and now - _torrent_poll_last_qbit_warn_ts >= 30.0:
                    _torrent_poll_last_qbit_warn_ts = now
                    log.warning("[torrent-poll] %s", detail)
        except Exception as exc:
            if bool(load_config().get("debug")):
                log.warning("[torrent-poll] exception: %s", exc, exc_info=True)


class _LineTee(io.TextIOBase):
    """Copy writes to *orig* and log each line to *logger* (for downloader output)."""

    encoding = "utf-8"

    def __init__(self, orig: Any, logger: logging.Logger, prefix: str) -> None:
        super().__init__()
        self._orig = orig
        self._log = logger
        self._buf = ""
        self._prefix = prefix

    def write(self, s: Any) -> int:
        if not isinstance(s, str):
            s = str(s)
        self._orig.write(s)
        self._buf += s
        while "\n" in self._buf:
            line, _, rest = self._buf.partition("\n")
            self._buf = rest
            if line.strip():
                self._log.info("%s%s", self._prefix, line.rstrip("\r"))
        return len(s)

    def flush(self) -> None:
        self._orig.flush()

    def drain_remaining(self) -> None:
        rest = self._buf.strip()
        self._buf = ""
        if rest:
            self._log.info("%s%s", self._prefix, rest.rstrip("\r"))

    def isatty(self) -> bool:
        return getattr(self._orig, "isatty", lambda: False)()

    def fileno(self) -> int:
        return int(self._orig.fileno())


def _try_start_download_job(source: str) -> bool:
    """Start the downloader thread if not already running. Returns True if a new job was started."""
    global _download_thread
    with _download_lock:
        if _download_state["running"]:
            log.info("Download start skipped (%s): already running", source)
            return False
        _download_state["running"] = True
        _download_state["error"] = None
        _download_state["started_at"] = datetime.now(timezone.utc).isoformat()
        _download_state["finished_at"] = None
        _reset_episode_download_progress()
    thr = threading.Thread(
        target=_download_worker_main,
        name="donghua-downloader",
        daemon=True,
    )
    thr.start()
    _download_thread = thr
    log.info("Download job started (%s)", source)
    return True


def _download_worker_main() -> None:
    cfg = str(CONFIG_PATH)
    log.info("Downloader thread started (config=%s)", cfg)

    set_download_progress_hook(_on_download_file_event)
    tee_out = _LineTee(sys.__stdout__, log, "[download] ")
    tee_err = _LineTee(sys.__stderr__, log, "[download:err] ")
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = tee_out, tee_err
        run_downloads(cfg)
    except BaseException as exc:
        with _download_lock:
            _download_state["error"] = str(exc)
        log.error("Downloader crashed: %s", exc, exc_info=True)
    finally:
        set_download_progress_hook(None)
        sys.stdout, sys.stderr = old_out, old_err
        tee_out.drain_remaining()
        tee_err.drain_remaining()
        with _download_lock:
            _download_state["running"] = False
            _download_state["finished_at"] = datetime.now(timezone.utc).isoformat()
        log.info("Downloader thread finished")


def _try_start_rss_import_job(source: str) -> bool:
    """Start the RSS pending-import thread if not already running."""
    global _rss_import_thread
    with _rss_import_lock:
        if _rss_import_state["running"]:
            log.info("RSS import start skipped (%s): already running", source)
            return False
        _rss_import_state["running"] = True
        _rss_import_state["error"] = None
        _rss_import_state["started_at"] = datetime.now(timezone.utc).isoformat()
        _rss_import_state["finished_at"] = None
    thr = threading.Thread(
        target=_rss_import_worker_main,
        name="donghua-rss-import",
        daemon=True,
    )
    thr.start()
    _rss_import_thread = thr
    log.info("RSS pending-import job started (%s)", source)
    return True


def _rss_import_worker_main() -> None:
    cfg = str(CONFIG_PATH)
    log.info("RSS import thread started (config=%s)", cfg)
    tee_out = _LineTee(sys.__stdout__, log, "[rss-import] ")
    tee_err = _LineTee(sys.__stderr__, log, "[rss-import:err] ")
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = tee_out, tee_err
        run_rss_pending_import_pass(cfg)
    except BaseException as exc:
        with _rss_import_lock:
            _rss_import_state["error"] = str(exc)
        log.error("RSS import crashed: %s", exc, exc_info=True)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        tee_out.drain_remaining()
        tee_err.drain_remaining()
        with _rss_import_lock:
            _rss_import_state["running"] = False
            _rss_import_state["finished_at"] = datetime.now(timezone.utc).isoformat()
        log.info("RSS import thread finished")


def _run_with_print_capture(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run *fn* while capturing stdout/stderr so print() shows in the activity log + terminal."""
    out_buf, err_buf = StringIO(), StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        result = fn(*args, **kwargs)
    for line in out_buf.getvalue().splitlines():
        if line.strip():
            log.info("[stdout] %s", line)
    for line in err_buf.getvalue().splitlines():
        if line.strip():
            log.warning("[stderr] %s", line)
    return result


async def _download_schedule_loop(interval_seconds: int) -> None:
    """Wake every *interval_seconds* and start a download pass if idle."""
    while True:
        await asyncio.sleep(interval_seconds)
        if not _try_start_download_job("hourly schedule"):
            log.info("Hourly schedule: skipped (downloader still running)")


async def _rss_import_schedule_loop(interval_seconds: int) -> None:
    """Periodically move finished RSS torrent files into the library (without full RSS poll)."""
    await asyncio.sleep(45)
    while True:
        await asyncio.sleep(interval_seconds)
        if not _try_start_rss_import_job("schedule"):
            log.debug("RSS import schedule: skipped (already running)")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    log.info("Web GUI started (config: %s)", CONFIG_PATH)
    set_torrent_progress_hook(_on_torrent_progress_event)
    torrent_poll_sec = float(os.environ.get("TORRENT_PROGRESS_POLL_SECONDS", "2"))
    torrent_poll_task: Optional[asyncio.Task] = None
    if torrent_poll_sec > 0:
        torrent_poll_task = asyncio.create_task(_torrent_gui_poll_loop(torrent_poll_sec))
        if bool(load_config().get("debug")):
            log.info(
                "[torrent-poll] torrent progress hook registered (RSS emits update /api/download/progress)"
            )
            log.info(
                "Torrent progress poll: every %s s (set TORRENT_PROGRESS_POLL_SECONDS=0 to disable)",
                torrent_poll_sec,
            )
    schedule_sec = int(os.environ.get("DOWNLOAD_SCHEDULE_SECONDS", "3600"))
    sched_task: Optional[asyncio.Task] = None
    if schedule_sec > 0:
        sched_task = asyncio.create_task(_download_schedule_loop(schedule_sec))
        log.info("Download schedule: every %s seconds", schedule_sec)
    else:
        log.info("Download schedule: disabled (DOWNLOAD_SCHEDULE_SECONDS=0)")
    rss_import_sec = int(os.environ.get("RSS_IMPORT_SCHEDULE_SECONDS", "1800"))
    rss_import_task: Optional[asyncio.Task] = None
    if rss_import_sec > 0:
        rss_import_task = asyncio.create_task(_rss_import_schedule_loop(rss_import_sec))
        log.info(
            "RSS pending-import pass: every %s s (%s min); set RSS_IMPORT_SCHEDULE_SECONDS=0 to disable",
            rss_import_sec,
            rss_import_sec // 60,
        )
    else:
        log.info("RSS pending-import pass: disabled (RSS_IMPORT_SCHEDULE_SECONDS=0)")
    try:
        yield
    finally:
        set_torrent_progress_hook(None)
        if torrent_poll_task is not None:
            torrent_poll_task.cancel()
            try:
                await torrent_poll_task
            except asyncio.CancelledError:
                pass
        if sched_task is not None:
            sched_task.cancel()
            try:
                await sched_task
            except asyncio.CancelledError:
                pass
        if rss_import_task is not None:
            rss_import_task.cancel()
            try:
                await rss_import_task
            except asyncio.CancelledError:
                pass
        log.info("Web GUI shutting down")


app = FastAPI(title="Donghua Downloader", lifespan=_lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
static_dir = Path(__file__).parent / "static"
if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> RedirectResponse:
    """Browsers often request /favicon.ico; the file lives under /static/."""
    return RedirectResponse(url="/static/favicon.ico", status_code=307)


@app.exception_handler(HTTPException)
async def _log_http_exception(request: Request, exc: HTTPException):
    log.warning("%s %s -> %s", request.method, request.url.path, exc.detail)
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def _log_validation(request: Request, exc: RequestValidationError):
    log.warning("Validation error on %s: %s", request.url.path, exc.errors())
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(Exception)
async def _log_unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, HTTPException):
        return await http_exception_handler(request, exc)
    log.error(
        "Unhandled %s %s: %s",
        request.method,
        request.url.path,
        exc,
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}"},
    )


def _default_config() -> dict[str, Any]:
    return {
        "base_folder_download": str(REPO_ROOT / "downloads"),
        "test": False,
        "headless": True,
        "skyhook_base_url": "",
        "skyhook_language": "en",
        "thetvdb_include_episode_title": True,
        "debug": False,
        "qbittorrent": {
            "host": "127.0.0.1",
            "port": 8080,
            "username": "",
            "password": "",
            "tag_prefix": "donghua",
        },
        "list": [],
    }


def _ensure_config_shape(data: dict[str, Any]) -> dict[str, Any]:
    base = _default_config()
    base.update({k: data[k] for k in base if k in data})
    if "list" not in data or not isinstance(data["list"], list):
        base["list"] = []
    else:
        base["list"] = data["list"]
    qb_def = dict(base["qbittorrent"] or {})
    if isinstance(data.get("qbittorrent"), dict):
        qb_def.update(data["qbittorrent"])
        base["qbittorrent"] = qb_def
    for key in (
        "base_folder_download",
        "test",
        "headless",
        "skyhook_base_url",
        "skyhook_language",
        "thetvdb_include_episode_title",
        "debug",
    ):
        if key in data:
            base[key] = data[key]
    for show in base.get("list") or []:
        if isinstance(show, dict):
            normalize_legacy_show_entry(show)
    return base


def normalize_link(link: str) -> str:
    return link.strip().rstrip("/")


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        return _default_config()
    return _ensure_config_shape(load_config_file(str(CONFIG_PATH)))


def save_config(config: dict[str, Any]) -> None:
    save_config_file(config, str(CONFIG_PATH))


def find_entry_by_link(
    config_list: list[dict[str, Any]], link: str
) -> Optional[dict[str, Any]]:
    n = normalize_link(link)
    for entry in config_list:
        if normalize_link(entry["link"]) == n:
            return entry
    return None


def normalize_feed_url(url: str) -> str:
    return url.strip().rstrip("/")


def rss_entry_feed_url(entry: dict[str, Any]) -> str:
    st = str(entry.get("type") or "animexin").strip().lower()
    if st not in ("rss_qbittorrent", "rss"):
        return ""
    raw = str(entry.get("rss_url") or entry.get("link") or "").strip()
    return normalize_feed_url(raw) if raw else ""


def find_entry_by_feed_url(
    config_list: list[dict[str, Any]], feed_url: str
) -> Optional[dict[str, Any]]:
    n = normalize_feed_url(feed_url)
    if not n:
        return None
    for entry in config_list:
        if rss_entry_feed_url(entry) == n:
            return entry
    return None


def _season_number_from_any(value: Any, default: Optional[int] = None) -> int:
    """Parse season input from int or strings like ``Season 02`` / ``Season.02`` (legacy) / ``2``."""
    if value is None:
        if default is not None:
            return int(default)
        raise ValueError("season is required")
    if isinstance(value, int):
        if value >= 1:
            return int(value)
        raise ValueError("season must be >= 1")
    s = str(value).strip()
    if not s:
        if default is not None:
            return int(default)
        raise ValueError("season is required")
    if s.isdigit():
        n = int(s)
        if n >= 1:
            return n
        raise ValueError("season must be >= 1")
    m = re.search(r"Season\D*(\d+)", s, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        if n >= 1:
            return n
    raise ValueError("season must be a positive integer or 'Season NN'")


def _apply_season_to_show_entry(
    entry: dict[str, Any], season_raw: Optional[Any]
) -> None:
    """Set normalized ``season`` and sync ``ep`` prefix slug."""
    if season_raw is None:
        return
    sn = _season_number_from_any(season_raw)
    entry["season"] = f"Season {sn:02d}"
    slug = normalize_series_slug(str(entry.get("series") or ""))
    entry["series"] = slug
    entry["ep"] = episode_slug_prefix(slug, f"{sn:02d}")


class AddShowBody(BaseModel):
    link: str = Field(..., min_length=1)
    last_ep: int = Field(0, ge=0)
    thetvdb_id: Optional[int] = None

    @field_validator("thetvdb_id")
    @classmethod
    def _thetvdb_id_add(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 1:
            raise ValueError("thetvdb_id must be >= 1")
        return v


class UpdateShowBody(BaseModel):
    last_ep: Optional[int] = Field(None, ge=0)
    link: Optional[str] = None
    thetvdb_id: Optional[int] = None
    season: Optional[int] = Field(None, ge=1)
    show_name_regex: Optional[str] = None
    episode_regex: Optional[str] = None

    @field_validator("thetvdb_id")
    @classmethod
    def _thetvdb_id_update(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 1:
            raise ValueError("thetvdb_id must be >= 1")
        return v

    @field_validator("season", mode="before")
    @classmethod
    def _season_update(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        return _season_number_from_any(v)


class AddRssShowBody(BaseModel):
    name: str = Field(..., min_length=1)
    rss_url: str = Field(..., min_length=1)
    thetvdb_id: int = Field(..., ge=1)
    last_ep: int = Field(0, ge=0)
    season: int = Field(1, ge=1)
    show_name_regex: Optional[str] = None
    episode_regex: Optional[str] = None

    @field_validator("rss_url")
    @classmethod
    def _rss_url_strip(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("rss_url is required")
        return s

    @field_validator("season", mode="before")
    @classmethod
    def _season_add_rss(cls, v: Any) -> int:
        return _season_number_from_any(v, default=1)


class SettingsPatchBody(BaseModel):
    """Top-level config toggles exposed in the web UI."""

    debug: Optional[bool] = None


@app.get("/api/settings")
async def get_settings() -> JSONResponse:
    config = load_config()
    log.info("GET /api/settings debug=%s", bool(config.get("debug")))
    return JSONResponse({"debug": bool(config.get("debug"))})


@app.patch("/api/settings")
async def patch_settings(body: SettingsPatchBody) -> JSONResponse:
    payload = body.model_dump(exclude_unset=True)
    if not payload:
        config = load_config()
        return JSONResponse({"ok": True, "debug": bool(config.get("debug"))})
    config = load_config()
    if "debug" in payload:
        config["debug"] = bool(payload["debug"])
        save_config(config)
        log.info("PATCH /api/settings debug=%s", config["debug"])
    return JSONResponse({"ok": True, "debug": bool(config.get("debug"))})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    log.info("GET / (show manager page)")
    return templates.TemplateResponse(
        request,
        "index.html",
        {"config_path": str(CONFIG_PATH)},
    )


@app.get("/api/shows")
async def list_shows() -> JSONResponse:
    config = load_config()
    n = len(config["list"])
    log.info("GET /api/shows -> %d show(s)", n)
    rows: list[dict[str, Any]] = []
    for i, show in enumerate(config["list"]):
        folder_series = folder_series_for_show(show, config)
        stype = show.get("type") or "animexin"
        jobs = show.get("rss_torrent_jobs")
        rss_pending = (
            len([j for j in (jobs or []) if isinstance(j, dict) and str(j.get("hash"))])
            if isinstance(jobs, list)
            else 0
        )
        rows.append(
            {
                "index": i,
                "type": stype,
                "name": show.get("name", ""),
                "link": show.get("link", ""),
                "rss_url": show.get("rss_url"),
                "series": show.get("series", ""),
                "folder_series": folder_series,
                "season": show.get("season", ""),
                "ep_prefix": show.get("ep", ""),
                "last_ep": show.get("last_ep", 0),
                "last_downloaded_at": show.get("last_downloaded_at"),
                "missing_ep": show.get("missing_ep") or [],
                "thetvdb_id": show.get("thetvdb_id"),
                "show_name_regex": show.get("show_name_regex"),
                "episode_regex": show.get("episode_regex"),
                "rss_pending": rss_pending,
            }
        )
    return JSONResponse({"shows": rows})


@app.post("/api/shows")
async def add_show(body: AddShowBody) -> JSONResponse:
    log.info(
        "POST /api/shows add link=%r last_ep=%s thetvdb_id=%s",
        body.link.strip(),
        body.last_ep,
        body.thetvdb_id,
    )
    config = load_config()
    link = body.link.strip()
    if find_entry_by_link(config["list"], link):
        raise HTTPException(status_code=409, detail="A show with this URL already exists.")
    try:
        new_entry = _run_with_print_capture(
            create_config_from_link, link, last_ep=body.last_ep
        )
    except Exception as e:
        log.error("create_config_from_link failed: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e)) from e
    if body.thetvdb_id is not None:
        new_entry["thetvdb_id"] = body.thetvdb_id
    config["list"].append(new_entry)
    save_config(config)
    log.info(
        "Added show %r (series=%s, season=%s, last_ep=%s)",
        new_entry.get("name"),
        new_entry.get("series"),
        new_entry.get("season"),
        new_entry.get("last_ep"),
    )
    return JSONResponse({"ok": True, "show": new_entry})


@app.post("/api/shows/rss")
async def add_rss_show(body: AddRssShowBody) -> JSONResponse:
    log.info(
        "POST /api/shows/rss name=%r rss_url=%r thetvdb_id=%s last_ep=%s",
        body.name.strip(),
        body.rss_url.strip(),
        body.thetvdb_id,
        body.last_ep,
    )
    config = load_config()
    if find_entry_by_feed_url(config["list"], body.rss_url):
        raise HTTPException(
            status_code=409, detail="A show with this RSS URL already exists."
        )
    try:
        new_entry = _run_with_print_capture(
            create_rss_show_entry,
            name=body.name.strip(),
            rss_url=body.rss_url.strip(),
            thetvdb_id=body.thetvdb_id,
            last_ep=body.last_ep,
            season=body.season,
            show_name_regex=body.show_name_regex,
            episode_regex=body.episode_regex,
        )
    except Exception as e:
        log.error("create_rss_show_entry failed: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e)) from e
    config["list"].append(new_entry)
    save_config(config)
    log.info(
        "Added RSS show %r (thetvdb_id=%s, last_ep=%s)",
        new_entry.get("name"),
        new_entry.get("thetvdb_id"),
        new_entry.get("last_ep"),
    )
    return JSONResponse({"ok": True, "show": new_entry})


@app.put("/api/shows/{index}")
async def update_show(index: int, body: UpdateShowBody) -> JSONResponse:
    log.info(
        "PUT /api/shows/%s last_ep=%s link=%r thetvdb_id=%s (in_body=%s)",
        index,
        body.last_ep,
        body.link,
        body.thetvdb_id,
        "thetvdb_id" in body.model_fields_set,
    )
    config = load_config()
    if index < 0 or index >= len(config["list"]):
        raise HTTPException(status_code=404, detail="Show not found.")
    link_empty = body.link is None or not str(body.link).strip()
    current = config["list"][index]
    ctype = str(current.get("type") or "animexin").strip().lower()
    rss_fields_set = {
        "season",
        "show_name_regex",
        "episode_regex",
    } & set(body.model_fields_set)
    if ctype in ("rss_qbittorrent", "rss"):
        if (
            body.last_ep is None
            and link_empty
            and "thetvdb_id" not in body.model_fields_set
            and not rss_fields_set
        ):
            raise HTTPException(
                status_code=400,
                detail="Provide last_ep, RSS URL, TheTVDB ID, and/or regex/season fields to update.",
            )
        new_link = (body.link or "").strip() or None
        try:
            if new_link and normalize_feed_url(new_link) != rss_entry_feed_url(
                current
            ):
                other = find_entry_by_feed_url(config["list"], new_link)
                if other is not None and other is not current:
                    raise HTTPException(
                        status_code=409,
                        detail="Another show already uses this RSS URL.",
                    )
                current["rss_url"] = new_link.strip()
                current["link"] = current["rss_url"]
            if body.last_ep is not None:
                current["last_ep"] = body.last_ep
            if "thetvdb_id" in body.model_fields_set:
                if body.thetvdb_id is None:
                    raise HTTPException(
                        status_code=400,
                        detail="RSS shows require a TheTVDB ID.",
                    )
                current["thetvdb_id"] = body.thetvdb_id
            if body.season is not None:
                _apply_season_to_show_entry(current, body.season)
            if "show_name_regex" in body.model_fields_set:
                v = body.show_name_regex
                current["show_name_regex"] = (
                    v.strip() if isinstance(v, str) and v.strip() else None
                )
            if "episode_regex" in body.model_fields_set:
                v = body.episode_regex
                current["episode_regex"] = (
                    v.strip() if isinstance(v, str) and v.strip() else None
                )
            config["list"][index] = current
        except HTTPException:
            raise
        except Exception as e:
            log.error("update RSS show failed: %s", e, exc_info=True)
            raise HTTPException(status_code=400, detail=str(e)) from e
        save_config(config)
        updated = config["list"][index]
        log.info(
            "Updated RSS show index=%s name=%r last_ep=%s",
            index,
            updated.get("name"),
            updated.get("last_ep"),
        )
        return JSONResponse({"ok": True, "show": updated})

    if (
        body.last_ep is None
        and link_empty
        and "thetvdb_id" not in body.model_fields_set
    ):
        raise HTTPException(
            status_code=400,
            detail="Provide last_ep, a series URL, and/or thetvdb_id to update.",
        )
    new_link = (body.link or "").strip() or None
    try:
        if new_link and normalize_link(new_link) != normalize_link(current["link"]):
            other = find_entry_by_link(config["list"], new_link)
            if other is not None and other is not current:
                raise HTTPException(status_code=409, detail="Another show already uses this URL.")
            last = body.last_ep if body.last_ep is not None else int(current.get("last_ep", 0))
            new_entry = _run_with_print_capture(
                create_config_from_link, new_link, last_ep=last
            )
            if "thetvdb_id" in body.model_fields_set:
                if body.thetvdb_id is not None:
                    new_entry["thetvdb_id"] = body.thetvdb_id
                else:
                    new_entry.pop("thetvdb_id", None)
            elif current.get("thetvdb_id") is not None:
                new_entry["thetvdb_id"] = current["thetvdb_id"]
            config["list"][index] = new_entry
            if body.season is not None:
                _apply_season_to_show_entry(new_entry, body.season)
        else:
            if body.last_ep is not None:
                current["last_ep"] = body.last_ep
            if "thetvdb_id" in body.model_fields_set:
                if body.thetvdb_id is None:
                    current.pop("thetvdb_id", None)
                else:
                    current["thetvdb_id"] = body.thetvdb_id
            if body.season is not None:
                _apply_season_to_show_entry(current, body.season)
            config["list"][index] = current
    except HTTPException:
        raise
    except Exception as e:
        log.error("update show failed: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e)) from e
    save_config(config)
    updated = config["list"][index]
    log.info(
        "Updated show index=%s name=%r last_ep=%s",
        index,
        updated.get("name"),
        updated.get("last_ep"),
    )
    return JSONResponse({"ok": True, "show": updated})


@app.delete("/api/shows/{index}")
async def delete_show(index: int) -> JSONResponse:
    log.info("DELETE /api/shows/%s", index)
    config = load_config()
    if index < 0 or index >= len(config["list"]):
        raise HTTPException(status_code=404, detail="Show not found.")
    removed = config["list"].pop(index)
    save_config(config)
    log.info("Removed show index=%s name=%r", index, removed.get("name"))
    return JSONResponse({"ok": True})


@app.get("/api/logs")
async def get_logs() -> JSONResponse:
    with _log_lock:
        return JSONResponse({"lines": list(_log_lines)})


@app.delete("/api/logs")
async def clear_logs() -> JSONResponse:
    with _log_lock:
        _log_lines.clear()
    log.info("Activity log cleared (server buffer)")
    return JSONResponse({"ok": True})


@app.post("/api/download")
async def start_download() -> JSONResponse:
    if not _try_start_download_job("api"):
        raise HTTPException(
            status_code=409,
            detail="Downloader is already running.",
        )
    return JSONResponse(
        status_code=202,
        content={"ok": True, "started": True},
    )


@app.post("/api/rss-import")
async def start_rss_import() -> JSONResponse:
    if not _try_start_rss_import_job("api"):
        raise HTTPException(
            status_code=409,
            detail="RSS pending-import is already running.",
        )
    return JSONResponse(
        status_code=202,
        content={"ok": True, "started": True},
    )


@app.get("/api/download/progress")
async def download_episode_progress() -> JSONResponse:
    with _episode_progress_lock:
        progress_items = list(_episode_progress_items.values())

    items = [_serialize_progress_item(item) for item in progress_items if item.get("active")]
    def _episode_sort_value(raw: Any) -> int:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    items.sort(
        key=lambda p: (
            str(p.get("show_name") or p.get("series") or ""),
            _episode_sort_value(p.get("episode")),
            str(p.get("id") or ""),
        )
    )
    active = bool(items)
    primary = items[0] if items else {}
    body: dict[str, Any] = {
        "active": active,
        "items": items,
        "active_count": len(items),
        "source": primary.get("source"),
        "show_link": primary.get("show_link"),
        "show_name": primary.get("show_name"),
        "series": primary.get("series"),
        "episode": primary.get("episode"),
        "filename": primary.get("filename"),
        "downloaded": primary.get("downloaded", 0),
        "total": primary.get("total", 0),
        "speed_bps": primary.get("speed_bps", 0),
        "torrent_state": primary.get("torrent_state"),
        "torrent_eta": primary.get("torrent_eta"),
        "percent": primary.get("percent"),
        "indeterminate": bool(primary.get("indeterminate", False)),
    }
    return JSONResponse(body)


@app.get("/api/download/status")
async def download_status() -> JSONResponse:
    with _download_lock:
        body = {
            "running": bool(_download_state["running"]),
            "error": _download_state["error"],
            "started_at": _download_state["started_at"],
            "finished_at": _download_state["finished_at"],
        }
        thr = _download_thread
    body["thread_alive"] = thr is not None and thr.is_alive()
    body["schedule_interval_seconds"] = int(
        os.environ.get("DOWNLOAD_SCHEDULE_SECONDS", "3600")
    )
    return JSONResponse(body)


@app.get("/api/rss-import/status")
async def rss_import_status() -> JSONResponse:
    with _rss_import_lock:
        body = {
            "running": bool(_rss_import_state["running"]),
            "error": _rss_import_state["error"],
            "started_at": _rss_import_state["started_at"],
            "finished_at": _rss_import_state["finished_at"],
        }
        thr = _rss_import_thread
    body["thread_alive"] = thr is not None and thr.is_alive()
    body["schedule_interval_seconds"] = int(
        os.environ.get("RSS_IMPORT_SCHEDULE_SECONDS", "1800")
    )
    return JSONResponse(body)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
