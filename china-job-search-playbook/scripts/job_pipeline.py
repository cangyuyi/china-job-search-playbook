#!/usr/bin/env python3
"""Initialize, validate, deduplicate, prioritize, and review a China job-search pipeline."""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any


SOURCE_LEVELS = {
    "official_job",
    "official_notice",
    "certified_platform",
    "lead_only",
    "unavailable",
}
SOURCE_RANK = {
    "official_job": 5,
    "official_notice": 4,
    "certified_platform": 3,
    "lead_only": 2,
    "unavailable": 1,
}
RECOMMENDATIONS = {"优先投递", "可以投递", "待确认", "暂不建议", "已截止"}
JOB_APPLICATION_STATUS = {"open", "unknown", "closed"}
HARD_GATE_STATUS = {"pass", "conditional", "fail", "unknown"}
RECOMMENDATION_RANK = {
    "优先投递": 5,
    "可以投递": 4,
    "待确认": 3,
    "暂不建议": 2,
    "已截止": 1,
}
APPLICATION_STATES = [
    "待核验",
    "待投递",
    "已投递",
    "测评",
    "笔试",
    "AI面试",
    "群面",
    "业务面",
    "HR面",
    "终面",
    "意向",
    "Offer",
    "三方",
    "拒绝",
    "主动结束",
    "失联",
]
EMPLOYER_TYPES = {
    "central_soe",
    "local_soe",
    "public_institution",
    "internet_platform",
    "industry_leader",
    "foreign_invested",
    "quality_private",
    "specialized_sme",
    "regional_leader",
    "unicorn_company",
    "vc_backed",
    "small_champion",
    "research_commercialization",
}
COMPANY_SEARCH_STATES = {
    "not_checked",
    "entity_verified",
    "recruiting_signal",
    "job_verified",
    "tracking",
    "no_current_recruiting",
    "stale",
}


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"File not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Top-level JSON must be an object: {path}")
    return data


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def report(errors: list[str], warnings: list[str], **extra: Any) -> dict[str, Any]:
    payload = {
        "status": "fail" if errors else ("warn" if warnings else "pass"),
        "errors": errors,
        "warnings": warnings,
    }
    payload.update(extra)
    return payload


def exit_code(payload: dict[str, Any]) -> int:
    return 2 if payload["status"] == "fail" else (1 if payload["status"] == "warn" else 0)


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def job_key(job: dict[str, Any]) -> str:
    company = clean(job.get("company"))
    code = clean(job.get("job_code"))
    if company and code:
        return f"code|{company}|{code}"
    fields = (
        company,
        clean(job.get("title")),
        clean(job.get("location")),
        clean(job.get("program_or_batch")),
        clean(job.get("source_url")),
    )
    return "fallback|" + "|".join(fields)


def completeness(job: dict[str, Any]) -> int:
    important = (
        "company",
        "title",
        "location",
        "recruitment_type",
        "candidate_batch_or_experience",
        "education",
        "responsibilities",
        "deadline",
        "source_url",
        "verified_at",
    )
    return sum(bool(job.get(field)) for field in important)


def merge_jobs(primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
    result = dict(primary)
    for key, value in secondary.items():
        if not result.get(key) and value:
            result[key] = value
    for key in ("qualifications", "tools_or_technical_requirements", "responsibilities", "risk_or_unknowns"):
        values: list[Any] = []
        for source in (primary.get(key, []), secondary.get(key, [])):
            if isinstance(source, list):
                for item in source:
                    if item not in values:
                        values.append(item)
        if values:
            result[key] = values
    return result


def validate_jobs(data: dict[str, Any], stale_days: int) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        return report(["jobs must be a list."], [])

    ids: set[str] = set()
    keys: Counter[str] = Counter()
    employer_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    recommendation_counts: Counter[str] = Counter()
    stale_count = 0

    for index, job in enumerate(jobs):
        label = f"jobs[{index}]"
        if not isinstance(job, dict):
            errors.append(f"{label} must be an object.")
            continue
        job_id = str(job.get("job_id", "")).strip()
        if not job_id:
            errors.append(f"{label} has no job_id.")
        elif job_id in ids:
            errors.append(f"Duplicate job_id: {job_id}")
        ids.add(job_id)
        for field in ("company", "title"):
            if not str(job.get(field, "")).strip():
                errors.append(f"{job_id or label} has no {field}.")

        source_level = str(job.get("source_level", "")).strip()
        if source_level not in SOURCE_LEVELS:
            errors.append(f"{job_id or label} has invalid source_level: {source_level}")
        else:
            source_counts[source_level] += 1
        if source_level in {"official_job", "official_notice", "certified_platform"} and not job.get("source_url"):
            errors.append(f"{job_id or label} claims {source_level} but has no source_url.")
        if source_level == "lead_only" and job.get("recommendation") == "优先投递":
            errors.append(f"{job_id or label} is lead_only but marked 优先投递.")

        recommendation = str(job.get("recommendation", "")).strip()
        if recommendation not in RECOMMENDATIONS:
            errors.append(f"{job_id or label} has invalid recommendation: {recommendation}")
        else:
            recommendation_counts[recommendation] += 1

        application_status = str(job.get("application_status", "")).strip()
        if application_status and application_status not in JOB_APPLICATION_STATUS:
            errors.append(
                f"{job_id or label} has invalid application_status: {application_status}"
            )
        hard_gate_status = str(job.get("hard_gate_status", "")).strip()
        if hard_gate_status and hard_gate_status not in HARD_GATE_STATUS:
            errors.append(
                f"{job_id or label} has invalid hard_gate_status: {hard_gate_status}"
            )
        if application_status == "closed" and recommendation in {"优先投递", "可以投递"}:
            errors.append(f"{job_id or label} is closed but marked {recommendation}.")
        if hard_gate_status == "fail" and recommendation in {"优先投递", "可以投递"}:
            errors.append(f"{job_id or label} has a hard-gate failure but is marked {recommendation}.")

        employer_type = str(job.get("employer_type", "")).strip()
        if employer_type:
            employer_counts[employer_type] += 1
            if employer_type not in EMPLOYER_TYPES:
                warnings.append(f"{job_id or label} uses non-standard employer_type: {employer_type}")
        else:
            warnings.append(f"{job_id or label} has no employer_type.")

        verified_at = parse_date(job.get("verified_at"))
        if verified_at is None:
            warnings.append(f"{job_id or label} has no valid verified_at date.")
        elif (date.today() - verified_at).days > stale_days:
            stale_count += 1
            warnings.append(f"{job_id or label} was verified more than {stale_days} days ago.")

        if not job.get("next_action") and recommendation in {"优先投递", "可以投递", "待确认"}:
            warnings.append(f"{job_id or label} has no next_action.")
        keys[job_key(job)] += 1

    duplicate_groups = {key: count for key, count in keys.items() if count > 1}
    if duplicate_groups:
        warnings.append(f"Found {len(duplicate_groups)} duplicate job group(s).")

    expected_coverage = sorted(EMPLOYER_TYPES - set(employer_counts))
    return report(
        errors,
        warnings,
        counts={
            "jobs": len(jobs),
            "stale_jobs": stale_count,
            "duplicate_groups": len(duplicate_groups),
        },
        coverage={
            "employer_types": dict(sorted(employer_counts.items())),
            "source_levels": dict(sorted(source_counts.items())),
            "recommendations": dict(sorted(recommendation_counts.items())),
            "uncovered_employer_types": expected_coverage,
        },
        duplicates=duplicate_groups,
    )


def validate_companies(data: dict[str, Any], stale_days: int) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    companies = data.get("companies")
    if not isinstance(companies, list):
        return report(["companies must be a list."], [])

    ids: set[str] = set()
    names: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    tag_counts: Counter[str] = Counter()
    state_counts: Counter[str] = Counter()
    stale_count = 0

    for index, company in enumerate(companies):
        label = f"companies[{index}]"
        if not isinstance(company, dict):
            errors.append(f"{label} must be an object.")
            continue
        company_id = str(company.get("company_id", "")).strip()
        name = str(company.get("name", "")).strip()
        if not company_id:
            errors.append(f"{label} has no company_id.")
        elif company_id in ids:
            errors.append(f"Duplicate company_id: {company_id}")
        ids.add(company_id)
        if not name:
            errors.append(f"{company_id or label} has no name.")
        else:
            names[clean(name)] += 1

        employer_type = str(company.get("employer_type", "")).strip()
        if employer_type not in EMPLOYER_TYPES:
            errors.append(f"{company_id or label} has invalid employer_type: {employer_type}")
        else:
            type_counts[employer_type] += 1

        employer_tags = company.get("employer_tags", [])
        if employer_tags and not isinstance(employer_tags, list):
            errors.append(f"{company_id or label} employer_tags must be a list.")
        elif isinstance(employer_tags, list):
            for tag in employer_tags:
                tag_value = str(tag).strip()
                if tag_value not in EMPLOYER_TYPES:
                    errors.append(
                        f"{company_id or label} has invalid employer_tag: {tag_value}"
                    )
                else:
                    tag_counts[tag_value] += 1

        search_status = str(company.get("search_status", "")).strip()
        if search_status not in COMPANY_SEARCH_STATES:
            errors.append(f"{company_id or label} has invalid search_status: {search_status}")
        else:
            state_counts[search_status] += 1

        sources = company.get("discovery_sources", [])
        if not isinstance(sources, list) or not sources:
            warnings.append(f"{company_id or label} has no discovery_sources.")
        if search_status in {"recruiting_signal", "job_verified", "tracking"} and not company.get("career_url"):
            warnings.append(f"{company_id or label} has recruiting status but no career_url.")

        verified_at = parse_date(company.get("verified_at"))
        if verified_at is None:
            warnings.append(f"{company_id or label} has no valid verified_at date.")
        elif (date.today() - verified_at).days > stale_days:
            stale_count += 1
            warnings.append(f"{company_id or label} was verified more than {stale_days} days ago.")

    duplicate_names = {name: count for name, count in names.items() if count > 1}
    if duplicate_names:
        warnings.append(f"Found {len(duplicate_names)} duplicate company name group(s).")
    covered_types = set(type_counts) | set(tag_counts)
    return report(
        errors,
        warnings,
        counts={
            "companies": len(companies),
            "stale_companies": stale_count,
            "duplicate_name_groups": len(duplicate_names),
        },
        coverage={
            "employer_types": dict(sorted(type_counts.items())),
            "employer_tags": dict(sorted(tag_counts.items())),
            "search_states": dict(sorted(state_counts.items())),
            "uncovered_employer_types": sorted(EMPLOYER_TYPES - covered_types),
        },
        duplicate_names=duplicate_names,
    )


def command_init_workspace(args: argparse.Namespace) -> int:
    target = args.out.resolve()
    if target.exists() and not target.is_dir():
        payload = report([f"Target exists and is not a directory: {target}"], [])
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    if target.exists() and any(target.iterdir()) and not args.force:
        payload = report([f"Target is not empty: {target}"], [])
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    target.mkdir(parents=True, exist_ok=True)
    assets = Path(__file__).resolve().parent.parent / "assets"
    mapping = {
        "candidate-profile-template.json": "candidate-profile.json",
        "company-pool-template.json": "companies.json",
        "job-pool-template.json": "jobs.json",
        "research-progress-template.json": "research-progress.json",
        "source-snapshots-template.json": "source-snapshots.json",
        "monitoring-template.json": "monitoring.json",
        "applications-template.csv": "applications.csv",
        "training-template.csv": "training.csv",
    }
    created = []
    for source_name, target_name in mapping.items():
        destination = target / target_name
        if destination.exists() and not args.force:
            continue
        shutil.copyfile(assets / source_name, destination)
        created.append(target_name)
    for name in ("companies", "applications", "reports", "source-captures"):
        (target / name).mkdir(exist_ok=True)
    payload = report([], [], workspace=str(target), created=created)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def command_validate(args: argparse.Namespace) -> int:
    payload = validate_jobs(load_json(args.jobs), args.stale_days)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code(payload)


def command_validate_companies(args: argparse.Namespace) -> int:
    payload = validate_companies(load_json(args.companies), args.stale_days)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code(payload)


def command_dedupe(args: argparse.Namespace) -> int:
    data = load_json(args.jobs)
    jobs = data.get("jobs", [])
    if not isinstance(jobs, list):
        payload = report(["jobs must be a list."], [])
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in jobs:
        if isinstance(item, dict):
            grouped.setdefault(job_key(item), []).append(item)
    output: list[dict[str, Any]] = []
    merged_groups = []
    for key, group in grouped.items():
        ranked = sorted(
            group,
            key=lambda job: (
                SOURCE_RANK.get(str(job.get("source_level")), 0),
                completeness(job),
                parse_date(job.get("verified_at")) or date.min,
            ),
            reverse=True,
        )
        merged = ranked[0]
        for other in ranked[1:]:
            merged = merge_jobs(merged, other)
        output.append(merged)
        if len(group) > 1:
            merged_groups.append({"key": key, "job_ids": [item.get("job_id") for item in group]})
    result = dict(data)
    result["jobs"] = output
    result["deduplicated_at"] = date.today().isoformat()
    save_json(args.out, result)
    payload = report(
        [],
        [],
        input_jobs=len(jobs),
        output_jobs=len(output),
        merged_groups=merged_groups,
        output=str(args.out),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def command_prioritize(args: argparse.Namespace) -> int:
    data = load_json(args.jobs)
    jobs = data.get("jobs", [])
    if not isinstance(jobs, list):
        payload = report(["jobs must be a list."], [])
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    def score(job: dict[str, Any]) -> tuple[Any, ...]:
        recommendation = RECOMMENDATION_RANK.get(str(job.get("recommendation")), 0)
        match = job.get("match_score")
        numeric_match = float(match) if isinstance(match, (int, float)) else -1.0
        source = SOURCE_RANK.get(str(job.get("source_level")), 0)
        deadline = parse_date(job.get("deadline")) or date.max
        return (-recommendation, -numeric_match, -source, deadline, clean(job.get("company")))

    sorted_jobs = sorted((job for job in jobs if isinstance(job, dict)), key=score)
    for index, job in enumerate(sorted_jobs, 1):
        job["priority_rank"] = index
    result = dict(data)
    result["jobs"] = sorted_jobs
    result["prioritized_at"] = date.today().isoformat()
    save_json(args.out, result)
    payload = report([], [], jobs=len(sorted_jobs), output=str(args.out))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except FileNotFoundError:
        raise ValueError(f"File not found: {path}") from None


def ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def command_funnel(args: argparse.Namespace) -> int:
    rows = read_csv_rows(args.applications)
    states = Counter(str(row.get("当前状态", "")).strip() for row in rows)
    invalid = sorted(state for state in states if state and state not in APPLICATION_STATES)
    submitted_states = set(APPLICATION_STATES[2:])
    progressed_states = set(APPLICATION_STATES[3:13])
    interview_states = {"AI面试", "群面", "业务面", "HR面", "终面", "意向", "Offer", "三方"}
    final_interview_states = {"终面", "意向", "Offer", "三方"}
    offer_states = {"Offer", "三方"}

    submitted = sum(count for state, count in states.items() if state in submitted_states)
    progressed = sum(count for state, count in states.items() if state in progressed_states)
    interviews = sum(count for state, count in states.items() if state in interview_states)
    finals = sum(count for state, count in states.items() if state in final_interview_states)
    offers = sum(count for state, count in states.items() if state in offer_states)
    missing_next_action = sum(
        1
        for row in rows
        if str(row.get("当前状态", "")).strip() not in {"拒绝", "主动结束"}
        and not str(row.get("下一动作", "")).strip()
    )
    warnings = []
    if invalid:
        warnings.append("Unknown application states: " + ", ".join(invalid))
    if missing_next_action:
        warnings.append(f"{missing_next_action} active row(s) have no next action.")
    payload = report(
        [],
        warnings,
        counts={
            "rows": len(rows),
            "submitted": submitted,
            "progressed_after_submission": progressed,
            "interview_stage": interviews,
            "final_stage": finals,
            "offers": offers,
            "missing_next_action": missing_next_action,
        },
        rates={
            "screening_or_progress_rate": ratio(progressed, submitted),
            "interview_rate": ratio(interviews, submitted),
            "offer_per_final_rate": ratio(offers, finals),
            "offer_per_submission_rate": ratio(offers, submitted),
        },
        state_counts=dict(states),
        small_sample=submitted < 5,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_workspace = subparsers.add_parser("init-workspace")
    init_workspace.add_argument("--out", type=Path, required=True)
    init_workspace.add_argument("--force", action="store_true")
    init_workspace.set_defaults(func=command_init_workspace)

    validate = subparsers.add_parser("validate-jobs")
    validate.add_argument("jobs", type=Path)
    validate.add_argument("--stale-days", type=int, default=30)
    validate.set_defaults(func=command_validate)

    validate_companies_parser = subparsers.add_parser("validate-companies")
    validate_companies_parser.add_argument("companies", type=Path)
    validate_companies_parser.add_argument("--stale-days", type=int, default=60)
    validate_companies_parser.set_defaults(func=command_validate_companies)

    dedupe = subparsers.add_parser("dedupe")
    dedupe.add_argument("--jobs", type=Path, required=True)
    dedupe.add_argument("--out", type=Path, required=True)
    dedupe.set_defaults(func=command_dedupe)

    prioritize = subparsers.add_parser("prioritize")
    prioritize.add_argument("--jobs", type=Path, required=True)
    prioritize.add_argument("--out", type=Path, required=True)
    prioritize.set_defaults(func=command_prioritize)

    funnel = subparsers.add_parser("funnel")
    funnel.add_argument("applications", type=Path)
    funnel.set_defaults(func=command_funnel)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except ValueError as exc:
        print(json.dumps(report([str(exc)], []), ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())
