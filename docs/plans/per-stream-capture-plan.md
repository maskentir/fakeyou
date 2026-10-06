# Rencana implementasi capture per stream

> **Untuk pekerja agen:** SUB-SKILL WAJIB: gunakan superpowers:subagent-driven-development (disarankan) atau superpowers:executing-plans untuk mengerjakan plan ini per task. Langkah memakai sintaks checkbox (`- [ ]`) untuk pelacakan.

**Tujuan:** Menangkap tiap receiver Meet secara terpisah, mentranskrip tiap stream pada stream STT Muse sendiri, dan memancarkan transkrip beridentitas yang digabung kronologis.

**Arsitektur:** JS dalam halaman menangkap PCM 16 kHz mono per receiver dengan stamp `{slot, csrcs, level, page_ms}`; satu pipeline pipecat per stream (input dorong, VAD Silero, STT Muse) memancarkan transkrip bertag stream; conversation menggabung menurut jam mono dan me-resolve nama lewat binder yang sudah ada.

**Tech stack:** WebRTC Chromium + MediaStreamTrackProcessor (JS), pipeline pipecat (Python), WebSocket STT Muse, uji browser Playwright.

**Spec:** `docs/plans/per-stream-capture.md` — desain, gate, dan peta file yang disetujui; pelaksana membaca keduanya.

## Batasan global

- Python Inggris, tanpa komentar/docstring, tanpa string Bahasa Indonesia di `.py`; dokumentasi Bahasa Indonesia.
- Journal hanya metadata (`_ms`, kosakata `taxonomy.py`); tanpa teks transkrip, audio, prompt, atau secret.
- Capture 16 kHz mono per stream; playback 24 kHz tidak berubah.
- Flag `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED` default mati sampai acceptance lulus; saat mati perilaku identik mixed.
- Tiap peran memakai key sendiri; tidak ada pinjaman key.
- Sesi live hanya di container worker; jangan pernah di browser Mac. Backup `.env` sebelum mengubahnya.
- Fixture baru disamarkan (Host/Guest/Twin).
- Tes merah sebelum implementasi; reviewer terpisah per task; tanpa push/merge.
- Tanpa dependency baru; tanpa protokol privat Meet; tanpa Meet Media API.
- Semua gate yang sudah hijau dipertahankan, termasuk replay probe-attr-8 dan csrc-live-3; tanpa `xfail`.

---

## Struktur file

- Buat `src/fakeyou/assets/stream_capture.js` — capture PCM per receiver, stamp, buffer drain. Kode browser murni, tanpa import.
- Buat `src/fakeyou/stream_capture.py` — `StreamAudioInput`, `StreamSession`, `StreamRegistry`, parsing drain. Pemilik pipeline per stream.
- Ubah `src/fakeyou/audio.py` — pakai ulang bentuk `ManagedMuseSTT` dan `_TranscriptObserver` untuk pipeline per stream; tambah factory `open_stream_audio` di sebelah `open_meeting_audio`.
- Ubah `src/fakeyou/conversation.py` — `TranscriptEvent.stream_id`, merge menurut timestamp.
- Ubah `src/fakeyou/voice_activity.py` — ikat identitas stream slot/CSRC ke participant.
- Ubah `src/fakeyou/settings.py` — `per_stream_enabled: bool = False` pada conversation settings.
- Ubah `src/fakeyou/cli/join.py` — wiring jalur per stream di balik flag.
- Buat `tests/test_stream_capture.py` — uji unit offline plus uji browser Playwright pada halaman loopback.
- Buat `tests/fixtures/per-stream-*.json` — fixture replay yang disamarkan.

## Task 1: Capture per receiver dalam halaman

**File:**
- Buat: `src/fakeyou/assets/stream_capture.js`
- Uji: `tests/test_stream_capture.py`

**Antarmuka:**
- Konsumsi: tidak ada (kode browser murni).
- Produksi: `window.__fakeyouStreamCapture` dengan `drain() -> {streams: [{slot, csrcs, level, page_ms, pcm_b64}], roster: [...], dropped: int}` dan `pending() -> int`.

- [ ] **Langkah 1: Tulis uji browser yang gagal**

```python
@pytest.mark.browser
async def test_stream_capture_separates_two_tones() -> None:
    from importlib.resources import files

    script = (files("fakeyou") / "assets" / "stream_capture.js").read_text(encoding="utf-8")
    async with open_direct_page(headless=True) as page:
        await page.add_init_script(script)
        await page.goto("about:blank")
        await page.evaluate("() => window.__fakeyouStreamCapture.attachTestTones()")
        await asyncio.sleep(2.0)
        drained = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
    assert len(drained["streams"]) == 2
    assert drained["streams"][0]["pcm_b64"]
    assert drained["streams"][0]["slot"] != drained["streams"][1]["slot"]
```

- [ ] **Langkah 2: Jalankan uji untuk memastikan gagal**

Jalankan: `.venv/bin/python -m pytest tests/test_stream_capture.py::test_stream_capture_separates_two_tones -v`
Hasil: FAIL dengan `FileNotFoundError` (aset `stream_capture.js` belum ada).

- [ ] **Langkah 3: Tulis `stream_capture.js` minimal**

Syarat utama (terbukti di spike): bungkus receiver saat `ontrack`,
tempel tiap track ke elemen `<audio>` yang `play()`, simpan referensi
node pada `window`, AudioWorklet PCM 16 kHz mono, stamp
`{slot, csrcs, level, page_ms}` per jendela 50 ms, buffer drain
berbatas dengan counter `dropped`. Sertakan `attachTestTones()`
(dua oscillator 440/880 Hz lewat pasangan RTCPeerConnection
loopback) hanya untuk uji.

- [ ] **Langkah 4: Jalankan uji untuk memastikan lulus**

Jalankan: `.venv/bin/python -m pytest tests/test_stream_capture.py -v -m browser`
Hasil: PASS.

- [ ] **Langkah 5: Verifikasi separasi, bukan sekadar keberadaan**

Perluas uji: bisukan nada A lewat `setTestTone(0, 0.0)` dan assert
RMS PCM stream 0 turun sementara stream 1 tetap. Helper RMS
mendecode int16 mono `pcm_b64` dan menghitung peak per jendela.

- [ ] **Langkah 6: Jalankan gate statis**

Jalankan: `.venv/bin/ruff check tests/test_stream_capture.py`
Hasil: All checks passed.

- [ ] **Langkah 7: Commit**

```bash
git add src/fakeyou/assets/stream_capture.js tests/test_stream_capture.py
git commit -m "feat(stream): in-page per-receiver PCM capture with stamps"
```

## Task 2: Pipeline per stream dengan input PCM dorong

**File:**
- Buat: `src/fakeyou/stream_capture.py`
- Ubah: `src/fakeyou/audio.py:514-530` (area signature `open_meeting_audio` — tambah factory `open_stream_audio` sesudahnya, jangan ubah perilaku lama)
- Uji: `tests/test_stream_capture.py`

**Antarmuka:**
- Konsumsi: bentuk `ManagedMuseSTT`, `SileroVADAnalyzer`, `VADProcessor`, `_TranscriptObserver` dari `src/fakeyou/audio.py`; `InputAudioRawFrame` dari `pipecat.frames.frames`; `BaseInputTransport` dari `pipecat.transports.base_input`.
- Produksi: `StreamAudioInput.feed(audio: bytes) -> None`, `open_stream_audio(*, stream_id: str, on_transcript, stt, ...) -> AsyncIterator[MeetingAudioSession]`, `load_capture_script() -> str`.

- [ ] **Langkah 1: Tulis uji yang gagal**

```python
async def test_stream_input_tags_transcripts_with_stream_id() -> None:
    from fakeyou.audio import open_stream_audio
    from fakeyou.stream_capture import StreamAudioInput

    seen: list[tuple[str, str]] = []

    async def on_transcript(frame: TranscriptionFrame) -> None:
        seen.append((frame.transport_source or "", frame.text))

    assert callable(open_stream_audio)
    assert callable(StreamAudioInput)
```

- [ ] **Langkah 2: Jalankan uji untuk memastikan gagal**

Jalankan: `.venv/bin/python -m pytest tests/test_stream_capture.py -q -k stream_id`
Hasil: FAIL dengan `ModuleNotFoundError` (modul `stream_capture` belum ada).

- [ ] **Langkah 3: Tulis `stream_capture.py` minimal**

```python
class StreamAudioInput(BaseInputTransport):
    def __init__(self, params: TransportParams, **kwargs: Any) -> None:
        super().__init__(params, **kwargs)

    async def feed(self, audio: bytes, sample_rate: int = 16000) -> None:
        await self.push_audio_frame(
            InputAudioRawFrame(audio=audio, sample_rate=sample_rate, num_channels=1)
        )
```

`open_stream_audio` di `audio.py` meniru scaffold `open_meeting_audio`
baris per baris (timeout, jalur error, dan pembungkus
`MeetingAudioSession` yang sama) kecuali: menerima `input:
BaseInputTransport` dari pemanggil sebagai pengganti membangun
`_native_transport()`, dan observer transkripnya mengisi
`frame.transport_source = stream_id` sebelum memanggil
`on_transcript`. Satu pipeline (input, VAD, STT, observer)
berjalan per stream; `MeetingAudioSession` tidak bertambah
perilaku.

- [ ] **Langkah 4: Tulis uji pipeline dengan STT palsu**

Perluas uji: bangun dua pipeline dengan service STT stub yang
menggema teks tetap, dorong PCM berbeda ke tiap input, assert
tiap callback menerima `transport_source` dan teksnya sendiri.
STT stub mensubkelas `FrameProcessor` dan memancarkan
`TranscriptionFrame` saat menerima `AudioRawFrame`.

- [ ] **Langkah 5: Jalankan uji untuk memastikan lulus**

Jalankan: `.venv/bin/python -m pytest tests/test_stream_capture.py -q -m 'not browser'`
Hasil: PASS.

- [ ] **Langkah 6: Jalankan gate statis**

Jalankan: `.venv/bin/ruff check src/fakeyou/stream_capture.py src/fakeyou/audio.py && .venv/bin/mypy src`
Hasil: bersih, tanpa temuan.

- [ ] **Langkah 7: Commit**

```bash
git add src/fakeyou/stream_capture.py src/fakeyou/audio.py tests/test_stream_capture.py
git commit -m "feat(stream): per-stream pipeline with pushed PCM input"
```

## Task 3: Transport drain PCM dengan bujet throughput

**File:**
- Ubah: `src/fakeyou/stream_capture.py` (tambah `parse_drain`, `StreamRegistry`)
- Ubah: `src/fakeyou/runtime.py:313-321` (jalankan loop drain capture di sebelah drain probe saat flag menyala)
- Uji: `tests/test_stream_capture.py`

**Antarmuka:**
- Konsumsi: bentuk drain Task 1 `{streams, roster, dropped}`; pola `page.evaluate` dari `src/fakeyou/attribution_probe.py:92`.
- Produksi: `parse_drain(payload: Mapping[str, Any]) -> list[StreamChunk]`, `StreamRegistry.feed(chunk) -> None`, `StreamChunk(slot, csrcs, level, page_ms, pcm)`.

- [ ] **Langkah 1: Tulis uji yang gagal**

```python
def test_parse_drain_decodes_pcm_windows() -> None:
    from fakeyou.stream_capture import parse_drain

    payload = {
        "streams": [
            {"slot": 0, "csrcs": [111], "level": 0.4, "page_ms": 1000.0, "pcm_b64": "AAP//w=="}
        ],
        "roster": [],
        "dropped": 0,
    }
    (chunk,) = parse_drain(payload)
    assert chunk.slot == 0 and chunk.csrcs == (111,) and len(chunk.pcm) == 4
```

- [ ] **Langkah 2: Jalankan uji untuk memastikan gagal**

Jalankan: `.venv/bin/python -m pytest tests/test_stream_capture.py -q -k parse_drain`
Hasil: FAIL dengan `ImportError` (belum ada `parse_drain`).

- [ ] **Langkah 3: Implementasi `parse_drain` dan `StreamRegistry`**

`parse_drain` mendecode base64 `pcm_b64` menjadi bytes, memvalidasi
key wajib, dan melempar `MeetingAudioError` untuk jendela rusak.
`StreamRegistry` memetakan slot ke `StreamSession`, membuat sesi
saat pertama terlihat, melepas setelah 30 dtk hening, dan
meneruskan PCM ke `StreamAudioInput.feed`.

- [ ] **Langkah 4: Ukur throughput drain secara offline**

Uji: dorong drain sintetis 60 dtk empat stream (32 KB/dtk per
stream) lewat `parse_drain` + registry dan assert waktu tembok
di bawah 5 dtk. Ini gate bujet untuk bertahan pada drain CDP;
bila gagal, berhenti dan alihkan desain transport ke WebSocket
halaman sebelum lanjut.

- [ ] **Langkah 5: Wire loop drain di `runtime.py`**

Cerminkan `start_probe_drain`: `start_capture_drain` mempoll
`window.__fakeyouStreamCapture.drain()` tiap 250 ms dan memberi
makan registry. Dijaga flag baru (Task 5 mendefinisikannya;
pakai parameter lokal `enabled: bool` di sini, di-wire ke
settings di Task 5).

- [ ] **Langkah 6: Jalankan uji dan gate**

Jalankan: `.venv/bin/python -m pytest tests/test_stream_capture.py -q -m 'not browser' && .venv/bin/ruff check src/fakeyou/stream_capture.py src/fakeyou/runtime.py && .venv/bin/mypy src`
Hasil: semua hijau.

- [ ] **Langkah 7: Commit**

```bash
git add src/fakeyou/stream_capture.py src/fakeyou/runtime.py tests/test_stream_capture.py
git commit -m "feat(stream): PCM drain transport with throughput budget"
```

## Task 4: Merge bertag stream dan identitas binder

**File:**
- Ubah: `src/fakeyou/conversation.py:40-64` (tambah `stream_id` ke `TranscriptEvent` dan `transcript_event`)
- Ubah: `src/fakeyou/voice_activity.py:79-135` (`SlotBinder`: lacak CSRC per slot, resolve nama atas identitas stream)
- Uji: `tests/test_stream_capture.py`, plus perbarui call site `transcript_event` di `tests/test_conversation.py`

**Antarmuka:**
- Konsumsi: `TranscriptEvent`, `transcript_event`, `SlotBinder.update_roster/resolve` yang sudah ada.
- Produksi: `transcript_event(frame, participant_id, speaker_evidence_id, stream_id) -> TranscriptEvent`, `merge_transcripts(events: Sequence[TranscriptEvent]) -> list[TranscriptEvent]`, `SlotBinder.observe_stream(slot, csrcs) -> None`.

- [ ] **Langkah 1: Tulis uji yang gagal**

```python
def test_merge_orders_two_streams_chronologically() -> None:
    from fakeyou.conversation import merge_transcripts, transcript_event

    first = transcript_event(_frame("hello", "2026-10-06T01:00:02+00:00"), "a", None, "s1")
    second = transcript_event(_frame("hi", "2026-10-06T01:00:01+00:00"), "b", None, "s0")
    assert [event.text for event in merge_transcripts([first, second])] == ["hi", "hello"]
```

`_frame` membangun `TranscriptionFrame(text, user_id="", timestamp=ts)`
sesuai `pipecat.frames.frames`.

- [ ] **Langkah 2: Jalankan uji untuk memastikan gagal**

Jalankan: `.venv/bin/python -m pytest tests/test_stream_capture.py -q -k merge_orders`
Hasil: FAIL dengan `ImportError` (belum ada `merge_transcripts`).

- [ ] **Langkah 3: Implementasi field `stream_id` dan merge**

Tambah `stream_id: str` ke `TranscriptEvent` (default `""` menjaga
call site lama tetap kompilasi; perbarui semuanya agar mengisi id
asli). `merge_transcripts` mengurut menurut timestamp terparse,
stabil pada id. Perbarui tiap call site `transcript_event(` yang
ada (cari dengan `rg -n "transcript_event\("`) agar mengisi
`stream_id`.

- [ ] **Langkah 4: Ikat identitas stream slot/CSRC**

Tambah `SlotBinder.observe_stream(slot: int, csrcs: Sequence[int]) -> None`
yang mencatat himpunan CSRC per slot; `resolve` mengutamakan
tenure yang himpunan CSRC-nya beririsan, dan fallback ke
eliminasi yang sudah ada. Uji: dua slot dengan CSRC berbeda
me-resolve ke label berbeda lintas rotasi slot (CSRC yang sama
muncul kembali di slot lain, label mengikuti CSRC).

- [ ] **Langkah 5: Jalankan suite yang terdampak**

Jalankan: `.venv/bin/python -m pytest tests/test_stream_capture.py tests/test_conversation.py tests/test_voice_activity.py -q -m 'not live and not container and not browser'`
Hasil: PASS.

- [ ] **Langkah 6: Jalankan gate statis**

Jalankan: `.venv/bin/ruff check src/fakeyou/conversation.py src/fakeyou/voice_activity.py && .venv/bin/mypy src`
Hasil: bersih.

- [ ] **Langkah 7: Commit**

```bash
git add src/fakeyou/conversation.py src/fakeyou/voice_activity.py tests/test_stream_capture.py tests/test_conversation.py
git commit -m "feat(stream): stream-tagged merge and binder identity"
```

## Task 5: Wiring, flag, dan integrasi record/journal

**File:**
- Ubah: `src/fakeyou/settings.py` (tambah `per_stream_enabled: bool = False` pada conversation settings)
- Ubah: `src/fakeyou/cli/join.py:343-397` (jalur transkrip per stream di balik flag)
- Ubah: `src/fakeyou/conversation.py:581` (`handle_transcript` menerima event stream yang sudah di-merge)
- Uji: `tests/test_stream_capture.py`

**Antarmuka:**
- Konsumsi: Task 2–4 (`open_stream_audio`, `parse_drain`, `StreamRegistry`, `merge_transcripts`).
- Produksi: perilaku flag-mati identik byte dengan mixed; perilaku flag-nyala memakai jalur per stream ujung ke ujung di `join`.

- [ ] **Langkah 1: Tulis uji yang gagal**

```python
def test_flag_off_keeps_mixed_path() -> None:
    from fakeyou.settings import Settings

    assert Settings().conversation.per_stream_enabled is False
```

- [ ] **Langkah 2: Jalankan uji untuk memastikan gagal**

Jalankan: `.venv/bin/python -m pytest tests/test_stream_capture.py -q -k flag_off`
Hasil: FAIL dengan `AttributeError` (belum ada `per_stream_enabled`).

- [ ] **Langkah 3: Tambah flag dan wire `join.py`**

Tambah setting. Di `join.py`, saat flag menyala: jalankan script
capture plus loop drain, buat registry, rute tiap pipeline stream
`on_transcript` lewat `transcript_event(..., stream_id)` dengan
atribusi binder, merge sebelum `conversation.handle_transcript`.
Saat mati: jalur kode lama tidak tersentuh — buktikan dengan
membandingkan bentuk journal `transcript_finalized` pada dua mode
di uji berikut.

- [ ] **Langkah 4: Buktikan flag-mati identik**

Uji dengan fake: jalankan callback transkrip join pada dua mode
di atas frame fixture yang sama dan assert baris `record.jsonl`
identik kecuali field `stream_id`/`origin`. Fixture: sesi mixed
dua giliran yang disamarkan.

Ruling: deviasi Langkah-4 DITERIMA. Keputusan handoff-(c) (stream_id masuk hash identitas) dan handoff-(b) (field stream_id di baris flag-mati) membuat byte-identik HEAD-vs-base tak dapat dipenuhi bersama; bar yang berlaku adalah identik lintas-mode pada HEAD modulo field identitas (id/transcript_id/stream_id/origin). Kode jalur flag-mati tak berubah field-per-field (verifikasi reviewer); id adalah hash konten per-run tanpa garansi stabil lintas versi. Catatan plan Task 5 Langkah 4 diamendemen oleh fix implementer.

- [ ] **Langkah 5: Jalankan suite offline penuh**

Jalankan: `.venv/bin/python -m pytest -m 'not live and not container and not browser' -q`
Hasil: semua hijau, tanpa skip baru.

- [ ] **Langkah 6: Jalankan gate statis**

Jalankan: `.venv/bin/ruff check && .venv/bin/ruff format --check && .venv/bin/mypy src`
Hasil: bersih.

- [ ] **Langkah 7: Commit**

```bash
git add src/fakeyou/settings.py src/fakeyou/cli/join.py src/fakeyou/conversation.py tests/test_stream_capture.py
git commit -m "feat(stream): wiring, flag, and record integration"
```

## Task 6: Acceptance cutover pada sesi live

**File:**
- Buat: `docs/reports/YYYY-MM-DD-per-stream-acceptance.md`
- Uji: sesi live (lihat skrip di bawah), fixture replay di `tests/fixtures/per-stream-*.json`

**Antarmuka:**
- Konsumsi: Task 1–5 selesai dan direview.
- Produksi: laporan acceptance dengan gate terukur; jalur mixed dihapus hanya sesudah gate lulus.

- [ ] **Langkah 1: Build image worker dengan branch**

Jalankan: `docker compose build meeting-worker`
Hasil: exit 0.

- [ ] **Langkah 2: Sesi live PS-1 (2 manusia + host + twin)**

Skrip: 10 giliran solo bergantian, satu segmen mute 30 dtk per
orang, satu overlap 2 dtk (hitung bersama), satu rejoin dengan
nama sama. Catat ground truth per giliran (pembicara,
solo/overlap, fragmen).

- [ ] **Langkah 3: Ukur gate acceptance secara offline**

Replay PS-1 lewat `merge_transcripts` + binder dan hitung:
overlap terpisah menjadi dua teks benar ≥95%; atribusi solo ke
stream yang benar ≥95%; presisi nama terikat ≥0,98 dan sisanya
`unknown`; akhir-ucapan ke PCM-pertama p50 ≤3 dtk, p95 ≤6 dtk.

- [ ] **Langkah 4: Sesi live PS-2 dengan versi skrip baru**

Ulangi dengan pembicara/urutan berbeda; semua gate harus lulus
lagi. Bila ada gate gagal, berhenti: perbaiki akar penyebab
dengan tes merah, atau laporkan dan pertahankan mixed.

- [ ] **Langkah 5: Hapus jalur mixed dan gate lama**

Hanya sesudah PS-1 dan PS-2 lulus: hapus cabang capture mixed
di `join.py`/`audio.py`, jadikan per-stream satu-satunya jalur,
perbarui docs dan fixture. Jalankan ulang suite offline penuh
plus gate replay.

- [ ] **Langkah 6: Tulis laporan acceptance**

Laporan berisi tabel gate (angka ukur, perintah, exit code),
risiko, dan rekomendasi default flag. Bahasa Indonesia.

- [ ] **Langkah 7: Commit**

```bash
git add docs/reports/ tests/fixtures/per-stream-*.json src/fakeyou/
git commit -m "feat(stream): cutover acceptance on PS-1 and PS-2"
```

## Task 7: Mesin encoded-tap plus decode Opus

Konteks: diagnosis Task 6 membuktikan Meet mengkonsumsi
encoded frames tanpa write-back ke native decode sehingga MSTP
membaca silence fabrikan (repro offline: consume -> 0.0,
writeback -> 0.708). Engine primer berpindah ke tap
encoded-frames plus decode Opus WebCodecs; MSTP menjadi
fallback. Jalur Python tidak berubah.

**File:**
- Ubah: `src/fakeyou/assets/stream_capture.js` (wrap
`RTCRtpReceiver.prototype.createEncodedStreams` yang
forward-transparan, AudioDecoder Opus per receiver, seleksi
engine per slot, MSTP sebagai fallback)
- Uji: `tests/test_stream_capture.py` (browser test setup
consume)

**Antarmuka:**
- Konsumsi: bentuk drain Task 1 `{streams, roster, dropped}`
TETAP (field `track` opsional sudah ada; parser mengabaikan
field tak dikenal).
- Produksi: windows PCM per slot dari decode Opus saat
encoded mengalir; dari MSTP saat tidak. Seleksi otomatis per
slot berdasar sinyal (encoded diterima <2 dtk terakhir).

- [ ] **Langkah 1: Tulis uji browser yang gagal**

Uji loopback satu tone dengan consumer encoded
(read-tanpa-write-back, pola `/tmp/consume_probe.py` mode
consume): drain HARUS memuat `level > 0.01` (jalur decode).
Kode kini gagal (MSTP = 0.0). Bukti merah wajib.

- [ ] **Langkah 2: Jalankan uji untuk memastikan gagal**

Jalankan: `.venv/bin/python -m pytest
tests/test_stream_capture.py -q -m browser -k consume`
Hasil: FAIL asersi level (bukan error API).

- [ ] **Langkah 3: Implementasi tap plus decode**

Wrap prototipe `createEncodedStreams` (chain aman dengan wrap
milik probe bila ada: selalu panggil original):
pipeThrough tap yang mencatat frame lalu
`controller.enqueue` setiap frame (forward-transparan, Meet
tidak terganggu). Tiap receiver yang ter-tap mendapat satu
AudioDecoder Opus (codec `opus`, konfigurasi dari parameter
yang terbukti bekerja di uji loopback; catat pilihan di
laporan); output AudioData masuk jalur `pushFloat` yang sudah
ada. Error decoder bersifat toleran (tutup dan buka ulang
per receiver, tidak menjatuhkan slot lain).

- [ ] **Langkah 4: Seleksi engine dan fallback MSTP**

Per slot: bila encoded diterima <2 dtk terakhir, windows
berasal dari decode; bila tidak, dari MSTP (kode lama
dipertahankan utuh). Buktikan kedua rezim: setup consume ->
decode non-sunyi; setup none -> MSTP non-sunyi (uji lama
tetap hijau).

- [ ] **Langkah 5: Jalankan suite dan gate**

Jalankan: `.venv/bin/python -m pytest -m 'not live and not
container and not browser' -q` (TANPA `-n`), lalu browser
`tests/test_stream_capture.py -q -m browser`, lalu
`.venv/bin/ruff check && .venv/bin/ruff format --check &&
.venv/bin/mypy src`. Hasil: semua hijau, tanpa skip baru.

- [ ] **Langkah 6: Commit dan laporan**

Self-review diff PENUH termasuk perubahan tree kotor yang
sudah ada (rescan, fleet-4, heartbeat, track): pahami tiap
hunk sebelum commit. Commit lokal per unit logis (fix,
instrumentasi, engine). Tulis
`.superpowers/sdd/per-stream-capture-plan/task-7-report.md`
(ringkasan, file diubah, perintah verifikasi plus hasil
nyata, bukti merah, concerns) dan perbarui
`.runtime/per-stream-capture/status.json`. Tanpa push/merge.
