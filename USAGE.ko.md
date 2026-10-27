# BEYONDEE Skills 사용법

설치는 [README.md](./README.md)를 참고하세요. 설치나 업데이트 후에는 Codex는 새 스레드에서, Claude Code는 재시작 후 사용하세요.

## Multi Repo Worktree

여러 독립 Git 저장소를 하나의 영구 개발 묶음(slot)으로 관리합니다. 개발 워크트리는 유지하고, PR이 머지되면 로컬 작업 브랜치만 새 세대(`<branch-family>-mrw-g<N>`)로 옮겨 이어서 작업합니다.

명시적으로 호출할 때만 동작합니다.

| 호스트 | 호출 |
|---|---|
| Codex | `$common-skills:multi-repo-worktree <keyword> [요청]` |
| Claude Code | `/common-skills:multi-repo-worktree <keyword> [요청]` |

### 키워드

| 키워드 | 하는 일 | 쓰기 여부 |
|---|---|---|
| `help` | 흐름과 안전 규칙 설명 | 없음 |
| `status` | 저장소별 브랜치, 세대, PR, 변경상태 조회 | 읽기 전용 |
| `create` | 영구 slot과 첫 작업 워크트리를 한 번 생성 | 로컬 |
| `submit` | 작업 커밋(한국어) 후 ready PR 생성, 머지된 PR이 있으면 세대 회전 후 다음 PR 생성 | 로컬·원격 |
| `sync` | 원격 소스 브랜치 fetch, 머지된 저장소는 세대 회전 후 머지된 PR 브랜치 정리 | 로컬·원격 |
| `pull` | 원본(소스) 워크트리만 fast-forward | 로컬 |
| `promote` | 원격 소스 브랜치를 `dev → dev2 → prod` 순서로 fast-forward 승격 | 원격 |

### 기본 흐름

```text
create (최초 1회)
  → A 개발 → submit (PR A)
  → 같은 워크트리에서 B 계속 개발
  → GitHub에서 PR A squash merge
  → sync 또는 다음 submit: B만 새 세대 브랜치로 이동
  → submit (PR B) → 반복
```

- PR 머지는 항상 사용자가 GitHub에서 직접 합니다. 자동 머지는 하지 않습니다.
- force push, reset, 히스토리 재작성은 하지 않습니다.
- 저장소별로 독립 처리합니다. 한 저장소가 실패하거나 대기 중이어도 나머지 저장소의 성공은 기록됩니다.
- 회전 중 충돌이 나면 격리된 워크트리에서 양쪽 의도를 보존해 해결한 뒤 이어서 진행합니다.

### 예시

```text
/common-skills:multi-repo-worktree status
/common-skills:multi-repo-worktree submit 문서 정리 작업 PR 올려줘
/common-skills:multi-repo-worktree sync
/common-skills:multi-repo-worktree promote api, web 저장소만 prod까지 올려줘
```

Codex에서는 앞의 `/`를 `$`로 바꿔 사용합니다.

### 상태 파일

작업 공간 루트의 `.multi-repo-worktree/` 아래에 slot 매니페스트(`slots.json`), 작업 잠금, 세대·충돌 처리용 임시 워크트리, submit/sync/promote 실행 기록이 저장됩니다. 이 디렉터리를 직접 수정하지 마세요.
