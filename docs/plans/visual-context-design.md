# Desain Konteks Visual (Milestone 6) — Digital AI Twin

Status: spec untuk review. Implementasi menyusul setelah spec disetujui.

## 1. Tujuan

Twin melihat screenshare/slide di Meet, menjawab pertanyaan tentang
visual yang benar, dan meminta klarifikasi bila ambigu. Acceptance
(`docs/plans/next-development.md:301`): jawaban merujuk visual yang
benar (dengan sitiran frame + timestamp); klarifikasi netral saat
referensi ambigu; tanpa menambah latensi jalur jawab; tanpa
bukti-isi di jurnal.

Non-tujuan: presentasi oleh twin (twin tidak pernah share),
perekaman video rapat, OCR sebagai produk mandiri.

## 2. Arsitektur: tiga jalur terpisah

```
[Jalur V] Meet page → VisualObserver → dedup → caption (VLM dedicated)
              → visual.jsonl + PNG → index waktu
[Jalur J] speech → prefetch (+caption terbaru) → model jawab → bicara
[Jalur M] pasca-meeting: caption → episode Graphiti (ingatan antar-meeting)
```

- **Jalur V (visual, async):** hidup selama ada share, tidak pernah
  ditunggu Jalur J. Gagal/lambatnya VLM tidak mengubah percakapan.
- **Jalur J (jawab, hot path):** hanya menerima evidence TEKS
  (caption + `frame:<hash8>@<ts>`). Byte gambar dihydrate on-demand
  per model-call via middleware, tidak pernah masuk state,
  checkpoint, atau jurnal.
- **Jalur M (memori):** ingestion caption visual menjadi episode
  Graphiti; recall antar-meeting bisa menjawab "slide kemarin".

Titik temu V↔J hanya dua: (1) prefetch menempelkan caption visual
terbaru; (2) tool `describe_frame(frame_id)` untuk baca mendalam.

## 3. Komponen

| Komponen | Tugas | Pola repo yang dipakai ulang |
| --- | --- | --- |
| `VisualObserver` | poll flag presenting, screenshot klip kotak konten | `StreamPoller`, `ParticipantObserver` |
| Dedup + ring | dhash Hamming, ring RAM 90 frame/3 mnt + spill disk | `audio_persist` anchors |
| Captioner VLM | 1 caption per hash distinct, cache TTL | peran provider baru `visual` |
| `VisualHydrateMiddleware` | hydrate ref→image block per-call | `_MemoryPrefetchMiddleware` |
| `describe_frame` | baca mendalam on-demand oleh agen | pola tool meeting |
| Ingest visual | caption → episode Graphiti | `memory-ingest` windows |
| Purge visual | hapus PNG kedaluwarsa + laporan | `audio_purge_expired` |

## 4. Data dan skema

- `visuals.jsonl` per meeting:
  `{visual_id, kind, captured_page_ms, captured_at, visible_from,
  visible_to|null, sha256, thumb}`. Kunci gabung = `page_ms`.
- Evidence `visual_evidence/v1`:
  `{visual_id, captured_at, kind, thumb_ref, ocr_excerpt,
  transcript_anchor}`.
- Provenance v2: `kind=meeting_visual`, granularity `frame`,
  `frame_ref`.
- Record: `VisualEntry` baru (discriminator record menolaknya hari
  ini — harus ditambah).
- Gabung ke ucapan: `capture_ms` dalam `[from-tol, to+tol]`,
  `tol=2000ms` (pola diarization); `capture_ms==0` fallback ke
  kedekatan dinding `spoken_at/at`.
- Ambiguitas (>1 visual dalam tol atau nol): jawab sitir
  `visual_id` terdekat + satu klarifikasi netral; tidak menebak.

## 5. Budget, cap, dan gate M6

- Capture 1 frame/2 detik ke ring lokal (90 frame/3 menit, ~gratis);
  VLM/OCR hanya saat dedup-miss, maks 1 panggilan/10 detik.
- Cap per meeting: 120 panggilan VLM (~$0,12–0,24 tier flash),
  maks 2 gambar per giliran jawab, maks biaya $0,50/meeting.
- Capture→referensi: p95 ≤ 12 detik, maks 30 detik,
  stabilitas-resolve ≥ 0,95 (cermin Q-6 M4).
- M6 LULUS = replay (pass ≥ 0,9, trailing ≤ 20%, p95 agent ≤ 4,0)
  DAN live (p50 ≤ 3, p95 ≤ 6) DAN visual (p95 ≤ 12, maks ≤ 30,
  stabilitas ≥ 0,95, panggilan ≤ 120, biaya ≤ $0,50) DAN
  `response_budget` hijau DAN tanpa dep baru.
- Nol tambah latensi agent bersifat mutlak (gate live sudah merah).

## 6. Retensi (default usulan, menunggu konfirmasi review)

- Teks turunan (caption/OCR): 30 hari (ikut `AudioSettings`).
- Frame mentah PNG: 7 hari (batas disk).
- Sidik hash + sitiran frame di memori/debrief ikut retensi teks.

## 7. Batasan keras

- Tanpa protokol privat Meet dan tanpa Meet Media API; otomasi UI
  publik + screenshot Playwright saja.
- Flag default mati + gate lulus eksplisit sebelum dinyalakan
  (pola voice-labels).
- Jurnal metadata saja: ID/frame-ref/hash/timestamp/durasi —
  byte/base64 gambar DILARANG di jurnal (kontrak metadata-saja +
  jurnal tercetak ke stdout).
- Tanpa screenshot untuk evidence rutin; ledger baca-balik di
  sidecar, bukan jurnal.
- Tes merah dulu; tanpa menghapus sampel lambat; tanpa dep baru
  (Pillow + Playwright sudah terkunci transitif).

## 8. Pengujian

- Unit red-first per komponen (trigger, ambiguitas, atribusi,
  hydrate-cycle, dedup, purge).
- Probe Task-0 live: kunci selector tile presentasi (2x pindai
  sepakat) + ukur TTFT gambar-di-prompt sebelum bekukan p95 12 detik.
- Skenario eval: jawab-benarkan, klarifikasi (larang-tebakan),
  debrief sitir-visual.
- Acceptance live: tanya soal slide yang di-share manusia;
  verifikasi sitiran + klarifikasi ambigu.

## 9. Keputusan yang dikunci spec ini

- D1: tiga jalur terpisah (V async, J hot path, M memori).
- D2: VLM dedicated (peran `visual`), bukan numpang model jawab.
- D3: ref-di-state, hydrate-per-call; base64 tidak masuk checkpoint.
- D4: eager caption per hash + lazy `describe_frame`; maks 2 gambar
  per giliran.
- D5: tolak desain inline-gambar-di-thread (bloat checkpoint) dan
  eager-VLM-tanpa-dedup (biaya jebol).
- D6: angka §5–§6 sebagai default, disahkan saat review spec.

## 10. Penargetan capture (koreksi lanjutan Task-0)

- `xpath` `"Presentation"` TERBUKTI salah sasaran pada run live
  2026-10-09: locator mengenai strip spanduk 233x36 (`section`,
  label `"Name (Presenting)"`), bukan isi presentasi, sehingga
  ronde 1–3 gagal total.
- Penargetan baru memakai `VISUAL_CONTENT_BOX_JS`: jangkar =
  `button` yang `aria-label`-nya berawalan `"unpin"` dan memuat
  `"presentation"` (case-insensitive); rambat naik ke leluhur
  pertama berukuran minimal 400x300; `page.screenshot` memakai
  `clip` kotak itu.
- Nama kelas CSS Meet ter-obfuscate dan TIDAK PERNAH dipakai
  sebagai pencocokan; geometri + `aria-label` saja.
- Bila jangkar/kotak tidak ketemu, atau kotak < 400x300, observer
  memakai screenshot full-page (kontrak cadangan poin 2 laporan
  probe); jalur yang dipakai (`"clip"`/`"full"`) tercatat di jurnal
  pada field `capture` event `visual_captured`.
- Deteksi tidak berubah: sufiks `"is presenting"` ATAU persis
  `"Presentation"` + label Unpin; debounce 2 pindai.

