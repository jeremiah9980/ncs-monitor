#!/usr/bin/env python3
"""
GameChanger Hit Clip Downloader
================================

Local-only helper for a parent/coach who has authorized access to a GameChanger
team. It finds a player's last N batting lines from reports/gc-player-stats.json,
filters hit outcomes, attempts to discover video clip URLs from the authenticated
GameChanger box-score pages, downloads clips with yt-dlp, and optionally uploads
those clips to a shared Google Drive folder using rclone.

Why local-only:
  - GameChanger requires your logged-in browser session.
  - Video clips are authenticated content.
  - Google Drive upload uses your local rclone config.

Example:
  python gc_hit_clip_downloader.py \
    --player "Kassidy Cargill" \
    --games 10 \
    --drive-folder-id 1XBQ7A13fetXinlxl5ighfWTVSAnUyaeG \
    --drive-remote gdrive \
    --headful

Supported hit filters:
  single, double, triple, home_run, hit

Notes:
  - The existing GC stat data usually stores AB/R/H/RBI/BB/SO totals, not exact
    play-by-play hit type. When play result text is unavailable, any game with H>0
    is treated as a generic hit candidate.
  - Clip discovery is best-effort because GC page markup/API names can change.
    The script saves a manifest showing which games were downloaded and which
    need manual review.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    sys.exit("Missing Selenium deps. Run: pip install -r requirements-gc.txt")

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "reports" / "gc-player-stats.json"
OUT_ROOT = ROOT / "gc_clips"

if sys.platform == "darwin":
    DEFAULT_PROFILE = Path.home() / "Library/Application Support/Google/Chrome/Default"
elif sys.platform.startswith("win"):
    DEFAULT_PROFILE = Path.home() / "AppData/Local/Google/Chrome/User Data/Default"
else:
    DEFAULT_PROFILE = Path.home() / ".config/google-chrome/Default"

PROFILE_SKIP = {
    "Cache", "Code Cache", "GPUCache", "Service Worker", "DawnCache",
    "DawnGraphiteCache", "DawnWebGPUCache", "ShaderCache", "GrShaderCache",
    "OptimizationGuide", "Download Service", "blob_storage", "IndexedDB",
    "File System", "Site Characteristics Database", "Safe Browsing",
}

VIDEO_RE = re.compile(r"https?://[^\"'\\\s<>]+(?:\.m3u8|\.mp4|/hls/|/video/|clip|clips)[^\"'\\\s<>]*", re.I)
HIT_WORDS = {
    "hit": ["hit", "single", "double", "triple", "home run", "homer", "inside the park"],
    "single": ["single", "singles"],
    "double": ["double", "doubles"],
    "triple": ["triple", "triples"],
    "home_run": ["home run", "homer", "homers", "inside the park"],
}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip()).strip("-").lower()
    return s or "player"


def as_int(value: Any) -> int:
    try:
        return int(str(value or "0").strip())
    except ValueError:
        return 0


def clone_profile(src: Path) -> Path:
    if not src.exists():
        sys.exit(f"Chrome profile not found: {src}\nOpen chrome://version in Chrome and copy the Profile Path, then pass --profile.")
    dst = Path(tempfile.mkdtemp(prefix="gc_clip_profile_")) / "Default"
    log(f"Cloning Chrome profile: {src}")
    def ignore(dirpath: str, names: list[str]) -> set[str]:
        return {name for name in names if name in PROFILE_SKIP or name.endswith((".tmp", ".lock"))}
    shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=True)
    return dst


def build_driver(profile: Path, headful: bool) -> webdriver.Chrome:
    opts = Options()
    if not headful:
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1440,1100")
    opts.add_argument(f"--user-data-dir={profile.parent}")
    opts.add_argument(f"--profile-directory={profile.name}")
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL", "browser": "ALL"})
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)


@dataclass
class Candidate:
    player: str
    team: str
    gc_team: str
    game_id: str
    game_date: str
    opponent: str
    source_url: str
    positions: str
    stats: dict[str, Any]
    hit_type: str
    downloaded: bool = False
    clip_urls: list[str] | None = None
    files: list[str] | None = None
    note: str = ""


def iter_player_lines(report: dict[str, Any], player_name: str) -> Iterable[dict[str, Any]]:
    target = player_name.strip().lower()
    players = report.get("players")
    if isinstance(players, list):
        for p in players:
            if str(p.get("name", "")).strip().lower() == target:
                for line in p.get("games", []) or p.get("lines", []) or []:
                    yield {**line, "_player_name": p.get("name", player_name)}
    elif isinstance(players, dict):
        for _pid, p in players.items():
            if str(p.get("name", "")).strip().lower() == target:
                for line in p.get("games", []) or p.get("lines", []) or []:
                    yield {**line, "_player_name": p.get("name", player_name)}

    # Fallback for report variants that are keyed by player display name.
    for key, p in (players or {}).items() if isinstance(players, dict) else []:
        if str(key).strip().lower() == target and isinstance(p, dict):
            for line in p.get("games", []) or p.get("lines", []) or []:
                yield {**line, "_player_name": p.get("name", player_name)}


def infer_hit_type(line: dict[str, Any], wanted: set[str]) -> str | None:
    stats = line.get("stats") or line.get("batting") or {}
    if isinstance(stats, str):
        try:
            stats = json.loads(stats)
        except Exception:
            stats = {}

    text = " ".join(str(line.get(k, "")) for k in ("result", "play", "description", "event", "summary", "note")).lower()
    for hit_type, words in HIT_WORDS.items():
        if hit_type in wanted and any(w in text for w in words):
            return hit_type

    if "home_run" in wanted and as_int(stats.get("HR") or stats.get("Homeruns") or stats.get("Home Runs")) > 0:
        return "home_run"
    if "triple" in wanted and as_int(stats.get("3B") or stats.get("Triples")) > 0:
        return "triple"
    if "double" in wanted and as_int(stats.get("2B") or stats.get("Doubles")) > 0:
        return "double"
    if "single" in wanted and as_int(stats.get("1B") or stats.get("Singles")) > 0:
        return "single"
    if "hit" in wanted and as_int(stats.get("H")) > 0:
        return "hit"
    if ({"single", "double", "triple", "home_run"} & wanted) and as_int(stats.get("H")) > 0:
        return "hit"
    return None


def load_candidates(player: str, games: int, hit_types: set[str], team_filter: str | None) -> list[Candidate]:
    if not REPORT.exists():
        sys.exit(f"Missing report: {REPORT}\nRun ./export_gc_stats.sh --max-games 999 first.")
    report = json.loads(REPORT.read_text())
    rows = []
    for line in iter_player_lines(report, player):
        if str(line.get("section", "batting")).lower() not in ("", "batting"):
            continue
        team = str(line.get("team") or line.get("roster_team") or "")
        gc_team = str(line.get("gc_team") or "")
        if team_filter and team_filter.lower() not in f"{team} {gc_team}".lower():
            continue
        source_url = str(line.get("source_url") or line.get("url") or "")
        if not source_url:
            continue
        ht = infer_hit_type(line, hit_types)
        if not ht:
            continue
        stats = line.get("stats") if isinstance(line.get("stats"), dict) else {}
        rows.append(Candidate(
            player=str(line.get("_player_name") or player),
            team=team,
            gc_team=gc_team,
            game_id=str(line.get("game_id") or ""),
            game_date=str(line.get("game_date") or line.get("date") or ""),
            opponent=str(line.get("opponent") or ""),
            source_url=source_url,
            positions=str(line.get("positions") or ""),
            stats=stats,
            hit_type=ht,
        ))
    rows.sort(key=lambda c: c.game_date, reverse=True)
    return rows[:games]


def extract_urls_from_performance(driver: webdriver.Chrome) -> set[str]:
    urls: set[str] = set()
    try:
        logs = driver.get_log("performance")
    except Exception:
        return urls
    for entry in logs:
        try:
            msg = json.loads(entry["message"])["message"]
            params = msg.get("params", {})
            req = params.get("request", {})
            resp = params.get("response", {})
            for url in (req.get("url"), resp.get("url")):
                if url and VIDEO_RE.search(url):
                    urls.add(url)
        except Exception:
            continue
    return urls


def extract_urls_from_dom(driver: webdriver.Chrome) -> set[str]:
    urls: set[str] = set()
    html = driver.page_source or ""
    urls.update(m.group(0).replace("\\u002F", "/") for m in VIDEO_RE.finditer(html))
    for attr in ("src", "href"):
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, f"video[{attr}], source[{attr}], a[{attr}]"):
                value = el.get_attribute(attr)
                if value and VIDEO_RE.search(value):
                    urls.add(value)
        except Exception:
            pass
    return urls


def try_open_player_clip_context(driver: webdriver.Chrome, player: str) -> None:
    # Best effort: search/click a visible player row or video/highlight control.
    names = [player]
    if " " in player:
        first, last = player.split()[0], player.split()[-1]
        names.extend([f"{first} {last[0]}", first])
    for name in names:
        try:
            el = WebDriverWait(driver, 3).until(EC.presence_of_element_located((By.XPATH, f"//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{name.lower()}')]")))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            time.sleep(0.8)
            try:
                el.click()
                time.sleep(1.2)
            except Exception:
                pass
            break
        except Exception:
            continue
    for word in ("video", "clip", "highlight", "plays"):
        try:
            buttons = driver.find_elements(By.XPATH, f"//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{word}')]")
            for b in buttons[:3]:
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", b)
                    b.click()
                    time.sleep(1.0)
                except Exception:
                    continue
        except Exception:
            pass


def discover_clip_urls(driver: webdriver.Chrome, candidate: Candidate, wait_seconds: int) -> list[str]:
    log(f"Opening {candidate.game_date} {candidate.opponent}: {candidate.source_url}")
    driver.get(candidate.source_url)
    time.sleep(3)
    try_open_player_clip_context(driver, candidate.player)
    end = time.time() + wait_seconds
    urls: set[str] = set()
    while time.time() < end:
        urls.update(extract_urls_from_dom(driver))
        urls.update(extract_urls_from_performance(driver))
        time.sleep(1)
    # Prefer direct clip/video URLs, but preserve all candidates.
    return sorted(urls)


def run_yt_dlp(url: str, out_template: Path) -> list[Path]:
    cmd = ["yt-dlp", "--no-playlist", "--restrict-filenames", "-o", str(out_template), url]
    subprocess.run(cmd, check=True)
    return list(out_template.parent.glob(out_template.name.replace("%(ext)s", "*")))


def upload_to_drive(local_dir: Path, drive_remote: str, folder_id: str) -> None:
    if not shutil.which("rclone"):
        raise RuntimeError("rclone not found. Install with: brew install rclone && rclone config")
    cmd = ["rclone", "copy", str(local_dir), f"{drive_remote}:", "--drive-root-folder-id", folder_id, "--progress"]
    log("Uploading clips to Google Drive folder with rclone…")
    subprocess.run(cmd, check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Download GameChanger hit clips for a player and optionally upload to Google Drive.")
    ap.add_argument("--player", required=True, help="Exact player display name, e.g. 'Kassidy Cargill'")
    ap.add_argument("--games", type=int, default=10, help="Last N candidate games to inspect")
    ap.add_argument("--hit-types", default="hit,single,double,triple,home_run", help="Comma list: hit,single,double,triple,home_run")
    ap.add_argument("--team", default="", help="Optional team filter, e.g. Hotshots")
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="Chrome profile path")
    ap.add_argument("--headful", action="store_true", help="Show browser while discovering clips")
    ap.add_argument("--wait", type=int, default=8, help="Seconds to wait per game page while capturing video URLs")
    ap.add_argument("--out", type=Path, default=OUT_ROOT, help="Local output root")
    ap.add_argument("--drive-folder-id", default="", help="Google Drive folder ID to upload into")
    ap.add_argument("--drive-remote", default="gdrive", help="rclone Google Drive remote name")
    ap.add_argument("--dry-run", action="store_true", help="Build manifest only; do not download/upload")
    args = ap.parse_args()

    hit_types = {x.strip() for x in args.hit_types.split(",") if x.strip()}
    out_dir = args.out / slugify(args.player) / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    candidates = load_candidates(args.player, args.games, hit_types, args.team or None)
    if not candidates:
        sys.exit("No hit candidates found. Run export_gc_stats first, check the player name, or remove --team filter.")

    log(f"Found {len(candidates)} hit candidate games for {args.player}.")
    profile_clone = clone_profile(args.profile)
    driver = build_driver(profile_clone, args.headful)

    try:
        for idx, c in enumerate(candidates, 1):
            try:
                urls = discover_clip_urls(driver, c, args.wait)
                c.clip_urls = urls
                if not urls:
                    c.note = "No video URLs discovered. Open source_url manually and confirm clips are available for this game/player."
                    continue
                if args.dry_run:
                    c.note = "Dry run: URLs discovered but not downloaded."
                    continue
                c.files = []
                for u_idx, url in enumerate(urls[:3], 1):
                    base = f"{idx:02d}-{c.game_date}-{slugify(c.opponent)}-{c.hit_type}-{u_idx}.%(ext)s"
                    files = run_yt_dlp(url, out_dir / base)
                    c.files.extend(str(p.relative_to(ROOT)) for p in files)
                c.downloaded = bool(c.files)
            except Exception as e:
                c.note = f"ERROR: {e}"
                log(c.note)
    finally:
        driver.quit()
        shutil.rmtree(profile_clone.parent, ignore_errors=True)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "player": args.player,
        "hit_types": sorted(hit_types),
        "games_requested": args.games,
        "output_dir": str(out_dir.relative_to(ROOT)),
        "drive_folder_id": args.drive_folder_id,
        "candidates": [asdict(c) for c in candidates],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    log(f"Wrote manifest: {out_dir / 'manifest.json'}")

    if args.drive_folder_id and not args.dry_run:
        upload_to_drive(out_dir, args.drive_remote, args.drive_folder_id)

    downloaded = sum(1 for c in candidates if c.downloaded)
    log(f"Complete. Downloaded clips for {downloaded}/{len(candidates)} candidate games.")
    log(f"Local folder: {out_dir}")


if __name__ == "__main__":
    main()
