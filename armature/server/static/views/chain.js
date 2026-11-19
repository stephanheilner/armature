import { element, link, statusText, statusTone, when } from '../dom.js';
import { hashFor } from '../router.js';
import { isNewer, subjectHref, verdictChip } from './runs.js';

const ACTIVE = new Set(['queued', 'running', 'waiting']);
const SOURCES = { jira: 'Jira', linear: 'Linear' };

// In a chain only a newer run of the same workflow replaces a run. Other workflows are its next steps.
export const replacedBy = (run, runs, chain = false) => runs.filter((other) => String(other.id) !== String(run.id)
  && isNewer(other, run) && (!chain || other.workflow === run.workflow));

const clock = (timestamp) => {
  const date = new Date(timestamp || '');
  return Number.isNaN(date.getTime()) ? null : `${date.getHours()}:${String(date.getMinutes()).padStart(2, '0')}`;
};

export function watchText(watch) {
  const minutes = Number(watch.interval_minutes);
  const every = minutes === 1 ? 'every minute' : `every ${minutes} minutes`;
  const next = clock(watch.next_poll_at);
  return `Next: ${watch.workflow_label || watch.workflow} checks ${SOURCES[watch.source] || watch.source} ${every}${next ? ` (next check ${next})` : ''}.`;
}

// What follows the newest run: the pollers that may still pick the issue up, else why the chain stopped.
export function nextLines(detail) {
  const last = (detail?.runs || []).at(-1);
  if (!last || ACTIVE.has(last.status)) return [];
  const watching = detail.watching || [];
  if (watching.length) return watching.map(watchText);
  const reason = [last.verdict, last.title].filter(Boolean).join(': ');
  return [`Stopped: ${reason || `${last.workflow_label || last.workflow || 'the last run'} ${statusText(last.status).toLowerCase()}`}.`];
}

function step(run, current) {
  const here = String(run.id) === String(current.id);
  return element('li', { class: `chain-step${here ? ' chain-current' : ''}`, 'aria-current': here ? 'step' : null },
    link(hashFor('run', [run.id]), run.workflow_label || run.workflow || 'Run'), ' ',
    element('span', { class: `state state-${statusTone(run.status)}` }, statusText(run.status)), ' ',
    verdictChip(run.verdict), ' ',
    element('span', { class: 'muted' }, when(run.started_at)),
    run.title ? element('span', { class: 'chain-title' }, run.title) : null);
}

export function chainPanel(detail, current) {
  const thread = detail.subject;
  return element('section', { class: 'card chain', 'aria-label': `Runs on ${thread}` },
    element('h2', { class: 'inline' }, 'Runs on ', link(subjectHref(thread), thread)),
    element('ol', { class: 'chain-steps' }, (detail.runs || []).map((run) => step(run, current))),
    nextLines(detail).map((line) => element('p', { class: 'chain-next' }, line)));
}
