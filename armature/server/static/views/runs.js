import { api, listOf } from '../api.js';
import { append, card, clear, duration, element, empty, errorBox, link, loading, money, pageHeader, secondsBetween, statusChip, statusText, table, tableRow, when } from '../dom.js';
import { loadDisplay, workflowLabel } from '../display.js';
import { every, subscribe } from '../live.js';
import { hashFor } from '../router.js';

const STATUSES = ['queued', 'running', 'succeeded', 'failed', 'canceled', 'lost'];
const PAGE_SIZE = 50;
const MAX_ROWS = 200;
const POLL_MS = 5000;
const HEADERS = ['Started', 'Run', 'Subject', 'Team', 'Project', 'Status', 'Triggered by', ['Time', 'num'], ['Cost', 'num']];

export const runLabel = (run, display) => run.title || workflowLabel(display, run.workflow);

export const isHidden = (display, workflow) => display?.workflow_labels?.[workflow]?.hidden === true;

export function chosenWorkflows(query) {
  return [...new Set(String(query.get('workflows') || '').split(',').map((name) => name.trim()).filter(Boolean))];
}

export function listParams(filter, before = '') {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
  for (const key of ['team', 'project', 'status', 'subject']) if (filter[key]) params.set(key, filter[key]);
  if (filter.workflows?.length) params.set('workflows', filter.workflows.join(','));
  if (filter.hidden) params.set('include_hidden', '1');
  if (before) params.set('before', before);
  return params;
}

export function hashParams(filter) {
  return { team: filter.team, project: filter.project, status: filter.status, subject: filter.subject,
    workflows: filter.workflows?.join(',') || '', hidden: filter.hidden ? '1' : '' };
}

export function matches(run, filter, display) {
  if (filter.status && run.status !== filter.status) return false;
  if (filter.subject && run.subject !== filter.subject) return false;
  if (filter.workflows?.length) return filter.workflows.includes(run.workflow);
  return filter.hidden || !isHidden(display, run.workflow);
}

export function workflowOptions(display, names) {
  const all = new Set([...Object.keys(display?.workflow_labels || {}), ...names.filter(Boolean)]);
  return [...all].map((name) => ({ id: name, name: workflowLabel(display, name), hidden: isHidden(display, name) }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

export const subjectHref = (subject) => hashFor('subject', [subject]);

export function verdictTone(verdict) {
  const text = String(verdict || '').toLowerCase();
  if (/not approv|block|change|fail|reject/.test(text)) return 'bad';
  if (/approv|pass|merged|done|pushed|opened/.test(text)) return 'ok';
  return '';
}

export function verdictChip(verdict) {
  if (!verdict) return null;
  return element('span', { class: `chip ${verdictTone(verdict)}`.trim() }, String(verdict).replace(/_/g, ' '));
}

export function subjectLink(subject) {
  return subject ? link(subjectHref(subject), subject) : '—';
}

function projectLoader() {
  const cache = new Map();
  return (teamId) => {
    if (!cache.has(teamId)) {
      cache.set(teamId, api(`/api/teams/${encodeURIComponent(teamId)}/projects`).then((payload) => listOf(payload, 'projects')).catch(() => []));
    }
    return cache.get(teamId);
  };
}

function labelled(text, control) {
  return element('label', {}, text, control);
}

function fillOptions(select, anyLabel, items, selected) {
  append(clear(select), [element('option', { value: '' }, anyLabel), items.map((item) => element('option', { value: item.id }, item.name))]);
  select.value = items.some((item) => String(item.id) === selected) ? selected : '';
}

function elapsed(run) {
  if (!run.finished_at) return run.status === 'running' ? 'Running' : '—';
  return duration(secondsBetween(run.started_at, run.finished_at));
}

export async function render(root, { query, signal }) {
  root.append(pageHeader('Runs'));
  const projectsOf = projectLoader();
  const teamSelect = element('select', { name: 'team' });
  const projectSelect = element('select', { name: 'project' });
  const statusSelect = element('select', { name: 'status' }, element('option', { value: '' }, 'Any status'),
    STATUSES.map((status) => element('option', { value: status }, statusText(status))));
  statusSelect.value = STATUSES.includes(query.get('status')) ? query.get('status') : '';
  const subject = query.get('subject') || '';
  const workflowSelect = element('select', { name: 'workflows', multiple: true, size: 1 });
  const hiddenBox = element('input', { type: 'checkbox', name: 'hidden' });
  hiddenBox.checked = query.get('hidden') === '1';
  const filters = element('form', { class: 'filters', role: 'search', 'aria-label': 'Filter runs' },
    labelled('Team', teamSelect), labelled('Project', projectSelect), labelled('Status', statusSelect),
    labelled('Workflows', workflowSelect), labelled('Show hidden', hiddenBox), element('button', { type: 'submit' }, 'Apply'),
    subject ? element('span', { class: 'muted' }, `Subject: ${subject} `, link(hashFor('runs'), 'Clear')) : null);
  const body = element('div', {}, loading());
  root.append(filters, body);

  const [teams, display, definitions] = await Promise.all([
    api('/api/teams').then((payload) => listOf(payload, 'teams')).catch(() => []),
    loadDisplay(),
    api('/api/workflows').then((payload) => listOf(payload, 'workflows')).catch(() => []),
  ]);
  const chosen = chosenWorkflows(query);
  const fillWorkflows = (names) => {
    const selected = new Set([...workflowSelect.selectedOptions].map((option) => option.value).concat(chosen));
    append(clear(workflowSelect), workflowOptions(display, [...names, ...chosen]).map((item) =>
      element('option', { value: item.id, selected: selected.has(item.id) }, item.hidden ? `${item.name} (hidden)` : item.name)));
    workflowSelect.size = Math.min(Math.max(workflowSelect.options.length, 1), 4);
  };
  fillWorkflows(definitions.map((definition) => definition.name));
  const teamName = new Map(teams.map((team) => [String(team.id), team.name]));
  fillOptions(teamSelect, 'All teams', teams, query.get('team') || '');
  fillOptions(projectSelect, 'All projects', teamSelect.value ? await projectsOf(teamSelect.value) : [], query.get('project') || '');
  teamSelect.addEventListener('change', async () => {
    fillOptions(projectSelect, 'All projects', teamSelect.value ? await projectsOf(teamSelect.value) : [], '');
  });
  filters.addEventListener('submit', (submitEvent) => {
    submitEvent.preventDefault();
    location.hash = hashFor('runs', [], hashParams({ team: teamSelect.value, project: projectSelect.value, status: statusSelect.value, subject,
      workflows: [...workflowSelect.selectedOptions].map((option) => option.value), hidden: hiddenBox.checked }));
  });

  const filter = { team: teamSelect.value, project: projectSelect.value, status: statusSelect.value, subject, workflows: chosen, hidden: hiddenBox.checked };
  const params = listParams(filter, query.get('before') || '');
  let page;
  try {
    page = await api(`/api/runs?${params}`);
  } catch (err) {
    clear(body).append(errorBox(err));
    return;
  }
  const runs = listOf(page, 'runs');
  fillWorkflows(runs.map((run) => run.workflow));
  const projectName = new Map();
  for (const teamProjects of await Promise.all([...new Set(runs.map((run) => String(run.team_id)))].map(projectsOf))) {
    for (const project of teamProjects) projectName.set(String(project.id), project.name);
  }
  const cells = (run) => [
    element('span', { title: run.started_at || null }, when(run.started_at)),
    element('span', {}, link(hashFor('run', [run.id]), runLabel(run, display)), ' ', verdictChip(run.verdict),
      run.title ? element('div', { class: 'muted' }, workflowLabel(display, run.workflow)) : null),
    subjectLink(run.subject), teamName.get(String(run.team_id)) || '—', projectName.get(String(run.project_id)) || '—',
    statusChip(run.status), run.triggered_by || '—', elapsed(run), money(run.cost_usd),
  ];
  const rowFor = (run) => {
    const row = tableRow(HEADERS, cells(run));
    row.dataset.runId = String(run.id);
    return row;
  };
  const isFiltered = [filter.team, filter.project, filter.status, filter.subject, filter.workflows.length].some(Boolean) || query.get('before');
  const emptyCard = card(empty(isFiltered ? 'No runs match these filters. Clear a filter to see more.' : 'No runs yet.'));
  const runTable = table(HEADERS, []);
  const tbody = runTable.querySelector('tbody');
  for (const run of runs) tbody.append(rowFor(run));
  const tableCard = card(runTable);
  clear(body).append(runs.length ? tableCard : emptyCard);
  const pager = element('nav', { class: 'pager', 'aria-label': 'Pages' });
  if (query.get('before')) pager.append(link(hashFor('runs', [], hashParams(filter)), 'Newest'));
  if (page.next_before) pager.append(link(hashFor('runs', [], { ...hashParams(filter), before: page.next_before }), 'Older'));
  if (pager.firstChild) body.append(pager);
  if (query.get('before')) return;

  const pending = new Map();
  let frame = 0;
  const flush = async () => {
    frame = 0;
    const batch = [...pending.values()];
    pending.clear();
    const unknownTeams = [...new Set(batch.filter((run) => run.project_id && !projectName.has(String(run.project_id))).map((run) => String(run.team_id)))];
    for (const teamProjects of await Promise.all(unknownTeams.map(projectsOf))) {
      for (const project of teamProjects) projectName.set(String(project.id), project.name);
    }
    if (signal?.aborted) return;
    for (const run of batch) upsert(run);
  };
  const upsert = (run) => {
    const existing = tbody.querySelector(`tr[data-run-id="${CSS.escape(String(run.id))}"]`);
    if (existing) {
      const freshCells = [...rowFor(run).cells];
      [...existing.cells].forEach((cell, index) => {
        if (!cell.contains(document.activeElement)) cell.replaceWith(freshCells[index]);
      });
      return;
    }
    if (!matches(run, filter, display)) return;
    const row = rowFor(run);
    const scrolledPast = window.scrollY > 0 && runTable.getBoundingClientRect().top < 0;
    tbody.prepend(row);
    if (!tableCard.isConnected) emptyCard.replaceWith(tableCard);
    if (scrolledPast) window.scrollBy(0, row.getBoundingClientRect().height);
    while (tbody.rows.length > MAX_ROWS) tbody.lastElementChild.remove();
  };
  const receive = (run) => {
    if (!run || run.id === undefined || run.id === null) return;
    pending.set(String(run.id), run);
    if (!frame) frame = requestAnimationFrame(flush);
  };
  const streamParams = new URLSearchParams();
  if (filter.team) streamParams.set('team', filter.team);
  if (filter.project) streamParams.set('project', filter.project);
  const liveNote = element('span', { class: 'live-state muted', role: 'status' });
  root.querySelector('header.page').append(liveNote);
  const states = { live: 'Live', reconnecting: 'Reconnecting…', polling: 'Refreshing every 5 s' };
  subscribe(`/api/stream/runs?${streamParams}`, {
    events: { run: receive },
    onState: (state) => { liveNote.textContent = states[state] || ''; },
    onFallback: () => every(POLL_MS, async () => {
      for (const run of listOf(await api(`/api/runs?${params}`), 'runs').reverse()) receive(run);
    }, signal),
  }, { signal });
}
