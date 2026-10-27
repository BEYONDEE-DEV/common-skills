#!/usr/bin/env python3
"""Read-only inspection for multi-repo Git worktree bundles."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

SKIP_DIRS = {
    ".git",
    ".gradle",
    ".idea",
    ".run",
    "build",
    "dist",
    "node_modules",
}


def run_git(path: Path, args: list[str]) -> str | None:
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    proc = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(path), *args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def discover_candidates(root: Path, max_depth: int) -> list[Path]:
    candidates: list[Path] = []

    def walk(path: Path, depth: int) -> None:
        if depth > max_depth:
            return
        git_marker = path / ".git"
        if git_marker.exists():
            top_level = run_git(path, ["rev-parse", "--show-toplevel"])
            if top_level:
                candidates.append(Path(top_level).resolve())
        if depth == max_depth:
            return
        try:
            children = sorted(p for p in path.iterdir() if p.is_dir())
        except OSError:
            return
        for child in children:
            if child.name in SKIP_DIRS:
                continue
            walk(child, depth + 1)

    walk(root.resolve(), 0)
    return sorted(set(candidates))


def git_common_dir(repo: Path) -> Path | None:
    value = run_git(repo, ["rev-parse", "--git-common-dir"])
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = repo / path
    return path.resolve()


def current_branch(path: Path) -> str:
    branch = run_git(path, ["branch", "--show-current"])
    if branch:
        return branch
    return "(detached)"


def upstream(path: Path) -> str:
    value = run_git(path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    return value or "-"


def short_head(path: Path, fallback: str | None = None) -> str:
    value = run_git(path, ["rev-parse", "--short", "HEAD"])
    return value or fallback or "(no HEAD)"


def dirty_state(path: Path) -> str:
    value = run_git(path, ["status", "--porcelain"])
    if value is None:
        return "unknown"
    return "dirty" if value else "clean"


def git_path(path: Path, name: str) -> Path | None:
    value = run_git(path, ["rev-parse", "--git-path", name])
    if not value:
        return None
    result = Path(value)
    if not result.is_absolute():
        result = path / result
    return result.resolve()


def operation_state(path: Path) -> str:
    markers = [
        ("rebase", ("rebase-merge", "rebase-apply")),
        ("merge", ("MERGE_HEAD",)),
        ("cherry-pick", ("CHERRY_PICK_HEAD",)),
        ("revert", ("REVERT_HEAD",)),
        ("bisect", ("BISECT_LOG",)),
    ]
    for state, names in markers:
        for name in names:
            marker = git_path(path, name)
            if marker is not None and marker.exists():
                return state
    return "-"


def parse_worktree_list(repo: Path) -> list[dict[str, str]]:
    output = run_git(repo, ["worktree", "list", "--porcelain"])
    if output is None:
        return []
    rows: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        if not line:
            if current:
                rows.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            if current:
                rows.append(current)
            current = {"path": value}
        elif key == "HEAD":
            current["head"] = value[:12]
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key == "bare":
            current["bare"] = "true"
        elif key == "detached":
            current["branch"] = "(detached)"
        elif key in {"locked", "prunable"}:
            current[key] = value or "true"
    if current:
        rows.append(current)
    return rows


def inspect(root: Path, max_depth: int) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    for repo in discover_candidates(root, max_depth):
        common = git_common_dir(repo)
        if common is None:
            continue
        key = str(common)
        if key not in groups:
            groups[key] = {
                "common_git_dir": key,
                "discovered_from": str(repo),
                "worktrees": [],
            }
            for item in parse_worktree_list(repo):
                path_text = item.get("path", "")
                path = Path(path_text)
                if path.is_dir():
                    branch = item.get("branch") or current_branch(path)
                    item.update(
                        {
                            "path": str(path),
                            "branch": branch,
                            "upstream": upstream(path),
                            "head": short_head(path, item.get("head")),
                            "dirty": dirty_state(path),
                            "operation_state": operation_state(path),
                        }
                    )
                groups[key]["worktrees"].append(item)

    ordered = sorted(groups.values(), key=lambda group: group["common_git_dir"])
    return {
        "root": str(root.resolve()),
        "repository_group_count": len(ordered),
        "worktree_count": sum(len(group["worktrees"]) for group in ordered),
        "groups": ordered,
    }


def bundle_info(root: Path, worktree_path: Path) -> tuple[str, Path, str] | None:
    try:
        rel = worktree_path.relative_to(root)
    except ValueError:
        return None
    if len(rel.parts) >= 3 and rel.parts[0] == "worktrees":
        bundle_rel = Path(*rel.parts[:2]).as_posix()
        bundle_base = root / rel.parts[0] / rel.parts[1]
        folder_rel = Path(*rel.parts[2:]).as_posix()
        return bundle_rel, bundle_base, folder_rel
    if root.parent.name == "worktrees" and rel.parts:
        return f"worktrees/{root.name}", root, rel.as_posix()
    if rel.parts:
        return "workspace", root, rel.as_posix()
    return "workspace", root, "."


def source_branch_for(root: Path, group: dict[str, Any], current_path: Path) -> str:
    branches: set[str] = set()
    for wt in group["worktrees"]:
        path_text = wt.get("path")
        if not path_text:
            continue
        path = Path(path_text)
        if path == current_path:
            continue
        info = bundle_info(root, path)
        branch = wt.get("branch", "-")
        if info and info[0] == "workspace" and branch not in {"-", "(detached)"}:
            branches.add(branch)
    if len(branches) == 1:
        return f"{next(iter(branches))} (추정)"
    return "확인 필요"


def build_bundles(data: dict[str, Any]) -> list[dict[str, Any]]:
    root = Path(data["root"])
    bundles: dict[str, dict[str, Any]] = {}

    for group in data["groups"]:
        for wt in group["worktrees"]:
            path_text = wt.get("path")
            if not path_text:
                continue
            path = Path(path_text)
            info = bundle_info(root, path)
            if info is None:
                continue
            name, base, folder = info
            bundle = bundles.setdefault(
                name,
                {
                    "name": name,
                    "base": str(base),
                    "items": [],
                    "source_branches": [],
                    "source_unknown": False,
                },
            )
            source_branch = source_branch_for(root, group, path)
            if source_branch != "확인 필요":
                bundle["source_branches"].append(source_branch)
            else:
                bundle["source_unknown"] = True
            bundle["items"].append(
                {
                    **wt,
                    "repo": Path(folder).parts[0] if folder != "." else path.name,
                    "folder": folder,
                    "source_branch": source_branch,
                    "common_git_dir": group["common_git_dir"],
                }
            )

    return [
        bundles[name]
        for name in sorted(bundles, key=lambda value: (not value.startswith("worktrees/"), value))
    ]


def select_bundles(bundles: list[dict[str, Any]], selector: str | None) -> list[dict[str, Any]]:
    if selector is None:
        return bundles

    value = selector.strip().rstrip("/\\")
    direct_names = {value, value.replace("\\", "/")}
    if not value.startswith("worktrees/") and not value.startswith("worktrees\\"):
        direct_names.add(f"worktrees/{value}")

    requested_path = Path(value).expanduser()
    if requested_path.is_absolute():
        requested_base = str(requested_path.resolve())
    else:
        requested_base = None

    return [
        bundle
        for bundle in bundles
        if bundle["name"] in direct_names
        or (requested_base is not None and str(Path(bundle["base"]).resolve()) == requested_base)
    ]


def resolve_bundle(root: Path, selector: str) -> tuple[Path, str | None, dict[str, Any]]:
    """Resolve the manifest before doing any Git discovery; never guess a slot binding."""
    from slot_manifest import load_manifest, manifest_path, ManifestError

    root = root.resolve()
    value = selector.replace("\\", "/").rstrip("/")
    candidate = Path(value).expanduser()
    candidates = {candidate.resolve()} if candidate.is_absolute() else {
        (root / value).resolve(), (root / "worktrees" / value).resolve(),
    }
    if value in {root.name, f"worktrees/{root.name}"}:
        candidates.add(root)
    path = manifest_path(root)
    if path.exists():
        slots = load_manifest(path)["slots"]
        matches = [(name, slot) for name, slot in slots.items()
                   if name == value or Path(slot["path"]).resolve() in candidates]
        if len(matches) != 1:
            raise ManifestError("bundle must match exactly one manifest slot")
        name, slot = matches[0]
        return Path(slot["path"]).resolve(), name, slot["repositories"]
    existing = [path for path in candidates if path.is_dir() and path.is_relative_to(root)]
    if len(existing) != 1:
        raise ManifestError("bundle must resolve to one directory under the workspace")
    return existing[0], None, {}


def inspect_bundle(root: Path, selector: str, max_depth: int = 4) -> dict[str, Any]:
    base, slot_name, bindings = resolve_bundle(root, selector)
    repos = [base / name for name in bindings] if slot_name else discover_candidates(base, max_depth)
    groups: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    errors: list[str] = []
    for repo in repos:
        if repo.resolve() != repo or not repo.is_relative_to(base):
            errors.append(f"repository path escapes or aliases its bundle: {repo}")
            continue
        top = run_git(repo, ["rev-parse", "--show-toplevel"])
        common = git_common_dir(repo) if top and Path(top).resolve() == repo else None
        if common is None:
            errors.append(f"missing or mismatched repository: {repo}")
            continue
        worktrees = parse_worktree_list(repo)
        branch = current_branch(repo)
        binding = bindings.get(repo.name, {})
        rotation = binding.get("rotation", {})
        expected = rotation.get("target_branch") if rotation.get("phase") in {"switched", "retired"} else binding.get("branch")
        for wt in worktrees:
            if wt.get("branch") in {branch, expected} and Path(wt["path"]).resolve() != repo:
                conflicts.append({"repository": repo.name, "branch": wt["branch"], "path": wt["path"]})
        item = {"path": str(repo), "branch": branch, "head": short_head(repo),
                "upstream": upstream(repo), "dirty": dirty_state(repo),
                "operation_state": operation_state(repo)}
        # Other worktrees supply branch occupancy/inference only: never run status in them.
        item["source_branch"] = binding.get("source_branch") or source_branch_for(
            root.resolve(), {"worktrees": worktrees}, repo)
        group = groups.setdefault(str(common), {"common_git_dir": str(common),
                                               "discovered_from": str(repo), "worktrees": []})
        group["worktrees"].append(item)
    data = {"root": str(root.resolve()), "repository_group_count": len(groups),
            "worktree_count": sum(len(g["worktrees"]) for g in groups.values()),
            "groups": list(groups.values()), "conflicts": conflicts, "errors": errors,
            "discovery": "manifest" if slot_name else "bundle", "slot": slot_name}
    items = [{**wt, "repo": Path(wt["path"]).name,
              "folder": Path(wt["path"]).relative_to(base).as_posix(),
              "common_git_dir": group["common_git_dir"]}
             for group in groups.values() for wt in group["worktrees"]]
    data["bundles"] = [{"name": (base.relative_to(root.resolve()).as_posix()
                                  if base != root.resolve() and base.is_relative_to(root.resolve())
                                  else f"worktrees/{base.name}"),
                        "base": str(base), "items": items,
                        "source_branches": [item["source_branch"] for item in items
                                            if item["source_branch"] != "확인 필요"],
                        "source_unknown": any(item["source_branch"] == "확인 필요" for item in items)}]
    return data


def print_status_block(bundle: dict[str, Any]) -> None:
    source_branches = sorted(set(bundle["source_branches"]))
    if bundle.get("source_unknown"):
        source_branch = "확인 필요"
    elif len(source_branches) == 1:
        source_branch = source_branches[0]
    elif source_branches:
        source_branch = "확인 필요"
    else:
        source_branch = "확인 필요"

    print(f"대상 묶음: {bundle['name']}")
    print(f"원본 브랜치: {source_branch}")
    print(f"기준 폴더: {bundle['base']}")
    print()
    for item in sorted(bundle["items"], key=lambda row: row["repo"]):
        print(
            f"{item['repo']}: 폴더 {item['folder']} / "
            f"현재 브랜치 {item['branch']} / 변경상태 {item['dirty']}"
        )


def print_status(bundles: list[dict[str, Any]]) -> None:
    for index, bundle in enumerate(bundles, start=1):
        if index > 1:
            print()
        print_status_block(bundle)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Git worktree bundle inspector.")
    parser.add_argument("--root", required=True, help="Workspace root to inspect.")
    parser.add_argument("--max-depth", type=int, default=4, help="Directory scan depth.")
    parser.add_argument("--bundle", help="Show only this bundle name or absolute bundle path.")
    parser.add_argument("--all-worktrees", action="store_true", help="Explicit workspace-wide diagnostics, even with --bundle.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of status text.")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        parser.error(f"--root is not a directory: {root}")
    if args.bundle and not args.all_worktrees:
        try:
            data = inspect_bundle(root, args.bundle, args.max_depth)
        except (ValueError, RuntimeError, OSError) as exc:
            parser.error(str(exc))
        bundles = data["bundles"]
    else:
        data = inspect(root, args.max_depth)
        bundles = select_bundles(build_bundles(data), args.bundle)
    if args.bundle and not bundles:
        parser.error(f"--bundle did not match a discovered bundle: {args.bundle}")
    if args.json:
        data["bundles"] = bundles
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print_status(bundles)
        for error in data.get("errors", []):
            print(f"확인 필요: {error}")
        for conflict in data.get("conflicts", []):
            print(f"브랜치 점유 충돌: {conflict['repository']} / {conflict['branch']} / {conflict['path']}")
    return int(bool(data.get("errors") or data.get("conflicts")))


if __name__ == "__main__":
    raise SystemExit(main())
