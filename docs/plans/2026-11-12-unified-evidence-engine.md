# Unified Evidence Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use galyarder-framework:subagent-driven-development (recommended) or galyarder-framework:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the regex-driven perception routing with one evidence engine that resolves visual evidence by what the turn actually used, never publishes evidence from an invalidated stream, and reports every answer with a traceable evidence path.

**Architecture:** Four components replace the current mixed responsibilities. `SnapshotStore` owns frames, stream identity, epoch, timestamps and liveness. `EvidenceStore` owns inspection results with provenance and validity. `EvidenceResolver` owns reuse, inspection, budget and invalidation. `VisionInspector` owns the model call and returns a structured verdict. `PerceptionContext` becomes a thin adapter from the conversation runtime to the resolver. One report contract reads the current journal format; a legacy adapter reads historical journals so published scorecards stay identical.

**Tech Stack:** Python 3.12, pydantic v2, langchain-core chat models, pytest, ruff, mypy, import-linter, deptry, docker compose (meeting simulator).

---

## Why this plan exists

The current perception stack grew one workaround at a time:

- `camera_evidence.py` classifies human questions with keyword regexes (`question_need`), decides sufficiency from whether a field is non-empty (`observation_sufficient`), and reads free text to guess refusal (`_detail_not_visible` in `perception/context.py`).
- `perception/context.py` owns liveness validation, question classification, cache routing, vision calls, refusal interpretation and evidence shaping.
- `observability/report_contract.py` carries two live algorithms (`_path_of` and `_classify_v2`) selected by a capability marker that `Journal.record()` stamps on every new event, so a late background event can change how a historical journal is read.

Three concrete defects follow from that shape:

1. A filled field is treated as proof of an answer. `clothing: "the participant wears a hoodie"` makes "what colour is the hoodie" sufficient even though no colour was observed.
2. Keyword routing guesses intent. "Is he wearing a watch?" routes to `clothing` because it contains "wearing".
3. An inference that started before a camera-off can publish its result into the cache after the invalidation, so a later question can be answered from a frame that no longer exists.

The redesign removes the guessing: the vision model returns a structured verdict, the resolver decides reuse from snapshot identity and question identity, and nothing outside the resolver decides what counts as proof.

---

## Baseline CI debt (must be closed before this plan merges)

`main` is red at `5680e49`. Both failures are known and are fixed inside this plan.

| Failure | Cause | Fix | State |
| --- | --- | --- | --- |
| Foundation checks: `test_python_files_contain_no_indonesian[src/fakeyou/perception/camera_evidence.py]` | The regex classifier carried Indonesian keywords | Phase 3 deletes the classifier | Fixed in Phase 3 |
| Foundation checks: `test_python_files_contain_no_indonesian[src/fakeyou/perception/context.py]` | `_detail_not_visible` carried Indonesian refusal markers | Phase 3 deletes the substring refusal check | Fixed in Phase 3 |
| Foundation checks: `test_test_files_are_named_after_a_module_or_a_cross_cutting_suite[test_camera_liveness.py]` | `camera_liveness` is not a module name | Phase 1 merges the file into `tests/test_camera_evidence.py` | Fixed in Phase 1 |
| Meeting simulator: `test_the_twin_only_describes_a_camera_it_can_attribute` | The camera path sent the generic observation prompt while the cassette recorded the targeted camera prompt, so the replay missed and the turn failed | Phase 3 restores the targeted camera prompt | Fixed in Phase 3 |

The branch `refactor/unified-evidence-engine` carries the fixes so the baseline recovers in one merge. `main` stays red until the branch merges.

---

## File structure

| File | Responsibility after this plan |
| --- | --- |
| `src/fakeyou/perception/timeline.py` | Owns the snapshot identity: `CameraFrame` carries its sequence and generation, `CameraFrameStore` assigns them and invalidates them. This is the `SnapshotStore` of Phase 2, not a third store |
| `src/fakeyou/perception/evidence.py` | New. `VisualInspection`, `EvidenceResult`, `EvidenceStore`: inspection verdicts with provenance and validity |
| `src/fakeyou/perception/resolver.py` | New. `EvidenceResolver`: reuse, inspection, budget, invalidation, generation checks |
| `src/fakeyou/perception/inspector.py` | New. `VisionInspector`: one model call that returns a structured verdict for a question about a frame |
| `src/fakeyou/perception/camera_evidence.py` | The answer cache only: one answer per participant, keyed by question and snapshot identity. It owns no identity counters |
| `src/fakeyou/perception/context.py` | Adapter. Validates the request, calls the resolver, shapes the evidence payload. No regex, no cache algorithm |
| `src/fakeyou/perception/interpreter.py` | Keeps screen reading and reply planning helpers. Camera observation moves to the inspector |
| `src/fakeyou/observability/report_contract.py` | One contract for current journals, one legacy adapter module for historical journals |
| `src/fakeyou/observability/legacy_report.py` | New. Reads historical journals with their historical semantics |
| `src/fakeyou/observability/journal.py` | Records the session contract once at session start instead of stamping capability markers on every event |

---

## Phase 1 — Generation-safe snapshot lifecycle

**Release condition:** an inference that started before a camera-off, a participant leave, a stream change or a browser rejoin can never publish evidence, and can never be served to a later turn.

**Status: implemented.** The snapshot identity lives with the snapshot, not with the answer cache. `CameraFrameStore` assigns every stored frame a `sequence` and stamps it with the participant `generation`; `drop` bumps the generation and `clear` bumps the store epoch. The answer cache in `camera_evidence.py` owns no counters at all.

The ticket is captured before the first asynchronous operation: the context reads the snapshot synchronously and the interpreter builds the ticket from it before the JPEG preprocessing is scheduled. After the inference the ticket is re-validated against the store on five points: the store epoch, the participant generation, the active frame sequence, the active frame generation and the stream identity. A mismatch raises `StaleEvidence`, the answer is neither cached nor returned, and the turn reports the camera as unavailable.

The camera inspection is bounded: the context wraps the whole inspection in `asyncio.timeout(LOOK_TIMEOUT_SECONDS)`, a provider failure becomes `look_failed` with the error type, and a budget rejection stays `budget_exhausted`. A cancellation is never converted into a result.

The same validation runs twice. It runs before the cache lookup and before the vision call, so a request that captured an older snapshot can neither reuse a cached answer nor start an inspection for it, and it runs again after the inference, so an answer produced for a snapshot that has since been replaced is never published.

**The ingestion boundary.** `PerceptionSensors._accept_frame` captures the epoch, the assignment identity and the participant generation before it decodes the JPEG, and re-validates all three after the decode. The participant generation is monotonic, so an assignment that goes away and comes back, the ABA case, is caught even though the final state looks identical. A frame that was decoded across an epoch change, a camera-off, a participant leave or a stream reassignment is dropped instead of entering the store, because a frame that arrives late must never look like the current picture.

**Immediate invalidation on an assignment change.** The camera assignment identity is compared before and after every roster update. When a participant's camera stream changes, its frame is dropped and its answer cache is cleared at once, without waiting for the first JPEG of the new stream, so an answer from the old stream can never be served while the new stream is still arriving.

**The heartbeat contract.** Liveness is the progress of the sampling, not the arrival of the drain. The page reports the timestamp of the last sample it took for each stream, and the store refreshes `seen_at` only when that timestamp moves forward. A timestamp that stands still or moves backwards is not progress. A stream that keeps sampling keeps the frame live even when the picture never changes, a stream whose sampling timestamp stops moving goes unavailable after the liveness timeout, and a browser epoch resets the recorded timestamps so the new page clock starts clean.

**Files:**
- Modify: `src/fakeyou/perception/camera_evidence.py`
- Modify: `src/fakeyou/perception/interpreter.py`
- Modify: `src/fakeyou/perception/context.py`
- Test: `tests/test_camera_evidence.py`
- Move: `tests/test_camera_liveness.py` into `tests/test_camera_evidence.py`

### Task 1.1: Give the camera cache a generation token

- [ ] **Step 1: Write the failing test**

```python
async def test_an_inference_that_finishes_after_an_invalidation_is_not_published() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    interpreter = interpreter_with(model)
    frame = jpeg_of((120, 120, 120))
    started = asyncio.Event()
    release = asyncio.Event()
    original = interpreter.answer

    async def answered(image: bytes, question: str, *, source: str) -> str | None:
        started.set()
        await release.wait()
        return await original(image, question, source=source)

    object.__setattr__(interpreter, "answer", answered)

    task = asyncio.create_task(interpreter.camera_answer("p1", QUESTION, frame, source=SOURCE))
    await started.wait()
    interpreter.drop_camera_observation("p1")
    release.set()

    with pytest.raises(StaleEvidence):
        await task
    cache = object.__getattribute__(interpreter, "_camera_cache")
    assert cache.get("p1", QUESTION) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -q tests/test_camera_evidence.py -k invalidation`
Expected: FAIL, the cache entry is published after the invalidation.

- [ ] **Step 3: Add the snapshot identity to the frame store**

In `src/fakeyou/perception/timeline.py`:

```python
@dataclass(frozen=True, slots=True)
class CameraFrame:
    data: bytes
    stream_id: str
    captured_at: datetime
    epoch: int
    seen_at: float
    sequence: int = 0
    generation: int = 0


@dataclass(frozen=True, slots=True)
class CameraSnapshot:
    participant_id: str
    frame: CameraFrame
    store_epoch: int


class CameraFrameStore:
    def __init__(self, *, capacity: int = CAMERA_FRAME_CACHE_SIZE) -> None:
        self._capacity = capacity
        self._frames: OrderedDict[str, CameraFrame] = OrderedDict()
        self._sequences: dict[str, int] = {}
        self._generations: dict[str, int] = {}
        self._epoch = 0

    def put(self, participant_id: str, frame: CameraFrame) -> CameraFrame:
        sequence = self._sequences.get(participant_id, 0) + 1
        self._sequences[participant_id] = sequence
        stored = replace(
            frame,
            sequence=sequence,
            generation=self._generations.get(participant_id, 0),
        )
        self._frames[participant_id] = stored
        self._frames.move_to_end(participant_id)
        while len(self._frames) > self._capacity:
            self._frames.popitem(last=False)
        return stored

    def snapshot(self, participant_id: str) -> CameraSnapshot | None:
        frame = self.get(participant_id)
        if frame is None:
            return None
        return CameraSnapshot(participant_id=participant_id, frame=frame, store_epoch=self._epoch)

    def touch(self, participant_id: str, *, seen_at: float) -> None:
        frame = self._frames.get(participant_id)
        if frame is None:
            return
        self._frames[participant_id] = replace(frame, seen_at=seen_at)
        self._frames.move_to_end(participant_id)

    def drop(self, participant_id: str) -> None:
        self._generations[participant_id] = self._generations.get(participant_id, 0) + 1
        self._frames.pop(participant_id, None)

    def clear(self) -> None:
        self._epoch += 1
        self._frames.clear()
```

A heartbeat `touch` keeps the sequence, because the picture did not change. A new picture gets a new sequence, an invalidation gets a new generation.

- [ ] **Step 4: Reject a stale inference in the interpreter**

In `src/fakeyou/perception/interpreter.py`, inside `camera_answer`:

```python
            answer = await self.answer(snapshot.frame.data, question, source=source)
            if answer is None:
                return None
            if not self._snapshot_current(participant_id, snapshot):
                raise StaleEvidence
```

with

```python
    def _snapshot_current(self, participant_id: str, snapshot: CameraSnapshot) -> bool:
        current = self._snapshots.snapshot(participant_id)
        if current is None:
            return False
        return (
            current.store_epoch == snapshot.store_epoch
            and current.sequence == snapshot.sequence
            and current.generation == snapshot.generation
            and current.stream_id == snapshot.stream_id
            and current.epoch == snapshot.epoch
        )
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest -q tests/test_camera_evidence.py`
Expected: PASS.

### Task 1.2: Report a stale camera look as unavailable

- [ ] **Step 1: Write the failing test**

```python
async def test_a_stale_camera_look_reports_the_camera_as_unavailable() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    interpreter = interpreter_with(model)
    context = _context_with(interpreter)

    async def answered(image: bytes, question: str, *, source: str) -> str | None:
        interpreter.drop_camera_observation("device-p1")
        return await original(image, question, source=source)

    object.__setattr__(interpreter, "answer", answered)

    result = json.loads(await context.look_at_participant("Budi", "What color is his shirt?"))

    assert result["code"] == "camera_unavailable"
    assert result["resolution"] == "unavailable"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -q tests/test_camera_evidence.py -k unavailable`
Expected: FAIL, the stale error escapes the context.

- [ ] **Step 3: Catch the stale error and keep the boundary**

In `src/fakeyou/perception/context.py`:

```python
        try:
            async with asyncio.timeout(LOOK_TIMEOUT_SECONDS):
                evidence = await self.interpreter.camera_answer(
                    person.participant_id,
                    question,
                    snapshot,
                    source=CAMERA_SOURCE.format(name=person.name),
                )
        except TimeoutError:
            return _look_result(
                "look_failed",
                error_type="TimeoutError",
                participant=person.ref,
                resolution="unavailable",
                strategy="targeted_vision",
            )
        except (ProviderFault, FakeYouError, OSError) as error:
            return _look_result(
                "look_failed",
                error_type=type(error).__name__,
                participant=person.ref,
                resolution="unavailable",
                strategy="targeted_vision",
            )
        except StaleEvidence:
            return _look_result(
                "camera_unavailable",
                participant=person.ref,
                resolution="unavailable",
                strategy="targeted_vision",
            )
```

`TimeoutError` is caught before `OSError` because it is a subclass of it, and a cancellation is not caught at all, so it propagates.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest -q tests/test_camera_evidence.py tests/test_context.py`
Expected: PASS.

### Task 1.3: Move the liveness tests into a module-named file

- [ ] **Step 1: Move the file**

Move every test in `tests/test_camera_liveness.py` into `tests/test_camera_evidence.py`, renaming the helpers to `liveness_observation`, `liveness_jpeg`, `liveness_frame`, `liveness_people` and `CameraStack`, then delete the old file.

- [ ] **Step 2: Verify the naming rule passes**

Run: `uv run pytest -q tests/test_code_rules.py -k named_after`
Expected: PASS. `camera_evidence` is a module name, so the merged file satisfies the rule.

### Task 1.4: Reject a frame that was decoded across a state change

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_frame_from_the_previous_epoch_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = CameraStack()
    data = liveness_jpeg((120, 120, 120))
    await stack.setup_camera(data)
    task, release = await _frame_in_flight(stack, monkeypatch, data)

    stack.sensors.begin_epoch()
    release.set()
    await task

    assert stack.timeline.camera_frames.get(BUDI) is None
```

The same shape covers the camera-off, the participant leave and the stream reassignment.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_camera_evidence.py -k dropped`
Expected: FAIL, the late frame is written into the store.

- [ ] **Step 3: Re-validate the epoch and the assignment after the decode**

In `src/fakeyou/perception/sensors.py`:

```python
        epoch = self.epoch
        assignment = self.roster.assignment_for(frame.stream_id)
        ...
        device_id = assignment.device_id
        try:
            data = base64.b64decode(frame.jpeg_b64, validate=True)
            keyframe = await asyncio.to_thread(
                keyframe_from_jpeg, data, captured_at=page_time(frame.page_ms)
            )
        except (binascii.Error, OSError, ValueError) as error:
            self._reject("frame", error)
            return
        if self.epoch != epoch or not self._assignment_current(frame, device_id):
            self._journal.record(
                "perception_frame_dropped_stale",
                source="perception",
                stream_id=frame.stream_id,
                role=frame.role,
                epoch=epoch,
                current_epoch=self.epoch,
            )
            return
```

with

```python
    def _assignment_current(self, frame: DrainedFrame, device_id: str) -> bool:
        assignment = self.roster.assignment_for(frame.stream_id)
        if assignment is None or assignment.device_id != device_id:
            return False
        if assignment.role != frame.role:
            return False
        return device_id in self.roster.people()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_camera_evidence.py`
Expected: PASS.

### Task 1.5: Invalidate the frame and the answer cache on an assignment change

- [ ] **Step 1: Write the failing test**

```python
async def test_a_reassigned_camera_drops_the_frame_and_the_cached_answer() -> None:
    stack = CameraStack()
    data = liveness_jpeg((120, 120, 120))
    await stack.setup_camera(data)
    await stack.heartbeat()
    first = await stack.look()
    assert first["code"] == "ok"
    seen = len(stack.model.seen)

    rebound = collection_payload(
        users=[
            user_info(AYU, "Ayu"),
            user_info(BUDI, "Budi"),
            user_info(TWIN, "Twin", current=True),
        ],
        outputs=[output(BUDI, "stream-budi-2")],
    )
    await stack.sensors.accept_batch(
        DrainBatch(
            messages=[
                DrainedMessage(
                    channel="collections",
                    page_ms=JOINED_MS + 3000,
                    b64=base64.b64encode(rebound).decode(),
                )
            ]
        )
    )

    assert stack.timeline.camera_frames.get(BUDI) is None

    unavailable = await stack.look()
    assert unavailable["code"] == "camera_unavailable"
    assert len(stack.model.seen) == seen
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -q tests/test_camera_evidence.py -k reassigned_camera`
Expected: FAIL, the old frame and the cached answer survive the reassignment.

- [ ] **Step 3: Compare the camera assignments around every roster update**

In `src/fakeyou/perception/sensors.py`:

```python
def _camera_assignments(self) -> dict[str, str]:
    return {
        item.device_id: item.stream_id
        for item in self.roster.assignments()
        if item.role == "camera"
    }


def _invalidate_changed_assignments(self, before: Mapping[str, str]) -> None:
    after = self._camera_assignments()
    for device_id, stream_id in before.items():
        if after.get(device_id) == stream_id:
            continue
        self.timeline.camera_frames.drop(device_id)
        self._invalidate_camera(device_id)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest -q tests/test_camera_evidence.py`
Expected: PASS.

### Task 1.6: Make the ingestion check ABA safe

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_frame_decoded_across_a_camera_off_and_on_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None: ...


async def test_a_frame_decoded_across_a_stream_cycle_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None: ...
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_camera_evidence.py -k decoded_across`
Expected: FAIL, the late frame is accepted because the final assignment matches.

- [ ] **Step 3: Validate the monotonic generation after the decode**

```python
        revision = self.timeline.camera_frames.current_generation(device_id)
        try:
            ...
        if (
            self.epoch != epoch
            or self.timeline.camera_frames.current_generation(device_id) != revision
            or not self._assignment_current(frame, device_id)
        ):
            ...drop...
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_camera_evidence.py`
Expected: PASS.

### Task 1.7: Tie liveness to the sampling progress

- [ ] **Step 1: Write the failing test**

```python
async def test_a_stalled_sampling_stream_becomes_unavailable() -> None:
    stack = CameraStack()
    data = liveness_jpeg((30, 40, 50))
    await stack.setup_camera(data)
    await stack.heartbeat()
    assert (await stack.look())["code"] == "ok"

    for step in range(6):
        stack.clock[0] = step * 4.5
        await stack.heartbeat(advance=False)

    result = await stack.look()
    assert result["code"] == "camera_unavailable"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -q tests/test_camera_evidence.py -k stalled`
Expected: FAIL, the drain keeps the frame live.

- [ ] **Step 3: Report the last sampling timestamp**

In `src/fakeyou/assets/perception.js` the active samplers report the timestamp of the last sample they took, with no fallback to the current page time:

```javascript
    return [...samplers.values()].map((state) => ({
      stream_id: state.id,
      role: state.role,
      last_sample_page_ms: state.lastSamplePageMs,
    }));
```

In `src/fakeyou/perception/sensors.py` the model carries the same field and the liveness follows its progress:

```python
    def _sampling_advanced(self, sampler: ActiveSampler) -> bool:
        if sampler.last_sample_page_ms <= 0:
            return False
        previous = self._sampler_samples.get(sampler.stream_id)
        if previous is not None and sampler.last_sample_page_ms == previous:
            return False
        self._sampler_samples[sampler.stream_id] = sampler.last_sample_page_ms
        return True
```

`begin_epoch` clears the recorded timestamps so a new page clock starts clean.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest -q tests/test_camera_evidence.py tests/test_sensors.py`
Expected: PASS.

### Task 1.8: Commit the phase

- [ ] **Step 1: Run the gates**

Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run lint-imports && uv run deptry src && uv run pytest -q tests/test_camera_evidence.py tests/test_context.py tests/test_context_evidence.py tests/test_code_rules.py`
Expected: all green.

- [ ] **Step 2: Commit**

```bash
git add src tests
git commit -m "fix(perception): reject camera evidence from an invalidated stream"
```

## Phase 2 — Unified visual snapshot and evidence contracts

**Release condition:** camera and screen reach the conversation runtime through one domain interface, and every evidence payload carries its snapshot provenance.

**Status: implemented.** `CameraFrameStore` remains the single source of truth for the camera snapshot. The evidence contract lives in `src/fakeyou/perception/evidence.py` and every look, camera or screen, produces it.

### The coupling this phase removes

The phase was written against the code as it stood, not against the architecture in the diagram. Three responsibilities were duplicated and are now single.

| Duplication before | Where it hurt | Now |
| --- | --- | --- |
| The snapshot identity was copied field by field into the answer cache and compared field by field in two places. A new identity field would silently stop being compared. | `interpreter.py`, the cache entry | One `SnapshotRef` value object, produced only by the store, compared with one equality |
| The frame store and the answer cache had separate lifecycles, so every invalidation site had to remember to clear both. Four sites did it, and one of them could forget. | `sensors.py`, `orchestration/perception.py` | The store owns the invalidation and notifies the answers through one registered invalidator. One call clears both |
| The evidence a look produced was a hand built dictionary, projected again by hand into a journal event, so the provenance the model saw and the provenance the report read could drift apart. | `context.py`, `MeetingEvidence` | One typed `EvidenceResult`, projected once for the model and read once for the journal |

What the phase deliberately does **not** add: there is no new store, and there is no `EvidenceResolver` wrapper class. The interpreter already resolves evidence and a wrapper would only rename it. Adding one would have been the kind of refactor that looks like architecture and removes nothing.

### The contract

```python
@dataclass(frozen=True, slots=True)
class SnapshotRef:
    source: EvidenceSource
    subject_id: str
    stream_id: str
    epoch: int
    sequence: int
    generation: int
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class EvidenceResult:
    source: EvidenceSource
    subject_id: str
    snapshot: SnapshotRef | None
    acquisition: Acquisition
    status: EvidenceStatus
    verification: Verification
    strategy: EvidenceStrategy
    code: EvidenceCode
    answer: str | None
    observed_at: datetime
    age_ms: int = 0
    vision_latency_ms: int = 0
    error_type: str | None = None
```

`verification` is the separation the review asked for: `unverified` means the model produced the text, `verified` means something checked it against the frame. Nothing verifies a claim yet, so every result today is `unverified` and the field says so where the model and the report can both read it. The verdict, the reason and the timestamp travel with the answer.

The screen path uses the same contract. A screen has no stream lifecycle, so its `SnapshotRef` carries the keyframe digest as the identity and zero for the camera lifecycle fields. The camera fields only mean something for the camera.

### Files**

- Create: `src/fakeyou/perception/evidence.py`
- Modify: `src/fakeyou/perception/timeline.py`
- Modify: `src/fakeyou/perception/camera_evidence.py`
- Modify: `src/fakeyou/perception/interpreter.py`
- Modify: `src/fakeyou/perception/context.py`
- Modify: `src/fakeyou/orchestration/perception.py`
- Test: `tests/test_camera_evidence.py`
- Test: `tests/test_context_evidence.py`

### Task 2.1: Introduce the snapshot model

- [ ] **Step 1: Write the failing test**

```python
def test_a_snapshot_carries_its_stream_identity_and_generation() -> None:
    snapshot = VisualSnapshot(
        snapshot_id="person#1:0:stream-a",
        source="camera",
        subject_id="person#1",
        stream_id="stream-a",
        epoch=0,
        generation=3,
        captured_at=JOINED,
        data=b"jpeg",
    )

    assert snapshot.identity == ("person#1", "stream-a", 0)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -q tests/test_snapshots.py`
Expected: FAIL, `VisualSnapshot` is not defined.

- [ ] **Step 3: Create the module**

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Source = Literal["camera", "screen"]


@dataclass(frozen=True, slots=True)
class VisualSnapshot:
    snapshot_id: str
    source: Source
    subject_id: str
    stream_id: str
    epoch: int
    generation: int
    captured_at: datetime
    data: bytes

    @property
    def identity(self) -> tuple[str, str, int]:
        return (self.subject_id, self.stream_id, self.epoch)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest -q tests/test_snapshots.py`
Expected: PASS.

### Task 2.2: Introduce the evidence model

- [ ] **Step 1: Write the failing test**

```python
def test_an_inspection_result_reports_status_and_provenance() -> None:
    inspection = VisualInspection(
        status="supported", answer="Harun's shirt is red.", observed_facts=["red shirt"]
    )

    result = EvidenceResult(
        source="camera",
        subject_id="person#1",
        strategy="targeted_vision",
        acquisition="inference",
        status="supported",
        answer="Harun's shirt is red.",
        snapshot_id="person#1:0:stream-a",
        vision_latency_ms=1800,
    )

    assert result.status == "supported"
    assert inspection.observed_facts == ["red shirt"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -q tests/test_evidence.py`
Expected: FAIL, the models are not defined.

- [ ] **Step 3: Create the module**

```python
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict

Acquisition = Literal["reused", "inference"]
Status = Literal["supported", "insufficient", "unavailable"]
Strategy = Literal["targeted_vision", "structured_observation"]


class VisualInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["supported", "not_visible", "unclear"]
    answer: str | None = None
    observed_facts: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EvidenceResult:
    source: Literal["camera", "screen"]
    subject_id: str
    strategy: Strategy
    acquisition: Acquisition
    status: Status
    answer: str | None
    snapshot_id: str
    age_ms: int = 0
    vision_latency_ms: int = 0
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest -q tests/test_evidence.py`
Expected: PASS.

### Task 2.3: Route both sources through one resolver call

- [ ] **Step 1: Write the failing test**

```python
async def test_a_camera_question_and_a_screen_question_use_one_resolver() -> None:
    resolver = resolver_with(vision_model())

    camera = await resolver.resolve(
        question="What is he wearing?", source="camera", subject_id="person#1", turn_id="t1"
    )
    screen = await resolver.resolve(
        question="What is on the slide?", source="screen", subject_id="screen#1", turn_id="t2"
    )

    assert camera.source == "camera"
    assert screen.source == "screen"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -q tests/test_resolver.py`
Expected: FAIL, `EvidenceResolver` is not defined.

- [ ] **Step 3: Create the resolver**

```python
class EvidenceResolver:
    def __init__(
        self, store: EvidenceStore, inspector: VisionInspector, snapshots: SnapshotStore
    ) -> None:
        self._store = store
        self._inspector = inspector
        self._snapshots = snapshots

    async def resolve(
        self, *, question: str, source: Source, subject_id: str, turn_id: str
    ) -> EvidenceResult:
        snapshot = self._snapshots.current(source, subject_id)
        if snapshot is None:
            return EvidenceResult(
                source=source,
                subject_id=subject_id,
                strategy="targeted_vision",
                acquisition="inference",
                status="unavailable",
                answer=None,
                snapshot_id="",
            )
        reused = self._store.find(snapshot, question)
        if reused is not None:
            return reused
        inspection = await self._inspector.inspect(snapshot, question)
        result = EvidenceResult(
            source=source,
            subject_id=subject_id,
            strategy="targeted_vision",
            acquisition="inference",
            status="supported" if inspection.status == "supported" else "insufficient",
            answer=inspection.answer,
            snapshot_id=snapshot.snapshot_id,
            vision_latency_ms=0,
        )
        self._store.store(snapshot, question, result)
        return result
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest -q tests/test_resolver.py`
Expected: PASS.

### Task 2.5: Commit the phase

```bash
git add src tests
git commit -m "feat(perception): resolve camera and screen evidence through one interface"
```

---

## Phase 3 — Question-aware inspection without keyword routing

**Release condition:** no keyword routing exists on the critical path. Every camera question is answered by one inspection of the live frame, cached by snapshot identity and question identity, and the perception layer never guesses what a question means.

**Live implication of the strict snapshot check.** The camera is sampled every four seconds and a frame is only emitted when at least twelve of the 144 signature blocks changed, so the stored frame changes on substantial movement rather than on every sample. A question that lands while the participant moves on can therefore be rejected as stale, which is the intended behaviour: the answer describes the frame that was current when the inspection started, or it is not published at all. Phase 5 measures how often that happens.

**Status: implemented (camera path).** The classifier is gone. A camera question is sent to the vision model with the question itself and the participant's camera as the source, the answer is cached per participant, question and frame, and the perception layer no longer decides whether the evidence answers the question.

**Files:**
- Modify: `src/fakeyou/perception/camera_evidence.py`
- Modify: `src/fakeyou/perception/interpreter.py`
- Modify: `src/fakeyou/perception/context.py`
- Modify: `tests/_perception_fakes.py`
- Test: `tests/test_camera_evidence.py`

### Task 3.1: Send the question with the camera inspection

- [ ] **Step 1: Write the failing test**

```python
async def test_a_camera_question_is_answered_by_one_inspection_of_the_live_frame() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    context = live_context(model)

    result = json.loads(await context.look_at_participant("Budi", "What color is his shirt?"))

    assert result["code"] == "ok"
    assert result["answer"] == "A dark blue shirt."
    assert result["resolution"] == "fresh"
    assert result["strategy"] == "targeted_vision"
    assert len(model.seen) == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -q tests/test_camera_evidence.py -k inspection`
Expected: FAIL, the current code routes the question to the generic observation.

- [ ] **Step 3: Inspect the frame with the question**

In `src/fakeyou/perception/interpreter.py`:

```python
async def camera_answer(
    self,
    participant_id: str,
    question: str,
    image: bytes,
    *,
    source: str,
    captured_at: datetime | None = None,
) -> CameraEvidence | None:
    keyframe = await asyncio.to_thread(
        keyframe_from_jpeg, image, captured_at=captured_at or datetime.now(UTC)
    )
    async with self._camera_cache.lock_for(participant_id):
        generation = self._camera_cache.generation(participant_id)
        cached = self._camera_cache.get(participant_id, question)
        if cached is not None:
            age = self._monotonic() - cached.stored_at
            if age <= CAMERA_CACHE_TTL_SECONDS and observations_match(cached.keyframe, keyframe):
                return CameraEvidence(
                    answer=cached.answer,
                    resolution="cache_hit",
                    age_ms=round(age * 1000),
                    vision_latency_ms=0,
                    generation=generation.participant,
                )
        started = self._monotonic()
        answer = await self.answer(image, question, source=source)
        if answer is None:
            return None
        if self._camera_cache.generation(participant_id) != generation:
            raise StaleEvidence
        self._camera_cache.put(
            participant_id,
            CameraCacheEntry(
                question=question,
                answer=answer,
                keyframe=keyframe,
                stored_at=self._monotonic(),
            ),
        )
        return CameraEvidence(
            answer=answer,
            resolution="fresh",
            age_ms=0,
            vision_latency_ms=round((self._monotonic() - started) * 1000),
            generation=generation.participant,
        )
```

In `src/fakeyou/perception/context.py`:

```python
            evidence = await self.interpreter.camera_answer(
                person.participant_id,
                question,
                frame.data,
                source=CAMERA_SOURCE.format(name=person.name),
                captured_at=frame.captured_at,
            )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest -q tests/test_camera_evidence.py tests/test_context.py tests/test_context_evidence.py`
Expected: PASS.

### Task 3.2: Delete the classifier and the substring refusal check

- [ ] **Step 1: Delete the classifier**

Remove `question_need`, `observation_sufficient`, `CameraQuestionNeed`, the schema field literals, the strategy literals and every regex constant from `src/fakeyou/perception/camera_evidence.py`. Remove `_detail_not_visible` and the `insufficient_evidence` branch from `src/fakeyou/perception/context.py`. Remove `CameraObservation` and `CAMERA_OBSERVATION_PROMPT` from the perception package.

- [ ] **Step 2: Run the code rules**

Run: `uv run pytest -q tests/test_code_rules.py`
Expected: PASS. The Indonesian keywords and markers left with the classifier.

- [ ] **Step 3: Run the simulator**

Run: `docker run --rm -e SIMULATOR_PROVIDERS=replay -e SIMULATOR_ARTIFACT_DIR=/artifacts -v "$PWD/src:/workspace/src:ro" -v "$PWD/evals:/workspace/evals:ro" -v "$PWD/tests:/workspace/tests:ro" fakeyou-test:local uv run --no-sync pytest -m simulator evals/test_meeting_simulator_eval.py -q -p no:cacheprovider`
Expected: the camera scenario passes because the camera request matches the recorded cassette prompt.

### Task 3.3: Commit the phase

```bash
git add src tests
git commit -m "refactor(perception): answer camera questions with one question-aware inspection"
```

## Phase 3b-A — Offline verdict contract and evaluation harness

Runs after Phase 4 is merged. Define `VisualInspection.verdict` as `supported`,
`not_visible`, or `unclear`, separately from acquisition/status telemetry. Build a
controlled image dataset with expected answers and evidence annotations, independent
of the evaluated model. Deterministic scoring measures answer correctness, grounding,
false claims, and abstention; a model's `supported` verdict never supplies ground truth.

The standalone contract, synthetic dataset, fake-model tests, metric denominators,
limitations, and acquisition-activity semantics are described in
[`2026-11-14-offline-visual-evaluation.md`](2026-11-14-offline-visual-evaluation.md).
This work needs no provider credentials and changes no production prompt or cassette.

## Phase 3b-B — Provider integration

After credentials are available, integrate the verdict contract with an actual vision
model, validate provider structured output, measure parsing failures, and re-record
cassettes from valid provider interactions. Keep model verdicts separate from independent
evaluation labels and from `EvidenceResult.status` (`produced`, `insufficient`, `unavailable`).
Do not infer production vision quality from successful fake-model tests.

## Phase 4 — Canonical reporting architecture cleanup

**Release condition:** one report contract, one scorecard path, no version branch, and the historical benchmark kept as an artefact rather than as runtime logic.

**Release criteria.**

1. `report_contract.py` has one active evidence classification algorithm.
2. `scorecard.py` uses one canonical report contract with no v1 or v2 fallback.
3. No `evidence_resolution_v2` marker and no historical marker survives in a runtime path.
4. The baseline benchmark is stored with its commit, its configuration and its metric definitions, so a change in the numbers is never read as a performance improvement.

**Historical evaluation.** No legacy evaluator is built, not even as a fixture based one, because that still maintains the historical algorithm. Historical scorecard outputs are stored as immutable JSON artefacts, the historical input is kept when it is needed to reproduce them, and the commit sha and the environment lock are recorded with them. Recomputing a historical score means checking out the historical commit in isolation. Historical outputs are not golden tests for the new evaluator, because the metric semantics may change; what is tested is the truth of the current contract.

**Before a persisted format is removed**, a simple consumer inventory runs. If no compatibility obligation exists, it is removed directly. No migration framework is built.

**Status: implemented.** The inventory for this phase found one consumer of the report and the scorecard, the `GET /{meeting_id}/report` endpoint in `api/debrief.py`, and one consumer of the journal format, the report builder itself. The debrief report is a different schema and does not read the scorecard. The repository has no release tag and the package version is `0.1.0`, so no consumer has been promised a format. The two retired markers were therefore removed from the journal rows without a compatibility shim.

**What was removed.** `report_contract.py` keeps one classification algorithm, derived from the acquisition and the status of the evidence events. The legacy resolution based classifier, the `evidence_resolution_v2` marker and the scorecard fallback that returned the older target version for a journal without the evidence paths are gone. The scorecard applies the path gates unconditionally and reports an absent path as not applicable.

**What was added.** The report keeps the provenance the evidence contract produces: `evidence_provenance` counts the acquisition, the status and the verification of the evidence the turns used, next to `evidence_cache` and `evidence_unclassified`. A produced answer is never reported as a verified claim, and an acquisition of `not_attempted` is never counted as a vision call.

**Retry identity.** Deferred turns can gather evidence again under the same transcript ID. Each gather records `perception_evidence_started` with a new `attempt_id` before acquisition; its resolved events carry that ID. Classification uses the latest started attempt and requires one valid event per requested visual kind. Missing, duplicate, invalid or mismatched telemetry remains unclassified. Evidence from an earlier attempt cannot fill gaps in a retry, including a retry interrupted before any result was recorded.

**The baseline.** `docs/reports/2026-11-14-pre-phase4-benchmark.json` is an archived metric snapshot containing selected per-session values, the baseline commit, the environment lock, the configuration and historical evaluator definitions. It does not contain complete scorecards or per-session evaluator markers. The baseline commit used target contract version 3 and resolution-based classification; journals without the evidence-path marker could receive scorecard version 2. Phase 4 moves the current contract to version 4 without changing target values. Comparisons must account for these metric semantics before interpreting numeric differences as improvements.

**Scope decided after the review of the evidence contract.** Fakeyou is in development, so backward compatibility is not an automatic requirement. The legacy report evaluator, the `evidence_resolution_v2` branching and the journal compatibility markers are removed rather than maintained. Historical benchmark results are archived as static fixtures so a later change can be compared against them, and the existing tests are updated to the new contract.

**Files:**
- Modify: `src/fakeyou/observability/report_contract.py`
- Modify: `src/fakeyou/observability/journal.py`
- Modify: `src/fakeyou/observability/scorecard.py`
- Test: `tests/test_scorecard_contract.py`
- Archive: `docs/reports/`

**Not built:** a legacy journal adapter, a migration framework, or a compatibility shim for the old scorecard. If a consumer turns up that needs one, the boundary is defined then, not now.

### Task 4.1: Record the session contract once

- [ ] **Step 1: Write the failing test**

```python
def test_the_session_contract_is_recorded_once_at_session_start() -> None:
    journal = RecordingJournal()
    journal.record("session_started", source="cli", report_contract="v3")

    assert journal.events("session_started")[0]["report_contract"] == "v3"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -q tests/test_journal.py -k contract`
Expected: FAIL, no contract marker exists.

- [ ] **Step 3: Move the marker to the session record**

In `src/fakeyou/observability/journal.py`, drop the per-event `evidence_paths` and `evidence_resolution_v2` stamps and add a `report_contract` field that the session start record carries.

### Task 4.2: Split the current contract from the legacy adapter

- [ ] **Step 1: Write the failing test**

```python
def test_a_historical_journal_keeps_its_published_verdict() -> None:
    rows = archived_scorecards()
    report = legacy_session_report(rows)
    card = score_session(report)

    assert card["metrics"] == archived_scorecards()["metrics"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -q tests/test_scorecard_contract.py -k historical`
Expected: FAIL, the current contract reads the historical rows with the new semantics.

- [ ] **Step 3: Move the historical algorithm into the adapter**

Move `_evidence_resolutions`, `_path_of` and `_latency_by_path` into `src/fakeyou/observability/legacy_report.py`. The current contract keeps only `_evidence_observations` and `_classify`.

### Task 4.3: Commit the phase

```bash
git add src tests
git commit -m "refactor(observability): read every journal with one contract and one legacy adapter"
```

---

## Phase 4b — Measured limits of the current cache

**Known limit.** The answer cache holds one entry per participant and matches the question by exact string. A follow-up with the same wording on an unchanged frame is served from the cache; a paraphrase or a different question costs a new inspection. With the strict snapshot contract of Phase 1 the cache also requires the same frame sequence, so the hit rate is bounded by how often the picture stays identical.

**Release condition for this limit:** the hit rate is measured before any change. `evidence_cache.hit_rate` already exists in the report. Phase 5 records it for the benchmark conversations, and only then is it decided whether a question-similarity index or a per-subject fact store is worth it. Semantic matching is explicitly out of scope until the measurement exists.

## Phase 4c — The memory prefetch lifecycle (tracked separately)

**Finding.** `tests/test_persona_join.py::test_join_hung_recall_never_blocks_the_reply_and_keeps_listening` asserted exactly two memory timeout faults. The count is not a product invariant.

The memory prefetch runs as a background task. Each finalized transcript schedules one prefetch, and each prefetch search is bounded by `recall.timeout_seconds`. When the session stops, `RecallListener.close` cancels the prefetch that is still in flight. Whether that prefetch records a timeout fault or a cancellation therefore depends on whether the recall timeout fires before the teardown, which under load depends on the event loop's timer latency.

The product contract is the one the stop test already proves: a stop cancels the recall and no late reply follows. The fault count at teardown is a consequence of that contract, not a guarantee.

**Change.** The join test now asserts the invariant that matters, that at least one memory timeout was recorded and that every recorded memory fault is a timeout. Two deterministic contract tests in `tests/test_memory_prefetch.py` pin the behaviour: an expired prefetch records a memory timeout fault, and closing the listener cancels an in-flight prefetch before its timeout.

**Release condition:** the reply is never blocked by a hung recall, the search is always cancelled or timed out, and the memory outcome is always journalled. The fault record at teardown stays best effort by design.

## Phase 4d — The simulator cassette is timing brittle (tracked separately)

**Finding.** The first CI run of `d08283d` failed the Meeting simulator on `sim-s1` with a cassette miss: the turn judge request scored 0.72 against the recorded focus, below the 0.75 similarity threshold. The same commit passes when the same run is repeated, and it passes locally with the same image.

The sim-s1 cassette holds two recorded judge requests that differ only in whether the transcript was still partial. The replay matches on a word level similarity of the last twelve words or the whole focus. Any additional or shifted model call, for example the twin's proactive turn landing inside the scenario window, produces a focus that falls below the threshold and the scenario fails even though the behaviour is correct.

**Status.** Not caused by the perception change, but it makes the simulator gate unreliable, and a gate that fails for the wrong reason is worse than no gate.

**Release condition for this item:** the replay either records enough variants of a request to cover the legitimate orderings, or the similarity threshold is scoped per route so that a shifted-but-equivalent request is served rather than missed. Tracked apart from the perception work.

## Phase 5 — Live end-to-end correctness and cost benchmark

**Current milestone:** review the [Phase 5 Live E2E Measurement Plan](2026-11-14-live-e2e-measurement.md)
before implementation. It defines independent claim adjudication, publication-time
freshness, timing/accounting requirements, and proposed acceptance gates. The #194
experiment budget remains closed; paid execution requires a separately approved budget.
The earlier task outline below is not authorization to run a live benchmark.

**Release condition:** grounded answers, measured latency, measured vision calls and measured CPU from a real meeting, across general questions, follow-up questions, small objects, visual change, several participants, screen share, speaker change and failed streams.

**Files:**
- Create: `docs/plans/2026-11-12-evidence-benchmark.md`
- Create: `evals/test_evidence_benchmark_eval.py`
- Modify: `docs/plans/2026-11-11-rustfs-live-validation.md`

### Task 5.1: Define the benchmark conversations

- [ ] **Step 1: Write the scenario list**

Ten scenarios with a ground truth answer each, covering a general question, a follow-up on the same subject, a follow-up on a different subject, a small object, a visual change between two turns, two participants with one camera, a screen share question, a speaker change, a camera-off question and a stream failure.

### Task 5.2: Measure the evidence telemetry

- [ ] **Step 1: Add the benchmark harness**

The harness records, per turn: the evidence path, the acquisition, the vision latency, the answer and whether the answer matches the ground truth.

### Task 5.3: Run the live benchmark

- [ ] **Step 1: Run against the rebuilt stack**

Run: `docker compose --profile audio up -d --build` then the benchmark scenario runner.
Expected: every turn lands in exactly one evidence path, and the report states the vision call count per turn.

### Task 5.4: Commit the phase

```bash
git add docs evals
git commit -m "test(evals): measure grounded answers, latency and vision cost"
```

---

## Development stage policy

The review of the evidence contract settled a policy that changes how the remaining phases are built.

Backward compatibility is a product requirement, not an automatic engineering requirement. We keep compatibility because there are users, integrations or data to protect, not because deleting old code is frightening.

| Area | Decision |
| --- | --- |
| Legacy report evaluator v1 and v2 | Removed |
| `evidence_resolution_v2` branching | Removed |
| Legacy journal adapter | Not built |
| Old scorecard compatibility | Not required |
| Canonical evidence contract | One implementation that is current |
| Historical benchmark results | Kept as artefacts, not as runtime logic |
| Existing unit and integration tests | Updated to the new contract |
| Simulator cassettes | May be updated, with a correct replay validation |
| Schema versioning | Minimal where data is persisted or crosses a process |

Three things are never traded away: a breaking change may never hide a regression, the ability to compare quality before and after a change survives as static artefacts, and a public repository means persisted formats and public APIs are checked for consumers before they are removed.

## Risks carried into the next phase

These stay open after Phase 2 and are not closed by a green pipeline.

1. **Semantic correctness is unproven.** The vision model still returns free text. `code="ok"` means the model answered, not that the answer is supported by the frame. The structured verdict is Phase 3b and needs the simulator cassettes re-recorded with provider credentials.
2. **Liveness during an inference is decided, not yet proven.** The product decision is that evidence may describe what was visible when the frame was captured, but it may not be presented as a live condition without a liveness check at the moment it is used. The provenance timestamp is mandatory, a valid historical answer is not failed because someone moved, and the question "what is happening right now?" may not be answered from a stale frame as if it were live. The contract carries the capture time and the observation time; proving the rule is Phase 3b and the Live E2E benchmark.
3. **The simulator replay is not a strong oracle.** The simulator is green at HEAD, but the cassette matching is timing brittle and a scenario can fail for the wrong reason. That is recorded as Phase 4d.

## Self-review

**Spec coverage.** Every requirement of the redesign maps to a phase: generation-safe lifecycle is Phase 1, the four component boundaries are Phase 2, removal of keyword routing and substring refusal is Phase 3, removal of the version branch in reporting is Phase 4, and the measured benchmark is Phase 5.

**Placeholder scan.** No step contains "TBD", "add error handling" or "write tests for the above". Each task names its files, shows the code, and states the command and the expected result.

**Type consistency.** `VisualSnapshot.identity`, `VisualInspection.status`, `EvidenceResult.acquisition` and `EvidenceResult.status` are defined once in Phase 2 and used unchanged in Phase 3. `Generation` is defined in Phase 1 and consumed by the interpreter and the context.

**Known gap.** Phase 3 is implemented against the current interpreter instead of the final resolver, so Phase 2 must move the camera path into the resolver without changing the observable behaviour that Phase 3 established: one inspection per question, reuse only for the same question on the same frame, and no sufficiency judgement in the perception layer.

**Deliberate deferral.** The structured inspection verdict of Phase 3b is blocked by the simulator cassettes, which replay recorded free text for the camera prompt. Re-recording them needs provider credentials, so the camera inspection keeps returning the model's own words and the reply layer owns semantic sufficiency.
