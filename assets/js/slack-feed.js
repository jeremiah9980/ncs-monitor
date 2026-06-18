(() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));

  function formatTime(timestamp) {
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return timestamp || '';
    return date.toLocaleString('en-US', {
      timeZone: 'America/Chicago',
      month: 'short', day: 'numeric', year: 'numeric',
      hour: 'numeric', minute: '2-digit'
    });
  }

  function renderTeam(team) {
    const location = [team.city, team.region].filter(Boolean).join(', ');
    const rows = [];
    if (team.new_team) rows.push('<li class="slack-change new-team">New team now tracked <span>No prior baseline</span></li>');
    (team.removed || []).forEach(name => rows.push(`<li class="slack-change removed"><strong>Removed</strong> ${esc(name)}</li>`));
    (team.added || []).forEach(name => rows.push(`<li class="slack-change added"><strong>Added</strong> ${esc(name)}</li>`));
    return `<section class="slack-team"><h4>${esc(team.name)}${location ? `<span>${esc(location)}</span>` : ''}</h4><ul>${rows.join('')}</ul></section>`;
  }

  function render(alerts) {
    const out = document.getElementById('slack-alerts');
    if (!out) return;
    if (!alerts.length) {
      out.innerHTML = '<div class="empty">No Slack roster alerts have been recorded yet.</div>';
      return;
    }
    out.innerHTML = alerts.map(alert => `
      <article class="slack-alert-card">
        <header class="slack-alert-header">
          <div><div class="slack-app">NCS-MONITOR</div><h3>${esc(alert.subject)}</h3></div>
          <time datetime="${esc(alert.timestamp)}">${esc(formatTime(alert.timestamp))}</time>
        </header>
        <div class="slack-alert-summary">
          <span class="summary-pill removed">${Number(alert.removed || 0)} removed</span>
          <span class="summary-pill added">${Number(alert.added || 0)} added</span>
          <span class="summary-pill teams">${Number(alert.team_count || (alert.teams || []).length)} team${Number(alert.team_count || (alert.teams || []).length) === 1 ? '' : 's'}</span>
        </div>
        <div class="slack-team-list">${(alert.teams || []).map(renderTeam).join('')}</div>
      </article>
    `).join('');
  }

  fetch('reports/slack-feed.json', {cache: 'no-store'})
    .then(response => response.ok ? response.json() : Promise.reject(new Error(`Feed ${response.status}`)))
    .then(data => render(Array.isArray(data.alerts) ? data.alerts : []))
    .catch(error => {
      console.error('Unable to load Slack feed:', error);
      render([]);
    });
})();
