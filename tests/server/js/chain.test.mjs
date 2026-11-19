import assert from 'node:assert/strict';
import { test } from 'node:test';

// Just enough DOM for the chain panel to build.
class FakeNode {}
class FakeText extends FakeNode {
  constructor(text) {
    super();
    this.text = text;
  }
}
class FakeElement extends FakeNode {
  constructor(tag) {
    super();
    this.tag = tag;
    this.attributes = {};
    this.children = [];
    this.className = '';
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  addEventListener() {}

  append(...nodes) {
    this.children.push(...nodes);
  }
}
globalThis.Node = FakeNode;
globalThis.document = { createElement: (tag) => new FakeElement(tag), createTextNode: (text) => new FakeText(text), activeElement: null };

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { chainPanel, endLines, replacedBy, threadNote, watchText } = await import(new URL('views/chain.js', STATIC));
const { chained, prTitle } = await import(new URL('views/runs.js', STATIC));

const texts = (node) => (node instanceof FakeText ? [node.text] : (node.children || []).flatMap(texts));
const find = (node, test) => [...(test(node) ? [node] : []), ...(node.children || []).flatMap((child) => find(child, test))];
const step = (id, workflow, status = 'succeeded', extra = {}) => ({ id, workflow, workflow_label: workflow, status, subject: null, started_at: `2026-11-19T1${id}:00:00Z`, ...extra });
const chain = [step(1, 'bug-triage'), step(2, 'bug-approve'), step(3, 'bug-fix'), step(4, 'development', 'succeeded', { subject: 'MEM-1679', verdict: 'blocked', title: 'Unreserve does nothing' })];
const clock = (iso) => `${new Date(iso).getHours()}:${String(new Date(iso).getMinutes()).padStart(2, '0')}`;
const fixWatch = { workflow: 'bug-fix', workflow_label: 'Bug fix', source: 'jira', interval_minutes: 5, next_poll_at: '2026-11-19T18:12:00Z' };
const watchLine = `Watching: Bug fix checks Jira every 5 minutes (next check ${clock(fixWatch.next_poll_at)}).`;
// MEMTEMDIS-1618's shape: two issue steps, then three reviews of the pull request bug-fix opened.
const PR = 'fs-eng/mobile-ios#10787';
const review = (id, verdict, status = 'succeeded') => step(id, 'code-review', status, { kind: 'review', subject: PR, verdict, title: '[MEM-1618] Start VoiceOver' });
const withPull = [step(1, 'bug-triage'), step(2, 'bug-fix'), review(3, 'Changes Recommended'), review(4, null), review(5, 'Approved (incremental)')];
const slot = () => new FakeElement('div');
const signal = new AbortController().signal;
const loaded = (detail) => async () => ({ subject: detail });

test('chained takes an issue\'s own runs and leaves pull request and other threads alone', () => {
  assert.equal(chained({ thread: 'MEM-1679', subject: null }), true);
  assert.equal(chained({ thread: 'MEM-1679', subject: 'MEM-1679' }), true);
  assert.equal(chained({ thread: null, subject: 'MEM-1679' }), true);
  assert.equal(chained({ thread: 'MEM-1679', subject: 'acme/app#7' }), false);
  assert.equal(chained({ thread: 'slack:C1:1700.1', subject: null }), false);
  assert.equal(chained({ thread: null, subject: null }), false);
});

test('in a chain only a newer run of the same workflow replaces a run', () => {
  const approve = chain[1];
  assert.deepEqual(replacedBy(approve, chain, true), []);
  const again = step(5, 'bug-approve');
  assert.deepEqual(replacedBy(approve, [...chain, again], true).map((run) => run.id), [5]);
  assert.deepEqual(replacedBy(approve, chain, false).map((run) => run.id), [3, 4], 'a plain thread counts every newer run');
});

test('the last issue step says why it stopped, and pollers still watching are listed', () => {
  assert.deepEqual(endLines({ subject: 'MEM-1679', runs: chain.slice(0, 2), watching: [fixWatch] }), [watchLine]);
  assert.equal(watchText({ workflow: 'ship', source: 'linear', interval_minutes: 1, next_poll_at: null }), 'Watching: ship checks Linear every minute.');
  assert.deepEqual(endLines({ subject: 'MEM-1679', runs: chain, watching: [] }), ['Stopped: blocked: Unreserve does nothing.']);
  assert.deepEqual(endLines({ subject: 'MEM-1679', runs: chain, watching: [fixWatch] }), ['Stopped: blocked: Unreserve does nothing.', watchLine],
    'a reported verdict stops the chain even while a poller watches');
  const reasoned = { ...chain[3], fields: { reason: 'Nothing changed: no skills in the VM.' } };
  assert.deepEqual(endLines({ subject: 'MEM-1679', runs: [...chain.slice(0, 3), reasoned], watching: [] }), ['Stopped: blocked: Nothing changed: no skills in the VM.']);
  assert.deepEqual(endLines({ subject: 'MEM-1679', runs: [step(1, 'bug-triage', 'failed')] }), ['Stopped: bug-triage failed.']);
  assert.deepEqual(endLines({ subject: 'MEM-1679', runs: [step(1, 'bug-triage', 'succeeded', { verdict: 'not a bug' })], watching: [] }), ['Stopped: not a bug.']);
  assert.deepEqual(endLines({ subject: 'MEM-1679', runs: [...chain.slice(0, 3), step(4, 'development', 'running')], watching: [fixWatch] }), []);
  assert.deepEqual(endLines({ subject: 'MEM-1679', runs: [] }), []);
});

test('a chain with a pull request ends on its state, never on a review\'s verdict', () => {
  const detail = { subject: 'MEM-1618', runs: withPull, watching: [] };
  assert.deepEqual(endLines(detail), ['PR #10787 opened, latest review: approved (incremental).']);
  assert.deepEqual(endLines({ ...detail, runs: withPull.slice(0, 3) }), ['PR #10787 opened, latest review: changes recommended.']);
  assert.deepEqual(endLines({ ...detail, runs: [...withPull, step(6, 'pr-merged', 'succeeded', { kind: 'merge', subject: PR })] }),
    ['PR #10787 merged, latest review: approved (incremental).']);
  assert.deepEqual(endLines({ ...detail, runs: [...withPull.slice(0, 4), review(5, null, 'running')] }), [], 'nothing while a review runs');
  assert.deepEqual(endLines({ ...detail, runs: [...withPull.slice(0, 2), review(3, null, 'queued')] }), []);
});

test('the chain panel lists issue steps in order, then the pull request group, and marks the current run', () => {
  const panel = chainPanel({ subject: 'MEM-1679', runs: chain.slice(0, 2), watching: [fixWatch] }, { id: 2 });
  const steps = find(panel, (node) => node.tag === 'li');
  assert.deepEqual(steps.map((node) => texts(node)[0]), ['bug-triage', 'bug-approve']);
  assert.deepEqual(steps.map((node) => node.attributes['aria-current'] || null), [null, 'step']);
  assert.equal(find(steps[0], (node) => node.tag === 'a')[0].attributes.href, '#/run/1');
  assert.ok(texts(panel).includes(watchLine), texts(panel).join('|'));
  assert.ok(!texts(panel).includes('null') && !texts(panel).includes('undefined'), texts(panel).join('|'));

  const pullUrl = ['https:', '', 'github.com', 'fs-eng', 'mobile-ios', 'pull', '10787'].join('/');
  const pulled = chainPanel({ subject: 'MEM-1618', runs: withPull, watching: [], links: [{ label: 'Issue', url: 'x' }, { label: 'Pull request', url: pullUrl }] }, { id: 1 });
  const [issueList, pullList] = find(pulled, (node) => node.tag === 'ol');
  assert.deepEqual(find(issueList, (node) => node.tag === 'li').map((node) => texts(node)[0]), ['bug-triage', 'bug-fix']);
  assert.equal(find(pullList, (node) => node.tag === 'li').length, 3);
  const [group] = find(pulled, (node) => node.className === 'chain-pull');
  assert.equal(find(group, (node) => node.className === 'out-link')[0].attributes.href, pullUrl);
  const unlinked = chainPanel({ subject: 'MEM-1618', runs: withPull, watching: [], links: [] }, { id: 1 });
  assert.equal(find(find(unlinked, (node) => node.className === 'chain-pull')[0], (node) => node.tag === 'a')[0].attributes.href, '#/subject/fs-eng%2Fmobile-ios%2310787');
  const lines = find(pulled, (node) => node.className === 'chain-next').map((node) => texts(node).join(''));
  assert.deepEqual(lines, ['PR #10787 opened, latest review: approved (incremental).']);
  assert.ok(!texts(pulled).some((text) => text.startsWith('Stopped')));
});

test('threadNote shows a chained run its chain and no replaced banner, even alone', async () => {
  const approve = { ...chain[1], thread: 'MEM-1679' };
  const page = slot();
  await threadNote(page, approve, signal, loaded({ subject: 'MEM-1679', runs: chain, watching: [] }));
  assert.deepEqual(page.children.map((node) => node.className), ['card chain']);
  const lone = slot();
  const triage = { ...step(1, 'bug-triage', 'succeeded', { verdict: 'not a bug' }), thread: 'MEM-7' };
  await threadNote(lone, triage, signal, loaded({ subject: 'MEM-7', runs: [triage], watching: [] }));
  assert.deepEqual(lone.children.map((node) => node.className), ['card chain']);
  assert.ok(texts(lone).includes('Stopped: not a bug.'), texts(lone).join('|'));
  const rerun = slot();
  await threadNote(rerun, approve, signal, loaded({ subject: 'MEM-1679', runs: [...chain, step(5, 'bug-approve')], watching: [] }));
  assert.deepEqual(rerun.children.map((node) => node.className), ['banner warn', 'card chain']);
});

test('threadNote keeps the plain note and banner on a run outside an issue chain', async () => {
  const detail = { subject: 'MEM-1618', title: 'VoiceOver', runs: withPull, watching: [fixWatch] };
  const older = slot();
  await threadNote(older, { ...withPull[2], thread: 'MEM-1618' }, signal, loaded(detail));
  assert.deepEqual(older.children.map((node) => node.className), ['banner warn']);
  assert.ok(texts(older).join('').startsWith('2 later runs replaced this one'), texts(older).join('|'));
  const latest = slot();
  await threadNote(latest, { ...withPull[4], thread: 'MEM-1618' }, signal, loaded(detail));
  assert.deepEqual(latest.children.map((node) => node.tag), ['p']);
  assert.ok(texts(latest).join('').startsWith('This is the latest of 5 runs on'));
  const alone = slot();
  await threadNote(alone, { id: 9, workflow: 'webhook-job', subject: 'acme/app#2', thread: null }, signal, loaded({ subject: 'acme/app#2', runs: [{ id: 9 }] }));
  assert.deepEqual(alone.children, []);
  const stopped = slot();
  const aborted = new AbortController();
  aborted.abort();
  await threadNote(stopped, { ...chain[1], thread: 'MEM-1679' }, aborted.signal, loaded({ subject: 'MEM-1679', runs: chain }));
  assert.deepEqual(stopped.children, []);
});

test('a grouped chain row without a title names its issue, and a pull request row reads as before', () => {
  assert.equal(prTitle({ workflow: 'bug-fix', thread: 'MEM-1679', subject: null }, null, {}), 'MEM-1679');
  assert.equal(prTitle({ workflow: 'development', thread: 'MEM-1679', subject: 'MEM-1679', title: 'Fix unreserve' }, null, {}), 'Fix unreserve');
  assert.equal(prTitle({ workflow: 'code-review', thread: 'MEM-1679', subject: 'acme/app#7' }, null, {}), 'code-review');
  assert.equal(prTitle({ workflow: 'webhook-job', thread: null, subject: null }, null, {}), 'webhook-job');
});
