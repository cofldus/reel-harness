# Reel Harness

Reel Harness는 사용자가 주제와 원하는 영상 방향을 입력하면 대본 생성부터 음성 합성, 영상 소스 수집, 세로형 영상 렌더링까지 자동으로 처리하는 숏폼 제작 서비스입니다.
완성된 영상은 바로 게시되지 않고 미리보기와 검수 화면에서 제목, 설명, 사용한 미디어, 라이선스, 영상 품질, 게시 가능 여부를 한 번에 확인할 수 있습니다.
사용자는 마음에 들지 않는 단계만 골라 대본, 영상 소스, 음성, 렌더링을 다시 만들 수 있어 처음부터 전체 작업을 반복할 필요가 없습니다.
검수가 끝나면 YouTube, TikTok, Instagram 중 원하는 플랫폼과 계정을 선택하고 공개 범위와 게시 옵션을 확인한 뒤 업로드를 진행합니다.
업로드가 중단되거나 오류가 발생해도 이어 올리기, 재시도, 상태 복구가 가능하며 현재 작업 상태와 실패 원인을 사용자에게 명확하게 보여주는 서비스입니다.

## 현재 범위

`reel-harness serve`를 실행하면 브라우저에서 주제 입력부터 진행 상태 확인, 완성 영상 재생·다운로드, 그리고 이제 YouTube/TikTok/Instagram 계정 연결과 실제 게시까지 클릭만으로 가능합니다(Phase 5A + 5B). 실제 provider(OpenAI-compatible LLM/TTS, Pexels) 자격 증명 입력은 여전히 웹에서 안내만 하고, 값 입력은 환경변수/`.env`로 합니다 — OAuth 계정 연결만은 예외로, `/publisher-accounts` 화면에서 브라우저로 직접 연결합니다.

## 로컬에서 실행하기

다른 컴퓨터에서 이 코드를 받아 실행하려면 최소한 다음이 필요합니다.

1. 저장소 clone
2. Python 환경과 의존성 설치 (`uv sync --extra dev`)
3. FFmpeg·FFprobe 설치 (또는 `.tools/ffmpeg/bin/`에 배치)
4. 로컬 DB 초기화 또는 `serve` 실행 (첫 실행 시 자동으로 스키마가 생성됩니다)

```
uv run reel-harness preflight --profile fake
uv run reel-harness serve
```

즉, DB를 따로 구축하지 않아도 로컬 테스트는 바로 돌아가지만, 별도의 배포 설정 없이는 다른 사람이 접속하는 실제 운영 서비스로 굴러가지는 않습니다. 실제 운영 환경으로 배포하기 전에는 `reel-harness preflight --profile production`으로 API 키·자격 증명·저장소 설정을 먼저 점검하세요.

## 웹 UI로 실행하기 (터미널 명령 없이)

`serve`는 API·워커와 함께 웹 UI도 같은 프로세스에서 띄웁니다. 별도의 `web` 명령은 없습니다.

```
uv run reel-harness serve
```

브라우저에서 `http://127.0.0.1:8000/`을 열면:

1. 대시보드에서 "새 영상 만들기" 클릭
2. 주제·언어·길이·스타일 입력 (기본값은 API 키가 필요 없는 Demo Mode)
3. 생성 후 자동으로 진행 상태 화면으로 이동, 2초 간격으로 자동 갱신 (완료/실패/검수 필요 시 자동 정지)
4. 완성되면 화면에서 바로 재생, 다운로드 버튼으로 MP4 저장
5. 검수 화면에서 승인/반려/재시도/취소

Windows에서 이 저장소처럼 Application Control 정책이 `reel-harness.exe` 실행을 막는 환경이라면 `python -m reel_harness.cli.main serve`로 실행하세요 (같은 UI가 뜹니다).

로컬 단일 사용자 도구이므로 로그인 화면은 없습니다. 대신 브라우저 기반 CSRF 방어(더블서밋 쿠키)가 모든 변경 요청(생성/승인/취소/재시도/반려)에 적용됩니다. `REEL_HARNESS_API_HOST`를 loopback(127.0.0.1) 밖으로 바꿔 다른 기기에서 접속하게 하려면 `reel-harness preflight`가 그 사실을 경고/차단(운영 프로파일)하니, 신뢰할 수 있는 네트워크 안에서만 열거나 앞단에 실제 인증을 하는 리버스 프록시를 두세요.

## 웹에서 게시하기 (Phase 5B)

1. `/publisher-accounts`에서 YouTube/TikTok/Instagram 중 원하는 플랫폼의 "계정 연결" 버튼 클릭 → 각 플랫폼의 실제 인증 화면으로 이동 → 로그인/승인 후 자동으로 돌아와 계정이 연결됩니다. (OAuth 클라이언트 자체는 여전히 환경변수로 먼저 등록해야 합니다: `REEL_HARNESS_YOUTUBE_CLIENT_ID`/`_SECRET` 등 — 자세한 값은 `docs/OPERATIONS.md` 참고.)
2. 완성된(COMPLETED) 영상의 작업 상세 화면에서 "게시하기" 클릭 → 연결된 플랫폼과 계정, 공개 범위 선택 → 게시 요청 생성.
3. 게시 상세 화면이 업로드/처리 상태를 2초 간격으로 자동 갱신하며, 완료되면 게시된 영상 링크가 표시됩니다. 실패 시 재시도, 필요하면 취소 또는 플랫폼과 상태 동기화(reconcile)도 같은 화면에서 클릭만으로 가능합니다.
4. Demo Mode 영상은 라이선스상 실제 게시가 항상 불가능합니다 — "게시하기" 버튼 자체가 표시되지 않고, 그 이유가 작업 상세 화면에 그대로 나타납니다.

계정 연결 해제는 이 기기에 저장된 인증 정보만 삭제하며 플랫폼 쪽 접근 권한은 별도로 취소해야 합니다. YouTube는 공개 범위 기본값이 비공개(private), TikTok은 앱이 검수를 통과하지 못한 경우 실제로는 본인만 보기(SELF_ONLY)로 게시될 수 있음을 화면에서 경고하고, Instagram Reels는 비공개 옵션 자체가 없어 항상 공개로 게시된다는 점을 확인 체크박스로 한 번 더 확인받습니다. `REEL_HARNESS_ALLOW_PUBLIC_UPLOAD`가 꺼져 있으면(기본값) 공개 범위 선택 자체가 비활성화됩니다.

## Demo Mode — API 키 없이 실제 결과물 눈으로 확인하기

`fake` provider(Phase 0/1의 파이프라인 검증용)는 단색 프레임과 무음 오디오만 만들기 때문에 실제 UX를 판단하기 어렵습니다. Demo Mode는 **API 키를 전혀 요구하지 않으면서** 실제로 보고 들을 수 있는 결과물을 만드는 별도 provider 계열입니다.

```
uv sync --extra demo
REEL_HARNESS_LLM_PROVIDER=demo REEL_HARNESS_TTS_PROVIDER=demo REEL_HARNESS_ASSET_PROVIDER=demo \
REEL_HARNESS_RENDER_BURN_SUBTITLES=true \
uv run reel-harness demo-run --topic "김치찌개 맛있게 끓이는 법" --niche cooking --language ko
```

- **화면**: 장면마다 다른 색(고정 팔레트)의 배경 + 자막 텍스트가 실제로 화면에 박힙니다. 실사 영상은 아닙니다.
- **음성**: 로컬 OS TTS(Windows SAPI5 / Linux espeak-ng)로 실제 사람 목소리가 나옵니다. 무료·오프라인이지만 상용 API 대비 기계음스럽고, 요청 언어의 로컬 음성이 설치돼 있지 않으면 다른 언어 음성으로 대체됩니다.
- **대본**: 실제 LLM 호출 없이 주제 텍스트를 장면 수만큼 반복하는 결정론적 템플릿입니다.

즉 Demo Mode는 **콘텐츠 품질이 아니라 파이프라인(검수 대기 전환, 라이선스 게이트, 자막/오디오/렌더링 배관)이 실제로 도는지**를 확인하는 용도입니다. Demo Mode 자산도 Fake provider와 동일하게 `DEMO_TEST_LICENSE`로 표시되어 실제 게시 게이트를 절대 통과하지 못합니다.

진짜 콘텐츠 품질(문맥 있는 대본, 자연스러운 음성, 실제 스톡 영상)을 보려면 필요한 것만 골라 실제 provider를 설정하면 됩니다.

- **LLM**: OpenAI-compatible 엔드포인트 API 키 (`REEL_HARNESS_LLM_PROVIDER=openai_compatible`, `REEL_HARNESS_LLM_API_KEY` 등)
- **TTS**: OpenAI-compatible 엔드포인트 API 키 (`REEL_HARNESS_TTS_PROVIDER=openai_compatible`, `REEL_HARNESS_TTS_API_KEY` 등)
- **스톡 미디어**: Pexels API 키 (`REEL_HARNESS_ASSET_PROVIDER=pexels`, `REEL_HARNESS_ASSET_API_KEY`)
- **게시**: YouTube/TikTok/Instagram 각각 OAuth 클라이언트 ID/Secret (계정 연결은 `reel-harness publisher-auth` CLI 또는 웹 `/publisher-accounts` 화면 둘 다 가능)

## 실제 플랫폼 게시 검증 상태

YouTube·TikTok·Instagram 업로드 기능은 각 플랫폼의 공개 API 사양에 맞춰 구현되어 있고 계약(contract) E2E 테스트로 커버되지만, 이 릴리스를 빌드·테스트한 환경에는 세 플랫폼 모두 실제 자격 증명이 구성되어 있지 않습니다. 즉 **실제 계정으로의 게시가 아직 검증되지 않은 preview 기능**입니다. `reel-harness live-verify`로 직접 상태를 확인할 수 있고, 실제 자격 증명을 구성한 뒤 `--upload-tests`로 재검증할 수 있습니다. 자세한 내용은 `CHANGELOG.md`와 릴리스 매니페스트의 `live_verification` 필드를 참고하세요.

자세한 운영 방법은 `docs/OPERATIONS.md`, 아키텍처는 `docs/ARCHITECTURE.md`, 현재 구현 상태는 `docs/STATUS.md`를 참고하세요.
