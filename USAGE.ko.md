# BEYONDEE Skills 사용법

설치는 [README.md](./README.md)를 참고하세요. 설치나 업데이트 후에는 Codex는 새 스레드에서, Claude Code는 재시작 후 사용하세요.

## Multi Repo

폴더 바로 아래에 있는 Git 저장소들을 한 번에 다룹니다. 저장소마다 독립적으로 처리하므로 한 저장소가 실패해도 나머지는 계속 진행됩니다.

명시적으로 호출할 때만 동작합니다.

| 호스트 | 호출 |
|---|---|
| Codex | `$common-skills:multi-repo <keyword> [요청]` |
| Claude Code | `/common-skills:multi-repo <keyword> [요청]` |

### 키워드

| 키워드 | 하는 일 |
|---|---|
| `status` | 저장소별 현재 브랜치, source 대비 앞섬/뒤처짐, 미푸시 커밋, PR 상태 |
| `sync` | 지금 브랜치에 최신 source를 받아옵니다(커밋·push·PR 없음). 커밋하지 않은 변경이 있는 저장소는 건너뛰고, PR이 머지되어 내용이 모두 source에 들어간 원격 작업 브랜치는 삭제합니다(열린 PR이 있거나 다른 브랜치의 source로 쓰이는 브랜치, `main`·`master`·`dev`·`dev2`·`prod`는 삭제하지 않음) |
| `submit` | 작업 커밋(한국어) → 최신 source 위로 rebase(충돌은 에이전트가 해결) → push → PR 생성 또는 갱신 |
| `promote` | 작업 브랜치를 `작업 브랜치 → <source> → dev → dev2 → prod` 순서로 fast-forward 승격(PR 없이 dev에 바로 올리기 포함). `--to dev`처럼 멈출 단계를 정할 수 있고, `dev`에 이미 있는 것만 올리려면 `dev부터`라고 말하면 됩니다. 계획을 먼저 보여 준 뒤 실행 |
| `help` | 사용법 안내 |

### 기본 흐름

```text
작업 브랜치에서 A 개발 → submit (PR A 생성)
  → 계속 개발 → submit (source 최신화 + PR A 갱신)
  → GitHub에서 PR A merge (squash 가능)
  → sync (최신 source 받아오기 + 머지된 원격 브랜치 정리)
  → 같은 브랜치에서 B 개발 → submit (source 위로 rebase 후 PR B 생성, B 변경만 포함)
```

- source 브랜치(rebase 기준이자 PR 대상)는 처음 한 번만 알려 주면 저장소별로 기억합니다(`git config branch.<브랜치>.multiRepoSource`). 열린 PR이 있으면 PR의 base 브랜치를 씁니다.
- `main`, `master`, `dev`, `dev2`, `prod`에서는 submit하지 않습니다. 작업 브랜치에서 사용하세요.
- 작업 브랜치는 merge 커밋 없이 source 끝 위에 일직선으로 쌓입니다. 이미 있던 merge 커밋은 rebase하면서 없어집니다. rebase한 브랜치는 `--force-with-lease`로 push하므로, 그사이 누가 push했으면 덮어쓰지 않고 거부됩니다.
- 동료 커밋이 있는 브랜치나 다른 브랜치의 source로 쓰이는 브랜치는 기록을 바꾸지 않도록 지금처럼 source를 merge합니다.
- PR merge는 항상 GitHub에서 직접 합니다. dev 기록까지 일직선으로 두려면 **Squash and merge** 또는 **Rebase and merge**를 고르세요. 에이전트가 직접 force push, reset, rebase, 브랜치 삭제를 하지는 않습니다(위 스크립트 동작만 예외).
- squash merge된 브랜치는 merge 커밋을 만들지 않고 source 끝으로 다시 시작하며, 머지된 PR의 원격 브랜치는 새 작업으로 교체합니다(해당 SHA일 때만). 동료가 원격 브랜치에 올린 커밋은 교체하지 않고 merge합니다.
- 커밋되지 않은 파일이 있으면 에이전트가 작업 관련 파일만 커밋하고, 애매하면 물어봅니다. ignore된 파일은 무시합니다.

### 예시

```text
/common-skills:multi-repo status
/common-skills:multi-repo sync
/common-skills:multi-repo submit 로그인 개선 작업 PR 올려줘
/common-skills:multi-repo submit source는 dev야
/common-skills:multi-repo promote api, web 저장소만 prod까지 올려줘
/common-skills:multi-repo promote 이번 작업 PR 없이 dev에 바로 올려줘
/common-skills:multi-repo promote dev에 있는 것만 dev2로 올려줘
```

promote할 때 작업이 아직 커밋·push되지 않았거나 최신 source가 합쳐져 있지 않으면, 에이전트가 먼저 submit한 뒤 승격합니다.

Codex에서는 앞의 `/`를 `$`로 바꿔 사용합니다.

### 준비물

- Python 3.9 이상, `git`
- GitHub CLI `gh` 로그인 (`gh auth status`로 확인)
