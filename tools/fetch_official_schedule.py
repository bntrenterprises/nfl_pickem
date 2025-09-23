# tools/fetch_official_schedule.py
# Source: NFL Operations "2025 NFL Schedule" page (official).
# This script fetches the page, parses Weeks 1–18, converts ET times to America/Chicago,
# and writes data/schedule_2025.json in the exact structure your app expects.

import json
import re
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

URL = "https://operations.nfl.com/gameday/nfl-schedule/2025-nfl-schedule/"
OUT = Path(__file__).resolve().parent.parent / "data" / "schedule_2025.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

MONTHS = {
    "Jan": 1, "January": 1,
    "Feb": 2, "February": 2,
    "Mar": 3, "March": 3,
    "Apr": 4, "April": 4,
    "May": 5,
    "Jun": 6, "June": 6,
    "Jul": 7, "July": 7,
    "Aug": 8, "August": 8,
    "Sep": 9, "Sept": 9, "September": 9,
    "Oct": 10, "October": 10,
    "Nov": 11, "November": 11,
    "Dec": 12, "December": 12,
}

def normalize_date_line(line: str):
    """
    Examples from the page:
      'Thursday, Sept. 4, 2025'
      'Sunday, Sept. 07, 2025'
      'Monday, Dec. 1, 2025'
    Return (year, month, day)
    """
    s = line.replace(",", "").replace(".", "").strip()
    # -> 'Thursday Sept 4 2025'
    parts = s.split()
    if len(parts) < 4:
        return None
    # parts[1] is month (e.g., 'Sept'), parts[2] is day, parts[3] is year
    month_txt = parts[1]
    day_txt = parts[2]
    year_txt = parts[3]
    month = MONTHS.get(month_txt, None)
    if not month:
        return None
    day = int(day_txt)
    year = int(year_txt)
    return year, month, day

def parse_time_et_to_ct(date_ymd, et_text: str):
    """
    Page shows two times under each matchup:
      - a line like '1:00p (ET)' (or with other zones for 'local')
      - a next line like '1:00p'  <-- this is ET (per page layout)
    We pass the ET-only line (e.g., '8:20p', '9:30a') here.
    Convert ET -> America/Chicago, return ISO 'YYYY-MM-DDTHH:MM:SS'
    """
    hm = et_text.strip().lower()
    m = re.match(r"^(\d{1,2}):(\d{2})([ap])$", hm)
    if not m:
        # Sometimes ESPN-style '8:15p' vs '8:15 pm'; we'll try a forgiving parse:
        hm = re.sub(r"\s+", "", hm.replace("pm", "p").replace("am", "a"))
        m = re.match(r"^(\d{1,2}):(\d{2})([ap])$", hm)
        if not m:
            return ""  # leave empty if we can't parse
    hour = int(m.group(1)) % 12
    minute = int(m.group(2))
    if m.group(3) == "p":
        hour += 12
    y, mo, d = date_ymd
    dt_et = datetime(y, mo, d, hour, minute, tzinfo=ZoneInfo("America/New_York"))
    dt_ct = dt_et.astimezone(ZoneInfo("America/Chicago"))
    return dt_ct.strftime("%Y-%m-%dT%H:%M:00")

def fetch_lines():
    r = requests.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    # Pull all text in order to keep the page sequence (week -> date -> games -> times)
    text = soup.get_text("\n")
    # Clean & split
    lines = [re.sub(r"\s+", " ", ln.strip()) for ln in text.splitlines()]
    # Filter out empties
    return [ln for ln in lines if ln]

def build_schedule():
    lines = fetch_lines()

    week_re = re.compile(r"^WEEK\s+(\d+)$", re.I)
    date_re = re.compile(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+[A-Za-z]+\.?\s+\d{1,2},\s+2025$", re.I)
    # Matchups like:
    #  'Dallas Cowboys at Philadelphia Eagles'
    #  'Kansas City Chiefs vs Los Angeles Chargers (Sao Paulo)'
    game_re = re.compile(r"^(.+?)\s+(at|vs)\s+(.+?)(?:\s+\(([^)]+)\))?$", re.I)
    # Time lines:
    #   '8:20p (ET)'  (we ignore this; we want the next bare ET time)
    time_with_zone_re = re.compile(r"^\d{1,2}:\d{2}[ap]\s+\([A-Z]{2,4}\)$")
    #   '8:20p' (ET) — bare time line (per page layout)
    time_bare_re = re.compile(r"^\d{1,2}:\d{2}[ap]$")

    weeks = {}
    current_week = None
    current_date = None
    pending_game = None
    game_counter = {}

    for ln in lines:
        # Detect week
        m = week_re.match(ln)
        if m:
            current_week = int(m.group(1))
            weeks[str(current_week)] = []
            game_counter[current_week] = 0
            current_date = None
            pending_game = None
            continue

        # Detect date
        if date_re.match(ln):
            nd = normalize_date_line(ln)
            if nd:
                current_date = nd
            continue

        # Detect game row
        gm = game_re.match(ln)
        if gm and current_week:
            away = gm.group(1).strip()
            sep = gm.group(2).lower()
            home = gm.group(3).strip()
            note = gm.group(4).strip() if gm.group(4) else ""
            # Neutral site noted by 'vs'
            pending_game = {
                "away": away,
                "home": home,
                "note": note,
                "neutral": (sep == "vs")
            }
            continue

        # Detect times
        if time_with_zone_re.match(ln):
            # This is the "LOCAL (ZONE)" line; ignore for conversion
            continue

        if time_bare_re.match(ln):
            # This is the ET time; convert to Central using current_date
            if pending_game and current_week and current_date:
                iso_ct = parse_time_et_to_ct(current_date, ln)
                game_counter[current_week] += 1
                gid = f"W{current_week}G{game_counter[current_week]}"
                weeks[str(current_week)].append({
                    "id": gid,
                    "away": pending_game["away"],
                    "home": pending_game["home"],
                    "kickoff_local": iso_ct,   # stored as America/Chicago local in ISO
                    "note": pending_game["note"],
                })
                pending_game = None  # clear for next game

    return {"year": 2025, "weeks": weeks}

def main():
    data = build_schedule()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved: {OUT}")
    # Quick sanity:
    total = sum(len(v) for v in data["weeks"].values())
    print(f"Weeks: {len(data['weeks'])} | Games: {total}")

if __name__ == "__main__":
    main()
