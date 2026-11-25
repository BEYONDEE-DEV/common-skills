---
name: task
description: 여러 레포에 걸친 작업을 작업 단위 worktree 폴더로 만들고(작업 시작, 작업 만들어, 레포 추가), 상태를 보고(작업 상태), 머지 뒤 정리할 때(작업 정리, 작업 끝) 사용. 프로젝트 루트의 .task.toml 규칙을 따른다. PR 올리기는 pr 스킬.
---

# 작업 폴더 관리

프로젝트 루트(`.task.toml` 이 있는 폴더)에 레포들이 나란히 있고, 작업마다 `<worktrees>/<작업>/<레포>` worktree 를 만든다. 루트의 레포 폴더(원본 체크아웃)는 직접 고치지 않는다.

## 실행

```bash
python3 "<skill-dir>/../../scripts/task.py" <명령> ...
```

- `<skill-dir>` 은 이 `SKILL.md` 가 있는 폴더다. 프로젝트 루트에 `task.sh` 가 있으면 `./task.sh <명령>` 도 같다.
- 프로젝트 안 어디서 실행해도 위쪽의 `.task.toml` 을 찾는다. 없으면 아래 형식으로 루트에 만든다.

## 명령

| 명령 | 하는 일 |
|---|---|
| `new <작업> <레포...> [--issue <ID>...\|--no-issue] [--base <브랜치>] [--branch <브랜치>]` | 이슈를 작업에 저장하고, 레포마다 `<branch_prefix><작업>` 브랜치를 기준 브랜치에서 upstream 없이 만든다. 이미 있는 작업이면 빠진 레포만 더한다. herdr 안이면 작업 워크스페이스(이름은 `[herdr] label`, cwd=루트)를 만든다 |
| `list` | 작업 폴더와 레포별 브랜치·미커밋 수 |
| `status <작업>` | 레포별 앞섬/뒤처짐, 미커밋, PR 상태·충돌 여부, PR 본문 준비 상태 |
| `done <작업> [--force]` | worktree·로컬 브랜치·폴더를 지우고 herdr 워크스페이스를 닫는다 |

- 작업 이름은 소문자·숫자·`-`. 사용자가 이름을 주지 않으면 요청 내용으로 짧게 지어 확인받는다.
- `.task.toml` 에 `[issue]` 가 있으면 **새 작업을 만들기 전에 사용자에게 연결할 이슈 ID 를 묻는다.** 받은 ID 는 `--issue`(여러 개면 반복), 사용자가 없다거나 건너뛰자고 하면 `--no-issue`. 둘 다 없으면 `new` 가 멈춘다. 이슈를 나중에 더하려면 같은 작업 이름으로 `new <작업> <레포> --issue <ID>`.
- 저장한 이슈는 pr 이 PR 본문 맨 위 링크, PR 제목, 커밋 첫 줄에 붙인다.
- `new` 는 기준 브랜치를 받아 와 GitHub 와 같은지 확인한 뒤 만든다. 같은 이름의 브랜치가 로컬이나 원격에 있으면 멈춘다(지난 작업일 수 있음) — 다른 이름을 쓴다.
- 작업 중 다른 레포가 필요해지면 같은 작업 이름으로 `new` 를 다시 부른다.
- `[herdr] orchestrator` 가 있으면 herdr 안에서 **새 작업은 그 이름의 워크스페이스에서만** 만든다. 작업 워크스페이스에서 다른 작업 이름으로 `new` 를 부르면 멈춘다 — 그때는 직접 만들지 말고, 사용자에게 오케스트레이터 탭에서 새 작업으로 요청해 달라고 알린다. 있는 작업에 레포를 더하는 건 어디서나 된다.
- `done` 은 이 폴더에서 도는 프로세스, 미커밋 변경, 원격에도 없고 머지된 PR 도 없는 커밋이 있으면 아무것도 지우지 않고 멈춘다. `--force` 는 마지막 경우만 넘기며, 사용자가 그 커밋을 버리라고 했을 때만 쓴다. 원격 브랜치는 지우지 않는다.

## .task.toml

프로젝트 루트에 둔다. 아래는 형식 예시이고, 키를 빼면 기본값을 쓴다.

```toml
worktrees = "worktrees"
branch_prefix = "feature-"
default_base = "dev"
reserved = ["main", "tmp-*"]
run_hint = "make run TASK={task}"

[bases]
legacy-api = "release"

[issue]
url = "https://example.youtrack.cloud/issue/{id}"
pattern = "PROJ-[0-9]+"

[herdr]
orchestrator = "my-project"
label = "└ {task}"
```

| 키 | 기본값 | 뜻 |
|---|---|---|
| `worktrees` | `"worktrees"` | 작업 폴더를 둘 곳(루트 기준) |
| `branch_prefix` | `"feature-"` | 브랜치 이름 = 접두어 + 작업 이름 |
| `default_base` | `"dev"` | 기준 브랜치(PR 대상) |
| `reserved` | `["main"]` | 작업 이름으로 못 쓰는 이름. `*` 패턴 가능 |
| `run_hint` | 없음 | `new` 뒤에 보여 줄 실행 안내. `{task}` 는 작업 이름 |
| `[bases]` | 비어 있음 | 레포 폴더 이름 = 그 레포만의 기준 브랜치 |
| `[issue] url` | 없음 | 이슈 링크 주소. `{id}` 는 이슈 ID. 있으면 `new` 때 이슈를 묻는다 |
| `[issue] pattern` | 없음 | 이슈 ID 형식(정규식). 맞지 않는 ID 는 거절 |
| `[herdr] orchestrator` | 없음 | herdr 안에서 새 작업을 만들 수 있는 워크스페이스 이름. 없으면 어디서나 |
| `[herdr] label` | `"{task}"` | 작업 워크스페이스 이름. `{task}` 는 작업 이름. `└ {task}` 처럼 쓰면 사이드바에서 오케스트레이터 아래처럼 보인다(herdr 는 git worktree 가 아닌 워크스페이스를 묶지 못함) |

필요: Python 3.11+, `git`, 인증된 `gh`.
