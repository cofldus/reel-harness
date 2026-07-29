# Reel Harness

Reel Harness는 사용자가 주제와 원하는 영상 방향을 입력하면 대본 생성부터 음성 합성, 영상 소스 수집, 세로형 영상 렌더링까지 자동으로 처리하는 숏폼 제작 서비스입니다.
완성된 영상은 바로 게시되지 않고 미리보기와 검수 화면에서 제목, 설명, 사용한 미디어, 라이선스, 영상 품질, 게시 가능 여부를 한 번에 확인할 수 있습니다.
사용자는 마음에 들지 않는 단계만 골라 대본, 영상 소스, 음성, 렌더링을 다시 만들 수 있어 처음부터 전체 작업을 반복할 필요가 없습니다.
검수가 끝나면 YouTube, TikTok, Instagram 중 원하는 플랫폼과 계정을 선택하고 공개 범위와 게시 옵션을 확인한 뒤 업로드를 진행합니다.
업로드가 중단되거나 오류가 발생해도 이어 올리기, 재시도, 상태 복구가 가능하며 현재 작업 상태와 실패 원인을 사용자에게 명확하게 보여주는 서비스입니다.

## 현재 범위

웹 UI는 아직 범위 밖입니다. 현재 Reel Harness는 완성된 SaaS 화면이라기보다 **CLI + FastAPI 기반의 영상 자동화 백엔드**에 가깝습니다.

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

자세한 운영 방법은 `docs/OPERATIONS.md`, 아키텍처는 `docs/ARCHITECTURE.md`, 현재 구현 상태는 `docs/STATUS.md`를 참고하세요.
