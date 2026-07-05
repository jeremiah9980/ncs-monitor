#!/usr/bin/env python3
"""
NCS stats DB layer
==================
SQLite schema + idempotent upsert helpers for the NCS fastpitch stats pipeline
(see collect_stats.py). NCS publishes no per-player stat lines, so this DB is
team-level season stats + game logs + tournament results, plus a player ->
team/season linkage table built from each player's roster history.

Everything here is offline / pure sqlite -- no network. init_db(path) opens the
DB (WAL mode), creates the tables if needed, and returns the connection. Each
upsert_* helper does INSERT ... ON CONFLICT(pk) DO UPDATE, preserving the
original ``first_seen`` and bumping ``last_updated`` to the current UTC time.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def now_iso() -> str:
    """Current time as a UTC ISO-8601 string (used for first_seen/last_updated)."""
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    team_id          TEXT PRIMARY KEY,
    name             TEXT,
    division         TEXT,
    city             TEXT,
    region           TEXT,
    url              TEXT,
    record           TEXT,
    win_pct          REAL,
    last10           TEXT,
    streak           TEXT,
    avg_finish       REAL,
    ranking_points   INTEGER,
    avg_runs_scored  REAL,
    avg_runs_allowed REAL,
    avg_run_diff     REAL,
    runs_scored      INTEGER,
    runs_allowed     INTEGER,
    first_seen       TEXT,
    last_updated     TEXT
);

CREATE TABLE IF NOT EXISTS players (
    player_id    TEXT PRIMARY KEY,
    name         TEXT,
    age          TEXT,
    location     TEXT,
    url          TEXT,
    first_seen   TEXT,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS roster (
    team_id    TEXT,
    player_id  TEXT,
    number     TEXT,
    first_seen TEXT,
    last_seen  TEXT,
    PRIMARY KEY (team_id, player_id)
);

CREATE TABLE IF NOT EXISTS player_team_history (
    player_id TEXT,
    team_name TEXT,
    team_id   TEXT,
    division  TEXT,
    season    TEXT,
    status    TEXT,
    location  TEXT,
    PRIMARY KEY (player_id, team_name, season, division, status)
);

CREATE TABLE IF NOT EXISTS team_class_records (
    team_id        TEXT,
    opponent_class TEXT,
    record         TEXT,
    PRIMARY KEY (team_id, opponent_class)
);

CREATE TABLE IF NOT EXISTS team_tournament_results (
    team_id  TEXT,
    date     TEXT,
    event    TEXT,
    division TEXT,
    place    TEXT,
    points   INTEGER,
    record   TEXT,
    avg_rs   REAL,
    max_rs   REAL,
    avg_ra   REAL,
    avg_rd   REAL,
    PRIMARY KEY (team_id, date, event, division)
);

CREATE TABLE IF NOT EXISTS team_games (
    team_id    TEXT,
    game_uid   TEXT,
    date       TEXT,
    team_score INTEGER,
    opp_score  INTEGER,
    result     TEXT,
    opponent   TEXT,
    opp_state  TEXT,
    opp_class  TEXT,
    opp_record TEXT,
    PRIMARY KEY (team_id, game_uid)
);

CREATE TABLE IF NOT EXISTS rankings (
    season_id     TEXT,
    age_id        TEXT,
    class_id      TEXT,
    state         TEXT,
    team_id       TEXT,
    rank          INTEGER,
    team_label    TEXT,
    record        TEXT,
    in_class      TEXT,
    win_pct       REAL,
    events_played INTEGER,
    events_won    INTEGER,
    points        INTEGER,
    captured_at   TEXT,
    PRIMARY KEY (season_id, age_id, class_id, state, team_id, captured_at)
);
"""


def init_db(path: str) -> sqlite3.Connection:
    """Open (creating if needed) the stats DB at ``path`` and ensure the schema.

    Enables WAL journal mode for concurrent readers, creates every table
    IF NOT EXISTS, and returns the live connection.
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Upsert helpers. Each takes the open connection + the row's fields and does an
# INSERT ... ON CONFLICT DO UPDATE that keeps the original first_seen and bumps
# last_updated. Callers are responsible for conn.commit() (batched by caller).
# ---------------------------------------------------------------------------
def upsert_team(conn: sqlite3.Connection, team: dict) -> None:
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO teams (team_id, name, division, city, region, url, record,
                           win_pct, last10, streak, avg_finish, ranking_points,
                           avg_runs_scored, avg_runs_allowed, avg_run_diff,
                           runs_scored, runs_allowed, first_seen, last_updated)
        VALUES (:team_id, :name, :division, :city, :region, :url, :record,
                :win_pct, :last10, :streak, :avg_finish, :ranking_points,
                :avg_runs_scored, :avg_runs_allowed, :avg_run_diff,
                :runs_scored, :runs_allowed, :ts, :ts)
        ON CONFLICT(team_id) DO UPDATE SET
            name=excluded.name,
            division=excluded.division,
            city=excluded.city,
            region=excluded.region,
            url=excluded.url,
            record=excluded.record,
            win_pct=excluded.win_pct,
            last10=excluded.last10,
            streak=excluded.streak,
            avg_finish=excluded.avg_finish,
            ranking_points=excluded.ranking_points,
            avg_runs_scored=excluded.avg_runs_scored,
            avg_runs_allowed=excluded.avg_runs_allowed,
            avg_run_diff=excluded.avg_run_diff,
            runs_scored=excluded.runs_scored,
            runs_allowed=excluded.runs_allowed,
            last_updated=excluded.last_updated
        """,
        {**{
            "team_id": None, "name": None, "division": None, "city": None,
            "region": None, "url": None, "record": None, "win_pct": None,
            "last10": None, "streak": None, "avg_finish": None,
            "ranking_points": None, "avg_runs_scored": None,
            "avg_runs_allowed": None, "avg_run_diff": None,
            "runs_scored": None, "runs_allowed": None,
        }, **team, "ts": ts},
    )


def upsert_player(conn: sqlite3.Connection, player: dict) -> None:
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO players (player_id, name, age, location, url,
                             first_seen, last_updated)
        VALUES (:player_id, :name, :age, :location, :url, :ts, :ts)
        ON CONFLICT(player_id) DO UPDATE SET
            name=excluded.name,
            age=excluded.age,
            location=excluded.location,
            url=excluded.url,
            last_updated=excluded.last_updated
        """,
        {**{"player_id": None, "name": None, "age": None, "location": None,
            "url": None}, **player, "ts": ts},
    )


def upsert_roster(conn: sqlite3.Connection, team_id: str, player_id: str,
                  number: str = "") -> None:
    """Record that ``player_id`` is on ``team_id``'s roster. first_seen sticks;
    last_seen bumps on every observation."""
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO roster (team_id, player_id, number, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(team_id, player_id) DO UPDATE SET
            number=excluded.number,
            last_seen=excluded.last_seen
        """,
        (team_id, player_id, number, ts, ts),
    )


def upsert_player_team_history(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO player_team_history
            (player_id, team_name, team_id, division, season, status, location)
        VALUES (:player_id, :team_name, :team_id, :division, :season, :status,
                :location)
        ON CONFLICT(player_id, team_name, season, division, status) DO UPDATE SET
            team_id=excluded.team_id,
            location=excluded.location
        """,
        {**{"player_id": None, "team_name": None, "team_id": None,
            "division": None, "season": None, "status": None,
            "location": None}, **row},
    )


def upsert_team_class_record(conn: sqlite3.Connection, team_id: str,
                             opponent_class: str, record: str) -> None:
    conn.execute(
        """
        INSERT INTO team_class_records (team_id, opponent_class, record)
        VALUES (?, ?, ?)
        ON CONFLICT(team_id, opponent_class) DO UPDATE SET
            record=excluded.record
        """,
        (team_id, opponent_class, record),
    )


def upsert_tournament_result(conn: sqlite3.Connection, team_id: str,
                             row: dict) -> None:
    conn.execute(
        """
        INSERT INTO team_tournament_results
            (team_id, date, event, division, place, points, record,
             avg_rs, max_rs, avg_ra, avg_rd)
        VALUES (:team_id, :date, :event, :division, :place, :points, :record,
                :avg_rs, :max_rs, :avg_ra, :avg_rd)
        ON CONFLICT(team_id, date, event, division) DO UPDATE SET
            place=excluded.place,
            points=excluded.points,
            record=excluded.record,
            avg_rs=excluded.avg_rs,
            max_rs=excluded.max_rs,
            avg_ra=excluded.avg_ra,
            avg_rd=excluded.avg_rd
        """,
        {**{"date": None, "event": None, "division": None, "place": None,
            "points": None, "record": None, "avg_rs": None, "max_rs": None,
            "avg_ra": None, "avg_rd": None}, **row, "team_id": team_id},
    )


def upsert_game(conn: sqlite3.Connection, team_id: str, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO team_games
            (team_id, game_uid, date, team_score, opp_score, result,
             opponent, opp_state, opp_class, opp_record)
        VALUES (:team_id, :game_uid, :date, :team_score, :opp_score, :result,
                :opponent, :opp_state, :opp_class, :opp_record)
        ON CONFLICT(team_id, game_uid) DO UPDATE SET
            date=excluded.date,
            team_score=excluded.team_score,
            opp_score=excluded.opp_score,
            result=excluded.result,
            opponent=excluded.opponent,
            opp_state=excluded.opp_state,
            opp_class=excluded.opp_class,
            opp_record=excluded.opp_record
        """,
        {**{"game_uid": None, "date": None, "team_score": None,
            "opp_score": None, "result": None, "opponent": None,
            "opp_state": None, "opp_class": None, "opp_record": None},
         **row, "team_id": team_id},
    )


def upsert_ranking(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO rankings
            (season_id, age_id, class_id, state, team_id, rank, team_label,
             record, in_class, win_pct, events_played, events_won, points,
             captured_at)
        VALUES (:season_id, :age_id, :class_id, :state, :team_id, :rank,
                :team_label, :record, :in_class, :win_pct, :events_played,
                :events_won, :points, :captured_at)
        ON CONFLICT(season_id, age_id, class_id, state, team_id, captured_at)
        DO UPDATE SET
            rank=excluded.rank,
            team_label=excluded.team_label,
            record=excluded.record,
            in_class=excluded.in_class,
            win_pct=excluded.win_pct,
            events_played=excluded.events_played,
            events_won=excluded.events_won,
            points=excluded.points
        """,
        {**{"season_id": None, "age_id": None, "class_id": None, "state": None,
            "team_id": None, "rank": None, "team_label": None, "record": None,
            "in_class": None, "win_pct": None, "events_played": None,
            "events_won": None, "points": None, "captured_at": None}, **row},
    )
