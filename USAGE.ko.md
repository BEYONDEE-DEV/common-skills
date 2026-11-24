# BEYONDEE Skills 사용법

설치는 [README.md](./README.md)를 참고하세요. 설치나 업데이트 후에는 Codex는 새 스레드에서, Claude Code는 재시작 후 사용하세요.

## 준비

프로젝트 루트(레포들이 나란히 있는 폴더)에 `.task.toml` 을 둡니다. 형식은 [skills/task/SKILL.md](./skills/task/SKILL.md) 를 보세요.
사람이 직접 실행할 때는 `python3 <플러그인>/scripts/task.py <명령>` 입니다(프로젝트에 `task.sh` 래퍼를 둬도 됩니다).

## 흐름

```text
/common-skills:task  order_api, admin_api 로 sales-owner-contract 작업 만들어줘   # new
/common-skills:task  sales-owner-contract 상태                                   # status
/common-skills:pr    sales-owner-contract PR 올려줘                               # pr --prepare → 본문 → pr
(사람이 GitHub 에서 Rebase and merge)
/common-skills:task  sales-owner-contract 정리해줘                                # done
```

- 첫 PR 은 작업 브랜치 커밋을 하나로 합쳐 올리고, PR 이 생긴 뒤의 수정은 새 커밋으로 그대로 올립니다.
- PR 본문의 파일 표는 git diff 에서 만들고, 빠진 파일이나 `TODO` 가 남으면 어떤 레포도 올리지 않습니다.
- 충돌 때문에 직접 rebase 했을 때만 `--rebased` 로 `--force-with-lease` push 합니다.
