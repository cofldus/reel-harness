# ADR-0003: Continuous scenes via Veo extend, cuts only between scenes

**Status: proposed.** Nothing below is built. It is written down so the
trade-offs are argued before any of it is, because the change reaches the
schema, the worker, the review UI and the cost model at once.

## Context

The pipeline generates each shot as an independent 8-second Veo clip with
approved character reference images, then concatenates the chosen takes.
Watching a real film it produced (project 「우산」, 4 shots, 32s) the
verdict from the person who watched it was blunt: the shots do not follow
one another. They are four clips of the same story, not a sequence.

That is inherent to the method. Nothing connects clip N to clip N+1
except the words in the prompt, so the model is free to restage the room,
move the actor and relight the set at every cut. Prompt work has already
been pushed about as far as it goes: each shot now describes the frame it
cuts from and carries a standing set contract. It helps; it does not make
a cut invisible.

### What was measured (2026-08-04, live against Vertex)

A probe reused an existing take and its approved reference sheets, so
only the extension itself was paid for.

| Question | Answer |
|---|---|
| `video=` (extend) together with `reference_images`? | **Refused.** `Video and reference images cannot be both set.` |
| Local file, or must it be `gs://`? | **Local works** — `video_bytes` is accepted, so remote storage (6A-3) is NOT a prerequisite |
| Length after one extension | **15.04s** — exactly 8 + 7 |
| Own prompt per extension? | **Yes**, and it was obeyed (the clerk turned toward the door as instructed) |
| Does the character survive without reference images? | **Yes** — same face, same uniform, same set, same lighting and camera height |
| Audio on the extension? | Yes |

The last row is the one that changes the decision. The refusal looked
fatal at first: if an extension cannot carry the approved reference
sheets, the film would abandon its cast after eight seconds. It does not.
**The footage is itself the reference.** The person is already on screen,
so continuing the video continues them.

## Decision

Generate a scene as ONE continuous chain, and cut only between scenes.

- The **first segment of a scene** is generated the way every shot is
  generated today: 8 seconds, reference-image driven, so the approved
  cast is what appears.
- **Every later segment of that scene** is an extension of the chosen
  take before it: +7 seconds, its own prompt carrying that beat's action,
  camera intent and dialogue.
- **Scene boundaries stay hard cuts.** A scene is by definition one place
  and one continuous moment; when it changes, the audience is supposed to
  feel a jump. The film editor keeps concatenating scenes exactly as now.

A scene of N segments runs `8 + 7(N-1)` seconds. Total runtime is the sum
over scenes, so the offered durations stop being multiples of 8.

## Consequences

### The review model changes shape, and this is the real cost

Today a shot is independent: generate candidates, pick one, regenerate
just that shot if none are good. In a chain, segment 3 is generated from
segment 2's **chosen** take. Rejecting segment 2 invalidates 3 and
everything after it in that scene.

Review therefore moves from "pick a take per shot" to "accept the chain
to here, or rewind to segment K and regenerate forward". That is a
genuine reduction in control, and it must be shown honestly in the UI:
rejecting a segment has to state how many segments it discards and what
that costs, before the click.

Candidate takes still make sense — several attempts at the next 7 seconds
— but only one can be chosen, because choosing is what the next segment
extends from.

### Schema and worker

- A take needs to record which take it extends (`extends_take_id`), so a
  chain is reconstructable and a rewind knows what to discard.
- Shot readiness stops being independent: a shot is only leasable once
  its predecessor in the same scene has a SELECTED take. The worker's
  lease query gains that condition. Shots in DIFFERENT scenes remain
  independent and can still run concurrently.
- A failed or timed-out extension blocks the rest of its scene. Today it
  blocks one shot. The failure UI has to say which.

### Cost

**Unresolved and load-bearing: is an extension billed for the new seconds
or for the whole returned video?** A 29-second scene costs 29 billed
seconds one way and 8+15+22+29 = 74 the other. That is 2.5x, and it
decides whether long scenes are viable at all.

Until an invoice settles it, the estimate must assume the expensive
reading. Under-quoting lets a project overrun the ceiling its owner set;
over-quoting only makes the gate refuse work that was affordable. Same
asymmetry that keeps the per-second price at the higher figure.

### Risk this design does not remove

Each extension is conditioned on generated footage, so artefacts compound
along a chain. **Only ONE extension has been tested.** Whether quality
holds at four or ten is unknown, and the vendor's 20-extension ceiling is
not evidence that ten look good. Before committing, generate one long
chain and watch it.

## Alternatives considered

**Keep independent shots, push the prompt further.** Already done, and it
is what the current code does. It cannot make a cut invisible because
nothing shares state between generations.

**First-frame chaining** (pass shot N's last frame as shot N+1's first
frame). Same mutual-exclusion problem as extend, and strictly worse: it
forfeits the reference images without the compensating property, since a
single still does not carry the set and lighting the way continuing
footage does.

**Do nothing.** Defensible if the cuts turn out to bother nobody but the
author. It has bothered the author.
