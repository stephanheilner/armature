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
const { chainPanel, nextLines, replacedBy, watchText } = await import(new URL('views/chain.js', STATIC));
const { chained, prTitle } = await import(new URL('views/runs.js', STATIC));

const texts = (node) => (node instanceof FakeText ? [node.text] : (node.children || []).flatMap(texts));
const find = (node, test) => [...(test(node) ? [node] : []), ...(node.children || []).flatMap((child) => find(child, test))];
const step = (id, workflow, status = 'succeeded', extra = {}) => ({ id, workflow, workflow_label: workflow, status, started_at: `2026-11-19T1${id}:00:00Z`, ...extra });
const chain = [step(1, 'bug-triage'), step(2, 'bug-approve'), step(3, 'bug-fix'), step(4, 'development', 'succeeded', { verdict: 'blocked', title: 'Unreserve does nothing' })];
const clock = (iso) => `${new Date(iso).getHours()}:${String(new Date(iso).getMinutes()).padStart(2, '0')}`;
const fixWatch = { workflow: 'bug-fix', workflow_label: 'Bug fix', source: 'jira', interval_minutes: 5, next_poll_at: '2026-11-19T18:12:00Z' };

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

test('the newest finished run says which poller is next, else why the chain stopped', () => {
  const handedOff = { runs: chain.slice(0, 2), watching: [fixWatch] };
  assert.deepEqual(nextLines(handedOff), [`Next: Bug fix checks Jira every 5 minutes (next check ${clock(fixWatch.next_poll_at)}).`]);
  assert.equal(watchText({ workflow: 'ship', source: 'linear', interval_minutes: 1, next_poll_at: null }), 'Next: ship checks Linear every minute.');
  assert.deepEqual(nextLines({ runs: chain, watching: [] }), ['Stopped: blocked: Unreserve does nothing.']);
  assert.deepEqual(nextLines({ runs: [step(1, 'bug-triage', 'failed')] }), ['Stopped: bug-triage failed.']);
  const reasoned = step(4, 'development', 'succeeded', { verdict: 'blocked', title: 'Unreserve does nothing', fields: { reason: 'Nothing changed: no skills in the VM.' } });
  assert.deepEqual(nextLines({ runs: [...chain.slice(0, 3), reasoned], watching: [] }), ['Stopped: blocked: Nothing changed: no skills in the VM.']);
  assert.deepEqual(nextLines({ runs: [...chain.slice(0, 3), step(4, 'development', 'running')], watching: [fixWatch] }), []);
  assert.deepEqual(nextLines({ runs: [] }), []);
});

test('the chain panel lists every run in order, marks the current one, and says what comes next', () => {
  const panel = chainPanel({ subject: 'MEM-1679', runs: chain.slice(0, 2), watching: [fixWatch] }, { id: 2 });
  const steps = find(panel, (node) => node.tag === 'li');
  assert.deepEqual(steps.map((node) => texts(node)[0]), ['bug-triage', 'bug-approve']);
  assert.deepEqual(steps.map((node) => node.attributes['aria-current'] || null), [null, 'step']);
  assert.equal(find(steps[0], (node) => node.tag === 'a')[0].attributes.href, '#/run/1');
  assert.ok(texts(panel).some((text) => text.startsWith('Next: Bug fix checks Jira')), texts(panel).join('|'));
  assert.ok(!texts(panel).includes('null') && !texts(panel).includes('undefined'), texts(panel).join('|'));
  const done = chainPanel({ subject: 'MEM-1679', runs: chain, watching: [] }, { id: 4 });
  assert.equal(find(done, (node) => node.tag === 'li').length, 4);
  assert.ok(texts(done).includes('Stopped: blocked: Unreserve does nothing.'));
});

test('a grouped chain row without a title names its issue, and a pull request row reads as before', () => {
  assert.equal(prTitle({ workflow: 'bug-fix', thread: 'MEM-1679', subject: null }, null, {}), 'MEM-1679');
  assert.equal(prTitle({ workflow: 'development', thread: 'MEM-1679', subject: 'MEM-1679', title: 'Fix unreserve' }, null, {}), 'Fix unreserve');
  assert.equal(prTitle({ workflow: 'code-review', thread: 'MEM-1679', subject: 'acme/app#7' }, null, {}), 'code-review');
  assert.equal(prTitle({ workflow: 'webhook-job', thread: null, subject: null }, null, {}), 'webhook-job');
});
