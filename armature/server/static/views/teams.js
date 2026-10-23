import { api, listOf } from '../api.js';
import { attempt, card, clear, element, empty, errorBox, link, loading, meta, pageHeader, table } from '../dom.js';
import { hashFor } from '../router.js';
import { canManageTeam, isAdmin } from '../session.js';
import { providersSection } from './providers.js';
import { runnerKeysSection } from './runnerkeys.js';

const PLATFORMS = ['ios', 'android', 'backend', 'dotnet'];

function addProjectForm(team, reload) {
  const nameInput = element('input', { type: 'text', id: `project-name-${team.id}`, required: true, maxlength: 100, autocapitalize: 'none', spellcheck: 'false', placeholder: 'mobile-ios' });
  const platformInput = element('input', { type: 'text', id: `project-platform-${team.id}`, required: true, maxlength: 32, list: 'platforms', autocapitalize: 'none', spellcheck: 'false' });
  const addButton = element('button', { type: 'submit' }, 'Add project');
  const status = element('div', { role: 'status' });
  const form = element('form', { class: 'wf-actions' },
    element('label', { for: nameInput.id }, 'Project'), nameInput, element('label', { for: platformInput.id }, 'Platform'), platformInput, addButton);
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const added = await attempt(status, addButton, () => api(`/api/teams/${encodeURIComponent(team.id)}/projects`, {
      method: 'POST', body: { name: nameInput.value.trim(), platform: platformInput.value.trim() },
    }));
    if (added) reload();
  });
  return element('div', {}, form, status);
}

function teamCard(team, projects, providers, secretNames, reload) {
  const canManage = canManageTeam(team.id);
  return card(
    element('h2', { class: 'inline' }, team.name),
    projects.length ? table(['Project', 'Platform'], projects.map((project) => [
      link(hashFor('project', [project.id]), project.name), project.platform || '—',
    ])) : empty('No projects yet.'),
    canManage ? addProjectForm(team, reload) : null,
    providersSection(team, providers, { canManage, secretNames, reload }),
    canManage ? runnerKeysSection(team.id) : null,
  );
}

function addTeamForm(reload) {
  const nameInput = element('input', { type: 'text', id: 'new-team-name', required: true, maxlength: 60 });
  const addButton = element('button', { type: 'submit', class: 'primary' }, 'Add team');
  const status = element('div', { role: 'status' });
  const form = element('form', { class: 'wf-actions' }, element('label', { for: nameInput.id }, 'Name'), nameInput, addButton);
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    if (await attempt(status, addButton, () => api('/api/teams', { method: 'POST', body: { name: nameInput.value.trim() } }))) reload();
  });
  return card(element('h2', { class: 'inline' }, 'Add a team'), form, status);
}

async function list(root) {
  root.append(pageHeader('Teams'));
  root.append(element('datalist', { id: 'platforms' }, PLATFORMS.map((platform) => element('option', { value: platform }))));
  const body = element('div', {}, loading());
  root.append(body);

  async function reload() {
    let teams;
    try {
      teams = listOf(await api('/api/teams'), 'teams');
    } catch (err) {
      clear(body).append(errorBox(err));
      return;
    }
    const projectLists = await Promise.all(teams.map((team) => api(`/api/teams/${encodeURIComponent(team.id)}/projects`)
      .then((payload) => listOf(payload, 'projects')).catch(() => [])));
    // Project-only members cannot read a team's providers, so a refusal just hides the section.
    const providerLists = await Promise.all(teams.map((team) => api(`/api/teams/${encodeURIComponent(team.id)}/providers`)
      .then((payload) => listOf(payload, 'providers')).catch(() => [])));
    const instanceSecrets = isAdmin() ? await api('/api/secrets').then((payload) => listOf(payload, 'secrets')).catch(() => []) : [];
    const secretLists = await Promise.all(teams.map((team) => (canManageTeam(team.id)
      ? api(`/api/teams/${encodeURIComponent(team.id)}/secrets`).then((payload) => listOf(payload, 'secrets')).catch(() => [])
      : Promise.resolve([]))));
    clear(body);
    if (!teams.length) body.append(card(empty(isAdmin() ? 'No teams yet. Add the first one below.' : 'You are not on any team yet.')));
    teams.forEach((team, index) => {
      const secretNames = [...new Set([...secretLists[index], ...instanceSecrets].map((secret) => secret.name))].sort();
      body.append(teamCard(team, projectLists[index], providerLists[index], secretNames, reload));
    });
    if (isAdmin()) body.append(addTeamForm(reload));
  }

  await reload();
}

async function project(root, projectId) {
  let detail;
  try {
    detail = await api(`/api/projects/${encodeURIComponent(projectId)}`);
  } catch (err) {
    root.append(pageHeader('Project'), errorBox(err));
    return;
  }
  const projectRecord = detail?.project ?? detail;
  root.append(pageHeader(projectRecord.name || 'Project'));
  root.append(meta(projectRecord.platform ? `Platform ${projectRecord.platform}` : null, link('#/teams', 'All teams')));
  root.append(card(element('p', {}, link(hashFor('runs', [], { team: projectRecord.team_id, project: projectRecord.id }), 'See this project’s runs'))));
}

export async function render(root, { name, args }) {
  if (name === 'project' && args[0]) return project(root, args[0]);
  return list(root);
}
