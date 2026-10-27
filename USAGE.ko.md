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
| `submit` | 작업 커밋(한국어) → 최신 source merge(충돌은 에이전트가 해결) → push → PR 생성 또는 갱신 |
| `promote` | 원격 브랜치를 `<source> → dev → dev2 → prod` 순서로 fast-forward 승격. 계획을 먼저 보여 준 뒤 실행 |
| `help` | 사용법 안내 |

### 기본 흐름

```text
작업 브랜치에서 A 개발 → submit (PR A 생성)
  → 계속 개발 → submit (source 최신화 + PR A 갱신)
  → GitHub에서 PR A merge (squash 가능)
  → 같은 브랜치에서 B 개발 → submit (source merge 후 PR B 생성, B 변경만 포함)
```

- source 브랜치(merge해 올 브랜치이자 PR 대상)는 처음 한 번만 알려 주면 저장소별로 기억합니다(`git config branch.<브랜치>.multiRepoSource`). 열린 PR이 있으면 PR의 base 브랜치를 씁니다.
- `main`, `master`, `dev`, `dev2`, `prod`에서는 submit하지 않습니다. 작업 브랜치에서 사용하세요.
- PR merge는 항상 GitHub에서 직접 합니다. force push, reset, rebase, 브랜치 삭제는 하지 않습니다.
- 커밋되지 않은 파일이 있으면 에이전트가 작업 관련 파일만 커밋하고, 애매하면 물어봅니다. ignore된 파일은 무시합니다.

### 예시

```text
/common-skills:multi-repo status
/common-skills:multi-repo submit 로그인 개선 작업 PR 올려줘
/common-skills:multi-repo submit source는 dev야
/common-skills:multi-repo promote api, web 저장소만 prod까지 올려줘
```

Codex에서는 앞의 `/`를 `$`로 바꿔 사용합니다.

### 준비물

- Python 3.9 이상, `git`
- GitHub CLI `gh` 로그인 (`gh auth status`로 확인)
