"""RSS feed monitoring + qBittorrent handoff and library relocation."""

from __future__ import annotations

import os
import re
from collections import defaultdict
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Optional

import feedparser

from tvdb_naming import (
    TVDBSeriesData,
    episode_filename,
    get_cached_series_data,
    match_episode_record,
    parse_scraped_season_number,
    rss_title_matches_series,
    season_folder_name,
    series_root_folder,
)

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".webm", ".m4v", ".mov", ".wmv"}

_DEFAULT_EPISODE_NUM_PATTERNS: tuple[str, ...] = (
    r"(?i)\bS\d{1,4}\s*[_\-\.]?\s*E\s*(\d{1,5})\b",
    r"(?i)\b(\d{1,5})\s*[_\-\.]?\s*\(\s*END\s*\)",
    r"(?i)\bEpisode\s*[._:\#]?\s*(\d{1,5})\b",
    r"(?i)\bE(?:p(?:isode)?)?[._\# ]?(\d{1,5})\b",
    r"(?i)\b第\s*(\d{1,5})\s*話\b",
    r"(?i)\[\s*(\d{1,5})\s*v(?:\d+)?\s*\]",
    r"(?i)\s-\s(\d{1,5})\s-\s",
)


def get_qbittorrent_settings(config: dict[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 8080,
        "username": "",
        "password": "",
        "tag_prefix": "donghua",
    }
    merged: dict[str, Any] = {**defaults, **(config.get("qbittorrent") or {})}
    eh = (os.environ.get("QBITTORRENT_HOST") or "").strip()
    if eh:
        merged["host"] = eh
    ep = (os.environ.get("QBITTORRENT_PORT") or "").strip()
    if ep:
        try:
            merged["port"] = int(ep)
        except ValueError:
            pass
    eu = (os.environ.get("QBITTORRENT_USERNAME") or "").strip()
    if eu or "QBITTORRENT_USERNAME" in os.environ:
        merged["username"] = eu
    epw = os.environ.get("QBITTORRENT_PASSWORD")
    if epw is not None:
        merged["password"] = str(epw)
    return merged


def _qbittorrent_client(settings: dict[str, Any]) -> Any:
    from qbittorrentapi import Client

    host = str(settings.get("host") or "127.0.0.1")
    port = int(settings.get("port") or 8080)
    user = str(settings.get("username") or "")
    password = str(settings.get("password") or "")
    verify = settings.get("VERIFY_WEBUI_CERTIFICATE")
    if verify is None:
        verify_env = (os.environ.get("QBITTORRENT_VERIFY_CERT") or "").strip().lower()
        if verify_env in ("0", "false", "no"):
            verify = False
    kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "username": user or None,
        "password": password or None,
    }
    if verify is not None:
        kwargs["VERIFY_WEBUI_CERTIFICATE"] = bool(verify)
    return Client(**kwargs)


def _feed_item_torrent_uri(entry: Any) -> Optional[str]:
    for enc in getattr(entry, "enclosures", None) or []:
        href = getattr(enc, "href", None) or getattr(enc, "url", None) or ""
        if not isinstance(href, str) or not href.strip():
            continue
        h = href.strip()
        ct = (getattr(enc, "type", "") or "").lower()
        if h.startswith("magnet:") or "torrent" in ct or h.lower().endswith(".torrent"):
            return h
    link = getattr(entry, "link", None) or ""
    if isinstance(link, str) and link.strip().startswith("magnet:"):
        return link.strip()
    for lnk in getattr(entry, "links", None) or []:
        if isinstance(lnk, dict):
            href = str(lnk.get("href") or "")
            rel = str(lnk.get("rel") or "").lower()
        else:
            href = str(getattr(lnk, "href", "") or "")
            rel = str(getattr(lnk, "rel", "") or "").lower()
        if not href:
            continue
        if href.startswith("magnet:") or href.lower().endswith(".torrent"):
            return href
        if "torrent" in rel:
            return href
    if isinstance(link, str) and link.strip().lower().endswith(".torrent"):
        return link.strip()
    return None


def rss_episode_number(
    title: str,
    episode_regex: Optional[str],
) -> Optional[int]:
    if episode_regex and str(episode_regex).strip():
        try:
            m = re.search(episode_regex, title)
        except re.error:
            return None
        if not m:
            return None
        if m.lastindex:
            try:
                return int(m.group(1))
            except (TypeError, ValueError):
                return None
        try:
            return int(m.group(0))
        except (TypeError, ValueError):
            return None
    for pat in _DEFAULT_EPISODE_NUM_PATTERNS:
        m = re.search(pat, title)
        if m:
            try:
                return int(m.group(1))
            except (TypeError, ValueError, IndexError):
                continue
    return None


def _largest_video_under(path: Path) -> Optional[Path]:
    if path.is_file():
        return path if path.suffix.lower() in VIDEO_EXTS else None
    best: Optional[Path] = None
    best_size = -1
    try:
        for p in path.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
                continue
            try:
                sz = p.stat().st_size
            except OSError:
                continue
            if sz > best_size:
                best_size = sz
                best = p
    except OSError:
        return None
    return best


def _path_looks_like_uri_scheme(p: Path) -> bool:
    """True if *p* looks like afp/smb/file URL — pathlib cannot open these as local files."""
    s = str(p)
    if "://" in s:
        return True
    # pathlib may collapse afp:// to afp:/
    if s.startswith("afp:") or s.startswith("smb:"):
        return True
    return False


def _normalize_save_path_rule_prefix(p: str) -> str:
    s = p.replace("\\", "/").strip()
    while len(s) > 1 and s.endswith("/"):
        s = s[:-1]
    return s


def _save_path_rewrite_rules(qsettings: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (remote_prefix, local_prefix) pairs; longest remote prefix wins first."""
    rules: list[tuple[str, str]] = []
    raw_list = qsettings.get("save_path_rewrite")
    if isinstance(raw_list, list):
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            fr = str(item.get("from") or "").strip()
            to = str(item.get("to") or "").strip()
            if fr and to:
                rules.append((_normalize_save_path_rule_prefix(fr), to))

    ef = (os.environ.get("QBITTORRENT_SAVE_PATH_FROM") or "").strip()
    et = (os.environ.get("QBITTORRENT_SAVE_PATH_TO") or "").strip()
    if ef and et:
        rules.append((_normalize_save_path_rule_prefix(ef), et))

    cf = str(qsettings.get("save_path_from") or "").strip()
    ct = str(qsettings.get("save_path_to") or "").strip()
    if cf and ct:
        rules.append((_normalize_save_path_rule_prefix(cf), ct))

    seen: set[str] = set()
    uniq: list[tuple[str, str]] = []
    for fr, to in rules:
        if fr in seen:
            continue
        seen.add(fr)
        uniq.append((fr, to))
    uniq.sort(key=lambda x: len(x[0]), reverse=True)
    return uniq


def _rewrite_qbit_save_path(path: Path, rules: list[tuple[str, str]]) -> Path:
    """Map API paths (e.g. NAS filesystem as seen by qBittorrent) to this machine's mount."""
    if not rules:
        return path
    s = path.as_posix()
    if not s or s == ".":
        return path
    for fr, to in rules:
        if s == fr or s.startswith(fr + "/"):
            rest = s[len(fr) :].lstrip("/")
            base = Path(to)
            return base / rest if rest else base
    return path


def _torrent_save_path(t: Any, qsettings: Optional[dict[str, Any]] = None) -> Path:
    """Resolve torrent content directory on *this* machine.

    qBittorrent may report paths from the client's OS (e.g. NAS ``/volume1/...``). Use
    ``qbittorrent.save_path_rewrite`` (or ``QBITTORRENT_SAVE_PATH_FROM`` / ``_TO``) so those
    prefixes map to your local SMB/NFS mount before existence checks and imports.
    """
    rules = _save_path_rewrite_rules(qsettings) if qsettings else []

    raw = getattr(t, "content_path", None) or getattr(t, "save_path", None) or ""
    name = getattr(t, "name", None) or ""
    p = Path(str(raw))
    sp = Path(str(getattr(t, "save_path", "") or ""))

    trial: list[Path] = []
    if raw:
        trial.append(p)
    if sp and name:
        trial.append(sp / str(name))
    if sp:
        trial.append(sp)

    for cand in trial:
        local = _rewrite_qbit_save_path(cand, rules)
        if local.exists():
            return local

    if trial:
        return _rewrite_qbit_save_path(trial[0], rules)
    return _rewrite_qbit_save_path(p, rules)


def _torrent_byte_progress(t: Any) -> tuple[float, int, int]:
    """Return (progress 0..1, total size bytes, downloaded bytes) for display."""
    try:
        prog = float(getattr(t, "progress", 0) or 0)
    except (TypeError, ValueError):
        prog = 0.0
    prog = max(0.0, min(1.0, prog))
    try:
        size = int(getattr(t, "size", 0) or 0)
    except (TypeError, ValueError):
        size = 0
    dl_raw = getattr(t, "downloaded", None)
    try:
        downloaded = int(dl_raw) if dl_raw is not None else 0
    except (TypeError, ValueError):
        downloaded = 0
    if downloaded <= 0 and size > 0:
        downloaded = int(prog * size)
    return prog, size, downloaded


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit, div in (("KiB", 1024), ("MiB", 1024**2), ("GiB", 1024**3), ("TiB", 1024**4)):
        v = n / div
        if v < 1024 or unit == "TiB":
            return f"{v:.1f} {unit}"
    return f"{n} B"


def _format_eta(seconds: Optional[int]) -> str:
    if seconds is None:
        return "?"
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "?"
    if s < 0 or s >= 8640000:
        return "∞"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def _torrent_active_for_progress_ui(t: Any) -> bool:
    """Whether this torrent should still show download activity (progress bar, polling).

    Debrid proxies (e.g. RDT Client, fake qBittorrent API) often run two phases: cache on
    the debrid service, then copy to disk. After phase 1 the API may report 100% done
    while phase 2 is still copying — treat as active if dlspeed > 0 or amount_left > 0.
    """
    try:
        dlspeed = int(getattr(t, "dlspeed", 0) or 0)
    except (TypeError, ValueError):
        dlspeed = 0
    if dlspeed > 0:
        return True
    al = getattr(t, "amount_left", None)
    if al is not None:
        try:
            if int(al) > 0:
                return True
        except (TypeError, ValueError):
            pass
    return not _torrent_is_complete(t)


def _torrent_ready_to_relocate(t: Any) -> bool:
    """True when the client reports completion *and* local copy is idle (import-safe).

    Debrid clients may report 100% complete while still writing files to disk; relocating
    during that window fails or imports partial files. Re-use the same heuristic as the UI.
    """
    if not _torrent_is_complete(t):
        return False
    if _torrent_active_for_progress_ui(t):
        return False
    return True


def _report_pending_torrent_jobs_progress(
    *,
    client: Any,
    show_name: str,
    jobs: list[dict[str, Any]],
    emit_gui_first: bool,
    quiet: bool = False,
) -> None:
    """Log qBittorrent progress for pending jobs; optionally notify GUI for the first job."""
    from downloader import emit_torrent_progress

    pending = [
        j
        for j in jobs
        if isinstance(j, dict) and str(j.get("hash") or "").strip()
    ]
    def _ep_sort_key(j: dict[str, Any]) -> int:
        try:
            return int(j.get("episode") or 0)
        except (TypeError, ValueError):
            return 0

    pending.sort(key=_ep_sort_key)
    for idx, job in enumerate(pending):
        h = str(job.get("hash") or "").strip().lower()
        try:
            ep_n = int(job.get("episode"))
        except (TypeError, ValueError):
            continue
        info_list = client.torrents.info(torrent_hashes=h)
        t = info_list[0] if info_list else None
        if t is None:
            if not quiet:
                print(f"📥 {show_name}: EP{ep_n} — torrent {h[:8]}… not found in qBittorrent")
            continue
        if not _torrent_active_for_progress_ui(t):
            continue
        prog, size, downloaded = _torrent_byte_progress(t)
        pct = 100.0 * prog
        try:
            dlspeed = int(getattr(t, "dlspeed", 0) or 0)
        except (TypeError, ValueError):
            dlspeed = 0
        eta_raw = getattr(t, "eta", None)
        try:
            eta = int(eta_raw) if eta_raw is not None else None
        except (TypeError, ValueError):
            eta = None
        state = str(getattr(t, "state", "") or "")
        item_title = str(job.get("item_title") or "")[:60]
        tail = f" — {item_title}…" if item_title else ""
        if not quiet:
            print(
                f"📥 {show_name}: EP{ep_n} — {pct:.1f}% "
                f"({_format_bytes(downloaded)}/{_format_bytes(size) if size else '?'}) "
                f"{_format_bytes(dlspeed)}/s ETA {_format_eta(eta)} [{state}]{tail}"
            )
        if emit_gui_first and idx == 0:
            emit_torrent_progress(
                {
                    "kind": "torrent_progress",
                    "show_name": show_name,
                    "episode": ep_n,
                    "filename": f"{show_name} EP{ep_n}",
                    "progress": prog,
                    "percent": pct,
                    "downloaded": downloaded,
                    "total": size,
                    "dlspeed": dlspeed,
                    "eta": eta,
                    "state": state,
                    "info_hash": h,
                }
            )


def poll_all_shows_rss_torrent_gui_progress(
    config: dict[str, Any],
) -> tuple[Optional[bool], str]:
    """Poll qBittorrent for all RSS shows; emit GUI progress for the first incomplete torrent.

    Call from the web UI background loop so the progress bar updates while torrents download
    after ``run_downloads()`` has finished (hooks would otherwise be cleared).

    Returns:
        (True, detail) if progress was emitted,
        (False, detail) if qBittorrent responded but nothing was downloading,
        (None, detail) if qBittorrent could not be reached (do not clear the UI).
    """
    from downloader import emit_torrent_progress

    qsettings = get_qbittorrent_settings(config)
    try:
        client = _qbittorrent_client(qsettings)
        _ = client.app.web_api_version
    except Exception as exc:
        return None, f"qBittorrent unreachable ({type(exc).__name__}: {exc}) host={qsettings.get('host')} port={qsettings.get('port')}"

    rss_show_count = 0
    jobs_total = 0
    skipped_missing = 0
    skipped_complete = 0

    for show in config.get("list") or []:
        if not isinstance(show, dict):
            continue
        st = str(show.get("type") or "").strip().lower()
        if st not in ("rss_qbittorrent", "rss"):
            continue
        rss_show_count += 1
        show_name = str(show.get("name") or "RSS show")
        jobs_raw = show.get("rss_torrent_jobs") or []
        pending = [
            j
            for j in jobs_raw
            if isinstance(j, dict) and str(j.get("hash") or "").strip()
        ]
        jobs_total += len(pending)

        def _ep_key(j: dict[str, Any]) -> int:
            try:
                return int(j.get("episode") or 0)
            except (TypeError, ValueError):
                return 0

        pending.sort(key=_ep_key)
        for job in pending:
            h = str(job.get("hash") or "").strip().lower()
            try:
                ep_n = int(job.get("episode"))
            except (TypeError, ValueError):
                continue
            info_list = client.torrents.info(torrent_hashes=h)
            t = info_list[0] if info_list else None
            if t is None:
                skipped_missing += 1
                continue
            if not _torrent_active_for_progress_ui(t):
                skipped_complete += 1
                continue
            prog, size, downloaded = _torrent_byte_progress(t)
            pct = 100.0 * prog
            try:
                dlspeed = int(getattr(t, "dlspeed", 0) or 0)
            except (TypeError, ValueError):
                dlspeed = 0
            eta_raw = getattr(t, "eta", None)
            try:
                eta = int(eta_raw) if eta_raw is not None else None
            except (TypeError, ValueError):
                eta = None
            state = str(getattr(t, "state", "") or "")
            emit_torrent_progress(
                {
                    "kind": "torrent_progress",
                    "show_name": show_name,
                    "episode": ep_n,
                    "filename": f"{show_name} EP{ep_n}",
                    "progress": prog,
                    "percent": pct,
                    "downloaded": downloaded,
                    "total": size,
                    "dlspeed": dlspeed,
                    "eta": eta,
                    "state": state,
                    "info_hash": h,
                }
            )
            return (
                True,
                f"emit show={show_name!r} EP{ep_n} {pct:.1f}% state={state!r} "
                f"hash={h[:8]}… dl={downloaded}/{size} B",
            )

    if rss_show_count == 0:
        return False, "no shows with type rss_qbittorrent/rss in config list"
    if jobs_total == 0:
        return (
            False,
            f"{rss_show_count} RSS show(s) but rss_torrent_jobs is empty — run downloader or add a release",
        )
    return (
        False,
        f"qBit OK: {rss_show_count} RSS show(s), {jobs_total} tracked hash(es), "
        f"none active for progress UI (missing_in_qbit={skipped_missing}, idle={skipped_complete})",
    )


def _torrent_is_complete(t: Any) -> bool:
    try:
        prog = float(getattr(t, "progress", 0) or 0)
    except (TypeError, ValueError):
        prog = 0.0
    if prog >= 1.0 - 1e-9:
        return True
    al = getattr(t, "amount_left", None)
    if al is not None:
        try:
            if int(al) == 0:
                return True
        except (TypeError, ValueError):
            pass
    state = str(getattr(t, "state", "") or "")
    return state in (
        "pausedUP",
        "uploading",
        "stalledUP",
        "queuedUL",
        "forcedUP",
    )


def _rss_tag(settings: dict[str, Any], show_index: int) -> str:
    prefix = str(settings.get("tag_prefix") or "donghua").strip() or "donghua"
    return f"{prefix}_{show_index}"


def _add_torrent_and_find_hash(
    client: Any,
    uri: str,
    tag: str,
) -> Optional[str]:
    before = {str(getattr(t, "hash", "") or "") for t in (client.torrents.info(tag=tag) or [])}
    before.discard("")
    client.torrents.add(urls=uri, tags=tag)
    for _ in range(40):
        time.sleep(0.2)
        after = client.torrents.info(tag=tag) or []
        for t in after:
            h = str(getattr(t, "hash", "") or "")
            if h and h not in before:
                return h
    return None


def _build_output_paths(
    *,
    show: dict[str, Any],
    tvdb_bundle: Optional[TVDBSeriesData],
    ep_num: int,
    include_episode_title: bool,
    base_download: str,
    series_slug: str,
    season_field: str,
    ep_prefix: str,
    video_ext: str,
) -> tuple[str, str]:
    if tvdb_bundle is not None:
        scraped_season = parse_scraped_season_number(str(season_field))
        ep_rec = match_episode_record(tvdb_bundle.episodes, ep_num, scraped_season)
        if ep_rec is not None:
            s_num = int(ep_rec.get("seasonNumber") or 0)
            e_num = int(ep_rec.get("number") or 0)
            ep_title = (ep_rec.get("name") or "").strip() or None
        else:
            s_num = int(scraped_season or 1)
            e_num = ep_num
            ep_title = None
        root_folder = series_root_folder(tvdb_bundle)
        season_folder = season_folder_name(s_num, tvdb_bundle.seasons)
        display_name = tvdb_bundle.name
        filename = episode_filename(
            series_display_name=display_name,
            season_num=s_num,
            episode_num=e_num,
            episode_title=ep_title,
            extension=video_ext,
            include_episode_title=include_episode_title,
        )
        out = str(Path(base_download) / root_folder / season_folder / filename)
        return out, filename
    season_str = str(season_field or "Season.01")
    filename = f"{ep_prefix}{str(ep_num).zfill(2)}.{video_ext.lstrip('.')}"
    out = str(Path(base_download) / series_slug / season_str / filename)
    return out, filename


def _library_destination_exists_for_ep(
    *,
    show: dict[str, Any],
    tvdb_bundle: Optional[TVDBSeriesData],
    ep_num: int,
    include_episode_title: bool,
    base_download: str,
    series_slug: str,
    season_field: str,
    ep_prefix: str,
) -> Optional[Path]:
    """Return path if the TVDB or legacy-named episode file already exists under base_download."""
    for ext in ("mkv", "mp4", "avi", "webm", "m4v", "mov"):
        outp, _ = _build_output_paths(
            show=show,
            tvdb_bundle=tvdb_bundle,
            ep_num=ep_num,
            include_episode_title=include_episode_title,
            base_download=base_download,
            series_slug=series_slug,
            season_field=season_field,
            ep_prefix=ep_prefix,
            video_ext=ext,
        )
        p = Path(outp)
        if p.is_file():
            return p
    return None


def _finish_rss_torrent_jobs_for_show(
    *,
    name: str,
    show: dict[str, Any],
    config: dict[str, Any],
    cfg_path: str,
    client: Any,
    tvdb_bundle: Optional[TVDBSeriesData],
    base_path: str,
    test_mode: bool,
    include_ep_title: bool,
    series_slug: str,
    season_field: str,
    ep_prefix: str,
    list_problems: Optional[list[str]],
    emit_progress: bool = True,
) -> int:
    """Move completed torrent outputs into the library; update ``rss_torrent_jobs``.

    Returns the number of episodes successfully relocated this call.
    """
    from downloader import save_config

    save_cfg: Callable[..., None] = save_config
    qsettings = get_qbittorrent_settings(config)

    jobs_raw = show.get("rss_torrent_jobs")
    if not isinstance(jobs_raw, list):
        jobs_raw = []
    jobs_snapshot = list(jobs_raw)

    relocated_ok = 0
    still: list[dict[str, Any]] = []
    for job in jobs_snapshot:
        if not isinstance(job, dict):
            continue
        h = str(job.get("hash") or "").strip().lower()
        ep_j = job.get("episode")
        try:
            ep_j_int = int(ep_j)
        except (TypeError, ValueError):
            ep_j_int = -1
        if not h or ep_j_int < 0:
            continue
        info_list = client.torrents.info(torrent_hashes=h)
        t = info_list[0] if info_list else None
        if t is None:
            lib_hit = _library_destination_exists_for_ep(
                show=show,
                tvdb_bundle=tvdb_bundle,
                ep_num=ep_j_int,
                include_episode_title=include_ep_title,
                base_download=base_path,
                series_slug=series_slug,
                season_field=season_field,
                ep_prefix=ep_prefix,
            )
            if lib_hit is not None:
                print(
                    f"✅ {name}: EP{ep_j_int} already in library ({lib_hit.name}); "
                    f"clearing RSS job (torrent no longer in client)."
                )
                show["last_ep"] = max(int(show.get("last_ep") or 0), ep_j_int)
                save_cfg(config, cfg_path)
                print("💾 Config updated!")
                continue
            print(
                f"⚠️ {name}: torrent {h[:8]}… not in client — will retry next run. "
                f"If your client removes finished torrents before import runs, wait for "
                f"the scheduled downloader or run it manually once files exist."
            )
            still.append(job)
            continue
        if not _torrent_ready_to_relocate(t):
            still.append(job)
            continue
        content = _torrent_save_path(t, qsettings)
        src_video = _largest_video_under(content)
        if src_video is None:
            print(
                f"❌ {name}: EP{ep_j_int} complete but no video file under {content}"
            )
            if _path_looks_like_uri_scheme(content):
                print(
                    '    Hint: QBITTORRENT_SAVE_PATH_TO / save_path_rewrite "to" must be a real '
                    "filesystem path (e.g. /Volumes/YourShare/... on macOS), not afp:// or smb:// "
                    "URLs — Python cannot stat or move files through those schemes."
                )
            if list_problems is not None:
                list_problems.append(
                    f"{name}: EP{ep_j_int} no video under {content}"
                )
            still.append(job)
            continue
        ext = src_video.suffix.lstrip(".") or "mkv"
        output_path, filename = _build_output_paths(
            show=show,
            tvdb_bundle=tvdb_bundle,
            ep_num=ep_j_int,
            include_episode_title=include_ep_title,
            base_download=base_path,
            series_slug=series_slug,
            season_field=season_field,
            ep_prefix=ep_prefix,
            video_ext=ext,
        )
        dest = Path(output_path)
        try:
            if test_mode:
                print(
                    f"TEST MODE: would move {src_video} -> {dest} "
                    f"and remove qBittorrent job {h[:8]}…"
                )
                still.append(job)
                continue
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    print(f"⚠️ {name}: destination exists, skipping move: {dest}")
                    still.append(job)
                    continue
                shutil.move(str(src_video), str(dest))
                relocated_ok += 1
                try:
                    client.torrents.delete(delete_files=True, torrent_hashes=h)
                except Exception as del_exc:
                    print(f"⚠️ {name}: moved file but torrent delete failed: {del_exc}")
                print(f"✅ {name}: EP{ep_j_int} -> {dest.name}")
                show["last_ep"] = max(int(show.get("last_ep") or 0), ep_j_int)
                save_cfg(config, cfg_path)
                print("💾 Config updated!")
        except Exception as exc:
            print(f"❌ {name}: relocate EP{ep_j_int} failed: {exc}")
            if list_problems is not None:
                list_problems.append(f"{name}: relocate EP{ep_j_int} {exc}")
            still.append(job)

    show["rss_torrent_jobs"] = still
    if still != jobs_snapshot and not test_mode:
        save_cfg(config, cfg_path)

    if emit_progress:
        _report_pending_torrent_jobs_progress(
            client=client,
            show_name=name,
            jobs=still,
            emit_gui_first=True,
        )

    return relocated_ok


def run_rss_pending_import_pass(cfg_path: str) -> None:
    """Connect to qBittorrent and import any finished RSS torrent jobs (library move).

    Intended for a timer separate from full ``run_downloads()`` / RSS polling — e.g. every
    30 minutes so completed torrents are relocated even when no new RSS items appear.
    """
    from downloader import load_config

    config = load_config(cfg_path)
    qsettings = get_qbittorrent_settings(config)
    try:
        client = _qbittorrent_client(qsettings)
        _ = client.app.web_api_version
    except Exception as exc:
        print(f"📦 RSS import pass: qBittorrent unreachable ({exc})")
        return

    print("\n📦 RSS pending-import pass…")

    for show_index, show in enumerate(config.get("list") or []):
        if not isinstance(show, dict):
            continue
        if str(show.get("type") or "").strip().lower() not in ("rss_qbittorrent", "rss"):
            continue
        jobs = show.get("rss_torrent_jobs") or []
        if not isinstance(jobs, list) or not jobs:
            continue

        name = str(show.get("name") or "RSS show")
        raw_tid = show.get("thetvdb_id")
        tvdb_id_int = 0
        if raw_tid is not None and str(raw_tid).strip() != "":
            try:
                tvdb_id_int = int(raw_tid)
            except (TypeError, ValueError):
                tvdb_id_int = 0
        if tvdb_id_int <= 0:
            print(f"⚠️ {name}: skip import pass (needs thetvdb_id)")
            continue

        try:
            tvdb_bundle = get_cached_series_data(tvdb_id_int, config)
        except Exception as exc:
            tvdb_bundle = None
            print(
                f"⚠️ {name}: Skyhook unavailable ({exc}); using slug naming for import."
            )

        base_path = str(config.get("base_folder_download") or "")
        test_mode = bool(config.get("test"))
        include_ep_title = bool(config.get("thetvdb_include_episode_title", True))
        series_slug = str(show.get("series") or "")
        season_field = str(show.get("season") or "Season.01")
        ep_prefix = str(show.get("ep") or f"{series_slug}.S01E")

        _finish_rss_torrent_jobs_for_show(
            name=name,
            show=show,
            config=config,
            cfg_path=cfg_path,
            client=client,
            tvdb_bundle=tvdb_bundle,
            base_path=base_path,
            test_mode=test_mode,
            include_ep_title=include_ep_title,
            series_slug=series_slug,
            season_field=season_field,
            ep_prefix=ep_prefix,
            list_problems=None,
            emit_progress=True,
        )


def process_rss_qbittorrent_show(
    show: dict[str, Any],
    show_index: int,
    config: dict[str, Any],
    cfg_path: str,
    list_problems: list[str],
) -> None:
    from downloader import save_config

    save_cfg: Callable[..., None] = save_config

    name = str(show.get("name") or "RSS show")
    rss_url = str(show.get("rss_url") or show.get("link") or "").strip()
    if not rss_url:
        print(f"❌ {name}: missing rss_url")
        list_problems.append(f"{name}: rss_qbittorrent show has no rss_url")
        return

    raw_tid = show.get("thetvdb_id")
    tvdb_id_int = 0
    if raw_tid is not None and str(raw_tid).strip() != "":
        try:
            tvdb_id_int = int(raw_tid)
        except (TypeError, ValueError):
            tvdb_id_int = 0
    if tvdb_id_int <= 0:
        print(f"❌ {name}: rss_qbittorrent requires a positive thetvdb_id")
        list_problems.append(f"{name}: rss_qbittorrent requires thetvdb_id")
        return

    try:
        tvdb_bundle = get_cached_series_data(tvdb_id_int, config)
        print(
            f"📚 Skyhook / TheTVDB: {tvdb_bundle.name} "
            f"({len(tvdb_bundle.episodes)} episode(s) in index)"
        )
    except Exception as exc:
        tvdb_bundle = None
        print(
            f"⚠️ Could not load metadata from Skyhook ({exc}); "
            f"using scraped series/season slugs from config (series={show.get('series')!r})."
        )

    base_path = str(config.get("base_folder_download") or "")
    test_mode = bool(config.get("test"))
    include_ep_title = bool(config.get("thetvdb_include_episode_title", True))
    last_ep = int(show.get("last_ep") or 0)
    series_slug = str(show.get("series") or "")
    season_field = str(show.get("season") or "Season.01")
    ep_prefix = str(show.get("ep") or f"{series_slug}.S01E")
    show_name_regex = show.get("show_name_regex")
    erregex = show.get("episode_regex")

    if tvdb_bundle is None and not (
        show_name_regex and str(show_name_regex).strip()
    ):
        print(
            f"⚠️ {name}: set show_name_regex while Skyhook is unavailable, "
            f"otherwise no RSS titles can be matched."
        )

    qsettings = get_qbittorrent_settings(config)
    tag = _rss_tag(qsettings, show_index)

    jobs = show.get("rss_torrent_jobs")
    if not isinstance(jobs, list):
        jobs = []
        show["rss_torrent_jobs"] = jobs

    try:
        client = _qbittorrent_client(qsettings)
        _ = client.app.web_api_version
    except Exception as exc:
        print(f"❌ {name}: qBittorrent unreachable or login failed ({exc})")
        list_problems.append(f"{name}: qBittorrent {exc}")
        return

    # --- Finish completed jobs first ---
    relocated_ok = _finish_rss_torrent_jobs_for_show(
        name=name,
        show=show,
        config=config,
        cfg_path=cfg_path,
        client=client,
        tvdb_bundle=tvdb_bundle,
        base_path=base_path,
        test_mode=test_mode,
        include_ep_title=include_ep_title,
        series_slug=series_slug,
        season_field=season_field,
        ep_prefix=ep_prefix,
        list_problems=list_problems,
        emit_progress=True,
    )

    still = show["rss_torrent_jobs"]
    pending_eps: set[int] = set()
    for j in still:
        if not isinstance(j, dict):
            continue
        try:
            pending_eps.add(int(j["episode"]))
        except (KeyError, TypeError, ValueError):
            continue

    # --- Poll RSS for new episodes ---
    parsed = feedparser.parse(rss_url)
    if getattr(parsed, "bozo", False) and not (parsed.entries or []):
        err = getattr(parsed, "bozo_exception", None)
        print(f"❌ {name}: RSS parse error: {err}")
        list_problems.append(f"{name}: RSS parse {err}")
        return

    rss_debug = bool(config.get("debug"))
    rss_item_count = len(parsed.entries or [])
    skip_counts: dict[str, int] = defaultdict(int)
    skip_samples: dict[str, list[str]] = {}
    _MAX_SKIP_SAMPLES = 8

    def _skip_sample(reason: str, line: str) -> None:
        if not rss_debug:
            return
        bucket = skip_samples.setdefault(reason, [])
        if len(bucket) < _MAX_SKIP_SAMPLES:
            bucket.append(line[:500])

    candidates: list[tuple[int, str, str, str]] = []
    for entry in parsed.entries or []:
        title = str(getattr(entry, "title", "") or "").strip()
        if not title:
            skip_counts["empty_title"] += 1
            continue
        if not rss_title_matches_series(
            feed_title=title,
            regex_pattern=show_name_regex if show_name_regex else None,
            tvdb_bundle=tvdb_bundle,
        ):
            skip_counts["series_title_no_match"] += 1
            _skip_sample("series_title_no_match", title)
            continue
        epn = rss_episode_number(title, erregex if erregex else None)
        if epn is None:
            skip_counts["episode_number_unparsed"] += 1
            _skip_sample("episode_number_unparsed", title)
            continue
        if epn <= last_ep:
            skip_counts["episode_not_after_last_ep"] += 1
            _skip_sample(
                "episode_not_after_last_ep",
                f"{title}  (parsed EP{epn}, last_ep={last_ep})",
            )
            continue
        if epn in pending_eps:
            skip_counts["episode_already_pending"] += 1
            _skip_sample(
                "episode_already_pending",
                f"{title}  (EP{epn} already in rss_torrent_jobs)",
            )
            continue
        uri = _feed_item_torrent_uri(entry)
        if not uri:
            skip_counts["no_magnet_or_torrent_link"] += 1
            _skip_sample("no_magnet_or_torrent_link", title)
            if not rss_debug:
                print(f"⚠️ {name}: matched RSS item but no magnet/torrent link: {title!r}")
            continue
        dedupe = str(
            getattr(entry, "id", None)
            or getattr(entry, "guid", None)
            or getattr(entry, "link", None)
            or uri
        )
        candidates.append((epn, uri, title, dedupe))

    if rss_debug:
        snr_set = bool(show_name_regex and str(show_name_regex).strip())
        erx_set = bool(erregex and str(erregex).strip())
        skyhook = tvdb_bundle.name if tvdb_bundle is not None else "(none)"
        print(
            f"\n🔍 [{name}] RSS debug — "
            f"feed_items={rss_item_count}, last_ep={last_ep}, "
            f"pending_EP={sorted(pending_eps)}, "
            f"show_name_regex={'set' if snr_set else 'none'}, "
            f"episode_regex={'set' if erx_set else 'none'}, "
            f"skyhook_series={skyhook!r}"
        )
        parts = [f"{k}={v}" for k, v in sorted(skip_counts.items()) if v]
        print(f"🔍 [{name}] RSS skips (before adding candidates): " + (", ".join(parts) or "(none)"))
        if not candidates and rss_item_count > 0:
            print(
                f"🔍 [{name}] No episode candidates — sample skipped titles "
                f"(up to {_MAX_SKIP_SAMPLES} per reason):"
            )
            for reason in sorted(skip_samples.keys()):
                for line in skip_samples[reason]:
                    print(f"    · [{reason}] {line}")
    candidates.sort(key=lambda x: x[0])
    new_ep_candidates = len(candidates)
    torrents_added_this_run = 0
    seen_dedupe: set[str] = set()
    for epn, uri, title, dedupe in candidates:
        if dedupe in seen_dedupe:
            continue
        seen_dedupe.add(dedupe)
        print(f"\n➡️ {name}: RSS EP{epn} — {title}")
        if test_mode:
            print(f"TEST MODE: would add torrent to qBittorrent (tag={tag}): {uri[:80]}…")
            continue
        try:
            nh = _add_torrent_and_find_hash(client, uri, tag)
            if not nh:
                print(f"❌ {name}: could not resolve torrent hash after add for EP{epn}")
                list_problems.append(f"{name}: add torrent EP{epn} hash unknown")
                continue
            still.append(
                {
                    "hash": nh,
                    "episode": epn,
                    "item_title": title,
                    "source": dedupe,
                }
            )
            show["rss_torrent_jobs"] = still
            pending_eps.add(epn)
            save_cfg(config, cfg_path)
            torrents_added_this_run += 1
            print(f"✅ {name}: added qBittorrent job for EP{epn} ({nh[:8]}…)")
            print("💾 Config updated!")
        except Exception as exc:
            print(f"❌ {name}: qBittorrent add failed for EP{epn}: {exc}")
            list_problems.append(f"{name}: qBittorrent add EP{epn} {exc}")

    show["rss_torrent_jobs"] = still
    _report_pending_torrent_jobs_progress(
        client=client,
        show_name=name,
        jobs=still,
        emit_gui_first=True,
    )

    pending_hashes = [
        j
        for j in still
        if isinstance(j, dict) and str(j.get("hash") or "").strip()
    ]
    pending_eps_list: list[int] = []
    for j in pending_hashes:
        try:
            pending_eps_list.append(int(j["episode"]))
        except (KeyError, TypeError, ValueError):
            continue
    pending_eps_list = sorted(set(pending_eps_list))
    pending_eps_str = ", ".join(str(e) for e in pending_eps_list) if pending_eps_list else "none"

    print(
        f"\n📊 {name}: RSS pass finished — "
        f"last_ep={int(show.get('last_ep') or 0)}, "
        f"feed_items={rss_item_count}, "
        f"new_episode_candidates={new_ep_candidates}, "
        f"moved_to_library_this_run={relocated_ok}, "
        f"torrents_added_this_run={torrents_added_this_run}, "
        f"pending_torrent_jobs={len(pending_hashes)} "
        f"(EP {pending_eps_str})"
        + (" [TEST MODE]" if test_mode else "")
    )
