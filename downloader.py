from __future__ import annotations

import errno
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

# Optional hook for structured file download events (e.g. web GUI progress bar).
# Each event is a dict with key "kind": file_start | file_progress | file_complete | file_error.
_download_progress_hook: Optional[Callable[[dict[str, Any]], None]] = None


def set_download_progress_hook(
    hook: Optional[Callable[[dict[str, Any]], None]],
) -> None:
    global _download_progress_hook
    _download_progress_hook = hook


import requests
from filelock import FileLock
from bs4 import BeautifulSoup
from tqdm import tqdm
from playwright.sync_api import sync_playwright

from tvdb_naming import (
    episode_filename,
    get_cached_series_data,
    match_episode_record,
    parse_scraped_season_number,
    season_folder_name,
    series_root_folder,
)


def extract_links_episode(series_url: str) -> dict[str, str]:
    response = requests.get(series_url)
    soup = BeautifulSoup(response.content, "html.parser")

    episodes = {}

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        title_num = a_tag.find("div", class_="epl-num")

        if title_num:  # Make sure the div exists
            episode_number = title_num.text.strip()
            episodes[episode_number] = href  # Use the cleaned-up number as key

    # for num,link in episodes.items():
    #     print(num,link)

    return episodes

def filter_link_episode(series_url: str, last_episode: int) -> dict[str, str]:
    episodes = extract_links_episode(series_url)

    # Convert keys to integers for comparison
    filtered = {
        ep_num: link
        for ep_num, link in episodes.items()
        if ep_num.isdigit() and int(ep_num) > int(last_episode)
    }

    return filtered


def normalize_legacy_show_entry(show: dict[str, Any]) -> None:
    """Migrate legacy ``serie`` / ``saison`` keys to ``series`` / ``season`` (in place)."""
    if "series" not in show and "serie" in show:
        show["series"] = show["serie"]
    show.pop("serie", None)
    if "season" not in show and "saison" in show:
        show["season"] = show["saison"]
    show.pop("saison", None)


def extract_mediafire_1080p_link(episode_url: str) -> Optional[dict[str, Optional[str]]]:
    response = requests.get(episode_url)

    if response.status_code != 200:
        print(f"❌ Failed to load page, status code: {response.status_code}")
        return None

    soup = BeautifulSoup(response.content, "html.parser")
    blocks = soup.find_all("div", class_="soraddlx")

    for block in blocks:
        heading = block.find("h3")
        if heading and "subtitle english" in heading.text.strip().lower():
            links_container = block.find("div", class_="soraurlx")
            if not links_container:
                continue

            strong_tag = links_container.find("strong")
            if strong_tag and "1080" in strong_tag.text:
                # Start extracting all 3 links
                links = {"terabox": None, "mirror": None, "mediafire": None}
                for link in links_container.find_all("a", href=True):
                    href = link["href"].lower()
                    if "terabox" in href:
                        links["terabox"] = link["href"]
                    elif "mirrored.to" in href:
                        links["mirror"] = link["href"]
                    elif "mediafire.com" in href:
                        links["mediafire"] = link["href"]
                return links

    return None


def get_new_mediafire_links(
    series_url: str, last_seen_episode: int
) -> dict[str, dict[str, str]]:
    new_episodes = filter_link_episode(series_url, last_seen_episode)
    results = {}

    for ep, link in new_episodes.items():
        link_data = extract_mediafire_1080p_link(link)
        if link_data and link_data["mediafire"]:
            print(f"Episode {ep} - ✅ Mediafire 1080p Link: {link_data['mediafire']}")
            results[ep] = {
                "page": link,
                "mediafire": link_data["mediafire"]
            }
        else:
            print(f"Episode {ep} - ❌ Mediafire 1080p link not found from this page {link}")

    return results



# def get_true_mediafire_link_playwright(url, headless):
#     with sync_playwright() as p:
#         browser = p.chromium.launch(headless=headless)  # 👈 Turn off headless to test
#         context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
#         page = context.new_page()
#         print(f"🔍 Navigating to: {url}")
#         page.goto(url)

#         try:
#             page.wait_for_selector("#downloadButton", timeout=30000)
#             link = page.query_selector("#downloadButton").get_attribute("href")
#             print(f"✅ Found direct link: {link}")
#             return link
#         except Exception as e:
#             print("❌ Still blocked:", e)
#             page.screenshot(path="cloudflare_block.png", full_page=True)
#             return None
#         finally:
#             browser.close()

def get_true_mediafire_link_playwright(
    url: str, headless: bool = True
) -> Optional[str]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ))
        page = context.new_page()
        print(f"🔍 Navigating to: {url}")
        # Mediafire keeps long-lived analytics/ad requests open; "networkidle" often never fires.
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        try:
            # Wait until the button exists and is visible
            page.wait_for_selector("#downloadButton", timeout=45000)
            download_btn = page.query_selector("#downloadButton")
            if not download_btn:
                print("❌ downloadButton selector matched nothing.")
                return None

            # Retry up to 10 seconds if href is still a placeholder
            for i in range(10):
                href = download_btn.get_attribute("href")
                if href and href != "#":
                    print(f"✅ Found direct download link: {href}")
                    return href
                print(f"⏳ Waiting for real link to appear... ({i+1}/10)")
                time.sleep(1)

            print("❌ Timeout: Button appeared but no valid download link.")
        except Exception as e:
            print("❌ Error occurred:", e)
            page.screenshot(path="mediafire_debug.png", full_page=True)
        finally:
            browser.close()

        return None

def download_file(
    url: str,
    filename: str,
    *,
    file_meta: Optional[dict[str, Any]] = None,
) -> None:
    hook = _download_progress_hook
    base_name = os.path.basename(filename)
    meta = file_meta or {}
    event_base: dict[str, Any] = {
        "show_link": meta.get("show_link", ""),
        "show_name": meta.get("show_name", ""),
        "series": meta.get("series", ""),
        "episode": meta.get("episode"),
        "filename": meta.get("filename") or base_name,
    }

    try:
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))

            if hook is not None:
                hook(
                    {
                        "kind": "file_start",
                        **event_base,
                        "total": total,
                    }
                )
                downloaded = 0
                last_emit = time.monotonic()
                last_pct_bucket = -1
                chunk_size = 8192

                def emit_progress(force: bool = False) -> None:
                    nonlocal last_emit, last_pct_bucket
                    now = time.monotonic()
                    pct_bucket = (
                        min(100, int(100 * downloaded / total)) if total > 0 else -1
                    )
                    if not force:
                        if now - last_emit < 0.35:
                            if total > 0 and pct_bucket >= last_pct_bucket + 2:
                                pass
                            else:
                                return
                    last_emit = now
                    if total > 0:
                        last_pct_bucket = pct_bucket
                    hook(
                        {
                            "kind": "file_progress",
                            **event_base,
                            "downloaded": downloaded,
                            "total": total,
                        }
                    )

                with open(filename, "wb") as f:
                    for chunk in r.iter_content(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        emit_progress(force=False)
                    if total <= 0 or last_pct_bucket < 100:
                        emit_progress(force=True)
                hook(
                    {
                        "kind": "file_complete",
                        **event_base,
                        "downloaded": downloaded,
                        "total": total,
                    }
                )
                return

            with open(filename, "wb") as f, tqdm(
                total=total, unit="B", unit_scale=True, desc=filename
            ) as bar:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
                    bar.update(len(chunk))
    except Exception as exc:
        if hook is not None:
            hook(
                {
                    "kind": "file_error",
                    **event_base,
                    "error": str(exc),
                }
            )
        raise


def create_donghua_structure(base_path: str, series: str, season: str) -> None:
    series_path = os.path.join(base_path, series)
    season_path = os.path.join(series_path, season)
    os.makedirs(season_path, exist_ok=True)


def _config_lock_path(path: str) -> str:
    return str(Path(path).resolve()) + ".lock"


def load_config(path: str) -> dict[str, Any]:
    lock = FileLock(_config_lock_path(path), timeout=120)
    with lock:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    for show in data.get("list") or []:
        if isinstance(show, dict):
            normalize_legacy_show_entry(show)
    return data


def write_config_json(path: str, config: dict[str, Any]) -> None:
    """Write JSON config; uses atomic replace when possible, falls back for bind mounts (e.g. Docker)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(config, indent=4, ensure_ascii=False) + "\n"
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(str(tmp), str(p))
    except OSError as exc:
        # os.replace over an existing bind-mounted file often raises EBUSY/EXDEV on Docker Desktop.
        _rename_fallback = (errno.EBUSY, errno.EXDEV)
        if hasattr(errno, "ETXTBSY"):
            _rename_fallback += (errno.ETXTBSY,)
        if exc.errno not in _rename_fallback:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        with open(p, "w", encoding="utf-8") as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def save_config(config: dict[str, Any], path: str) -> None:
    lock = FileLock(_config_lock_path(path), timeout=120)
    with lock:
        write_config_json(path, config)


def create_config_from_link(
    link: str,
    last_ep: int = 0,
    missing_ep: Optional[list[Any]] = None,
) -> dict[str, Any]:
    if missing_ep is None:
        missing_ep = []

    response = requests.get(link)
    soup = BeautifulSoup(response.content, "html.parser")

    # Extract raw title
    title_tag = soup.find("h1", class_="entry-title")
    if not title_tag:
        raise ValueError("❌ Could not find <h1 class='entry-title'> on the page.")

    full_title = title_tag.text.strip()

    # Extract season number
    season_match = re.search(r'[Ss]eason\s*(\d+)', full_title)
    season_number = int(season_match.group(1)) if season_match else 1
    season_str = f"{season_number:02d}"

    # Clean name (remove "Season X")
    name = re.sub(r'[Ss]eason\s*\d+', '', full_title).strip()

    # Normalize for file-friendly identifiers
    normalized = re.sub(r"[^\w]", ".", name).strip(".")
    normalized = re.sub(r"\.+", ".", normalized)  # Replace multiple dots

    return {
        "name": name,
        "link": link,
        "series": normalized,
        "season": f"Season.{season_str}",
        "ep": f"{normalized}.S{season_str}E",
        "last_ep": last_ep,
        "missing_ep": missing_ep
    }


def run_downloads(config_path: str = "config.json") -> None:
    """Load *config_path*, download new episodes for each show, update config on disk."""
    cfg_path = config_path
    config = load_config(cfg_path)
    BASE_DOWNLOAD_PATH = config["base_folder_download"]
    TEST = config["test"]
    HEADLESS = config["headless"]

    list_problems = []

    for show in config["list"]:
        name = show["name"]
        link = show["link"]
        season = show["season"]
        # Scraped slug naming (same as create_config_from_link); used when Skyhook is off or fails.
        series = show["series"]
        ep_prefix = show["ep"]
        last_ep = show["last_ep"]
        print("---------------------------")
        print(f"\n📺 Processing: {name}")

        raw_tid = show.get("thetvdb_id")
        tvdb_id_int = 0
        if raw_tid is not None and str(raw_tid).strip() != "":
            try:
                tvdb_id_int = int(raw_tid)
            except (TypeError, ValueError):
                tvdb_id_int = 0

        tvdb_bundle = None
        if tvdb_id_int > 0:
            try:
                tvdb_bundle = get_cached_series_data(tvdb_id_int, config)
                print(
                    f"📚 Skyhook / TheTVDB: {tvdb_bundle.name} "
                    f"({len(tvdb_bundle.episodes)} episode(s) in index)"
                )
            except Exception as exc:
                print(
                    f"⚠️ Could not load metadata from Skyhook ({exc}); "
                    f"using scraped series/season names from the site (series={series!r})."
                )

        use_tvdb_layout = tvdb_bundle is not None
        include_ep_title = bool(config.get("thetvdb_include_episode_title", True))

        if not use_tvdb_layout:
            create_donghua_structure(BASE_DOWNLOAD_PATH, series, season)

        episode_links = filter_link_episode(link, last_ep)
        if not episode_links:
            print("✅ No new episodes.")
            continue

        # ? Step 1: List upcoming downloads
        print(f"\n📋 Episodes to download (EP > {last_ep}):")
        sorted_episodes = sorted(
            ((int(ep), url) for ep, url in episode_links.items()),
            key=lambda x: x[0]
        )
        for ep_num, _ in sorted_episodes:
            print(f"  - Episode {ep_num}")
        missing_list = []
        # ? Step 2: Start downloading
        for ep_num, page_url in sorted_episodes:
            print(f"\n➡️ Episode {ep_num} - Page: {page_url}")

            # Get 1080p English links
            links = extract_mediafire_1080p_link(page_url)
            if not links or not links["mediafire"]:
                print(f"❌ No mediafire link found for episode {ep_num} {page_url}.")
                missing_list.append(ep_num)
                show["missing_ep"] = missing_list
                list_problems.append(f"{series} season: {season} episode: {ep_prefix} link: {page_url} No mediafire link found ")
                continue  # continue sequentially

            # Get direct download link
            direct_link = get_true_mediafire_link_playwright(links["mediafire"],HEADLESS)
            if not direct_link:
                print(f"❌ Could not resolve direct link {ep_num} {page_url} {direct_link}.")
                missing_list.append(ep_num)
                show["missing_ep"] = missing_list
                list_problems.append(f"{series} season: {season} episode: {ep_prefix} link: {page_url} Could not resolve direct link {direct_link}")
                continue  # continue sequentially

            # Build output path (TheTVDB library layout or legacy)
            if use_tvdb_layout and tvdb_bundle is not None:
                scraped_season = parse_scraped_season_number(str(show.get("season", "")))
                ep_rec = match_episode_record(
                    tvdb_bundle.episodes,
                    ep_num,
                    scraped_season,
                )
                if ep_rec is not None:
                    s_num = int(ep_rec.get("seasonNumber") or 0)
                    e_num = int(ep_rec.get("number") or 0)
                    ep_title = (ep_rec.get("name") or "").strip() or None
                else:
                    print(
                        f"⚠️ No Skyhook/TVDB episode match for site episode {ep_num}; "
                        "using scraped season and site number in the filename."
                    )
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
                    extension="mp4",
                    include_episode_title=include_ep_title,
                )
                create_donghua_structure(BASE_DOWNLOAD_PATH, root_folder, season_folder)
                output_path = os.path.join(
                    BASE_DOWNLOAD_PATH,
                    root_folder,
                    season_folder,
                    filename,
                )
                meta_series = display_name
            else:
                filename = f"{ep_prefix}{str(ep_num).zfill(2)}.mp4"
                output_path = os.path.join(BASE_DOWNLOAD_PATH, series, season, filename)
                meta_series = series

            try:
                if not TEST:
                    download_file(
                        direct_link,
                        output_path,
                        file_meta={
                            "show_link": link,
                            "show_name": name,
                            "series": meta_series,
                            "episode": ep_num,
                            "filename": filename,
                        },
                    )
                    print(f"✅ Downloaded: {filename}")
                    show["last_ep"] = ep_num  # Update last successfully downloaded
                    save_config(config, cfg_path)
                    print("\n💾 Config updated!")
                else:
                    print(
                        f"TEST MODE: Skipping download to {output_path} "
                        f"with link: {direct_link}"
                    )
            except Exception as e:
                print(f"❌ Download failed: {e}")
                missing_list.append(ep_num)
                show["missing_ep"] = missing_list
                list_problems.append(f"{series} season: {season} episode: {ep_prefix} link: {page_url} Error while downloading link {direct_link}")
                continue  # continue sequentially

    for problem in list_problems:
        print(problem)
