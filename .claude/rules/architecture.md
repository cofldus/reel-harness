# 아키텍처 규칙 (Reel Harness)

전체 설계 근거는 `docs/adr/`와 `docs/ARCHITECTURE.md`. 여기서는 코드 작성 시 지켜야 할 불변식만 정리한다.

## 확정된 결정

- 오케스트레이션: n8n 등 외부 워크플로우 엔진 없음. 애플리케이션 DB(SQLite)가 잡 상태의 유일한 원본이다. (ADR-0001)
- API: FastAPI. 요청은 즉시 `job_id`를 반환하고, 실제 작업은 별도 워커 프로세스(폴링)가 수행한다.
- 워커: 단일 프로세스 폴링 워커. DB 테이블 기반 lease(`locked_by` + `heartbeat_at`)로 동시 lease를 방지한다.
- 저장소: `StorageBackend` Protocol의 첫 구현체는 `LocalFilesystemStorage`(`jobs/{job_id}/` 아래로 경로를 강제한다). (ADR-0002)
- Provider: LLM/TTS/StockMedia/Publisher 각각 Protocol + registry. 지금은 Fake 구현체만 존재한다.
- 관리 인터페이스: CLI 우선(`reel-harness` 명령). 웹 UI는 아직 없다.

## 상태 모델

- `status`: 잡의 전체 상태. 허용된 값과 전이 규칙은 `reel_harness/core/state_machine.py`의 `ALLOWED_TRANSITIONS`에 있다. 코드에서 이 모듈을 거치지 않고 상태를 바꾸지 않는다.
- `current_stage`: 현재 또는 마지막으로 실행된 파이프라인 단계. `status`와 별도 필드다(예: `status=RETRY_WAIT`이면서 `current_stage=RENDERING`일 수 있다 — 어느 단계에서 재시도를 기다리는지 알아야 하기 때문).
- `RETRY_WAIT`에는 `retry_target_stage`, `next_retry_at`, `failure_code`, `failure_summary`가 항상 채워져야 한다.
- `REVIEW_REQUIRED`에는 `reason_code`가 항상 채워져야 한다(`CONTENT_POLICY_REVIEW`, `ASSET_NOT_FOUND`, `TECHNICAL_VALIDATION_FAILED`, `USER_APPROVAL_REQUIRED`, `LICENSE_INFORMATION_MISSING`).
- 반려(reject) 후 재생성은 기본적으로 같은 `job_id` 안에서 새 `StageRun`을 만든다. `parent_job_id`는 명시적으로 별도 A/B 변형 잡을 만들 때만 쓴다.
- 콘텐츠 정책이나 품질 판단이 불확실하면 자동 차단(FAILED)하지 말고 `REVIEW_REQUIRED`로 보낸다. 자동 FAILED는 명백히 재시도 불가능한 경우(예: `BLOCKED_DEPENDENCY`, 명백한 정책 위반)에만 쓴다.

## 미디어/의존성

- ffmpeg/ffprobe 존재 여부는 렌더/검증 단계 진입 시 매번 확인한다(`check_ffmpeg_available()`). 없으면 `DependencyError(code=BLOCKED_DEPENDENCY)` → `FAILED`(재시도 불가). 캐시된 "설치돼 있었음" 가정을 하지 않는다.
- 프로세스 취소는 `reel_harness.media.runner.ProcessRunner`에 캡슐화한다. POSIX와 Windows의 프로세스 트리 종료 방식이 다르다는 것을 가정하고 플랫폼 분기를 이 클래스 밖으로 노출하지 않는다.
- 테스트에서 결과 mp4의 바이트를 그대로 비교하지 않는다(ffmpeg 버전/OS에 따라 달라진다). 정규화된 manifest, ffprobe 결과(해상도/코덱/길이/오디오 스트림 존재), 단계별 입력 hash로 검증한다.

## 미래 확장 지점 (아직 코드 없음)

- Redis/RQ 기반 큐로 워커 교체 (`JobQueue` 인터페이스가 생기면 그 뒤에 구현)
- S3 호환 `StorageBackend` 구현체 추가
- 실제 LLM/TTS/StockMedia/Publisher 구현체 (`reel_harness/providers/registry.py`에 등록만 추가)
- Alembic 기반 증분 마이그레이션 (현재는 `init_db()`로 전체 스키마 생성 — 실제 스키마 변경이 필요해지는 시점에 도입)
- 웹 관리 UI, TikTok/YouTube 게시 연동, A/B 성과 데이터 수집
