# app.py
from flask import Flask, render_template, request, redirect, url_for, session
from functools import wraps
from pathlib import Path
from datetime import datetime
import json

app = Flask(__name__)
app.secret_key = "change-me-in-production"  # needed for sessions

# ----- Users (fixed pins for now) -----
USERS = {
    "tim": "0724",
    "zak": "1022",
}

# ----- Picks & Results (in-memory for now) -----
# PICKS structure:   {username: {week_num: {game_id: "TeamName"}}}
# RESULTS structure: {week_num: {game_id: "TeamName"}}
PICKS = {"tim": {}, "zak": {}}
RESULTS = {}

# ---------- Load schedule from JSON with logging ----------
BASE_DIR = Path(__file__).parent
SCHEDULE_PATH = BASE_DIR / "data" / "schedule_2025.json"

def load_schedule():
    print(f"[SCHEDULE] Loading: {SCHEDULE_PATH}")
    if not SCHEDULE_PATH.exists():
        print("[SCHEDULE] NOT FOUND")
        return {"year": 2025, "weeks": {}}
    try:
        with open(SCHEDULE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[SCHEDULE] JSON ERROR: {e}")
        return {"year": 2025, "weeks": {}}

    weeks = data.get("weeks", {})
    total_games = sum(len(glist) for glist in weeks.values()) if isinstance(weeks, dict) else 0
    print(f"[SCHEDULE] Loaded weeks: {len(weeks)} | total games: {total_games}")
    return data if isinstance(weeks, dict) else {"year": data.get("year", 2025), "weeks": {}}

SCHEDULE_DATA = load_schedule()
WEEKS_DICT = SCHEDULE_DATA.get("weeks", {})

def get_games_for_week(week_num: int):
    """Return games for the given week, handling keys like '1' or 'Week 1'."""
    weeks = SCHEDULE_DATA.get("weeks", {})
    wk = str(week_num)

    if isinstance(weeks, dict):
        candidates = {wk, f"Week {wk}", f"WEEK {wk}", f"week {wk}"}
        for key in weeks.keys():
            if key in candidates:
                return weeks[key]
        return []
    elif isinstance(weeks, list):
        idx = week_num - 1
        return weeks[idx] if 0 <= idx < len(weeks) else []
    return []

# ----- Formatting helpers -----
def prettify_games(games):
    """
    Add g['kickoff_fmt'] formatted as 'Sun, Sep 07 • 12:00 PM' (12-hour).
    If kickoff_local isn't ISO, fallback to the raw string.
    """
    pretty = []
    for g in games:
        k = g.get("kickoff_local", "")
        nice = ""
        if k:
            try:
                dt = datetime.fromisoformat(k)
                nice = dt.strftime("%a, %b %d • %I:%M %p")
            except Exception:
                nice = k
        ng = dict(g)
        ng["kickoff_fmt"] = nice
        pretty.append(ng)
    return pretty

# ----- Records -----
def compute_records():
    """Compare each user's picks to RESULTS and tally wins/losses."""
    records = {u: {"wins": 0, "losses": 0} for u in USERS.keys()}
    for user, weeks in PICKS.items():
        for week, picks in weeks.items():
            winners = RESULTS.get(week, {})
            for gid, pick in picks.items():
                winner = winners.get(gid)
                if not winner:
                    continue  # no result yet
                if pick == winner:
                    records[user]["wins"] += 1
                else:
                    records[user]["losses"] += 1
    return records

def get_season_records():
    return compute_records()

def compute_week_record_for_user(user: str, week_num: int):
    wins = losses = 0
    picks = PICKS.get(user, {}).get(week_num, {})
    winners = RESULTS.get(week_num, {})
    for gid, winner in winners.items():
        pick = picks.get(gid)
        if not pick:
            continue
        if pick == winner:
            wins += 1
        else:
            losses += 1
    return {"wins": wins, "losses": losses}

# ----- Auth helper -----
def login_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("home"))
        return view_func(*args, **kwargs)
    return wrapper

# ===================== Routes =====================

@app.route("/", methods=["GET"])
def home():
    records = get_season_records()
    return render_template("login.html", records=records)

@app.route("/login", methods=["POST"])
def login():
    username = request.form.get("username")
    pin = request.form.get("pin")
    if username in USERS and USERS[username] == pin:
        session["user"] = username
        return redirect(url_for("dashboard"))
    records = get_season_records()
    return render_template("login.html", records=records, error="Invalid login. Try again.")

@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("home"))

@app.route("/dashboard")
@login_required
def dashboard():
    # Prefer weeks from JSON; if none, show 1..18 fallback
    json_weeks = sorted(int(w) for w in WEEKS_DICT.keys()) if WEEKS_DICT else []
    weeks = json_weeks if json_weeks else list(range(1, 19))

    user = session["user"]

    # Latest week that has any results entered
    week_with_results = max(RESULTS.keys()) if RESULTS else None

    # Live weekly records for BOTH users (if we have any results)
    weekly_records = None
    if week_with_results:
        weekly_records = {
            u: compute_week_record_for_user(u, week_with_results)
            for u in USERS.keys()
        }

    records = get_season_records()
    return render_template(
        "dashboard.html",
        user=user,
        weeks=weeks,
        records=records,
        week_with_results=week_with_results,
        weekly_records=weekly_records,   # both users' weekly records
    )

@app.route("/week/<int:week_num>", methods=["GET", "POST"])
@login_required
def week_view(week_num):
    user = session["user"]
    raw_games = get_games_for_week(week_num)
    games = prettify_games(raw_games)

    if request.method == "POST":
        user_picks = PICKS.setdefault(user, {}).setdefault(week_num, {})
        for g in raw_games:
            pick_key = f"pick_{g['id']}"
            chosen = request.form.get(pick_key)
            if chosen:
                user_picks[g["id"]] = chosen
        return redirect(url_for("week_view", week_num=week_num))

    existing = PICKS.get(user, {}).get(week_num, {})
    print(f"[WEEK] {week_num} games loaded:", len(games))
    return render_template("week.html", user=user, week=week_num, games=games, existing=existing)

# ----- Admin: set winners for a week -----
@app.route("/admin/results/<int:week_num>", methods=["GET", "POST"])
@login_required
def results_week(week_num):
    raw_games = get_games_for_week(week_num)
    games = prettify_games(raw_games)

    if request.method == "POST":
        winners = RESULTS.setdefault(week_num, {})
        for g in raw_games:
            choice = request.form.get(f"win_{g['id']}")
            if choice in (g["away"], g["home"]):
                winners[g["id"]] = choice
        return redirect(url_for("results_week", week_num=week_num))

    winners = RESULTS.get(week_num, {})
    return render_template("results.html", week=week_num, games=games, winners=winners)

# ==================================================

if __name__ == "__main__":
    # Bind to all interfaces so other PCs on your Wi-Fi can reach it
    app.run(host="0.0.0.0", port=5000, debug=True)
