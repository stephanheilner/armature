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

// Renders the page with fixed answers for the open runs and the grouped history.
async function mount(t, open, history, failing = { open: false }) {
  const asked = [];
  globalThis.fetch = async (path) => {
    asked.push(path);
    const query = new URLSearchParams(path.split('?')[1] || '');
    if (path.startsWith('/api/runs') && query.get('status') && failing.open) return { status: 500, json: async () => ({ error: 'boom' }) };
    const payload = path.startsWith('/api/runs') ? { runs: query.get('status') ? open : history, next_before: null } : {};
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
    send: (...runs) => {
      for (const sent of runs) source.listeners.run({ data: JSON.stringify(sent) });
      for (const frame of frames.splice(0)) frame();
    },
    reconnect: async () => {
      source.listeners.open();
      for (let turn = 0; turn < 5; turn += 1) await new Promise((resolve) => setImmediate(resolve));
    },
  };
}

const fix = run(1, 'running');
const review = run(2, 'succeeded', { workflow: 'code-review', verdict: 'approve', group_size: 2 });

test('a grouped row reads In Progress while an older run in its thread is open, and its verdict once that run ends', async (t) => {
  const { asked, section, rows, outcome, send } = await mount(t, [fix], [review]);
  assert.deepEqual(rows('In progress').map((row) => row.dataset.runId), ['1']);
  assert.deepEqual(rows('Recent').map((row) => [row.dataset.runId, outcome(row)]), [['2', 'In Progress']]);

  const grouped = () => asked.filter((path) => path.includes('group=1')).length;
  const fetchedBefore = grouped();
  send({ ...fix, status: 'succeeded', finished_at: '2026-11-20T10:05:00Z' });

  assert.deepEqual(rows('In progress'), []);
  assert.equal(section('In progress').textContent, 'Nothing is in progress.');
  assert.deepEqual(rows('Recent').map((row) => [row.dataset.runId, outcome(row)]), [['2', 'Approved']]);
  assert.equal(grouped(), fetchedBefore);
});

test('a fetch that still lists a run the stream reported ended does not bring it back', async (t) => {
  const { asked, rows, outcome, send, reconnect } = await mount(t, [fix], [review]);
  send({ ...fix, status: 'succeeded', finished_at: '2026-11-20T10:05:00Z' });
  const openFetches = asked.filter((path) => path.includes('status=')).length;
  await reconnect();

  assert.equal(asked.filter((path) => path.includes('status=')).length, openFetches + 1);
  assert.deepEqual(rows('In progress'), []);
  assert.deepEqual(rows('Recent').map((row) => [row.dataset.runId, outcome(row)]), [['2', 'Approved']]);
});

test('a full recent list takes a newer run but never pushes a listed row out for an older one', async (t) => {
  const listed = Array.from({ length: 10 }, (_, index) => run(20 - index, 'succeeded', { thread: `T${20 - index}`, subject: `T${20 - index}` }));
  const { rows, send } = await mount(t, [], listed);
  const ids = () => rows('Recent').map((row) => row.dataset.runId);
  const shown = ids();
  assert.equal(shown.length, 10);

  send(run(5, 'succeeded', { thread: 'T5', subject: 'T5' }));
  assert.deepEqual(ids(), shown);

  send(run(30, 'succeeded', { thread: 'T30', subject: 'T30' }));
  assert.deepEqual(ids(), ['30', ...shown.slice(0, 9)]);
});

test('a failed open-runs load shows its error until a later load succeeds', async (t) => {
  const failing = { open: true };
  const open = [fix];
  const { section, rows, outcome, send, reconnect } = await mount(t, open, [review], failing);
  assert.equal(section('In progress').querySelectorAll('div.error-box').length, 1);
  assert.deepEqual(rows('Recent').map((row) => [row.dataset.runId, outcome(row)]), [['2', 'Approved']]);

  const started = run(3, 'running', { thread: 'MEM-3', subject: 'MEM-3' });
  send(started);
  open.unshift(started);
  assert.equal(section('In progress').querySelectorAll('div.error-box').length, 1);

  failing.open = false;
  await reconnect();
  assert.equal(section('In progress').querySelectorAll('div.error-box').length, 0);
  assert.deepEqual(rows('In progress').map((row) => row.dataset.runId), ['3', '1']);
  assert.deepEqual(rows('Recent').map((row) => [row.dataset.runId, outcome(row)]), [['3', 'In Progress'], ['2', 'In Progress']]);
});
