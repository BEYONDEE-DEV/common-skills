#!/usr/bin/env python3
"""Prepare submit review material and expand reviewed prose without interpreting it."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import verify_pr as verifier


INLINE_BYTES = 24 * 1024


def required_fields(state: str, paths: list[str], committed_paths: list[str], plan: dict,
                    *, revalidation: bool = False, rotation_required: bool = False) -> list[str]:
    if state == "RECOVER":
        return []
    fields = []
    if paths or (state != "OPEN" and committed_paths):
        if not plan.get("task_summary") and (
                (paths and not plan.get("commit_message")) or
                (state != "OPEN" and not (plan.get("pr_title") and plan.get("pr_body")))):
            fields.append("task_summary")
        if state != "OPEN" and rotation_required and not plan.get("task_summary") and \
                not plan.get("transfer_subject") and "task_summary" not in fields:
            fields.append("transfer_subject")
    if revalidation and not plan.get("validation"):
        fields.append("validation")
    return fields


def expand_prose(plan: dict) -> dict:
    result = dict(plan)
    summary = result.get("task_summary")
    if isinstance(summary, str) and summary.strip():
        if any(ord(char) < 32 or ord(char) == 127 for char in summary):
            raise ValueError("task_summary must be a single subject without control characters")
        # Explicit overrides allow different commit/continuation/PR scopes and conventions.
        for field in ("commit_message", "pr_title", "transfer_subject"):
            if not result.get(field):
                result[field] = summary
        if not result.get("pr_body"):
            result["pr_body"] = "## 변경 내용\n\n" + summary
            result["generated_pr_body"] = True
    return result


def publication_body(plan: dict, phases: list[dict]) -> str:
    body = plan["pr_body"]
    if not plan.get("generated_pr_body") or body != "## 변경 내용\n\n" + plan.get("task_summary", ""):
        return body
    validation = next((phase for phase in reversed(phases) if phase["phase"] == "validation"), {})
    if validation.get("ok") and validation.get("executed"):
        evidence = "실행한 검증 명령이 모두 성공했습니다.\n\n```json\n" + \
            json.dumps(plan["validation"], ensure_ascii=False, indent=2) + "\n```"
    else:
        evidence = "이번 submit에서 검증 명령을 실행하지 않았습니다."
    return body + "\n\n## 검증\n\n" + evidence


def action(state: str, paths: list[str], committed_paths: list[str]) -> str:
    if state == "RECOVER":
        return "RECOVER_PUBLICATION"
    if state == "OPEN":
        return "COMMIT_AND_WAIT" if paths else "WAIT"
    return "SUBMIT" if paths or committed_paths else "SYNC_ONLY"


def write_material(repo: Path, directory: Path, *, head: str, boundary: str,
                   committed_head: str, include_committed: bool) -> str:
    directory.mkdir(parents=True, exist_ok=False)
    remaining = INLINE_BYTES

    def section(name: str, raw: bytes) -> dict:
        nonlocal remaining
        target = directory / name
        target.write_bytes(raw)
        result = {"path": str(target), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        if b"\0" not in raw:
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                pass
            else:
                if len(raw) <= remaining:
                    result["content"] = content
                    remaining -= len(raw)
        return result

    options = ["--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "--full-index"]
    committed = (verifier.git(repo, "diff", *options, boundary, committed_head, "--")
                 if include_committed else b"")
    tracked = verifier.git(repo, "diff", *options, head, "--")
    staged = verifier.git(repo, "diff", "--cached", *options, "--")
    unstaged = verifier.git(repo, "diff", *options, "--")
    untracked = []
    names = verifier.git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    for name in names.decode(errors="surrogateescape").split("\0"):
        if not name:
            continue
        path = repo / name
        if path.is_symlink():
            raw = os.fsencode(os.readlink(path))
            kind = "symlink"
        elif path.is_file():
            raw = path.read_bytes()
            kind = "file"
        else:
            raise RuntimeError("untracked review input must be a regular file or symlink: " + name)
        key = hashlib.sha256(name.encode(errors="surrogateescape")).hexdigest()
        untracked.append({"file": name, "kind": kind, "mode": oct(path.lstat().st_mode),
                          **section(key + ".content", raw)})
    packet = {
        "head_sha": head, "boundary_sha": boundary, "committed_head_sha": committed_head,
        "committed": section("committed.diff", committed),
        "tracked": section("tracked.diff", tracked),
        "staged": section("staged.diff", staged), "unstaged": section("unstaged.diff", unstaged),
        "untracked": untracked,
    }
    target = directory / "review.json"
    target.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8", errors="backslashreplace")
    return str(target)
