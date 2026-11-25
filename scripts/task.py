#!/usr/bin/env python3
"""task — 작업 단위 멀티레포 worktree 관리 (new / list / status / pr / done)

프로젝트 루트(.task.toml 이 있는 폴더)에 레포들이 나란히 있고, 작업마다
<worktrees>/<작업>/<레포> worktree 를 만든다. 규칙은 .task.toml 에서 읽는다.

- 브랜치는 모든 레포에서 같은 이름(<branch_prefix><작업>)이고, 레포별 기준 브랜치([bases], 없으면 default_base)에서
  upstream 없이 만든다.
- PR 은 사람이 GitHub 에서 Rebase and merge 하는 것을 전제로 한다.
  · 첫 PR: 작업 브랜치 커밋을 하나로 합치고(squash) 일반 push.
  · PR 이 이미 있으면: 새 커밋을 그대로 일반 push(합치지 않음).
  · 충돌 때문에 직접 rebase 한 경우에만 --rebased 로 --force-with-lease push.
- PR 본문은 <worktrees>/<작업>/.pr/<레포>.md 에 쓴다(형식은 pr 스킬). 파일별 변경 표의 파일 목록은 git diff 에서 뽑고,
  빠진 파일·TODO 가 남으면 어떤 레포도 올리지 않는다. 커밋 목록과 관련 PR 링크는 스크립트가 붙인다.
- done 은 잃을 커밋이 없을 때만 지운다: 미커밋 변경이 없고, (원격에 없는 커밋이 없거나 || 머지된 PR 이 있거나).
- herdr 안에서 실행하면 작업 워크스페이스를 만들고(이름은 [herdr] label, cwd 는 프로젝트 루트) done 때 닫는다.
  [herdr] orchestrator 가 있으면 새 작업은 그 이름의 워크스페이스에서만 만든다(작업 탭 에이전트가 작업을 늘리지 않게).

.task.toml 예 (키를 빼면 기본값: worktrees="worktrees", branch_prefix="feature-", default_base="dev",
reserved=["main"], run_hint 없음, [bases] 비어 있음):
    worktrees = "worktrees"
    branch_prefix = "feature-"
    default_base = "dev"
    reserved = ["main", "tmp-*"]          # 작업 이름으로 못 쓰는 이름(* 패턴)
    run_hint = "make run TASK={task}"
    [bases]
    legacy-api = "release"
    [issue]                               # 있으면 new 때 이슈를 묻고 PR 본문 맨 위에 링크를 붙인다
    url = "https://example.youtrack.cloud/issue/{id}"
    pattern = "PROJ-[0-9]+"
    [herdr]                               # herdr 안에서 실행할 때만 쓴다
    orchestrator = "my-project"           # 새 작업을 만들 수 있는 워크스페이스 이름(없으면 어디서나)
    label = "└ {task}"                    # 작업 워크스페이스 이름(기본 "{task}")
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

CONFIG_NAME = ".task.toml"


def find_root(start: Path) -> Path:
    for d in [start, *start.parents]:
        if (d / CONFIG_NAME).is_file():
            return d
    raise SystemExit(f"ERROR: {start} 위쪽에 {CONFIG_NAME} 가 없습니다. 프로젝트 루트에 만드세요 (python3 task.py --help)")


class Config:
    def __init__(self, root: Path):
        data = tomllib.loads((root / CONFIG_NAME).read_text(encoding="utf-8"))
        self.root = root
        self.wt_dir = root / data.get("worktrees", "worktrees")
        self.branch_prefix = data.get("branch_prefix", "feature-")
        self.default_base = data.get("default_base", "dev")
        self.bases = dict(data.get("bases", {}))
        self.reserved = list(data.get("reserved", ["main"]))
        self.run_hint = data.get("run_hint", "")
        issue = data.get("issue", {})
        self.issue_url = issue.get("url", "")          # 예: https://example.youtrack.cloud/issue/{id}
        self.issue_pattern = issue.get("pattern", "")  # 예: PROJ-[0-9]+
        herdr = data.get("herdr", {})
        self.herdr_orchestrator = herdr.get("orchestrator", "")  # 새 작업을 만들 수 있는 워크스페이스 이름
        self.herdr_label = herdr.get("label", "{task}")          # 작업 워크스페이스 이름


CFG: Config  # main() 에서 채운다

# PR 본문에서 사람이(AI 가) 채우는 칸. 커밋·관련 PR 은 스크립트가 붙인다.
REQUIRED_SECTIONS = ["요청", "요청 밖 변경", "주의해서 볼 곳", "데이터 흐름", "파일별 변경", "확인"]
FILES_SECTION = "파일별 변경"
PLACEHOLDER = "TODO"


class Fail(Exception):
    pass


def info(msg: str = "") -> None:
    print(msg, flush=True)


def run(args: list[str], cwd: Path | None = None, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(args, cwd=cwd, text=True, capture_output=True, input=input)
    if check and proc.returncode != 0:
        raise Fail(f"{' '.join(args)}\n{(proc.stderr or proc.stdout).strip()}")
    return proc


def git(repo: Path, *args: str, check: bool = True) -> str:
    return run(["git", "-C", str(repo), *args], check=check).stdout.strip()


def git_ok(repo: Path, *args: str) -> bool:
    return run(["git", "-C", str(repo), *args], check=False).returncode == 0


def gh_json(repo: Path, *args: str):
    out = run(["gh", *args], cwd=repo).stdout.strip()
    return json.loads(out) if out else None


# ---------- 공통 ----------

def check_task_name(task: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", task):
        raise Fail(f"작업 이름은 소문자·숫자·- 만 씁니다: '{task}'")
    for pat in CFG.reserved:
        if fnmatch.fnmatchcase(task, pat):
            raise Fail(f"'{task}' 는 {CONFIG_NAME} 의 reserved({pat}) 라 작업 이름으로 못 씁니다")


def task_dir(task: str, must_exist: bool = True) -> Path:
    check_task_name(task)
    d = CFG.wt_dir / task
    if must_exist and not d.is_dir():
        raise Fail(f"작업 폴더가 없습니다: {d}")
    return d


def task_repos(tdir: Path) -> list[Path]:
    return sorted(p for p in tdir.iterdir() if p.is_dir() and (p / ".git").exists())


def base_of(repo_name: str) -> str:
    return CFG.bases.get(repo_name, CFG.default_base)


def remote_sha(repo: Path, branch: str) -> str:
    line = git(repo, "ls-remote", "origin", f"refs/heads/{branch}")
    return line.split()[0] if line else ""


def fetch_base(repo: Path, base: str) -> str:
    """origin/<base> 를 받아오고 GitHub 와 같은지 확인한다(fetch 가 조용히 안 갱신된 적이 있음)."""
    run(["git", "-C", str(repo), "fetch", "--quiet", "origin", base])
    remote = remote_sha(repo, base)
    local = git(repo, "rev-parse", f"refs/remotes/origin/{base}", check=False)
    if not remote or remote != local:
        raise Fail(f"{repo.name}: origin/{base} 가 GitHub 와 다릅니다(로컬 {local[:9]}, 원격 {remote[:9]})")
    return local


def in_herdr() -> bool:
    return os.environ.get("HERDR_ENV") == "1" and shutil.which("herdr") is not None


def herdr_workspaces() -> list[dict]:
    if not in_herdr():
        return []
    proc = run(["herdr", "workspace", "list"], check=False)
    try:
        return list(json.loads(proc.stdout)["result"]["workspaces"])
    except (ValueError, KeyError, TypeError):
        return []


def herdr_task_label(task: str) -> str:
    return CFG.herdr_label.format(task=task)


def herdr_task_ws(task: str) -> str:
    """작업 워크스페이스 id. label 설정 전에 만든 워크스페이스(이름 = 작업 이름)도 찾는다."""
    by_label = {w.get("label"): w["workspace_id"] for w in herdr_workspaces() if "workspace_id" in w}
    return by_label.get(herdr_task_label(task)) or by_label.get(task, "")


def check_orchestrator(task: str) -> None:
    """[herdr] orchestrator 가 있으면 새 작업은 그 워크스페이스에서만 만든다. 있는 작업에 레포를 더하는 건 된다."""
    if not (CFG.herdr_orchestrator and in_herdr()):
        return
    here = os.environ.get("HERDR_WORKSPACE_ID", "")
    label = next((w.get("label", "") for w in herdr_workspaces() if w.get("workspace_id") == here), "")
    if label != CFG.herdr_orchestrator:
        raise Fail(f"새 작업('{task}')은 오케스트레이터 워크스페이스('{CFG.herdr_orchestrator}')에서만 만듭니다"
                   f"(지금: '{label or here or '?'}'). 사용자에게 오케스트레이터 탭에서 새 작업으로 요청해 달라고 하세요. "
                   f"지금 작업에 레포를 더하려면 지금 작업 이름을 쓰세요")


# ---------- new / list ----------

META_NAME = ".task.json"


def load_meta(tdir: Path) -> dict | None:
    f = tdir / META_NAME
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def save_meta(tdir: Path, meta: dict) -> None:
    (tdir / META_NAME).write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_issues(a: argparse.Namespace, meta: dict | None) -> dict:
    """작업에 연결할 이슈를 정한다. 처음 만드는 작업이면 --issue 나 --no-issue 중 하나가 있어야 한다
    (터미널에서 사람이 실행하면 물어본다). [issue] 설정이 없는 프로젝트는 묻지 않는다."""
    meta = meta or {"issues": [], "issue_skipped": False}
    new = [i.strip() for i in (a.issue or []) if i.strip()]
    if not new and not a.no_issue and not (meta["issues"] or meta["issue_skipped"]) and CFG.issue_url:
        if sys.stdin.isatty():
            new = input("이 작업의 이슈 ID (여러 개는 공백으로, 없으면 엔터로 건너뜀): ").split()
        else:
            raise Fail("이 작업에 연결할 이슈를 사용자에게 물어 --issue <ID> 로 주세요. 사용자가 건너뛰자고 하면 --no-issue")
        if not new:
            a.no_issue = True
    for i in new:
        if CFG.issue_pattern and not re.fullmatch(CFG.issue_pattern, i):
            raise Fail(f"이슈 ID 형식이 아닙니다: '{i}' ({CONFIG_NAME} [issue] pattern = {CFG.issue_pattern})")
        if i not in meta["issues"]:
            meta["issues"].append(i)
    meta["issue_skipped"] = not meta["issues"] and (a.no_issue or meta["issue_skipped"])
    return meta


def with_issue_ids(subject: str, ids: list[str]) -> str:
    """제목(커밋 첫 줄·PR 제목)에 이슈 ID 가 하나도 없으면 끝에 ' (ID, ID)' 를 붙인다."""
    if not ids or any(i in subject for i in ids):
        return subject
    return f"{subject} ({', '.join(ids)})"


def tag_head_commit() -> int:
    """git rebase --exec 에서 커밋마다 불린다: 첫 줄에 이슈 ID 가 없으면 붙여 고쳐 쓴다."""
    ids = [i for i in os.environ.get("TASK_ISSUE_IDS", "").split(",") if i]
    message = subprocess.run(["git", "log", "-1", "--format=%B"], text=True, capture_output=True, check=True).stdout
    subject, _, rest = message.partition("\n")
    tagged = with_issue_ids(subject, ids)
    if tagged != subject:
        subprocess.run(["git", "commit", "--quiet", "--amend", "-F", "-"], input=tagged + "\n" + rest, text=True, check=True)
    return 0


def tag_unpushed_commits(c: dict, ids: list[str]) -> int:
    """아직 push 하지 않은 커밋 가운데 이슈 ID 가 없는 커밋의 첫 줄에 ID 를 붙인다(원격 기록은 바꾸지 않음)."""
    r = c["repo"]
    since = c["remote"] if c["remote"] and git_ok(r, "merge-base", "--is-ancestor", c["remote"], "HEAD") else \
        git(r, "merge-base", "HEAD", f"origin/{c['base']}")
    subjects = git(r, "log", "--format=%s", f"{since}..HEAD").splitlines()
    missing = [x for x in subjects if not any(i in x for i in ids)]
    if not ids or not missing:
        return 0
    env = {**os.environ, "TASK_ISSUE_IDS": ",".join(ids)}
    exec_cmd = f"{sys.executable} {Path(__file__).resolve()} _tag-head"
    proc = subprocess.run(["git", "-C", str(r), "rebase", "--quiet", "--exec", exec_cmd, since], env=env, text=True, capture_output=True)
    if proc.returncode != 0:
        run(["git", "-C", str(r), "rebase", "--abort"], check=False)
        raise Fail(f"{c['name']}: 커밋 메시지에 이슈 ID 를 붙이지 못했습니다\n{(proc.stderr or proc.stdout).strip()}")
    return len(missing)


def issue_lines(meta: dict | None) -> list[str]:
    if not meta or not (meta["issues"] or meta["issue_skipped"] or CFG.issue_url):
        return []
    if not meta["issues"]:
        return ["- 없음 (작업을 만들 때 건너뜀)"]
    return [f"- [{i}]({CFG.issue_url.format(id=i)})" if CFG.issue_url else f"- {i}" for i in meta["issues"]]


def cmd_new(a: argparse.Namespace) -> None:
    tdir = task_dir(a.task, must_exist=False)
    if not tdir.is_dir():
        check_orchestrator(a.task)
    repos = a.repos
    for r in repos:
        if "/" in r or not (CFG.root / r / ".git").exists():
            raise Fail(f"레포가 아닙니다: '{r}' ({CFG.root} 아래 레포 폴더 이름을 쓰세요)")
    branch = a.branch or f"{CFG.branch_prefix}{a.task}"
    meta = resolve_issues(a, load_meta(tdir) if tdir.is_dir() else None)
    tdir.mkdir(parents=True, exist_ok=True)
    save_meta(tdir, meta)
    lines = issue_lines(meta)
    if lines:
        info("· 이슈: " + ", ".join(meta["issues"]) if meta["issues"] else "· 이슈: 없음(건너뜀)")

    for r in repos:
        path, main = tdir / r, CFG.root / r
        if path.exists():
            info(f"· {r}: 이미 있음 — 건너뜀")
            continue
        base = a.base or base_of(r)
        sha = fetch_base(main, base)
        if git_ok(main, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"):
            raise Fail(f"{r}: 로컬 브랜치 {branch} 가 이미 있습니다. 다른 작업 이름이나 --branch 를 쓰세요")
        if remote_sha(main, branch):
            raise Fail(f"{r}: 원격에 {branch} 가 이미 있습니다(지난 작업일 수 있음). 다른 작업 이름이나 --branch 를 쓰세요")
        git(main, "worktree", "add", "--quiet", "--no-track", "-b", branch, str(path), f"origin/{base}")
        info(f"· {r}: {branch} ← origin/{base} ({sha[:9]})")

    if in_herdr():
        label = herdr_task_label(a.task)
        if herdr_task_ws(a.task):
            info(f"· herdr: 워크스페이스 '{label}' 이미 있음")
        else:
            run(["herdr", "workspace", "create", "--cwd", str(CFG.root), "--label", label, "--no-focus"])
            info(f"· herdr: 워크스페이스 '{label}' 만듦 (cwd={CFG.root})")
    if CFG.run_hint:
        info()
        info("실행: " + CFG.run_hint.format(task=a.task))


def cmd_list(a: argparse.Namespace) -> None:
    found = False
    dirs = sorted(p for p in CFG.wt_dir.iterdir() if p.is_dir()) if CFG.wt_dir.is_dir() else []
    for d in (p for p in dirs if not any(fnmatch.fnmatchcase(p.name, pat) for pat in CFG.reserved)):
        found = True
        meta = load_meta(d)
        issues = ", ".join(meta["issues"]) if meta and meta["issues"] else ""
        info(d.name + (f"  [{issues}]" if issues else ""))
        for r in task_repos(d):
            branch = git(r, "rev-parse", "--abbrev-ref", "HEAD", check=False) or "?"
            dirty = len(git(r, "status", "--porcelain").splitlines())
            info(f"  {r.name:<28} {branch:<40} {'변경 ' + str(dirty) if dirty else ''}")
    if not found:
        info("작업 폴더가 없습니다")


# ---------- PR 본문 ----------

def parse_sections(text: str) -> tuple[str, list[tuple[str, list[str]]]]:
    """'# 제목' 한 줄과 '## 이름' 섹션들로 나눈다."""
    title, sections, cur = "", [], None
    for line in text.splitlines():
        if line.startswith("# ") and not title and cur is None:
            title = line[2:].strip()
        elif line.startswith("## "):
            cur = (line[3:].strip(), [])
            sections.append(cur)
        elif cur is not None:
            cur[1].append(line)
    return title, sections


def render_sections(title: str, sections: list[tuple[str, list[str]]]) -> str:
    out = [f"# {title}", ""]
    for name, lines in sections:
        body = "\n".join(lines).strip("\n")
        out += [f"## {name}", body, ""]
    return "\n".join(out).rstrip() + "\n"


ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|[^|]*\|(.*)\|\s*$")


def diff_files(repo: Path, mb: str) -> list[tuple[str, str]]:
    """기준 분기점부터 HEAD 까지 바뀐 파일과 +/−. 이름 바꿈은 지운 파일·새 파일 둘로 본다."""
    rows = []
    for line in git(repo, "diff", "--numstat", "--no-renames", f"{mb}..HEAD").splitlines():
        add, delete, path = line.split("\t", 2)
        stat = "binary" if add == "-" else f"+{add} −{delete}"
        rows.append((path, stat))
    return rows


def files_table(files: list[tuple[str, str]], known: dict[str, str]) -> list[str]:
    lines = ["| 파일 | +/- | 무엇을 · 왜 |", "|---|---|---|"]
    for path, stat in files:
        lines.append(f"| `{path}` | {stat} | {known.get(path, PLACEHOLDER)} |")
    return lines


def skeleton(files: list[tuple[str, str]]) -> str:
    sections = [
        ("요청", [f"- {PLACEHOLDER} 사람이 시킨 것 1~3줄"]),
        ("요청 밖 변경", [f"- {PLACEHOLDER} 시키지 않았는데 바뀐 것. 없으면 \"없음\""]),
        ("주의해서 볼 곳", [f"- {PLACEHOLDER} DB 스키마·API 형식·설정·권한·외부 호출·삭제된 코드. 없으면 \"없음\""]),
        ("데이터 흐름", [f"{PLACEHOLDER} 호출 경로·데이터·저장 위치가 바뀌면 mermaid 시퀀스 + 구간별 기존/변경 표. 아니면 \"변경 없음\""]),
        (FILES_SECTION, files_table(files, {})),
        ("확인", [f"- 함: {PLACEHOLDER}", f"- 못 함: {PLACEHOLDER}"]),
    ]
    return render_sections(f"{PLACEHOLDER} PR 제목", sections)


def refresh_files(text: str, files: list[tuple[str, str]]) -> str:
    """파일별 변경 표를 지금 diff 로 다시 만들되, 이미 쓴 설명은 살린다."""
    title, sections = parse_sections(text)
    known = {}
    for name, lines in sections:
        if name == FILES_SECTION:
            for line in lines:
                m = ROW_RE.match(line)
                if m:
                    known[m.group(1)] = m.group(2).strip()
    new_sections = [(n, files_table(files, known) if n == FILES_SECTION else l) for n, l in sections]
    if not any(n == FILES_SECTION for n, _ in sections):
        new_sections.append((FILES_SECTION, files_table(files, known)))
    return render_sections(title, new_sections)


def validate_body(text: str, files: list[tuple[str, str]]) -> list[str]:
    errors = []
    title, sections = parse_sections(text)
    names = [n for n, _ in sections]
    if not title or PLACEHOLDER in title:
        errors.append("'# 제목' 줄이 없거나 TODO 가 남음")
    for need in REQUIRED_SECTIONS:
        if need not in names:
            errors.append(f"'## {need}' 칸이 없음")
        elif not "\n".join(dict(sections)[need]).strip():
            errors.append(f"'## {need}' 칸이 비어 있음")
    if PLACEHOLDER in text:
        errors.append("TODO 가 남아 있음")
    listed = set()
    for line in dict(sections).get(FILES_SECTION, []):
        m = ROW_RE.match(line)
        if m:
            listed.add(m.group(1))
    changed = {p for p, _ in files}
    for p in sorted(changed - listed):
        errors.append(f"파일별 변경 표에 없는 파일: {p}")
    for p in sorted(listed - changed):
        errors.append(f"표에는 있는데 바뀌지 않은 파일: {p} (--prepare 로 표를 다시 만드세요)")
    return errors


def with_generated(text: str, commits: list[str], related: list[str], issues: list[str]) -> str:
    title, sections = parse_sections(text)
    sections = [(n, l) for n, l in sections if n not in ("이슈", "커밋", "관련 PR")]
    if issues:
        sections.insert(0, ("이슈", issues))
    idx = next((i for i, (n, _) in enumerate(sections) if n == FILES_SECTION), len(sections))
    sections.insert(idx, ("커밋", [f"- {c}" for c in commits]))
    if related:
        sections.append(("관련 PR", [f"- {r}" for r in related]))
    return render_sections(title, sections)


# ---------- status / pr ----------

def repo_ctx(r: Path, tdir: Path, fetch: bool = True) -> dict:
    base = base_of(r.name)
    if fetch:
        fetch_base(r, base)
    branch = git(r, "rev-parse", "--abbrev-ref", "HEAD")
    mb = git(r, "merge-base", "HEAD", f"origin/{base}")
    behind, ahead = git(r, "rev-list", "--left-right", "--count", f"origin/{base}...HEAD").split()
    prs = gh_json(r, "pr", "list", "--head", branch, "--state", "all", "--limit", "10",
                  "--json", "number,state,url,baseRefName,mergeable") or []
    open_pr = next((p for p in prs if p["state"] == "OPEN"), None)
    return {
        "repo": r, "name": r.name, "base": base, "branch": branch, "mb": mb,
        "ahead": int(ahead), "behind": int(behind),
        "dirty": git(r, "status", "--porcelain").splitlines(),
        "remote": remote_sha(r, branch), "prs": prs, "open_pr": open_pr,
        "body": tdir / ".pr" / f"{r.name}.md",
    }


def body_state(ctx: dict, files: list[tuple[str, str]]) -> str:
    if not ctx["body"].exists():
        return "본문 없음"
    errs = validate_body(ctx["body"].read_text(encoding="utf-8"), files)
    return "본문 준비됨" if not errs else f"본문 미완성 {len(errs)}건"


def cmd_status(a: argparse.Namespace) -> None:
    tdir = task_dir(a.task)
    lines = issue_lines(load_meta(tdir))
    if lines:
        info("이슈: " + " ".join(l[2:] for l in lines))
    for r in task_repos(tdir):
        c = repo_ctx(r, tdir)
        pr = c["open_pr"] or (c["prs"][0] if c["prs"] else None)
        pr_txt = f"PR #{pr['number']} {pr['state']}" + (f" {pr['mergeable']}" if pr["state"] == "OPEN" else "") if pr else "PR 없음"
        files = diff_files(r, c["mb"])
        info(f"{c['name']:<26} {c['branch']} (← {c['base']}) 앞섬 {c['ahead']} · 뒤처짐 {c['behind']}"
             f" / 미커밋 {len(c['dirty'])} / {pr_txt} / {body_state(c, files)}")


def push(c: dict, rebased: bool) -> str:
    r, branch, remote = c["repo"], c["branch"], c["remote"]
    head = git(r, "rev-parse", "HEAD")
    if remote == head:
        return "push 할 것 없음"
    if not remote or git_ok(r, "merge-base", "--is-ancestor", remote, "HEAD"):
        git(r, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
        return "push"
    if not rebased:
        raise Fail(f"{c['name']}: 로컬 브랜치가 원격 {branch} 와 갈라졌습니다. 충돌 때문에 rebase 했다면 --rebased 를 붙이세요")
    git(r, "push", "--quiet", f"--force-with-lease=refs/heads/{branch}:{remote}", "origin", f"HEAD:refs/heads/{branch}")
    return "push(--force-with-lease, rebase 후)"


def squash(c: dict, title: str, body_text: str) -> None:
    """첫 PR 전에 작업 브랜치 커밋을 하나로 합친다. 커밋들의 Co-Authored-By 는 모아 남긴다."""
    r, mb = c["repo"], c["mb"]
    if c["ahead"] <= 1:
        return
    trailers = []
    for line in git(r, "log", "--format=%(trailers:key=Co-Authored-By)", f"{mb}..HEAD").splitlines():
        if line.strip() and line.strip() not in trailers:
            trailers.append(line.strip())
    _, sections = parse_sections(body_text)
    request = "\n".join(dict(sections).get("요청", [])).strip()
    message = title + ("\n\n" + request if request else "") + ("\n\n" + "\n".join(trailers) if trailers else "")
    git(r, "reset", "--soft", mb)
    run(["git", "-C", str(r), "commit", "--quiet", "-F", "-"], input=message)


def cmd_pr(a: argparse.Namespace) -> None:
    tdir = task_dir(a.task)
    (tdir / ".pr").mkdir(exist_ok=True)
    ctxs = []
    for r in task_repos(tdir):
        if a.repo and r.name not in a.repo:
            continue
        c = repo_ctx(r, tdir)
        if c["ahead"] == 0 and not c["open_pr"]:
            info(f"· {c['name']}: 기준 브랜치와 차이 없음 — 건너뜀")
            continue
        if c["dirty"]:
            raise Fail(f"{c['name']}: 커밋 안 된 변경이 있습니다. 작업에 속한 것만 커밋한 뒤 다시 실행하세요\n  " + "\n  ".join(c["dirty"]))
        if c["open_pr"] and c["open_pr"]["baseRefName"] != c["base"]:
            raise Fail(f"{c['name']}: 열린 PR #{c['open_pr']['number']} 의 기준이 {c['open_pr']['baseRefName']} 인데 설정은 {c['base']} 입니다")
        ctxs.append(c)
    if not ctxs:
        info("PR 로 올릴 레포가 없습니다")
        return

    if a.prepare:
        for c in ctxs:
            files = diff_files(c["repo"], c["mb"])
            body = c["body"]
            body.write_text(refresh_files(body.read_text(encoding="utf-8"), files) if body.exists() else skeleton(files), encoding="utf-8")
            mode = f"PR #{c['open_pr']['number']} 갱신(새 커밋 그대로 push)" if c["open_pr"] else "새 PR(커밋을 하나로 합침)"
            info(f"== {c['name']}  {c['branch']} → {c['base']}  {mode}")
            for line in git(c["repo"], "log", "--format=%h %s", f"{c['mb']}..HEAD").splitlines():
                info(f"  {line}")
            info(f"  파일 {len(files)}개, 본문: {body}")
            for e in validate_body(body.read_text(encoding="utf-8"), files):
                info(f"  - {e}")
        info()
        info("본문의 TODO 를 모두 채운 뒤: ./task.sh pr " + a.task)
        return

    # 1단계: 모든 레포 본문을 검사하고, 하나라도 문제면 아무것도 올리지 않는다
    problems = []
    for c in ctxs:
        if not c["body"].exists():
            problems.append(f"{c['name']}: 본문이 없습니다 (./task.sh pr {a.task} --prepare)")
            continue
        for e in validate_body(c["body"].read_text(encoding="utf-8"), diff_files(c["repo"], c["mb"])):
            problems.append(f"{c['name']}: {e}")
    if problems:
        raise Fail("PR 본문을 고쳐야 합니다:\n  " + "\n  ".join(problems))

    # 2단계: 커밋 정리 → push → PR 생성/갱신
    meta = load_meta(tdir)
    issues = issue_lines(meta)
    ids = meta["issues"] if meta else []
    for c in ctxs:
        text = c["body"].read_text(encoding="utf-8")
        title, _ = parse_sections(text)
        title = with_issue_ids(title, ids)
        r = c["repo"]
        if not c["open_pr"] and not c["remote"]:
            squash(c, title, text)
        tagged = tag_unpushed_commits(c, ids)
        if tagged:
            info(f"· {c['name']}: 커밋 {tagged}개 첫 줄에 이슈 ID 를 붙임")
        how = push(c, a.rebased)
        mb = git(r, "merge-base", "HEAD", f"origin/{c['base']}")
        commits = git(r, "log", "--reverse", "--format=%s", f"{mb}..HEAD").splitlines()
        c["final"] = with_generated(text, commits, [], issues)
        body_only = c["final"].split("\n", 1)[1].lstrip("\n")
        if c["open_pr"]:
            run(["gh", "pr", "edit", str(c["open_pr"]["number"]), "--title", title, "--body-file", "-"], cwd=r, input=body_only)
            c["url"] = c["open_pr"]["url"]
            info(f"· {c['name']}: {how}, PR 갱신 {c['url']}")
        else:
            out = run(["gh", "pr", "create", "--base", c["base"], "--head", c["branch"], "--title", title, "--body-file", "-"],
                      cwd=r, input=body_only).stdout.strip()
            c["url"] = out.splitlines()[-1] if out else ""
            info(f"· {c['name']}: {how}, PR 생성 {c['url']}")

    # 3단계: 같은 작업의 다른 레포 PR 을 서로 링크한다
    all_prs = {}
    for r in task_repos(tdir):
        for c in ctxs:
            if c["repo"] == r:
                all_prs[r.name] = c["url"]
                break
        else:
            branch = git(r, "rev-parse", "--abbrev-ref", "HEAD")
            prs = gh_json(r, "pr", "list", "--head", branch, "--state", "open", "--json", "url") or []
            if prs:
                all_prs[r.name] = prs[0]["url"]
    if len(all_prs) > 1:
        for c in ctxs:
            related = [f"{name}: {url}" for name, url in all_prs.items() if name != c["name"]]
            title, _ = parse_sections(c["final"])
            mb = git(c["repo"], "merge-base", "HEAD", f"origin/{c['base']}")
            commits = git(c["repo"], "log", "--reverse", "--format=%s", f"{mb}..HEAD").splitlines()
            final = with_generated(c["body"].read_text(encoding="utf-8"), commits, related, issues)
            run(["gh", "pr", "edit", c["url"], "--body-file", "-"], cwd=c["repo"], input=final.split("\n", 1)[1].lstrip("\n"))
        info(f"· 관련 PR 링크를 {len(ctxs)}개 PR 에 넣음")


# ---------- done ----------

def cmd_done(a: argparse.Namespace) -> None:
    tdir = task_dir(a.task)
    os.chdir(CFG.root)
    procs = [l for l in run(["pgrep", "-af", "--", f"{tdir}/"], check=False).stdout.splitlines()
             if "task.py" not in l and "task.sh" not in l and str(os.getpid()) != l.split()[0]]
    if procs:
        raise Fail("이 작업 폴더에서 도는 프로세스가 있습니다. 먼저 끄세요:\n" + "\n".join(procs))

    plan = []
    for r in task_repos(tdir):
        branch = git(r, "rev-parse", "--abbrev-ref", "HEAD")
        if git(r, "status", "--porcelain"):
            raise Fail(f"{r.name}: 커밋 안 된 변경이 있습니다")
        local_only = int(git(r, "rev-list", "--count", "HEAD", "--not", "--remotes"))
        if local_only:
            merged = gh_json(r, "pr", "list", "--head", branch, "--state", "merged", "--json", "number") or []
            if not merged and not a.force:
                raise Fail(f"{r.name}: 원격에 없는 커밋 {local_only}개, 머지된 PR 없음 ({branch}). push 하거나 --force")
        plan.append((r, branch))

    for r, branch in plan:
        main = CFG.root / r.name
        # 서브모듈이 있는 worktree 는 git 이 --force 없이 지우지 않는다. 미커밋 변경이 없는 건 위에서 확인했다.
        force = ["--force"] if (r / ".gitmodules").exists() else []
        git(main, "worktree", "remove", *force, str(r))
        git(main, "branch", "-D", branch)
        info(f"· {r.name}: worktree·로컬 브랜치 {branch} 지움")
    shutil.rmtree(tdir)
    info(f"· 폴더 {tdir} 지움")

    ws = herdr_task_ws(a.task)
    if ws:
        if ws == os.environ.get("HERDR_WORKSPACE_ID"):
            info(f"· herdr: 지금 이 워크스페이스({ws})라 닫지 않았습니다. 직접 닫으세요")
        else:
            run(["herdr", "workspace", "close", ws])
            info(f"· herdr: 작업 '{a.task}' 워크스페이스({ws}) 닫음")


# ---------- main ----------

USAGE = """\
(프로젝트 루트의 task.sh 또는 python3 <common-skills>/scripts/task.py 로 실행. 설정은 루트의 .task.toml)
./task.sh new    <작업> <레포...> [--issue <ID>...|--no-issue] [--base <브랜치>] [--branch <브랜치>]
./task.sh list
./task.sh status <작업>
./task.sh pr     <작업> --prepare [--repo <레포>...]   본문 틀 만들기/파일 표 갱신
./task.sh pr     <작업> [--repo <레포>...] [--rebased] 검사 → (첫 PR 이면 커밋 합침) → push → PR 생성/갱신
./task.sh done   <작업> [--force]

예:
  ./task.sh new login-device api web --issue PROJ-123
  ./task.sh pr login-device --prepare   # 본문 TODO 채우기
  ./task.sh pr login-device
  ./task.sh done login-device
"""


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="task.sh", usage=USAGE, add_help=True)
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("new"); s.add_argument("task"); s.add_argument("repos", nargs="+")
    s.add_argument("--issue", action="append"); s.add_argument("--no-issue", action="store_true")
    s.add_argument("--base"); s.add_argument("--branch"); s.set_defaults(fn=cmd_new)
    s = sub.add_parser("list"); s.set_defaults(fn=cmd_list)
    s = sub.add_parser("status"); s.add_argument("task"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("pr"); s.add_argument("task"); s.add_argument("--prepare", action="store_true")
    s.add_argument("--repo", action="append"); s.add_argument("--rebased", action="store_true"); s.set_defaults(fn=cmd_pr)
    s = sub.add_parser("done"); s.add_argument("task"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_done)
    if argv[:1] == ["_tag-head"]:  # tag_unpushed_commits 의 rebase --exec 전용
        return tag_head_commit()
    a = p.parse_args(argv)
    global CFG
    if not getattr(a, "fn", None):
        print(USAGE)
        return 0
    try:
        CFG = Config(find_root(Path.cwd()))
        a.fn(a)
    except Fail as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
