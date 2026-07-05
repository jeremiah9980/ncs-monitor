// GET /api/report -- serves the GameChanger per-player stats report, assembled
// live from the D1 database (binding `DB`) into the exact JSON shape that
// gc_player_stats.py's build_report() produced, so gc-stats-dashboard.html
// renders it unchanged. Behind functions/_middleware.js (fail-closed).
import { buildReport } from "./_report_lib.js";

const NO_STORE = { "cache-control": "no-store" };

export async function onRequestGet({ env }) {
  // No DB bound: behave like an empty report rather than erroring.
  if (!env.DB) {
    return Response.json(
      { generated_at: new Date().toISOString(), players: {}, teams_scraped: {} },
      { headers: NO_STORE }
    );
  }

  try {
    const [players, teams, games, statLines, seasonTotals] = await Promise.all([
      env.DB.prepare("SELECT player_id, name, roster_team FROM players").all(),
      env.DB.prepare("SELECT gc_url, gc_name, season, ncs_teams FROM teams").all(),
      env.DB.prepare(
        "SELECT game_id, gc_url, date, opponent, home_away, source_url FROM games"
      ).all(),
      env.DB.prepare(
        "SELECT game_id, player_id, section, stats, positions FROM stat_lines"
      ).all(),
      env.DB.prepare(
        "SELECT player_id, gc_url, season, section, games, stats FROM season_totals"
      ).all(),
    ]);

    const report = buildReport(
      players.results || [],
      teams.results || [],
      games.results || [],
      statLines.results || [],
      seasonTotals.results || []
    );
    return Response.json(report, { headers: NO_STORE });
  } catch (err) {
    return Response.json(
      { error: "report query failed", detail: String(err && err.message ? err.message : err) },
      { status: 500, headers: NO_STORE }
    );
  }
}
