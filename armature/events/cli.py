"""`armature emit` without the rest of Armature: `python -m armature.events`."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from armature.events.emitter import get_emitter


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="armature emit",
        description="Send an event to ARMATURE_EVENTS. Does nothing when it is unset.",
    )
    parser.add_argument("kind", nargs="?", help="Event kind, such as step_start or finding")
    parser.add_argument("--step", help="Step id the event belongs to")
    parser.add_argument("--json", dest="json_payload", help="Payload as a JSON object")
    parser.add_argument("--field", dest="fields", action="append", default=[],
                        help="Payload field as key=value, repeatable")
    parser.add_argument("--stream-claude", action="store_true",
                        help="Read Claude Code stream-json on stdin, emit its events, and copy stdin to stdout")
    return parser


def _payload(parser: argparse.ArgumentParser, args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if args.json_payload:
        try:
            payload = json.loads(args.json_payload)
        except ValueError as exc:
            parser.error(f"--json is not valid JSON: {exc}")
        if not isinstance(payload, dict):
            parser.error("--json must be a JSON object")
    for item in args.fields:
        key, sep, value = item.partition("=")
        if not sep or not key:
            parser.error(f"--field must be key=value, got {item!r}")
        try:
            payload[key] = json.loads(value)
        except ValueError:
            payload[key] = value
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.stream_claude:
        from armature.events.claude_stream import pipe
        emitter = get_emitter()
        pipe(emitter, args.step, sys.stdin.buffer, sys.stdout.buffer)
        emitter.close()
        return 0
    if not args.kind:
        parser.error("KIND is required unless --stream-claude is set")
    payload = _payload(parser, args)
    emitter = get_emitter()
    emitter.emit(args.kind, args.step, **payload)
    emitter.close()
    return 0
