import { api, listOf } from '../api.js';
import { append, card, clear, duration, element, empty, errorBox, link, loading, money, pageHeader, secondsBetween, statusChip, statusText, table, when } from '../dom.js';
import { hashFor } from '../router.js';

const STATUSES = ['queued', 'running', 'succeeded', 'failed', 'canceled'];
const PAGE_SIZE = 50;

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

export async function render(root, { query }) {
  root.append(pageHeader('Runs'));
  const projectsOf = projectLoader();
  const teamSelect = element('select', { name: 'team' });
  const projectSelect = element('select', { name: 'project' });
  const statusSelect = element('select', { name: 'status' }, element('option', { value: '' }, 'Any status'),
    STATUSES.map((status) => element('option', { value: status }, statusText(status))));
  statusSelect.value = STATUSES.includes(query.get('status')) ? query.get('status') : '';
  const filters = element('form', { class: 'filters', role: 'search', 'aria-label': 'Filter runs' },
    labelled('Team', teamSelect), labelled('Project', projectSelect), labelled('Status', statusSelect), element('button', { type: 'submit' }, 'Apply'));
  const body = element('div', {}, loading());
  root.append(filters, body);

  const teams = await api('/api/teams').then((payload) => listOf(payload, 'teams')).catch(() => []);
  const teamName = new Map(teams.map((team) => [String(team.id), team.name]));
  fillOptions(teamSelect, 'All teams', teams, query.get('team') || '');
  fillOptions(projectSelect, 'All projects', teamSelect.value ? await projectsOf(teamSelect.value) : [], query.get('project') || '');
  teamSelect.addEventListener('change', async () => {
    fillOptions(projectSelect, 'All projects', teamSelect.value ? await projectsOf(teamSelect.value) : [], '');
  });
  filters.addEventListener('submit', (submitEvent) => {
    submitEvent.preventDefault();
    location.hash = hashFor('runs', [], { team: teamSelect.value, project: projectSelect.value, status: statusSelect.value });
  });

  const filter = { team: teamSelect.value, project: projectSelect.value, status: statusSelect.value };
  const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
  for (const [key, value] of Object.entries({ ...filter, before: query.get('before') || '' })) if (value) params.set(key, value);
  let page;
  try {
    page = await api(`/api/runs?${params}`);
  } catch (err) {
    clear(body).append(errorBox(err));
    return;
  }
  const runs = listOf(page, 'runs');
  const projectName = new Map();
  for (const teamProjects of await Promise.all([...new Set(runs.map((run) => String(run.team_id)))].map(projectsOf))) {
    for (const project of teamProjects) projectName.set(String(project.id), project.name);
  }
  clear(body);
  if (!runs.length) {
    const isFiltered = Object.values(filter).some(Boolean) || query.get('before');
    body.append(card(empty(isFiltered ? 'No runs match these filters. Clear a filter to see more.' : 'No runs yet.')));
    return;
  }
  body.append(card(table(['Started', 'Workflow', 'Team', 'Project', 'Status', 'Triggered by', ['Time', 'num'], ['Cost', 'num']], runs.map((run) => [
    element('span', { title: run.started_at || null }, when(run.started_at)), run.workflow || '—',
    teamName.get(String(run.team_id)) || '—', projectName.get(String(run.project_id)) || '—',
    statusChip(run.status), run.triggered_by || '—', elapsed(run), money(run.cost_usd),
  ]))));
  const pager = element('nav', { class: 'pager', 'aria-label': 'Pages' });
  if (query.get('before')) pager.append(link(hashFor('runs', [], filter), 'Newest'));
  if (page.next_before) pager.append(link(hashFor('runs', [], { ...filter, before: page.next_before }), 'Older'));
  if (pager.firstChild) body.append(pager);
}
