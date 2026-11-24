from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "task.py"

# PR 을 파일(JSON)에 기록하는 가짜 gh. task.py 가 쓰는 pr list / create / edit 만 흉내 낸다.
FAKE_GH = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, subprocess, sys
    db = os.environ["FAKE_GH_DB"]
    data = json.load(open(db)) if os.path.exists(db) else {"prs": []}
    repo = os.path.basename(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip())
    args = sys.argv[1:]
    body = sys.stdin.read() if "--body-file" in args else None
    def opt(n): return args[args.index(n) + 1] if n in args else None
    if args[:2] == ["pr", "list"]:
        head, state = opt("--head"), (opt("--state") or "open").upper()
        rows = [p for p in data["prs"] if p["repo"] == repo and p["head"] == head and (state == "ALL" or p["state"] == state)]
        print(json.dumps([{"number": p["number"], "state": p["state"], "url": p["url"], "baseRefName": p["base"],
                           "mergeable": "MERGEABLE"} for p in rows]))
    elif args[:2] == ["pr", "create"]:
        n = len(data["prs"]) + 1
        url = f"https://github.com/fake/{repo}/pull/{n}"
        data["prs"].append({"repo": repo, "number": n, "head": opt("--head"), "base": opt("--base"),
                            "title": opt("--title"), "body": body, "state": "OPEN", "url": url})
        print(url)
    elif args[:2] == ["pr", "edit"]:
        for p in data["prs"]:
            if p["url"] == args[2] or (p["repo"] == repo and str(p["number"]) == args[2]):
                if body is not None: p["body"] = body
                if opt("--title"): p["title"] = opt("--title")
    json.dump(data, open(db, "w"), ensure_ascii=False)
''')

CONFIG = textwrap.dedent('''\
    worktrees = "worktrees"
    branch_prefix = "dev-feature-"
    default_base = "dev"
    reserved = ["main", "slot-*"]
    [bases]
    other = "dev-feature-food"
    [issue]
    url = "https://example.youtrack.cloud/issue/{id}"
    pattern = "PROJ-[0-9]+"
''')


def body(title: str, rows: list[tuple[str, str]]) -> str:
    table = "\n".join(f"| `{path}` | {stat} | 추가 — 요청 |" for path, stat in rows)
    return textwrap.dedent(f'''\
        # {title}

        ## 요청
        - {title}

        ## 요청 밖 변경
        - 없음

        ## 주의해서 볼 곳
        - 없음

        ## 데이터 흐름
        변경 없음

        ## 파일별 변경
        | 파일 | +/- | 무엇을 · 왜 |
        |---|---|---|
        ''') + table + "\n\n## 확인\n- 함: 테스트\n- 못 함: 없음\n"


class TaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "project"
        (self.tmp / "bin").mkdir()
        gh = self.tmp / "bin" / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        self.env = {**os.environ, "PATH": f"{self.tmp / 'bin'}:{os.environ['PATH']}",
                    "FAKE_GH_DB": str(self.tmp / "gh.json"), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        self.env.pop("HERDR_ENV", None)
        self.root.mkdir()
        (self.root / ".task.toml").write_text(CONFIG)
        for name in ("api", "other"):
            remote = self.tmp / "remotes" / f"{name}.git"
            self.sh("git", "init", "-q", "--bare", str(remote), cwd=self.tmp)
            self.sh("git", "clone", "-q", str(remote), name, cwd=self.root)
            repo = self.root / name
            (repo / "README.md").write_text("init\n")
            self.sh("git", "add", ".", cwd=repo)
            self.sh("git", "commit", "-qm", "init", cwd=repo)
            for branch in ("dev", "dev-feature-food"):
                self.sh("git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}", cwd=repo)

    def tearDown(self) -> None:
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def sh(self, *args: str, cwd: Path) -> str:
        return subprocess.run(args, cwd=cwd, env=self.env, check=True, text=True, capture_output=True).stdout.strip()

    def task(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd or self.root, env=self.env,
                              text=True, capture_output=True, stdin=subprocess.DEVNULL)

    def commit(self, repo: Path, name: str, message: str) -> None:
        (repo / name).write_text(name + "\n")
        self.sh("git", "add", name, cwd=repo)
        self.sh("git", "commit", "-qm", message, cwd=repo)

    def prs(self) -> list[dict]:
        return json.loads((self.tmp / "gh.json").read_text())["prs"]

    def test_new_uses_per_repo_base_and_no_upstream(self) -> None:
        out = self.task("new", "t1", "api", "other", "--no-issue")
        self.assertEqual(out.returncode, 0, out.stderr)
        api, other = self.root / "worktrees/t1/api", self.root / "worktrees/t1/other"
        self.assertEqual(self.sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=api), "dev-feature-t1")
        self.assertIn("origin/dev-feature-food", out.stdout)
        upstream = subprocess.run(["git", "rev-parse", "--abbrev-ref", "@{u}"], cwd=other, capture_output=True)
        self.assertNotEqual(upstream.returncode, 0)

    def test_reserved_names_and_missing_config(self) -> None:
        self.assertIn("reserved", self.task("new", "slot-9", "api", "--no-issue").stderr)
        (self.root / ".task.toml").unlink()
        self.assertIn(".task.toml", self.task("list").stderr)

    def test_issue_required_validated_and_skippable(self) -> None:
        out = self.task("new", "t1", "api")
        self.assertIn("--no-issue", out.stderr, "이슈를 안 주면 사용자에게 물으라고 멈춘다")
        self.assertFalse((self.root / "worktrees/t1").exists())
        self.assertIn("형식이 아닙니다", self.task("new", "t1", "api", "--issue", "XYZ-1").stderr)
        self.assertEqual(self.task("new", "t2", "api", "--no-issue").returncode, 0)
        self.assertEqual(self.task("new", "t2", "other").returncode, 0, "이미 정한 작업에 레포를 더할 때는 다시 묻지 않는다")
        self.assertEqual(self.task("new", "t3", "api", "--issue", "PROJ-1").returncode, 0)
        self.task("new", "t3", "api", "--issue", "PROJ-2")
        self.assertIn("t3  [PROJ-1, PROJ-2]", self.task("list").stdout)

    def test_pr_flow(self) -> None:
        self.task("new", "t1", "api", "other", "--issue", "PROJ-7")
        api, other = self.root / "worktrees/t1/api", self.root / "worktrees/t1/other"
        self.commit(api, "a.txt", "wip a")
        self.commit(api, "b.txt", "wip b")
        self.commit(other, "c.txt", "add c")

        self.assertIn("본문이 없습니다", self.task("pr", "t1").stderr)
        prep = self.task("pr", "t1", "--prepare", cwd=api)  # 작업 폴더 안에서도 루트를 찾는다
        self.assertEqual(prep.returncode, 0, prep.stderr)
        pr_dir = self.root / "worktrees/t1/.pr"
        self.assertIn("| `b.txt` | +1 −0 | TODO |", (pr_dir / "api.md").read_text())

        (pr_dir / "api.md").write_text(body("A 추가", [("a.txt", "+1 −0")]))
        (pr_dir / "other.md").write_text(body("C 추가", [("c.txt", "+1 −0")]))
        out = self.task("pr", "t1")
        self.assertIn("표에 없는 파일: b.txt", out.stderr)
        self.assertEqual(self.prs() if (self.tmp / "gh.json").exists() else [], [], "검사에 걸리면 아무것도 안 올린다")

        (pr_dir / "api.md").write_text(body("A 추가", [("a.txt", "+1 −0"), ("b.txt", "+1 −0")]))
        out = self.task("pr", "t1")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.sh("git", "log", "--format=%s", "origin/dev..HEAD", cwd=api), "A 추가 (PROJ-7)",
                         "첫 PR 은 커밋을 합치고 제목에 이슈 ID 를 붙인다")
        pr = next(p for p in self.prs() if p["repo"] == "api")
        self.assertEqual(pr["base"], "dev")
        self.assertEqual(pr["title"], "A 추가 (PROJ-7)")
        self.assertTrue(pr["body"].startswith("## 이슈\n- [PROJ-7](https://example.youtrack.cloud/issue/PROJ-7)"), pr["body"][:80])
        self.assertIn("## 커밋\n- A 추가", pr["body"])
        self.assertIn("other: https://github.com/fake/other/pull/", pr["body"])
        self.assertEqual(next(p for p in self.prs() if p["repo"] == "other")["base"], "dev-feature-food")

        # PR 뒤 수정: 합치지 않고 일반 push
        self.commit(api, "d.txt", "fix: d 보완")
        self.task("pr", "t1", "--prepare", "--repo", "api")
        text = (pr_dir / "api.md").read_text()
        self.assertIn("| `a.txt` | +1 −0 | 추가 — 요청 |", text, "이미 쓴 설명은 남는다")
        (pr_dir / "api.md").write_text(text.replace("| `d.txt` | +1 −0 | TODO |", "| `d.txt` | +1 −0 | d — 요청 |"))
        out = self.task("pr", "t1", "--repo", "api")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.sh("git", "log", "--format=%s", "origin/dev..HEAD", cwd=api), "fix: d 보완 (PROJ-7)\nA 추가 (PROJ-7)",
                         "PR 뒤 커밋도 push 전에 이슈 ID 를 붙인다")
        self.assertEqual(len(self.prs()), 2, "PR 을 새로 만들지 않는다")

        # 원격과 갈라지면 --rebased 없이는 멈춘다
        self.sh("git", "commit", "-q", "--amend", "-m", "fix: d 보완(수정)", cwd=api)
        self.assertIn("--rebased", self.task("pr", "t1", "--repo", "api").stderr)
        out = self.task("pr", "t1", "--repo", "api", "--rebased")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.sh("git", "ls-remote", "origin", "dev-feature-t1", cwd=api).split()[0],
                         self.sh("git", "rev-parse", "HEAD", cwd=api))

    def test_skipped_issue_shows_none(self) -> None:
        self.task("new", "t1", "api", "--no-issue")
        api = self.root / "worktrees/t1/api"
        self.commit(api, "a.txt", "add a")
        self.task("pr", "t1", "--prepare")
        (self.root / "worktrees/t1/.pr/api.md").write_text(body("A 추가", [("a.txt", "+1 −0")]))
        out = self.task("pr", "t1")
        self.assertEqual(out.returncode, 0, out.stderr)
        pr = self.prs()[0]
        self.assertEqual(pr["title"], "A 추가")
        self.assertTrue(pr["body"].startswith("## 이슈\n- 없음"), pr["body"][:60])

    def test_done_refuses_unpushed_then_cleans(self) -> None:
        self.task("new", "t1", "api", "--no-issue")
        api = self.root / "worktrees/t1/api"
        self.commit(api, "a.txt", "local only")
        self.assertIn("머지된 PR 없음", self.task("done", "t1").stderr)
        (api / "x.txt").write_text("x")
        self.assertIn("커밋 안 된 변경", self.task("done", "t1", "--force").stderr)
        (api / "x.txt").unlink()
        out = self.task("done", "t1", "--force")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse((self.root / "worktrees/t1").exists())
        self.assertEqual(self.sh("git", "branch", "--list", "dev-feature-t1", cwd=self.root / "api"), "")


if __name__ == "__main__":
    unittest.main()
