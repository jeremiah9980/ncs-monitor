-- GameChanger per-player stats schema for the D1 database `gc_stats`.
-- Mirrors gc_player_stats.py's DB_SCHEMA exactly (D1 is SQLite). Apply with:
--   wrangler d1 execute gc_stats --remote --file=migrations/0001_gc_stats.sql
-- (use --local for validation). No PRAGMAs; every statement is idempotent.

CREATE TABLE IF NOT EXISTS players (
    player_id   TEXT PRIMARY KEY,           -- stable NCS player id
    name        TEXT NOT NULL,
    roster_team TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS teams (
    gc_url    TEXT PRIMARY KEY,
    gc_name   TEXT DEFAULT '',
    season    TEXT DEFAULT '',
    ncs_teams TEXT DEFAULT ''               -- JSON list of NCS team names
);
CREATE TABLE IF NOT EXISTS games (
    game_id    TEXT PRIMARY KEY,
    gc_url     TEXT NOT NULL,
    date       TEXT NOT NULL,
    opponent   TEXT DEFAULT '',
    home_away  TEXT DEFAULT '',
    source_url TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS stat_lines (
    game_id   TEXT NOT NULL,
    player_id TEXT NOT NULL,
    section   TEXT NOT NULL,                -- batting | pitching
    stats     TEXT NOT NULL,                -- JSON of the raw box-score line
    positions TEXT DEFAULT '',
    PRIMARY KEY (game_id, player_id, section)
);
CREATE TABLE IF NOT EXISTS season_totals (
    player_id  TEXT NOT NULL,
    gc_url     TEXT NOT NULL,
    season     TEXT DEFAULT '',
    section    TEXT NOT NULL,
    games      INTEGER NOT NULL,
    stats      TEXT NOT NULL,               -- JSON of summed stats (+AVG, IP)
    updated_at TEXT NOT NULL,
    PRIMARY KEY (player_id, gc_url, section)
);
CREATE INDEX IF NOT EXISTS idx_lines_player ON stat_lines (player_id);
CREATE INDEX IF NOT EXISTS idx_games_team ON games (gc_url);
