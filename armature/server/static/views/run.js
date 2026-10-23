import { api, listOf } from '../api.js';
import { card, clear, duration, element, empty, errorBox, link, money, pageHeader, statusChip, when } from '../dom.js';
import { applyStepEvent, branchText, descendants, stepKey, describe, earlierPage, elapsedSeconds, FeedModel, groupItems, newestBefore, oneLine, stepTree, summarizeInput, TERMINAL } from '../feed.js';
import { every, subscribe } from '../live.js';
import { hashFor } from '../router.js';

const PAGE = 1000;
const EARLIER_PAGE = 500;
const POLL_MS = 2000;
const REFRESH_MS = 1500;
const SETTLE_MS = 1500;
const ANNOUNCE_MS = 2000;
const FINDINGS_SHOWN = 500;
const FINDINGS_PAGE = 50;
const BOTTOM_SLACK = 24;
const LIVE_STATES = { live: 'Live', reconnecting: 'Reconnecting…', polling: 'Refreshing every 2 s', ended: 'Finished' };
const STEP_WORDS = { step_start: 'started', step_end: 'finished', step_failed: 'failed', step_skipped: 'skipped', retry: 'retrying' };

const runPath = (runId) => `/api/runs/${encodeURIComponent(runId)}`;
const eventsPath = (runId, after, limit) => `/api/runs/${encodeURIComponent(runId)}/events?after=${after}&limit=${limit}`;
const findingsPath = (runId, before, limit) => (before === null ? `/api/runs/${encodeURIComponent(runId)}/findings?limit=${limit}` : `/api/runs/${encodeURIComponent(runId)}/findings?before=${encodeURIComponent(before)}&limit=${limit}`);
const beforePath = (runId, before, limit) => `/api/runs/${encodeURIComponent(runId)}/events?before=${before}&limit=${limit}`;
const streamPath = (runId, after) => (after === null ? `/api/runs/${encodeURIComponent(runId)}/stream` : `/api/runs/${encodeURIComponent(runId)}/stream?after=${after}`);

const tokens = (value) => (Number.isFinite(Number(value)) ? Number(value).toLocaleString('en-US') : '—');

function labelled(label, valueNode) {
  return element('span', {}, element('span', { class: 'meta-label' }, `${label} `), valueNode);
}

function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

function toolNode(item) {
  const call = item.payload;
  const result = item.result;
  const state = !result ? element('span', { class: 'tool-state muted' }, 'running')
    : result.is_error ? element('span', { class: 'tool-state bad' }, 'error') : element('span', { class: 'tool-state ok' }, 'done');
  const details = element('details', { class: `feed-item feed-tool${result?.is_error ? ' tone-bad' : ''}`, title: item.ts },
    element('summary', {}, element('span', { class: 'feed-label' }, call.tool || 'Tool'), ' ', element('span', { class: 'tool-input' }, summarizeInput(call.input)), ' ', state));
  details.addEventListener('toggle', () => {
    if (details.open && details.childElementCount === 1) fillTool(details, item);
  });
  return details;
}

function fillTool(details, item) {
  const call = item.payload;
  const result = item.result;
  details.append(element('div', { class: 'tool-body' },
    element('div', { class: 'tool-head' }, call.truncated ? 'Input (truncated)' : 'Input'),
    element('pre', {}, String(call.input ?? '')),
    result ? [
      element('div', { class: `tool-head${result.is_error ? ' bad' : ''}` }, `${result.is_error ? 'Error' : 'Result'}${result.truncated ? ' (truncated)' : ''}`),
      element('pre', {}, String(result.content ?? '')),
    ] : element('div', { class: 'tool-head muted' }, 'No result yet.')));
}

function itemNode(item) {
  if (item.kind === 'tool_call') return toolNode(item);
  if (item.kind === 'text') {
    return element('div', { class: 'feed-item feed-text', title: item.ts }, String(item.payload.text ?? ''),
      item.payload.truncated ? element('span', { class: 'muted' }, ' (truncated)') : null);
  }
  const { tone, label, text } = describe(item);
  return element('div', { class: `feed-item feed-line tone-${tone}`, title: item.ts },
    element('span', { class: 'feed-label' }, label), ' ', element('span', {}, text));
}

export async function render(root, { args, signal }) {
  const runId = args[0];
  if (!runId) {
    root.append(pageHeader('Run'), empty('No run given.'));
    return;
  }
  let detail;
  try {
    detail = await api(runPath(runId));
  } catch (err) {
    root.append(pageHeader('Run'), errorBox(err));
    return;
  }
  let run = detail?.run || {};
  let steps = Array.isArray(detail?.steps) ? detail.steps.map((step) => ({ ...step })) : [];

  const chipSlot = element('span', {}, statusChip(run.status));
  const liveNote = element('span', { class: 'live-state muted', role: 'status' }, 'Connecting…');
  const projectText = element('span', {}, '—');
  const durationText = element('span', { class: 'mono' });
  const costText = element('span', { class: 'mono' });
  const tokenText = element('span', { class: 'mono' });
  const extraMeta = element('span', {});
  const errorSlot = element('div', {});
  root.append(element('p', { class: 'crumb' }, link(hashFor('runs'), 'Runs')),
    pageHeader(run.workflow || 'Run', chipSlot, liveNote),
    element('div', { class: 'meta' },
      labelled('Project', projectText),
      labelled('Triggered by', element('span', {}, [run.triggered_by || '—', run.trigger_kind ? ` (${run.trigger_kind})` : ''].join(''))),
      labelled('Started', element('span', { title: run.started_at || null }, when(run.started_at))),
      labelled('Duration', durationText), labelled('Cost', costText), labelled('Tokens', tokenText), extraMeta),
    errorSlot);
  if (run.team_id) {
    api(`/api/teams/${encodeURIComponent(run.team_id)}/projects`).then((payload) => {
      const project = listOf(payload, 'projects').find((candidate) => String(candidate.id) === String(run.project_id));
      if (project) setText(projectText, project.name);
    }).catch(() => {});
  }

  let shownStatus = run.status;
  const updateHeader = () => {
    if (run.status !== shownStatus) {
      shownStatus = run.status;
      clear(chipSlot).append(statusChip(run.status));
    }
    const isOpen = !TERMINAL.has(run.status);
    setText(durationText, duration(elapsedSeconds(run.started_at, run.finished_at || (isOpen ? null : run.started_at))));
    setText(costText, money(run.cost_usd, 4));
    setText(tokenText, `${tokens(run.input_tokens)} in · ${tokens(run.output_tokens)} out`);
    clear(extraMeta);
    if (run.rerun_of) extraMeta.append(link(hashFor('run', [run.rerun_of]), 'Rerun of an earlier run'));
    if (run.cancelled_by) extraMeta.append(` Canceled by ${run.cancelled_by}`);
    const errorText = run.error ? oneLine(run.error, 600) : '';
    if (errorSlot.textContent !== errorText) clear(errorSlot).append(errorText ? element('div', { class: 'banner bad', role: 'alert' }, errorText) : null);
  };
  updateHeader();

  // Steps
  let stepFilter = null;
  let shownSteps = null;
  const stepRows = new Map();
  const stepList = element('ul', { class: 'step-list', role: 'list' });
  const allSteps = element('button', { type: 'button', class: 'link-button', hidden: true }, 'Show all steps');
  const stepsCard = card(element('h2', { class: 'inline' }, 'Steps'), stepList, allSteps);
  const syncSteps = () => {
    for (const { step, key, parent } of stepTree(steps)) {
      let row = stepRows.get(key);
      if (!row) {
        const button = element('button', { type: 'button', class: 'step-row', 'aria-pressed': 'false' });
        row = {
          button, chip: element('span', {}), name: element('span', { class: 'step-name' }, step.step_id), time: element('span', { class: 'step-num step-time' }),
          cost: element('span', { class: 'step-num step-cost' }), branches: element('span', { class: 'step-branches' }), item: element('li', {}), children: null, status: null,
        };
        button.append(row.chip, row.name, row.time, row.cost, row.branches);
        button.addEventListener('click', () => setFilter(stepFilter === key ? null : key));
        row.item.append(button);
        stepRows.set(key, row);
      }
      const parentRow = parent ? stepRows.get(parent) : null;
      if (parentRow && !parentRow.children) parentRow.item.append(parentRow.children = element('ul', { class: 'step-list step-children', role: 'list' }));
      const list = parentRow ? parentRow.children : stepList;
      if (row.item.parentElement !== list) list.append(row.item);
      if (row.status !== step.status) {
        row.status = step.status;
        clear(row.chip).append(statusChip(step.status));
      }
      setText(row.time, duration(elapsedSeconds(step.started_at, step.finished_at)));
      setText(row.cost, step.cost_usd === null || step.cost_usd === undefined ? '' : money(step.cost_usd, 4));
      setText(row.branches, branchText(step));
      if (step.error) row.button.title = oneLine(step.error, 300);
    }
    if (!steps.length && !stepList.firstChild) stepList.append(element('li', { class: 'muted' }, 'No steps yet.'));
    else if (steps.length) stepList.querySelector(':scope > li.muted')?.remove();
  };

  // Findings come newest first from their own route. Live finding events go on top.
  let findingsFromRoute = false;
  let findingsBefore = null;
  const routeFindings = new Set();
  const findingSignature = (finding) => `${finding.path || ''}:${finding.line || ''}:${finding.title || ''}`;
  const findingList = element('ul', { class: 'finding-list', role: 'list' });
  const findingNote = element('p', { class: 'muted' }, 'None yet.');
  const moreFindings = element('button', { type: 'button', class: 'link-button', hidden: true }, 'Load more');
  const findingsCard = card(element('h2', { class: 'inline' }, 'Findings'), findingNote, findingList, moreFindings);
  const findingNode = (finding) => {
    const where = finding.path ? `${finding.path}${finding.line ? `:${finding.line}` : ''}` : '';
    return element('li', {},
      element('div', {}, element('span', { class: `chip ${finding.severity === 'high' || finding.severity === 'critical' ? 'bad' : 'warn'}` }, String(finding.severity || 'finding')), oneLine(finding.title || 'Finding', 200)),
      where ? element('code', { class: 'wrap-anywhere' }, where) : null,
      finding.body ? element('p', { class: 'finding-body' }, oneLine(finding.body, 400)) : null);
  };
  const addFinding = (finding, { atTop = true } = {}) => {
    if (atTop && routeFindings.delete(findingSignature(finding))) return;
    findingNote.hidden = true;
    if (atTop) findingList.prepend(findingNode(finding));
    else findingList.append(findingNode(finding));
    while (findingList.childElementCount > FINDINGS_SHOWN) findingList.lastElementChild.remove();
  };
  const loadFindings = async () => {
    const page = await api(findingsPath(runId, findingsBefore, FINDINGS_PAGE));
    for (const finding of listOf(page, 'findings')) addFinding(finding, { atTop: false });
    findingsBefore = page?.next_before ?? null;
    moreFindings.hidden = findingsBefore === null || findingList.childElementCount >= FINDINGS_SHOWN;
    return page;
  };
  moreFindings.addEventListener('click', async () => {
    moreFindings.disabled = true;
    try {
      await loadFindings();
    } catch (err) {
      findingsCard.append(errorBox(err));
    } finally {
      moreFindings.disabled = false;
    }
  });
  // A finding written between the run summary and this fetch also arrives as a live event, so the first page's are remembered.
  const firstFindings = loadFindings().then((page) => {
    findingsFromRoute = true;
    for (const finding of listOf(page, 'findings')) routeFindings.add(findingSignature(finding));
  }).catch(() => {});

  // Feed
  const announcer = element('div', { class: 'visually-hidden', role: 'log', 'aria-live': 'polite' });
  const earlierButton = element('button', { type: 'button', class: 'link-button' }, 'Load earlier');
  const earlierRow = element('div', { class: 'feed-earlier', hidden: true }, earlierButton);
  const feedList = element('div', { class: 'feed-list' });
  const feedBox = element('div', { class: 'feed', tabindex: 0, role: 'region', 'aria-label': 'Run activity' }, earlierRow, feedList);
  const jumpButton = element('button', { type: 'button', class: 'jump', hidden: true }, 'Jump to latest');
  const filterNote = element('p', { class: 'note', hidden: true });
  const feedCard = card(element('h2', { class: 'inline' }, 'Activity'), filterNote, element('div', { class: 'feed-frame' }, feedBox, jumpButton), announcer);
  root.append(element('div', { class: 'run-layout' },
    element('div', { class: 'run-steps' }, stepsCard), element('div', { class: 'run-feed' }, feedCard), element('div', { class: 'run-findings' }, findingsCard)));
  syncSteps();

  const model = new FeedModel();
  const nodes = new Map();
  let following = true;
  const isAtBottom = () => feedBox.scrollHeight - feedBox.scrollTop - feedBox.clientHeight <= BOTTOM_SLACK;
  const toBottom = () => {
    feedBox.scrollTop = feedBox.scrollHeight;
  };
  const updateChrome = () => {
    earlierRow.hidden = !model.hasEarlier;
    jumpButton.hidden = following && !model.detached;
    setText(jumpButton, model.detached && model.missed ? `Jump to latest (${model.missed.toLocaleString('en-US')} new)` : 'Jump to latest');
  };
  feedBox.addEventListener('scroll', () => {
    following = isAtBottom() && !model.detached;
    updateChrome();
  });

  const groupNode = (stepId) => element('section', { class: 'feed-group', 'data-step': stepId ?? '', hidden: shownSteps !== null && !shownSteps.has(stepId) },
    element('h3', { class: 'feed-step' }, stepId || 'Run'));
  const place = (item, node) => {
    const last = feedList.lastElementChild;
    const group = last && last.dataset.step === (item.step_key ?? '') ? last : feedList.appendChild(groupNode(item.step_key));
    group.append(node);
    nodes.set(item.id, node);
  };
  const renderAll = () => {
    clear(feedList);
    nodes.clear();
    const fragment = document.createDocumentFragment();
    for (const group of groupItems(model.items)) {
      const section = groupNode(group.step_key);
      for (const item of group.items) {
        const node = itemNode(item);
        section.append(node);
        nodes.set(item.id, node);
      }
      fragment.append(section);
    }
    feedList.append(fragment);
    updateChrome();
  };
  const applyChanges = ({ added, updated, removed }) => {
    if (removed.length) {
      const before = feedList.scrollHeight;
      for (const id of removed) {
        const node = nodes.get(id);
        nodes.delete(id);
        const group = node?.parentElement;
        node?.remove();
        if (group && group.childElementCount <= 1) group.remove();
      }
      if (!following) feedBox.scrollTop -= before - feedList.scrollHeight;
    }
    for (const item of updated) {
      const old = nodes.get(item.id);
      if (!old) continue;
      const fresh = itemNode(item);
      if (old.open) {
        fresh.open = true;
        fillTool(fresh, item);
      }
      const hadFocus = old.contains(document.activeElement);
      old.replaceWith(fresh);
      nodes.set(item.id, fresh);
      if (hadFocus) fresh.querySelector('summary')?.focus();
    }
    for (const item of added) place(item, itemNode(item));
  };

  const setFilter = (stepId) => {
    stepFilter = stepId;
    for (const [id, row] of stepRows) row.button.setAttribute('aria-pressed', String(id === stepId));
    shownSteps = stepId === null ? null : descendants(steps, stepId);
    for (const group of feedList.children) group.hidden = shownSteps !== null && !shownSteps.has(group.dataset.step);
    allSteps.hidden = stepId === null;
    filterNote.hidden = stepId === null;
    setText(filterNote, stepId === null ? '' : `Showing step ${stepId}${shownSteps.size > 1 ? ' and its child steps' : ''} only.`);
    toBottom();
  };
  allSteps.addEventListener('click', () => setFilter(null));

  // Screen readers hear step and run changes at most every ANNOUNCE_MS, never each event.
  let announceTimer = 0;
  let pendingAnnouncements = [];
  const announce = (text) => {
    pendingAnnouncements.push(text);
    if (announceTimer) return;
    announceTimer = setTimeout(() => {
      announceTimer = 0;
      announcer.append(element('p', {}, pendingAnnouncements.slice(-3).join(' ')));
      pendingAnnouncements = [];
      while (announcer.childElementCount > 5) announcer.firstElementChild.remove();
    }, ANNOUNCE_MS);
  };

  let refreshTimer = 0;
  let lastRefresh = 0;
  const refresh = async () => {
    refreshTimer = 0;
    lastRefresh = Date.now();
    try {
      const fresh = await api(runPath(runId));
      if (signal?.aborted) return;
      if (fresh?.run) run = { ...run, ...fresh.run };
      if (Array.isArray(fresh?.steps)) {
        const known = new Map(steps.map((step) => [stepKey(step), step]));
        steps = fresh.steps.map((step) => ({ branch_total: known.get(stepKey(step))?.branch_total, branches_done: known.get(stepKey(step))?.branches_done, ...step }));
      }
      updateHeader();
      syncSteps();
      if (TERMINAL.has(run.status)) settleSoon();
    } catch {}
  };
  const refreshSoon = () => {
    if (!refreshTimer) refreshTimer = setTimeout(refresh, Math.max(0, REFRESH_MS - (Date.now() - lastRefresh)));
  };

  let sideSeq = 0;
  const sideEffects = (events, { live }) => {
    let stepsChanged = false;
    for (const event of events) {
      const seq = Number(event?.seq);
      if (!Number.isFinite(seq) || seq <= sideSeq) continue;
      sideSeq = seq;
      const payload = event.payload || {};
      if (applyStepEvent(steps, event)) {
        stepsChanged = true;
        if (live) announce(`Step ${event.step_id} ${STEP_WORDS[event.kind]}.`);
      }
      if (event.kind === 'finding' && (live || !findingsFromRoute)) addFinding(payload);
      if (event.kind === 'run_end' && payload.status) {
        run = { ...run, status: payload.status, error: payload.error ?? run.error, finished_at: run.finished_at || event.ts };
        if (live) announce(`Run ${payload.status}.`);
        updateHeader();
      }
      if (live && ['step_end', 'step_failed', 'cost', 'run_end'].includes(event.kind)) refreshSoon();
    }
    if (stepsChanged) syncSteps();
  };

  let phase = 'loading';
  let queue = [];
  let frame = 0;
  const flush = () => {
    frame = 0;
    if (phase !== 'live' || !queue.length) return;
    const batch = queue.sort((left, right) => left.seq - right.seq);
    queue = [];
    sideEffects(batch, { live: true });
    applyChanges(model.append(batch));
    if (following) toBottom();
    updateChrome();
  };
  let streamSeq = 0;
  const seenSeq = () => Math.max(sideSeq, streamSeq);
  const receive = (events) => {
    for (const event of events) streamSeq = Math.max(streamSeq, Number(event?.seq) || 0);
    queue.push(...events);
    if (phase === 'live' && !frame) frame = requestAnimationFrame(flush);
  };

  const fetchAfter = async (after, sink) => {
    for (;;) {
      const page = await api(eventsPath(runId, after, PAGE));
      if (signal?.aborted) return;
      const events = listOf(page, 'events');
      sink(events);
      const next = page?.next_after ?? events.at(-1)?.seq;
      if (events.length < PAGE || next === null || next === undefined || Number(next) <= after) return;
      after = Number(next);
    }
  };
  let catching = null;
  const catchUp = () => {
    catching ||= fetchAfter(sideSeq, receive).catch(() => {}).finally(() => { catching = null; });
    return catching;
  };

  let stream = null;
  let poller = null;
  let settled = false;
  const setLive = (state) => setText(liveNote, LIVE_STATES[state] || '');
  const settleSoon = () => {
    if (settled) return;
    settled = true;
    setTimeout(async () => {
      await catchUp();
      stream?.close();
      poller?.stop();
      clearInterval(ticker);
      setLive('ended');
      updateHeader();
    }, SETTLE_MS);
  };

  const ticker = setInterval(() => {
    updateHeader();
    syncSteps();
  }, 1000);
  signal?.addEventListener('abort', () => {
    clearInterval(ticker);
    clearTimeout(refreshTimer);
    clearTimeout(announceTimer);
    cancelAnimationFrame(frame);
    stream?.close();
    poller?.stop();
  });

  const startPolling = () => {
    poller = every(POLL_MS, async () => {
      await catchUp();
      await refresh();
      return !settled;
    }, signal);
  };
  // The server sends `reconnect` and closes when this page falls behind, so the stream reopens from the last seq seen.
  const openStream = (after) => {
    stream?.close();
    stream = subscribe(streamPath(runId, after), {
      events: {
        'run-event': (event) => receive([event]),
        run: (summary) => {
          run = { ...run, ...summary };
          updateHeader();
          if (TERMINAL.has(run.status)) settleSoon();
        },
        reconnect: () => {
          if (!settled) openStream(seenSeq());
        },
      },
      onState: setLive,
      onOpen: () => {
        if (phase === 'live') catchUp();
      },
      onFallback: startPolling,
    }, { signal });
  };

  const finishLoad = () => {
    phase = 'live';
    renderAll();
    following = true;
    requestAnimationFrame(() => {
      toBottom();
      updateChrome();
    });
    if (queue.length && !frame) frame = requestAnimationFrame(flush);
  };
  const loadTail = async (after) => {
    phase = 'loading';
    await fetchAfter(after, (events) => {
      sideEffects(events, { live: false });
      model.append(events);
    });
    finishLoad();
  };
  const loadNewest = async (before) => {
    phase = 'loading';
    const page = await api(beforePath(runId, before, PAGE));
    const events = listOf(page, 'events');
    sideEffects(events, { live: false });
    model.append(events);
    model.noteStart(page, events);
    finishLoad();
  };
  const lastSeq = run.last_seq === undefined ? NaN : Number(run.last_seq);
  const hasBefore = Number.isFinite(lastSeq);
  await firstFindings;
  try {
    if (hasBefore) await loadNewest(newestBefore(lastSeq));
    else await loadTail(0);
  } catch (err) {
    feedList.append(errorBox(err));
    phase = 'live';
  }
  if (signal?.aborted) return;
  if (TERMINAL.has(run.status)) {
    settled = true;
    clearInterval(ticker);
    setLive('ended');
  } else {
    openStream(hasBefore ? Math.max(lastSeq, seenSeq()) : null);
  }

  earlierButton.addEventListener('click', async () => {
    const { after, limit } = earlierPage(model.firstSeq, EARLIER_PAGE);
    if (!hasBefore && !limit) return;
    earlierButton.disabled = true;
    try {
      const page = await api(hasBefore ? beforePath(runId, model.firstSeq, EARLIER_PAGE) : eventsPath(runId, after, limit));
      const events = listOf(page, 'events');
      const before = feedList.scrollHeight;
      const top = feedBox.scrollTop;
      model.prepend(events);
      model.noteStart(page, events);
      renderAll();
      feedBox.scrollTop = top + feedList.scrollHeight - before;
      if (model.detached) following = false;
      updateChrome();
    } catch (err) {
      earlierRow.append(errorBox(err));
    } finally {
      earlierButton.disabled = false;
    }
  });
  jumpButton.addEventListener('click', async () => {
    if (model.detached) {
      model.reset();
      await (hasBefore ? loadNewest(newestBefore(seenSeq())) : loadTail(Math.max(0, sideSeq - PAGE))).catch(() => {});
    }
    following = true;
    toBottom();
    updateChrome();
    feedBox.focus();
  });
}
