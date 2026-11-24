import assert from 'node:assert/strict';
import { test } from 'node:test';

// Just enough DOM for the Runs page to build, list, and take stream events.
class FakeNode {
  remove() {
    if (!this.parent) return;
    this.parent.children.splice(this.parent.children.indexOf(this), 1);
    this.parent = null;
  }

  replaceWith(node) {
    const { parent } = this;
    if (!parent) return;
    node.remove();
    parent.children[parent.children.indexOf(this)] = node;
    node.parent = parent;
    this.parent = null;
  }

  get isConnected() {
    let node = this;
    while (node.parent) node = node.parent;
    return node === page;
  }
}
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
    this.dataset = {};
    this.classList = { add: (name) => { this.className = `${this.className} ${name}`.trim(); } };
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return this.attributes[name] ?? null;
  }

  addEventListener() {}

  contains() {
    return false;
  }

  getBoundingClientRect() {
    return { top: 0, height: 0 };
  }

  get firstChild() {
    return this.children[0] || null;
  }

  removeChild(node) {
    node.remove();
  }

  append(...nodes) {
    for (const node of nodes) {
      node.remove();
      node.parent = this;
      this.children.push(node);
    }
  }

  prepend(node) {
    this.insertBefore(node, this.children[0] || null);
  }

  insertBefore(node, ref) {
    node.remove();
    node.parent = this;
    this.children.splice(ref ? this.children.indexOf(ref) : this.children.length, 0, node);
  }

  replaceChildren(...nodes) {
    for (const node of [...this.children]) node.remove();
    this.append(...nodes);
  }

  set textContent(text) {
    this.replaceChildren(new FakeText(text));
  }

  get textContent() {
    return texts(this).join('');
  }

  get lastElementChild() {
    return this.children.filter((node) => node instanceof FakeElement).at(-1) || null;
  }

  get rows() {
    return this.children.filter((node) => node.tag === 'tr');
  }

  get cells() {
    return this.children.filter((node) => node.tag === 'td');
  }

  get options() {
    return this.children.filter((node) => node.tag === 'option');
  }

  get selectedOptions() {
    return [];
  }

  all() {
    return this.children.flatMap((node) => (node instanceof FakeElement ? [node, ...node.all()] : []));
  }

  querySelectorAll(selector) {
    const runId = /data-run-id="([^"]*)"/.exec(selector)?.[1];
    const [tagName, className] = selector.replace(/\[.*\]/, '').split('.');
    return this.all().filter((node) => (!tagName || node.tag === tagName) && (!className || node.className.split(' ').includes(className))
      && (runId === undefined || node.dataset.runId === runId));
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}
const texts = (node) => (node instanceof FakeText ? [node.text] : (node.children || []).flatMap(texts));
const page = new FakeElement('body');

const frames = [];
const sources = [];
globalThis.Node = FakeNode;
globalThis.document = { createElement: (tag) => new FakeElement(tag), createTextNode: (text) => new FakeText(text), activeElement: null };
globalThis.window = { scrollY: 0, scrollBy: () => {} };
globalThis.location = { hash: '' };
globalThis.history = { replaceState: () => {} };
globalThis.CSS = { escape: (value) => value };
globalThis.requestAnimationFrame = (callback) => frames.push(callback);
globalThis.EventSource = class {
  static CLOSED = 2;

  constructor(path) {
    this.path = path;
    this.listeners = {};
    sources.push(this);
  }

  addEventListener(name, listener) {
    this.listeners[name] = listener;
  }

  close() {}
};

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { render } = await import(new URL('views/runs.js', STATIC));

const run = (id, status, extra = {}) => ({ id, status, thread: 'MEM-1', subject: 'MEM-1', workflow: 'pr-fix', started_at: `2026-11-20T10:${String(id).padStart(2, '0')}:00Z`, ...extra });

const threadKey = (listed) => listed.thread || listed.subject;
const ended = (listed) => ['merged', 'closed'].includes(listed.pr?.state);

// Renders the page with answers that filter like the server: history leaves out threads with a live run and splits on
// the pull request's state. live defaults to the open runs, and differs when a test needs the open-runs answer stale.
async function mount(t, open, history, { failing = { open: false }, live = open, display = {} } = {}) {
  const asked = [];
  globalThis.fetch = async (path) => {
    asked.push(path);
    const query = new URLSearchParams(path.split('?')[1] || '');
    if (path.startsWith('/api/runs') && query.get('status') && failing.open) return { status: 500, json: async () => ({ error: 'boom' }) };
    const listed = () => history.filter((item) => !live.some((other) => threadKey(other) === threadKey(item))
      && ended(item) === (query.get('pr') === 'done') && (!query.get('thread') || threadKey(item) === query.get('thread')));
    let payload = {};
    if (path.startsWith('/api/runs')) payload = { runs: query.get('status') ? open : listed(), next_before: null };
    if (path.startsWith('/api/display')) payload = { display };
    return { status: 200, json: async () => payload };
  };
  const controller = new AbortController();
  t.after(() => controller.abort());
  const root = new FakeElement('main');
  page.append(root);
  await render(root, { query: new URLSearchParams(''), signal: controller.signal });
  const source = sources.at(-1);
  const section = (title) => root.children[root.children.findIndex((node) => node.tag === 'h2' && node.textContent === title) + 1];
  return {
    asked,
    section,
    rows: (title) => section(title).querySelectorAll('tr').filter((row) => row.dataset.runId),
    outcome: (row) => row.querySelector('span.state').textContent,
    send: async (...runs) => {
      for (const sent of runs) source.listeners.run({ data: JSON.stringify(sent) });
      for (const frame of frames.splice(0)) frame();
      await settle();
    },
    reconnect: async () => {
      source.listeners.open();
      await settle();
    },
  };
}

async function settle() {
  for (let turn = 0; turn < 5; turn += 1) await new Promise((resolve) => setImmediate(resolve));
  for (const frame of frames.splice(0)) frame();
  for (let turn = 0; turn < 5; turn += 1) await new Promise((resolve) => setImmediate(resolve));
}

const fix = run(1, 'running');
const review = run(2, 'succeeded', { workflow: 'code-review', verdict: 'approve', group_size: 2 });

const ids = (listed) => listed.map((row) => row.dataset.runId);

// A run as the stream sends it, with exactly the fields ingest.list_row gives it.
const streamed = (listed) => Object.fromEntries(['id', 'team_id', 'project_id', 'workflow', 'kind', 'status', 'triggered_by', 'started_at',
  'finished_at', 'cost_usd', 'subject', 'title', 'verdict', 'links', 'thread', 'outcome', 'risk', 'trigger', 'location', 'host_cancel']
  .map((key) => [key, listed[key] ?? null]));

test('a thread with a live run lists only in progress, and moves to Recent once the run ends', async (t) => {
  const open = [fix];
  const { section, rows, outcome, send } = await mount(t, open, [review]);
  assert.deepEqual(ids(rows('In progress')), ['1']);
  assert.deepEqual(rows('Recent'), []);
  assert.deepEqual(rows('Completed'), []);

  open.splice(0);
  await send({ ...fix, status: 'succeeded', finished_at: '2026-11-20T10:05:00Z' });

  assert.deepEqual(rows('In progress'), []);
  assert.equal(section('In progress').textContent, 'Nothing is in progress.');
  assert.deepEqual(rows('Recent').map((row) => [row.dataset.runId, outcome(row)]), [['2', 'Approved']]);
  assert.deepEqual(rows('Completed'), []);
});

test('a run that starts takes its thread out of Recent and never lists there itself', async (t) => {
  const open = [];
  const { rows, send } = await mount(t, open, [review]);
  assert.deepEqual(ids(rows('Recent')), ['2']);

  const started = run(3, 'running');
  open.push(started);
  const sent = send(started);
  assert.deepEqual(rows('Recent'), []);
  await sent;
  assert.deepEqual(ids(rows('In progress')), ['3']);
  assert.deepEqual(rows('Recent'), []);

  const other = send(run(4, 'queued', { thread: 'MEM-4', subject: 'MEM-4' }));
  assert.deepEqual(ids(rows('In progress')), ['4', '3']);
  assert.deepEqual(rows('Recent'), []);
  await other;
});

test('a fetch that still lists a run the stream reported ended neither brings it back nor drops its thread', async (t) => {
  const { asked, rows, outcome, send, reconnect } = await mount(t, [fix], [review]);
  await send({ ...fix, status: 'succeeded', finished_at: '2026-11-20T10:05:00Z' });
  assert.deepEqual(rows('In progress'), []);
  assert.deepEqual(rows('Recent').map((row) => [row.dataset.runId, outcome(row)]), [['1', 'Done']]);

  const openFetches = asked.filter((path) => path.includes('status=')).length;
  await reconnect();
  assert.equal(asked.filter((path) => path.includes('status=')).length, openFetches + 1);
  assert.deepEqual(rows('In progress'), []);
  assert.deepEqual(rows('Recent').map((row) => [row.dataset.runId, outcome(row)]), [['1', 'Done']]);
});

test('a history fetch that still lists a thread now in progress leaves it to In progress', async (t) => {
  const { rows, reconnect } = await mount(t, [fix], [review], { live: [] });
  assert.deepEqual(ids(rows('In progress')), ['1']);
  assert.deepEqual(rows('Recent'), []);
  await reconnect();
  assert.deepEqual(ids(rows('In progress')), ['1']);
  assert.deepEqual(rows('Recent'), []);
});

test('a merged pull request lists in Completed as Merged and a closed one as Closed', async (t) => {
  const merged = run(5, 'succeeded', { thread: null, subject: 'acme/app#5', workflow: 'code-review', pr: { verdict: 'approve', merged: true, state: 'merged' } });
  const closed = run(6, 'succeeded', { thread: null, subject: 'acme/app#6', workflow: 'code-review', pr: { verdict: 'Approved (incremental)', merged: false, state: 'closed' } });
  const { rows, outcome, section } = await mount(t, [], [review, closed, merged]);
  assert.deepEqual(ids(rows('Recent')), ['2']);
  assert.deepEqual(rows('Completed').map((row) => [row.dataset.runId, outcome(row)]), [['6', 'Closed'], ['5', 'Merged']]);
  assert.match(section('Completed').querySelector('p').textContent, /See all/);
});

test('a merge run moves its pull request from Recent to Completed even when the merge workflow is hidden', async (t) => {
  const pull = run(7, 'succeeded', { thread: null, subject: 'acme/app#7', workflow: 'code-review', pr: { verdict: 'approve', merged: false, state: 'open' } });
  const history = [pull];
  const display = { workflow_labels: { 'pr-merged': { label: 'Merged', hidden: true } } };
  const { asked, rows, outcome, send } = await mount(t, [], history, { display });
  assert.deepEqual(ids(rows('Recent')), ['7']);

  const listFetches = asked.filter((path) => path.includes('pr=')).length;
  const merge = streamed(run(8, 'succeeded', { thread: null, subject: 'acme/app#7', workflow: 'pr-merged', kind: 'merge' }));
  history[0] = { ...pull, pr: { ...pull.pr, merged: true, state: 'merged' } };
  await send(merge);

  assert.equal(asked.filter((path) => path.includes('pr=')).length, listFetches + 2);
  assert.deepEqual(rows('Recent'), []);
  assert.deepEqual(rows('Completed').map((row) => [row.dataset.runId, outcome(row)]), [['7', 'Merged']]);
});

test('a merge run that ends before the server stores it lists its pull request in Completed on the late reload', async (t) => {
  const pull = run(7, 'succeeded', { thread: null, subject: 'acme/app#7', workflow: 'code-review', pr: { verdict: 'approve', merged: false, state: 'open' } });
  const history = [pull];
  const live = [];
  const { asked, rows, outcome, send } = await mount(t, [], history, { live });
  assert.deepEqual(ids(rows('Recent')), ['7']);

  t.mock.timers.enable({ apis: ['setTimeout'] });
  const merge = run(8, 'running', { thread: null, subject: 'acme/app#7', workflow: 'pr-merged', kind: 'merge' });
  live.push(merge);
  history[0] = { ...pull, pr: { ...pull.pr, merged: true, state: 'merged' } };
  await send(streamed({ ...merge, status: 'succeeded', finished_at: '2026-11-20T10:09:00Z' }));
  assert.deepEqual(rows('Recent'), []);
  assert.deepEqual(rows('Completed'), []);
  await send(streamed({ ...merge, status: 'succeeded', finished_at: '2026-11-20T10:09:00Z', outcome: 'merged' }));

  live.splice(0);
  const listFetches = asked.filter((path) => path.includes('pr=')).length;
  t.mock.timers.tick(1500);
  await settle();
  assert.equal(asked.filter((path) => path.includes('pr=')).length, listFetches + 2);
  assert.deepEqual(rows('Recent'), []);
  assert.deepEqual(rows('Completed').map((row) => [row.dataset.runId, outcome(row)]), [['7', 'Merged']]);
});

test('a full recent list takes a newer run but never pushes a listed row out for an older one', async (t) => {
  const listed = Array.from({ length: 10 }, (_, index) => run(20 - index, 'succeeded', { thread: `T${20 - index}`, subject: `T${20 - index}` }));
  const { rows, send } = await mount(t, [], listed);
  const recent = () => ids(rows('Recent'));
  const shown = recent();
  assert.equal(shown.length, 10);

  await send(run(5, 'succeeded', { thread: 'T5', subject: 'T5' }));
  assert.deepEqual(recent(), shown);

  await send(run(30, 'succeeded', { thread: 'T30', subject: 'T30' }));
  assert.deepEqual(recent(), ['30', ...shown.slice(0, 9)]);
});

test('a failed open-runs load shows its error until a later load succeeds', async (t) => {
  const failing = { open: true };
  const open = [fix];
  const { section, rows, send, reconnect } = await mount(t, open, [review], { failing });
  assert.equal(section('In progress').querySelectorAll('div.error-box').length, 1);
  assert.deepEqual(rows('Recent'), []);

  const started = run(3, 'running', { thread: 'MEM-3', subject: 'MEM-3' });
  open.unshift(started);
  await send(started);
  assert.equal(section('In progress').querySelectorAll('div.error-box').length, 1);

  failing.open = false;
  await reconnect();
  assert.equal(section('In progress').querySelectorAll('div.error-box').length, 0);
  assert.deepEqual(ids(rows('In progress')), ['3', '1']);
  assert.deepEqual(rows('Recent'), []);
});
