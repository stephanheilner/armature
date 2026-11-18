// "How does another model review compared with Claude?" kimi-evaluate's replay reports, for admins, with each round's
// findings side by side. A report from before side-by-side findings has counts only, so every read has a fallback.
import { element, money } from '../../dom.js';
import { fixed, plural } from './format.js';
import { page } from './index.js';
import { emptyLine, emptyState, events, filters, section, select, skeletonRows } from './ui.js';

export const QUESTION = 'How does another model review compared with Claude?';
const LIST = '/api/insights/replays';
const GITHUB_PR = /^([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)#([1-9][0-9]{0,9})$/;
const SHA = /^[0-9a-f]{40}$/;
const SEVERITIES = ['high', 'medium', 'low'];
const VERDICTS = { valid: ['Judged valid', 'ok'], not_valid: ['Judged not valid', 'bad'], not_judged: ['Not judged', ''] };
const DECISIONS = { block: 'Lead: block', suggest: 'Lead: suggest', drop: 'Lead: drop', fixed: 'Lead: fixed' };

const obj = (value) => (value && typeof value === 'object' && !Array.isArray(value) ? value : {});
const list = (value) => (Array.isArray(value) ? value : []);
const count = (value) => (Number.isInteger(value) ? value : 0);
const total = (split) => count(obj(split).total);
const minutes = (value) => (Number.isFinite(value) ? `${fixed(value, 1)} min` : '—');
const per = (amount, rounds) => (Number.isFinite(amount) && rounds ? amount / rounds : null);
const named = (alias) => (alias ? alias.charAt(0).toUpperCase() + alias.slice(1) : 'Replay');

export function prHref(subject) {
  const found = GITHUB_PR.exec(String(subject || ''));
  return found ? `https://github.com/${found[1]}/pull/${found[2]}` : null;
}

// Only a checked repository, commit, and path make a link, as the HTML report builds it.
export function fileHref(subject, head, path, line) {
  const found = GITHUB_PR.exec(String(subject || ''));
  if (!found || typeof head !== 'string' || !SHA.test(head) || typeof path !== 'string' || !path || path.startsWith('/') || path.split('/').includes('..')) return null;
  const anchor = Number.isInteger(line) && line > 0 ? `#L${line}` : '';
  return `https://github.com/${found[1]}/blob/${head}/${path.split('/').map(encodeURIComponent).join('/')}${anchor}`;
}

const badge = (text, tone = '') => element('span', { class: `rp-badge${tone ? ` is-${tone}` : ''}` }, text);

export function card(found, { side, subject, head, oneSided, alias }) {
  if (!found || typeof found !== 'object') return element('div', { class: 'rp-card is-none' }, `Not raised by ${side === 'claude' ? 'Claude' : alias}`);
  const badges = [];
  badges.push(SEVERITIES.includes(found.severity) ? badge(found.severity.charAt(0).toUpperCase() + found.severity.slice(1), found.severity) : badge('Severity unknown'));
  if (found.blocking === true) badges.push(badge('Blocking', 'bad'));
  else if (found.blocking === false) badges.push(badge('Not blocking'));
  if (DECISIONS[found.decision]) badges.push(badge(DECISIONS[found.decision], found.decision === 'block' ? 'bad' : ''));
  if (oneSided && VERDICTS[found.verdict]) badges.push(badge(...VERDICTS[found.verdict]));
  if (side === 'claude' && found.fixed) badges.push(badge('Fixed by author', 'accent'));
  const where = `${found.path || '(no file)'}${Number.isInteger(found.line) ? `:${found.line}` : ''}`;
  const href = fileHref(subject, head, found.path, found.line);
  const body = typeof found.body === 'string' && found.body && found.body !== found.claim
    ? element('details', {}, element('summary', { class: 'rp-more' }, 'Details'), element('p', { class: 'rp-body' }, found.body)) : null;
  return element('div', { class: 'rp-card' },
    element('div', { class: 'rp-side' }, side === 'claude' ? 'Claude' : alias),
    element('div', { class: 'rp-badges' }, badges),
    href ? element('a', { class: 'rp-where in-link', href }, where) : element('span', { class: 'rp-where' }, where),
    element('p', { class: 'rp-claim' }, found.claim || ''),
    found.reason ? element('p', { class: 'rp-reason' }, found.reason) : null,
    body);
}

const sideFacts = (run) => `${money(obj(run).cost_usd)}, ${count(obj(run).input_tokens).toLocaleString('en-US')} in, ${count(obj(run).output_tokens).toLocaleString('en-US')} out, ${minutes(obj(run).minutes)}`;

export function round(entry, alias) {
  const verdict = obj(entry.verdict);
  const rate = obj(entry.rate);
  const head = typeof entry.head === 'string' ? entry.head : '';
  const summary = element('summary', {},
    element('span', { class: 'rp-round-name' }, `Round ${entry.round ?? '?'}`),
    element('span', { class: 'rp-facts' }, `${head ? `${head.slice(0, 7)} · ` : ''}verdict Claude ${verdict.claude || '—'}, ${alias} ${verdict.kimi || '—'} · ${alias} rated ${rate.kimi || '—'}, Claude risk ${rate.risk || '—'} · both ${total(entry.both)}, Claude only ${total(entry.claude_only)}, ${alias} only ${total(entry.kimi_only)}`));
  const runs = element('p', { class: 'rp-runs' }, `Claude ${sideFacts(entry.claude_run)} · ${alias} ${sideFacts(entry.kimi_run)}`);
  const found = entry.findings;
  if (!found || typeof found !== 'object') return element('details', { class: 'rp-round' }, summary, runs, emptyLine('This report predates side-by-side findings. Rebuild it to see them.'));
  const rows = [
    ...list(found.pairs).map((pair) => [obj(pair).claude, obj(pair).kimi, false]),
    ...list(found.claude_only).map((one) => [one, null, true]),
    ...list(found.kimi_only).map((one) => [null, one, true]),
  ];
  if (!rows.length) return element('details', { class: 'rp-round' }, summary, runs, emptyLine('Neither side raised a finding.'));
  const where = { subject: entry.subject, head, alias };
  return element('details', { class: 'rp-round' }, summary, runs, element('div', { class: 'rp-grid' },
    element('div', { class: 'rp-col' }, 'Claude'), element('div', { class: 'rp-col' }, alias),
    rows.flatMap(([left, right, oneSided]) => [card(left, { ...where, side: 'claude', oneSided }), card(right, { ...where, side: 'kimi', oneSided })])));
}

function tiles(totals, skipped, alias) {
  const rounds = count(totals.rounds);
  const claude = obj(totals.claude_run);
  const kimi = obj(totals.kimi_run);
  const one = (label, value, note = null) => element('div', { class: 'in-tile', tabindex: 0 },
    element('span', { class: 'in-tile-label' }, label), element('span', { class: 'in-tile-value' }, String(value)), note ? element('span', { class: 'in-ref' }, note) : null);
  return element('div', { class: 'in-tiles' },
    one('Rounds compared', rounds, `${plural(skipped, 'round')} skipped`),
    one('Found by both', total(totals.both)),
    one('Claude only', total(totals.claude_only)),
    one(`${alias} only`, total(totals.kimi_only)),
    one(`Fixed by author, ${alias} missed`, count(totals.fixed_missed), `of ${count(totals.fixed)} the author fixed`),
    one(`${alias} only, judged valid`, count(totals.kimi_only_valid), `of ${total(totals.kimi_only)}`),
    one('Verdict agreement', 'verdict_agrees' in totals ? `${count(totals.verdict_agrees)} of ${rounds}` : '—'),
    one('Claude cost per round', money(per(claude.cost_usd, rounds))),
    one(`${alias} cost per round`, money(per(kimi.cost_usd, rounds))),
    one('Claude minutes per round', minutes(per(claude.minutes, rounds))),
    one(`${alias} minutes per round`, minutes(per(kimi.minutes, rounds))));
}

function prTable(prs, alias) {
  if (!prs.length) return emptyLine('No replay finished.');
  const head = ['Pull request', 'Rounds', 'Both', 'Claude only', `${alias} only`, `Fixed, ${alias} missed`, `${alias} only, valid`, 'Verdicts agree', `Cost, ${alias} vs Claude`];
  const rows = prs.map((pr) => {
    const t = obj(pr.totals);
    const href = prHref(pr.subject);
    return element('tr', {},
      element('td', {}, href ? element('a', { class: 'in-link', href }, pr.subject) : pr.subject || 'Unknown'),
      [count(t.rounds), total(t.both), total(t.claude_only), total(t.kimi_only), count(t.fixed_missed), count(t.kimi_only_valid),
        'verdict_agrees' in t ? `${count(t.verdict_agrees)} of ${count(t.rounds)}` : '—',
        `${money(obj(t.kimi_run).cost_usd)} vs ${money(obj(t.claude_run).cost_usd)}`].map((cell) => element('td', { class: 'num' }, String(cell))));
  });
  return element('div', { class: 'in-table-scroll' }, element('table', { class: 'in-table' },
    element('thead', {}, element('tr', {}, head.map((text, index) => element('th', { class: index ? 'num' : null }, text)))),
    element('tbody', {}, rows)));
}

export function draw(found) {
  const data = obj(found.report);
  const alias = named(found.alias || data.alias);
  const prs = list(data.prs).filter((pr) => pr && typeof pr === 'object');
  const skipped = list(data.skipped).filter((row) => row && typeof row === 'object');
  return [
    tiles(obj(data.totals), skipped.length, alias),
    section({ title: 'Pull requests', takeaway: 'Counts of the lead’s findings on each side.' }, prTable(prs, alias)),
    ...prs.map((pr) => section({ title: pr.subject || 'Unknown pull request', link: prHref(pr.subject) ? element('a', { class: 'in-link', href: prHref(pr.subject) }, 'On GitHub') : null },
      list(pr.rounds).filter((entry) => entry && typeof entry === 'object').map((entry) => round({ ...entry, subject: entry.subject || pr.subject }, alias)))),
    section({ title: 'Skipped rounds', count: skipped.length || null }, skipped.length
      ? events(skipped.map((row) => [`${row.subject || 'Unknown'} round ${row.round ?? '?'}`, row.reason || 'No reason recorded.']))
      : emptyLine('No round was skipped.')),
  ];
}

export function answer(found) {
  const data = obj(found?.report);
  const totals = obj(data.totals);
  if (!data.compared_at) {
    const arms = obj(found?.arms);
    const waiting = count(arms.queued) + count(arms.running);
    return waiting ? `${plural(waiting, 'replay')} still running.` : 'Not compared yet.';
  }
  return `${plural(count(totals.rounds), 'round')} compared: ${total(totals.both)} found by both, ${total(totals.claude_only)} by Claude only, ${total(totals.kimi_only)} by ${named(found.alias)} only.`;
}

export function render(body, ctx) {
  return page(body, ctx, {
    question: QUESTION, what: 'the replay reports',
    skeleton: () => skeletonRows(6),
    load: async () => {
      const replays = list((await ctx.api(LIST))?.replays);
      const wanted = ctx.query.get('experiment');
      const id = replays.some((row) => row.id === wanted) ? wanted : replays[0]?.id || null;
      const found = id ? await ctx.api(`${LIST}/${encodeURIComponent(id)}`) : null;
      return { replays, id, found };
    },
    draw: ({ replays, id, found }, head) => {
      if (!found) {
        head.answerNode.replaceChildren('No replays yet.');
        return emptyState('An admin starts one with armature server kimi-evaluate.');
      }
      head.node.append(filters(select({
        label: 'Comparison', value: id, onChange: (value) => ctx.setQuery({ experiment: value }),
        options: replays.map((row) => [row.id, `${row.project || row.team || 'Unknown'} on ${named(row.alias)}, ${String(row.created_at || '').slice(0, 10)}`]),
      })));
      head.answerNode.replaceChildren(answer(found));
      head.scopeNode.replaceChildren(`${found.team}${found.project ? ` · ${found.project}` : ''} · model ${found.model || 'unknown'}${found.report?.compared_at ? ` · compared ${String(found.report.compared_at).slice(0, 16).replace('T', ' ')}` : ''}`);
      return found.report ? draw(found) : emptyState('The report appears once every replay has ended.');
    },
  });
}
