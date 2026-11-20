import { outcomeText } from '../actions.js';
import { api, listOf } from '../api.js';
import { append, attempt, card, clear, confirmDialog, duration, element, empty, errorBox, link, loading, money, pageHeader, secondsBetween, statusText, statusTone, table, tableRow, when } from '../dom.js';
import { loadDisplay, workflowColor, workflowLabel } from '../display.js';
import { elapsedSeconds } from '../feed.js';
import { every, subscribe } from '../live.js';
import { externalLink, externalLinks } from '../outcome.js';
import { hashFor } from '../router.js';
import { canCancel, mayCancel } from '../runpage.js';
import { currentSession } from '../session.js';

const STATUSES = ['queued', 'running', 'waiting', 'succeeded', 'failed', 'canceled', 'lost'];
const PAGE_SIZE = 50;
const RECENT_ROWS = 10;
const MAX_ROWS = 200;
const POLL_MS = 5000;
const APPROVED = new Set(['approve', 'approved', 'approval recommended']);
const CHANGES = new Set(['comment', 'changes', 'request_changes', 'changes_requested', 'changes recommended']);
const OUTCOMES = { opened: 'PR Created', pr_opened: 'PR Created', recommended: 'Changes Recommended', noop: 'Nothing to Do', pushed: 'Changes Pushed', 'review-blocked': 'Review Blocked', 'dry-run': 'Dry Run', 'push-failed': 'Push Failed',
  'pr-failed': 'PR Failed' };
const OUT_KINDS = new Set(['pull', 'issue', 'slack']);
const ACTIVE = new Set(['queued', 'running']);
const ACTIVE_OR_WAITING = new Set(['queued', 'running', 'waiting']);
const ENDED = new Set(['failed', 'canceled', 'lost']);
const SETTLED = new Set(['succeeded', 'failed', 'canceled']);

export const runHeaders = (display) => [['Started', 'col-tight'], ['Run', 'col-title'], ['Workflow', 'col-tight'], 'Links',
  ['Outcome', 'col-tight'], ['Where', 'col-tight'], 'Trigger', ['Time', 'num col-tight'], ...(display?.show_cost ? [['Cost', 'num col-tight']] : [])];

export function shortWhen(timestamp, now = new Date()) {
  const date = new Date(timestamp || '');
  if (!timestamp || Number.isNaN(date.getTime())) return '—';
  const time = `${date.getHours()}:${String(date.getMinutes()).padStart(2, '0')}`;
  const day = `${date.getMonth() + 1}/${date.getDate()}`;
  return date.getFullYear() === now.getFullYear() ? `${day} ${time}` : `${day}/${String(date.getFullYear()).slice(2)} ${time}`;
}

export const prTitle = (run, pr, display) => pr?.title || run.title || (chained(run) ? threadOf(run) : null) || workflowLabel(display, run.workflow);

export function verdictText(verdict) {
  const text = String(verdict || '').trim();
  if (!text) return '';
  if (APPROVED.has(text.toLowerCase())) return 'Approved';
  if (CHANGES.has(text.toLowerCase())) return 'Changes Recommended';
  if (Object.hasOwn(OUTCOMES, text.toLowerCase())) return OUTCOMES[text.toLowerCase()];
  return text.charAt(0).toUpperCase() + text.slice(1).replace(/_/g, ' ');
}

const outcomeTone = (label) => {
  if (label === 'Changes Recommended') return 'warn';
  return /fail|reject|block/i.test(label) ? 'bad' : 'ok';
};

// threadOpen marks a grouped row whose thread still has an open run, which can be older than the row's own run.
export function runStatus(run, pr, threadOpen = false) {
  if (ACTIVE.has(run.status)) return { text: 'In Progress', tone: 'running' };
  if (run.status === 'waiting') return { text: 'Waiting for approval', tone: 'warn' };
  if (threadOpen) return { text: 'In Progress', tone: 'running' };
  if (ENDED.has(run.status)) return { text: statusText(run.status), tone: statusTone(run.status) };
  if (pr) {
    const verdict = verdictText(pr.verdict);
    const text = [verdict, pr.merged ? 'Merged' : ''].filter(Boolean).join(' - ');
    if (text) return { text, tone: outcomeTone(verdict) };
  }
  // A typed outcome only exists on newer runs. Older ones read as they always did: verdict, then fix outcome.
  const own = verdictText(run.outcome) || verdictText(run.verdict) || verdictText(run.fix_outcome) || 'Done';
  return { text: own, tone: outcomeTone(own) };
}

export const isHidden = (display, workflow) => display?.workflow_labels?.[workflow]?.hidden === true;

export function chosenWorkflows(query) {
  return [...new Set(String(query.get('workflows') || '').split(',').map((name) => name.trim()).filter(Boolean))];
}

export function listParams(filter, before = '') {
  const params = new URLSearchParams({ limit: String(filter.all || before ? PAGE_SIZE : RECENT_ROWS) });
  for (const key of ['team', 'project', 'status', 'subject', 'thread']) if (filter[key]) params.set(key, filter[key]);
  if (filter.workflows?.length) params.set('workflows', filter.workflows.join(','));
  if (filter.hidden) params.set('include_hidden', '1');
  if (!filter.every) params.set('group', '1');
  if (before) params.set('before', before);
  return params;
}

// Null when the chosen status is never open, so nothing is in progress.
export function openParams(filter) {
  if (filter.status && !ACTIVE_OR_WAITING.has(filter.status)) return null;
  const params = listParams({ ...filter, every: true, status: filter.status || [...ACTIVE_OR_WAITING].join(',') });
  params.set('limit', String(MAX_ROWS));
  return params;
}

export function hashParams(filter) {
  return { team: filter.team, project: filter.project, status: filter.status, subject: filter.subject, thread: filter.thread,
    workflows: filter.workflows?.join(',') || '', hidden: filter.hidden ? '1' : '', every: filter.every ? '1' : '', all: filter.all ? '1' : '' };
}

export const threadOf = (run) => run.thread || run.subject || '';

const ISSUE_KEY = /^[A-Z][A-Z0-9_]{1,100}-[1-9][0-9]{0,9}$/;

// An issue's own runs are a chain of workflows. A run on one of its pull requests is not one of its steps.
export const chained = (run) => ISSUE_KEY.test(threadOf(run)) && (!run.subject || run.subject === threadOf(run));

export const rethreaded = (shown, run) => (shown || '') !== threadOf(run);

export const groupText = (size) => (size > 1 ? `${size} runs` : '');

const startedAt = (value) => {
  const at = Date.parse(value || '');
  return Number.isNaN(at) ? -Infinity : at;
};

// The server's order: started_at, then id, both descending.
export function isNewer(run, shown) {
  const left = startedAt(run.started_at);
  const right = startedAt(shown.started_at);
  return left === right ? String(run.id) > String(shown.id) : left > right;
}

export function matches(run, filter, display) {
  if (filter.status && run.status !== filter.status) return false;
  if (filter.subject && run.subject !== filter.subject) return false;
  if (filter.thread && threadOf(run) !== filter.thread) return false;
  if (filter.hidden) return !filter.workflows?.length || filter.workflows.includes(run.workflow);
  if (display?.hidden_actors?.includes(run.triggered_by)) return false;
  if (filter.workflows?.length) return filter.workflows.includes(run.workflow);
  return !isHidden(display, run.workflow);
}

export function workflowOptions(display, names) {
  const all = new Set([...Object.keys(display?.workflow_labels || {}), ...names.filter(Boolean)]);
  return [...all].map((name) => ({ id: name, name: workflowLabel(display, name), hidden: isHidden(display, name) }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

// "All" first, then each visible workflow. One chosen workflow presses its capsule, several press none.
export function capsules(display, names, chosen) {
  const shown = workflowOptions(display, names).filter((item) => !item.hidden);
  const order = shown.map((item) => item.id);
  return [{ id: '', name: 'All', color: null, pressed: !chosen.length },
    ...shown.map((item) => ({ id: item.id, name: item.name, color: workflowColor(display, item.id, order), pressed: chosen.length === 1 && chosen[0] === item.id }))];
}

export function triggerOf(run) {
  const trigger = run.trigger;
  return trigger?.label ? { label: trigger.label, detail: trigger.detail || '' } : { label: run.triggered_by || '—', detail: '' };
}

export const liveElapsed = (run, now = Date.now()) => (run.started_at ? duration(elapsedSeconds(run.started_at, null, now)) : 'Waiting');

export const subjectHref =(subject) => hashFor('subject', [subject]);

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

export const locationText = (where) => (typeof where?.label === 'string' && where.label) || 'Unknown';

export function locationChip(where) {
  const detail = [where?.environment_type, where?.instance, where?.instance_type].filter((part) => typeof part === 'string' && part).join(' · ');
  return element('span', { class: 'chip', title: detail || null }, locationText(where));
}

// Cancel for an open run in a list, as the run page offers it. A click on it never opens the row.
export function cancelControl(run, session = currentSession()) {
  const job = run?.job;
  if (job?.cancel_requested_at && ACTIVE_OR_WAITING.has(run.status)) return element('span', { class: 'muted' }, 'Cancel requested');
  const offered = job ? canCancel(job, run, session) : run?.host_cancel === true && mayCancel(run, session);
  if (!ACTIVE_OR_WAITING.has(run?.status) || !offered) return null;
  const button = element('button', { type: 'button', class: 'small' }, 'Cancel');
  const status = element('span', { role: 'status' });
  const slot = element('span', { class: 'row-cancel' }, button, status);
  button.addEventListener('click', async (clickEvent) => {
    clickEvent.stopPropagation();
    if (!(await confirmDialog('Cancel this run?', 'Its runner stops it and the run ends as canceled. It cannot be resumed.', 'Cancel run'))) return;
    const answer = await attempt(status, button, () => api(`/api/runs/${encodeURIComponent(run.id)}/cancel`, { method: 'POST', body: {} }));
    if (answer?.job) slot.replaceChildren(element('span', { class: 'muted' }, 'Cancel requested'));
    if (answer?.operation) slot.replaceChildren(element('span', { class: 'muted' }, answer.operation.outcome === 'ok' ? 'Cancelled' : outcomeText(answer.operation)));
  });
  return slot;
}

export function subjectLink(subject) {
  return subject ? link(subjectHref(subject), subject) : '—';
}

// The subject a trigger names is already in the row, so it is not repeated.
function triggerCell(run) {
  const { label, detail: named } = triggerOf(run);
  const detail = named === run.subject || named === run.thread ? '' : named;
  return [element('span', { class: 'trigger-label' }, label), detail ? element('span', { class: 'muted trigger-detail' }, detail) : null];
}

function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

// The thread's page when the run is in one, else the run's own.
export const runRowHref = (run) => (threadOf(run) ? subjectHref(threadOf(run)) : hashFor('run', [run.id]));

function linksCell(run) {
  const out = externalLinks({ links: run.links }).filter((item) => OUT_KINDS.has(item.kind));
  return out.length ? element('span', { class: 'row-links' }, out.map((item) => externalLink({ ...item, label: item.short }))) : '—';
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
  if (ACTIVE.has(run.status)) return element('span', { class: 'row-elapsed', 'data-started': run.started_at || '' }, liveElapsed(run));
  if (!run.finished_at) return '—';
  return duration(secondsBetween(run.started_at, run.finished_at));
}

// A click anywhere on a row opens what its title links to, unless it landed on a link of its own.
function openRow(clickEvent) {
  const row = clickEvent.target.closest('tr[data-href]');
  if (!row || clickEvent.target.closest('a, button, input, select, summary') || getSelection()?.toString()) return;
  location.hash = row.dataset.href;
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
  const thread = query.get('thread') || '';
  const workflowSelect = element('select', { name: 'workflows', multiple: true, size: 1 });
  const pickerToggle = element('button', { type: 'button', class: 'picker-toggle', 'aria-expanded': 'false', 'aria-controls': 'runs-workflow-picker' }, 'Workflows');
  const picker = element('label', { id: 'runs-workflow-picker', hidden: true }, element('span', { class: 'visually-hidden' }, 'Workflows'), workflowSelect);
  pickerToggle.addEventListener('click', () => {
    const open = pickerToggle.getAttribute('aria-expanded') !== 'true';
    pickerToggle.setAttribute('aria-expanded', String(open));
    picker.hidden = !open;
    if (open) workflowSelect.focus();
  });
  const hiddenBox = element('input', { type: 'checkbox', name: 'hidden' });
  hiddenBox.checked = query.get('hidden') === '1';
  const everyBox = element('input', { type: 'checkbox', name: 'every' });
  everyBox.checked = query.get('every') === '1';
  const filters = element('form', { class: 'filters', role: 'search', 'aria-label': 'Filter runs' },
    labelled('Team', teamSelect), labelled('Project', projectSelect), labelled('Status', statusSelect),
    pickerToggle, picker, labelled('Show hidden', hiddenBox), labelled('Show every run', everyBox), element('button', { type: 'submit' }, 'Apply'),
    subject ? element('span', { class: 'muted' }, `Subject: ${subject} `, link(hashFor('runs'), 'Clear')) : null,
    thread ? element('span', { class: 'muted' }, `Thread: ${thread} `, link(hashFor('runs'), 'Clear')) : null);
  const capsuleRow = element('div', { class: 'capsules', role: 'group', 'aria-label': 'Show one workflow' });
  const all = query.get('all') === '1' || Boolean(query.get('before'));
  const openArea = element('div', {}, loading());
  const body = element('div', {}, loading());
  root.append(capsuleRow, filters, element('h2', {}, 'In progress'), openArea, element('h2', {}, all ? 'All runs' : 'Recent'), body);

  const [teams, display, definitions] = await Promise.all([
    api('/api/teams').then((payload) => listOf(payload, 'teams')).catch(() => []),
    loadDisplay(),
    api('/api/workflows').then((payload) => listOf(payload, 'workflows')).catch(() => []),
  ]);
  const filter = { team: '', project: '', status: statusSelect.value, subject, thread, workflows: chosenWorkflows(query), hidden: hiddenBox.checked, every: everyBox.checked, all };
  let before = query.get('before') || '';
  const names = new Set(definitions.map((definition) => definition.name));
  const fillWorkflows = (more) => {
    for (const name of more) if (name) names.add(name);
    const selected = new Set([...workflowSelect.selectedOptions].map((option) => option.value).concat(filter.workflows));
    append(clear(workflowSelect), workflowOptions(display, [...names, ...filter.workflows]).map((item) =>
      element('option', { value: item.id, selected: selected.has(item.id) }, item.hidden ? `${item.name} (hidden)` : item.name)));
    workflowSelect.size = Math.min(Math.max(workflowSelect.options.length, 1), 6);
  };
  const syncPicker = () => {
    for (const option of workflowSelect.options) option.selected = filter.workflows.includes(option.value);
    setText(pickerToggle, filter.workflows.length > 1 ? `Workflows (${filter.workflows.length})` : 'Workflows');
  };
  fillWorkflows([]);
  syncPicker();
  fillOptions(teamSelect, 'All teams', teams, query.get('team') || '');
  fillOptions(projectSelect, 'All projects', teamSelect.value ? await projectsOf(teamSelect.value) : [], query.get('project') || '');
  filter.team = teamSelect.value;
  filter.project = projectSelect.value;
  teamSelect.addEventListener('change', async () => {
    fillOptions(projectSelect, 'All projects', teamSelect.value ? await projectsOf(teamSelect.value) : [], '');
  });
  filters.addEventListener('submit', (submitEvent) => {
    submitEvent.preventDefault();
    location.hash = hashFor('runs', [], hashParams({ team: teamSelect.value, project: projectSelect.value, status: statusSelect.value, subject, thread,
      workflows: [...workflowSelect.selectedOptions].map((option) => option.value), hidden: hiddenBox.checked, every: everyBox.checked, all }));
  });

  const prs = new Map();
  const prFor = (run) => {
    if (run.pr && run.subject) prs.set(run.subject, run.pr);
    return prs.get(run.subject) || null;
  };
  // Stream updates carry no group_size, so each row keeps the count its run was listed with.
  const sizes = new Map();
  const counted = new Set();
  // Open run id to its thread, for the In progress table and for grouped rows whose thread is still open.
  let openThreads = new Map();
  const threadOpen = (key) => Boolean(key) && [...openThreads.values()].includes(key);
  const shownRuns = new Map();
  const cells = (run, grouped) => {
    const pr = prFor(run);
    const status = runStatus(run, pr, grouped && !filter.every && threadOpen(threadOf(run)));
    const count = grouped ? groupText(sizes.get(String(run.id))) : '';
    return [
      link(hashFor('run', [run.id]), shortWhen(run.started_at), { title: `Open this run, started ${when(run.started_at)}` }),
      [link(runRowHref(run), prTitle(run, pr, display)), count ? element('span', { class: 'muted' }, ` · ${count}`) : null],
      workflowLabel(display, run.workflow),
      linksCell(run), [element('span', { class: `state state-${status.tone}` }, status.text), cancelControl(run)], locationChip(run.location),
      triggerCell(run), elapsed(run), ...(display.show_cost ? [money(run.cost_usd)] : []),
    ];
  };
  const headers = runHeaders(display);
  const rowFor = (run, grouped = true) => {
    if (run.group_size) sizes.set(String(run.id), run.group_size);
    if (grouped) shownRuns.set(String(run.id), run);
    const row = tableRow(headers, cells(run, grouped));
    row.dataset.runId = String(run.id);
    row.dataset.href = runRowHref(run);
    row.classList.add('row-link');
    if (run.started_at) row.dataset.startedAt = run.started_at;
    if (threadOf(run)) row.dataset.thread = threadOf(run);
    return row;
  };
  const refreshRow = (existing, fresh) => {
    const freshCells = [...fresh.cells];
    [...existing.cells].forEach((cell, index) => {
      if (!cell.contains(document.activeElement)) cell.replaceWith(freshCells[index]);
    });
  };

  const openTable = table(headers, []);
  const openBody = openTable.querySelector('tbody');
  const openCard = card(openTable);
  const nothingOpen = card(empty('Nothing is in progress.'));
  let openFailed = false;
  const showOpen = () => {
    const shown = openBody.rows.length ? openCard : nothingOpen;
    if (!openFailed && !shown.isConnected) clear(openArea).append(shown);
  };
  // The stream can report a run ended before its row is written, so a later fetch may still list it open.
  const settled = new Set();
  const streamed = new Map();
  let streamSeq = 0;
  let openToken = 0;
  const loadOpen = async () => {
    const params = openParams(filter);
    const token = ++openToken;
    const seen = streamSeq;
    let runs = [];
    if (params) {
      try {
        runs = listOf(await api(`/api/runs?${params}`), 'runs');
      } catch (err) {
        if (token === openToken) {
          openFailed = true;
          clear(openArea).append(errorBox(err));
        }
        return;
      }
    }
    if (token !== openToken || signal?.aborted) return;
    const byId = new Map(runs.map((run) => [String(run.id), run]));
    for (const [id, [seq, run]] of streamed) {
      if (seq <= seen) streamed.delete(id);
      else if (ACTIVE_OR_WAITING.has(run.status) && matches(run, filter, display)) byId.set(id, run);
      else byId.delete(id);
    }
    const shown = [...byId.values()].filter((run) => !settled.has(String(run.id))).sort((left, right) => (isNewer(left, right) ? -1 : 1));
    openThreads = new Map(shown.map((run) => [String(run.id), threadOf(run)]));
    const scrolledPast = window.scrollY > 0 && openArea.getBoundingClientRect().top < 0;
    const height = openArea.getBoundingClientRect().height;
    openBody.replaceChildren(...shown.map((run) => rowFor(run, false)));
    openFailed = false;
    showOpen();
    if (scrolledPast) window.scrollBy(0, openArea.getBoundingClientRect().height - height);
    refreshThreads();
  };
  // Returns the threads whose open state the run changed.
  const upsertOpen = (run) => {
    const id = String(run.id);
    const key = threadOf(run);
    const was = openThreads.get(id);
    streamed.set(id, [++streamSeq, run]);
    if (SETTLED.has(run.status)) settled.add(id);
    const keep = ACTIVE_OR_WAITING.has(run.status) && !settled.has(id) && matches(run, filter, display);
    const existing = openBody.querySelector(`tr[data-run-id="${CSS.escape(id)}"]`);
    if (keep && existing && rethreaded(existing.dataset.thread, run)) {
      existing.replaceWith(rowFor(run, false));
    } else if (keep && existing) {
      refreshRow(existing, rowFor(run, false));
    } else if (keep || existing) {
      const scrolledPast = window.scrollY > 0 && openArea.getBoundingClientRect().top < 0;
      const height = openArea.getBoundingClientRect().height;
      if (existing) existing.remove();
      else openBody.insertBefore(rowFor(run, false), [...openBody.rows].find((row) => isNewer(run, { started_at: row.dataset.startedAt, id: row.dataset.runId })) || null);
      showOpen();
      if (scrolledPast) window.scrollBy(0, openArea.getBoundingClientRect().height - height);
    }
    let touched = [];
    if (keep && was !== key) {
      openThreads.set(id, key);
      touched = [was, key];
    } else if (!keep && openThreads.has(id)) {
      openThreads.delete(id);
      touched = [was];
    }
    return touched.filter(Boolean);
  };

  let runTable = table(headers, []);
  let tbody = runTable.querySelector('tbody');
  let tableCard = card(runTable);
  let emptyCard = card(empty('No runs yet.'));
  let listToken = 0;
  const refreshThreads = (keys = null) => {
    if (filter.every) return;
    for (const row of [...tbody.rows]) {
      const run = shownRuns.get(row.dataset.runId);
      if (run && (!keys || keys.has(row.dataset.thread))) refreshRow(row, rowFor(run));
    }
  };
  const loadList = async () => {
    const token = ++listToken;
    let page;
    try {
      page = await api(`/api/runs?${listParams(filter, before)}`);
    } catch (err) {
      if (token === listToken) clear(body).append(errorBox(err));
      return;
    }
    if (token !== listToken || signal?.aborted) return;
    const runs = listOf(page, 'runs');
    fillWorkflows(runs.map((run) => run.workflow));
    const isFiltered = [filter.team, filter.project, filter.status, filter.subject, filter.thread, filter.workflows.length, before].some(Boolean);
    emptyCard = card(empty(isFiltered ? 'No runs match these filters. Clear a filter to see more.' : 'No runs yet.'));
    runTable = table(headers, []);
    tbody = runTable.querySelector('tbody');
    for (const run of runs) tbody.append(rowFor(run));
    tableCard = card(runTable);
    clear(body).append(runs.length ? tableCard : emptyCard);
    if (!filter.all) {
      if (runs.length) body.append(element('p', { class: 'note' }, link(hashFor('runs', [], { ...hashParams(filter), all: '1' }), 'See all runs')));
      return;
    }
    const pager = element('nav', { class: 'pager', 'aria-label': 'Pages' });
    if (before) pager.append(link(hashFor('runs', [], hashParams(filter)), 'Newest'));
    if (page.next_before) pager.append(link(hashFor('runs', [], { ...hashParams(filter), before: page.next_before }), 'Older'));
    if (pager.firstChild) body.append(pager);
  };

  const ticker = setInterval(() => {
    const now = Date.now();
    for (const span of root.querySelectorAll('.row-elapsed')) setText(span, liveElapsed({ started_at: span.dataset.started }, now));
  }, 1000);
  signal?.addEventListener('abort', () => clearInterval(ticker));
  body.addEventListener('click', openRow);
  openArea.addEventListener('click', openRow);

  // Capsules filter in place, without reloading the page.
  const capsuleButtons = new Map();
  const choose = (workflows) => {
    filter.workflows = workflows;
    before = '';
    history.replaceState(null, '', hashFor('runs', [], hashParams(filter)));
    syncPicker();
    for (const item of capsules(display, [...names], filter.workflows)) capsuleButtons.get(item.id)?.setAttribute('aria-pressed', String(item.pressed));
    loadList();
    loadOpen();
  };

  await Promise.all([loadList(), loadOpen()]);
  const shownCapsules = capsules(display, [...names], filter.workflows);
  for (const item of shownCapsules) {
    const button = element('button', { type: 'button', class: 'capsule', 'aria-pressed': String(item.pressed) },
      item.color ? element('span', { class: `swatch sw-${item.color}`, 'aria-hidden': 'true' }) : null, item.name);
    button.addEventListener('click', () => choose(item.id ? [item.id] : []));
    capsuleButtons.set(item.id, button);
    capsuleRow.append(button);
  }

  const pending = new Map();
  let frame = 0;
  const flush = () => {
    frame = 0;
    const batch = [...pending.values()];
    pending.clear();
    if (signal?.aborted) return;
    const touched = new Set();
    for (const run of batch) {
      for (const key of upsertOpen(run)) touched.add(key);
      if (!before) upsert(run);
    }
    if (touched.size && !before) refreshThreads(touched);
  };
  const upsert = (run) => {
    const existing = tbody.querySelector(`tr[data-run-id="${CSS.escape(String(run.id))}"]`);
    if (existing && (filter.every || !rethreaded(existing.dataset.thread, run))) {
      refreshRow(existing, rowFor(run));
      return;
    }
    // A run whose thread changed mid-run moves into the row for its new thread.
    if (existing) {
      existing.remove();
      counted.delete(String(run.id));
    }
    if (!matches(run, filter, display)) return;
    const id = String(run.id);
    const key = threadOf(run);
    const grouped = !filter.every && key;
    const replaced = grouped ? [...tbody.rows].find((row) => row.dataset.thread === key) : null;
    const last = tbody.lastElementChild;
    // A full table only takes a run newer than its last row, so an older one never pushes out a listed one.
    if (!replaced && last && tbody.rows.length >= (filter.all ? MAX_ROWS : RECENT_ROWS) && !isNewer(run, { started_at: last.dataset.startedAt, id: last.dataset.runId })) return;
    if (replaced) {
      if (!isNewer(run, { started_at: replaced.dataset.startedAt, id: replaced.dataset.runId })) return;
      sizes.set(id, (sizes.get(replaced.dataset.runId) || 1) + (counted.has(id) ? 0 : 1));
      counted.add(id);
      replaced.remove();
    } else if (grouped) {
      const params = listParams({ ...filter, thread: key });
      params.set('limit', '1');
      api(`/api/runs?${params}`).then((page) => {
        const latest = listOf(page, 'runs')[0];
        if (String(latest?.id) === id) receive(latest);
      }).catch(() => {});
    }
    const row = rowFor(run);
    const scrolledPast = window.scrollY > 0 && runTable.getBoundingClientRect().top < 0;
    tbody.prepend(row);
    if (!tableCard.isConnected) emptyCard.replaceWith(tableCard);
    if (scrolledPast) window.scrollBy(0, row.getBoundingClientRect().height);
    while (tbody.rows.length > (filter.all ? MAX_ROWS : RECENT_ROWS)) tbody.lastElementChild.remove();
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
    // Events missed while the stream was down would leave runs in progress that have ended.
    onOpen: loadOpen,
    onFallback: () => every(POLL_MS, async () => {
      await loadOpen();
      if (before) return;
      for (const run of listOf(await api(`/api/runs?${listParams(filter)}`), 'runs').reverse()) receive(run);
    }, signal),
  }, { signal });
}
