"""Measure adaptation QUALITY, not whether adaptation runs.

The pipeline has been verified end to end for a while; what was never
checked is whether the shot plans it produces are any good. This exists
because "good" needs to be counted rather than felt -- the alternative is
one person's impression of one run, which is how a prompt regression
survives for months.

What it measures, and why each one:

  runtime fit  The schema bounds a plan to 4-15 shots but says nothing
               about the runtime that was ordered, so four shots is
               "valid" whether you asked for 32 seconds or 120.
  coverage     Distinct shot sizes / angles / movements. A plan that is
               entirely medium, eye-level and locked off is a slideshow,
               and that is the specific failure this catches.
  repetition   Identical action lines across shots.
  dialogue     Share of shots carrying a spoken line. Users are told to
               write dialogue in quotes; a pipeline that discards it
               makes that instruction a lie.
  fidelity     Whether each scene's source_beat is genuinely drawn from
               the author's text rather than invented.
  variance     The same story adapted N times. Zero variance means the
               prompt has collapsed to a single answer.

Everything here runs through `run_adaptation`, so what is measured is
what a user would actually receive -- repair loop included. An earlier
version of this measured the director's RAW output and reported failures
that the validators would have caught and repaired, which made the
pipeline look worse than it was.
"""
from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field

from reel_harness.core.adaptation_service import AdaptationOutcome, run_adaptation
from reel_harness.pipeline.adaptation_parser import _SPOKEN_RE
from reel_harness.pipeline.adaptation_schema import AdaptationModel
from reel_harness.providers.base import AdaptationRequest

# Short, deliberately different pieces: one with quoted speech and one
# character, one with two characters and no speech at all, one whose
# turn is a physical discovery. A prompt that only works on interior
# monologue is not a prompt that works.
SAMPLE_STORIES: dict[str, str] = {
    "비 오는 밤": (
        "새벽 세 시, 지우는 호텔 방 창가에 서 있었다. 서른쯤의 그녀는 젖은 트렌치코트를 "
        "벗지 않은 채였고, 머리는 하나로 묶여 있었다. 네온 간판이 창으로 붉게 들어와 "
        "바닥에 긴 그림자를 만들었다. 전화벨이 울렸다. 그녀는 받지 않았다. 벨이 끊기고, "
        "다시 울리고, 또 끊겼다. “이제 그만하자.” 그녀가 아무도 없는 방에 대고 말했다. "
        "한참 뒤, 지우는 창을 닫았다. 그리고 탁자 위의 사진을 반으로 접어 코트 주머니에 "
        "밀어 넣고, 문 쪽으로 천천히 돌아섰다."
    ),
    "마지막 승객": (
        "막차의 승객은 그 사람 하나였다. 기사는 백미러로 몇 번이나 뒷좌석을 확인했다. "
        "중년의 남자는 창에 이마를 대고 잠든 것처럼 보였다. 버스가 정류장마다 섰다가 "
        "다시 출발했고, 문이 여닫히는 소리만 반복됐다. 종점에 도착했을 때 기사는 "
        "뒤를 돌아보았다. 좌석은 비어 있었고 창문에는 손자국만 남아 있었다. "
        "그는 한참 동안 그 자리에 앉아 있다가, 시동을 껐다."
    ),
    "아침의 편지": (
        "이사한 집의 우편함에는 이전 주인 앞으로 온 편지가 쌓여 있었다. 스무 살의 그는 "
        "낡은 후드티 차림으로 계단에 앉아 한 통을 열어보았다. 아침 햇빛이 계단참 창으로 "
        "비스듬히 들어왔다. 편지에는 병원 이름과 날짜가 적혀 있었다. 그는 봉투를 다시 "
        "접었다. 그날부터 그는 매주 답장을 썼다. 보낼 곳도 모른 채. 어느 날 아침, "
        "우편함에는 그가 쓴 편지 한 통이 되돌아와 있었다."
    ),
}


def _normalise(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


@dataclass
class PlanMetrics:
    shots: int
    scenes: int
    size_kinds: int
    angle_kinds: int
    move_kinds: int
    # Share of shots using the single most common movement. 1.0 means the
    # camera never moves.
    move_top_share: float
    duplicate_actions: int
    dialogue_share: float
    beats_quoted: float
    characters: int
    characters_described: int
    movements: dict[str, int] = field(default_factory=dict)
    # None when the caller did not ask for a particular runtime.
    target_shots: int | None = None

    @property
    def fits_runtime(self) -> bool:
        if self.target_shots is None:
            return True
        return abs(self.shots - self.target_shots) <= 1

    @property
    def camera_never_moves(self) -> bool:
        return self.shots >= 3 and self.move_kinds == 1 and "locked" in self.movements

    @property
    def single_angle(self) -> bool:
        return self.shots >= 3 and self.angle_kinds == 1


def measure(
    adaptation: AdaptationModel, source_text: str, target_shots: int | None = None,
) -> PlanMetrics:
    """Pure over an already-validated model. No network, no I/O."""
    shots = [shot for scene in adaptation.scenes for shot in scene.shots]
    scenes = adaptation.scenes
    if not shots:
        return PlanMetrics(0, len(scenes), 0, 0, 0, 0.0, 0, 0.0, 0.0, 0, 0,
                           target_shots=target_shots)

    sizes = Counter(s.shot_size for s in shots)
    angles = Counter(s.camera_angle for s in shots)
    moves = Counter(s.camera_movement for s in shots)
    actions = [_normalise(s.action) for s in shots]

    haystack = _normalise(source_text)
    quoted = sum(
        1 for scene in scenes
        if _normalise(scene.source_beat) and _normalise(scene.source_beat) in haystack
    )
    described = sum(
        1 for c in adaptation.characters
        if all(str(getattr(c, f, "") or "").strip() for f in ("appearance", "wardrobe", "hair"))
    )
    return PlanMetrics(
        shots=len(shots),
        scenes=len(scenes),
        size_kinds=len(sizes),
        angle_kinds=len(angles),
        move_kinds=len(moves),
        move_top_share=moves.most_common(1)[0][1] / len(shots),
        duplicate_actions=len(actions) - len(set(actions)),
        dialogue_share=sum(1 for s in shots if (s.dialogue_line or "").strip()) / len(shots),
        beats_quoted=quoted / len(scenes) if scenes else 0.0,
        characters=len(adaptation.characters),
        characters_described=described,
        movements=dict(moves),
        target_shots=target_shots,
    )


@dataclass
class RunResult:
    story: str
    run: int
    metrics: PlanMetrics | None
    outcome: AdaptationOutcome | None
    error: str | None = None


def evaluate(
    director, stories: dict[str, str], *, runs: int = 3, target_duration_sec: int = 32,
    language: str = "ko",
) -> list[RunResult]:
    """One adaptation per story per run, through the REAL service path.

    A failure is data, not a crash: a story that cannot be adapted at all
    is exactly the kind of thing this is meant to surface, so it is
    recorded and the sweep continues.
    """
    from reel_harness.pipeline.adaptation_parser import SHOT_SECONDS

    target_shots = max(1, round(target_duration_sec / SHOT_SECONDS))
    results: list[RunResult] = []
    for title, source in stories.items():
        for run in range(1, runs + 1):
            request = AdaptationRequest(
                source_text=source, language=language, genre=None, tone=None,
                target_duration_sec=target_duration_sec, aspect_ratio="9:16",
            )
            try:
                outcome = run_adaptation(director, request)
            except Exception as exc:  # noqa: BLE001 - a failed run is a finding
                results.append(RunResult(title, run, None, None, f"{type(exc).__name__}: {exc}"))
                continue
            results.append(RunResult(
                title, run, measure(outcome.adaptation, source, target_shots), outcome,
            ))
    return results


def format_plan(adaptation: AdaptationModel) -> str:
    """The actual shot plan, readable.

    Numbers say whether a plan is varied; only the plan itself says
    whether it is any good. Both belong in the report.
    """
    lines = [f"  logline: {adaptation.logline}"]
    for scene in adaptation.scenes:
        lines.append(f"  ── scene {scene.scene_order} · {scene.location_name} · {scene.story_purpose}")
        lines.append(f"     beat: {scene.source_beat[:70]}")
        for shot in scene.shots:
            grammar = " · ".join(filter(None, (
                shot.shot_size, shot.camera_angle, shot.camera_movement,
            )))
            lines.append(f"     [{shot.shot_order}] {shot.action}")
            lines.append(f"         {grammar}")
            if (shot.dialogue_line or "").strip():
                lines.append(f"         “{shot.dialogue_line}”")
    return "\n".join(lines)


def format_report(results: list[RunResult], *, show_plans: bool = False) -> str:
    lines: list[str] = []
    ok = [r for r in results if r.metrics is not None]

    for result in results:
        if result.metrics is None:
            lines.append(f"  {result.story} run{result.run}: FAILED {result.error}")
            continue
        m = result.metrics
        flags = "".join((
            "" if m.fits_runtime else " RUNTIME",
            " ONE-ANGLE" if m.single_angle else "",
            " NO-MOVEMENT" if m.camera_never_moves else "",
        ))
        repairs = result.outcome.attempts - 1 if result.outcome else 0
        lines.append(
            f"  {result.story} run{result.run}: shots={m.shots:2d} scenes={m.scenes} "
            f"sizes={m.size_kinds} "
            f"angles={m.angle_kinds} moves={m.move_kinds} top-move={m.move_top_share:.0%} "
            f"dup={m.duplicate_actions} dlg={m.dialogue_share:.0%} "
            f"quoted={m.beats_quoted:.0%} repairs={repairs}{flags}"
        )
        if show_plans and result.outcome is not None:
            lines.append(format_plan(result.outcome.adaptation))

    if not ok:
        lines.append("\nno successful runs")
        return "\n".join(lines)

    lines.append("\n=== summary ===")
    by_story: dict[str, list[PlanMetrics]] = {}
    for result in ok:
        by_story.setdefault(result.story, []).append(result.metrics)  # type: ignore[arg-type]
    for title, metrics in by_story.items():
        # `metrics` bound as a default: a closure over the loop variable
        # reads whatever the last iteration left behind, which is the
        # kind of bug that only shows up once there is more than one
        # story in the sweep.
        def avg(pick, metrics=metrics) -> float:
            return statistics.fmean(pick(m) for m in metrics)
        lines.append(
            f"{title}: shots~{avg(lambda m: m.shots):.1f} "
            f"scenes~{avg(lambda m: m.scenes):.1f} "
            f"sizes~{avg(lambda m: m.size_kinds):.1f} "
            f"angles~{avg(lambda m: m.angle_kinds):.1f} "
            f"moves~{avg(lambda m: m.move_kinds):.1f} "
            f"dlg~{avg(lambda m: m.dialogue_share):.0%} "
            f"quoted~{avg(lambda m: m.beats_quoted):.0%}"
        )
        counts = sorted({m.shots for m in metrics})
        lines.append(f"    shot counts across runs: {counts}")

    all_metrics = [r.metrics for r in ok if r.metrics]
    lines.append("")
    lines.append(f"runs: {len(ok)}/{len(results)} succeeded")
    lines.append(f"  off-runtime:    {sum(1 for m in all_metrics if not m.fits_runtime)}")
    lines.append(f"  single angle:   {sum(1 for m in all_metrics if m.single_angle)}")
    lines.append(f"  camera static:  {sum(1 for m in all_metrics if m.camera_never_moves)}")
    lines.append(f"  fidelity < 100%: {sum(1 for m in all_metrics if m.beats_quoted < 1.0)}")
    return "\n".join(lines)


def has_quoted_speech(source_text: str) -> bool:
    """Whether a source contains speech the plan is obliged to keep."""
    return bool(_SPOKEN_RE.search(source_text or ""))
