# Rencana Implementasi Fase 1 Robustness (Butir 13 + 14)

> **Untuk pekerja agentic:** SUB-SKILL WAJIB: pakai subagent-driven-development untuk mengerjakan plan ini task-per-task. Step memakai sintaks checkbox (`- [ ]`) untuk pelacakan.

**Goal:** Slot tap yang macet sunyi terdeteksi dan disambung ulang otomatis dengan slot yang sama, dan sesi STT per stream tahan hening panjang via pre-roll onset, reconnect proaktif saat sepi, dan uji kesiapan eksplisit.

**Architecture:** Supervisor JS melacak jendela terakhir per slot dan membuka ulang slot macet tanpa ganti nomor slot, melaporkan health per slot; Python mencatat rescue dan health; prosesor pre-roll menyuntikkan ring 500 ms saat VAD mulai bicara; sesi audio reconnect proaktif saat idle melewati ambang dalam kondisi sepi dan memverifikasi kesiapan STT saat start.

**Tech Stack:** Python 3.12, JavaScript in-page (WebRTC standar saja), pytest + Playwright, ruff, mypy.

**Spec:** [long-term-architecture.md](long-term-architecture.md) butir 13 (L0) dan 14 (L2); rantai sebelumnya [phase-1-ordering.md](phase-1-ordering.md) (cabang basis `feat/phase-1-ordering`, belum merge).

## Global Constraints

- Python Inggris tanpa komentar/docstring dan tanpa string Bahasa Indonesia di `.py`; dokumentasi Bahasa Indonesia.
- Journal hanya metadata; tanpa teks transkrip, audio, prompt, atau secret.
- Nama event jurnal wajib akhiran partisip lampau terkendali (`EventVerb` di `taxonomy.py`) — pelajaran fixup `drain_gap_detected`.
- Tanpa dependency baru; tanpa protokol privat Meet dan tanpa Meet Media API; WebRTC standar saja.
- Flag `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED` default tetap mati; plan ini tidak mengubah flag.
- Tes merah sebelum implementasi; reviewer terpisah per task; tanpa push/merge.
- Semua gate lama dipertahankan: suite serial hijau, browser hijau, `ruff check`, `ruff format --check`, `mypy`; tanpa `xfail`; tanpa tambah latensi agent (pre-roll menyuntik audio yang sudah diterima, bukan menunda; reconnect proaktif hanya saat sepi).
- Verifikasi hanya offline + browser harness; tanpa sesi live di plan ini.
- Progres dicatat di `.runtime/phase-1-robustness/status.json` tiap task selesai.

---

## Scope Check

Sisa Fase 1 adalah tiga subsistem independen. Plan ini hanya mencakup robustness (butir 13 + 14: supervisor tap L0 + ketahanan STT L2). Butir 15 (tabel fusi identitas L4) dan 16 (endpointing semantik + pra-generate) masing-masing dapat plan tersendiri sesudah ini.

## File Structure

| File | Tanggung jawab |
| --- | --- |
| `src/fakeyou/assets/stream_capture.js` | Supervisor stall + rescue seslot + `health` di drain + hook uji (Task 1) |
| `src/fakeyou/stream_capture.py` | Parse `health`, jurnal rescue, health di heartbeat (Task 2) |
| `src/fakeyou/report.py` | Hitung `tap_slot_rescues` (Task 2) |
| `src/fakeyou/audio.py` | `PreRollBuffer`, idle STT, reconnect proaktif, `stt_ready` (Task 3–4) |
| `src/fakeyou/settings.py` | Knob `preroll_ms`, `idle_reconnect_seconds`, `ready_timeout_seconds` (Task 3–4) |
| `tests/test_stream_capture.py` | Tes Task 1–2 |
| `tests/test_report.py` | Tes hitung rescue (Task 2) |
| `tests/test_audio_preroll.py` (baru) | Tes pre-roll (Task 3) |
| `tests/test_audio_stt_reconnect.py` | Tes proaktif + readiness (Task 4) |
| `docs/reports/2026-10-06-per-stream-acceptance.md` | Adendum implementasi (Task 5) |

---

### Task 1: supervisor tap JS — stall, rescue, health

**Files:**

- Modify: `src/fakeyou/assets/stream_capture.js:8-14` (konstanta `STALL_MS`), `:15-31` (peta supervisor), `:80-106` (cap waktu jendela), `:338-361` (ekstrak `openSlot`), `:587-600` + `:601-656` (`supervise` + `health` di drain), `:577-585` + `:665-672` (hook `stallTestSlot` + ekspor)
- Test: `tests/test_stream_capture.py` (tambah tes browser)

**Interfaces:**

- Consumes: `receiversBySlot`, `readersBySlot`, `pendingSamples`, `monoNow`, `decodeFresh`, `releaseSlot`.
- Produces: `drain()` mengembalikan `health: [{slot, stalled, rescued, decode_fresh, engine}]` untuk SEMUA slot terpasang (termasuk yang tanpa jendela); slot tanpa jendela > `STALL_MS = 3000` dibuka ulang otomatis dengan nomor slot yang sama (counter `rescued` kumulatif per slot); hook uji `stallTestSlot(slot)` membatalkan reader agar pump berhenti.

- [ ] **Step 1: tulis tes merah**

```python
@pytest.mark.browser
async def test_stream_capture_supervisor_rescues_stalled_slot() -> None:
    script = load_capture_script()
    async with open_direct_page(headless=True) as page:
        await page.add_init_script(script)
        await page.goto("about:blank")
        await page.evaluate("() => window.__fakeyouStreamCapture.attachTestTones()")
        await asyncio.sleep(1.5)
        await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        await page.evaluate("() => window.__fakeyouStreamCapture.stallTestSlot(0)")
        await asyncio.sleep(3.5)
        rescued = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        await asyncio.sleep(0.8)
        recovered = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
    health = {entry["slot"]: entry for entry in rescued["health"]}
    assert health[0]["rescued"] == 1
    assert health[1]["rescued"] == 0
    assert {entry["slot"] for entry in recovered["streams"]} == {0, 1}
    assert all(entry["pcm_b64"] for entry in recovered["streams"])
```

- [ ] **Step 2: jalankan tes, pastikan merah**

Run: `pytest tests/test_stream_capture.py::test_stream_capture_supervisor_rescues_stalled_slot -v`
Expected: FAIL (`stallTestSlot` bukan fungsi / `health` KeyError)

- [ ] **Step 3: implementasi minimal**

Tambah konstanta `const STALL_MS = 3000;` setelah `ENCODED_FRESH_MS`, dan peta setelah `encodedAtBySlot`:

```js
const lastWindowAtBySlot = new Map();
const rescuedBySlot = new Map();
```

Di `pushSamples`, dalam `while (acc.length >= WINDOW_SAMPLES)` setelah `windows.push({...})`:

```js
      lastWindowAtBySlot.set(slot, monoNow());
```

Ekstrak badan `attachReceiver` menjadi `openSlot(slot, key, receiver)` (isi blok `try` yang ada: buat `el`/`processor`/`reader`, `keeps.push`, `el.play`, listener `ended`, `pumpSlot`); `attachReceiver` menjadi guard + `slotFor` + `receiversBySlot.set` + `pendingSamples.set` + `openSlot` + `lastWindowAtBySlot.set(slot, monoNow())`. Pada kegagalan `openSlot`, perilaku lama dipertahankan (`releaseSlot`).

Tambah fungsi supervisor sebelum `rescan()`:

```js
  function slotKey(slot) {
    for (const [key, found] of slots) {
      if (found === slot) return key;
    }
    return "";
  }

  function supervise() {
    const now = monoNow();
    for (const [slot, receiver] of receiversBySlot) {
      const last = lastWindowAtBySlot.get(slot);
      if (last !== undefined && now - last <= STALL_MS) continue;
      const key = slotKey(slot);
      if (!key || !receiver) continue;
      try {
        const reader = readersBySlot.get(slot);
        if (reader) {
          readersBySlot.delete(slot);
          reader.cancel().catch(() => {});
        }
      } catch {
        /* reader already gone */
      }
      pendingSamples.delete(slot);
      resamplers.delete(slot);
      try {
        pendingSamples.set(slot, []);
        openSlot(slot, key, receiver);
        lastWindowAtBySlot.set(slot, now);
        rescuedBySlot.set(slot, (rescuedBySlot.get(slot) || 0) + 1);
      } catch {
        /* rescue failed; next drain retries */
      }
    }
  }
```

Di `drain()`, setelah `rescan();` tambah `supervise();`. Bentuk `health` sebelum `return`:

```js
    const health = [...receiversBySlot.keys()]
      .sort((left, right) => left - right)
      .map((slot) => ({
        slot,
        stalled: monoNow() - (lastWindowAtBySlot.get(slot) || 0) > STALL_MS,
        rescued: rescuedBySlot.get(slot) || 0,
        decode_fresh: decodeFresh(slot),
        engine: decodeFresh(slot) ? "decode" : "mstp",
      }));
```

dan tambah `health` ke objek return. Tambah hook uji setelah `releaseTestTrack`:

```js
  function stallTestSlot(slot) {
    try {
      const reader = readersBySlot.get(slot);
      if (reader) reader.cancel().catch(() => {});
    } catch {
      /* test-only helper */
    }
  }
```

dan ekspor `stallTestSlot` di `window.__fakeyouStreamCapture`.

Catatan perilaku: slot yang di-rescue pada drain itu juga tidak punya jendela (kosong sejak macet) sehingga tidak muncul di `streams` drain itu, tetapi muncul di `health` dengan `rescued` naik; drain berikutnya mengalir lagi dengan nomor slot dan `seq` yang sama (kunci `seqBySlot` per slot tak tersentuh).

- [ ] **Step 4: jalankan tes, pastikan hijau**

Run: `pytest tests/test_stream_capture.py -q -p no:cacheprovider -k "browser or supervisor or seq or drain"`
Expected: PASS

- [ ] **Step 5: review reviewer terpisah, lalu commit**

```bash
git add src/fakeyou/assets/stream_capture.js tests/test_stream_capture.py
git commit -m "feat(l0): supervise taps and rescue stalled slots"
```

---

### Task 2: observabilitas supervisor di Python + report

**Files:**

- Modify: `src/fakeyou/stream_capture.py` (parse `health` lenien, jurnal rescue, health di heartbeat)
- Modify: `src/fakeyou/report.py` (`tap_slot_rescues` di `stt_quality`)
- Test: `tests/test_stream_capture.py`, `tests/test_report.py`

**Interfaces:**

- Consumes: `drain().health` (Task 1).
- Produces: `parse_health(payload) -> dict[int, TapHealth]` dengan `TapHealth(slot, stalled, rescued, decode_fresh, engine)`; entri non-mapping/field tak valid dilewati diam-diam (pola `parse_roster`); `CaptureDrainLoop` mengingat `rescued` terakhir per slot dan mencatat jurnal `tap_slot_rescued` (source `transport`, field `slot`, `rescued`, `stalled`) saat counter naik; dict slot di heartbeat `capture_polled` tambah kunci `stalled`, `rescued`, `decode_fresh`; `build_session_report` mengeluarkan `stt_quality.tap_slot_rescues: int`.

- [ ] **Step 1: tulis tes merah**

```python
def test_parse_health_skips_malformed_entries() -> None:
    payload = {
        "health": [
            {"slot": 0, "stalled": False, "rescued": 2, "decode_fresh": True, "engine": "decode"},
            {"slot": "x", "stalled": False, "rescued": 0, "decode_fresh": False, "engine": "mstp"},
            {"slot": 1},
            "junk",
        ]
    }
    assert parse_health(payload) == {
        0: TapHealth(slot=0, stalled=False, rescued=2, decode_fresh=True, engine="decode")
    }
    assert parse_health({}) == {}


async def test_drain_loop_journals_slot_rescue_once() -> None:
    from fakeyou.stream_capture import CaptureDrainLoop

    def payload(rescued: int) -> dict:
        return {
            "streams": [],
            "roster": [],
            "dropped": 0,
            "health": [
                {
                    "slot": 0,
                    "stalled": True,
                    "rescued": rescued,
                    "decode_fresh": False,
                    "engine": "mstp",
                }
            ],
        }

    journal = Journal(emit=collect_lines(lines))
    registry, _ = _recording_registry(FakeClock().monotonic)
    page = meet_page([], evaluates=[payload(1), payload(1), payload(2)])
    loop = CaptureDrainLoop(page, registry, journal=journal, heartbeat_polls=1)
    assert await loop.poll() is True
    assert await loop.poll() is True
    assert await loop.poll() is True
    rescues = [row for line in lines if (row := json.loads(line))["event"] == "tap_slot_rescued"]
    assert [(row["slot"], row["rescued"]) for row in rescues] == [(0, 1), (0, 2)]
```

(pola `Journal(emit=...)` + `meet_page` + `_recording_registry` mengikuti helper yang sudah ada di berkas tes; sesuaikan nama dengan helper aktual.)

Tes report:

```python
def test_session_report_counts_tap_slot_rescues() -> None:
    rows = [
        {"event": "tap_slot_rescued", "slot": 0, "rescued": 1},
        {"event": "tap_slot_rescued", "slot": 0, "rescued": 2},
    ]
    assert build_session_report(rows)["stt_quality"]["tap_slot_rescues"] == 2
```

- [ ] **Step 2: jalankan tes, pastikan merah**

Run: `pytest tests/test_stream_capture.py::test_parse_health_skips_malformed_entries tests/test_stream_capture.py::test_drain_loop_journals_slot_rescue_once tests/test_report.py::test_session_report_counts_tap_slot_rescues -v`
Expected: FAIL (`parse_health`/`TapHealth`/`tap_slot_rescued`/`tap_slot_rescues` belum ada)

- [ ] **Step 3: implementasi minimal**

```python
@dataclass(frozen=True)
class TapHealth:
    slot: int
    stalled: bool
    rescued: int
    decode_fresh: bool
    engine: str


def parse_health(payload: Mapping[str, Any]) -> dict[int, TapHealth]:
    if not isinstance(payload, Mapping):
        return {}
    raw = payload.get("health")
    if not isinstance(raw, list):
        return {}
    found: dict[int, TapHealth] = {}
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        slot = entry.get("slot")
        stalled = entry.get("stalled")
        rescued = entry.get("rescued")
        fresh = entry.get("decode_fresh")
        engine = entry.get("engine")
        if (
            isinstance(slot, bool)
            or not isinstance(slot, int)
            or slot < 0
            or not isinstance(stalled, bool)
            or isinstance(rescued, bool)
            or not isinstance(rescued, int)
            or rescued < 0
            or not isinstance(fresh, bool)
            or not isinstance(engine, str)
        ):
            continue
        found[slot] = TapHealth(
            slot=slot, stalled=stalled, rescued=rescued, decode_fresh=fresh, engine=engine
        )
    return found
```

`CaptureDrainLoop.__init__` tambah `self._rescued_seen: dict[int, int] = {}`. Di `poll()`, setelah `parse_drain` berhasil (tirukan alur sukses yang ada), tambah:

```python
        for slot, health in parse_health(batch).items():
            if health.rescued > self._rescued_seen.get(slot, 0):
                self._rescued_seen[slot] = health.rescued
                if self._journal is not None:
                    self._journal.record(
                        "tap_slot_rescued",
                        source="transport",
                        slot=slot,
                        rescued=health.rescued,
                        stalled=health.stalled,
                    )
```

`_heartbeat`: bangun `health = parse_health(batch)` sekali, dan tiap dict slot tambah:

```python
                "stalled": health.get(chunk.slot).stalled if chunk.slot in health else False,
                "rescued": health.get(chunk.slot).rescued if chunk.slot in health else 0,
                "decode_fresh": health.get(chunk.slot).decode_fresh if chunk.slot in health else False,
```

`report.py`: tambah `tap_slot_rescues = 0`, cabang `elif event == "tap_slot_rescued": tap_slot_rescues += 1`, output `"tap_slot_rescues": tap_slot_rescues` di `stt_quality`.

- [ ] **Step 4: jalankan tes, pastikan hijau**

Run: `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false pytest tests/test_stream_capture.py tests/test_report.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: review reviewer terpisah, lalu commit**

```bash
git add src/fakeyou/stream_capture.py src/fakeyou/report.py tests/test_stream_capture.py tests/test_report.py
git commit -m "feat(l0): observe tap supervisor rescues"
```

---

### Task 3: pre-roll onset 500 ms

**Files:**

- Modify: `src/fakeyou/audio.py` (kelas `PreRollBuffer` + wiring di `open_stream_audio` saja)
- Modify: `src/fakeyou/settings.py` (`STTSettings.preroll_ms: float = 500.0`)
- Test: `tests/test_audio_preroll.py` (baru)

**Interfaces:**

- Consumes: `AudioRawFrame(audio: bytes, sample_rate: int, num_channels: int)`, `VADUserStartedSpeakingFrame`, `VADUserStoppedSpeakingFrame` (pipecat; impor yang sudah ada di `audio.py`).
- Produces: `PreRollBuffer(FrameProcessor)` dengan `__init__(sample_rate, *, preroll_ms = 500.0, channels = 1)`; saat idle menyimpan audio sampai `preroll_ms`; saat bingkai mulai-bicara lewat, mendorong isi ring sebagai `AudioRawFrame` gabungan DAHULU lalu bingkai itu; passthrough saat bicara; kembali menyimpan saat berhenti-bicara. `open_stream_audio` memasangnya di antara `_CaptureObserver` dan `stt` dengan `preroll_ms=stt_settings.preroll_ms`. Jalur mixed (`open_meeting_audio`) TIDAK diubah (beku warisan).

- [ ] **Step 1: tulis tes merah**

```python
import asyncio

from pipecat.frames.frames import AudioRawFrame, EndFrame
from pipecat.processors.frame_processor import FrameDirection

from fakeyou.audio import PreRollBuffer


class _Sink(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.frames: list = []

    async def process_frame(self, frame, direction) -> None:
        await super().process_frame(frame, direction)
        self.frames.append(frame)


def _audio(byte: int, count: int = 320) -> AudioRawFrame:
    return AudioRawFrame(audio=bytes([byte]) * count, sample_rate=16000, num_channels=1)


async def test_preroll_replays_ring_before_speech_start() -> None:
    from pipecat.audio.vad.vad_analyzer import (
        VADUserStartedSpeakingFrame,
        VADUserStoppedSpeakingFrame,
    )

    buffer = PreRollBuffer(sample_rate=16000, preroll_ms=40.0)
    sink = _Sink()
    buffer.next(sink)
    for index in range(4):
        await buffer.process_frame(_audio(index), FrameDirection.DOWNSTREAM)
    await buffer.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await buffer.process_frame(_audio(9), FrameDirection.DOWNSTREAM)
    await buffer.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    kinds = [type(frame).__name__ for frame in sink.frames]
    assert kinds[0] == "AudioRawFrame"
    assert sink.frames[0].audio == b"".join(bytes([index]) * 320 for index in range(4))
    assert kinds[1] == "VADUserStartedSpeakingFrame"
    assert any(
        isinstance(frame, AudioRawFrame) and frame.audio == bytes([9]) * 320
        for frame in sink.frames
    )


async def test_preroll_ring_caps_at_preroll_ms() -> None:
    buffer = PreRollBuffer(sample_rate=16000, preroll_ms=40.0)
    sink = _Sink()
    buffer.next(sink)
    for index in range(10):
        await buffer.process_frame(_audio(index), FrameDirection.DOWNSTREAM)
    assert buffer.buffered_ms <= 40.0
```

(`buffered_ms` properti baca-saja untuk observasi tes. `FrameProcessor.next`/`process_frame` mengikuti API pipecat yang dipakai `_CaptureObserver`; sesuaikan dengan pola pemakaian prosesor di kode produksi.)

- [ ] **Step 2: jalankan tes, pastikan merah**

Run: `pytest tests/test_audio_preroll.py -v`
Expected: FAIL (`PreRollBuffer` belum ada)

- [ ] **Step 3: implementasi minimal**

```python
class PreRollBuffer(FrameProcessor):
    def __init__(self, sample_rate: int, *, preroll_ms: float = 500.0, channels: int = 1) -> None:
        super().__init__()
        self._sample_rate = sample_rate
        self._channels = max(channels, 1)
        self._limit_bytes = max(int(sample_rate * 2 * self._channels * preroll_ms / 1000.0), 0)
        self._ring: deque[bytes] = deque()
        self._ring_bytes = 0
        self._speaking = False

    @property
    def buffered_ms(self) -> float:
        width = self._sample_rate * 2 * self._channels
        return (self._ring_bytes / width * 1000.0) if width else 0.0

    async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._speaking = True
            if self._ring_bytes:
                await self.push_frame(
                    AudioRawFrame(
                        audio=b"".join(self._ring),
                        sample_rate=self._sample_rate,
                        num_channels=self._channels,
                    ),
                    direction,
                )
                self._ring.clear()
                self._ring_bytes = 0
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._speaking = False
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, AudioRawFrame) and not self._speaking:
            if frame.audio:
                self._ring.append(frame.audio)
                self._ring_bytes += len(frame.audio)
                while self._ring_bytes > self._limit_bytes and self._ring:
                    self._ring_bytes -= len(self._ring.popleft())
            await self.push_frame(frame, direction)
            return
        await self.push_frame(frame, direction)
```

(PENTING: audio idle tetap diteruskan — ring hanya salinan; STT hilir tetap menerima semua audio seperti sebelumnya. Nol perubahan perilaku selain injeksi onset.)

Wiring di `open_stream_audio`: sisipkan `PreRollBuffer(CAPTURE_SAMPLE_RATE, preroll_ms=stt_settings.preroll_ms)` di antara `_CaptureObserver(...)` dan `stt` dalam daftar `Pipeline([...])`.

`settings.py` `STTSettings` tambah `preroll_ms: float = 500.0`.

- [ ] **Step 4: jalankan tes, pastikan hijau**

Run: `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false pytest tests/test_audio_preroll.py tests/test_audio_stt_reconnect.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: review reviewer terpisah, lalu commit**

```bash
git add src/fakeyou/audio.py src/fakeyou/settings.py tests/test_audio_preroll.py
git commit -m "feat(l2): replay pre-roll audio on speech start"
```

---

### Task 4: reconnect proaktif + uji kesiapan STT

**Files:**

- Modify: `src/fakeyou/audio.py` (`ManagedMuseSTT` idle, helper `_quiet`, cabang proaktif di `maintenance_tick`, `stt_ready` + wiring start, param sesi)
- Modify: `src/fakeyou/settings.py` (`STTSettings.idle_reconnect_seconds`, `ready_timeout_seconds`)
- Test: `tests/test_audio_stt_reconnect.py`

**Interfaces:**

- Consumes: `maintenance_tick`, `request_stt_reconnect`, `stt_socket_dead`, `STTSettings`.
- Produces: `ManagedMuseSTT.idle_seconds(now: float) -> float | None` (`None` bila belum pernah terima audio; cap waktu diperbarui di `run_stt` saat `audio` tak kosong); `MeetingAudioSession(..., stt_idle_reconnect_seconds: float = 0.0)` — bila > 0 dan idle melewati ambang dalam kondisi sepi (syarat sepi yang sama persis dengan jalur antre) dan state `idle`, panggil `request_stt_reconnect("proactive_idle")`; `stt_ready(stt, *, timeout_seconds: float) -> bool` (duck-typed `_session_ready`; True langsung bila atribut tak ada); `open_stream_audio` memverifikasi kesiapan setelah pipeline start: sukses → jurnal `stt_session_ready` (source `stt`), gagal → jurnal `stt_session_not_ready` + `MeetingAudioError("STT session did not become ready")`. `STTSettings.idle_reconnect_seconds: float = 0.0` (mati default; dinyalakan dari data live), `ready_timeout_seconds: float = 10.0`.

- [ ] **Step 1: tulis tes merah**

```python
async def test_proactive_reconnect_requests_only_when_idle_and_quiet() -> None:
    from tests.fakes import FakeReconnectableSTT

    stt = FakeReconnectableSTT()
    session = _quiet_session(stt, stt_idle_reconnect_seconds=60.0, idle_seconds=61.0)
    await session.maintenance_tick(is_busy=False, remote_participants=2)
    assert session.reconnect_state() in ("queued", "idle")
    assert stt.calls >= 1


async def test_proactive_reconnect_stays_off_by_default() -> None:
    from tests.fakes import FakeReconnectableSTT

    stt = FakeReconnectableSTT()
    session = _quiet_session(stt, stt_idle_reconnect_seconds=0.0, idle_seconds=3600.0)
    await session.maintenance_tick(is_busy=False, remote_participants=2)
    assert stt.calls == 0


async def test_stt_ready_reports_duck_typed_session() -> None:
    assert await stt_ready(object(), timeout_seconds=0.1) is True
    ready = asyncio.Event()
    assert await stt_ready(_ReadySTT(ready), timeout_seconds=0.1) is False
    ready.set()
    assert await stt_ready(_ReadySTT(ready), timeout_seconds=0.1) is True
```

(`_quiet_session` helper lokal: `MeetingAudioSession` dengan worker/runner/engine tiruan mengikuti pola konstruksi sesi di tes yang ada, `CaptureActivity` sepi (tanpa speech 60 dtk+), STT tiruan yang mengembalikan idle terkendali — `FakeReconnectableSTT` perlu metode `idle_seconds` ATAU sesi membaca via `getattr` duck-typed; pilih duck-typed `getattr(stt, "idle_seconds", None)` agar STT warisan (tanpa idle) dilewati aman. `_ReadySTT` kelas lokal dengan atribut `_session_ready`.)

- [ ] **Step 2: jalankan tes, pastikan merah**

Run: `pytest tests/test_audio_stt_reconnect.py::test_proactive_reconnect_requests_only_when_idle_and_quiet tests/test_audio_stt_reconnect.py::test_proactive_reconnect_stays_off_by_default tests/test_audio_stt_reconnect.py::test_stt_ready_reports_duck_typed_session -v`
Expected: FAIL (`idle_seconds`/`stt_ready`/param sesi belum ada)

- [ ] **Step 3: implementasi minimal**

`ManagedMuseSTT.__init__` tambah `self._last_audio_at: float | None = None`. Di `run_stt` paling awal tambah:

```python
        if audio:
            self._last_audio_at = time.monotonic()
```

Tambah metode:

```python
    def idle_seconds(self, now: float) -> float | None:
        if self._last_audio_at is None:
            return None
        return max(now - self._last_audio_at, 0.0)
```

(`time` sudah diimpor di `audio.py`; verifikasi.)

`MeetingAudioSession.__init__` tambah param `stt_idle_reconnect_seconds: float = 0.0`, simpan. Ekstrak syarat sepi dari `maintenance_tick` menjadi helper:

```python
    def _quiet_for_reconnect(self, stamp: float, is_busy: bool) -> bool:
        snapshot = self.activity.snapshot()
        quiet_since = (
            snapshot.last_speech_at if snapshot.last_speech_at is not None else snapshot.ready_at
        )
        return not (
            is_busy
            or self.is_speaking
            or snapshot.speech_active
            or quiet_since is None
            or stamp - quiet_since < 3
        )
```

dan ganti blok syarat sepi di `maintenance_tick` dengan `if not self._quiet_for_reconnect(stamp, is_busy): return verdict`. Sebelum `if self._reconnect_state != "queued": return verdict`, sisipkan:

```python
            if (
                self._reconnect_state == "idle"
                and self._stt_idle_reconnect_seconds > 0
                and not stt_socket_dead(self._stt)
                and self._quiet_for_reconnect(stamp, is_busy)
            ):
                idle_of = getattr(self._stt, "idle_seconds", None)
                idle = idle_of(stamp) if callable(idle_of) else None
                if idle is not None and idle >= self._stt_idle_reconnect_seconds:
                    self.request_stt_reconnect("proactive_idle")
```

(Aliran antre yang ada lalu mengeksekusinya; tanpa jalur eksekusi baru.)

Helper kesiapan di samping `stt_socket_dead`:

```python
async def stt_ready(stt: object, *, timeout_seconds: float) -> bool:
    ready = getattr(stt, "_session_ready", None)
    if ready is None:
        return True
    try:
        async with asyncio.timeout(timeout_seconds):
            await ready.wait()
    except TimeoutError:
        return False
    return True
```

Di `open_stream_audio` setelah blok start (`session.raise_if_failed()` pasca-start, sebelum `yield session`):

```python
        if not await stt_ready(stt, timeout_seconds=stt_settings.ready_timeout_seconds):
            if journal is not None:
                journal.record("stt_session_not_ready", source="stt", stream_id=stream_id)
            raise MeetingAudioError("STT session did not become ready")
        if journal is not None:
            journal.record("stt_session_ready", source="stt", stream_id=stream_id)
```

`open_stream_audio` teruskan `stt_idle_reconnect_seconds=stt_settings.idle_reconnect_seconds` ke konstruktor `MeetingAudioSession`. `settings.py` `STTSettings` tambah `idle_reconnect_seconds: float = 0.0` dan `ready_timeout_seconds: float = 10.0`.

- [ ] **Step 4: jalankan tes, pastikan hijau**

Run: `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false pytest tests/test_audio_stt_reconnect.py tests/test_audio_preroll.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: review reviewer terpisah, lalu commit**

```bash
git add src/fakeyou/audio.py src/fakeyou/settings.py tests/test_audio_stt_reconnect.py
git commit -m "feat(l2): proactive STT reconnect and readiness check"
```

---

### Task 5: gate penuh + adendum + status

**Files:**

- Modify: `docs/reports/2026-10-06-per-stream-acceptance.md` (adendum implementasi)
- Modify: `.runtime/phase-1-robustness/status.json` (final)
- Test: tanpa tes baru; verifikasi gate penuh.

**Interfaces:**

- Consumes: Task 1–4.
- Produces: adendum laporan + status final + cabang siap review merge (tanpa push/merge).

- [ ] **Step 1: gate serial penuh**

Run: `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false pytest tests/ -q -p no:cacheprovider`
Expected: exit 0, 0 failed (catat angka passed/skipped/deselected aktual)

- [ ] **Step 2: gate statis + browser**

Run: `ruff check .`, `ruff format --check .`, `mypy src`, browser `-m "browser"` (catat output aktual)
Expected: bersih semua

- [ ] **Step 3: tulis adendum + status, commit**

Adendum: tabel butir 13+14, angka gate aktual, penegasan jalur live tak berubah + default knob aman (proaktif mati sampai data live). Status: task, status tiap task, command + exit code, artefak, gate terbuka sisa (butir 15–16).

```bash
git add docs/reports/2026-10-06-per-stream-acceptance.md .runtime/phase-1-robustness/status.json
git commit -m "docs(phase-1): robustness implementation addendum and status"
```

(Catatan: `.runtime` di-ignore git; status.json pelacakan lokal saja — jangan `git add` bila ditolak.)

---

## Self-Review

1. **Cakupan spec:** butir 13 → Task 1 (supervisor + rescue seslot + health) + Task 2 (jurnal rescue + heartbeat + report); butir 14 → Task 3 (pre-roll) + Task 4 (reconnect proaktif + readiness). Keepalive tercakup sebagai reconnect proaktif saat sepi (bukan injeksi audio berbayar). Sinyal kesehatan kontinu `tap_healthy/decode_fresh/fallback_active` tercakup sebagai `health` per slot + `decode_fresh` + `engine`.
2. **Pindai placeholder:** tiap step berisi kode/perintah aktual; helper tes dirujuk ke pola helper yang sudah ada di berkas tes masing-masing.
3. **Konsistensi tipe:** `health[].rescued: int` JS→`TapHealth.rescued: int`→jurnal `rescued: int`→report `tap_slot_rescues: int`; `preroll_ms/idle_reconnect_seconds/ready_timeout_seconds: float` settings→audio; `idle_seconds(now) -> float | None` dipakai konsisten via duck-typed `getattr`.
