# Visual Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Twin melihat screenshare/slide, menjawab dengan sitiran frame yang benar, klarifikasi bila ambigu.

**Architecture:** Tiga jalur terpisah — (V) capture async + caption VLM dedicated, (J) jawab hot path menerima evidence teks + hydrate gambar on-demand, (M) ingestion memori visual. Detail di spec.

**Tech Stack:** Playwright `locator.screenshot` (terkunci 1.63.0), Pillow (terkunci 12.3.0, dhash manual), LangChain 1.4.2 standard image blocks, LangGraph 1.2.12 middleware `request.override`, peran provider `visual` baru.

**Spec:** `docs/plans/visual-context-design.md` — executor membaca spec + plan ini.

## Global Constraints

- Python Inggris tanpa komentar/docstring dan tanpa string Indonesia di `.py`; dokumentasi Indonesia.
- Tanpa protokol privat Meet dan tanpa Meet Media API; screenshot Playwright publik saja.
- Tanpa dependency baru (Pillow + Playwright sudah terkunci transitif).
- Tes merah dulu setiap task; tiap task direview reviewer terpisah sebelum commit.
- Flag default mati; jurnal metadata saja (byte/base64 gambar DILANG di jurnal).
- Tanpa push/merge; ledger SDD di `.superpowers/sdd/visual-context/progress.md`.
- Perintah verifikasi per task:
  `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false .venv/bin/pytest <target> -q -p no:cacheprovider`
  + `.venv/bin/ruff check src tests evals` + `.venv/bin/ruff format --check src tests evals` + `.venv/bin/mypy src`.

---

## Goal

Milestone 6: screenshare/slide bertimestamp terhubung ke pembicaraan; jawaban merujuk visual yang benar; klarifikasi bila ambigu; tanpa tambah latensi jawab.

## Success Criteria

- Jawaban soal slide menyitir `frame:<hash8>@<ts>` yang benar (cocok jendela waktu ucapan ±2000ms).
- Referensi ambigu/nol visual → satu klarifikasi netral, tanpa tebakan.
- Frame mentah tidak pernah masuk state/checkpoint/jurnal (audit: grep base64 nihil).
- Gate M6 hijau (§5 spec): replay + live + visual (p95≤12, maks≤30, stabilitas≥0,95, panggilan≤120, biaya≤$0,50) + `response_budget` hijau.

## Approach

Fase 1 (live QA): Task 0→1→2→3. Fase 2 (memori + debrief): Task 4→5→6. Probe Task-0 mengunci selector tile presentasi + mengukur TTFT gambar-di-prompt sebelum angka gate dibekukan.

## Steps

### Task 0: Probe selector presentasi + ukur TTFT (manual, gate)

**Files:**
- Create: `docs/reports/YYYY-MM-DD-visual-probe.md`
- Scripts: `/tmp` saja (tidak di repo)

**Interfaces:**
- Consumes: halaman Meet twin via `open_direct_page` (ada).
- Produces: seed selector tile (`SELECTOR` string), angka TTFT p50/p95 (catat di laporan).

- [ ] Step 1: Join twin ke Meet (butuh admit), mulai share dari manusia.
- [ ] Step 2: Snapshot aria-label penuh sebelum/saat/sesudah share, 2x sepakat per klaim.
- [ ] Step 3: Screenshot tile via kandidat selector; catat yang stabil.
- [ ] Step 4: Kirim 5 invoke multimodal ke model reply (isi: 1 frame + 1 kalimat); catat TTFT.
- [ ] Step 5: Tulis laporan; gate LULUS = selector terkunci + TTFT terukur.

Run: `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false .venv/bin/pytest tests/test_documentation.py -q -p no:cacheprovider`
Expected: PASS (laporan Indonesia, taksonomi benar).

Out of scope: kode produksi apa pun.

### Task 1: Pipa capture (observer + dedup + ring + sidecar)

**Files:**
- Create: `src/fakeyou/visual_observer.py`, `src/fakeyou/visual_store.py`
- Modify: `src/fakeyou/runtime.py` (start/stop observer di worker, di luar hot path)
- Test: `tests/test_visual_capture.py`

**Interfaces:**
- Consumes: `Page` worker, selector Task-0 (`PRESENT_TILE_XPATH: str`).
- Produces: `VisualFrame(frame_id, captured_page_ms, captured_at, sha256, path, thumb_path)`,
  `append_visual(store, frame) -> None`, `visuals.jsonl` schema §4 spec,
  event jurnal `visual_captured{visual_id, kind, chars, duration_ms}`.

Red tests (tulis dulu, buktikan merah):
- `test_capture_writes_frame_and_sidecar_row` — fake Page screenshot bytes → PNG + baris jsonl ada, `page_ms` terisi.
- `test_dedup_drops_near_duplicate` — dua frame Hamming ≤ 8 → 1 tersimpan, 1 `duplicate_suppressed`.
- `test_ring_spills_to_disk` — frame ke-91 → tertua ke disk, RAM tetap 90.
- `test_journal_has_no_bytes` — grep artefak jurnal: nihil base64/PNG.
- `test_capture_never_blocks_reply` — capture 5 detik macet → path jawab tetap < budget (fake clock).

Keputusan terkunci: JPEG q60, tile saja (bukan full-page), kadensi 1/2 detik, dhash 64-bit manual via Pillow (tanpa lib dhash), stop → observer berhenti ≤1,5 detik.

Out of scope: VLM/caption (Task 2), konsumsi model (Task 3).

### Task 2: Peran VLM dedicated + caption + describe_frame

**Files:**
- Create: `src/fakeyou/visual_caption.py`
- Modify: `src/fakeyou/providers.py` (peran `visual`), `src/fakeyou/settings.py` (VisualSettings: model, timeout, budget), `src/fakeyou/agent.py` (tool `describe_frame`)
- Test: `tests/test_visual_caption.py`

**Interfaces:**
- Consumes: `VisualFrame` (Task 1).
- Produces: `caption_frame(frame) -> VisualCaption{visual_id, text, tokens, cost_usd}`,
  cache hash→caption dengan TTL, tool `describe_frame(frame_id: str) -> str`.

Red tests:
- `test_caption_called_once_per_hash` — 3 frame hash sama → 1 panggilan VLM.
- `test_caption_prompt_images_before_text` — payload image block sebelum teks (sesuai dokumen vendor).
- `test_describe_frame_returns_caption_plus_ref` — tool mengembalikan teks + `frame:<hash8>@<ts>`.
- `test_budget_stops_calls` — cap $0,50 tercapai → caption dilewati + jurnal `budget_exhausted`, tanpa exception.
- `test_downscale_before_send` — sisi panjang >1568 → di-resize dulu (hemat token tier standard).

Keputusan terkunci: model default = tier flash vision termurah yang lolos probe multimodal; token diestimasi `⌈w/28⌉×⌈h/28⌉`; biaya aktual dicatat dari `usage_metadata`.

Out of scope: hydrate ke model jawab (Task 3).

### Task 3: Konsumsi model (hydrate + prefetch + greetings)

**Files:**
- Create: `src/fakeyou/visual_hydrate.py` (`VisualHydrateMiddleware`)
- Modify: `src/fakeyou/conversation.py` (`TranscriptEvent.visual_refs`, `_request_messages` tetap str),
  `src/fakeyou/greetings.py:78` (`.text` bukan `isinstance str`),
  `src/fakeyou/memory.py` (prefetch menempelkan caption terbaru; `_current_speech` abaikan kind visual),
  `src/fakeyou/assembly.py` (daftarkan middleware sesudah prefetch)
- Test: `tests/test_visual_hydrate.py`

**Interfaces:**
- Consumes: `visuals.jsonl` refs, `VisualCaption` (Task 2).
- Produces: pesan terhydrate per-call (tidak masuk checkpoint), evidence `visual_evidence/v1`.

Red tests:
- `test_state_has_refs_only` — setelah turn multimodal, checkpoint/state nihil base64.
- `test_hydrate_adds_max_two_frames` — 5 ref → payload hanya 2 terbaru + caption.
- `test_greeting_survives_multimodal` — greeting fast-path tetap jalan (pakai `.text`).
- `test_prefetch_appends_latest_caption` — caption terbaru menempel sesudah speech.
- `test_streaming_unchanged` — path `astream` + filter `langgraph_node==model` hijau.

Keputusan terkunci: hydrate di `awrap_model_call` via `request.override`; `before_model` memangkas frame lama jadi teks OCR; `after_model` membuang gema image block; maks 2 gambar per giliran.

Out of scope: record/provenance/ingest (Task 4).

### Task 4: Record + provenance + ingest + debrief visual

**Files:**
- Modify: `src/fakeyou/record.py` (`VisualEntry` + discriminator),
  `src/fakeyou/provenance.py` (schema v2: `kind=meeting_visual`, `frame_ref`),
  `src/fakeyou/ingestion.py` (jendela visual → episode),
  `src/fakeyou/debrief.py` + `src/fakeyou/debrief_sources.py` (`SourceEntry` id `frame:<hash8>@<ts>`, kutip caption)
- Test: `tests/test_record.py`, `tests/test_provenance.py`, `tests/test_ingestion.py`, `tests/test_debrief.py` (extend)

**Interfaces:**
- Consumes: `VisualFrame` + `VisualCaption` (Task 1–2).
- Produces: entri record visual, episode memori visual, item debrief bersitir frame.

Red tests:
- `test_record_accepts_visual_entry` — `extra=forbid` tidak menolak kind visual.
- `test_provenance_visual_kind` — render evidence v2 memuat `frame_ref`.
- `test_ingest_visual_window` — caption → episode dengan `reference_time` jendela.
- `test_debrief_cites_frame` — item debrief memuat `frame:<hash8>@<ts>` + kutip ≤1000 char.
- `test_forget_meeting_cleans_visuals` — lupa meeting menghapus episode + ref biner visual.

Keputusan terkunci: sidik hash + sitiran ikut retensi teks 30 hari; klaim visual konflik → item uncertain terpisah (pola debrief).

Out of scope: purge file + report + gate (Task 5).

### Task 5: Retensi + laporan + gate M6

**Files:**
- Modify: `src/fakeyou/cli/audio.py` (fungsi purge visual meniru `audio_purge_expired`),
  `src/fakeyou/report.py` (counter cakupan visual),
  `src/fakeyou/settings.py` (VisualSettings: retention + caps, flag default mati)
- Test: `tests/test_visual_retention.py`, `tests/test_report.py` (extend)

**Interfaces:**
- Consumes: `visual/` per meeting, `visuals.jsonl`.
- Produces: `visual_purge_expired(...) -> PurgeReport`, `report.visual{frames, captioned, cost_usd, p95_ms}`,
  konstanta gate `VISUAL_BUDGET_P95=12.0`, `VISUAL_BUDGET_MAX=30.0`, `VISUAL_STABILITY_MIN=0.95`.

Red tests:
- `test_purge_removes_expired_png` — PNG >7 hari terhapus + `freed_bytes` dilaporkan.
- `test_text_kept_30d` — caption <30 hari bertahan.
- `test_report_counts_visuals` — report memuat counter visual.
- `test_gate_constants_consumed` — tes gate memakai konstanta (bukan angka magis).
- `test_flag_off_disables_everything` — flag mati → tanpa capture/caption/hydrate.

Keputusan terkunci: frame mentah 7 hari, teks 30 hari (default §6 spec, disahkan saat review).

Out of scope: eval + acceptance live (Task 6).

### Task 6: Eval + acceptance live

**Files:**
- Create: `evals/test_visual_eval.py`, `evals/fixtures/visual-scenarios.json`
- Manual: acceptance live (manusia share slide + tanya)

**Interfaces:**
- Consumes: semua Task 0–5.
- Produces: verdict eval + laporan acceptance.

Red tests (skenario fixture):
- `answer_correct_visual` — tanya slide X → sitiran frame benar.
- `clarify_ambiguous` — referensi 2 kandidat → klarifikasi, aids melarang tebakan.
- `clarify_missing` — tanpa visual → klarifikasi netral.
- `debrief_cites_visual` — debrief memuat sitiran frame.
- `budget_caps_hold` — replay: panggilan ≤120, biaya ≤$0,50, p95 ≤12.

Acceptance live (manual, butuh manusia share slide):
tanya 3 soal slide (fakta, angka, perbandingan) + 1 ambigu;
lulus = 3 sitiran benar + 1 klarifikasi + gate angka hijau.

## Validation Plan

- Per task: file tes task + `ruff check` + `ruff format --check` + `mypy src`.
- Akhir: suite penuh `tests/` + `evals/` + review final cabang.
- Validasi tertinggi-risiko: probe Task-0 (TTFT gambar-di-prompt) + acceptance live Task-6.

## Risks / Open Questions

- TTFT gambar-di-prompt tak ada data publik → diukur di Task-0 sebelum bekukan p95 12 detik; bila >12 detik, gate direvisi (bukan dilonggar diam-diam).
- Jalur multimodal kita TERBUKTI 2026-10-09: `gemini-3.8-flash` via `google_genai` + standard image blocks → `ok:true`, TTFT 3,99 dtk, jawaban baca slide dengan benar, `usage_metadata` 1115/125 token. Sisa: ukur TTFT 5x formal di Task-0; endpoint `openai_compatible` lapangan belum ada yang live untuk diprobe → fallback caption-only bila ditolak.
- `openai_compatible` lapangan belum tentu terima blok image → fallback caption-only bila ditolak; diuji di Task-2.
- Harga promo model bisa berubah → verifikasi ulang saat build; cap $ proteksi.
- Open questions: None selain angka default §6 spec (retensi 7d/30d, cap $0,50) yang disahkan saat review.

## Sources

Keputusan eksternal material merujuk konten primer yang diperiksa run ini:

- https://docs.langchain.com/oss/python/langchain/messages (standard image blocks, base64/file form)
- https://docs.langchain.com/oss/python/langchain/middleware/custom (wrap hooks + `request.override`)
- https://playwright.dev/python/docs/screenshots (element screenshot ke bytes, format/clip/quality)
- https://playwright.dev/python/docs/videos (video finalize saat context close → ditolak untuk live path)
- https://platform.claude.com/docs/en/build-with-claude/vision (blok image base64/URL/file_id; token `⌈w/28⌉×⌈h/28⌉`; tier standard 1568; ≤20 gambar ≤2000px; gambar-sebelum-teks; harga Haiku $1/MTok)
