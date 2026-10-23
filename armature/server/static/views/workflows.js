import { api, listOf } from '../api.js';
import { card, element, empty, errorBox, link, meta, pageHeader, table, when } from '../dom.js';
import { hashFor } from '../router.js';

export async function teamNames() {
  const teams = await api('/api/teams').then((payload) => listOf(payload, 'teams')).catch(() => []);
  return new Map(teams.map((team) => [String(team.id), team.name]));
}

export const scopeLabel = (teamId, names) => (teamId ? names.get(String(teamId)) || 'Team' : 'Instance');

function lintLabel(lintOk) {
  if (lintOk === true) return element('span', { class: 'state state-ok' }, 'Clean');
  if (lintOk === false) return element('span', { class: 'state state-bad' }, 'Issues');
  return element('span', { class: 'muted' }, 'Not linted');
}

// Stored text is shown as text, never parsed as Markdown or HTML.
export function sourceCard(title, text) {
  return card(element('h2', { class: 'inline' }, title), element('pre', { class: 'source', tabindex: 0 }, text ?? ''));
}

export function versionsCard(versions) {
  return card(element('h2', { class: 'inline' }, 'Versions'), versions.length ? table(['Version', 'Author', 'Saved', 'Note'], versions.map((version) => [
    String(version.version), version.author || '—', when(version.created_at), element('span', { class: 'pre-line' }, version.note || ''),
  ])) : empty('No versions yet.'));
}

async function list(root) {
  root.append(pageHeader('Workflows'));
  let workflows;
  try {
    workflows = listOf(await api('/api/workflows'), 'workflows');
  } catch (err) {
    root.append(errorBox(err));
    return;
  }
  const names = await teamNames();
  root.append(card(workflows.length ? table(['Workflow', 'Scope', ['Version', 'num'], 'Lint', 'Updated'], workflows.map((workflow) => [
    link(hashFor('workflow', [workflow.id]), workflow.name), scopeLabel(workflow.team_id, names), String(workflow.current_version),
    lintLabel(workflow.lint_ok), when(workflow.updated_at),
  ])) : empty('No workflows yet.')));
}

async function detail(root, workflowId) {
  let payload;
  try {
    payload = await api(`/api/workflows/${encodeURIComponent(workflowId)}`);
  } catch (err) {
    root.append(pageHeader('Workflow'), errorBox(err));
    return;
  }
  const { workflow, version } = payload;
  const names = await teamNames();
  root.append(pageHeader(workflow.name));
  root.append(meta(scopeLabel(workflow.team_id, names), `Version ${workflow.current_version}`, lintLabel(workflow.lint_ok), link('#/workflows', 'All workflows')));
  root.append(version ? sourceCard('YAML', version.body) : card(empty('This workflow has no saved version.')));
  root.append(versionsCard(listOf(payload, 'versions')));
}

export async function render(root, { name, args }) {
  if (name === 'workflow' && args[0]) return detail(root, args[0]);
  return list(root);
}
