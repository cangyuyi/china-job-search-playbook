#!/usr/bin/env python3
"""Manage a verified resume profile and plan one evidence-backed resume per job."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any


PROFILE_STATES = {"verified", "pending", "do_not_use"}
MATCH_STATES = {"pass", "conditional", "fail", "unknown"}


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


def issue_report(errors: list[str], warnings: list[str], extra: dict | None = None) -> dict:
    report = {
        "status": "fail" if errors else ("warn" if warnings else "pass"),
        "errors": errors,
        "warnings": warnings,
    }
    if extra:
        report.update(extra)
    return report


def validate_profile_data(profile: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    required = {"schema_version", "person", "preferences", "experiences", "claims"}
    missing = sorted(required - profile.keys())
    if missing:
        errors.append("Missing top-level fields: " + ", ".join(missing))

    experiences = profile.get("experiences", [])
    claims = profile.get("claims", [])
    if not isinstance(experiences, list):
        errors.append("experiences must be a list.")
        experiences = []
    if not isinstance(claims, list):
        errors.append("claims must be a list.")
        claims = []

    experience_ids: set[str] = set()
    for index, experience in enumerate(experiences):
        experience_id = str(experience.get("experience_id", "")).strip()
        if not experience_id:
            errors.append(f"experiences[{index}] has no experience_id.")
        elif experience_id in experience_ids:
            errors.append(f"Duplicate experience_id: {experience_id}")
        experience_ids.add(experience_id)

    claim_ids: set[str] = set()
    verified = pending = disabled = 0
    for index, claim in enumerate(claims):
        claim_id = str(claim.get("claim_id", "")).strip()
        if not claim_id:
            errors.append(f"claims[{index}] has no claim_id.")
        elif claim_id in claim_ids:
            errors.append(f"Duplicate claim_id: {claim_id}")
        claim_ids.add(claim_id)

        experience_id = str(claim.get("experience_id", "")).strip()
        if experience_id and experience_id not in experience_ids:
            errors.append(f"{claim_id or f'claims[{index}]'} references unknown experience_id: {experience_id}")

        verification = claim.get("verification")
        if verification not in PROFILE_STATES:
            errors.append(
                f"{claim_id or f'claims[{index}]'} has invalid verification: {verification}"
            )
        elif verification == "verified":
            verified += 1
        elif verification == "pending":
            pending += 1
        else:
            disabled += 1

        strength = claim.get("strength")
        if not isinstance(strength, (int, float)) or not 1 <= strength <= 5:
            errors.append(f"{claim_id or f'claims[{index}]'} strength must be 1-5.")
        if not str(claim.get("statement", "")).strip():
            warnings.append(f"{claim_id or f'claims[{index}]'} has an empty statement.")
        if verification == "verified" and not str(claim.get("source", "")).strip():
            warnings.append(f"{claim_id} is verified but has no source note.")

    for skill_index, skill in enumerate(profile.get("skills", [])):
        for claim_id in skill.get("evidence_claim_ids", []):
            if claim_id not in claim_ids:
                errors.append(f"skills[{skill_index}] references unknown claim_id: {claim_id}")

    person = profile.get("person", {})
    if not str(person.get("name", "")).strip():
        warnings.append("person.name is empty.")
    if not str(person.get("email", "")).strip():
        warnings.append("person.email is empty.")
    if not profile.get("consent_to_persist", False):
        warnings.append("consent_to_persist is false; treat this profile as task-local.")

    return issue_report(
        errors,
        warnings,
        {
            "counts": {
                "experiences": len(experiences),
                "claims": len(claims),
                "verified_claims": verified,
                "pending_claims": pending,
                "disabled_claims": disabled,
            }
        },
    )


def validate_jd_data(jd: dict[str, Any], known_claim_ids: set[str] | None = None) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    required = {"job_id", "company", "title", "hard_requirements", "responsibilities"}
    missing = sorted(required - jd.keys())
    if missing:
        errors.append("Missing JD fields: " + ", ".join(missing))

    responsibility_ids: set[str] = set()
    total_weight = 0.0
    for index, responsibility in enumerate(jd.get("responsibilities", [])):
        responsibility_id = str(responsibility.get("responsibility_id", "")).strip()
        if not responsibility_id:
            errors.append(f"responsibilities[{index}] has no responsibility_id.")
        elif responsibility_id in responsibility_ids:
            errors.append(f"Duplicate responsibility_id: {responsibility_id}")
        responsibility_ids.add(responsibility_id)
        weight = responsibility.get("weight")
        if not isinstance(weight, (int, float)) or weight < 0:
            errors.append(f"{responsibility_id or f'responsibilities[{index}]'} has invalid weight.")
        else:
            total_weight += float(weight)

    if jd.get("responsibilities") and abs(total_weight - 100.0) > 0.1:
        warnings.append(f"Responsibility weights total {total_weight:g}, not 100.")

    requirement_ids: set[str] = set()
    match_counts = {state: 0 for state in MATCH_STATES}
    for index, requirement in enumerate(jd.get("hard_requirements", [])):
        requirement_id = str(requirement.get("requirement_id", "")).strip()
        if not requirement_id:
            errors.append(f"hard_requirements[{index}] has no requirement_id.")
        elif requirement_id in requirement_ids:
            errors.append(f"Duplicate requirement_id: {requirement_id}")
        requirement_ids.add(requirement_id)

        match = requirement.get("profile_match")
        if match not in MATCH_STATES:
            errors.append(f"{requirement_id or f'hard_requirements[{index}]'} has invalid profile_match.")
        else:
            match_counts[match] += 1

        if known_claim_ids is not None:
            for claim_id in requirement.get("evidence_ids", []):
                if claim_id not in known_claim_ids:
                    errors.append(f"{requirement_id} references unknown claim_id: {claim_id}")

    if not str(jd.get("source_url", "")).strip():
        warnings.append("JD source_url is empty.")
    if not str(jd.get("verified_at", "")).strip():
        warnings.append("JD verified_at is empty.")

    return issue_report(
        errors,
        warnings,
        {
            "responsibility_weight_total": total_weight,
            "hard_requirement_matches": match_counts,
        },
    )


def flatten_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(flatten_strings(item))
        return result
    return []


def canonical_tokens(values: list[str]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        normalized = re.sub(r"\s+", " ", value).strip().lower()
        if not normalized:
            continue
        tokens.add(normalized)
        tokens.update(
            token
            for token in re.findall(r"[a-z][a-z0-9+#./-]{1,}|[\u4e00-\u9fff]{2,8}", normalized)
            if len(token) >= 2
        )
    return tokens


def claim_tokens(claim: dict[str, Any]) -> set[str]:
    values = []
    for key in ("tags", "tasks", "tools"):
        values.extend(flatten_strings(claim.get(key, [])))
    values.extend(
        flatten_strings(
            [
                claim.get("statement", ""),
                claim.get("outcome_type", ""),
                claim.get("responsibility_scope", ""),
            ]
        )
    )
    return canonical_tokens(values)


def responsibility_tokens(responsibility: dict[str, Any]) -> set[str]:
    values = []
    for key in ("tags", "tasks", "tools", "keywords"):
        values.extend(flatten_strings(responsibility.get(key, [])))
    values.append(str(responsibility.get("text", "")))
    return canonical_tokens(values)


def parse_year(value: Any) -> int | None:
    match = re.search(r"(19|20)\d{2}", str(value or ""))
    return int(match.group(0)) if match else None


def recency_score(claim: dict[str, Any], experience: dict[str, Any] | None) -> float:
    year = parse_year(claim.get("date"))
    if year is None and experience:
        year = parse_year(experience.get("end_date")) or parse_year(experience.get("start_date"))
    if year is None:
        return 5.0
    age = max(0, date.today().year - year)
    if age <= 1:
        return 15.0
    if age <= 2:
        return 13.0
    if age <= 4:
        return 10.0
    if age <= 6:
        return 7.0
    return 4.0


def relevance_details(
    claim: dict[str, Any], responsibilities: list[dict[str, Any]]
) -> tuple[float, list[dict[str, Any]]]:
    c_tokens = claim_tokens(claim)
    matches = []
    weighted_overlap = 0.0
    total_weight = sum(float(item.get("weight", 0)) for item in responsibilities) or 100.0
    for responsibility in responsibilities:
        r_tokens = responsibility_tokens(responsibility)
        overlap = sorted(c_tokens & r_tokens)
        ratio = len(overlap) / max(1, min(len(r_tokens), 8))
        ratio = min(1.0, ratio)
        weight = float(responsibility.get("weight", 0))
        weighted_overlap += weight * ratio
        if overlap:
            matches.append(
                {
                    "responsibility_id": responsibility.get("responsibility_id"),
                    "weight": weight,
                    "overlap": overlap,
                    "overlap_ratio": round(ratio, 3),
                }
            )
    relevance = 40.0 * weighted_overlap / total_weight
    return round(relevance, 2), matches


def distinctiveness_score(claim: dict[str, Any]) -> float:
    score = 0.0
    if claim.get("allowed_numbers"):
        score += 4.0
    if str(claim.get("outcome_type", "")).strip():
        score += 3.0
    if int(claim.get("strength", 1)) >= 4:
        score += 2.0
    if str(claim.get("source", "")).strip():
        score += 1.0
    return min(10.0, score)


def transferability_score(matches: list[dict[str, Any]], claim: dict[str, Any]) -> float:
    if not matches:
        return 0.0
    score = 4.0 + min(4.0, len(matches) * 1.5)
    if claim.get("tools"):
        score += 1.0
    if claim.get("tasks"):
        score += 1.0
    return min(10.0, score)


def plan(profile: dict[str, Any], jd: dict[str, Any], top: int) -> tuple[dict[str, Any], int]:
    profile_report = validate_profile_data(profile)
    claim_lookup = {
        claim.get("claim_id"): claim
        for claim in profile.get("claims", [])
        if claim.get("claim_id")
    }
    jd_report = validate_jd_data(jd, set(claim_lookup))
    if profile_report["errors"] or jd_report["errors"]:
        result = {
            "status": "fail",
            "profile_validation": profile_report,
            "jd_validation": jd_report,
        }
        return result, 2

    experience_lookup = {
        item.get("experience_id"): item for item in profile.get("experiences", [])
    }
    responsibilities = jd.get("responsibilities", [])
    ranked = []
    pending_relevant = []
    for claim in profile.get("claims", []):
        relevance, matches = relevance_details(claim, responsibilities)
        verification = claim.get("verification")
        if verification != "verified" or claim.get("sensitive", False):
            if relevance > 0 and verification == "pending":
                pending_relevant.append(
                    {
                        "claim_id": claim.get("claim_id"),
                        "statement": claim.get("statement"),
                        "relevance": relevance,
                    }
                )
            continue

        strength = float(claim.get("strength", 1)) / 5.0 * 25.0
        recent = recency_score(
            claim, experience_lookup.get(claim.get("experience_id"))
        )
        distinctive = distinctiveness_score(claim)
        transferable = transferability_score(matches, claim)
        total = relevance + strength + recent + distinctive + transferable
        ranked.append(
            {
                "claim_id": claim.get("claim_id"),
                "experience_id": claim.get("experience_id"),
                "statement": claim.get("statement"),
                "score": round(total, 2),
                "score_breakdown": {
                    "relevance": relevance,
                    "evidence_strength": round(strength, 2),
                    "recency": recent,
                    "distinctiveness": distinctive,
                    "transferability": transferable,
                },
                "responsibility_matches": matches,
            }
        )

    ranked.sort(key=lambda item: (-item["score"], str(item["claim_id"])))
    selected = ranked[:top]
    selected_ids = {item["claim_id"] for item in selected}

    coverage = []
    uncovered = []
    for responsibility in responsibilities:
        responsibility_id = responsibility.get("responsibility_id")
        matches = [
            item
            for item in selected
            if any(
                detail["responsibility_id"] == responsibility_id
                for detail in item["responsibility_matches"]
            )
        ]
        entry = {
            "responsibility_id": responsibility_id,
            "text": responsibility.get("text"),
            "weight": responsibility.get("weight"),
            "claim_ids": [item["claim_id"] for item in matches],
        }
        coverage.append(entry)
        if not matches:
            uncovered.append(entry)

    hard_requirements = jd_report["hard_requirement_matches"]
    questions = []
    for requirement in jd.get("hard_requirements", []):
        if requirement.get("profile_match") in {"unknown", "conditional"}:
            questions.append(
                {
                    "type": "hard_requirement",
                    "requirement_id": requirement.get("requirement_id"),
                    "question": f"请确认：{requirement.get('text', '')}",
                }
            )
    for item in sorted(pending_relevant, key=lambda value: -value["relevance"])[:5]:
        questions.append(
            {
                "type": "pending_claim",
                "claim_id": item["claim_id"],
                "question": f"请核实这条事实及口径：{item['statement']}",
            }
        )

    status = "blocked" if hard_requirements["fail"] else (
        "warn" if uncovered or questions or profile_report["warnings"] or jd_report["warnings"] else "pass"
    )
    result = {
        "status": status,
        "job": {
            "job_id": jd.get("job_id"),
            "company": jd.get("company"),
            "title": jd.get("title"),
        },
        "eligibility": {
            "hard_requirement_matches": hard_requirements,
            "decision": (
                "hard_fail_present"
                if hard_requirements["fail"]
                else "proceed_with_tailoring"
            ),
        },
        "selected_claims": selected,
        "coverage": coverage,
        "uncovered_responsibilities": uncovered,
        "pending_relevant_claims": pending_relevant,
        "questions": questions,
        "profile_validation": profile_report,
        "jd_validation": jd_report,
        "planning_notes": {
            "selected_claim_count": len(selected_ids),
            "instruction": "Use ranking as an audit trail; apply semantic judgment before drafting.",
        },
    }
    return result, 2 if status == "blocked" else (1 if status == "warn" else 0)


def extract_number_tokens(text: str) -> set[str]:
    # Chinese characters count as ``\w`` in Python, so ASCII-only boundaries
    # are required to catch numbers in text such as “覆盖1200人”.
    return set(
        re.findall(r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?%?(?![A-Za-z0-9_])", text)
    )


def validate_manifest_data(profile: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    claim_lookup = {
        claim.get("claim_id"): claim
        for claim in profile.get("claims", [])
        if claim.get("claim_id")
    }
    if not manifest.get("job_id"):
        errors.append("Manifest job_id is empty.")
    if not manifest.get("resume_file"):
        warnings.append("Manifest resume_file is empty.")

    for index, bullet in enumerate(manifest.get("bullets", [])):
        bullet_id = bullet.get("bullet_id") or f"bullets[{index}]"
        text = str(bullet.get("text", "")).strip()
        if not text:
            errors.append(f"{bullet_id} has empty text.")
        claim_ids = bullet.get("claim_ids", [])
        if not claim_ids:
            errors.append(f"{bullet_id} has no claim_ids.")
            continue

        permitted_text = []
        for claim_id in claim_ids:
            claim = claim_lookup.get(claim_id)
            if claim is None:
                errors.append(f"{bullet_id} references unknown claim_id: {claim_id}")
                continue
            verification = claim.get("verification")
            if verification != "verified":
                errors.append(f"{bullet_id} references non-verified claim: {claim_id} ({verification})")
            if claim.get("sensitive", False):
                errors.append(f"{bullet_id} references sensitive claim: {claim_id}")
            permitted_text.append(str(claim.get("statement", "")))
            permitted_text.extend(str(value) for value in claim.get("allowed_numbers", []))

        allowed_numbers = extract_number_tokens(" ".join(permitted_text))
        used_numbers = extract_number_tokens(text)
        untraceable = sorted(used_numbers - allowed_numbers)
        if untraceable:
            errors.append(
                f"{bullet_id} contains untraceable number(s): {', '.join(untraceable)}"
            )
        if bullet.get("status") != "final":
            warnings.append(f"{bullet_id} status is not final.")

    if not manifest.get("bullets"):
        errors.append("Manifest has no bullets.")
    return issue_report(
        errors,
        warnings,
        {
            "bullet_count": len(manifest.get("bullets", [])),
            "job_id": manifest.get("job_id"),
        },
    )


def command_init_profile(args: argparse.Namespace) -> int:
    template = Path(__file__).resolve().parent.parent / "assets" / "candidate-profile-template.json"
    if args.out.exists() and not args.force:
        print(json.dumps({"status": "fail", "error": f"Output exists: {args.out}"}, ensure_ascii=False))
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, args.out)
    print(json.dumps({"status": "pass", "profile": str(args.out)}, ensure_ascii=False, indent=2))
    return 0


def command_validate_profile(args: argparse.Namespace) -> int:
    report = validate_profile_data(load_json(args.profile))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report["status"] == "fail" else (1 if report["status"] == "warn" else 0)


def command_validate_jd(args: argparse.Namespace) -> int:
    report = validate_jd_data(load_json(args.jd))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report["status"] == "fail" else (1 if report["status"] == "warn" else 0)


def command_plan(args: argparse.Namespace) -> int:
    result, code = plan(load_json(args.profile), load_json(args.jd), args.top)
    save_json(args.out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


def command_validate_manifest(args: argparse.Namespace) -> int:
    profile = load_json(args.profile)
    profile_report = validate_profile_data(profile)
    if profile_report["errors"]:
        report = {
            "status": "fail",
            "errors": ["Profile validation failed before manifest validation."],
            "warnings": [],
            "profile_validation": profile_report,
        }
    else:
        report = validate_manifest_data(profile, load_json(args.manifest))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report["status"] == "fail" else (1 if report["status"] == "warn" else 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_profile = subparsers.add_parser("init-profile")
    init_profile.add_argument("--out", type=Path, required=True)
    init_profile.add_argument("--force", action="store_true")
    init_profile.set_defaults(func=command_init_profile)

    validate_profile = subparsers.add_parser("validate-profile")
    validate_profile.add_argument("profile", type=Path)
    validate_profile.set_defaults(func=command_validate_profile)

    validate_jd = subparsers.add_parser("validate-jd")
    validate_jd.add_argument("jd", type=Path)
    validate_jd.set_defaults(func=command_validate_jd)

    planning = subparsers.add_parser("plan")
    planning.add_argument("--profile", type=Path, required=True)
    planning.add_argument("--jd", type=Path, required=True)
    planning.add_argument("--out", type=Path, required=True)
    planning.add_argument("--top", type=int, default=12)
    planning.set_defaults(func=command_plan)

    manifest = subparsers.add_parser("validate-manifest")
    manifest.add_argument("--profile", type=Path, required=True)
    manifest.add_argument("--manifest", type=Path, required=True)
    manifest.set_defaults(func=command_validate_manifest)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except ValueError as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())
