# 시간모아

현재 AI 대화에 제공된 여러 사람의 불가 시간과 선호를 한 번에 구조화해 후보를
계산하고, 정보가 부족한 사람만 추가로 확인하는 PlayMCP 서버입니다.

AGENTIC PLAYER 10 예선 제출용 구현입니다.

## 사용자 흐름

1. 사용자가 현재 AI 대화에 나온 참여자별 조건으로 일정 조율을 요청합니다.
2. Agent가 조건을 구조화하면 MCP가 한 번에 저장하고 후보를 즉시 계산합니다.
3. 정보가 부족한 참여자만 room code로 조건을 추가하거나 바꿉니다.
4. 주최자는 최신 후보와 미확인 참여자를 확인한 뒤 직접 확정합니다.

일반 카카오톡 대화방을 자동으로 읽지 않으며, 실제 Google/Outlook 캘린더와
연동됐다고 표현하지 않습니다.

## MCP Tools

- `coordinate_schedule`: 여러 참여자의 현재 조건을 저장하고 즉시 후보와 공유 code 반환
- `submit_participant_preference`: 참여자 한 명의 hard block, avoid, prefer 제출
- `get_coordination_candidates`: 후보 시간, 집계 근거, 미응답·미확인 반환

모든 응답은 typed structured output입니다. 후보 계산은 hard block 제거, 가능 확인
인원 최대화, avoid 인원 최소화, prefer 인원 최대화 순으로 결정됩니다.

## 로컬 실행

```bash
docker build -t timemoa-mcp .
docker run --rm -p 8000:8000 -v timemoa-data:/data timemoa-mcp
```

- MCP: `http://127.0.0.1:8000/mcp`
- Health: `http://127.0.0.1:8000/health`

서버 실행 후 smoke test:

```bash
docker run --rm --network host \
  -e MCP_URL=http://127.0.0.1:8000/mcp \
  -v "$PWD/mcp-server/smoke_test.py:/app/smoke_test.py:ro" \
  timemoa-mcp python smoke_test.py
```

## 카카오클라우드 Git 소스 빌드

- Git URL: 이 저장소의 HTTPS URL
- 브랜치/ref: `main`
- Dockerfile 경로: `Dockerfile`
- PAT: 공개 저장소는 비워두고, 비공개 저장소일 때만 사용

컨테이너는 `PORT` 환경변수를 지원하며 기본값은 `8000`입니다.

## 저장과 개인정보

- SQLite에 구조화된 시간 구간만 저장합니다.
- 대화 원문과 외부 캘린더 credential은 저장하지 않습니다.
- room은 기본 7일 후 만료되며 호출 시 만료 데이터를 정리합니다.
- room code를 모르면 조회할 수 없고, not-found 오류에서 다른 code를 노출하지 않습니다.
- 현재 room code를 가진 사용자는 결과를 조회하고 등록된 이름으로 조건을 다시 제출할
  수 있습니다. 참여자별 인증은 본선 보강 범위입니다.

## 환경변수

- `PORT`: HTTP port, 기본 `8000`
- `COORDINATION_DB_PATH`: SQLite 파일, 기본 `/data/coordination.db`
- `COORDINATION_ROOM_TTL_DAYS`: room 보존 기간, 기본 `7`
- `MCP_ALLOWED_HOSTS`: 선택적 host allowlist
- `MCP_ALLOWED_ORIGINS`: 선택적 origin allowlist

카카오클라우드는 endpoint hostname을 배포 후 할당하므로 기본 설정에서는 DNS
rebinding 검사를 비활성화합니다. 고정 hostname을 확보한 뒤 `MCP_ALLOWED_HOSTS`를
설정하면 allowlist 검사를 활성화할 수 있습니다.
