# app.py
from flask import Flask, render_template, request, redirect, url_for, session
from functools import wraps
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
import os
import json
import psycopg

app = Flask(__name__)
app.secret_key = "change-me-in-production"  # needed for sessions

# --- load .env so DATABASE_URL is available locally (Render uses its own env var) ---
BASE_DIR = Path(__file__).parent
load_dotenv(dotenv_path=BASE_DIR / ".env", override=False)
DB_URL = os.getenv("DATABASE_URL")
print("[ENV] DATABASE_URL set:", "yes" if DB_URL else "no")

# ----- Users (fixed pins for now) -----
USERS = {
    "tim": "0724",
    "zak": "1022",
}

# ---------- Paths ----------
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SCHEDULE_PATH = DATA_DIR / "schedule_2025.json"

# ---------- Load schedule from JSON with logging ----------
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

# ===================== Database helpers =====================

def db_connect():
    if not DB_URL:
        raise RuntimeError("DATABASE_URL is not set. Put it in .env or Render env vars.")
    # psycopg 3 opens SSL automatically when ?sslmode=require is in the URL (Neon default)
    return psycopg.connect(DB_URL, connect_timeout=10)

def init_db():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS picks (
                    username TEXT NOT NULL,
                    week     INT  NOT NULL,
                    game_id  TEXT NOT NULL,
                    pick     TEXT NOT NULL,
                    PRIMARY KEY (username, week, game_id)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS results (
                    week     INT  NOT NULL,
                    game_id  TEXT NOT NULL,
                    winner   TEXT NOT NULL,
                    PRIMARY KEY (week, game_id)
                );
            """)
        conn.commit()
    print("[DB] Schema ready.")

def get_user_picks_for_week(username: str, week: int) -> dict:
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT game_id, pick FROM picks WHERE username=%s AND week=%s;",
            (username, week),
        )
        return {gid: pick for gid, pick in cur.fetchall()}

def save_picks(username: str, week: int, picks: dict):
    if not picks:
        return
    with db_connect() as conn, conn.cursor() as cur:
        for gid, choice in picks.items():
            cur.execute(
                """
                INSERT INTO picks (username, week, game_id, pick)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (username, week, game_id)
                DO UPDATE SET pick = EXCLUDED.pick;
                """,
                (username, week, gid, choice),
            )
        conn.commit()

def get_results_for_week(week: int) -> dict:
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT game_id, winner FROM results WHERE week=%s;", (week,))
        return {gid: winner for gid, winner in cur.fetchall()}

def save_results(week: int, winners: dict):
    if not winners:
        return
    with db_connect() as conn, conn.cursor() as cur:
        for gid, winner in winners.items():
            cur.execute(
                """
                INSERT INTO results (week, game_id, winner)
                VALUES (%s, %s, %s)
                ON CONFLICT (week, game_id)
                DO UPDATE SET winner = EXCLUDED.winner;
                """,
                (week, gid, winner),
            )
        conn.commit()

def latest_week_with_results():
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT MAX(week) FROM results;")
        row = cur.fetchone()
        return row[0] if row and row[0] is not None else None

def compute_week_record_for_user(username: str, week: int):
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN r.winner IS NOT NULL AND p.pick = r.winner THEN 1 ELSE 0 END), 0) AS wins,
              COALESCE(SUM(CASE WHEN r.winner IS NOT NULL AND p.pick <> r.winner THEN 1 ELSE 0 END), 0) AS losses
            FROM picks p
            LEFT JOIN results r
              ON p.week = r.week AND p.game_id = r.game_id
            WHERE p.username = %s AND p.week = %s;
            """,
            (username, week),
        )
        wins, losses = cur.fetchone()
        return {"wins": int(wins or 0), "losses": int(losses or 0)}

def compute_season_records():
    # Start everyone at 0–0
    records = {u: {"wins": 0, "losses": 0} for u in USERS.keys()}
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.username,
                   COALESCE(SUM(CASE WHEN r.winner IS NOT NULL AND p.pick = r.winner THEN 1 ELSE 0 END), 0) AS wins,
                   COALESCE(SUM(CASE WHEN r.winner IS NOT NULL AND p.pick <> r.winner THEN 1 ELSE 0 END), 0) AS losses
            FROM picks p
            LEFT JOIN results r
              ON p.week = r.week AND p.game_id = r.game_id
            GROUP BY p.username;
            """
        )
        for username, wins, losses in cur.fetchall():
            records[username] = {"wins": int(wins or 0), "losses": int(losses or 0)}
    return records

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
    records = compute_season_records()
    return render_template("login.html", records=records)

@app.route("/login", methods=["POST"])
def login():
    username = request.form.get("username")
    pin = request.form.get("pin")
    if username in USERS and USERS[username] == pin:
        session["user"] = username
        return redirect(url_for("dashboard"))
    records = compute_season_records()
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

    # Latest week that has any results
    week_with_results = latest_week_with_results()

    # Live weekly records for BOTH users (if we have any results)
    weekly_records = None
    if week_with_results:
        weekly_records = {
            u: compute_week_record_for_user(u, week_with_results)
            for u in USERS.keys()
        }

    records = compute_season_records()
    return render_template(
        "dashboard.html",
        user=user,
        weeks=weeks,
        records=records,
        week_with_results=week_with_results,
        weekly_records=weekly_records,
    )

@app.route("/week/<int:week_num>", methods=["GET", "POST"])
@login_required
def week_view(week_num):
    user = session["user"]
    raw_games = get_games_for_week(week_num)
    games = prettify_games(raw_games)

    if request.method == "POST":
        # gather picks from the form and upsert them
        picks_to_save = {}
        for g in raw_games:
            pick_key = f"pick_{g['id']}"
            chosen = request.form.get(pick_key)
            if chosen:
                picks_to_save[g["id"]] = chosen
        save_picks(user, week_num, picks_to_save)
        return redirect(url_for("week_view", week_num=week_num))

    existing = get_user_picks_for_week(user, week_num)
    print(f"[WEEK] {week_num} games loaded:", len(games))
    return render_template("week.html", user=user, week=week_num, games=games, existing=existing)

# ----- Admin: set winners for a week -----
@app.route("/admin/results/<int:week_num>", methods=["GET", "POST"])
@login_required
def results_week(week_num):
    raw_games = get_games_for_week(week_num)
    games = prettify_games(raw_games)

    if request.method == "POST":
        winners = {}
        for g in raw_games:
            choice = request.form.get(f"win_{g['id']}")
            if choice in (g["away"], g["home"]):
                winners[g["id"]] = choice
        save_results(week_num, winners)
        return redirect(url_for("results_week", week_num=week_num))

    winners = get_results_for_week(week_num)
    return render_template("results.html", week=week_num, games=games, winners=winners)

# ==================================================

# IMPORTANT: ensure DB tables are created even under Gunicorn on Render.
init_db()

if __name__ == "__main__":
    app.run(debug=True)
