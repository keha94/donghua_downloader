"""Local web UI to manage donghua shows in config.json."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
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
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from filelock import FileLock
from pydantic import BaseModel, Field, field_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from downloader import (  # noqa: E402
    create_config_from_link,
    normalize_legacy_show_entry,
    run_downloads,
    set_download_progress_hook,
    write_config_json,
)
from tvdb_naming import folder_series_for_show  # noqa: E402

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", REPO_ROOT / "config.json")).resolve()
LOCK_PATH = str(CONFIG_PATH) + ".lock"

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

_episode_progress_lock = threading.Lock()
_episode_progress: dict[str, Any] = {
    "active": False,
    "show_link": None,
    "show_name": None,
    "series": None,
    "episode": None,
    "filename": None,
    "downloaded": 0,
    "total": 0,
    "_t0": None,
}


def _reset_episode_download_progress() -> None:
    with _episode_progress_lock:
        _episode_progress.update(
            {
                "active": False,
                "show_link": None,
                "show_name": None,
                "series": None,
                "episode": None,
                "filename": None,
                "downloaded": 0,
                "total": 0,
                "_t0": None,
            }
        )


def _on_download_file_event(ev: dict[str, Any]) -> None:
    kind = ev.get("kind")
    with _episode_progress_lock:
        if kind == "file_start":
            _episode_progress.update(
                {
                    "active": True,
                    "show_link": ev.get("show_link"),
                    "show_name": ev.get("show_name"),
                    "series": ev.get("series"),
                    "episode": ev.get("episode"),
                    "filename": ev.get("filename"),
                    "downloaded": 0,
                    "total": int(ev.get("total") or 0),
                    "_t0": time.monotonic(),
                }
            )
        elif kind == "file_progress":
            if not _episode_progress.get("active"):
                return
            _episode_progress["downloaded"] = int(ev.get("downloaded") or 0)
            _episode_progress["total"] = int(ev.get("total") or 0)
        elif kind == "file_complete":
            _episode_progress["downloaded"] = int(ev.get("downloaded") or 0)
            _episode_progress["total"] = int(ev.get("total") or 0)
            _episode_progress["active"] = False
            _episode_progress["_t0"] = None
        elif kind == "file_error":
            _episode_progress["active"] = False
            _episode_progress["_t0"] = None

    if kind == "file_start":
        log.info(
            "[download] Downloading EP%s — %s",
            ev.get("episode"),
            ev.get("filename"),
        )


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
        _reset_episode_download_progress()
        log.info("Downloader thread finished")


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


@asynccontextmanager
async def _lifespan(app: FastAPI):
    log.info("Web GUI started (config: %s)", CONFIG_PATH)
    schedule_sec = int(os.environ.get("DOWNLOAD_SCHEDULE_SECONDS", "3600"))
    sched_task: Optional[asyncio.Task] = None
    if schedule_sec > 0:
        sched_task = asyncio.create_task(_download_schedule_loop(schedule_sec))
        log.info("Download schedule: every %s seconds", schedule_sec)
    else:
        log.info("Download schedule: disabled (DOWNLOAD_SCHEDULE_SECONDS=0)")
    try:
        yield
    finally:
        if sched_task is not None:
            sched_task.cancel()
            try:
                await sched_task
            except asyncio.CancelledError:
                pass
        log.info("Web GUI shutting down")


app = FastAPI(title="Donghua Downloader", lifespan=_lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
static_dir = Path(__file__).parent / "static"
if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


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
        "last_run": 0,
        "base_folder_download": str(REPO_ROOT / "downloads"),
        "test": False,
        "headless": True,
        "skyhook_base_url": "",
        "skyhook_language": "en",
        "thetvdb_include_episode_title": True,
        "list": [],
        "disabled": [],
    }


def _ensure_config_shape(data: dict[str, Any]) -> dict[str, Any]:
    base = _default_config()
    base.update({k: data[k] for k in base if k in data})
    if "list" not in data or not isinstance(data["list"], list):
        base["list"] = []
    else:
        base["list"] = data["list"]
    if "disabled" in data and isinstance(data["disabled"], list):
        base["disabled"] = data["disabled"]
    for key in (
        "last_run",
        "base_folder_download",
        "test",
        "headless",
        "skyhook_base_url",
        "skyhook_language",
        "thetvdb_include_episode_title",
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
    lock = FileLock(LOCK_PATH, timeout=60)
    with lock:
        if not CONFIG_PATH.is_file():
            return _default_config()
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return _ensure_config_shape(json.load(f))


def save_config(config: dict[str, Any]) -> None:
    lock = FileLock(LOCK_PATH, timeout=60)
    with lock:
        write_config_json(str(CONFIG_PATH), config)


def find_entry_by_link(
    config_list: list[dict[str, Any]], link: str
) -> Optional[dict[str, Any]]:
    n = normalize_link(link)
    for entry in config_list:
        if normalize_link(entry["link"]) == n:
            return entry
    return None


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

    @field_validator("thetvdb_id")
    @classmethod
    def _thetvdb_id_update(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 1:
            raise ValueError("thetvdb_id must be >= 1")
        return v


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
        rows.append(
            {
                "index": i,
                "name": show.get("name", ""),
                "link": show.get("link", ""),
                "series": show.get("series", ""),
                "folder_series": folder_series,
                "season": show.get("season", ""),
                "ep_prefix": show.get("ep", ""),
                "last_ep": show.get("last_ep", 0),
                "missing_ep": show.get("missing_ep") or [],
                "thetvdb_id": show.get("thetvdb_id"),
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
    if (
        body.last_ep is None
        and link_empty
        and "thetvdb_id" not in body.model_fields_set
    ):
        raise HTTPException(
            status_code=400,
            detail="Provide last_ep, a series URL, and/or thetvdb_id to update.",
        )
    current = config["list"][index]
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
        else:
            if body.last_ep is not None:
                current["last_ep"] = body.last_ep
            if "thetvdb_id" in body.model_fields_set:
                if body.thetvdb_id is None:
                    current.pop("thetvdb_id", None)
                else:
                    current["thetvdb_id"] = body.thetvdb_id
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


@app.get("/api/download/progress")
async def download_episode_progress() -> JSONResponse:
    with _episode_progress_lock:
        active = bool(_episode_progress.get("active"))
        show_link = _episode_progress.get("show_link")
        show_name = _episode_progress.get("show_name")
        series = _episode_progress.get("series")
        episode = _episode_progress.get("episode")
        filename = _episode_progress.get("filename")
        downloaded = int(_episode_progress.get("downloaded") or 0)
        total = int(_episode_progress.get("total") or 0)
        t0 = _episode_progress.get("_t0")

    speed_bps = 0
    if active and t0 is not None and downloaded > 0:
        speed_bps = int(downloaded / max(time.monotonic() - float(t0), 1e-6))

    body: dict[str, Any] = {
        "active": active,
        "show_link": show_link,
        "show_name": show_name,
        "series": series,
        "episode": episode,
        "filename": filename,
        "downloaded": downloaded,
        "total": total,
        "speed_bps": speed_bps,
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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
