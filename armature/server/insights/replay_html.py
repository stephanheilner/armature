"""The kimi-evaluate report as one self-contained HTML file: Claude's and the replay's findings side by side.

It follows the daily report's file (report_file.py): inline CSS, no script, light and dark, every value escaped,
because a finding's text is code review content. A version 1 report has counts only, and renders without the
findings.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from armature.server.insights import report_file
from armature.server.insights.report import pr_link

esc, money = report_file.esc, report_file.money
SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
SEVERITIES = ("high", "medium", "low")
VERDICTS = {"valid": ("Judged valid", "ok"), "not_valid": ("Judged not valid", "bad"),
            "not_judged": ("Not judged", "")}
DECISIONS = {"block": "Lead: block", "suggest": "Lead: suggest", "drop": "Lead: drop", "fixed": "Lead: fixed"}

CSS = report_file.CSS + """
.tiles { grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); }
.vs { color: var(--muted); font-size: 13px; }
h2 a { text-decoration: none; }
details.round { border-top: 1px solid var(--rule); padding: 10px 0; }
details.round:first-of-type { border-top: 0; }
details.round > summary { cursor: pointer; list-style: none; display: flex; flex-wrap: wrap; gap: 6px 14px;
  align-items: baseline; }
details.round > summary::-webkit-details-marker { display: none; }
details.round > summary::before { content: "▸"; color: var(--muted); }
details.round[open] > summary::before { content: "▾"; }
.round-name { font-weight: 650; }
.facts { color: var(--muted); font-size: 13px; }
.runs { color: var(--muted); font-size: 13px; margin: 8px 0 10px; }
.grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 10px; margin-top: 6px; }
.col-head { font-size: 12px; font-weight: 650; letter-spacing: 0.04em; text-transform: uppercase;
  color: var(--muted); }
.card { border: 1px solid var(--rule); border-radius: 10px; padding: 10px 12px; background: var(--page);
  min-width: 0; }
.card.none { border-style: dashed; color: var(--muted); font-size: 13px; display: flex; align-items: center;
  justify-content: center; min-height: 48px; }
.side { display: none; font-size: 11px; font-weight: 650; letter-spacing: 0.04em; text-transform: uppercase;
  color: var(--muted); margin-bottom: 4px; }
.badges { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 6px; }
.badge { font-size: 11.5px; line-height: 1.5; padding: 0 8px; border-radius: 999px; border: 1px solid var(--rule);
  color: var(--muted); white-space: nowrap; }
.badge.high, .badge.bad { color: var(--bad); border-color: var(--bad); }
.badge.medium { color: var(--warn); border-color: var(--warn); }
.badge.ok { color: var(--ok); border-color: var(--ok); }
.badge.accent { color: var(--accent); border-color: var(--accent); }
.where { font-family: var(--mono); font-size: 12.5px; overflow-wrap: anywhere; }
.claim { margin: 4px 0 2px; font-weight: 560; overflow-wrap: anywhere; }
.reason { color: var(--muted); font-size: 13px; margin: 2px 0; overflow-wrap: anywhere; }
details.body { margin-top: 4px; }
details.body > summary { cursor: pointer; color: var(--muted); font-size: 12.5px; }
.body-text { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13px; margin: 6px 0 0; }
@media (max-width: 640px) {
  .grid { grid-template-columns: minmax(0, 1fr); }
  .col-head { display: none; }
  .side { display: block; }
}
"""


def _get(data: Any, key: str, default: Any = None) -> Any:
    return data.get(key, default) if isinstance(data, dict) else default


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _per(total: Any, rounds: int) -> float | None:
    return total / rounds if isinstance(total, int | float) and rounds else None


def _minutes(value: Any) -> str:
    return f"{value:,.1f} min" if isinstance(value, int | float) else "—"


def file_link(subject: Any, head: Any, path: Any, line: Any) -> str | None:
    """The file at the round's head on GitHub, built only from a checked repository, commit, and path."""
    found = pr_link(subject if isinstance(subject, str) else None)
    if not found or not SLUG.fullmatch(found["repo"]) or not isinstance(head, str) or not SHA.fullmatch(head) \
            or not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/") \
            or '"' in path or "'" in path:
        return None
    anchor = f"#L{line}" if isinstance(line, int) and not isinstance(line, bool) and line > 0 else ""
    return f"https://github.com/{found['repo']}/blob/{head}/{quote(path, safe='/._-')}{anchor}"


def _badge(text: str, tone: str = "") -> str:
    return f'<span class="badge {esc(tone)}">{esc(text)}</span>'


def _card(card: Any, side: str, subject: Any, head: Any, one_sided: bool, alias: str) -> str:
    if not isinstance(card, dict):
        return f'<div class="card none">Not raised by {esc("Claude" if side == "claude" else alias)}</div>'
    badges = []
    severity = card.get("severity")
    badges.append(_badge(severity.capitalize(), severity) if severity in SEVERITIES else _badge("Severity unknown"))
    if card.get("blocking") is True:
        badges.append(_badge("Blocking", "bad"))
    elif card.get("blocking") is False:
        badges.append(_badge("Not blocking"))
    if card.get("decision") in DECISIONS:
        badges.append(_badge(DECISIONS[card["decision"]], "bad" if card["decision"] == "block" else ""))
    if one_sided and card.get("verdict") in VERDICTS:
        badges.append(_badge(*VERDICTS[card["verdict"]]))
    if side == "claude" and card.get("fixed"):
        badges.append(_badge("Fixed by author", "accent"))
    path, line = card.get("path"), card.get("line")
    where = f"{path or '(no file)'}{f':{line}' if isinstance(line, int) else ''}"
    href = file_link(subject, head, path, line)
    where_html = f'<a class="where" href="{esc(href)}">{esc(where)}</a>' if href \
        else f'<span class="where">{esc(where)}</span>'
    reason = f'<p class="reason">{esc(card["reason"])}</p>' if card.get("reason") else ""
    body = card.get("body") or ""
    detail = (f'<details class="body"><summary>Details</summary><p class="body-text">{esc(body)}</p></details>'
              if body and body != card.get("claim") else "")
    label = "Claude" if side == "claude" else alias
    return (f'<div class="card"><div class="side">{esc(label)}</div><div class="badges">{"".join(badges)}</div>'
            f'{where_html}<p class="claim">{esc(card.get("claim") or "")}</p>{reason}{detail}</div>')


def _side_facts(run: Any) -> str:
    tokens = f"{_int(_get(run, 'input_tokens')):,} in, {_int(_get(run, 'output_tokens')):,} out"
    return f"{money(_get(run, 'cost_usd'))}, {tokens}, {_minutes(_get(run, 'minutes'))}"


def _round(r: dict[str, Any], alias: str) -> str:
    verdict, rate = _get(r, "verdict", {}) or {}, _get(r, "rate", {}) or {}
    counts = (f"both {_get(_get(r, 'both'), 'total', 0)}, Claude only {_get(_get(r, 'claude_only'), 'total', 0)}, "
              f"{alias} only {_get(_get(r, 'kimi_only'), 'total', 0)}")
    head = r.get("head") if isinstance(r.get("head"), str) else ""
    summary = (f'<summary><span class="round-name">Round {esc(r.get("round"))}</span>'
               f'<span class="facts">{esc(head[:7]) + " · " if head else ""}'
               f'verdict Claude {esc(verdict.get("claude") or "—")}, {esc(alias)} {esc(verdict.get("kimi") or "—")}'
               f' · {esc(alias)} rated {esc(rate.get("kimi") or "—")}, Claude risk {esc(rate.get("risk") or "—")}'
               f' · {esc(counts)}</span></summary>')
    runs = (f'<p class="runs">Claude {esc(_side_facts(r.get("claude_run")))} · {esc(alias)} '
            f'{esc(_side_facts(r.get("kimi_run")))}</p>')
    found = r.get("findings")
    if not isinstance(found, dict):
        return (f'<details class="round">{summary}{runs}'
                f'{report_file._empty("This report predates side-by-side findings. Rebuild it to see them.")}'
                f'</details>')
    rows = []
    subject = r.get("subject")
    for pair in found.get("pairs") or []:
        rows.append((_get(pair, "claude"), _get(pair, "kimi"), False))
    rows += [(card, None, True) for card in found.get("claude_only") or []]
    rows += [(None, card, True) for card in found.get("kimi_only") or []]
    if not rows:
        return f'<details class="round">{summary}{runs}{report_file._empty("Neither side raised a finding.")}</details>'
    cells = "".join(_card(left, "claude", subject, head, one, alias) + _card(right, "kimi", subject, head, one, alias)
                    for left, right, one in rows)
    grid = (f'<div class="grid"><div class="col-head">Claude</div><div class="col-head">{esc(alias)}</div>'
            f'{cells}</div>')
    return f'<details class="round">{summary}{runs}{grid}</details>'


def _pr_name(subject: Any) -> str:
    found = pr_link(subject if isinstance(subject, str) else None)
    return f'<a href="{esc(found["url"])}">{esc(subject)}</a>' if found else esc(subject or "unknown")


def _pr_row(subject: Any, t: dict[str, Any], alias: str) -> list[str]:
    claude, kimi = _get(t, "claude_run", {}), _get(t, "kimi_run", {})
    rounds = _int(_get(t, "rounds"))
    return [_pr_name(subject), esc(rounds), esc(_get(_get(t, "both"), "total", 0)),
            esc(_get(_get(t, "claude_only"), "total", 0)), esc(_get(_get(t, "kimi_only"), "total", 0)),
            esc(_int(_get(t, "fixed_missed"))), esc(_int(_get(t, "kimi_only_valid"))),
            esc(f"{_int(_get(t, 'verdict_agrees'))} of {rounds}") if "verdict_agrees" in t else "—",
            esc(f"{money(_get(kimi, 'cost_usd'))} vs {money(_get(claude, 'cost_usd'))}")]


def render(data: dict[str, Any]) -> str:
    alias = str(data.get("alias") or "replay")
    alias = alias[:1].upper() + alias[1:]
    totals = data.get("totals") if isinstance(data.get("totals"), dict) else {}
    rounds = _int(totals.get("rounds"))
    claude, kimi = _get(totals, "claude_run", {}) or {}, _get(totals, "kimi_run", {}) or {}
    tile = report_file._tile
    agree = f"{_int(totals.get('verdict_agrees'))} of {rounds}" if "verdict_agrees" in totals else "—"
    tiles = "".join((
        tile("Rounds compared", str(rounds), f"{len(data.get('skipped') or [])} skipped"),
        tile("Found by both", str(_get(_get(totals, "both"), "total", 0))),
        tile("Claude only", str(_get(_get(totals, "claude_only"), "total", 0))),
        tile(f"{alias} only", str(_get(_get(totals, "kimi_only"), "total", 0))),
        tile(f"Fixed by author, {alias} missed", str(_int(totals.get("fixed_missed"))),
             f"of {_int(totals.get('fixed'))} the author fixed"),
        tile(f"{alias} only, judged valid", str(_int(totals.get("kimi_only_valid"))),
             f"of {_get(_get(totals, 'kimi_only'), 'total', 0)}"),
        tile("Verdict agreement", agree),
        tile("Claude cost per round", money(_per(_get(claude, "cost_usd"), rounds))),
        tile(f"{alias} cost per round", money(_per(_get(kimi, "cost_usd"), rounds))),
        tile("Claude minutes per round", _minutes(_per(_get(claude, "minutes"), rounds))),
        tile(f"{alias} minutes per round", _minutes(_per(_get(kimi, "minutes"), rounds))),
    ))
    prs = [p for p in data.get("prs") or [] if isinstance(p, dict)]
    headers = [("Pull request", False), ("Rounds", True), ("Both", True), ("Claude only", True),
               (f"{alias} only", True), (f"Fixed, {alias} missed", True), (f"{alias} only, valid", True),
               ("Verdicts agree", True), (f"Cost, {alias} vs Claude", True)]
    table = report_file._table(headers, [_pr_row(p.get("subject"), p.get("totals") or {}, alias) for p in prs]) \
        if prs else report_file._empty("No replay finished.")
    per_pr = "".join(f'<section><h2>{_pr_name(p.get("subject"))}</h2>'
                     f'{"".join(_round(r, alias) for r in p.get("rounds") or [] if isinstance(r, dict))}</section>'
                     for p in prs)
    skipped = data.get("skipped") or []
    skipped_table = report_file._table(
        [("Pull request", False), ("Round", True), ("Why", False)],
        [[_pr_name(s.get("subject")), esc(s.get("round")), esc(s.get("reason"))] for s in skipped
         if isinstance(s, dict)]) if skipped else report_file._empty("No round was skipped.")
    judge = data.get("judge") if isinstance(data.get("judge"), dict) else {}
    note = (f"Judge {judge.get('alias') or 'unavailable'}, {_int(judge.get('calls'))} calls, "
            f"{money(judge.get('cost_usd'))}. {judge.get('unavailable') or judge.get('stopped') or ''}").strip()
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{esc(alias)} replay of Claude reviews</title><style>{CSS}</style></head><body><main>'
            f'<h1>Claude reviews replayed on {esc(alias)}</h1>'
            f'<p class="sub">Model {esc(data.get("model"))}. Compared {esc(data.get("compared_at"))}. Each round ran '
            f'the review again with every model step on {esc(alias)} and every other step answering as Claude\'s '
            f'round did.</p><div class="tiles">{tiles}</div>'
            + report_file._section("Pull requests", table, "Counts of the lead's findings on each side.")
            + per_pr
            + report_file._section("Skipped rounds", skipped_table)
            + f'<footer>{esc(note)}</footer></main></body></html>\n')
