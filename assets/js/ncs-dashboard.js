'use strict';

const SNAPSHOT_URL = 'snapshots/latest.json';
const DISCOVERED_URL = 'discovered_teams.json';
const CHANGELOG_URL = 'reports/changelog.csv';
const THEME_KEY = 'ncs-dashboard-theme';
const THEMES = ['light', 'mid', 'dark'];

const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[char]));

let snapshotData = null;
let discoveredData = null;
let changelogData = [];

function getAgeGroup(division) {
  const match = String(division ?? '').match(/(\d+U)/i);
  return match ? match[1].toUpperCase() : '';
}

function setTheme(theme) {
  const nextTheme = THEMES.includes(theme) ? theme : 'mid';
  document.documentElement.dataset.theme = nextTheme;
  localStorage.setItem(THEME_KEY, nextTheme);
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', getComputedStyle(document.documentElement).getPropertyValue('--theme-color').trim());
  // Update desktop theme switcher
  document.querySelectorAll('.theme-option').forEach(button => {
    const active = button.dataset.themeValue === nextTheme;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  // Update mobile theme pills
  document.querySelectorAll('.theme-pill').forEach(button => {
    const active = button.dataset.themeValue === nextTheme;
    button.classList.toggle('active', active);
  });
}

function initializeTheme() {
  setTheme(document.documentElement.dataset.theme || localStorage.getItem(THEME_KEY) || 'mid');
  document.querySelectorAll('.theme-option').forEach(button => {
    button.addEventListener('click', () => setTheme(button.dataset.themeValue));
  });
  // Mobile theme pills
  document.querySelectorAll('.theme-pill').forEach(button => {
    button.addEventListener('click', () => setTheme(button.dataset.themeValue));
  });
}

function initializeMobileNav() {
  const mobileNavItems = document.querySelectorAll('.mobile-nav-item');
  const sections = ['overview', 'rosters', 'changes', 'settings'];

  // Handle mobile nav clicks
  mobileNavItems.forEach(item => {
    item.addEventListener('click', (e) => {
      mobileNavItems.forEach(i => i.classList.remove('active'));
      item.classList.add('active');
    });
  });

  // Update active nav on scroll
  const observerOptions = { rootMargin: '-50% 0px -50% 0px' };
  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        const sectionId = entry.target.id;
        mobileNavItems.forEach(item => {
          item.classList.toggle('active', item.dataset.section === sectionId);
        });
      }
    });
  }, observerOptions);

  sections.forEach(id => {
    const section = document.getElementById(id);
    if (section) observer.observe(section);
  });

  // Mobile refresh button
  const mobileRefresh = document.getElementById('refresh-data-mobile');
  if (mobileRefresh) {
    mobileRefresh.addEventListener('click', loadAllData);
  }
}

async function loadAllData() {
  const spinner = $('refresh-spin');
  spinner.hidden = false;
  $('team-grid').innerHTML = '<div class="empty"><span class="spin"></span> Loading team data…</div>';

  try {
    const [snapshot, discovered] = await Promise.all([
      fetch(SNAPSHOT_URL, { cache: 'no-store' }).then(response => {
        if (!response.ok) throw new Error(`Snapshot not found (${response.status})`);
        return response.json();
      }),
      fetch(DISCOVERED_URL, { cache: 'no-store' }).then(response => response.ok ? response.json() : null).catch(() => null)
    ]);

    snapshotData = snapshot;
    discoveredData = discovered;

    if (!snapshotData?.teams || Object.keys(snapshotData.teams).length === 0) {
      throw new Error('No team data was found in snapshots/latest.json');
    }

    try {
      const response = await fetch(CHANGELOG_URL, { cache: 'no-store' });
      changelogData = response.ok ? parseCsv(await response.text()) : [];
    } catch {
      changelogData = [];
    }

    updateStats();
    populateCityFilter();
    populateTeamSelect();
    renderOverview();
    renderChanges();
    renderTeamsTable();
  } catch (error) {
    console.error('Failed to load dashboard data:', error);
    $('team-grid').innerHTML = `<div class="empty error">Failed to load team data: ${esc(error.message)}</div>`;
  } finally {
    spinner.hidden = true;
  }
}

function getTeams() {
  if (!snapshotData?.teams) return [];
  return Object.entries(snapshotData.teams).map(([key, team]) => ({
    key,
    ...team,
    ageGroup: getAgeGroup(team.division)
  }));
}

function updateStats() {
  const teams = getTeams();
  const countAge = age => teams.filter(team => team.ageGroup === age).length;
  const players = teams.reduce((total, team) => total + (team.players?.length || 0), 0);

  $('stat-teams').textContent = teams.length;
  $('stat-10u').textContent = countAge('10U');
  $('stat-12u').textContent = countAge('12U');
  $('stat-14u').textContent = countAge('14U');
  $('stat-players').textContent = players;

  if (snapshotData.saved_at) {
    const date = new Date(snapshotData.saved_at);
    $('stat-updated').textContent = Number.isNaN(date.getTime())
      ? 'Current'
      : date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  } else {
    $('stat-updated').textContent = 'Current';
  }
}

function populateCityFilter() {
  const cities = [...new Set(getTeams().map(team => team.city).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  $('overview-city').innerHTML = '<option value="all">All Cities</option>' + cities.map(city => `<option value="${esc(city)}">${esc(city)}</option>`).join('');
}

function renderOverview() {
  const age = $('overview-age').value;
  const city = $('overview-city').value;
  let teams = getTeams().sort((a, b) => String(a.team_name).localeCompare(String(b.team_name)));

  if (age !== 'all') teams = teams.filter(team => team.ageGroup === age);
  if (city !== 'all') teams = teams.filter(team => team.city === city);

  if (!teams.length) {
    $('team-grid').innerHTML = '<div class="empty">No teams match the selected filters.</div>';
    return;
  }

  $('team-grid').innerHTML = teams.map(team => `
    <article class="team-card is-${esc(team.ageGroup.toLowerCase())}" tabindex="0" role="button" data-key="${esc(team.key)}" aria-label="View ${esc(team.team_name)} roster">
      <h3>${esc(team.team_name)}</h3>
      <div class="meta">${esc([team.city, team.region].filter(Boolean).join(', '))} &bull; ${esc(team.division)}</div>
      <span class="roster-size">${team.players?.length || 0}</span>
      <span class="roster-label">Players</span>
    </article>
  `).join('');
}

function openTeamRoster(key) {
  $('team-select').value = key;
  renderRoster();
  $('rosters').scrollIntoView({ behavior: 'smooth' });
}

function populateTeamSelect() {
  const age = $('roster-age').value;
  let teams = getTeams().sort((a, b) => String(a.team_name).localeCompare(String(b.team_name)));
  if (age !== 'all') teams = teams.filter(team => team.ageGroup === age);

  $('team-select').innerHTML = '<option value="">— Select a Team —</option>' + teams.map(team => `
    <option value="${esc(team.key)}">${esc(team.team_name)} — ${esc([team.city, team.region].filter(Boolean).join(', '))} (${esc(team.ageGroup)})</option>
  `).join('');
}

function renderRoster() {
  const key = $('team-select').value;
  const team = snapshotData?.teams?.[key];
  const info = $('team-info');
  const out = $('roster-out');
  const count = $('roster-count');

  if (!team) {
    info.hidden = true;
    out.innerHTML = '';
    count.textContent = '';
    return;
  }

  const location = [team.city, team.region].filter(Boolean).join(', ');
  info.hidden = false;
  info.innerHTML = `
    <span class="team-badge">${esc(team.team_name)}</span>
    <span class="team-badge loc">${esc(location)}</span>
    <span class="team-badge div">${esc(team.division)}</span>
    ${team.url ? `<a href="${esc(team.url)}" target="_blank" rel="noopener noreferrer" class="team-badge link">View on NCS &rarr;</a>` : ''}
  `;

  const players = [...(team.players || [])].sort((a, b) => (parseInt(a.num, 10) || 999) - (parseInt(b.num, 10) || 999));
  if (!players.length) {
    out.innerHTML = '<div class="empty">No players are currently listed on this roster.</div>';
    count.textContent = '';
    return;
  }

  const details = snapshotData.player_details || {};
  out.innerHTML = `<table class="roster-table">
    <thead><tr><th style="text-align:center">#</th><th>Player</th><th>Age</th><th>History</th></tr></thead>
    <tbody>${players.map(player => {
      const playerDetails = details[player.player_id] || {};
      const history = playerDetails.team_history || [];
      const playerUrl = player.url || `https://www.playncs.com/fastpitch/Players/Details/${encodeURIComponent(player.player_id || '')}/`;
      return `<tr>
        <td class="num">${esc(player.num || '—')}</td>
        <td class="player">
          <a href="${esc(playerUrl)}" target="_blank" rel="noopener noreferrer">${esc(player.name)}</a>
          <div class="player-history" id="history-${esc(player.player_id)}">
            ${history.length ? `
              <h4>Team History (${history.length})</h4>
              <ul>${history.map(item => `
                <li><span class="team-name">${esc(item.team)}</span><span class="status ${esc(String(item.status || '').toLowerCase())}">${esc(item.status || 'Unknown')}</span><br><span class="history-meta">${esc(item.division)} &bull; ${esc(item.season)}</span></li>
              `).join('')}</ul>
            ` : '<p>No team history is available.</p>'}
          </div>
        </td>
        <td class="age">${esc(playerDetails.age || '—')}</td>
        <td><button type="button" class="btn-history" data-player-id="${esc(player.player_id)}">${history.length ? `${history.length} teams` : 'View'}</button></td>
      </tr>`;
    }).join('')}</tbody>
  </table>`;

  count.textContent = `${players.length} player${players.length === 1 ? '' : 's'} on roster`;
}

function parseCsv(text) {
  const lines = String(text || '').trim().split(/\r?\n/).filter(Boolean);
  if (lines.length < 2) return [];
  const headers = parseCsvLine(lines[0]).map(value => value.trim().toLowerCase());
  const index = name => headers.indexOf(name);

  return lines.slice(1).map(line => {
    const values = parseCsvLine(line);
    return {
      ts: values[index('timestamp')] || '',
      type: values[index('type')] || '',
      team: values[index('team')] || '',
      city: values[index('city')] || '',
      region: values[index('region')] || '',
      player: values[index('player')] || '',
      number: values[index('number')] || '',
      division: values[index('division')] || ''
    };
  });
}

function parseCsvLine(line) {
  const values = [];
  let value = '';
  let quoted = false;
  for (let i = 0; i < line.length; i += 1) {
    const char = line[i];
    if (char === '"' && quoted && line[i + 1] === '"') {
      value += '"';
      i += 1;
    } else if (char === '"') {
      quoted = !quoted;
    } else if (char === ',' && !quoted) {
      values.push(value);
      value = '';
    } else {
      value += char;
    }
  }
  values.push(value);
  return values;
}

function formatDate(timestamp) {
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? (timestamp || '—') : date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: '2-digit' });
}

function renderChanges() {
  const pasted = $('csv-input').value.trim();
  const allChanges = pasted ? [...changelogData, ...parseCsv(pasted)] : [...changelogData];
  const age = $('changes-age').value;
  const days = Number.parseInt($('changes-window').value, 10);
  const cutoff = days > 0 ? Date.now() - days * 86400000 : null;
  const activeTab = document.querySelector('.tab.active')?.dataset.tab || 'all-changes';

  const filtered = allChanges.filter(change => {
    if (cutoff !== null) {
      const timestamp = new Date(change.ts).getTime();
      if (!Number.isNaN(timestamp) && timestamp < cutoff) return false;
    }
    if (age !== 'all') {
      const rowAge = getAgeGroup(change.division) || getAgeGroup(change.team);
      if (rowAge && rowAge !== age) return false;
    }
    if (activeTab === 'new-teams' && change.type !== 'new_team') return false;
    if (activeTab === 'removals' && change.type !== 'removed') return false;
    if (activeTab === 'additions' && change.type !== 'added') return false;
    return true;
  }).sort((a, b) => new Date(b.ts) - new Date(a.ts));

  if (!allChanges.length) {
    $('changes-meta').textContent = '';
    $('changes-out').innerHTML = '<div class="empty">No changes have been tracked yet.</div>';
    return;
  }

  const removed = filtered.filter(row => row.type === 'removed').length;
  const added = filtered.filter(row => row.type === 'added').length;
  const newTeams = filtered.filter(row => row.type === 'new_team').length;
  const label = days === 0 ? 'All time' : days === 14 ? 'Last 2 weeks' : `Last ${days} days`;
  $('changes-meta').textContent = `${label} · ${removed} removed · ${added} added · ${newTeams} new teams · ${filtered.length} total`;

  if (!filtered.length) {
    $('changes-out').innerHTML = '<div class="empty">No changes match the selected filters.</div>';
    return;
  }

  // Group changes by timestamp (rounded to same run) then by team
  const runs = new Map();
  filtered.forEach(row => {
    // Round timestamp to nearest 30 min to group same run
    const ts = new Date(row.ts);
    const roundedTs = new Date(Math.floor(ts.getTime() / (30 * 60 * 1000)) * (30 * 60 * 1000));
    const runKey = roundedTs.toISOString();

    if (!runs.has(runKey)) {
      runs.set(runKey, { ts: roundedTs, teams: new Map() });
    }

    const teamKey = row.team || 'Unknown Team';
    const run = runs.get(runKey);
    if (!run.teams.has(teamKey)) {
      run.teams.set(teamKey, {
        team: row.team,
        city: row.city,
        region: row.region,
        changes: []
      });
    }
    run.teams.get(teamKey).changes.push(row);
  });

  // Render Slack-style cards
  let html = '';
  runs.forEach((run, runKey) => {
    const runDate = run.ts;
    const dateStr = runDate.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    const timeStr = runDate.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });

    let runRemoved = 0, runAdded = 0;
    run.teams.forEach(team => {
      team.changes.forEach(c => {
        if (c.type === 'removed') runRemoved++;
        if (c.type === 'added') runAdded++;
      });
    });

    html += `<div class="change-card">
      <div class="change-card-header">
        <div class="change-card-time">${esc(timeStr)} · ${esc(dateStr)}</div>
        <div class="change-card-summary">
          <span class="pill ${runRemoved > 0 ? 'rem' : 'muted'}">${runRemoved} removed</span>
          <span class="pill ${runAdded > 0 ? 'add' : 'muted'}">${runAdded} added</span>
        </div>
      </div>
      <div class="change-card-body">`;

    run.teams.forEach(team => {
      const location = [team.city, team.region].filter(Boolean).join(', ');
      html += `<div class="change-team-block">
        <div class="change-team-name">${esc(team.team)} <span class="change-team-loc">— ${esc(location)}</span></div>
        <ul class="change-list">`;

      team.changes.forEach(change => {
        if (change.type === 'new_team') {
          html += `<li class="change-item new-team">
            <span class="pill new-team">New Team</span> Now tracked (no prior baseline)
          </li>`;
        } else {
          const action = change.type === 'removed' ? 'removed' : 'added';
          const pillClass = change.type === 'removed' ? 'rem' : 'add';
          html += `<li class="change-item ${action}">
            <span class="pill ${pillClass}">${action}</span> ${esc(change.player || 'Unknown')}${change.number ? ` (#${esc(change.number)})` : ''}
          </li>`;
        }
      });

      html += `</ul></div>`;
    });

    html += `</div></div>`;
  });

  $('changes-out').innerHTML = html;
}

function renderTeamsTable() {
  const search = $('team-search').value.trim().toLowerCase();
  let teams = getTeams().sort((a, b) => a.ageGroup.localeCompare(b.ageGroup) || String(a.team_name).localeCompare(String(b.team_name)));

  if (search) {
    teams = teams.filter(team => String(team.team_name || '').toLowerCase().includes(search) || String(team.city || '').toLowerCase().includes(search));
  }

  if (!teams.length) {
    $('teams-table-out').innerHTML = '<div class="empty">No teams match your search.</div>';
    return;
  }

  $('teams-table-out').innerHTML = `<table class="teams-table">
    <thead><tr><th>Team</th><th>City</th><th>Division</th><th style="text-align:center">Roster</th><th>Link</th></tr></thead>
    <tbody>${teams.map(team => `<tr>
      <td class="player">${esc(team.team_name)}</td>
      <td>${esc([team.city, team.region].filter(Boolean).join(', '))}</td>
      <td><span class="team-badge div">${esc(team.division)}</span></td>
      <td class="num">${team.players?.length || 0}</td>
      <td>${team.url ? `<a href="${esc(team.url)}" target="_blank" rel="noopener noreferrer">View &rarr;</a>` : '—'}</td>
    </tr>`).join('')}</tbody>
  </table>`;
}

function initializeEvents() {
  $('overview-age').addEventListener('change', renderOverview);
  $('overview-city').addEventListener('change', renderOverview);

  // Mobile filter chips
  document.querySelectorAll('.filter-chip[data-age]').forEach(chip => {
    chip.addEventListener('click', () => {
      document.querySelectorAll('.filter-chip[data-age]').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      $('overview-age').value = chip.dataset.age;
      renderOverview();
    });
  });
  $('refresh-data').addEventListener('click', loadAllData);
  $('roster-age').addEventListener('change', () => {
    populateTeamSelect();
    renderRoster();
  });
  $('team-select').addEventListener('change', renderRoster);
  $('changes-age').addEventListener('change', renderChanges);
  $('changes-window').addEventListener('change', renderChanges);
  $('parse-csv').addEventListener('click', renderChanges);
  $('team-search').addEventListener('input', renderTeamsTable);

  $('team-grid').addEventListener('click', event => {
    const card = event.target.closest('.team-card');
    if (card) openTeamRoster(card.dataset.key);
  });
  $('team-grid').addEventListener('keydown', event => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const card = event.target.closest('.team-card');
    if (card) {
      event.preventDefault();
      openTeamRoster(card.dataset.key);
    }
  });
  $('roster-out').addEventListener('click', event => {
    const button = event.target.closest('.btn-history');
    if (!button) return;
    document.getElementById(`history-${button.dataset.playerId}`)?.classList.toggle('open');
  });

  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(item => {
        const active = item === tab;
        item.classList.toggle('active', active);
        item.setAttribute('aria-selected', String(active));
      });
      renderChanges();
    });
  });
}

initializeTheme();
initializeMobileNav();
initializeEvents();
loadAllData();
