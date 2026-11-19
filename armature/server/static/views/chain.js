import { api } from '../api.js';
import { element, link, statusText, statusTone, when } from '../dom.js';
import { externalLink } from '../outcome.js';
import { hashFor } from '../router.js';
import { chained, isNewer, subjectHref, verdictChip, verdictText } from './runs.js';
import { subjectPath } from './subject.js';

const ACTIVE = new Set(['queued', 'running', 'waiting']);
const SOURCES = { jira: 'Jira', linear: 'Linear' };
const PULL = /^(?<repo>[^#\s]+\/[^#\s]+)#(?<number>[1-9][0-9]{0,9})$/;

// In a chain only a newer run of the same workflow replaces a run. Other workflows are its next steps.
export const replacedBy = (run, runs, chain = false) => runs.filter((other) => String(other.id) !== String(run.id)
  && isNewer(other, run) && (!chain || other.workflow === run.workflow));

// The issue's own steps, and the runs on each of its pull requests, both in run order.
export function splitChain(detail) {
  const runs = detail?.runs || [];
  const steps = runs.filter((run) => !run.subject || run.subject === detail.subject);
  const pulls = new Map();
  for (const run of runs.filter((candidate) => !steps.includes(candidate))) pulls.set(run.subject, [...(pulls.get(run.subject) || []), run]);
  return { steps, pulls: [...pulls].map(([subject, pullRuns]) => ({ subject, runs: pullRuns })) };
}

const clock = (timestamp) => {
  const date = new Date(timestamp || '');
  return Number.isNaN(date.getTime()) ? null : `${date.getHours()}:${String(date.getMinutes()).padStart(2, '0')}`;
};

export function watchText(watch) {
  const minutes = Number(watch.interval_minutes);
  const every = minutes === 1 ? 'every minute' : `every ${minutes} minutes`;
  const next = clock(watch.next_poll_at);
  return `Watching: ${watch.workflow_label || watch.workflow} checks ${SOURCES[watch.source] || watch.source} ${every}${next ? ` (next check ${next})` : ''}.`;
}

export function pullText(pull) {
  const number = PULL.exec(pull.subject || '')?.groups.number;
  const merged = pull.runs.some((run) => run.kind === 'merge');
  const review = [...pull.runs].reverse().find((run) => run.kind === 'review' && run.verdict);
  return `${number ? `PR #${number}` : pull.subject} ${merged ? 'merged' : 'opened'}, ${review ? `latest review: ${verdictText(review.verdict).toLowerCase()}` : 'no review yet'}.`;
}

function stoppedText(last) {
  const reason = [last.verdict, last.fields?.reason || last.title].filter(Boolean).join(': ').replace(/[.\s]+$/, '');
  return `Stopped: ${reason || `${last.workflow_label || last.workflow || 'the last run'} ${statusText(last.status).toLowerCase()}`}.`;
}

// How the chain ends, once no run in it is going: each pull request's state, else why its last step stopped.
export function endLines(detail) {
  const { steps, pulls } = splitChain(detail);
  if ([...steps, ...pulls.flatMap((pull) => pull.runs)].some((run) => ACTIVE.has(run.status))) return [];
  const watching = (detail?.watching || []).map(watchText);
  if (pulls.length) return [...pulls.map(pullText), ...watching];
  const last = steps.at(-1);
  if (!last) return watching;
  return last.verdict || last.fields?.reason || !watching.length ? [stoppedText(last), ...watching] : watching;
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

// The server's own link to the pull request, else its page here.
function pullLink(pull, links) {
  const match = PULL.exec(pull.subject || '');
  const found = match && (links || []).find((item) => item?.label === 'Pull request' && typeof item.url === 'string'
    && item.url.replace(/\/$/, '').endsWith(`/${match.groups.repo}/pull/${match.groups.number}`));
  return found ? externalLink({ label: pull.subject, url: found.url }) : link(subjectHref(pull.subject), pull.subject);
}

function pullGroup(pull, current, links) {
  return element('div', { class: 'chain-pull' }, element('h3', {}, 'Pull request ', pullLink(pull, links)),
    element('ol', { class: 'chain-steps' }, pull.runs.map((run) => step(run, current))));
}

export function chainPanel(detail, current) {
  const thread = detail.subject;
  const { steps, pulls } = splitChain(detail);
  return element('section', { class: 'card chain', 'aria-label': `Runs on ${thread}` },
    element('h2', { class: 'inline' }, 'Runs on ', link(subjectHref(thread), thread)),
    element('ol', { class: 'chain-steps' }, steps.map((run) => step(run, current))),
    pulls.map((pull) => pullGroup(pull, current, detail.links)),
    endLines(detail).map((line) => element('p', { class: 'chain-next' }, line)));
}

// Where this run sits in its thread: its issue's chain, or a pointer to the newest run when a later one replaced it.
export async function threadNote(slot, run, signal, load = (thread) => api(subjectPath(thread))) {
  const thread = run.thread || run.subject;
  let detail;
  try {
    detail = (await load(thread))?.subject;
  } catch {
    return;
  }
  const others = (detail?.runs || []).filter((other) => String(other.id) !== String(run.id));
  const chain = chained(run);
  if (signal?.aborted || !detail || !(others.length || chain)) return;
  const newer = replacedBy(run, others, chain);
  if (newer.length) {
    slot.append(element('div', { class: 'banner warn' }, `${newer.length === 1 ? 'A later run' : `${newer.length} later runs`} replaced this one, so its result may be out of date. `,
      link(subjectHref(thread), 'See the latest')));
  }
  if (chain) slot.append(chainPanel(detail, run));
  else if (!newer.length) slot.append(element('p', { class: 'note' }, `This is the latest of ${others.length + 1} runs on `, link(subjectHref(thread), detail.title || thread), '.'));
}
