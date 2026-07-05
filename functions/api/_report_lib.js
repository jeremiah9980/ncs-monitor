// Pure reshaping helpers for the /api/report Pages Function.
//
// buildReport() takes plain arrays of D1 rows and produces the SAME JSON shape
// that gc_player_stats.py's build_report() writes to reports/gc-player-stats.json,
// so gc-stats-dashboard.html (a copy of gc-player-stats.html) renders it
// unchanged. Kept free of any Workers-runtime dependency so it can be
// unit-tested under plain node.
//
// Files prefixed with "_" are not routed by Cloudflare Pages, so this module is
// import-only.

const SEASON_ORDER = { winter: 0, spring: 1, summer: 2, fall: 3 };
const SEASON_RE = /\b(Winter|Spring|Summer|Fall)\s+(20\d{2})\b/i;

// Column sets mirror gc_player_stats.py (BAT_SUM_COLS / PIT_SUM_COLS and the
// batting-totals columns used by finalize_player).
const BAT_SUM_COLS = ["AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "SB"];
const PIT_SUM_COLS = ["H", "R", "ER", "BB", "SO", "HR"];
const BAT_TOTAL_COLS = ["AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "SO"];

function int0(v) {
  const n = parseInt(v, 10);
  return Number.isNaN(n) ? 0 : n;
}

function round3(v) {
  return Math.round(v * 1000) / 1000;
}

// "Spring 2026" -> [2026, 1]; unknown -> [0, 0]. Higher = more recent.
function seasonSortKey(text) {
  const m = SEASON_RE.exec(text || "");
  if (!m) return [0, 0];
  return [parseInt(m[2], 10), SEASON_ORDER[m[1].toLowerCase()] ?? 0];
}

function cmpKey(a, b) {
  return a[0] - b[0] || a[1] - b[1];
}

// Innings pitched "2.1" -> 2 innings + 1 out -> 7 thirds.
function ipToThirds(v) {
  const parts = String(v ?? "").split(".");
  return int0(parts[0]) * 3 + Math.min(int0(parts[1]), 2);
}

function thirdsToIp(t) {
  return `${Math.floor(t / 3)}.${t % 3}`;
}

function parseJson(str, fallback) {
  try {
    const v = JSON.parse(str);
    return v == null ? fallback : v;
  } catch {
    return fallback;
  }
}

// Sort a player's games newest-first, cap at maxGames, recompute batting totals.
function finalizePlayer(rp, maxGames) {
  rp.games.sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));
  rp.games = rp.games.slice(0, maxGames);
  const bat = rp.games.filter((g) => g.batting).map((g) => g.batting);
  const totals = {};
  for (const c of BAT_TOTAL_COLS) {
    totals[c] = bat.reduce((s, b) => s + int0(b[c]), 0);
  }
  totals.GP = rp.games.length;
  totals.AVG = totals.AB ? round3(totals.H / totals.AB) : 0.0;
  rp.totals = totals;
}

// One combined season stat line per player: their most recent season, summed
// across every team they played for that season. Mirrors
// season_totals_for_report() in gc_player_stats.py.
function seasonTotalsForReport(seasonRows, pids) {
  const pidSet = new Set(pids);
  const latest = {};
  for (const r of seasonRows) {
    if (!pidSet.has(r.player_id)) continue;
    const k = seasonSortKey(r.season || "");
    if (!(r.player_id in latest) || cmpKey(k, latest[r.player_id]) > 0) {
      latest[r.player_id] = k;
    }
  }

  const out = {};
  for (const r of seasonRows) {
    if (!pidSet.has(r.player_id)) continue;
    if (cmpKey(seasonSortKey(r.season || ""), latest[r.player_id]) !== 0) continue;
    if (r.section !== "batting" && r.section !== "pitching") continue;

    let line = out[r.player_id];
    if (!line) {
      line = { label: r.season || "", batting: null, pitching: null };
      out[r.player_id] = line;
    }
    const stats = parseJson(r.stats, {});
    const cur = line[r.section];
    if (cur == null) {
      stats.GP = r.games;
      line[r.section] = stats;
    } else {
      // Same season, another team: combine.
      cur.GP = (cur.GP || 0) + r.games;
      if (r.section === "batting") {
        for (const c of BAT_SUM_COLS) cur[c] = int0(cur[c]) + int0(stats[c]);
        cur.AVG = cur.AB ? round3(cur.H / cur.AB) : 0.0;
      } else {
        for (const c of PIT_SUM_COLS) cur[c] = int0(cur[c]) + int0(stats[c]);
        cur.IP = thirdsToIp(ipToThirds(cur.IP) + ipToThirds(stats.IP));
      }
    }
  }
  return out;
}

/**
 * Build the report JSON from D1 row arrays.
 *
 * @param {Array} playersRows    {player_id, name, roster_team}
 * @param {Array} teamsRows      {gc_url, gc_name, season, ncs_teams(JSON str)}
 * @param {Array} gamesRows      {game_id, gc_url, date, opponent, home_away, source_url}
 * @param {Array} statLineRows   {game_id, player_id, section, stats(JSON str), positions}
 * @param {Array} seasonRows     {player_id, gc_url, season, section, games, stats(JSON str)}
 * @param {Object} [opts]        {maxGames=10, generatedAt=now}
 */
export function buildReport(
  playersRows = [],
  teamsRows = [],
  gamesRows = [],
  statLineRows = [],
  seasonRows = [],
  opts = {}
) {
  const maxGames = opts.maxGames ?? 10;
  const generatedAt = opts.generatedAt ?? new Date().toISOString();

  const teamsByUrl = {};
  for (const t of teamsRows) {
    let ncs = parseJson(t.ncs_teams, []);
    if (!Array.isArray(ncs)) ncs = [];
    teamsByUrl[t.gc_url] = {
      gc_name: t.gc_name || "",
      season: t.season || "",
      ncs_teams: ncs,
    };
  }

  const gamesById = {};
  const gameCountByUrl = {};
  for (const g of gamesRows) {
    gamesById[g.game_id] = g;
    gameCountByUrl[g.gc_url] = (gameCountByUrl[g.gc_url] || 0) + 1;
  }

  const reportPlayers = {};
  for (const p of playersRows) {
    reportPlayers[p.player_id] = {
      name: p.name,
      roster_team: p.roster_team || "",
      // The D1 schema doesn't persist per-player current/last-season team
      // lists, so these are empty here; the renderer treats them as optional.
      current_teams: [],
      last_season_teams: [],
      games: [],
    };
  }

  // Fold matched stat lines into per-player games.
  for (const line of statLineRows) {
    const rp = reportPlayers[line.player_id];
    if (!rp) continue;
    const g = gamesById[line.game_id];
    if (!g) continue;
    const team = teamsByUrl[g.gc_url] || { gc_name: "", ncs_teams: [] };

    let entry = rp.games.find((e) => e.game_id === line.game_id);
    if (!entry) {
      entry = {
        game_id: line.game_id,
        date: g.date,
        team: team.ncs_teams[0] || "",
        gc_team: team.gc_name,
        opponent: g.opponent || "",
        home_away: g.home_away || "",
        source_url: g.source_url || "",
      };
      rp.games.push(entry);
    }
    entry[line.section] = parseJson(line.stats, {});
    if (line.positions) entry.positions = line.positions;
  }

  for (const rp of Object.values(reportPlayers)) finalizePlayer(rp, maxGames);

  const seasons = seasonTotalsForReport(seasonRows, Object.keys(reportPlayers));
  for (const [pid, s] of Object.entries(seasons)) {
    reportPlayers[pid].season = s;
  }

  const teamsScraped = {};
  for (const t of teamsRows) {
    const ncs = teamsByUrl[t.gc_url].ncs_teams;
    teamsScraped[t.gc_url] = {
      ncs_teams: ncs.length ? ncs : [""],
      gc_name: t.gc_name || "",
      games: gameCountByUrl[t.gc_url] || 0,
    };
  }

  return {
    generated_at: generatedAt,
    max_games: maxGames,
    teams_scraped: teamsScraped,
    players: reportPlayers,
  };
}
