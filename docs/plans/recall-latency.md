# Plan recall satu perjalanan model dan perbaikan kecil pasca-Gemini

> **Untuk agentic worker:** eksekusi task berurutan dengan checkbox. Orkestrator menjalankan sendiri command verifikasi sebelum menerima hasil pekerja; reviewer semantik terpisah dari penulis.

**Tujuan:** menurunkan p95 agent pada giliran yang memerlukan memori dari ~5,9 detik ke ≤4 detik tanpa melonggarkan gate kualitas, lalu menutup tiga cacat kecil yang ditemukan pada review 4 Oktober.

**Status:** rencana, 4 Oktober 2026. Basis `master` pada `3450174`. Buat branch `feat/recall-latency`. Jangan push/merge.

## Bukti awal

Corpus trust 24×3 dengan prompt identik, provider Gemini native, recall memakai storage sintetis (durasi tool ~1 ms):

| Model | Giliran 1 call | Giliran recall (2 call) | Per call | Gate semantik |
| --- | --- | --- | --- | --- |
| `gemini-3.8-flash` | p50 1,98 s / p95 2,44 s | p50 4,23 s / p95 5,94 s | ~2,0 s | lulus: 0 severe, 69/72 useful |
| `gemini-3.5-flash-lite` | p50 0,78 s / p95 0,88 s | p50 1,56 s / p95 1,78 s | ~0,8 s | gagal: 56/72, 12 pair melewati recall wajib |

Kesimpulan: biaya ada pada perjalanan model kedua, bukan pada recall. [Pelajaran latensi 2 Oktober](../reports/2026-10-02-latency-lessons.md) menemukan pola yang sama di sesi live (recall 83–247 ms; dua perjalanan model 4,2–6,4 s). Model cepat yang memutuskan sendiri kapan recall justru melewatinya. Artefak: `.runtime/meeting-delegation/G2-*/` dan `resume-g2-*-review.md`.

## Pendekatan

Ambil evidence memori **sebelum** model dipanggil, lalu masukkan sebagai konteks giliran, sehingga jawaban cukup satu perjalanan model dan model tidak bisa melewati recall. Ini menerapkan pola LMA "siapkan resource sebelum jawaban diperlukan" (pre-connect pada frasa parsial, `nova-agent.ts:1860–1885`) ke retrieval. System prompt tetap statis agar cache prompt provider tidak pecah; evidence per giliran masuk sebagai pesan konteks terpisah.

Bukan bagian plan ini: streaming per kalimat ([plan tersendiri](sentence-streaming.md)), speech-to-speech, filler "sebentar saya cek" (tidak diperlukan bila tidak ada perjalanan kedua), dan model router tambahan.

## Batasan global

- Tanpa dependency baru atau perubahan lockfile. Python Inggris tanpa komentar/docstring; dokumentasi Bahasa Indonesia. Tanpa shim legacy atau alias konfigurasi.
- Jangan mengubah `.env`, backup `.env.*`, persona privat, record acceptance lama, atau artefak `.runtime` lama. Run baru memakai direktori baru.
- Gate tidak dilonggarkan: brief dan trust 0 severe dan ≥90% useful (trust ≥65/72), p95 agent ≤4 s, live akhir ucapan→PCM pertama p50 ≤3 s / p95 ≤6 s. Tanpa `xfail`, penghapusan kasus, atau pembuangan sampel lambat.
- Scope `brief_only` tetap tanpa memori: tidak ada prefetch, tidak ada tool recall.
- Evidence prefetch adalah data, bukan instruksi atau izin; seluruh aturan sumber, atribusi, dan kewenangan di `persona.py` tetap berlaku.

## Task 0 — Baseline

- [ ] Catat HEAD/status. Jalankan `uv run --no-sync ruff check . && uv run --no-sync ruff format --check . && uv run --no-sync mypy && uv run --no-sync pytest -m 'not live and not container' -q`. Kegagalan awal dicatat terpisah.
- [ ] Baca `src/fakeyou/memory.py` (`create_memory_tools`, `recall_meeting_context`, `render_evidence`), `src/fakeyou/provenance.py`, `src/fakeyou/assembly.py`, `src/fakeyou/persona.py` (`RECALL_RULES`, `PROVENANCE_RULE`), jalur giliran di `conversation.py`/`agent.py`, serta `evals/test_trust_eval.py` dan `evals/runtime_evidence.py`.
- [ ] Ukur latency search Graphiti nyata (bukan storage sintetis) untuk 20 query dari fixture trust terhadap memori lokal uji; catat p50/p95. Ini menentukan apakah prefetch harus dimulai pada transkrip interim (Task 4).

## Task 1 — Prefetch evidence per giliran

**File:** `memory.py`, `assembly.py`, `persona.py`, modul giliran, tes terkait.

- [ ] Tes merah: (a) giliran dengan memori menghasilkan tepat satu panggilan model dan evidence muncul sebagai pesan konteks dengan format `render_evidence` yang sama; (b) system prompt byte-identik antar giliran; (c) timeout/kegagalan search → giliran tetap berjalan dengan evidence "unavailable", tercatat di journal, tanpa mengarang; (d) `brief_only` tidak memanggil search; (e) namespace/group dan filter pending/superseded/missing-source sama dengan tool recall; (f) cancellation giliran membatalkan search yang sedang berjalan.
- [ ] Ekstrak fungsi search yang sekarang ada di dalam tool menjadi fungsi bersama, sehingga tool dan prefetch memakai satu jalur filter/render. Query prefetch = ucapan final peserta ditambah konteks giliran sebelumnya secukupnya; tentukan dan uji batasnya.
- [ ] Timeout prefetch memakai `memory.recall.timeout_seconds` yang ada kecuali Task 0 membuktikan perlu batas terpisah; bila menambah key, dokumentasikan dan tambahkan ke `.env.example`.
- [ ] Sesuaikan `RECALL_RULES`/`PROVENANCE_RULE` agar merujuk evidence yang disediakan untuk giliran ini, tanpa melemahkan aturan atribusi, tanggal sumber, konflik, dan "tidak diketahui".

## Task 2 — Tool recall: pertahankan atau lepas

- [ ] Varian A: prefetch tanpa tool recall. Varian B: prefetch dengan tool recall tetap terpasang untuk pencarian lanjutan. Keduanya dibangun dari kode yang sama lewat konfigurasi atau parameter assembly yang teruji; jangan menduplikasi jalur.
- [ ] Pilih varian berdasarkan Task 3: B hanya dipertahankan bila p95 tetap ≤4 s dan kualitas lebih baik.

## Task 3 — Eval harness dan corpus baru

- [ ] Perbarui harness trust/brief agar evidence prefetch tercatat per giliran (query, hasil, durasi, error) di `turns.json`, dan rubric "recall wajib" dinilai dari evidence yang benar-benar tersedia bagi model, bukan dari ada/tidaknya tool call. Jangan melonggarkan kriteria; tambahkan tes bahwa giliran tanpa evidence padahal recall wajib tetap gagal.
- [ ] Jalankan corpus brief 12×3 dan trust 24×3 untuk `gemini-3.8-flash` dan `gemini-3.5-flash-lite`, varian A dan B, ke direktori baru `.runtime/recall-latency/<run>/`. Provenance kandidat dan runtime snapshot harus valid.
- [ ] Review semantik independen seluruh turn untuk setiap run yang lulus latency, dengan standar severity review `resume-a-r4-review.md` (contoh severe: "bukan Rani" untuk speaker tak dikenal, tanggal `recorded_at` sebagai tanggal event, tambahan "pagi", janji owner akan konfirmasi nanti).
- [ ] Laporkan tabel model × varian: severe, useful, p50/p95 giliran 1-call dan giliran ber-memori, jumlah panggilan model per giliran.

## Task 4 — Prefetch pada transkrip interim (opsional, berdasar Task 0)

- [ ] Hanya bila search nyata p95 >300 ms: mulai search pada interim yang stabil, batalkan bila final berbeda jauh, pakai hasil bila query final cocok. Tes: hasil basi tidak pernah dipakai untuk final yang berbeda; pembatalan tidak bocor.

## Task 5 — Cacat kecil dari review 4 Oktober

- [ ] **Sapaan c05:** `greetings._tokens` mengembalikan `None` untuk teks apa pun yang mengandung `?`, sehingga `Halo, Sam di sini, apa kabar?` tidak masuk rute netral lokal. Perbaiki agar sapaan berbentuk tanya tetap dikenali tanpa menangkap pertanyaan substantif; tes kedua arah dan kasus nama.
- [ ] **Debrief `"null"`:** Gemini mengirim string literal `"null"` pada `assignee_claim`/`due_text` (`DebriefDraft`, `debrief_schema.py:22-23`). Periksa schema yang benar-benar dikirim langchain-google-genai; utamakan perbaikan bentuk schema. Tambahkan validasi bahwa `assignee_claim` dan `due_text`, bila ada, merupakan substring kutipan evidence item; nilai `"null"`/`"unknown"` tidak boleh terbit.
- [ ] **Rubric fixture trust usang:** `contract_reply` c07/c14/c16/c23 dan rubric c17/c18/c19/c23 masih menyebut "cek ke Rani". Selaraskan dengan aturan sekarang tanpa melonggarkan; catat perubahan fixture agar corpus lama tidak dibandingkan langsung.

## Task 6 — Verifikasi akhir dan handoff live

- [ ] Gate statis + non-live/non-container, build image `docker compose --profile audio build meeting-worker`, `pytest -m container tests/test_worker_image.py`.
- [ ] Corpus debrief 8 kasus dengan role debrief `gemini-3.8-flash`, review independen, coverage ≥0,9 dan tanpa `"null"`.
- [ ] Rekomendasikan model live berdasarkan tabel Task 3. Siapkan skrip acceptance Meet dua sesi (≥20 jawaban) dan minta link Meet baru dari pengguna; jangan memakai link lama.
- [ ] Commit lokal per task yang direview. Akhiri dengan daftar commit, command aktual + exit code, artefak, gate yang lulus/gagal, dan risiko.

## Prompt siap salin untuk Muse

```text
Kerjakan docs/plans/recall-latency.md di repo /Users/pushmove/Desktop/jemboed/fakeyou.

Baca plan itu penuh, docs/reports/2026-10-02-latency-lessons.md, dan review .runtime/meeting-delegation/resume-g2-gemini-3.8-flash-review.md serta resume-g2-gemini-3.5-flash-lite-review.md sebelum mulai. Buat branch feat/recall-latency dari master 3450174; jangan reset, buang perubahan, push, atau merge.

Masalah: giliran yang memerlukan memori butuh dua perjalanan model (tool call lalu jawaban), sehingga p95 gemini-3.8-flash 5,9 s melewati gate 4 s, sedangkan model cepat melewati recall wajib. Solusi yang diminta: ambil evidence memori sebelum model dipanggil dan masukkan sebagai pesan konteks giliran (system prompt tetap statis), lewat satu jalur search/filter/render bersama dengan tool recall. Uji varian tanpa dan dengan tool recall; pilih berdasarkan data.

Aturan: Python Inggris tanpa komentar/docstring baru; dokumentasi Bahasa Indonesia; tanpa dependency baru; jangan mengubah .env atau artefak lama; brief_only tetap tanpa memori; gate kualitas dan latency tidak dilonggarkan; tanpa xfail atau penghapusan kasus. Evidence prefetch adalah data, bukan izin.

Tulis tes merah sebelum implementasi. Jalankan sendiri setiap command verifikasi; klaim hijau tanpa output aktual bukan bukti. Corpus live memakai direktori baru di .runtime/recall-latency/ dan direview independen seluruh turn dengan standar severity review R4. Catat progres di .runtime/recall-latency/status.json (task, status, command, exit code, artefak, gate terbuka); setelah compaction baca file itu sebelum lanjut.

Akhiri dengan commit lokal yang direview, tabel model × varian (severe, useful, p50/p95, panggilan model per giliran), rekomendasi model live, risiko, dan gate yang belum terbukti. Minta link Meet baru hanya setelah offline, provider, dan container siap.
```

Jalankan:

```sh
mkdir -p .runtime/recall-latency
muse exec --workspace /Users/pushmove/Desktop/jemboed/fakeyou --json --prompt-file .runtime/recall-latency/muse-prompt.txt
```

Salin blok prompt ke `.runtime/recall-latency/muse-prompt.txt` terlebih dahulu; periksa `muse exec --help` bila opsi berbeda.
