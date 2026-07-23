# Reel Harness

TikTok/Shorts 숏폼 영상 자동 생성 서비스. local-first, single-user. 저장소 루트 디렉터리 이름은 `umma`이지만 프로젝트/패키지 식별자는 **Reel Harness / reel-harness / reel_harness**를 사용한다.

## 현재 구현 범위

지금 구현된 것은 **Phase 0(최소 프로젝트 기반)**과 **Phase 1(Fake provider 기반 수직 E2E)**뿐이다. Phase 2 이상(실제 provider 연결, 웹 UI, 게시 연동)은 아직 코드가 없다 — 확장 지점은 `docs/ARCHITECTURE.md`에 문서로만 존재한다. 현재 상태는 `docs/STATUS.md` 참조.

## 핵심 규칙

- 잡의 산출물/중간 파일은 항상 `jobs/{job_id}/{stage}/` 아래에만 쓴다(`reel_harness.storage.local.LocalFilesystemStorage`가 경로 이탈을 거부한다). 전역 temp 디렉터리에 쓰지 않는다.
- 잡 상태 전이는 `reel_harness.core.state_machine.transition()`을 통해서만 한다. `status`(전체 상태)와 `current_stage`(현재/마지막 단계)는 서로 다른 필드이며 혼용하지 않는다.
- LLM/TTS/StockMedia/Publisher 벤더명은 `reel_harness/providers/registry.py` 외 어디에도 하드코딩하지 않는다. 도메인 로직은 `reel_harness/providers/base.py`의 Protocol에만 의존한다.
- subprocess는 항상 `list[str]` 인자와 `shell=False`를 쓴다(`reel_harness.media.runner.ProcessRunner`). 문자열을 이어붙여 명령어를 만들지 않는다.
- job_id는 서버가 생성한 UUID만 사용한다. 사용자 입력이 파일 경로에 직접 반영되지 않는다.
- ffmpeg/ffprobe가 시스템에 없으면 성공한 것처럼 우회하지 않는다. `reel_harness.media.deps.check_ffmpeg_available()`이 없다고 보고하면 렌더/검증 단계는 `BLOCKED_DEPENDENCY`로 실패해야 한다(재시도 불가, FAILED). ffmpeg를 임의로 전역 설치하지 않는다.
- Fake provider가 만든 자산의 라이선스는 항상 `FAKE_TEST_LICENSE`로 표시하고, 실제 게시 게이트를 통과시키지 않는다.
- 새 provider/pipeline 코드는 `reel_harness/providers/fake_*.py`로 먼저 검증한다. 테스트에서 실제 외부 API를 호출하지 않는다(`tests/conftest.py`의 네트워크 차단 fixture 참조).

## 개발 환경

- 패키지/venv 관리는 `uv`를 사용한다(`uv sync`, `uv run pytest`, `uv run reel-harness ...`). 시스템 전역 설치는 하지 않는다.
- Docker 실행, 실제 외부 API 호출, git commit/push/remote 생성은 사용자의 명시적 요청이 있을 때만 한다.

세부 규칙: `.claude/rules/architecture.md`
