"""Series metadata via Sonarr Skyhook (TheTVDB-backed, no API key)."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

DEFAULT_SKYHOOK_BASE = "https://skyhook.sonarr.tv/v1/tvdb"

_SERIES_CACHE: dict[int, tuple["TVDBSeriesData", float]] = {}
_SERIES_CACHE_TTL_SEC = 3600.0


@dataclass
class TVDBSeriesData:
    series_id: int
    name: str
    year: str
    seasons: list[dict[str, Any]]
    episodes: list[dict[str, Any]]
    fetched_at: float = field(default_factory=time.time)


def get_skyhook_base(config: Optional[dict[str, Any]]) -> str:
    env = (os.environ.get("SKYHOOK_BASE_URL") or "").strip().rstrip("/")
    if env:
        return env
    if config:
        b = config.get("skyhook_base_url")
        if b is not None and str(b).strip():
            return str(b).strip().rstrip("/")
    return DEFAULT_SKYHOOK_BASE


def get_skyhook_language(config: Optional[dict[str, Any]]) -> str:
    env = (os.environ.get("SKYHOOK_LANGUAGE") or "").strip()
    if env:
        return env
    if config and config.get("skyhook_language"):
        return str(config["skyhook_language"]).strip()
    return "en"


def sanitize_path_component(s: str, *, max_len: int = 200) -> str:
    """Strip characters unsafe in file paths (Windows + cross-platform)."""
    if not s:
        return "Unknown"
    out = "".join(c for c in s if c not in '<>:"/\\|?\n\r\t\x00')
    out = out.strip(" .")
    if len(out) > max_len:
        out = out[: max_len - 1].rstrip() + "…"
    return out or "Unknown"


def parse_scraped_season_number(season_field: str) -> Optional[int]:
    """Parse ``Season.05`` / ``season 3`` → int."""
    m = re.search(r"Season\.(\d+)", season_field or "", re.IGNORECASE)
    if m:
        return int(m.group(1))
    m2 = re.search(r"(\d+)", season_field or "")
    if m2:
        return int(m2.group(1))
    return None


def _series_year_from_skyhook(rec: dict[str, Any]) -> str:
    fa = rec.get("firstAired") or ""
    if fa and len(fa) >= 4:
        return fa[:4]
    return ""


def _episode_from_skyhook(ep: dict[str, Any]) -> dict[str, Any]:
    """Normalize Skyhook episode JSON for :func:`match_episode_record`."""
    tid = ep.get("tvdbId")
    return {
        "id": int(tid) if tid is not None else 0,
        "seasonNumber": ep.get("seasonNumber"),
        "number": ep.get("episodeNumber"),
        "name": ep.get("title") or "",
        "absoluteNumber": ep.get("absoluteEpisodeNumber"),
    }


def fetch_skyhook_series(
    tvdb_id: int,
    *,
    lang: str,
    base_url: str,
) -> dict[str, Any]:
    url = f"{base_url}/shows/{lang}/{tvdb_id}"
    r = requests.get(
        url,
        headers={"Accept": "application/json"},
        timeout=90,
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise ValueError("Skyhook returned invalid JSON")
    return data


def load_series_data(
    series_id: int,
    config: Optional[dict[str, Any]] = None,
) -> TVDBSeriesData:
    lang = get_skyhook_language(config)
    base = get_skyhook_base(config)
    raw = fetch_skyhook_series(series_id, lang=lang, base_url=base)
    name = (raw.get("title") or "").strip() or f"Series {series_id}"
    year = _series_year_from_skyhook(raw)
    seasons = list(raw.get("seasons") or [])
    episodes = [_episode_from_skyhook(ep) for ep in (raw.get("episodes") or [])]
    return TVDBSeriesData(
        series_id=series_id,
        name=name,
        year=year,
        seasons=seasons,
        episodes=episodes,
    )


def get_cached_series_data(
    series_id: int,
    config: Optional[dict[str, Any]] = None,
    *,
    max_age_sec: float = _SERIES_CACHE_TTL_SEC,
) -> TVDBSeriesData:
    now = time.time()
    hit = _SERIES_CACHE.get(series_id)
    if hit and now - hit[1] < max_age_sec:
        return hit[0]
    data = load_series_data(series_id, config)
    _SERIES_CACHE[series_id] = (data, now)
    return data


def series_root_folder(data: TVDBSeriesData) -> str:
    """``Series Name (2010)`` style folder."""
    if data.year:
        return sanitize_path_component(f"{data.name} ({data.year})")
    return sanitize_path_component(data.name)


def folder_series_for_show(
    show: dict[str, Any],
    config: Optional[dict[str, Any]] = None,
) -> str:
    """
    Series root folder label for UI: Skyhook ``Series Name (year)`` when lookup succeeds;
    otherwise the scraped ``series`` slug (same as download fallback).
    """
    raw = show.get("thetvdb_id")
    tid = 0
    if raw is not None and str(raw).strip():
        try:
            tid = int(raw)
        except (TypeError, ValueError):
            tid = 0
    if tid > 0:
        try:
            bundle = get_cached_series_data(tid, config)
            return series_root_folder(bundle)
        except Exception:
            pass
    return str(show.get("series") or "")


def season_folder_name(
    season_number: int,
    seasons: list[dict[str, Any]],
) -> str:
    """
    Prefer season *name* when present (TVDB v4 / Skyhook with name).
    Skyhook often only provides ``seasonNumber`` → fall back to ``Season NN``.
    """
    for s in seasons:
        raw_n = s.get("number")
        if raw_n is None:
            raw_n = s.get("seasonNumber")
        if raw_n is None:
            continue
        try:
            num = int(raw_n)
        except (TypeError, ValueError):
            continue
        if num != int(season_number):
            continue
        raw_name = (s.get("name") or "").strip()
        if raw_name:
            return sanitize_path_component(raw_name)
        break
    return f"Season {int(season_number):02d}"


def match_episode_record(
    episodes: list[dict[str, Any]],
    site_ep_num: int,
    scraped_season: Optional[int],
) -> Optional[dict[str, Any]]:
    """
    Map site episode index to metadata:

    1. ``absoluteNumber == site_ep_num``
    2. Scraped season + ``number == site_ep_num`` (in-season index)
    3. Default order (season, episode, id)
    """
    sn = site_ep_num
    for ep in episodes:
        absn = ep.get("absoluteNumber")
        if absn is not None and int(absn) == sn:
            return ep

    if scraped_season is not None:
        for ep in episodes:
            try:
                s_num = int(
                    ep.get("seasonNumber")
                    if ep.get("seasonNumber") is not None
                    else -1
                )
                e_num = int(ep.get("number") if ep.get("number") is not None else -1)
            except (TypeError, ValueError):
                continue
            if s_num == int(scraped_season) and e_num == sn:
                return ep

    def _snum(e: dict[str, Any]) -> int:
        v = e.get("seasonNumber")
        return int(v) if v is not None else 0

    def _enum(e: dict[str, Any]) -> int:
        v = e.get("number")
        return int(v) if v is not None else 0

    ordered = sorted(
        episodes,
        key=lambda e: (_snum(e), _enum(e), int(e.get("id") or 0)),
    )
    if 1 <= sn <= len(ordered):
        return ordered[sn - 1]
    return None


def episode_filename(
    *,
    series_display_name: str,
    season_num: int,
    episode_num: int,
    episode_title: Optional[str],
    extension: str,
    include_episode_title: bool,
) -> str:
    """``Series A S01E03.mkv`` or ``Series A S01E03 - Pilot.mkv``."""
    base = sanitize_path_component(series_display_name, max_len=120)
    tag = f"S{int(season_num):02d}E{int(episode_num):02d}"
    stem = f"{base} {tag}"
    if include_episode_title and episode_title:
        t = sanitize_path_component(episode_title.strip(), max_len=80)
        if t:
            stem = f"{stem} - {t}"
    ext = extension if extension.startswith(".") else f".{extension}"
    full = stem + ext
    if len(full) > 240:
        stem = stem[: 240 - len(ext) - 3] + "…"
        full = stem + ext
    return full


def clear_caches() -> None:
    _SERIES_CACHE.clear()
