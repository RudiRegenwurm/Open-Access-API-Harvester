/* Open-Access Harvester — control center front end.
   No build step, no framework. All provider-supplied text is escaped before it
   reaches the DOM: remote metadata is untrusted input here just as it is in the core. */
'use strict';

/* ------------------------------------------------------------------ helpers */

const $ = (sel, root = document) => root.querySelector(sel);
const view = () => $('#view');

function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* Options for one `choice:` setting.
 *
 * Every option carries an explicit `value`, and `selected` is set only on an exact
 * match. A configured value that is not among the offered ones gets an option of its
 * own rather than no option at all: a select where nothing is selected silently
 * reports its *first* option instead, and because saving submits every field, that
 * invented value was then written to the configuration file — a setting the operator
 * never touched, changed behind their back. The extra option is marked so the save
 * can leave it alone, and labelled so the mismatch is visible instead of silent.
 */
function choiceOptions(spec, value) {
  const allowed = spec.split(':')[1].split(',');
  const current = value === null || value === undefined ? '' : String(value);
  const rows = allowed.map((option) =>
    `<option value="${esc(option)}"${current === option ? ' selected' : ''}>${
      esc(option)}</option>`);
  if (!allowed.includes(current)) {
    rows.unshift(`<option value="${esc(current)}" data-unknown="true" selected>${
      esc(current === '' ? '(not set)' : current)} — current value, not offered here</option>`);
  }
  return rows.join('');
}

function num(value) {
  return (value === null || value === undefined) ? '—' : Number(value).toLocaleString();
}

function bytes(value) {
  if (!value) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let n = Number(value), i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
}

function when(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
  return d.toISOString().replace('.000Z', 'Z');
}

function exact(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d) ? esc(iso) : d.toISOString().replace('.000Z', 'Z');
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  return `${m}m ${Math.round(seconds - m * 60)}s`;
}

/* How long something has been going, from the moment it started. Used for liveness:
   "running for 4m 12s" is a fact the backend already supplies, unlike a completion
   estimate, which it cannot. */
function elapsed(iso) {
  if (!iso) return '—';
  const started = new Date(iso);
  if (isNaN(started)) return '—';
  return duration(Math.max(0, (Date.now() - started.getTime()) / 1000));
}

const RUN_TONE = {
  COMPLETED: 'ok', RUNNING: 'busy', SUSPENDED: 'warn',
  INTERRUPTED: 'warn', FAILED: 'bad',
};

/* Acquisition state, said in words instead of in machine states.
 *
 * "completed" was read as a verdict on the document rather than on the download: in
 * the usability test the first row marked completed got opened instead of the paper
 * the researcher had come for. These labels answer the only question the state
 * actually answers — is the full text in the corpus? — and say nothing about whether
 * the document is any good. The backend value is unchanged and is kept in the title
 * attribute, so the raw state is still one hover away.
 */
const DOC_STATE = {
  COMPLETED:        ['Full text acquired', 'ok'],
  FAILED_PERMANENT: ['Full text unavailable', 'bad'],
  // The same document once it holds a validated XML but no PDF. Reached through
  // `docPill`'s artifact argument, never through a backend state: no document status
  // is added, renamed or reinterpreted for this.
  PARTIAL:          ['Partial full text', 'warn'],
  FAILED_RETRYABLE: ['Acquisition failed — can retry', 'warn'],
  SKIPPED:          ['Not acquired', 'neutral'],
  ACQUIRING:        ['Downloading', 'busy'],
  VALIDATING:       ['Validating', 'busy'],
  QUEUED:           ['Waiting to acquire', 'neutral'],
  NORMALIZED:       ['Waiting to acquire', 'neutral'],
  DISCOVERED:       ['Found by search', 'neutral'],
};

/* How document states group into the questions the interface asks, mirroring the
 * same grouping in `harvester.webui.api`.
 *
 * The server sends these totals ready-made. This is the fallback for when it does not:
 * the static assets are read from disk on every request while the API lives in the
 * server process's memory, so a Control Center left running across an upgrade serves
 * new markup against an older API. The per-document counts are in that older response
 * either way, and reconstructing from them beats printing a row of dashes over a run
 * whose documents are listed right below.
 */
const STATE_GROUPS = {
  full_text_available: ['COMPLETED'],
  unavailable: ['FAILED_PERMANENT', 'SKIPPED'],
  retryable: ['FAILED_RETRYABLE'],
  in_flight: ['ACQUIRING', 'VALIDATING'],
  pending: ['DISCOVERED', 'NORMALIZED', 'QUEUED'],
};

function groupCounts(counts) {
  const c = counts || {};
  const grouped = {};
  Object.keys(STATE_GROUPS).forEach((key) => {
    grouped[key] = STATE_GROUPS[key].reduce((total, name) => total + (c[name] || 0), 0);
  });
  grouped.in_progress = grouped.in_flight + grouped.pending;
  grouped.total = Object.keys(c).reduce((total, name) => total + (c[name] || 0), 0);
  return grouped;
}

/* States in which acquisition has stopped for good, so whatever artifacts are in hand
 * are the result rather than a snapshot of unfinished work.
 *
 * FAILED_RETRYABLE is deliberately absent. A document that will be tried again has no
 * final outcome yet, and "can retry" is the more useful thing to tell an operator than
 * what it happens to hold in the meantime. This is the same precedence
 * `StateStore.acquisition_state_counts` applies to its buckets — still-moving and
 * retryable documents are classified by state, settled ones by their artifacts — so a
 * badge and the counter above it can never disagree about the same document. */
const SETTLED_STATES = { COMPLETED: true, FAILED_PERMANENT: true, SKIPPED: true };

/* The sentence that explains a partial result wherever one is shown. */
const PARTIAL_EXPLANATION = 'XML available, PDF unavailable';

/* The badge for one document.
 *
 * `artifacts` is optional — {pdf: bool, xml: bool}, taken from the `has_pdf`/`has_xml`
 * every row already carries. When a settled document holds a validated XML and no PDF
 * the badge says so, instead of calling the full text unavailable while an XML chip
 * sits beside it: the contradiction reported from the roll-out. Called without the
 * argument this behaves exactly as before, and the raw state stays one hover away.
 */
/* One sentence over a document table: how many hold full text, how many are partial,
 * how many are unavailable, how many can still be retried, how many are still moving.
 * Built from exactly the same per-document logic docPill uses (including the
 * has_xml/has_pdf "partial" relabelling), so the sentence and the badges below it can
 * never disagree about the same document — the mismatch this replaces (Retry Failed's
 * two unreconciled counts) is exactly the failure mode this guards against.
 */
function docStatusSummary(documents) {
  const tally = {};
  documents.forEach((d) => {
    const partial = d.has_xml && !d.has_pdf && SETTLED_STATES[d.status];
    const known = DOC_STATE[partial ? 'PARTIAL' : d.status];
    const label = known ? known[0] : String(d.status || '').toLowerCase();
    tally[label] = (tally[label] || 0) + 1;
  });
  const total = documents.length;
  const acquired = tally['Full text acquired'] || 0;
  const partial = tally['Partial full text'] || 0;
  const unavailable = (tally['Full text unavailable'] || 0) + (tally['Not acquired'] || 0);
  const retryable = tally['Acquisition failed — can retry'] || 0;
  const inProgress = total - acquired - partial - unavailable - retryable;
  const parts = [`${num(acquired)} of ${num(total)} with full text`];
  if (partial) parts.push(`${num(partial)} partial`);
  if (unavailable) parts.push(`${num(unavailable)} unavailable`);
  if (retryable) parts.push(`${num(retryable)} can retry`);
  if (inProgress > 0) parts.push(`${num(inProgress)} still in progress`);
  return `<div class="doc-summary">${parts.join(' · ')}</div>`;
}

function docPill(status, artifacts) {
  const partial = artifacts && !artifacts.pdf && artifacts.xml && SETTLED_STATES[status];
  const known = DOC_STATE[partial ? 'PARTIAL' : status];
  const [label, tone] = known || [String(status || '').toLowerCase(), 'neutral'];
  return `<span class="pill ${tone}" title="${esc(status)}">${esc(label)}</span>`;
}

function pill(text, tone = 'neutral', animated = false) {
  return `<span class="pill ${tone}">${esc(text)}</span>`;
}

function runPill(status) {
  const tone = RUN_TONE[status] || 'neutral';
  return pill(status.toLowerCase(), tone, status === 'RUNNING');
}

function empty(icon, title, hint) {
  return `<div class="empty"><div>${esc(title)}</div>${
    hint ? `<div class="hint">${esc(hint)}</div>` : ''}</div>`;
}

/* --------------------------------------------------------------------- api */

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const buildVersion = response.headers.get('Server')?.match(/OAHarvesterUI\/(\d+\.\d+\.\d+)/);
  const versionLabel = $('#technical-version');
  if (versionLabel && buildVersion) versionLabel.textContent = ` ${buildVersion[1]}`;
  let payload = null;
  try { payload = await response.json(); } catch (_) { /* non-JSON body */ }
  if (!response.ok) {
    const error = new Error((payload && payload.error) || `Request failed (${response.status})`);
    error.status = response.status;
    error.detail = payload && payload.detail;
    throw error;
  }
  return payload;
}

function toast(title, message, tone = '') {
  const node = document.createElement('div');
  node.className = `toast ${tone}`;
  node.setAttribute('role', tone === 'bad' ? 'alert' : 'status');
  const label = tone === 'bad' ? 'Error: ' : tone === 'warn' ? 'Notice: ' : 'Note: ';
  node.innerHTML = `<div class="t-title">${label}${esc(title)}</div>${
    message ? `<div class="t-msg">${esc(message)}</div>` : ''}`;
  $('#toasts').appendChild(node);
  while ($('#toasts').children.length > 2) $('#toasts').firstElementChild.remove();
  setTimeout(() => { node.style.opacity = '0'; setTimeout(() => node.remove(), 300); },
    tone === 'bad' ? 8000 : 4500);
}

async function guard(fn, failTitle = 'Action failed') {
  try { return await fn(); }
  catch (error) { toast(failTitle, error.message, 'bad'); return null; }
}

/* ------------------------------------------------------------------- panel */

let panelReturnFocus = null;
function openPanel(title, subtitle, html) {
  panelReturnFocus = document.activeElement;
  $('#panel-title').textContent = title;
  $('#panel-sub').innerHTML = subtitle ? hashValue(subtitle) : '';
  $('#panel-content').innerHTML = html;
  $('#panel').hidden = false;
  $('#panel-content').scrollTop = 0;
  document.querySelector('.app').inert = true;
  document.querySelector('.sidebar-foot').inert = true;
  $('#panel [data-close-panel].icon-btn').focus();
}
function closePanel() {
  if ($('#panel').hidden) return;
  $('#panel').hidden = true;
  document.querySelector('.app').inert = false;
  document.querySelector('.sidebar-foot').inert = false;
  if (panelReturnFocus && panelReturnFocus.isConnected) panelReturnFocus.focus();
  panelReturnFocus = null;
}

document.addEventListener('click', (event) => {
  if (event.target.closest('[data-close-panel]')) closePanel();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closePanel();
  if (event.key === 'Tab' && !$('#panel').hidden) {
    const controls = [...$('#panel').querySelectorAll('a[href],button:not(:disabled),input,select,textarea,[tabindex="0"]')].filter(el => el.getClientRects().length);
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});

/* ------------------------------------------------------------------ header */

function setHeader(title, subtitle, actions = '') {
  $('#page-title').textContent = title;
  $('#page-sub').textContent = subtitle || '';
  $('#topbar-actions').innerHTML = actions;
}

/* ------------------------------------------------------ activity polling */

const activity = { timer: null, busy: false, announced: null };

async function pollActivity() {
  let data;
  try { data = await api('/api/activity'); } catch (_) { return; }
  const box = $('#sidebar-activity');
  const op = data.operation;
  activity.busy = data.busy;

  if (op && (data.busy || !op.finished)) {
    const progress = data.progress;
    box.hidden = false;
    box.innerHTML = `
      <div class="ma-label">${esc(op.label)}</div>
      <div class="ma-sub">${esc(phaseLabel(op, progress))}${
        progress && progress.discovery_complete
          ? ` · ${num(progress.settled)}/${num(progress.total)} documents`
          : (progress ? ` · ${num(progress.discovered)} found` : '')}</div>
      ${progress && progress.discovery_complete && progress.total
        ? `<div class="bar" style="margin-top:7px"><i style="width:${progress.percent}%"></i></div>` : ''}
      <div class="ma-sub">${esc(elapsed(op.started_at))} elapsed${
        progress && progress.last_activity_at ? ` · ${esc(when(progress.last_activity_at))}` : ''}</div>`;
  } else if (op && op.finished) {
    box.hidden = true;
  } else {
    box.hidden = true;
  }

  // Announce each finished operation exactly once. A dry run renders its preview in
  // place; anything else refreshes the page. Refreshing after a dry run would wipe
  // the preview table the operator is about to act on.
  const token = op && op.finished ? `${op.kind}:${op.started_at}` : null;
  if (token && token !== activity.announced) {
    activity.announced = token;
    if (op.kind === 'harvest' && op.ok && op.run_id) rememberCompletedHarvest(op);
    toast(op.ok ? 'Finished' : 'Did not complete', op.message || '', op.ok ? 'ok' : 'bad');
    if (op.kind === 'dry_run' && op.run_id && (location.hash || '').startsWith('#/harvest')) {
      guard(() => showPreview(op.run_id), 'Could not load the preview');
    } else {
      rerender();
    }
  }

  if (['#/dashboard', '#/harvest'].includes(location.hash || '#/dashboard')) {
    const live = $('#live-activity');
    if (live) live.innerHTML = activityCard(data);
  }
}

/* Keyed off the query the run recorded for itself rather than off whatever the
   browser last sent, so the page still knows what finished after a reload. */
function rememberCompletedHarvest(op) {
  const query = (op.detail && op.detail.query) || {};
  searchState.harvested = {
    runId: op.run_id,
    key: harvestKey(query),
    query: query.search || query.topic_id || '',
    message: op.message || '',
  };
}

function startPolling() {
  if (activity.timer) clearInterval(activity.timer);
  activity.timer = setInterval(pollActivity, 1800);
  pollActivity();
}

/* What the operation is doing right now, in words rather than in the internal phase
   name, and truthful about the one thing the operator cannot otherwise see: whether
   discovery has finished, and therefore whether the total is final or still growing. */
function phaseLabel(op, progress) {
  if (op.finished) return op.ok ? 'finished' : 'stopped';
  if (op.kind === 'verify') return 'checking artifacts';
  if (!progress) return 'starting';
  if (!progress.discovery_complete) return 'searching';
  if (progress.in_flight) return 'downloading';
  if (progress.pending) return 'acquiring';
  return op.phase || 'running';
}

/* How long the operation has been going, or how long it took.
 *
 * A finished run must never keep counting. Reporting time-since-it-started for a run
 * that stopped twenty hours ago produced "Ran for 1242m 4s" for a harvest that in fact
 * took ninety seconds — the elapsed clock is the right number only while something is
 * still running. Once it has stopped, the run's own recorded duration is the honest
 * answer, and when that was never recorded the honest answer is to say nothing about
 * the length rather than compute a number that means something else.
 */
function operationTiming(op, progress, running) {
  const p = progress || {};
  if (running) {
    return `Running for ${esc(elapsed(op.started_at))}.${
      p.last_activity_at ? ` Last activity ${esc(when(p.last_activity_at))}.` : ''}`;
  }
  const recorded = p.duration_seconds !== undefined && p.duration_seconds !== null
    ? p.duration_seconds
    : (op.result && op.result.stats ? op.result.stats.duration_seconds : null);
  const parts = [];
  if (recorded !== undefined && recorded !== null) parts.push(`Ran for ${esc(duration(recorded))}.`);
  const finished = p.finished_at || null;
  if (finished) parts.push(`Finished ${esc(when(finished))}.`);
  return parts.length ? parts.join(' ') : 'This operation has finished.';
}

function statCells(cells) {
  return `<div class="grid grid-4" style="margin-top:14px">${cells.map(
    ([label, value, tone]) => `<div><div class="stat-label">${esc(label)}</div>
      <div class="stat-value ${tone || ''}" style="font-size:20px">${value}</div></div>`).join('')}</div>`;
}

/* Live counters for one operation.
 *
 * A harvest that reported nothing but the word "running" for several minutes was the
 * single loudest complaint from the usability test: the operator could not tell a
 * working run from a hung one. Everything shown here is read from the state database,
 * and nothing is estimated. In particular the completion bar appears only once
 * discovery has finished, because until then the denominator is still growing and a
 * percentage of it would be a guess wearing the clothes of a measurement. While
 * discovery runs, the honest report is how many records have been found so far and
 * how many pages have been read. Elapsed time and last activity answer the rest.
 */
function activityCard(data) {
  const op = data.operation;
  if (!op) {
    return `<div class="card"><div class="card-head"><h2>Current activity</h2></div>
      <div class="card-body">${empty('', 'Nothing running', 'Start a harvest to see live progress here.')}</div></div>`;
  }
  const p = data.progress;
  const running = data.busy || !op.finished;
  const done = !running && op.ok && op.kind === 'harvest' && op.run_id;
  // An older API sends the per-document counts but not the grouped totals.
  const g = p ? (p.acquired === undefined ? groupCounts(p.document_counts) : p) : null;
  const acquired = g ? (g.acquired === undefined ? g.full_text_available : g.acquired) : 0;
  const stalled = g ? (g.unavailable || 0) + (g.retryable || 0) : 0;
  return `<div class="card">
    <div class="card-head">
      <div><h2>Current activity</h2><div class="sub">${esc(op.label)}</div></div>
      ${running ? pill(phaseLabel(op, p), 'busy', true) : pill(op.ok ? 'finished' : 'stopped', op.ok ? 'ok' : 'bad')}
    </div>
    <div class="card-body">
      ${op.message ? `<div class="notice ${op.ok === false ? 'bad' : 'info'}" role="status" style="margin-bottom:12px">
        <span class="n-ico">${op.ok === false ? 'Error:' : 'Note:'}</span><div>${esc(op.message)}</div></div>` : ''}
      ${p ? `
        ${p.discovery_complete
          ? `<div class="bar"><i style="width:${p.percent}%"></i></div>
             <div class="hint" style="margin-top:6px">${num(p.settled)} of ${num(p.total)} document(s) resolved</div>`
          : `<div class="hint">Still searching — ${num(p.discovered)} record(s) found so far across
             ${num(p.discovery_pages)} page(s). The total is not final yet, so there is nothing
             honest to show a percentage of.</div>`}
        ${statCells([
          ['Found by search', num(p.discovered)],
          ['Full text acquired', num(acquired), 'ok'],
          ...(p.partial_full_text === undefined ? [] : [
            ['Partial full text', num(p.partial_full_text), p.partial_full_text ? 'warn' : ''],
          ]),
          ['Downloading now', num(g.in_flight), g.in_flight ? 'busy' : ''],
          ['Unavailable', num(stalled), stalled ? 'bad' : ''],
        ])}
        <div class="hint" style="margin-top:10px">${operationTiming(op, p, running)}</div>`
        : `<div class="hint">${running
          ? `Starting — ${esc(elapsed(op.started_at))} elapsed. Waiting for the first page of results…`
          : operationTiming(op, null, false)}</div>`}
      ${op.run_id ? `<div style="margin-top:14px">
        <a class="btn small" href="#/runs/${encodeURIComponent(op.run_id)}">${
          done ? 'View harvest results' : 'Open run detail'}</a></div>` : ''}
    </div></div>`;
}

/* --------------------------------------------------------------- dashboard */

async function viewDashboard() {
  const data = await api('/api/dashboard');
  // No header actions here: "Start a harvest" and "Corpus integrity" below already put
  // both actions one click away on this same page, and the navigation still reaches New
  // Harvest and Verify from anywhere. Three entry points to the same action crowded a
  // page that already has to earn its space (usability finding A5).
  setHeader('Dashboard', `Storage root: ${data.storage_root}`);

  const v = data.last_verification;
  const cards = [
    { label: 'Documents', value: num(data.cards.documents),
      foot: `${num(data.cards.completed)} with full text` },
    { label: 'PDF artifacts', value: num(data.cards.pdfs), foot: bytes(data.cards.bytes) },
    { label: 'XML artifacts', value: num(data.cards.xmls),
      foot: data.cards.partial_full_text
        ? `${num(data.cards.partial_full_text)} document(s): ${PARTIAL_EXPLANATION}`
        : 'optional full text' },
    {
      label: 'Last verification',
      value: v ? (v.ok ? 'OK' : 'Problems') : '—',
      tone: v ? (v.ok ? 'ok' : 'bad') : '',
      foot: v ? `${v.deep ? 'deep · ' : ''}${when(v.checked_at)}` : 'never run',
    },
  ];

  view().innerHTML = `
    ${data.blockers.length ? `<div class="notice warn" role="status"><span class="n-ico">Notice:</span>
      <div><strong>Setup needed before harvesting.</strong><div>${esc(data.blockers[0])}</div>
      <div class="n-action"><a href="#/settings">Open Settings →</a></div></div></div>` : ''}

    <div class="grid grid-4">
      ${cards.map((c) => `<div class="card stat">
        <div class="stat-label">${esc(c.label)}</div>
        <div class="stat-value ${c.tone || ''}">${c.value}</div>
        <div class="stat-foot">${esc(c.foot)}</div></div>`).join('')}
    </div>

    <div class="split">
      <div style="display:flex;flex-direction:column;gap:18px">
        ${quickStartCard(data)}
        <div class="card">
          <div class="card-head"><h2>Recent runs</h2><a class="btn small" href="#/runs">View all</a></div>
          <div class="card-body tight">${runsTable(data.recent_runs, true)}</div>
        </div>
      </div>
      <div style="display:flex;flex-direction:column;gap:18px">
        <div id="live-activity">${activityCard(data.activity)}</div>
        ${providersCard(data.providers)}
        ${integrityCard(data)}
      </div>
    </div>`;
}

function quickStartCard(data) {
  return `<div class="card">
    <div class="card-head"><div><h2>Start a harvest</h2>
      <div class="sub">Search Open-Access literature and download what is legitimately available.</div></div></div>
    <div class="card-body">
      <form onsubmit="actions.quickStart(event)">
        <div class="field">
          <label for="q-search">What are you looking for?</label>
          <input id="q-search" type="text" placeholder="e.g. moral psychology, CRISPR gene editing" autocomplete="off">
          <span class="hint">Free-text search across OpenAlex. Add a Topic ID under New Harvest for precise subject filtering.</span>
        </div>
        <div class="row">
          <div class="field"><label for="q-limit">How many documents?</label>
            <input id="q-limit" type="number" min="1" max="10000" value="25"></div>
          <div class="field quick-start-actions"><label>&nbsp;</label>
            <div style="display:flex;gap:8px">
              <button class="btn" type="button" onclick="actions.quickStart(event, true)">Preview search results</button>
              <button class="btn primary" type="submit" ${data.can_start ? '' : 'disabled'}>Start harvest</button>
            </div></div>
        </div>
        ${data.can_start ? '' : '<div class="hint" style="color:var(--warn)">Harvesting is blocked until setup is complete — a preview still works.</div>'}
      </form>
    </div></div>`;
}

function providersCard(providers) {
  const tone = { ready: 'ok', warning: 'warn', blocked: 'bad', disabled: 'neutral' };
  return `<div class="card">
    <div class="card-head"><h2>Providers</h2><a class="btn small" href="#/settings">Configure</a></div>
    <div class="card-body">${providers.map((p) => `
      <div class="provider-row">
        <div class="pr-main">
          <div class="pr-name">${esc(p.name)}</div>
          <div class="pr-role">${esc(p.role)}</div>
          <div class="pr-detail">${esc(p.detail)}</div>
          ${p.action ? `<div class="pr-detail" style="color:var(--accent)">${esc(p.action)}</div>` : ''}
        </div>
        ${pill(p.state, tone[p.state] || 'neutral')}
      </div>`).join('')}</div></div>`;
}

function integrityCard(data) {
  const v = data.last_verification;
  return `<div class="card">
    <div class="card-head"><h2>Corpus integrity</h2>${
      v ? pill(v.ok ? 'healthy' : `${v.problem_count} problem(s)`, v.ok ? 'ok' : 'bad') : pill('unknown', 'neutral')}</div>
    <div class="card-body">
      <p style="color:var(--text-2);font-size:15px">${
        v ? `Checked ${esc(when(v.checked_at))}. Verification compares every recorded artifact against the file on disk, including its SHA-256.`
          : 'The corpus has not been verified yet. Verification confirms every recorded artifact still exists and still matches its checksum.'}</p>
      <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
        <button class="btn" onclick="actions.verify(false)">Run verify</button>
        <button class="btn" onclick="actions.verify(true)">Deep verify</button>
        <a class="btn link" href="#/verify">Details →</a>
      </div>
    </div></div>`;
}

/* ------------------------------------------------------------ new harvest */

/* Every New Harvest input lives here rather than only in the DOM, so the page can be
   re-rendered — on a mode switch, after an advisor answer, when a run finishes —
   without losing anything the operator typed (specification section 20). The two
   modes keep separate query state: the conventional query is never overwritten by a
   generated one, and a generated one is never silently rewritten. */
const searchState = {
  mode: 'conventional',
  conventionalQuery: '',
  researchQuestion: '',
  generatedQuery: '',   // exactly what the advisor proposed, kept for provenance
  effectiveQuery: '',   // what will actually be executed; the operator may edit it
  advice: null,
  advisorError: null,
  preview: null,        // { fingerprint, key, results, total_count, query }
  previewError: null,
  busy: null,           // 'advice' | 'preview' | 'harvest'
  // The harvest this search has already produced, if any: { runId, key, query, message }.
  // Its presence is what turns New Harvest from a page that is waiting to be filled in
  // into a page that reports a result (UX item 3).
  harvested: null,
  // A pending "you have run this before" answer, and the request key it was given for.
  // Advisory only — see actions.checkForRepeat (UX item 8).
  duplicates: null,
  duplicateAck: null,
  filters: {
    topic_id: '', limit: '25', from_year: '', to_year: '', oa_status: '',
    primary_topic_only: false, xml_policy: 'preferred', europe_pmc: true, unpaywall: true,
    // Empty means "any" for both — exactly the behaviour before these constraints
    // existed. Multiple values are OR-ed by the provider.
    languages: [], affiliation_countries: [],
  },
};

/* The value lists come from the server so the browser, the API and the CLI validate
   against one table. Fetched once per page load; they change only when the provider's
   own vocabulary does. */
let vocabulary = null;

async function loadVocabulary() {
  if (!vocabulary) vocabulary = await api('/api/vocabulary');
  return vocabulary;
}

function multiSelectField(id, key, label, hint, entries, extra = '') {
  const chosen = new Set(searchState.filters[key]);
  const options = entries.map((e) =>
    `<option value="${esc(e.code)}" ${chosen.has(e.code) ? 'selected' : ''}>${esc(e.name)}</option>`).join('');
  const names = entries.filter((e) => chosen.has(e.code)).map((e) => e.name);
  return `<div class="field">
    <label for="${id}">${esc(label)}</label>
    <select id="${id}" multiple size="6" onchange="actions.multi('${key}', this)">${options}</select>
    <div class="multi-state">
      <span>${chosen.size
        ? `${esc(names.join(', '))} — matches any of ${chosen.size}`
        : 'Any — no filter applied'}</span>
      ${chosen.size ? `<button type="button" class="link-btn" onclick="actions.clearMulti('${key}')">Clear</button>` : ''}
    </div>
    <span class="hint">${esc(hint)}${extra}</span>
  </div>`;
}

/* One canonical query per handoff, whichever mode produced it (section 21). */
function activeQuery() {
  const raw = searchState.mode === 'assisted'
    ? searchState.effectiveQuery : searchState.conventionalQuery;
  return raw.trim();
}

/* The discovery inputs a preview approval is tied to. This mirrors what the server
   fingerprints; the server recomputes and re-checks it before any harvest starts, so
   this copy only decides whether the button looks available. */
function discoveryKey() {
  const f = searchState.filters;
  return JSON.stringify([
    activeQuery(), f.topic_id.trim(), !!f.primary_topic_only,
    String(f.from_year), String(f.to_year), f.oa_status,
    // Sorted so the key depends on the selection, not on the click order.
    [...f.languages].sort(), [...f.affiliation_countries].sort(),
  ]);
}

function previewIsCurrent() {
  return !!(searchState.preview && searchState.preview.key === discoveryKey());
}

/* Identity of one harvest request, from its discovery inputs alone.
 *
 * Accepts either shape the same inputs arrive in — the request payload this page
 * builds, and the query a finished run recorded for itself — so "is the run that just
 * finished still the search in this form?" can be answered without keeping a copy of
 * the request around. Acquisition settings are deliberately left out: they change what
 * is downloaded, not what is searched for, and this key exists to recognise a search.
 */
function harvestKey(query) {
  const text = (value) => (value === null || value === undefined ? '' : String(value).trim());
  return JSON.stringify([
    text(query.search), text(query.topic_id), !!query.primary_topic_only,
    text(query.from_publication_year === undefined ? query.from_year : query.from_publication_year),
    text(query.to_publication_year === undefined ? query.to_year : query.to_publication_year),
    text(query.oa_status),
    [...(query.languages || [])].sort(),
    [...(query.affiliation_countries || [])].sort(),
  ]);
}

function currentHarvestKey() {
  return harvestKey(harvestPayload(false));
}

/* The finished harvest, but only while the form still describes the search that
   produced it. Editing anything puts the page back into its pre-harvest state. */
function harvestComplete() {
  const done = searchState.harvested;
  return done && done.key === currentHarvestKey() ? done : null;
}

function modeCard() {
  const option = (id, title, subtitle) => `
    <button type="button" class="mode-option ${searchState.mode === id ? 'active' : ''}"
      role="radio" aria-checked="${searchState.mode === id}" onclick="actions.setMode('${id}')">
      <span class="mo-title">${esc(title)}</span>
      <span class="mo-sub">${esc(subtitle)}</span>
    </button>`;
  return `<div class="field">
    <label>Search mode</label>
    <div class="mode-switch" role="radiogroup" aria-label="Search mode">
      ${option('conventional', 'Conventional Search',
        'I already have the words to search for — a phrase, a paper title, a DOI or an author name.')}
      ${option('assisted', 'Assisted Search',
        'I have a topic or a research question and want help turning it into a search query.')}
    </div>
    <span class="hint">Not sure? If you could type your search into a library catalogue
      as it stands, choose <strong>Conventional Search</strong>. If you would rather
      describe what you are trying to find out and let the query be written for you,
      choose <strong>Assisted Search</strong>. Both run the same harvest afterwards,
      and switching between them keeps whatever you have already typed.</span>
  </div>`;
}

function conventionalFields() {
  return `
    <div class="field">
      <label for="h-search">Search terms</label>
      <input id="h-search" type="text" placeholder="e.g. moral psychology" autocomplete="off"
        value="${esc(searchState.conventionalQuery)}"
        oninput="actions.field('conventionalQuery', this.value)">
      <span class="hint">Used exactly as typed. Leave empty if you are targeting a Topic ID instead.</span>
    </div>`;
}

function assistedFields(advisor) {
  const busy = searchState.busy === 'advice';
  const unavailable = advisor && advisor.state !== 'ready';
  return `
    <div class="field">
      <label for="h-question">What are you researching?</label>
      <textarea id="h-question" rows="4" placeholder="e.g. I want to investigate whether ADHD symptoms can remit during adulthood and later recur when life demands change."
        oninput="actions.field('researchQuestion', this.value)">${esc(searchState.researchQuestion)}</textarea>
      <span class="hint">Describe your research question in normal language. You do not need to formulate search keywords.</span>
    </div>
    <div class="notice info" role="status" style="margin-bottom:14px"><span class="n-ico">Note:</span>
      <div>Your research question is sent to the configured query-advisor service to
      generate a search query. Conventional Search never contacts it.</div></div>
    ${unavailable ? `<div class="notice warn" role="status" style="margin-bottom:14px"><span class="n-ico">Notice:</span>
      <div>${esc(advisor.detail)}${advisor.action ? `<div class="n-action"><a href="#/settings">${esc(advisor.action)} →</a></div>` : ''}</div></div>` : ''}
    ${searchState.advisorError ? `<div class="notice bad" role="alert" style="margin-bottom:14px"><span class="n-ico">Notice:</span>
      <div>${esc(searchState.advisorError)}<div class="n-action">Retry, or switch to Conventional Search — your input is preserved.</div></div></div>` : ''}
    <button class="btn ${searchState.advice ? '' : 'primary'}" type="button" ${busy ? 'disabled' : ''}
      onclick="actions.generateAdvice()">${busy ? 'Generating…' : (searchState.advice ? 'Generate another suggestion' : 'Generate search query')}</button>
    ${adviceBlock()}`;
}

function adviceBlock() {
  const advice = searchState.advice;
  if (!advice) return '';
  const previewing = searchState.busy === 'preview';
  return `
    <div class="advice">
      <div class="field" style="margin-top:18px">
        <label for="h-recommended">Recommended search query</label>
        <input id="h-recommended" type="text" autocomplete="off" value="${esc(searchState.effectiveQuery)}"
          oninput="actions.field('effectiveQuery', this.value)">
        <span class="hint">Editable. The harvest executes exactly what this field says — nothing is rewritten behind your back.</span>
      </div>
      ${advice.rationale ? `<div class="advice-note"><h3>Why this query?</h3><p>${esc(advice.rationale)}</p></div>` : ''}
      ${(advice.deferred_terms || []).length ? `<div class="advice-note"><h3>Deferred for now</h3>
        <div>${advice.deferred_terms.map((t) => `<span class="chip">${esc(t)}</span>`).join(' ')}</div>
        <p class="hint">Broad terms left out of the initial discovery. Add them to the query yourself if you want them.</p></div>` : ''}
      <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">
        <button class="btn ${previewIsCurrent() ? '' : 'primary'}" type="button" ${previewing ? 'disabled' : ''}
          onclick="actions.runPreview()">${previewing ? 'Previewing…' : 'Preview search results'}</button>
        <button class="btn" type="button" ${searchState.busy === 'advice' ? 'disabled' : ''}
          onclick="actions.generateAdvice()">Generate another suggestion</button>
      </div>
    </div>`;
}

/* What to do now that the harvest has run (UX item 3).
 *
 * The usability test ended with the operator back on a page that still looked exactly
 * like the one they had started from — search preview visible, "Start full harvest"
 * still the brightest button — and seriously considering running the same harvest
 * again because nothing said it was over. So the finished run gets the primary action,
 * and it points at the results rather than at another harvest.
 */
function harvestDoneCard(done, live) {
  const current = harvestComplete();
  const p = live && live.progress && live.progress.run_id === done.runId ? live.progress : null;
  const unavailable = p ? (p.unavailable || 0) + (p.retryable || 0) : 0;
  return `<div class="card" id="harvest-done" ${current ? '' : 'hidden'}>
    <div class="card-head">
      <div><h2>Harvest finished</h2>
        <div class="sub">${esc(done.query || 'this search')}</div></div>
      ${pill('finished', 'ok')}
    </div>
    <div class="card-body">
      ${done.message ? `<div class="notice ok" role="status" style="margin-bottom:14px"><span class="n-ico">Completed:</span>
        <div>${esc(done.message)}</div></div>` : ''}
      ${p ? statCells([
        ['Found by search', num(p.discovered)],
        ['Full text acquired', num(p.acquired), 'ok'],
        ...(p.partial_full_text === undefined ? [] : [
          ['Partial full text', num(p.partial_full_text), p.partial_full_text ? 'warn' : ''],
        ]),
        ['Unavailable', num(unavailable), unavailable ? 'bad' : ''],
      ]) : ''}
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:16px">
        <a class="btn primary" href="#/runs/${encodeURIComponent(done.runId)}">View harvest results</a>
        <button class="btn" type="button" onclick="actions.refineSearch()">Refine search</button>
        ${unavailable ? `<a class="btn" href="#/failures">Retry unavailable (${num(unavailable)})</a>` : ''}
        <button class="btn" type="button" onclick="actions.newHarvest()">New harvest</button>
      </div>
      <div class="hint" style="margin-top:12px">The settings below still describe this
        search. Change any of them to refine it — this panel steps aside as soon as you
        do — or use <strong>Run this harvest again</strong> to repeat it exactly.</div>
    </div></div>`;
}

/* The repeat-run advisory (UX item 8). It states a fact and offers both ways out; it
   never withholds the harvest, because deliberately re-running a search is how a
   result gets reproduced. */
function duplicateWarningCard() {
  const pending = searchState.duplicates;
  if (!pending || !pending.runs.length) return '';
  const rows = pending.runs.map((r) => {
    const state = (r.outcome && r.outcome.corpus_state) || {};
    return `<li class="pv-item">
      <div class="pv-title">${esc(when(r.started_at))} · ${esc(r.status.toLowerCase())}</div>
      <div class="pv-meta">${num(state.full_text_available || 0)} full text(s) available,
        ${num(state.unavailable || 0)} unavailable · limit ${
          r.record_limit ? num(r.record_limit) : 'none'}</div>
      ${(r.differences || []).length
        ? `<div class="pv-meta">Different this time — ${esc(r.differences.join('; '))}</div>` : ''}
    </li>`;
  }).join('');
  return `<div class="card" id="duplicate-warning" ${
      pending.key === currentHarvestKey() ? '' : 'hidden'}>
    <div class="card-head">
      <div><h2>This search was already harvested recently</h2>
        <div class="sub">Nothing has been started. Running the same search again is a
          legitimate thing to do — this is here so that doing it by accident is visible.</div></div>
      ${pill('already run', 'warn')}
    </div>
    <div class="card-body">
      <ol class="pv-list">${rows}</ol>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px">
        <a class="btn primary" href="#/runs/${encodeURIComponent(pending.runs[0].run_id)}">Open existing run</a>
        <button class="btn" type="button" onclick="actions.runAnyway()">Run again</button>
        <button class="btn" type="button" onclick="actions.dismissDuplicate()">Cancel</button>
      </div>
    </div></div>`;
}

async function viewHarvest() {
  const [data, vocab, live] = await Promise.all([
    api('/api/providers'),
    loadVocabulary(),
    // Rendered straight away rather than after the first poll, so the page never opens
    // on an empty "nothing running" panel while a harvest is in fact in flight.
    api('/api/activity').catch(() => ({ busy: false, operation: null, progress: null })),
  ]);
  const advisor = data.advisor || null;
  setHeader('New Harvest', 'Choose how the query is built, check what it finds, then run it.');
  const f = searchState.filters;
  const assisted = searchState.mode === 'assisted';
  const done = harvestComplete();
  if (data.providers.find((p) => p.id === 'unpaywall').state === 'blocked') f.unpaywall = false;

  view().innerHTML = `
    ${searchState.harvested ? harvestDoneCard(searchState.harvested, live) : ''}
    ${duplicateWarningCard()}
    <div class="split">
      <div class="card">
        <div class="card-head"><h2>Harvest settings</h2></div>
        <div class="card-body">
          <form id="harvest-form" onsubmit="actions.submitHarvest(event, false)">
            ${modeCard()}
            ${assisted ? assistedFields(advisor) : conventionalFields()}
            <div class="field">
              <label for="h-topic">OpenAlex Topic ID <span style="font-weight:400;color:var(--text-3)">(optional)</span></label>
              <input id="h-topic" type="text" placeholder="e.g. T10159" autocomplete="off"
                value="${esc(f.topic_id)}" oninput="actions.filter('topic_id', this.value)">
              <span class="hint">Topics are OpenAlex's current subject taxonomy. Deprecated Concept IDs (C…) are not supported.</span>
            </div>
            <div class="row">
              <div class="field"><label for="h-limit">Maximum documents</label>
                <input id="h-limit" type="number" min="1" max="10000" value="${esc(f.limit)}"
                  oninput="actions.filter('limit', this.value)">
                <span class="hint">Keeps a first run quick and cheap.</span></div>
              <div class="field"><label for="h-xml">Full-text XML</label>
                <select id="h-xml" onchange="actions.filter('xml_policy', this.value)">
                  <option value="preferred" ${f.xml_policy === 'preferred' ? 'selected' : ''}>Preferred — take it when available</option>
                  <option value="disabled" ${f.xml_policy === 'disabled' ? 'selected' : ''}>Skip XML — PDF only</option>
                  <option value="required" ${f.xml_policy === 'required' ? 'selected' : ''}>Required — only keep documents with XML</option>
                </select></div>
            </div>
            <div class="row" style="margin-bottom:14px">
              <label class="check"><input id="h-epmc" type="checkbox" ${f.europe_pmc ? 'checked' : ''}
                onchange="actions.filter('europe_pmc', this.checked)">
                <span><span class="ct">Europe PMC</span><span class="cs">Cross-check identity and fetch XML full text</span></span></label>
              <label class="check"><input id="h-upw" type="checkbox" ${f.unpaywall ? 'checked' : ''}
                onchange="actions.filter('unpaywall', this.checked)">
                <span><span class="ct">Unpaywall</span><span class="cs">Fallback when no PDF is found</span></span></label>
            </div>

            <details class="advanced" ${f.from_year || f.to_year || f.oa_status || f.primary_topic_only
                || (f.languages && f.languages.length) ? 'open' : ''}>
              <summary>Advanced query options</summary>
              <div class="adv-body">
                <div class="row">
                  <div class="field"><label for="h-from">From year</label>
                    <input id="h-from" type="number" min="1500" max="2100" placeholder="any"
                      value="${esc(f.from_year)}" oninput="actions.filter('from_year', this.value)"></div>
                  <div class="field"><label for="h-to">To year</label>
                    <input id="h-to" type="number" min="1500" max="2100" placeholder="any"
                      value="${esc(f.to_year)}" oninput="actions.filter('to_year', this.value)"></div>
                  <div class="field"><label for="h-oa">OA status</label>
                    <select id="h-oa" onchange="actions.filter('oa_status', this.value)">
                      ${['', 'gold', 'hybrid', 'green', 'bronze', 'diamond'].map((v) =>
                        `<option value="${v}" ${f.oa_status === v ? 'selected' : ''}>${v || 'any'}</option>`).join('')}
                    </select></div>
                </div>
                <label class="check"><input id="h-primary" type="checkbox" ${f.primary_topic_only ? 'checked' : ''}
                  onchange="actions.filter('primary_topic_only', this.checked)">
                  <span><span class="ct">Primary topic only</span><span class="cs">Match only works whose main subject is this topic</span></span></label>
                ${multiSelectField('h-langs', 'languages', 'Language(s)',
                  'The language a work is published in, as reported by OpenAlex. Leave everything unselected for any language; selecting several matches any of them.',
                  vocab.languages)}
                ${multiSelectField('h-countries', 'affiliation_countries', 'Research institutions from',
                  'Filters by countries associated with author institutional affiliations. This is not the country a study was carried out in, nor the publisher’s country, nor the geographic subject of the research. Leave everything unselected for any country; selecting several matches any of them.',
                  vocab.countries)}
              </div>
            </details>

            ${assisted ? `<div class="hint" style="margin-top:16px">${done
              ? 'This search has already been harvested — the results are one click away at the top of the page. Change the query here to search for something else.'
              : 'Assisted Search starts a harvest only after you have previewed the results and pressed <strong>Start full harvest</strong> below.'}</div>`
            : `<div style="display:flex;gap:8px;margin-top:16px">
              <button class="btn" type="button" onclick="actions.submitHarvest(event, true)">Preview search results</button>
              <button class="btn ${done ? '' : 'primary'}" id="start-harvest" type="submit" ${
                data.can_start ? '' : 'disabled'}>${done ? 'Run this harvest again' : 'Start harvest'}</button>
            </div>`}
            ${data.blockers.length ? `<div class="notice warn" role="status" style="margin-top:14px"><span class="n-ico">Notice:</span>
              <div>${esc(data.blockers[0])}<div class="n-action"><a href="#/settings">Open Settings →</a></div></div></div>` : ''}
          </form>
        </div></div>

      <div style="display:flex;flex-direction:column;gap:18px">
        <div id="live-activity">${activityCard(live)}</div>
        ${providersCard(data.providers)}
      </div>
    </div>
    <div id="assisted-preview" ${done ? 'hidden' : ''}>${assisted ? assistedPreviewCard() : ''}</div>
    <div id="preview-area"></div>`;

  pollActivity();
}

/* ------------------------------------------------------- assisted preview */

function assistedPreviewCard() {
  if (searchState.previewError) {
    return `<div class="card"><div class="card-body"><div class="notice bad" role="alert"><span class="n-ico">Notice:</span>
      <div><strong>The preview could not be completed.</strong><div>${esc(searchState.previewError)}</div>
      <div class="n-action">Your query and filters are unchanged. Try again.</div></div></div></div></div>`;
  }
  const preview = searchState.preview;
  if (!preview) return '';
  const stale = !previewIsCurrent();
  const previewButton = $('button[onclick="actions.runPreview()"]');
  if (previewButton) previewButton.classList.toggle('primary', stale);
  const rows = preview.results.map((r, i) => `
    <li class="pv-item">
      <div class="pv-title">${i + 1}. ${esc(r.title || '(untitled)')}</div>
      <div class="pv-meta">${esc(r.first_author || 'unknown author')}${
        r.authors && r.authors.length > 1 ? ' et al.' : ''} · ${esc(r.publication_year || 'year unknown')}${
        r.journal ? ` · ${esc(r.journal)}` : ''}</div>
      <div class="pv-meta mono">${r.doi ? `DOI: ${esc(r.doi)}` : 'no DOI'}${
        r.oa_status ? ` · ${esc(r.oa_status)}` : ''} · ${esc((r.discovered_via || []).join(', ') || 'openalex')}</div>
    </li>`).join('');

  return `<div class="card">
    <div class="card-head">
      <div><h2>Search preview</h2>
        <div class="sub">${preview.results.length
          ? `Showing ${num(preview.results.length)} of ${preview.total_count === null || preview.total_count === undefined
              ? 'an unreported number of' : num(preview.total_count)} matching record(s). Nothing has been downloaded and nothing was added to the corpus.`
          : 'No results found.'}</div></div>
      <div style="display:flex;gap:8px">
        <button class="btn primary" id="start-full-harvest" ${stale || !preview.results.length || searchState.busy ? 'disabled' : ''}
          onclick="actions.startAssistedHarvest()">Start full harvest</button>
        <button class="btn" type="button" onclick="actions.editQuery()">Edit query</button>
      </div>
    </div>
    <div class="card-body">
      <div class="kv-inline"><strong>Effective query:</strong> <code class="code-inline">${esc(preview.query)}</code></div>
      <div id="preview-stale" class="notice warn" role="status" style="margin-top:12px" ${stale ? '' : 'hidden'}>
        <span class="n-ico">Notice:</span>
        <div><strong>This preview is out of date.</strong> The query or a filter changed after it ran,
        so the full harvest is disabled until you preview again.</div></div>
      ${preview.results.length
        ? `<ol class="pv-list">${rows}</ol>`
        : empty('', 'No results found', 'Edit the query or relax the filters, then preview again.')}
    </div></div>`;
}

/* Editing the search after a harvest returns the page to its pre-harvest state.
 *
 * Toggled rather than re-rendered: rebuilding the form on every keystroke would take
 * the focus out of the field being typed into. The finished run is not forgotten, so
 * typing the original search back restores the panel rather than losing the result.
 */
function syncPostHarvest() {
  const done = harvestComplete();
  const card = $('#harvest-done');
  if (card && searchState.harvested) card.hidden = !done;
  const warning = $('#duplicate-warning');
  if (warning && searchState.duplicates) {
    warning.hidden = searchState.duplicates.key !== currentHarvestKey();
  }
  const preview = $('#assisted-preview');
  if (preview) preview.hidden = !!done;
  const start = $('#start-harvest');
  if (start) {
    start.classList.toggle('primary', !done);
    start.textContent = done ? 'Run this harvest again' : 'Start harvest';
  }
}

/* Cheap refresh of just the things staleness controls, so typing never re-renders
   the form and never steals focus. */
function syncControls() {
  syncPostHarvest();
  const button = $('#start-full-harvest');
  const banner = $('#preview-stale');
  if (!button || !banner) return;   // conventional mode has neither
  const stale = !previewIsCurrent();
  const empty_ = !searchState.preview || !searchState.preview.results.length;
  button.disabled = stale || empty_ || !!searchState.busy;
  banner.hidden = !stale;
}

function harvestPayload(dryRun) {
  const f = searchState.filters;
  const number = (v) => (String(v).trim() ? Number(v) : null);
  const payload = {
    search: activeQuery() || null,
    topic_id: f.topic_id.trim() || null,
    limit: number(f.limit),
    from_year: number(f.from_year),
    to_year: number(f.to_year),
    oa_status: f.oa_status || null,
    primary_topic_only: !!f.primary_topic_only,
    languages: [...f.languages],
    affiliation_countries: [...f.affiliation_countries],
    xml_policy: f.xml_policy || 'preferred',
    europe_pmc: !!f.europe_pmc,
    unpaywall: !!f.unpaywall,
    dry_run: dryRun,
    search_mode: searchState.mode,
  };
  if (searchState.mode === 'assisted') {
    payload.research_question = searchState.researchQuestion.trim();
    payload.generated_query = searchState.generatedQuery;
    payload.preview_fingerprint = searchState.preview ? searchState.preview.fingerprint : null;
    if (searchState.advice) {
      payload.advisor = {
        provider: searchState.advice.provider,
        model: searchState.advice.model,
        prompt_version: searchState.advice.prompt_version,
      };
    }
  }
  return payload;
}

/* The discovery-only preview request. Filters are the same shared model both modes
   use, so preview and harvest can never disagree about them. */
function previewPayload() {
  const payload = harvestPayload(false);
  return {
    search: payload.search,
    topic_id: payload.topic_id,
    primary_topic_only: payload.primary_topic_only,
    from_year: payload.from_year,
    to_year: payload.to_year,
    oa_status: payload.oa_status,
    languages: payload.languages,
    affiliation_countries: payload.affiliation_countries,
    limit: 10,
  };
}

async function showPreview(runId) {
  const area = $('#preview-area');
  if (!area) return;
  const detail = await api(`/api/runs/${encodeURIComponent(runId)}`);
  // Remember the query so "Start full harvest" still works after a reload, when the
  // in-memory form payload is gone.
  lastPreview = { runId, query: detail.query || {}, limit: detail.record_limit };
  const docs = detail.documents || [];
  area.innerHTML = `<div class="card">
    <div class="card-head">
      <div><h2>Search preview — ${num(detail.document_total)} record(s) found</h2>
        <div class="sub">These are the search results, not harvest results: nothing has been
        downloaded yet. A full harvest would try to acquire the full text of each one.</div></div>
      <button class="btn primary" onclick="actions.promotePreview()">Start full harvest</button>
    </div>
    <div class="card-body tight">${docs.length ? `
      <div class="table-wrap"><table>
        <thead><tr><th>Title</th><th>Year</th><th>DOI</th><th>Status</th></tr></thead>
        <tbody>${docs.slice(0, 200).map((d) => `<tr>
          <td><div class="cell-title truncate">${esc(d.title || '(untitled)')}</div></td>
          <td class="num">${esc(d.publication_year || '—')}</td>
          <td class="mono truncate" style="max-width:240px">${esc(d.doi || '—')}</td>
          <td>${docPill(d.status)}</td>
        </tr>`).join('')}</tbody></table></div>`
      : empty('', 'No documents matched', 'Try broader search terms or remove the year filter.')}
    </div></div>`;
  area.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'nearest' });
}

/* Preserves the chain "what did I want to know?" -> "what did the system run?"
   (specification section 33). Runs recorded before Assisted Search existed have no
   provenance; they are shown as what they were rather than guessed at. */
function searchProvenanceCard(provenance) {
  const p = provenance || {};
  const assisted = p.search_mode === 'assisted';
  const rows = [
    ['Search mode', assisted ? 'Assisted Search' : 'Conventional Search'],
  ];
  if (assisted) {
    rows.push(['Research question', p.research_question || '—']);
    rows.push(['Recommended query', p.generated_query || '—']);
  }
  rows.push(['Effective query executed', p.effective_search_query || '—']);
  const advisor = p.query_advisor || null;
  return `<div class="card"><div class="card-head"><div><h2>Search</h2>
      <div class="sub">${provenance ? 'How this run&rsquo;s query was constructed.'
        : 'Recorded before search modes existed; treated as a conventional search.'}</div></div></div>
    <div class="card-body"><dl class="kv">
      ${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}
      ${assisted && p.query_edited ? '<dt>Edited</dt><dd>The recommended query was edited before running.</dd>' : ''}
      ${advisor ? `<dt>Advisor</dt><dd>${esc(advisor.provider || 'unknown')} · ${esc(advisor.model || 'unknown model')}
        <span class="hint">prompt ${esc(advisor.prompt_version || 'unversioned')}</span></dd>` : ''}
    </dl></div></div>`;
}

/* ---------------------------------------------------------------- runs */

/* The run list's first column leads with what was searched for, not the machine-
 * generated run ID: a repeated search reads as the same phrase in every row, and the
 * ID it used to lead with told a human nothing a timestamp couldn't tell better
 * (usability finding A3). The ID itself still has a home — the run's own detail page
 * already shows it in the page title — so it is not reproduced here at all, and with
 * it goes the per-row Copy button that used to compete with the query for space.
 *
 * The compact variant (the Dashboard's "Recent runs" card, a narrow column on a wide
 * screen) drops straight to three columns — Run, Result, Status — instead of merely
 * hiding two of the full table's numeric columns: that older compact table still ran
 * out of room and cut its rightmost column off mid-word (usability finding A2). The
 * full table on /runs has the room to keep every counter. */
function runsTable(runs, compact = false) {
  if (!runs.length) return empty('', 'No runs yet', 'Start a harvest to create one.');
  // A preview downloads nothing. Its documents may later be completed by a real
  // harvest, so showing per-document counts on a preview row would credit the
  // preview with work it never did.
  const dash = '<span style="color:var(--text-3)" title="A preview does not acquire anything">—</span>';
  return `<div class="table-wrap"><table>
    <thead><tr>
      <th>Run</th><th>Status</th>${compact ? '<th>Result</th>' : `<th class="num">Found</th>
      <th class="num" title="Documents from this run whose full text is now in the corpus">Full text</th>
      <th class="num" title="Documents holding full text in XML only: XML available, PDF unavailable">Partial</th>
      <th class="num" title="Documents from this run whose full text could not be acquired">Unavailable</th>
      <th class="num">PDFs</th><th class="num">Duration</th>`}
    </tr></thead>
    <tbody>${runs.map((r) => {
      // Read through the same accessor the run page uses, so the two never disagree
      // about what "unavailable" counts. A skipped document is not acquired either.
      const state = corpusStateOf(r);
      const missing = (state.unavailable || 0) + (state.retryable || 0);
      const label = esc((r.query && (r.query.search || r.query.topic_id)) || 'harvest') +
        (r.dry_run ? ' · search preview' : '');
      const result = r.dry_run
        ? `${num(r.discovery_seen)} record(s) found`
        : `${num(state.full_text_available)} of ${num(state.total)} full text`;
      return `<tr class="clickable" onclick="location.hash='#/runs/${encodeURIComponent(r.run_id)}'">
      <td><div class="cell-title">${label}</div>
        <div class="cell-sub" title="${esc(exact(r.started_at))}">${esc(when(r.started_at))}</div></td>
      <td>${runPill(r.status)}</td>
      ${compact ? `<td>${result}</td>` : `
      <td class="num">${num(r.discovery_seen)}</td>
      <td class="num">${r.dry_run ? dash : num(state.full_text_available)}</td>
      <td class="num">${r.dry_run ? dash : (state.partial_full_text
        ? `<span style="color:var(--warn)">${num(state.partial_full_text)}</span>`
        : num(state.partial_full_text === undefined ? null : 0))}</td>
      <td class="num">${r.dry_run ? dash : (missing ? `<span style="color:var(--bad)">${num(missing)}</span>` : '0')}</td>
      <td class="num">${r.dry_run ? dash : num(r.artifacts.pdf || 0)}</td><td class="num">${esc(duration(r.duration_seconds))}</td>`}
    </tr>`; }).join('')}</tbody></table></div>`;
}

async function viewRuns() {
  const data = await api('/api/runs?limit=100');
  setHeader('Runs', `${data.runs.length} run(s) recorded`,
    '<a class="btn primary" href="#/harvest">New harvest</a>');
  view().innerHTML = `<div class="card"><div class="card-body tight">${runsTable(data.runs)}</div></div>`;
}

/* The resulting corpus state for one run, whether or not the API worked it out.
 *
 * The server's own `outcome` block is authoritative when present. When it is absent —
 * an older API — the same figures are reconstructed from the per-document counts that
 * response does carry, using the shared grouping so both routes agree exactly.
 */
function corpusStateOf(r) {
  const given = r.outcome && r.outcome.corpus_state;
  if (given) return given;
  return groupCounts(r.document_counts);
}

/* The work one run performed, from whichever record of it survives.
 *
 * Preference order: the API's own `this_run` block, then the run's stored counters
 * under their older names. Fields are copied across without defaulting, so a counter
 * an old run never recorded renders as "—" rather than as a zero that would read as
 * "this run did none of that". Returns null only when there are no counters at all.
 */
function runWorkOf(r) {
  const given = r.outcome && r.outcome.this_run;
  if (given) return { work: given, legacy: false };
  const stats = r.stats;
  if (!stats || typeof stats !== 'object') return { work: null, legacy: false };
  return {
    legacy: true,
    work: {
      newly_acquired: stats.completed,
      already_available: stats.already_complete,
      attempted: stats.attempted,
      downloaded: stats.downloaded,
      bytes_downloaded: stats.bytes_downloaded,
      retries_attempted: stats.retry_count,
      failed_permanent: stats.failed_permanent,
      failed_retryable: stats.failed_retryable,
      discovered: stats.records_discovered,
      duplicates: stats.duplicates,
    },
  };
}

/* A byte count, or "—" when the run never recorded one. `bytes(undefined)` says
   "0 B", which is a claim this cannot make on a legacy run's behalf. */
function bytesOrDash(value) {
  return value === undefined || value === null ? '—' : bytes(value);
}

/* Two questions, answered separately (UX item 6).
 *
 * A repeated run reported "10 completed, 15 failed" in the interface while its own
 * technical report said `completed: 0, already_complete: 10, failed_retryable: 2`.
 * Both were correct: the first pair described the documents as they now stand, the
 * second described the work this particular run performed. Printed as one row of
 * numbers they read as a contradiction, so they are two blocks here, each under the
 * question it answers. Every figure comes from the run record or from the documents —
 * nothing is derived to fill a gap, and a run that recorded no counters says so
 * rather than showing zeros it cannot vouch for.
 */
function runOutcomeCards(r) {
  const state = corpusStateOf(r);
  const found = runWorkOf(r);
  const work = found.work;
  if (r.dry_run) {
    return `<div class="card">
      <div class="card-head"><div><h2>What this search found</h2>
        <div class="sub">A search preview acquires nothing, so there is no acquisition
          outcome to report.</div></div></div>
      <div class="card-body">${statCells([
        ['Records found', num(r.discovery_seen)],
        ['Pages read', num(r.discovery_pages)],
      ])}</div></div>`;
  }
  return `<div class="grid grid-2">
    <div class="card">
      <div class="card-head"><div><h2>Full text in the corpus</h2>
        <div class="sub">Where the ${num(state.total)} document(s) this run covered stand
          <em>now</em>. Read live, so a later retry moves these numbers.</div></div></div>
      <div class="card-body">${statCells([
        ['Full text available', num(state.full_text_available), 'ok'],
        // Left out rather than shown as a zero when the API did not send it: an older
        // response cannot tell a partial result from an empty one, and inventing a
        // number for it would be worse than leaving the question unasked.
        ...(state.partial_full_text === undefined ? [] : [
          ['Partial full text', num(state.partial_full_text),
            state.partial_full_text ? 'warn' : ''],
        ]),
        ['Unavailable', num(state.unavailable), state.unavailable ? 'bad' : ''],
        ['Can be retried', num(state.retryable), state.retryable ? 'warn' : ''],
        ['Still to acquire', num(state.in_progress)],
      ])}
      <div class="hint" style="margin-top:10px">${num(r.artifacts.pdf || 0)} PDF and
        ${num(r.artifacts.xml || 0)} XML file(s) stored for these documents.${
          state.partial_full_text ? ` ${num(state.partial_full_text)} of them hold full
          text in XML only — ${PARTIAL_EXPLANATION} — which is why they are not counted
          as available.` : ''}</div>
      </div></div>
    <div class="card">
      <div class="card-head"><div><h2>What this run did</h2>
        <div class="sub">The work performed while this run was executing. Fixed when the
          run finished; it does not change afterwards.</div></div></div>
      <div class="card-body">${work ? `${statCells([
        ['Newly acquired', num(work.newly_acquired), work.newly_acquired ? 'ok' : ''],
        ['Already available', num(work.already_available)],
        ['Retries attempted', num(work.retries_attempted), work.retries_attempted ? 'warn' : ''],
        ['Downloaded', esc(bytesOrDash(work.bytes_downloaded))],
      ])}
      ${found.legacy ? `<div class="hint" style="margin-top:10px">Read from this run's own
        stored counters. Anything it never recorded is shown as &ldquo;—&rdquo; rather
        than as a zero.</div>` : ''}
      <div class="hint" style="margin-top:10px">${runWorkNote(work)}</div>`
        : `<div class="hint">${r.finished_at
          ? 'This run stored no counters, so what it did cannot be reported. The resulting corpus state beside it is read from the documents themselves and is unaffected.'
          : 'This run has not finished, so its counters have not been written yet.'}</div>`}
      </div></div>
  </div>`;
}

/* The sentence under the work counters, written only about numbers that exist. */
function runWorkNote(work) {
  const known = (value) => typeof value === 'number';
  const parts = [];
  if (known(work.discovered)) {
    parts.push(`${num(work.discovered)} record(s) found by the search${
      known(work.duplicates) ? `, ${num(work.duplicates)} of them already known` : ''}.`);
  }
  if (known(work.failed_permanent) && known(work.failed_retryable)) {
    const failed = work.failed_permanent + work.failed_retryable;
    parts.push(failed
      ? `${num(failed)} acquisition(s) failed during this run.`
      : 'No acquisition failed during this run.');
  }
  return parts.length ? parts.join(' ') : 'No further counters were recorded.';
}

async function viewRunDetail(runId) {
  const r = await api(`/api/runs/${encodeURIComponent(runId)}`);
  const actionsHtml = [
    r.resumable ? `<button class="btn primary" onclick="actions.resume('${esc(r.run_id)}')">Resume run</button>` : '',
    r.failed ? `<a class="btn" href="#/failures">Retry failed (${num(r.failed)})</a>` : '',
    r.report_path ? `<a class="btn" title="The raw machine-readable run record: every counter, every recorded failure. Kept for audit and debugging."
      href="/api/runs/${encodeURIComponent(r.run_id)}/report" target="_blank">Technical report (JSON)</a>` : '',
    '<button class="btn" onclick="actions.verify(false)">Verify corpus</button>',
  ].filter(Boolean).join('');
  setHeader(`Run ${r.run_id.replace(/^run-/, '')}`,
    r.dry_run ? 'Search preview — nothing was downloaded' : 'Full harvest', actionsHtml);

  const c = r.document_counts || {};
  // The run row is a finished historical record; the per-document counts are read live
  // from the documents themselves. A document re-queued after the run ended therefore
  // shows up as "queued" under a completed run — explained in the notice below.
  const queued = (c.QUEUED || 0) + (c.NORMALIZED || 0) + (c.DISCOVERED || 0);

  view().innerHTML = `
    ${r.dry_run ? `<div class="notice info" role="status"><span class="n-ico">Note:</span>
      <div><strong>This was a search preview.</strong> Nothing was downloaded by this run. The
      document states below are their <em>current</em> states — they may since have been
      acquired by a real harvest.</div></div>` : ''}
    ${!r.dry_run && r.status === 'COMPLETED' && queued ? `<div class="notice info" role="status"><span class="n-ico">Note:</span>
      <div><strong>This run is finished.</strong> ${num(queued)} document(s) from it have since been
      re-queued for a future harvest. The run's own result is unchanged — the counts below show each
      document's <em>current</em> state.</div></div>` : ''}
    ${r.suspend_reason ? `<div class="notice warn" role="status"><span class="n-ico">Notice:</span>
      <div><strong>This run was paused.</strong><div>${esc(r.suspend_reason)}</div>
      <div class="n-action">Resume it once the provider budget resets — completed documents will not be downloaded again.</div></div></div>` : ''}

    ${runOutcomeCards(r)}

    <div class="split">
      <div style="display:flex;flex-direction:column;gap:18px">
        <div class="card">
          <div class="card-head"><h2>Documents</h2><span class="chip">${num(r.document_total)} total</span></div>
          <div class="card-body tight">${r.documents.length ? `
            ${docStatusSummary(r.documents)}
            <div class="table-wrap"><table>
              <thead><tr><th>Title</th><th class="doc-status-col">Status</th><th>Artifacts</th><th class="num">Size</th></tr></thead>
              <tbody>${r.documents.map((d) => `<tr class="clickable" onclick="actions.openDocument('${esc(d.document_id)}')">
                <td><div class="cell-title truncate">${esc(d.title || '(untitled)')}</div>
                    <div class="cell-sub mono">${esc(d.doi || d.document_id)}</div></td>
                <td>${docPill(d.status, { pdf: d.has_pdf, xml: d.has_xml })}</td>
                <td>${d.has_pdf ? '<span class="chip">PDF</span> ' : ''}${d.has_xml ? '<span class="chip">XML</span>' : ''}${
                  !d.has_pdf && !d.has_xml ? '<span style="color:var(--text-3)">none</span>' : ''}</td>
                <td class="num">${d.size_bytes ? esc(bytes(d.size_bytes)) : '—'}</td>
              </tr>`).join('')}</tbody></table></div>` : empty('', 'No documents recorded for this run')}
          </div></div>

        ${r.failures.length ? `<div class="card">
          <div class="card-head"><h2>Failures</h2>${pill(`${r.failures.length} recorded`, 'bad')}</div>
          <div class="card-body tight"><div class="table-wrap"><table>
            <thead><tr><th>Reason</th><th>Document</th><th>Message</th><th>When</th></tr></thead>
            <tbody>${r.failures.slice(0, 100).map((f) => `<tr>
              <td>${pill(f.category, f.retryable ? 'warn' : 'bad')}</td>
              <td class="mono truncate" style="max-width:190px">${esc(f.document_id || '—')}</td>
              <td class="truncate" style="max-width:340px" title="${esc(f.message)}">${esc(f.message)}</td>
              <td>${esc(when(f.ts))}</td></tr>`).join('')}</tbody></table></div></div></div>` : ''}
      </div>

      <div style="display:flex;flex-direction:column;gap:18px">
        <div class="card"><div class="card-head"><h2>Run information</h2></div>
          <div class="card-body"><dl class="kv">
            <dt>Status</dt><dd>${runPill(r.status)}</dd>
            <dt>Started</dt><dd>${esc(exact(r.started_at))}</dd>
            <dt>Finished</dt><dd>${esc(exact(r.finished_at))}</dd>
            <dt>Duration</dt><dd>${esc(duration(r.duration_seconds))}</dd>
            <dt>Mode</dt><dd>${r.dry_run ? 'Search preview — nothing downloaded' : 'Full harvest'}</dd>
            <dt>Limit</dt><dd>${r.record_limit ? num(r.record_limit) : 'none'}</dd>
            <dt>Providers</dt><dd>${r.providers_used.length ? r.providers_used.map((p) => `<span class="chip">${esc(p)}</span>`).join(' ') : '—'}</dd>
            <dt>Downloaded</dt><dd>${esc(bytes(r.bytes_downloaded || 0))}</dd>
            <dt>Retries</dt><dd>${num(r.retry_count || 0)}</dd>
            <dt>Storage root</dt><dd class="mono">${esc(r.storage_root)}</dd>
          </dl></div></div>

        ${searchProvenanceCard(r.search_provenance)}

        <div class="card"><div class="card-head"><h2>Query</h2></div>
          <div class="card-body"><pre class="code">${esc(JSON.stringify(r.query, null, 2))}</pre></div></div>

        ${r.failure_groups.length ? `<div class="card"><div class="card-head"><div><h2>Failure reasons</h2>
          <div class="sub">Failures recorded while this run was executing.</div></div></div>
          <div class="card-body">${r.failure_groups.map((g) => `
            <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--grey)">
              <span>${pill(g.category, 'bad')}</span>
              <span class="mono">${num(g.documents)} doc(s)</span></div>`).join('')}</div></div>` : ''}
      </div>
    </div>`;
}

/* --------------------------------------------------------------- corpus */

const corpusState = { page: 1, filters: {} };

async function viewCorpus() {
  const facets = await api('/api/corpus/facets');
  setHeader('Corpus', 'Every document the corpus has recorded, searchable and filterable — including ones a harvest could not acquire.');

  view().innerHTML = `
    <div class="card"><div class="card-body">
      <div class="filters">
        <div class="field grow"><label for="c-search">Search</label>
          <input id="c-search" type="search" placeholder="Title, DOI or journal" value="${esc(corpusState.filters.search || '')}"></div>
        <div class="field small"><label for="c-oa">OA status</label><select id="c-oa">
          <option value="">any</option>${facets.oa_status.map((s) => `<option ${corpusState.filters.oa_status === s ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select></div>
        <div class="field small"><label for="c-year">Year</label><select id="c-year">
          <option value="">any</option>${facets.year.map((y) => `<option ${String(corpusState.filters.year) === String(y) ? 'selected' : ''}>${esc(y)}</option>`).join('')}</select></div>
        <div class="field small"><label for="c-pdf">PDF</label><select id="c-pdf">
          <option value="">any</option><option value="true">has PDF</option><option value="false">no PDF</option></select></div>
        <div class="field small"><label for="c-xml">XML</label><select id="c-xml">
          <option value="">any</option><option value="true">has XML</option><option value="false">no XML</option></select></div>
        <div class="field grow"><label for="c-journal">Journal</label>
          <input id="c-journal" type="text" list="journal-list" placeholder="any" value="${esc(corpusState.filters.journal || '')}">
          <datalist id="journal-list">${facets.journal.map((j) => `<option value="${esc(j)}"></option>`).join('')}</datalist></div>
        <div class="field small"><label>&nbsp;</label>
          <div style="display:flex;gap:6px">
            <button class="btn primary" onclick="actions.applyCorpusFilters()">Apply</button>
            <button class="btn" onclick="actions.clearCorpusFilters()">Clear</button></div></div>
      </div>
    </div></div>
    <div class="card"><div id="corpus-results" class="card-body tight"><div class="loading" role="status" style="padding:24px">Loading…</div></div></div>`;

  ['c-search', 'c-journal'].forEach((id) => {
    const el = $(`#${id}`);
    if (el) el.addEventListener('keydown', (e) => { if (e.key === 'Enter') actions.applyCorpusFilters(); });
  });
  await loadCorpus();
}

async function loadCorpus() {
  const params = new URLSearchParams({ page: corpusState.page, page_size: 50 });
  Object.entries(corpusState.filters).forEach(([k, v]) => {
    if (v !== '' && v !== null && v !== undefined) params.set(k, v);
  });
  const data = await api(`/api/corpus?${params}`);
  const box = $('#corpus-results');
  if (!box) return;

  if (!data.documents.length) {
    box.innerHTML = empty('', 'No documents match',
      data.total === 0 ? 'The corpus is empty — run a harvest to fill it.' : 'Try relaxing the filters.');
    return;
  }
  box.innerHTML = `
    <div class="table-wrap"><table>
      <thead><tr><th>Title</th><th>Journal</th><th class="num">Year</th><th>OA</th><th>Artifacts</th><th class="doc-status-col">Status</th></tr></thead>
      <tbody>${data.documents.map((d) => `<tr class="clickable" onclick="actions.openDocument('${esc(d.document_id)}')">
        <td><div class="cell-title truncate">${esc(d.title || '(untitled)')}</div>
            <div class="cell-sub mono truncate">${esc(d.doi || d.document_id)}</div></td>
        <td class="truncate" style="max-width:190px">${esc(d.journal || '—')}</td>
        <td class="num">${esc(d.publication_year || '—')}</td>
        <td>${d.oa_status ? pill(d.oa_status, 'neutral') : '—'}</td>
        <td>${d.has_pdf ? '<span class="chip">PDF</span> ' : ''}${d.has_xml ? '<span class="chip">XML</span>' : ''}${
          !d.has_pdf && !d.has_xml ? '<span style="color:var(--text-3)">none</span>' : ''}</td>
        <td>${docPill(d.status, { pdf: d.has_pdf, xml: d.has_xml })}</td>
      </tr>`).join('')}</tbody></table></div>
    <div class="pager">
      <span>${num(data.total)} document(s) · page ${data.page} of ${data.pages}</span>
      <span style="display:flex;gap:6px">
        <button class="btn small" ${data.page <= 1 ? 'disabled' : ''} onclick="actions.corpusPage(${data.page - 1})">Previous</button>
        <button class="btn small" ${data.page >= data.pages ? 'disabled' : ''} onclick="actions.corpusPage(${data.page + 1})">Next</button>
      </span></div>`;
}

async function openDocumentPanel(documentId) {
  const d = await api(`/api/corpus/${encodeURIComponent(documentId)}`);
  const m = d.metadata || {};
  const pdf = d.artifacts.pdf, xml = d.artifacts.xml;
  const prov = d.provenance || {};
  const fileUrl = (kind) => `/api/corpus/${encodeURIComponent(d.document_id)}/file/${kind}`;

  const artifactBlock = (kind, artifact) => !artifact ? '' : `
    <div class="provenance" style="margin-bottom:16px"><div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:9px">
        <strong>${kind.toUpperCase()}</strong>
        <a class="btn small" href="${fileUrl(kind)}" target="_blank" rel="noopener">Open ${kind.toUpperCase()}</a></div>
      <dl class="kv" style="grid-template-columns:120px 1fr;font-size:14px">
        <dt>Size</dt><dd>${esc(bytes(artifact.size_bytes))}</dd>
        <dt>SHA-256</dt><dd class="mono" style="font-size:14px">${hashValue(artifact.sha256)}</dd>
        <dt>Retrieved</dt><dd>${esc(exact(artifact.retrieved_at))}</dd>
        <dt>Source</dt><dd>${esc(artifact.source)}</dd>
        <dt>From</dt><dd class="mono" style="font-size:14px;word-break:break-all">${esc(artifact.resolved_url)}</dd>
      </dl></div></div>`;

  openPanel(m.title || d.document_id, d.document_id, `
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px">
      ${docPill(d.status, { pdf: !!pdf, xml: !!xml })}
      ${!pdf && xml ? pill(PARTIAL_EXPLANATION, 'warn') : ''}
      ${m.oa_status ? pill(`OA: ${m.oa_status}`, 'ok') : ''}
      ${pdf ? `<a class="btn small primary" href="${fileUrl('pdf')}" target="_blank" rel="noopener">Open PDF</a>` : ''}
      ${xml ? `<a class="btn small" href="${fileUrl('xml')}" target="_blank" rel="noopener">Open XML</a>` : ''}
      <a class="btn small" href="${fileUrl('json')}" target="_blank" rel="noopener">Metadata JSON</a>
    </div>

    <div class="section-title">Bibliographic</div>
    <dl class="kv">
      <dt>Title</dt><dd>${esc(m.title || '—')}</dd>
      <dt>Authors</dt><dd>${(m.authors || []).length ? esc((m.authors || []).join(', ')) : '—'}</dd>
      <dt>Journal</dt><dd>${esc(m.journal || '—')}</dd>
      <dt>Year</dt><dd>${esc(m.publication_year || '—')}</dd>
      <dt>DOI</dt><dd class="mono">${m.doi ? `<a href="https://doi.org/${esc(m.doi)}" target="_blank" rel="noopener">${esc(m.doi)}</a>` : '—'}</dd>
      <dt>OA status</dt><dd>${esc(m.oa_status || 'not reported')}${m.oa_status_source ? ` <span class="chip">via ${esc(m.oa_status_source)}</span>` : ''}</dd>
      <dt>Subjects</dt><dd>${(m.domain_tags || []).length ? `<div class="chips">${(m.domain_tags || []).map((t) => `<span class="chip">${esc(t)}</span>`).join('')}</div>` : '—'}</dd>
      <dt>Abstract</dt><dd>${m.abstract ? esc(m.abstract) : '<em style="color:var(--text-3)">not supplied by any provider</em>'}</dd>
      <dt>First seen</dt><dd>${esc(exact(d.first_seen))}</dd>
      <dt>Last updated</dt><dd>${esc(exact(d.last_updated))}</dd>
    </dl>

    <div class="section-title">Artifacts</div>
    ${pdf || xml ? artifactBlock('pdf', pdf) + artifactBlock('xml', xml)
      : `<div class="notice warn" role="status"><span class="n-ico">Notice:</span><div>No artifact was stored for this document.</div></div>`}

    <div class="section-title">Provenance</div>
    <div class="provenance source-link"><dl class="kv">
      <dt>Discovered via</dt><dd>${(prov.discovered_via || m.discovered_via || []).map((s) => `<span class="chip">${esc(s)}</span>`).join(' ') || '—'}</dd>
      <dt>Cross-checked via</dt><dd>${(prov.cross_checked_via || []).map((s) => `<span class="chip">${esc(s)}</span>`).join(' ') || '—'}</dd>
      <dt>Acquired via</dt><dd>${esc(prov.acquired_via || '—')}</dd>
      <dt>Resolved URL</dt><dd class="mono" style="font-size:14px;word-break:break-all">${esc(prov.resolved_url || '—')}</dd>
      <dt>HTTP status</dt><dd>${esc(prov.http_status || '—')}</dd>
    </dl></div>

    ${(d.cross_checks || []).length ? `<div class="section-title">Cross-check evidence</div>
      ${d.cross_checks.map((c) => `<div class="card" style="margin-bottom:9px"><div class="card-body">
        <div style="display:flex;justify-content:space-between;margin-bottom:7px">
          <strong>${esc(c.source)}</strong>${pill(c.matched_on && c.matched_on.length ? `matched on ${c.matched_on.join(', ')}` : 'no identifier match',
            c.matched_on && c.matched_on.length ? 'ok' : 'neutral')}</div>
        ${Object.keys(c.metadata_differences || {}).length ? `
          <div style="font-size:14px;color:var(--warn);margin-bottom:6px">Providers disagree — both values are preserved:</div>
          <dl class="kv" style="grid-template-columns:120px 1fr;font-size:14px">
            ${Object.entries(c.metadata_differences).map(([field, vals]) => `
              <dt>${esc(field)}</dt><dd>${Object.entries(vals).map(([src, v]) => `<div><span class="chip">${esc(src)}</span> ${esc(v)}</div>`).join('')}</dd>`).join('')}
          </dl>` : '<div style="font-size:14px;color:var(--text-3)">No metadata disagreement.</div>'}
      </div></div>`).join('')}` : ''}

    ${(d.failures || []).length ? `<div class="section-title">Failures</div>
      ${d.failures.slice(0, 10).map((f) => `<div class="notice bad" role="alert" style="margin-bottom:7px">
        <span class="n-ico">Notice:</span><div><strong>${esc(f.category)}</strong><div>${esc(f.message)}</div>
        <div class="n-action">${esc(when(f.ts))}${f.url ? ` · ${esc(f.url)}` : ''}</div></div></div>`).join('')}` : ''}

    <div class="section-title">Sidecar file</div>
    <pre class="code">${esc(JSON.stringify(d.sidecar || { note: 'No sidecar written yet.' }, null, 2))}</pre>`);
}

/* --------------------------------------------------------------- verify */

async function viewVerify() {
  const data = await api('/api/verify/history');
  setHeader('Verify', 'Check that every recorded artifact still exists and still matches its checksum.',
    `<button class="btn" onclick="actions.verify(false)">Run verify</button>
     <button class="btn primary" onclick="actions.verify(true)">Deep verify</button>`);

  const latest = data.latest;
  const report = latest ? latest.report : null;

  view().innerHTML = `
    <div class="notice info" role="status"><span class="n-ico">Note:</span>
      <div><strong>Verify</strong> compares the state database against the files on disk: missing artifacts,
      checksum mismatches, missing sidecars and files nothing accounts for.
      <strong>Deep verify</strong> additionally re-parses every PDF and XML, which is slower but catches corruption.</div></div>

    ${!latest ? `<div class="card"><div class="card-body">${
      empty('', 'No verification has been run yet', 'Run a verification to establish a baseline.')}</div></div>` : `
      <div class="grid grid-4">
        <div class="card stat"><div class="stat-label">Result</div>
          <div class="stat-value ${latest.ok ? 'ok' : 'bad'}" style="font-size:22px">${latest.ok ? 'Healthy' : 'Problems'}</div>
          <div class="stat-foot">${esc(when(latest.checked_at))}${latest.deep ? ' · deep' : ''}</div></div>
        <div class="card stat"><div class="stat-label">Artifacts checked</div>
          <div class="stat-value" style="font-size:22px">${num(report.artifacts_checked)}</div></div>
        <div class="card stat"><div class="stat-label">Problems</div>
          <div class="stat-value ${report.problem_count ? 'bad' : 'ok'}" style="font-size:22px">${num(report.problem_count)}</div></div>
        <div class="card stat"><div class="stat-label">Orphan files</div>
          <div class="stat-value ${(report.orphans || []).length ? 'warn' : 'ok'}" style="font-size:22px">${num((report.orphans || []).length)}</div>
          <div class="stat-foot">${num((report.temporary_files || []).length)} temporary</div></div>
      </div>

      <div class="split">
        <div style="display:flex;flex-direction:column;gap:18px">
          ${(report.problems || []).length ? `<div class="card">
            <div class="card-head"><h2>Problems</h2>${pill(`${report.problems.length}`, 'bad')}</div>
            <div class="card-body tight"><div class="table-wrap"><table>
              <thead><tr><th>Document</th><th>Kind</th><th>Problem</th></tr></thead>
              <tbody>${report.problems.map((p) => `<tr class="clickable" onclick="actions.openDocument('${esc(p.document_id)}')">
                <td class="mono truncate" style="max-width:230px">${esc(p.document_id)}</td>
                <td>${pill(p.kind || '—', 'neutral')}</td>
                <td>${esc(p.problem)}</td></tr>`).join('')}</tbody></table></div></div></div>`
            : `<div class="card"><div class="card-body"><div class="notice ok" role="status"><span class="n-ico">Completed:</span>
                <div>No problems found. Every recorded artifact exists and matches its stored SHA-256.</div></div></div></div>`}

          ${(report.orphans || []).length ? `<div class="card">
            <div class="card-head"><h2>Orphan files</h2>${pill(`${report.orphans.length}`, 'warn')}</div>
            <div class="card-body"><p style="color:var(--text-2);font-size:15px;margin-bottom:10px">
              Files in the corpus directory that no state record accounts for. They are never deleted automatically.</p>
              <div class="chips">${report.orphans.map((o) => `<span class="chip mono">${esc(o)}</span>`).join('')}</div></div></div>` : ''}

          ${(report.temporary_files || []).length ? `<div class="card">
            <div class="card-head"><h2>Temporary files</h2>${pill(`${report.temporary_files.length}`, 'neutral')}</div>
            <div class="card-body"><p style="color:var(--text-2);font-size:15px;margin-bottom:10px">
              Abandoned partial downloads. They hold no validated content and the next harvest removes them once stale.</p>
              <div class="chips">${report.temporary_files.map((t) => `<span class="chip mono">${esc(t)}</span>`).join('')}</div></div></div>` : ''}
        </div>

        <div class="card"><div class="card-head"><h2>History</h2></div>
          <div class="card-body tight"><div class="table-wrap"><table>
            <thead><tr><th>When</th><th>Mode</th><th>Result</th><th class="num">Problems</th></tr></thead>
            <tbody>${data.history.map((h) => `<tr>
              <td title="${esc(exact(h.checked_at))}">${esc(when(h.checked_at))}</td>
              <td>${h.deep ? 'deep' : 'standard'}</td>
              <td>${pill(h.ok ? 'ok' : 'problems', h.ok ? 'ok' : 'bad')}</td>
              <td class="num">${num(h.problem_count)}</td></tr>`).join('')}</tbody></table></div></div></div>
      </div>`}`;
}

/* -------------------------------------------------------------- failures */

/* "Re-queue everything" needs a confirmation step (usability finding B5) — it acts on
 * every currently-failing document at once and sat, until now, at the one spot on the
 * page a first-time visitor's eye lands. Kept at module scope, the same way New
 * Harvest's duplicate-run warning is, so it survives the rerender() that a single-
 * category re-queue triggers instead of being wiped by it. */
const failuresState = { confirmingAll: false };

async function viewFailures() {
  const data = await api('/api/failures');
  setHeader('Retry Failed', `${num(data.total_failed)} document(s) currently in a failed state`,
    data.total_failed ? '<button class="btn primary" onclick="actions.confirmRequeueAll()">Re-queue everything</button>' : '');
  if (!data.total_failed) failuresState.confirmingAll = false;

  // The same per-document category the left-hand table already shows, tallied once so
  // the right-hand card can lead with how many are failing *now* — the number that
  // actually explains the header above it — rather than only the historical count that
  // never reconciles with it (usability finding B4).
  const currentByCategory = {};
  data.documents.forEach((d) => {
    const cat = d.category || 'unknown';
    currentByCategory[cat] = (currentByCategory[cat] || 0) + 1;
  });
  const groups = [...data.groups].sort((a, b) =>
    (currentByCategory[b.category] || 0) - (currentByCategory[a.category] || 0));

  view().innerHTML = !data.total_failed ? `
    <div class="card"><div class="card-body">${
      empty('', 'Nothing has failed', 'Documents that could not be acquired would appear here, grouped by reason.')}</div></div>`
    : `
    ${failuresState.confirmingAll ? `
    <div class="notice warn" role="status"><span class="n-ico">Confirm:</span>
      <div><strong>Re-queue all ${num(data.total_failed)} currently failing document(s)?</strong>
      <div>They are retried by the next harvest or when you resume a run — nothing is downloaded twice, and
      permanent failures such as “no OA location” will simply fail again.</div>
      <div class="n-action" style="display:flex;gap:8px">
        <button class="btn primary" type="button" onclick="actions.requeueAllConfirmed()">Yes, re-queue everything</button>
        <button class="btn" type="button" onclick="actions.cancelRequeueAll()">Cancel</button>
      </div></div></div>` : `
    <div class="notice info" role="status"><span class="n-ico">Note:</span>
      <div>Re-queuing moves documents back into the queue. They are retried by the next harvest or when you
      resume a run — nothing is downloaded twice, and permanent failures such as “no OA location” will simply fail again.</div></div>`}

    <div class="split">
      <div class="card">
        <div class="card-head"><h2>Failed documents</h2></div>
        <div class="card-body tight"><div class="table-wrap"><table>
          <thead><tr><th>Document</th><th>Reason</th><th>Message</th><th class="num">Tries</th></tr></thead>
          <tbody>${data.documents.map((d) => `<tr class="clickable" onclick="actions.openDocument('${esc(d.document_id)}')">
            <td><div class="cell-title truncate" style="max-width:280px">${esc(d.title || d.document_id)}</div>
                <div class="cell-sub mono truncate">${esc(d.doi || '')}</div></td>
            <td>${d.category ? pill(d.category, 'bad') : '—'}</td>
            <td class="truncate" style="max-width:280px" title="${esc(d.message || '')}">${esc(d.message || '—')}</td>
            <td class="num">${num(d.attempts)}</td></tr>`).join('')}</tbody></table></div></div></div>

      <div class="card">
        <div class="card-head"><div><h2>By reason</h2>
          <div class="sub">Currently failing, by reason — plus how often each reason has been recorded in
          total. A document stays in that historical count after it has been re-queued or acquired from a
          fallback location, so it can run higher than the number currently failing.</div></div></div>
        <div class="card-body">${groups.map((g) => {
          const current = currentByCategory[g.category] || 0;
          return `
          <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 0;border-bottom:1px solid var(--grey)">
            <div style="min-width:0">
              <div>${pill(g.category, g.retryable_occurrences ? 'warn' : 'bad')}</div>
              <div style="font-size:15px;margin-top:4px">${num(current)} currently failing</div>
              <div style="font-size:14px;color:var(--text-3);margin-top:2px">
                ${num(g.occurrences)} recorded failure(s) across ${num(g.documents)} document(s) in total · last ${esc(when(g.last_seen))}</div>
            </div>
            <button class="btn small" title="Re-queues only the documents in this group that are still in a failed state."
              onclick="actions.retry({category:'${esc(g.category)}'})" ${current ? '' : 'disabled'}>Re-queue</button>
          </div>`;
        }).join('')}</div></div>
    </div>`;
}

/* -------------------------------------------------------------- settings */

const SETTING_GROUPS = [
  {
    title: 'Providers and credentials',
    hint: 'Credentials are stored in your local configuration file and are never shown again once saved.',
    keys: ['openalex.api_key', 'contact_email', 'openalex.allow_keyless',
      'openalex.enabled', 'europe_pmc.enabled', 'unpaywall.enabled'],
  },
  {
    title: 'Assisted Search',
    hint: 'Only used to turn a research question into a search query. Conventional Search and every harvest work without it.',
    keys: ['advisor.enabled', 'advisor.api_key', 'advisor.model', 'advisor.effort'],
  },
  {
    title: 'Harvesting behaviour',
    keys: ['xml_policy', 'openalex.daily_credit_ceiling', 'downloads.concurrency', 'retry.max_attempts'],
  },
  {
    title: 'Locations',
    hint: 'Where the corpus, the state database and run reports are kept.',
    keys: ['storage_root', 'state_db', 'reports_dir'],
  },
  {
    title: 'Advanced',
    advanced: true,
    keys: ['openalex.per_page', 'openalex.requests_per_second', 'europe_pmc.requests_per_second',
      'unpaywall.requests_per_second', 'downloads.max_download_size_bytes', 'downloads.min_pdf_size_bytes',
      'retry.backoff_initial_seconds', 'retry.backoff_max_seconds', 'log_level', 'log_format'],
  },
];

const SETTING_LABELS = {
  'openalex.api_key': ['OpenAlex API key', 'Required for normal harvesting. Get a free key at openalex.org/settings/api.'],
  'contact_email': ['Contact email', 'Required by Unpaywall’s terms and sent as courtesy identification.'],
  'openalex.allow_keyless': ['Allow running without an API key', 'Small daily testing allowance only — not for real harvesting.'],
  'openalex.enabled': ['OpenAlex enabled', 'Primary discovery provider.'],
  'europe_pmc.enabled': ['Europe PMC enabled', 'Cross-checking and XML full text.'],
  'unpaywall.enabled': ['Unpaywall enabled', 'Fallback OA location resolver.'],
  'advisor.enabled': ['Assisted Search enabled', 'Offers the query advisor on New Harvest. Switching it off leaves Conventional Search untouched.'],
  'advisor.api_key': ['Query advisor API key', 'Needed only for Assisted Search. Harvesting never uses it and is never blocked by it.'],
  'advisor.model': ['Query advisor model', 'Which model writes the recommended query.'],
  'advisor.effort': ['Query advisor effort', 'How much reasoning the advisor spends. The task is small, so low is usually enough. Choose "default" to ask for no particular effort — needed for models that accept no effort setting and reject a request that carries one.'],
  'xml_policy': ['XML policy', 'Whether XML full text is optional, required or skipped.'],
  'openalex.daily_credit_ceiling': ['Daily credit ceiling', 'Optional safety limit. Reaching it pauses the run cleanly so it can be resumed.'],
  'downloads.concurrency': ['Download concurrency', 'How many documents are acquired in parallel.'],
  'retry.max_attempts': ['Retry attempts', 'Attempts per request before a failure is recorded.'],
  'storage_root': ['Corpus directory', 'Where PDF, XML and JSON files are written.'],
  'state_db': ['State database', 'SQLite file holding runs, documents and failures.'],
  'reports_dir': ['Reports directory', 'Machine-readable run summaries.'],
};

async function viewSettings() {
  const data = await api('/api/settings');
  const byKey = Object.fromEntries(data.settings.map((s) => [s.key, s]));
  setHeader('Settings', `Stored in ${data.config_path}`,
    '<button class="btn primary" onclick="actions.saveSettings()">Save changes</button>');

  const renderField = (key) => {
    const s = byKey[key];
    if (!s) return '';
    const [label, hint] = SETTING_LABELS[key] || [key, ''];
    const locked = !s.editable;
    const lockNote = locked
      ? `<span class="chip">set by ${esc(s.env_var || 'environment variable')}</span>` : '';

    if (s.spec === 'secret') {
      return `<div class="field">
        <label>${esc(label)} ${s.configured ? pill('configured', 'ok') : pill('not set', 'warn')} ${lockNote}</label>
        <input type="password" data-key="${esc(key)}" placeholder="${
          s.configured ? esc(s.masked || 'unchanged — leave blank to keep') : 'not configured'}" ${locked ? 'disabled' : ''} autocomplete="off">
        <span class="hint">${esc(hint)}${s.configured ? ' Leave blank to keep the current value.' : ''}</span></div>`;
    }
    if (s.spec === 'bool') {
      return `<label class="check" style="margin-bottom:12px">
        <input type="checkbox" data-key="${esc(key)}" ${s.value ? 'checked' : ''} ${locked ? 'disabled' : ''}>
        <span><span class="ct">${esc(label)}</span><span class="cs">${esc(hint)}</span></span></label>`;
    }
    if (s.spec.startsWith('choice:')) {
      return `<div class="field"><label>${esc(label)} ${lockNote}</label>
        <select data-key="${esc(key)}" ${locked ? 'disabled' : ''}>${
          choiceOptions(s.spec, s.value)}</select>
        <span class="hint">${esc(hint)}</span></div>`;
    }
    const type = (s.spec === 'int' || s.spec === 'float' || s.spec === 'int_or_null') ? 'number' : 'text';
    return `<div class="field"><label>${esc(label)} ${lockNote}</label>
      <input type="${type}" ${s.spec === 'float' ? 'step="0.1"' : ''} data-key="${esc(key)}"
        value="${s.value === null || s.value === undefined ? '' : esc(s.value)}" ${locked ? 'disabled' : ''}
        placeholder="${s.spec === 'int_or_null' ? 'none' : ''}">
      <span class="hint">${esc(hint)}</span></div>`;
  };

  view().innerHTML = `
    <div class="notice info" role="status"><span class="n-ico">Note:</span>
      <div>These settings are written to <span class="mono">${esc(data.config_path)}</span>, the same file the
      command line reads with <span class="mono">--config</span>. Values supplied through environment variables
      win over the file and cannot be edited here.</div></div>

    <div class="grid grid-2">
      ${SETTING_GROUPS.filter((g) => !g.advanced).map((g) => `
        <div class="card"><div class="card-head"><h2>${esc(g.title)}</h2></div>
          <div class="card-body">
            ${g.hint ? `<p class="hint" style="margin-bottom:14px">${esc(g.hint)}</p>` : ''}
            ${g.keys.map(renderField).join('')}</div></div>`).join('')}
    </div>

    ${SETTING_GROUPS.filter((g) => g.advanced).map((g) => `
      <details class="advanced"><summary>${esc(g.title)} settings</summary>
        <div class="adv-body"><div class="grid grid-2" style="margin-top:10px">
          ${g.keys.map((k) => `<div>${renderField(k)}</div>`).join('')}</div></div></details>`).join('')}

    <div style="display:flex;gap:8px">
      <button class="btn primary" onclick="actions.saveSettings()">Save changes</button>
      <button class="btn" onclick="rerender()">Discard</button>
    </div>`;
}

/* ------------------------------------------------------------------ actions */

const actions = {
  async quickStart(event, dryRun = false) {
    if (event) event.preventDefault();
    const search = $('#q-search').value.trim();
    if (!search) { toast('Nothing to search for', 'Enter a search term first.', 'warn'); return; }
    const limit = Number($('#q-limit').value) || 25;
    // Seed the New Harvest form too. The operator lands there straight afterwards, and
    // a form that already holds the search they just started is what makes "refine" and
    // the finished-harvest panel work from the dashboard as well.
    searchState.mode = 'conventional';
    searchState.conventionalQuery = search;
    searchState.filters.limit = String(limit);
    await actions.launch({ search, limit, dry_run: dryRun, xml_policy: 'preferred' });
  },

  async submitHarvest(event, dryRun) {
    if (event) event.preventDefault();
    if (searchState.mode === 'assisted') {
      // Belt and braces: the assisted form has no unconditional submit button, and
      // the server refuses an assisted start without a current preview anyway.
      toast('Preview the search first',
        'Assisted Search runs a harvest only after you have seen the search results.', 'warn');
      return;
    }
    const payload = harvestPayload(dryRun);
    if (!payload.search && !payload.topic_id) {
      toast('Nothing to search for', 'Enter search terms or a Topic ID.', 'warn');
      return;
    }
    if (!dryRun && !(await actions.checkForRepeat(payload))) return;
    await actions.launch(payload);
  },

  /* -- assisted search ---------------------------------------------------- */

  setMode(mode) {
    if (searchState.mode === mode) return;
    // Nothing is cleared: each mode keeps its own query state, so switching back
    // and forth is lossless (specification section 20).
    searchState.mode = mode;
    rerender();
  },

  field(key, value) {
    searchState[key] = value;
    syncControls();
  },

  filter(key, value) {
    searchState.filters[key] = value;
    syncControls();
  },

  /* Multi-value constraints. Re-rendered rather than only synced, because the
     "matches any of N" line and the Clear button below the list have to follow. */
  multi(key, element) {
    searchState.filters[key] = Array.from(element.selectedOptions, (o) => o.value);
    rerender();
  },

  clearMulti(key) {
    searchState.filters[key] = [];
    rerender();
  },

  async generateAdvice() {
    if (searchState.busy) return;                       // double-click protection
    const question = searchState.researchQuestion.trim();
    if (!question) {
      toast('Nothing to work with', 'Describe your research question first.', 'warn');
      return;
    }
    searchState.busy = 'advice';
    searchState.advisorError = null;
    rerender();
    try {
      const result = await api('/api/search/advice', {
        method: 'POST',
        body: { research_question: question, ...previewPayload() },
      });
      searchState.advice = result;
      searchState.generatedQuery = result.recommended_query;
      searchState.effectiveQuery = result.recommended_query;
      // A new recommendation invalidates any approval the previous one earned.
      searchState.preview = null;
      searchState.previewError = null;
      toast('Query suggested', 'Review it, edit it if you want, then preview.', 'ok');
    } catch (error) {
      // The research question, the filters and any previous suggestion all survive.
      searchState.advisorError = error.message;
    } finally {
      searchState.busy = null;
      rerender();
    }
  },

  async runPreview() {
    if (searchState.busy) return;                       // double-click protection
    if (!activeQuery() && !searchState.filters.topic_id.trim()) {
      toast('Nothing to search for', 'Enter a query or a Topic ID first.', 'warn');
      return;
    }
    const key = discoveryKey();
    searchState.busy = 'preview';
    searchState.previewError = null;
    rerender();
    try {
      const result = await api('/api/search/preview', { method: 'POST', body: previewPayload() });
      searchState.preview = {
        fingerprint: result.fingerprint,
        key,
        results: result.results,
        total_count: result.total_count,
        query: (result.query && result.query.search) || activeQuery(),
      };
    } catch (error) {
      searchState.preview = null;
      searchState.previewError = error.message;
    } finally {
      searchState.busy = null;
      rerender();
    }
  },

  editQuery() {
    const field = $('#h-recommended') || $('#h-search');
    if (field) { field.focus(); field.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
  },

  async startAssistedHarvest() {
    if (searchState.busy) return;                       // double-click protection
    if (!previewIsCurrent()) {
      toast('Preview is out of date', 'Preview the current query before harvesting.', 'warn');
      return;
    }
    const payload = harvestPayload(false);
    // Asked before the button is disabled, so the answer can re-render the page.
    if (!(await actions.checkForRepeat(payload))) return;
    searchState.busy = 'harvest';
    syncControls();
    try {
      await actions.launch(payload);
    } finally {
      searchState.busy = null;
      syncControls();
    }
  },

  /* -- post-harvest workflow ---------------------------------------------- */

  /* Put the page back into its pre-harvest state and hand the query back to the
     operator. The finished run is kept: it is still reachable from Runs, and typing
     the same search back brings its panel with it. */
  refineSearch() {
    searchState.harvested = null;
    searchState.duplicates = null;
    rerender();
    const field = $('#h-recommended') || $('#h-search') || $('#h-question');
    if (field) { field.focus(); field.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
  },

  /* Clear the search itself. Provider toggles, limits and constraints are left alone:
     they are settings, not the question being asked. */
  newHarvest() {
    Object.assign(searchState, {
      harvested: null, duplicates: null, duplicateAck: null,
      preview: null, previewError: null, advice: null, advisorError: null,
      conventionalQuery: '', researchQuestion: '', generatedQuery: '', effectiveQuery: '',
    });
    rerender();
  },

  /* -- repeat-run advisory ------------------------------------------------ */

  /* True when the harvest may proceed.
   *
   * This can refuse nothing. A check that fails, times out or finds the server
   * unwilling lets the harvest through, because an advisory that can block is no
   * longer an advisory — and re-running an identical search on purpose is exactly how
   * a result is reproduced.
   *
   * The acknowledgement is one use only, and is granted only by the operator pressing
   * "Run again". Remembering instead that a *search* had once been cleared was wrong
   * in precisely the case this exists for: the first start of a search finds no
   * previous run, and had that counted as an answer, the second start — the actual
   * repeat — would have gone through in silence. */
  async checkForRepeat(payload) {
    const key = harvestKey(payload);
    if (searchState.duplicateAck === key) {
      searchState.duplicateAck = null;
      return true;
    }
    let found = null;
    try {
      found = await api('/api/search/similar-runs', { method: 'POST', body: payload });
    } catch (_) {
      return true;
    }
    if (!found || !(found.runs || []).length) return true;
    searchState.duplicates = { key, payload, runs: found.runs };
    rerender();
    toast('Already harvested recently',
      'The same search has been run before. Open the existing run, or run it again.', 'warn');
    return false;
  },

  async runAnyway() {
    const pending = searchState.duplicates;
    if (!pending) return;
    searchState.duplicateAck = pending.key;
    searchState.duplicates = null;
    await actions.launch(pending.payload);
  },

  dismissDuplicate() {
    searchState.duplicates = null;
    rerender();
  },

  async launch(payload) {
    const result = await guard(() => api('/api/harvest', { method: 'POST', body: payload }),
      'Could not start');
    if (!result) return;
    lastHarvestPayload = payload;
    toast(payload.dry_run ? 'Search preview started' : 'Harvest started',
      payload.dry_run ? 'Searching for matching records — nothing will be downloaded.'
        : 'Discovery and downloading are running in the background.', 'ok');
    if (!location.hash.startsWith('#/harvest')) location.hash = '#/harvest';
    startPolling();
  },

  async promotePreview() {
    if (lastHarvestPayload) {
      await actions.launch({ ...lastHarvestPayload, dry_run: false });
      return;
    }
    if (!lastPreview) {
      toast('Nothing to harvest', 'Run a preview first.', 'warn');
      return;
    }
    // Rebuild the request from the preview run's own recorded query.
    const q = lastPreview.query;
    await actions.launch({
      search: q.search || null,
      topic_id: q.topic_id || null,
      primary_topic_only: !!q.primary_topic_only,
      from_year: q.from_publication_year || null,
      to_year: q.to_publication_year || null,
      oa_status: q.oa_status || null,
      limit: lastPreview.limit || null,
      dry_run: false,
    });
  },

  async resume(runId) {
    const result = await guard(() => api(`/api/runs/${encodeURIComponent(runId)}/resume`, { method: 'POST', body: {} }),
      'Could not resume');
    if (!result) return;
    toast('Resuming', 'Completed documents will not be downloaded again.', 'ok');
    startPolling();
  },

  async verify(deep) {
    const result = await guard(() => api('/api/verify', { method: 'POST', body: { deep } }),
      'Could not start verification');
    if (!result) return;
    toast(deep ? 'Deep verification started' : 'Verification started',
      deep ? 'Re-parsing every artifact — this can take a while.' : 'Checking files and checksums.', 'ok');
    startPolling();
  },

  async retry(options) {
    const result = await guard(() => api('/api/failures/retry', { method: 'POST', body: options }),
      'Could not re-queue');
    if (!result) return;
    toast('Re-queued', result.message, 'ok');
    rerender();
  },

  confirmRequeueAll() {
    failuresState.confirmingAll = true;
    rerender();
  },

  cancelRequeueAll() {
    failuresState.confirmingAll = false;
    rerender();
  },

  async requeueAllConfirmed() {
    failuresState.confirmingAll = false;
    await actions.retry({});
  },

  openDocument(documentId) {
    guard(() => openDocumentPanel(documentId), 'Could not open document');
  },

  applyCorpusFilters() {
    corpusState.page = 1;
    corpusState.filters = {
      search: $('#c-search').value.trim(),
      oa_status: $('#c-oa').value,
      year: $('#c-year').value,
      has_pdf: $('#c-pdf').value,
      has_xml: $('#c-xml').value,
      journal: $('#c-journal').value.trim(),
    };
    guard(loadCorpus, 'Could not load corpus');
  },

  clearCorpusFilters() {
    corpusState.page = 1;
    corpusState.filters = {};
    guard(viewCorpus, 'Could not load corpus');
  },

  corpusPage(page) {
    corpusState.page = page;
    guard(loadCorpus, 'Could not load corpus');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  },

  async saveSettings() {
    const updates = {};
    document.querySelectorAll('[data-key]').forEach((el) => {
      if (el.disabled) return;
      const key = el.dataset.key;
      if (el.type === 'checkbox') { updates[key] = el.checked; return; }
      // A select still resting on a value this server does not offer is left out
      // entirely. Submitting it would either write back a value the server rejects
      // or, worse, quietly persist whatever the browser picked instead.
      const chosen = el.selectedOptions && el.selectedOptions[0];
      if (chosen && chosen.dataset.unknown === 'true') return;
      const value = el.value.trim();
      if (el.type === 'password') { if (value) updates[key] = value; return; }
      updates[key] = value;
    });
    const result = await guard(() => api('/api/settings', { method: 'PUT', body: { updates } }),
      'Could not save settings');
    if (!result) return;
    toast('Settings saved', 'They apply to the next harvest and to the command line too.', 'ok');
    rerender();
  },
};

let lastHarvestPayload = null;
let lastPreview = null;

/* ------------------------------------------------------------------ router */

const ROUTES = [
  [/^#\/dashboard$/, viewDashboard],
  [/^#\/harvest$/, viewHarvest],
  [/^#\/runs$/, viewRuns],
  [/^#\/runs\/(.+)$/, viewRunDetail],
  [/^#\/corpus$/, viewCorpus],
  [/^#\/verify$/, viewVerify],
  [/^#\/failures$/, viewFailures],
  [/^#\/settings$/, viewSettings],
];

async function render() {
  const hash = location.hash || '#/dashboard';
  document.querySelectorAll('.nav-item').forEach((item) => {
    const target = item.getAttribute('href');
    const active = hash === target || hash.startsWith(target + '/');
    item.classList.toggle('active', active);
    if (active) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
  closePanel();

  for (const [pattern, handler] of ROUTES) {
    const match = hash.match(pattern);
    if (!match) continue;
    const pageName = {dashboard: 'Dashboard', harvest: 'New Harvest', runs: 'Runs', corpus: 'Corpus', verify: 'Verify', failures: 'Retry Failed', settings: 'Settings'}[hash.split('/')[1]];
    setHeader(pageName || 'Control Center', '');
    view().innerHTML = '<div class="loading" role="status">Loading…</div>';
    try {
      await handler(...match.slice(1));
    } catch (error) {
      view().innerHTML = `<div class="card"><div class="card-body">
        <div class="notice bad" role="alert"><span class="n-ico">Notice:</span>
          <div><strong>Could not load this page.</strong><div>${esc(error.message)}</div></div></div></div></div>`;
    }
    return;
  }
  location.hash = '#/dashboard';
}

function rerender() { guard(render, 'Could not refresh'); }

async function loadSidebarMeta() {
  try {
    const data = await api('/api/dashboard');
    $('#sidebar-meta').innerHTML = `
      <div>${num(data.cards.documents)} documents · ${num(data.cards.pdfs)} PDFs</div>
      <div>${esc(data.storage_root)}</div>`;
    const badge = $('#nav-failed');
    if (data.cards.failed) { badge.textContent = data.cards.failed; badge.hidden = false; }
    else badge.hidden = true;
  } catch (_) { /* the page itself will report the failure */ }
}

/* A route change is a new page, not a continuation of the old scroll position — but an
 * in-page refresh (rerender() after a setting is saved, a poll tick, a filter applied)
 * must NOT jump the operator back to the top. Scrolling to the top belongs to the
 * navigation event itself, not to render(), which both kinds of refresh share. */
window.addEventListener('hashchange', () => { window.scrollTo(0, 0); render(); });
window.addEventListener('DOMContentLoaded', () => {
  render();
  startPolling();
  loadSidebarMeta();
  setInterval(loadSidebarMeta, 15000);
});

window.actions = actions;
window.rerender = rerender;

/* Presentation/accessibility only. No API payload or corpus value is changed. */
function hashValue(value) {
  if (!value) return '—';
  const full = String(value);
  return `<span class="hash-value"><span title="${esc(full)}">${esc(full.length > 12 ? full.slice(0, 12) + '…' : full)}</span><button class="btn small" type="button" data-copy="${esc(full)}" aria-label="Copy full value">Copy</button></span>`;
}

document.addEventListener('click', async event => {
  const button = event.target.closest('[data-copy]');
  if (!button) return;
  event.stopPropagation();
  try {
    await navigator.clipboard.writeText(button.dataset.copy);
    toast('Copied', 'The complete value is on the clipboard.');
  } catch (_) { toast('Could not copy', 'Select the complete value in the technical JSON view.', 'bad'); }
}, true);
document.addEventListener('keydown', event => {
  const row = event.target.closest('tr.clickable');
  if (row && event.target === row && (event.key === 'Enter' || event.key === ' ')) {
    event.preventDefault(); row.click();
  }
});
document.addEventListener('invalid', event => {
  event.target.setAttribute('aria-invalid', 'true');
}, true);
document.addEventListener('input', event => {
  if (event.target.validity && event.target.validity.valid) event.target.removeAttribute('aria-invalid');
});

function enhancePresentation() {
  document.querySelectorAll('tr.clickable:not([tabindex])').forEach(row => {
    row.tabIndex = 0;
    row.setAttribute('aria-label', `Open details: ${row.innerText.trim().replace(/\s+/g, ' ')}`);
  });
  document.querySelectorAll('.field').forEach((field, index) => {
    const label = field.querySelector('label'), input = field.querySelector('input,select,textarea'), hint = field.querySelector('.hint');
    if (!input || !label) return;
    if (!input.id) input.id = `notanda-field-${input.dataset.key || index}`;
    if (!label.htmlFor) label.htmlFor = input.id;
    if (hint) { if (!hint.id) hint.id = `${input.id}-hint`; input.setAttribute('aria-describedby', hint.id); }
  });
  document.querySelectorAll('.notice.bad .n-ico').forEach(label => {
    if (label.textContent === 'Notice:') label.textContent = 'Error:';
  });
  document.querySelectorAll('.table-wrap:not([tabindex])').forEach(panel => {
    panel.tabIndex = 0;
    panel.setAttribute('role', 'region');
    panel.setAttribute('aria-label', 'Scrollable data table');
  });
  document.querySelectorAll('pre.code:not([data-copy-ready])').forEach(code => {
    code.dataset.copyReady = 'true';
    const button = document.createElement('button');
    button.className = 'btn small code-copy'; button.type = 'button';
    button.textContent = 'Copy JSON'; button.dataset.copy = code.textContent;
    code.before(button);
  });
}
window.addEventListener('DOMContentLoaded', () => {
  enhancePresentation();
  new MutationObserver(enhancePresentation).observe(document.body, {childList: true, subtree: true});
});

