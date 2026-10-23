import { api, listOf } from '../api.js';
import { card, element, empty, errorBox, link, meta, pageHeader, table, tabs, when } from '../dom.js';
import { hashFor } from '../router.js';
import { scopeLabel, sourceCard, teamNames, versionsCard } from './workflows.js';

const KINDS = [{ id: 'role', label: 'Roles' }, { id: 'prompt', label: 'Prompts' }];
const KIND_LABEL = { role: 'Role', prompt: 'Prompt', mission: 'Mission' };

async function list(root, kindArg) {
  root.append(pageHeader('Roles and prompts'));
  const kind = KINDS.some((candidate) => candidate.id === kindArg) ? kindArg : KINDS[0].id;
  const { tablist, panel } = tabs('#/roles', KINDS, kind, { label: 'Kind' });
  root.append(tablist, panel);
  let documents;
  try {
    documents = listOf(await api(`/api/documents?kind=${encodeURIComponent(kind)}`), 'documents');
  } catch (err) {
    panel.append(errorBox(err));
    return;
  }
  const names = await teamNames();
  panel.append(card(documents.length ? table([KIND_LABEL[kind], 'Scope', ['Version', 'num'], 'Updated'], documents.map((doc) => [
    link(hashFor('document', [doc.id]), doc.key), scopeLabel(doc.team_id, names), String(doc.current_version), when(doc.updated_at),
  ])) : empty(kind === 'role' ? 'No roles yet.' : 'No prompts yet.')));
}

async function detail(root, documentId) {
  let payload;
  try {
    payload = await api(`/api/documents/${encodeURIComponent(documentId)}`);
  } catch (err) {
    root.append(pageHeader('Document'), errorBox(err));
    return;
  }
  const { document: doc, version } = payload;
  const names = await teamNames();
  root.append(pageHeader(doc.key));
  root.append(meta(KIND_LABEL[doc.kind] || doc.kind, scopeLabel(doc.team_id, names), `Version ${doc.current_version}`,
    link(hashFor('roles', [doc.kind === 'prompt' ? 'prompt' : 'role']), 'All roles and prompts')));
  root.append(version ? sourceCard('Markdown', version.body) : card(empty('This document has no saved version.')));
  root.append(versionsCard(listOf(payload, 'versions')));
}

export async function render(root, { name, args }) {
  if (name === 'document' && args[0]) return detail(root, args[0]);
  return list(root, args[0]);
}
