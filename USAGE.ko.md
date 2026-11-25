# BEYONDEE Skills 사용법

설치는 [README.md](./README.md)를 참고하세요. 설치나 업데이트 후에는 Codex는 새 스레드에서, Claude Code는 재시작 후 사용하세요.

## 준비

프로젝트 루트(레포들이 나란히 있는 폴더)에 `.task.toml` 을 둡니다. 형식은 [skills/task/SKILL.md](./skills/task/SKILL.md) 를 보세요.
사람이 직접 실행할 때는 `python3 <플러그인>/scripts/task.py <명령>` 입니다(프로젝트에 `task.sh` 래퍼를 둬도 됩니다).

## 흐름

```text
/common-skills:task  api, web 으로 login-device 작업 만들어줘 (이슈 PROJ-123)    # new --issue
/common-skills:task  login-device 상태                                         # status
/common-skills:pr    login-device PR 올려줘                                     # pr --prepare → 본문 → pr
(사람이 GitHub 에서 Rebase and merge)
/common-skills:task  login-device 정리해줘                                      # done
```

- `.task.toml` 에 `[issue]` 가 있으면 작업을 만들 때 이슈를 묻고(건너뛸 수 있음), PR 본문 맨 위 링크·PR 제목·커밋 첫 줄에 이슈 ID 를 붙입니다.
- 첫 PR 은 작업 브랜치 커밋을 하나로 합쳐 올리고, PR 이 생긴 뒤의 수정은 새 커밋으로 그대로 올립니다.
- PR 본문의 파일 표는 git diff 에서 만들고, 빠진 파일이나 `TODO` 가 남으면 어떤 레포도 올리지 않습니다.
- 충돌 때문에 직접 rebase 했을 때만 `--rebased` 로 `--force-with-lease` push 합니다.
- herdr 안에서 쓰면 작업마다 워크스페이스가 생깁니다. `.task.toml` 의 `[herdr] orchestrator` 를 정하면 새 작업은 그 워크스페이스에서만 만들어지고(작업 탭 에이전트가 작업을 늘리지 않게), `[herdr] label = "└ {task}"` 로 작업 워크스페이스가 오케스트레이터 아래처럼 보이게 할 수 있습니다.
