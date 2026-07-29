#!/usr/bin/env python3
"""Track search progress, version source facts, and manage job-search refreshes."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


SEARCH_ROUNDS = {
    "company_discovery",
    "program_discovery",
    "job_deep_check",
    "refresh",
}
RESULT_STATES = {"completed", "partial", "blocked", "no_result"}
SOURCE_LEVELS = {
    "official_job",
    "official_notice",
    "certified_platform",
    "lead_only",
    "unavailable",
}
SOURCE_TYPES = {"company", "program", "job", "policy", "search_result", "other"}
TARGET_TYPES = {"company", "program", "job", "policy", "search_query", "other"}
PRIORITIES = {"critical", "high", "medium", "low"}
WATCH_STATES = {"enabled", "paused", "closed"}


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def load_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"File not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Top-level JSON must be an object: {path}")
    return data


def save_object(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json_object(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    return load_object(path)


def normalize_text(text: str) -> str:
    text = text.replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", " ", text).strip()


def stable_hash(value: Any) -> str:
    if isinstance(value, str):
        payload = normalize_text(value)
    else:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_id(prefix: str, seed: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{prefix}_{stamp}_{stable_hash(seed)[:8]}"


def parse_date(value: str, label: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD: {value}") from exc


def list_arg(values: list[str] | None) -> list[str]:
    return [value.strip() for value in (values or []) if value.strip()]


def field_diff(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    changes = []
    for key in sorted(set(old) | set(new)):
        old_value = old.get(key)
        new_value = new.get(key)
        if old_value != new_value:
            changes.append({"field": key, "old": old_value, "new": new_value})
    return changes


def command_log_search(args: argparse.Namespace) -> int:
    data = load_object(args.progress)
    events = data.setdefault("search_events", [])
    if not isinstance(events, list):
        raise ValueError("search_events must be a list.")
    checked_at = args.checked_at or now_iso()
    event = {
        "event_id": make_id("search", f"{args.round}|{args.query}|{checked_at}"),
        "checked_at": checked_at,
        "round": args.round,
        "query": args.query,
        "dimensions": {
            "cities": list_arg(args.city),
            "industries": list_arg(args.industry),
            "job_families": list_arg(args.job_family),
            "employer_types": list_arg(args.employer_type),
        },
        "source_url": args.source_url or "",
        "source_level": args.source_level or "",
        "result_status": args.result_status,
        "pages_checked": args.pages_checked,
        "companies_found": args.companies_found,
        "programs_found": args.programs_found,
        "jobs_found": args.jobs_found,
        "next_cursor": args.next_cursor or "",
        "notes": args.notes or "",
    }
    events.append(event)
    data["updated_at"] = checked_at
    save_object(args.progress, data)
    print(json.dumps({"status": "pass", "event": event}, ensure_ascii=False, indent=2))
    return 0


def covered_values(events: list[dict[str, Any]], key: str) -> set[str]:
    result: set[str] = set()
    for event in events:
        dimensions = event.get("dimensions", {})
        if isinstance(dimensions, dict):
            values = dimensions.get(key, [])
            if isinstance(values, list):
                result.update(str(value).strip() for value in values if str(value).strip())
    return result


def command_audit_progress(args: argparse.Namespace) -> int:
    data = load_object(args.progress)
    events = data.get("search_events", [])
    if not isinstance(events, list):
        raise ValueError("search_events must be a list.")
    valid_events = [event for event in events if isinstance(event, dict)]
    round_counts = Counter(str(event.get("round", "")) for event in valid_events)
    status_counts = Counter(str(event.get("result_status", "")) for event in valid_events)
    errors = []
    for index, event in enumerate(valid_events):
        if event.get("round") not in SEARCH_ROUNDS:
            errors.append(f"search_events[{index}] has invalid round.")
        if event.get("result_status") not in RESULT_STATES:
            errors.append(f"search_events[{index}] has invalid result_status.")

    scope = data.get("scope", {})
    if not isinstance(scope, dict):
        scope = {}
        errors.append("scope must be an object.")
    dimensions = {
        "cities": covered_values(valid_events, "cities"),
        "industries": covered_values(valid_events, "industries"),
        "job_families": covered_values(valid_events, "job_families"),
        "employer_types": covered_values(valid_events, "employer_types"),
    }
    uncovered = {
        key: sorted(
            str(value)
            for value in scope.get(key, [])
            if str(value) and str(value) not in covered
        )
        for key, covered in dimensions.items()
    }
    open_cursors = [
        {
            "event_id": event.get("event_id"),
            "round": event.get("round"),
            "query": event.get("query"),
            "next_cursor": event.get("next_cursor"),
        }
        for event in valid_events
        if event.get("next_cursor")
    ]
    totals = {
        field: sum(
            int(event.get(field, 0))
            for event in valid_events
            if isinstance(event.get(field, 0), int)
        )
        for field in ("pages_checked", "companies_found", "programs_found", "jobs_found")
    }
    payload = {
        "status": "fail" if errors else "pass",
        "errors": errors,
        "events": len(valid_events),
        "round_counts": dict(sorted(round_counts.items())),
        "missing_core_rounds": sorted(
            {"company_discovery", "program_discovery", "job_deep_check"} - set(round_counts)
        ),
        "result_status_counts": dict(sorted(status_counts.items())),
        "totals": totals,
        "uncovered_scope": uncovered,
        "open_cursors": open_cursors,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 2 if errors else 0


def snapshots_for(data: dict[str, Any], source_id: str) -> list[dict[str, Any]]:
    snapshots = data.get("snapshots", [])
    if not isinstance(snapshots, list):
        raise ValueError("snapshots must be a list.")
    return [
        item
        for item in snapshots
        if isinstance(item, dict) and item.get("source_id") == source_id
    ]


def find_snapshot(data: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
    for item in data.get("snapshots", []):
        if isinstance(item, dict) and item.get("snapshot_id") == snapshot_id:
            return item
    raise ValueError(f"Snapshot not found: {snapshot_id}")


def update_watch_after_capture(
    watchlist_path: Path,
    watch_id: str,
    snapshot: dict[str, Any],
) -> None:
    data = load_object(watchlist_path)
    items = data.get("watch_items", [])
    if not isinstance(items, list):
        raise ValueError("watch_items must be a list.")
    item = next(
        (value for value in items if isinstance(value, dict) and value.get("watch_id") == watch_id),
        None,
    )
    if item is None:
        raise ValueError(f"Watch item not found: {watch_id}")
    checked_date = datetime.fromisoformat(snapshot["retrieved_at"]).date()
    cadence = int(item.get("cadence_days", 7))
    item["last_checked"] = checked_date.isoformat()
    item["next_check"] = (checked_date + timedelta(days=cadence)).isoformat()
    item["last_snapshot_id"] = snapshot["snapshot_id"]
    item["last_change"] = snapshot["change_status"]
    item["last_error"] = (
        "" if snapshot["access_status"] == "accessible" else snapshot["access_status"]
    )
    data["updated_at"] = snapshot["retrieved_at"]
    save_object(watchlist_path, data)


def command_capture(args: argparse.Namespace) -> int:
    data = load_object(args.store)
    snapshots = data.setdefault("snapshots", [])
    if not isinstance(snapshots, list):
        raise ValueError("snapshots must be a list.")
    if (
        args.content_file is None
        and args.fields_json is None
        and args.access_status == "accessible"
    ):
        raise ValueError(
            "Accessible capture requires --content-file or --fields-json."
        )

    text = ""
    if args.content_file is not None:
        try:
            text = args.content_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ValueError(f"File not found: {args.content_file}") from None
    fields = read_json_object(args.fields_json)
    previous_items = snapshots_for(data, args.source_id)
    previous = previous_items[-1] if previous_items else None
    retrieved_at = args.retrieved_at or now_iso()
    content_hash = stable_hash(text) if args.content_file is not None else ""
    fields_hash = stable_hash(fields)
    changes = (
        field_diff(previous.get("observed_fields", {}), fields)
        if previous and args.fields_json is not None
        else []
    )
    if previous is None:
        change_status = "new"
    elif (
        previous.get("content_hash") == content_hash
        and previous.get("fields_hash") == fields_hash
        and previous.get("access_status") == args.access_status
    ):
        change_status = "unchanged"
    else:
        change_status = "changed"

    snapshot = {
        "snapshot_id": make_id("snap", f"{args.source_id}|{retrieved_at}|{content_hash}|{fields_hash}"),
        "source_id": args.source_id,
        "source_type": args.source_type,
        "source_url": args.url,
        "source_level": args.source_level,
        "retrieved_at": retrieved_at,
        "access_status": args.access_status,
        "content_observed": args.content_file is not None,
        "content_hash": content_hash,
        "content_length": len(normalize_text(text)) if args.content_file is not None else None,
        "fields_observed": args.fields_json is not None,
        "fields_hash": fields_hash,
        "observed_fields": fields,
        "previous_snapshot_id": previous.get("snapshot_id") if previous else "",
        "previous_access_status": previous.get("access_status") if previous else "",
        "change_status": change_status,
        "field_changes": changes,
        "notes": args.notes or "",
    }
    snapshots.append(snapshot)
    data["updated_at"] = retrieved_at
    save_object(args.store, data)
    if args.watchlist is not None or args.watch_id:
        if args.watchlist is None or not args.watch_id:
            raise ValueError("--watchlist and --watch-id must be used together.")
        update_watch_after_capture(args.watchlist, args.watch_id, snapshot)
    print(json.dumps({"status": "pass", "snapshot": snapshot}, ensure_ascii=False, indent=2))
    return 0


def command_diff(args: argparse.Namespace) -> int:
    data = load_object(args.store)
    if args.from_snapshot or args.to_snapshot:
        if not args.from_snapshot or not args.to_snapshot:
            raise ValueError("--from-snapshot and --to-snapshot must be used together.")
        old = find_snapshot(data, args.from_snapshot)
        new = find_snapshot(data, args.to_snapshot)
    else:
        items = snapshots_for(data, args.source_id)
        if len(items) < 2:
            raise ValueError(f"At least two snapshots are required for source_id: {args.source_id}")
        old, new = items[-2], items[-1]
    payload = {
        "status": "pass",
        "source_id": new.get("source_id"),
        "from_snapshot": old.get("snapshot_id"),
        "to_snapshot": new.get("snapshot_id"),
        "content_changed": (
            old.get("content_hash") != new.get("content_hash")
            if old.get("content_observed") and new.get("content_observed")
            else None
        ),
        "access_changed": old.get("access_status") != new.get("access_status"),
        "field_changes": (
            field_diff(
                old.get("observed_fields", {}),
                new.get("observed_fields", {}),
            )
            if new.get("fields_observed")
            else []
        ),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def command_add_watch(args: argparse.Namespace) -> int:
    data = load_object(args.watchlist)
    items = data.setdefault("watch_items", [])
    if not isinstance(items, list):
        raise ValueError("watch_items must be a list.")
    if any(
        isinstance(item, dict) and item.get("watch_id") == args.watch_id
        for item in items
    ):
        raise ValueError(f"Duplicate watch_id: {args.watch_id}")
    if args.cadence_days < 1:
        raise ValueError("cadence_days must be at least 1.")
    start = parse_date(args.start_date, "start_date") if args.start_date else date.today()
    item = {
        "watch_id": args.watch_id,
        "source_id": args.source_id,
        "target_type": args.target_type,
        "url": args.url,
        "priority": args.priority,
        "cadence_days": args.cadence_days,
        "last_checked": "",
        "next_check": start.isoformat(),
        "last_snapshot_id": "",
        "last_change": "",
        "last_error": "",
        "state": "enabled",
        "owner": args.owner or "",
        "notes": args.notes or "",
    }
    items.append(item)
    data["updated_at"] = now_iso()
    save_object(args.watchlist, data)
    print(json.dumps({"status": "pass", "watch_item": item}, ensure_ascii=False, indent=2))
    return 0


def command_due(args: argparse.Namespace) -> int:
    data = load_object(args.watchlist)
    items = data.get("watch_items", [])
    if not isinstance(items, list):
        raise ValueError("watch_items must be a list.")
    as_of = parse_date(args.as_of, "as_of") if args.as_of else date.today()
    due_items = []
    errors = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"watch_items[{index}] must be an object.")
            continue
        if item.get("state") != "enabled":
            continue
        next_check = str(item.get("next_check", "")).strip()
        if not next_check:
            due_items.append(item)
            continue
        try:
            due_date = parse_date(next_check, f"watch_items[{index}].next_check")
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if due_date <= as_of:
            due_items.append(item)
    due_items.sort(
        key=lambda item: (
            {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(
                str(item.get("priority")), 9
            ),
            str(item.get("next_check", "")),
            str(item.get("watch_id", "")),
        )
    )
    payload = {
        "status": "fail" if errors else "pass",
        "errors": errors,
        "as_of": as_of.isoformat(),
        "due_count": len(due_items),
        "due_items": due_items,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 2 if errors else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    log_search = commands.add_parser("log-search")
    log_search.add_argument("--progress", type=Path, required=True)
    log_search.add_argument("--round", choices=sorted(SEARCH_ROUNDS), required=True)
    log_search.add_argument("--query", required=True)
    log_search.add_argument("--result-status", choices=sorted(RESULT_STATES), required=True)
    log_search.add_argument("--source-url")
    log_search.add_argument("--source-level", choices=sorted(SOURCE_LEVELS))
    log_search.add_argument("--city", action="append")
    log_search.add_argument("--industry", action="append")
    log_search.add_argument("--job-family", action="append")
    log_search.add_argument("--employer-type", action="append")
    log_search.add_argument("--pages-checked", type=int, default=0)
    log_search.add_argument("--companies-found", type=int, default=0)
    log_search.add_argument("--programs-found", type=int, default=0)
    log_search.add_argument("--jobs-found", type=int, default=0)
    log_search.add_argument("--next-cursor")
    log_search.add_argument("--checked-at")
    log_search.add_argument("--notes")
    log_search.set_defaults(func=command_log_search)

    audit_progress = commands.add_parser("audit-progress")
    audit_progress.add_argument("--progress", type=Path, required=True)
    audit_progress.set_defaults(func=command_audit_progress)

    capture = commands.add_parser("capture")
    capture.add_argument("--store", type=Path, required=True)
    capture.add_argument("--source-id", required=True)
    capture.add_argument("--source-type", choices=sorted(SOURCE_TYPES), required=True)
    capture.add_argument("--source-level", choices=sorted(SOURCE_LEVELS), required=True)
    capture.add_argument("--url", required=True)
    capture.add_argument("--content-file", type=Path)
    capture.add_argument("--fields-json", type=Path)
    capture.add_argument("--access-status", default="accessible")
    capture.add_argument("--retrieved-at")
    capture.add_argument("--notes")
    capture.add_argument("--watchlist", type=Path)
    capture.add_argument("--watch-id")
    capture.set_defaults(func=command_capture)

    diff = commands.add_parser("diff")
    diff.add_argument("--store", type=Path, required=True)
    diff.add_argument("--source-id")
    diff.add_argument("--from-snapshot")
    diff.add_argument("--to-snapshot")
    diff.set_defaults(func=command_diff)

    add_watch = commands.add_parser("add-watch")
    add_watch.add_argument("--watchlist", type=Path, required=True)
    add_watch.add_argument("--watch-id", required=True)
    add_watch.add_argument("--source-id", required=True)
    add_watch.add_argument("--target-type", choices=sorted(TARGET_TYPES), required=True)
    add_watch.add_argument("--url", required=True)
    add_watch.add_argument("--priority", choices=sorted(PRIORITIES), default="medium")
    add_watch.add_argument("--cadence-days", type=int, default=7)
    add_watch.add_argument("--start-date")
    add_watch.add_argument("--owner")
    add_watch.add_argument("--notes")
    add_watch.set_defaults(func=command_add_watch)

    due = commands.add_parser("due")
    due.add_argument("--watchlist", type=Path, required=True)
    due.add_argument("--as-of")
    due.set_defaults(func=command_due)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except ValueError as exc:
        print(
            json.dumps(
                {"status": "fail", "errors": [str(exc)]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
