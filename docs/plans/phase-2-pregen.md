# Phase 2 Pregen: Telemetri + Penekanan Restart — Implementation Plan

> **Untuk worker agen:** SUB-SKILL WAJIB: subagent-driven-development.
> Kerjakan plan task-per-task. Step memakai sintaks checkbox (`- [ ]`).

**Goal:** Ukur distribusi drift interim-vs-final pre-generation lalu tekan
restart sia-sia tanpa mengubah gerbang adopsi exact.

**Architecture:** Telemetri metadata-only di jurnal
(`pregeneration_started`/`pregeneration_finished` + seksi laporan
`pregeneration`), lalu flag terpisah menekan restart bila drift interim
imaterial (ekstensi sufiks ≤2 kata, atau jarak ≤1 pada n ≥ 6);
gerbang final tetap exact sehingga konten salah tak bisa bocor.

**Tech Stack:** Python, pytest serial, ruff, mypy.

**Spec:** [2026-10-07-fase-2-research.md](../reports/2026-10-07-fase-2-research.md)
(bagian 4) dan [long-term-architecture.md](long-term-architecture.md).
Pelaksana membaca kedua dokumen itu plus plan ini.

## Global Constraints

- Python Inggris tanpa komentar/docstring dan tanpa string Bahasa Indonesia
  di `.py`; dokumentasi Bahasa Indonesia.
- Tanpa dependency baru; tanpa protokol privat Meet; tanpa teks transkrip
  di jurnal (metadata angka/enum saja).
- Semua flag baru default mati; perilaku flag-mati identik bita dengan
  perilaku hari ini.
- Tes merah dulu; pytest serial (`-p no:cacheprovider`, tanpa `-n`).
- Tiap task direview reviewer terpisah sebelum commit lokal.

---

### Task 1: Telemetri Step-0 + seksi laporan pregeneration

**Files:**
- Modify: `src/fakeyou/utterance_quality.py`
- Modify: `src/fakeyou/conversation.py` (sekitar `_discard_pregen`,
  `_maybe_adopt_pregen`, cabang post-stop `handle_interim`)
- Modify: `src/fakeyou/report.py` (loop parse + seksi output)
- Test: `tests/test_utterance_quality.py`
- Test: `tests/test_conversation.py`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: `normalize_words`, `_word_edits` (sudah ada di
  `utterance_quality.py`).
- Produces:
  `describe_word_diff(old: Sequence[str], new: Sequence[str]) -> dict[str, float | int | str]`
  dengan kunci persis `dist`, `words_pre`, `words_final`,
  `norm_dist_bucket`, `diff_class`. `diff_class` salah satu dari
  `exact`, `prefix_extend`, `prefix_truncate`, `single_subst`, `other`.
  `norm_dist_bucket` = `dist / max(len, 1)` dibulatkan ke kelipatan
  0,05 (dua desimal; 0,0 bila kedua sisi kosong). Task 2 memakai
  fungsi ini apa adanya.

Aturan `diff_class` (deterministik, tanpa allowlist kata):
`exact` bila sama; `prefix_extend` bila `new` lebih panjang dan
diawali `old`; `prefix_truncate` bila `old` lebih panjang dan
diawali `new`; `single_subst` bila panjang sama dan tepat satu
posisi beda; selain itu `other`.

- [ ] **Step 1: tulis tes merah klasifikasi diff**

```python
def test_describe_word_diff_classes() -> None:
    assert describe_word_diff(("a", "b"), ("a", "b")) == {
        "dist": 0,
        "words_pre": 2,
        "words_final": 2,
        "norm_dist_bucket": 0.0,
        "diff_class": "exact",
    }
    extended = describe_word_diff(("a", "b"), ("a", "b", "c"))
    assert (extended["diff_class"], extended["dist"]) == ("prefix_extend", 1)
    truncated = describe_word_diff(("a", "b", "c"), ("a", "b"))
    assert (truncated["diff_class"], truncated["dist"]) == ("prefix_truncate", 1)
    subst = describe_word_diff(("a", "b"), ("a", "c"))
    assert (subst["diff_class"], subst["dist"]) == ("single_subst", 1)
    other = describe_word_diff(("a", "b"), ("c", "d"))
    assert (other["diff_class"], other["dist"]) == ("other", 2)
    assert other["norm_dist_bucket"] == 1.0
```

- [ ] **Step 2: jalankan tes, pastikan gagal**

Run:
`.venv/bin/python -m pytest tests/test_utterance_quality.py -q -p no:cacheprovider`
Expected: FAIL (`describe_word_diff` belum ada).

- [ ] **Step 3: implementasi minimal `describe_word_diff`**

```python
def describe_word_diff(
    old: Sequence[str], new: Sequence[str]
) -> dict[str, float | int | str]:
    before = tuple(old)
    after = tuple(new)
    if before == after:
        diff_class = "exact"
    elif len(after) > len(before) and after[: len(before)] == before:
        diff_class = "prefix_extend"
    elif len(before) > len(after) and before[: len(after)] == after:
        diff_class = "prefix_truncate"
    elif len(before) == len(after) and sum(
        first != second for first, second in zip(before, after)
    ) == 1:
        diff_class = "single_subst"
    else:
        diff_class = "other"
    dist = _word_edits(before, after)
    size = max(len(before), len(after))
    bucket = round(round(dist / size / 0.05) * 0.05, 2) if size else 0.0
    return {
        "dist": dist,
        "words_pre": len(before),
        "words_final": len(after),
        "norm_dist_bucket": bucket,
        "diff_class": diff_class,
    }
```

- [ ] **Step 4: jalankan tes, pastikan lulus**

Run: perintah Step 2. Expected: PASS.

- [ ] **Step 5: tulis tes merah jurnal diff di conversation**

Kasus: (a) `superseded` membawa lima kunci diff yang cocok dengan
`describe_word_diff(pregen.words, fresh)`; (b) `mismatched`
membawa lima kunci diff final-vs-pregen; (c) `adopted` membawa
`dist == 0` dan `diff_class == "exact"`; (d) discard tanpa
pembanding (alasan `error`) TIDAK membawa kunci diff; (e) tak ada
nilai jurnal baru yang memuat teks transkrip (semua nilai
`int`/`float`/`str` enum, dan untuk kunci `diff_class` nilainya
termasuk lima enum). Ikuti pola fixture conversation yang sudah
ada di `tests/test_conversation.py` untuk menyalakan pregen
(flag on, responder streaming, sesi idle).

- [ ] **Step 6: jalankan tes, pastikan gagal**

Run:
`.venv/bin/python -m pytest tests/test_conversation.py -q -p no:cacheprovider -k pregen`
Expected: FAIL pada kasus baru.

- [ ] **Step 7: implementasi minimal jurnal diff**

Ubah `_discard_pregen(self, reason: str, compared: tuple[str, ...] | None = None)`:
bila `compared` bukan None, gabungkan `describe_word_diff(pregen.words, compared)`
ke `_record("pregeneration_finished", ...)`. Situs panggil:
cabang post-stop `handle_interim` meneruskan `fresh` untuk
`superseded`; `_maybe_adopt_pregen` meneruskan
`normalize_words(event.text)` untuk `mismatched` dan untuk event
`adopted`; alasan lain (`error`, kegagalan task) meneruskan None.
Jangan ubah kondisi adopsi exact.

- [ ] **Step 8: jalankan tes, pastikan lulus**

Run: perintah Step 6. Expected: PASS.

- [ ] **Step 9: tulis tes merah seksi laporan**

```python
def test_session_report_summarizes_pregeneration() -> None:
    rows = [
        {"event": "pregeneration_started", "request_id": "a", "words": 4},
        {
            "event": "pregeneration_finished",
            "request_id": "a",
            "outcome": "discarded",
            "reason": "superseded",
            "dist": 1,
            "words_pre": 4,
            "words_final": 5,
            "norm_dist_bucket": 0.2,
            "diff_class": "prefix_extend",
        },
        {"event": "pregeneration_started", "request_id": "b", "words": 5},
        {
            "event": "pregeneration_finished",
            "request_id": "b",
            "outcome": "adopted",
            "duration_ms": 3000.0,
            "dist": 0,
            "words_pre": 5,
            "words_final": 5,
            "norm_dist_bucket": 0.0,
            "diff_class": "exact",
        },
    ]
    section = build_session_report(rows)["pregeneration"]
    assert section["starts"] == 2
    assert section["outcomes"] == {"discarded": 1, "adopted": 1}
    assert section["reasons"] == {"superseded": 1}
    assert section["diff_classes"] == {"prefix_extend": 1, "exact": 1}
    assert section["adopted_overlap_seconds"] == {
        "count": 1,
        "p50": 3.0,
        "p95": 3.0,
        "max": 3.0,
    }
```

Kunci `norm_dist_buckets` dihitung dari `str(nilai bucket)`:
`{"0.2": 1, "0.0": 1}` untuk rows di atas; assert itu juga.

- [ ] **Step 10: jalankan tes, pastikan gagal**

Run:
`.venv/bin/python -m pytest tests/test_report.py -q -p no:cacheprovider -k pregeneration`
Expected: FAIL (seksi belum ada).

- [ ] **Step 11: implementasi minimal seksi laporan**

Di loop parse `report.py`: `pregeneration_started` menaikkan
`starts`; `pregeneration_finished` menaikkan
`outcomes[outcome]`, `reasons[reason]` bila outcome
`discarded`, `diff_classes[diff_class]` bila kunci ada,
`norm_dist_buckets[str(bucket)]` bila kunci ada, dan menambah
`duration_ms / 1000` ke list overlap bila outcome `adopted`.
Output:

```python
"pregeneration": {
    "starts": starts,
    "outcomes": dict(outcomes),
    "reasons": dict(reasons),
    "diff_classes": dict(diff_classes),
    "norm_dist_buckets": dict(norm_dist_buckets),
    "adopted_overlap_seconds": summarize(overlaps),
},
```

- [ ] **Step 12: jalankan tes, pastikan lulus**

Run: perintah Step 10. Expected: PASS.

- [ ] **Step 13: gate task**

Run: tiga file tes di atas penuh (tanpa `-k`), `ruff check` dan
`ruff format --check` pada file yang diubah, `mypy` pada
`src/fakeyou/utterance_quality.py src/fakeyou/conversation.py src/fakeyou/report.py`.
Expected: semua hijau.

- [ ] **Step 14: review + commit**

Reviewer terpisah memeriksa diff Task 1 saja (telemetri,
tanpa teks transkrip di jurnal, perilaku flag-mati tak berubah).
Lalu:

```bash
git add src/fakeyou/utterance_quality.py src/fakeyou/conversation.py src/fakeyou/report.py tests/test_utterance_quality.py tests/test_conversation.py tests/test_report.py
git commit -m "feat(pregen): step-0 diff telemetry and report section"
```

### Task 2: D1 penekanan restart fuzzy (flag terpisah)

**Files:**
- Modify: `src/fakeyou/settings.py`
- Modify: `src/fakeyou/conversation.py` (ctor + cabang post-stop
  `handle_interim`)
- Modify: `src/fakeyou/assembly.py` (wiring flag)
- Test: `tests/test_conversation.py` (termasuk kasus (g) pola
  `test_sentence_streaming_default_follows_settings_flag`)

**Interfaces:**
- Consumes: `describe_word_diff` dari Task 1; `DEFAULTS.conversation`.
- Produces: setting `pregen_fuzzy_restart_enabled: bool = False`
  (env `FAKEYOU_CONVERSATION__PREGEN_FUZZY_RESTART_ENABLED`),
  parameter ctor `Conversation(..., pregen_fuzzy_restart_enabled: bool = DEFAULTS.conversation.pregen_fuzzy_restart_enabled)`,
  event jurnal `pregeneration_restart_suppressed` dengan kunci
  `request_id` + lima kunci diff.

Aturan penekanan (terkunci, flag on, `fresh` tak kosong dan beda
dari `pregen.words`): HITUNG `stats = describe_word_diff(pregen.words, fresh)`;
TEKAN (tanpa restart) bila (`diff_class == "prefix_extend"` DAN
`words_final - words_pre <= 2`) ATAU (`dist <= 1` DAN
`max(words_pre, words_final) >= 6`). Saat menekan:
`pregen.words = fresh`, jurnal `pregeneration_restart_suppressed`.
Selain itu: perilaku lama (discard `superseded` + begin baru).
Flag off: perilaku lama untuk semua drift, tanpa event baru.
Gerbang adopsi final tetap exact, tak tersentuh task ini.

- [ ] **Step 1: tulis tes merah penekanan**

Kasus (pola fixture pregen yang sama dengan Task 1):
(a) flag on + interim tumbuh satu kata sufiks → TIDAK restart
(`request_id` pregen sama, task lama hidup), `pregen.words`
diperbarui, satu event `pregeneration_restart_suppressed`
dengan `diff_class == "prefix_extend"`;
(b) flag on + substitusi kata-isi (`single_subst`) → restart
(`request_id` baru, satu `superseded`);
(c) flag on + drift `dist == 1` pada `n < 6` yang BUKAN
ekstensi sufiks (mis. satu kata beda pada dua kata) → restart;
(d) flag on + drift `dist == 1`, `n >= 6` → ditekan;
(e) flag off + ekstensi satu kata → restart (perilaku lama),
tanpa event `pregeneration_restart_suppressed`;
(f) pregen yang ditekan hanya diadopsi bila final exact
(final beda → `mismatched`, tanpa adopsi);
(g) cerminkan `test_sentence_streaming_default_follows_settings_flag`
(`tests/test_conversation.py:1207`): default ctor
`pregen_fuzzy_restart_enabled` mengikuti
`DEFAULTS.conversation.pregen_fuzzy_restart_enabled` yang False.

- [ ] **Step 2: jalankan tes, pastikan gagal**

Run:
`.venv/bin/python -m pytest tests/test_conversation.py -q -p no:cacheprovider -k "pregen and (suppress or fuzzy or restart)"`
Expected: FAIL (flag dan event belum ada). Bila `-k` tak cocok
karena nama tes berbeda, jalankan file penuh dan tunjukkan
kasus baru yang gagal.

- [ ] **Step 3: implementasi minimal**

`settings.py`: tambah `pregen_fuzzy_restart_enabled: bool = False`
di `ConversationSettings`. `conversation.py`: parameter ctor +
`self._pregen_fuzzy_restart_enabled`; di cabang post-stop
`handle_interim`, ganti perbandingan langsung dengan logika
aturan penekanan di atas. `assembly.py`: teruskan
`settings.conversation.pregen_fuzzy_restart_enabled`.
Event jurnal metadata-only (tanpa teks).

- [ ] **Step 4: jalankan tes, pastikan lulus**

Run: perintah Step 2 (atau file penuh). Expected: PASS.

- [ ] **Step 5: gate task**

Run: `tests/test_conversation.py` penuh, `ruff check` dan
`ruff format --check` pada file yang diubah, `mypy` pada
`src/fakeyou/settings.py src/fakeyou/conversation.py src/fakeyou/assembly.py`.
Expected: semua hijau.

- [ ] **Step 6: review + commit**

Reviewer terpisah (orang berbeda dari reviewer Task 1)
memeriksa diff Task 2 saja (aturan penekanan, flag default
mati, gerbang exact utuh). Lalu:

```bash
git add src/fakeyou/settings.py src/fakeyou/conversation.py src/fakeyou/assembly.py tests/test_conversation.py
git commit -m "feat(pregen): fuzzy restart suppression behind flag"
```

### Task 3: Ukur live twin-only + keputusan

Bukan task kode. Setelah Task 1–2 merge: sesi twin-only
berskrip dengan `pre_generate_enabled` on (D1 on di sesi kedua),
kumpulkan seksi `pregeneration` dari laporan, bandingkan dengan
baseline f1p-11 (restart 58%, adopsi 42%, hemat p50 0,03 dtk).
Kriteria D1 lolos: laju restart turun, adopsi naik, p50 overlap
naik, nol konten-salah. Butuh link Meet dari pemilik saat probe
siap. Hasil ditulis sebagai adendum laporan acceptance.
Fuzzy-final allowlist TETAP di luar scope sampai data Step-0
menunjukkan volume mismatch yang material.

## Self-Review

- Cakupan spec: telemetri (§6.1 riset) → Task 1; D1 (§4.2) →
  Task 2; D2 ditunda, D3 ditolak, D4 higiene thread dicatat
  sebagai follow-up M–L di luar plan ini; fuzzy-final di luar
  scope sampai data ada. Tak ada gap.
- Placeholder: tak ada TBD/TODO/validasi generik; semua step
  berisi kode atau perintah dan nilai harapan eksplisit.
- Konsistensi tipe: `describe_word_diff` didefinisikan di Task 1
  dengan signature persis yang dikonsumsi Task 2; nama event
  jurnal (`pregeneration_restart_suppressed`) dan kunci diff
  identik di kedua task; nama setting/flag/parameter ctor
  konsisten (`pregen_fuzzy_restart_enabled`).
