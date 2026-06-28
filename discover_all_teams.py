#!/usr/bin/env python3
"""
discover_all_teams.py
=====================
Discover ALL NCS fastpitch teams within a geographic radius by searching
multiple event "Who's Coming" pages and the NCS team search functionality.

Unlike event-based discovery, this script:
1. Crawls multiple events to find as many teams as possible
2. Filters by age group (10U, 12U) AND geographic radius from a center point
3. Uses city-to-coordinate mapping for accurate distance calculations
4. Outputs a comprehensive team list for monitoring

Usage:
  python discover_all_teams.py --center-zip 78628 --radius 50 --ages 10U 12U

Deps: pip install requests beautifulsoup4 pyyaml
"""

from __future__ import annotations
import argparse
import json
import math
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency 'beautifulsoup4'. Run: pip install beautifulsoup4")

ROOT = Path(__file__).resolve().parent
BASE = "https://www.playncs.com"
DEFAULT_UA = "ncs-roster-watch/4.0 (comprehensive team discovery)"

TEAM_LINK_RE = re.compile(r"/Teams/Details/(\d+)/([A-Za-z0-9\-]*)", re.I)

# Georgetown, TX coordinates (78628)
DEFAULT_CENTER = (30.6327, -97.6781)

# City coordinates in Central Texas (approximate centers)
# These cover the 50-mile radius from Georgetown
CITY_COORDS = {
    # Core area (0-15 miles)
    "georgetown": (30.6327, -97.6781),
    "round rock": (30.5083, -97.6789),
    "cedar park": (30.5052, -97.8203),
    "leander": (30.5788, -97.8531),
    "hutto": (30.5427, -97.5467),
    "liberty hill": (30.6649, -97.9219),
    "jarrell": (30.8243, -97.6042),
    "florence": (30.8413, -97.7936),
    "bertram": (30.7438, -98.0556),

    # Austin metro (15-30 miles)
    "austin": (30.2672, -97.7431),
    "pflugerville": (30.4394, -97.6200),
    "taylor": (30.5708, -97.4092),
    "manor": (30.3416, -97.5567),
    "lago vista": (30.4600, -97.9886),
    "jonestown": (30.4966, -97.9231),
    "granger": (30.7177, -97.4425),
    "thrall": (30.5794, -97.2969),
    "elgin": (30.3499, -97.3703),
    "salado": (30.9474, -97.5386),
    "troy": (31.2068, -97.3017),

    # Extended area (30-50 miles)
    "kyle": (29.9894, -97.8772),
    "buda": (30.0852, -97.8408),
    "bastrop": (30.1105, -97.3153),
    "san marcos": (29.8833, -97.9414),
    "lockhart": (29.8849, -97.6700),
    "dripping springs": (30.1902, -98.0867),
    "wimberley": (29.9975, -98.0986),
    "lakeway": (30.3638, -97.9794),
    "bee cave": (30.3083, -97.9467),
    "spicewood": (30.4691, -98.1547),
    "marble falls": (30.5783, -98.2728),
    "burnet": (30.7583, -98.2281),
    "horseshoe bay": (30.5466, -98.3633),
    "kingsland": (30.6591, -98.4403),
    "lampasas": (31.0638, -98.1817),
    "copperas cove": (31.1241, -97.9031),
    "killeen": (31.1171, -97.7278),
    "harker heights": (31.0834, -97.6597),
    "temple": (31.0982, -97.3428),
    "belton": (31.0560, -97.4642),
    "rogers": (30.9316, -97.2267),
    "moody": (31.3082, -97.3617),
    "mcgregor": (31.4441, -97.4092),
    "waco": (31.5493, -97.1467),
    "lorena": (31.3868, -97.2153),
    "crawford": (31.5441, -97.4458),
    "thorndale": (30.6133, -97.2050),
    "rockdale": (30.6555, -97.0017),
    "cameron": (30.8533, -96.9769),
    "blanco": (30.0999, -98.4217),
    "johnson city": (30.2769, -98.4117),
}

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch_html(url: str, ua: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in miles between two lat/lon points."""
    R = 3959  # Earth radius in miles
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))
    return R * c


def get_city_coords(city_name: str) -> tuple[float, float] | None:
    """Look up coordinates for a city name."""
    if not city_name:
        return None
    # Normalize city name
    city_lower = city_name.lower().strip()
    # Remove common suffixes
    for suffix in [", tx", " tx", ", texas", " texas"]:
        if city_lower.endswith(suffix):
            city_lower = city_lower[:-len(suffix)]
    # Direct lookup
    if city_lower in CITY_COORDS:
        return CITY_COORDS[city_lower]
    # Partial match
    for known_city, coords in CITY_COORDS.items():
        if known_city in city_lower or city_lower in known_city:
            return coords
    return None


def is_within_radius(city: str, center: tuple[float, float], radius_miles: float) -> bool:
    """Check if a city is within the specified radius of the center point."""
    coords = get_city_coords(city)
    if not coords:
        # If we can't find coordinates, check if city name matches any known city
        city_lower = city.lower()
        for known_city in CITY_COORDS.keys():
            if known_city in city_lower or city_lower in known_city:
                return True
        return False
    distance = haversine_distance(center[0], center[1], coords[0], coords[1])
    return distance <= radius_miles


def extract_city_from_text(text: str) -> str:
    """Extract city name from NCS team location text."""
    if not text:
        return ""
    # NCS often formats as "TeamName City, ST" or just "City, ST"
    # Try to extract just the city part
    parts = text.strip().split(",")
    if len(parts) >= 2:
        # Last part before state is usually the city
        city_part = parts[-2].strip() if len(parts) > 1 else parts[0].strip()
        # Remove team name prefix if present (look for last word before comma)
        words = city_part.split()
        if words:
            # The city is usually the last 1-2 words
            return " ".join(words[-2:]) if len(words) > 1 else words[-1]
    return text.strip()


def whos_coming_url(event: str | int) -> str:
    s = str(event)
    if s.startswith("http"):
        return s
    return f"{BASE}/fastpitch!/Events/WhosComing/{s}/event"


def parse_whos_coming(html: str) -> list[dict]:
    """Parse an event "Who's Coming" page for team registrations."""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=TEAM_LINK_RE):
        m = TEAM_LINK_RE.search(a["href"])
        if not m:
            continue
        tid, slug = m.group(1), m.group(2)
        if tid in seen:
            continue
        row = a.find_parent("tr")
        if not row:
            continue
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        # Find division (e.g., "10U C", "12U OPEN")
        division = next((c for c in cells if re.match(r"^\s*\d+U", c)), "")
        # Find location (e.g., "City, TX")
        loc = next((c for c in cells if re.search(r",\s*[A-Za-z]{2}\.?$", c)), "")
        city, region = "", ""
        if "," in loc:
            city, region = (x.strip() for x in loc.rsplit(",", 1))
        seen.add(tid)
        out.append({
            "team_id": tid,
            "name": a.get_text(strip=True),
            "division": division,
            "city": city,
            "region": region,
            "url": f"{BASE}/Fastpitch/Teams/Details/{tid}/{slug or 'team'}"
        })
    return out


def discover_from_events(
    events: list[str | int],
    ages: list[str],
    center: tuple[float, float],
    radius: float,
    ua: str,
    delay: float
) -> dict[str, dict]:
    """Discover teams from multiple event Who's Coming pages."""
    found = {}
    for i, event in enumerate(events):
        url = whos_coming_url(event)
        try:
            log(f"Fetching event: {url}")
            html = fetch_html(url, ua)
            teams = parse_whos_coming(html)
            log(f"  Found {len(teams)} teams registered")

            for team in teams:
                # Check age group
                div = team["division"].upper().replace(" ", "")
                age_match = any(div.startswith(age.upper().replace(" ", "")) for age in ages)
                if not age_match:
                    continue

                # Check geographic radius
                city = extract_city_from_text(team["city"])
                if not is_within_radius(city, center, radius):
                    log(f"    Skipping {team['name']} - {city} outside {radius}mi radius")
                    continue

                # Add to found teams (dedupe by team_id)
                if team["team_id"] not in found:
                    found[team["team_id"]] = team
                    log(f"    Added: {team['name']} ({team['division']}) - {city}")

        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            log(f"  Error fetching event {event}: {e}")

        if i < len(events) - 1:
            time.sleep(delay)

    return found


def search_ncs_teams(
    query: str,
    ages: list[str],
    center: tuple[float, float],
    radius: float,
    ua: str
) -> dict[str, dict]:
    """Search NCS team directory (if available)."""
    # NCS may have a team search page we can use
    # For now, this is a placeholder for future enhancement
    return {}


def load_existing_teams(path: Path) -> dict[str, dict]:
    """Load existing discovered teams."""
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return {t["team_id"]: t for t in data.get("teams", [])}
        except json.JSONDecodeError:
            pass
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Discover ALL NCS teams within a radius")
    ap.add_argument("--center-zip", default="78628", help="Center ZIP code (default: Georgetown)")
    ap.add_argument("--center-lat", type=float, help="Center latitude (overrides ZIP)")
    ap.add_argument("--center-lon", type=float, help="Center longitude (overrides ZIP)")
    ap.add_argument("--radius", type=float, default=50, help="Radius in miles (default: 50)")
    ap.add_argument("--ages", nargs="+", default=["10U", "12U"], help="Age groups to track")
    ap.add_argument("--events", nargs="+", type=str, help="Event IDs to crawl")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"), help="Config file path")
    ap.add_argument("--output", default=str(ROOT / "discovered_teams.json"), help="Output file")
    ap.add_argument("--delay", type=float, default=2, help="Delay between requests (seconds)")
    ap.add_argument("--merge", action="store_true", help="Merge with existing teams")
    args = ap.parse_args()

    # Determine center coordinates
    if args.center_lat and args.center_lon:
        center = (args.center_lat, args.center_lon)
    else:
        # Default to Georgetown, TX (78628)
        center = DEFAULT_CENTER

    log(f"Center: {center[0]:.4f}, {center[1]:.4f}")
    log(f"Radius: {args.radius} miles")
    log(f"Age groups: {args.ages}")

    # Get events from config or args
    events = args.events or []
    if not events and yaml:
        config_path = Path(args.config)
        if config_path.exists():
            with config_path.open() as f:
                cfg = yaml.safe_load(f) or {}
            disc = cfg.get("discovery", {})
            events = disc.get("events", [])

    if not events:
        log("No events specified. Add events to config.yaml or use --events")
        return 1

    log(f"Events to crawl: {events}")

    # Load existing teams if merging
    output_path = Path(args.output)
    existing = load_existing_teams(output_path) if args.merge else {}
    log(f"Existing teams: {len(existing)}")

    # Discover teams from events
    found = discover_from_events(
        events=events,
        ages=args.ages,
        center=center,
        radius=args.radius,
        ua=DEFAULT_UA,
        delay=args.delay
    )

    # Merge with existing
    all_teams = {**existing, **found}
    log(f"Total teams discovered: {len(all_teams)}")

    # Save output
    output_data = {
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "center": {"lat": center[0], "lon": center[1], "zip": args.center_zip},
        "radius_miles": args.radius,
        "age_groups": args.ages,
        "teams": list(all_teams.values())
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output_data, indent=2))
    log(f"Saved {len(all_teams)} teams to {output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
