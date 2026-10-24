import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { chosenWorkflows, hashParams, isHidden, listParams, matches, runLabel, subjectHref, verdictTone, workflowOptions } =
  await import(new URL('views/runs.js', STATIC));
const { hashFor } = await import(new URL('router.js', STATIC));

const display = { workflow_labels: { 'code-review': { label: 'Code review', color: 's1' }, digest: { label: '', hidden: true } } };

test('runLabel prefers the title, then the workflow label', () => {
  assert.equal(runLabel({ title: 'Fix the crash', workflow: 'code-review' }, display), 'Fix the crash');
  assert.equal(runLabel({ workflow: 'code-review' }, display), 'Code review');
  assert.equal(runLabel({ workflow: 'jira-work' }, display), 'jira-work');
  assert.equal(runLabel({}, {}), 'Unnamed workflow');
});

test('subjectHref links the run list filtered to the subject', () => {
  assert.equal(subjectHref('fs-eng/android-tree#12'), '#/runs?subject=fs-eng%2Fandroid-tree%2312');
});

test('verdictTone reads approvals as ok and blocks as bad', () => {
  assert.equal(verdictTone('approve'), 'ok');
  assert.equal(verdictTone('Approval Recommended'), 'ok');
  assert.equal(verdictTone('request_changes'), 'bad');
  assert.equal(verdictTone('block'), 'bad');
  assert.equal(verdictTone('Not approved'), 'bad');
  assert.equal(verdictTone('Changes Recommended'), 'bad');
  assert.equal(verdictTone('push-failed'), 'bad');
  assert.equal(verdictTone('pushed'), 'ok');
  assert.equal(verdictTone('comment'), '');
  assert.equal(verdictTone(null), '');
});

test('workflow choices round-trip through the hash', () => {
  const filter = { team: 't', project: '', status: '', subject: '', workflows: ['a', 'b'], hidden: true };
  const hash = hashFor('runs', [], hashParams(filter));
  assert.equal(hash, '#/runs?team=t&workflows=a%2Cb&hidden=1');
  const query = new URLSearchParams(hash.split('?')[1]);
  assert.deepEqual(chosenWorkflows(query), ['a', 'b']);
  assert.deepEqual(chosenWorkflows(new URLSearchParams('workflows=a,,a, b')), ['a', 'b']);
  assert.deepEqual(chosenWorkflows(new URLSearchParams('')), []);
});

test('listParams asks the API for the chosen workflows and hidden ones', () => {
  const params = listParams({ team: 't', subject: 'MEM-1', workflows: ['a', 'b'], hidden: true }, 'cursor');
  assert.equal(String(params), 'limit=50&team=t&subject=MEM-1&workflows=a%2Cb&include_hidden=1&before=cursor');
  assert.equal(String(listParams({ workflows: [] })), 'limit=50');
});

test('matches hides hidden workflows unless shown or chosen', () => {
  assert.equal(isHidden(display, 'digest'), true);
  assert.equal(isHidden({}, 'digest'), false);
  const base = { workflows: [], hidden: false };
  assert.equal(matches({ workflow: 'digest' }, base, display), false);
  assert.equal(matches({ workflow: 'digest' }, { ...base, hidden: true }, display), true);
  assert.equal(matches({ workflow: 'digest' }, { ...base, workflows: ['digest'] }, display), true);
  assert.equal(matches({ workflow: 'code-review' }, { ...base, workflows: ['digest'] }, display), false);
  assert.equal(matches({ workflow: 'x', subject: 'a' }, { ...base, subject: 'b' }, display), false);
  assert.equal(matches({ workflow: 'x', status: 'failed' }, { ...base, status: 'running' }, display), false);
});

test('workflowOptions merges configured and seen workflows by label', () => {
  assert.deepEqual(workflowOptions(display, ['jira-work', 'code-review', null]), [
    { id: 'code-review', name: 'Code review', hidden: false },
    { id: 'digest', name: 'digest', hidden: true },
    { id: 'jira-work', name: 'jira-work', hidden: false },
  ]);
});
