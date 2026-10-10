# Streaming jawaban per kalimat: desain serta rencana

**Status:** desain; tanpa perubahan source pada tahap ini. Pekerjaan berikut memakai cabang `feat/conversation-performance`; tanpa operasi git oleh pekerja. Acuan: [rencana pengembangan](next-development.md), [acceptance live](../reports/2026-10-02-live-acceptance.md), [laporan Task 4](../reports/2026-10-01-conversation-performance.md), [runbook benchmark](../runbooks/conversation-benchmark.md).

## 1 Ringkasan keputusan

Bukti ukur menunjukkan streaming jawaban per kalimat hanya menghemat median 0,074 detik, karena teks jawab pertama muncul pada median 93,7% durasi balasan penuh. Tunggu dominan berada sebelum teks pertama keluar, bukan pada ekor kalimat. Desain ini tetap disusun sebagai eksperimen terkendali beserta kriteria henti, bukan sebagai klaim kelulusan target. Bila delta ukur ulang tidak lebih baik dari alur lama, eksperimen dihentikan serta flag dimatikan.

## 2 Bukti pengukuran

### 2.1 Journal live S1+S2: 23 jawaban utuh dihitung ulang

| Tahap | p50 (dtk) | p95 (dtk) | Maks (dtk) |
| --- | --- | --- | --- |
| Agent → balasan | 1,498 | 6,497 | 6,758 |
| Balasan → PCM pertama | 1,145 | 2,468 | belum dicatat |
| Akhir ucapan → PCM pertama | 3,243 | 8,319 | 8,930 |

Target live (median ≤ 3 detik, p95 ≤ 6 detik) tidak terpenuhi. Waktu model mencapai 98,4% waktu agent. Dari sisi TTS, mulai-sintesis → PCM pertama berp50 1,137 detik serta berp95 2,458 detik; residu antara dua agregat hanya 0,008/0,011 detik, sehingga pemisahan tahap sahih.

### 2.2 Probe stream live: 6 giliran fixture replay

Metode: `astream` mode pesan + pewaktu callback, peran jawab via Settings, fixture `acc-0930-replay`, deteksi kalimat via `match_endofsentence`, teks pra-tool dikecualikan via cutoff `tool_end`.

| Giliran | Balasan penuh (dtk) | Teks pertama (dtk) | Kalimat pertama (dtk) | Hemat (dtk) |
| --- | --- | --- | --- | --- |
| s1-0 | 4,401 | 4,198 | 4,395 | 0,006 |
| s1-2 | 13,210 | 12,669 | 12,855 | 0,355 |
| s1-1 | 3,995 | 3,518 | 3,992 | 0,003 |
| s2-0 | 4,042 | 3,838 | 4,018 | 0,024 |
| s1-3 | 1,910 | 1,650 | 1,733 | 0,177 |
| s2-1 | 2,705 | 2,503 | 2,581 | 0,124 |

Agregat: teks jawab pertama pada median 93,7% (rentang 86,4–95,9%) durasi balasan penuh; kalimat pertama siap pada median 98,4% (rentang 90,7–99,9%); hemat overlap median 0,074 detik, maks 0,355 detik. Giliran s1-2 memanggil recall tak terduga (2 leg model, 13,21 detik).

Validasi silang:

- Dua jalur independen sepakat dalam 1 mdet pada giliran bersih (chunk mode pesan lawan callback token baru).
- Cutoff `tool_end` terbukti perlu: s1-2 bertoken mentah pertama 6,659 detik lawan teks jawab pertama 12,669 detik (selisih 6,0 detik pra-jawab).
- Rentang stream penuh 1,91–13,21 detik, median ~4,0 detik, konsisten pada replay `ainvoke` p50 3,399 serta p95 6,63 — stream sah sebagai proksi ukur.
- Chunking kasar: 2–18 chunk pesan lawan 7–115 token callback.

Artefak: `/tmp/measure_stream.py`, `/tmp/stream-measure.json` (4 giliran), `/tmp/stream-measure-2.json` (2 giliran); metadata saja, tanpa pola nilai secret; repo tak tersentuh.

### 2.3 Kesimpulan bukti

Tunggu dominan pra-teks-pertama (~94% waktu agent: TTFT model termasuk reasoning tersembunyi + leg pra-jawab). Streaming kalimat hanya overlap ~0,07 detik median → hasil negatif; tidak menutup gap p95. Desain bawah memakai temuan ini sebagai batas atas hemat.

## 3 Sasaran serta non-sasaran

Sasaran: ucapkan kalimat pertama segera setelah siap tanpa mengubah isi, persona, recall, fallback, interupsi, maupun stop. Non-sasaran: tweak prompt agar kalimat pertama keluar lebih cepat; ubah batas kalimat; ganti TTS; tambah framework tracing, CLI benchmark, atau refactor luar alur jawab.

## 4 Desain

### 4.1 Deteksi batas kalimat

- Pemisah: `match_endofsentence` dari `pipecat.utils.string`, bahasa dari `Settings.speech.language` — pemisah sama seperti `cap_spoken_reply` pada [conversation.py](../../src/fakeyou/conversation/conversation.py). Perilaku terverifikasi: mengembalikan indeks batas (> 0) atau 0 bila belum ada batas.
- Buffer inkremental: tiap potong teks jawab ditambah ke buffer; tiap penambahan, panggil pemisah; bila batas > 0, potong kalimat, kirim ke antre ucap, lanjutkan sisa buffer.
- Guard: hanya teks pasca-`tool_end` masuk buffer; kalimat kosong dilewati; hormati `max_spoken_sentences` (hentikan stream setelah kalimat kedua siap + catat `reply_capped`); sisa akhir tanpa titik diucapkan sebagai kalimat terakhir bila tak kosong.
- Tanpa pemisah baru, tanpa regex lokal, tanpa pustaka tambahan.

```python
buffer = ""
async for piece in answer_pieces():
    buffer += piece
    boundary = match_endofsentence(buffer, language=language)
    if boundary:
        sentence, buffer = buffer[:boundary].strip(), buffer[boundary:].strip()
        if sentence:
            await speak_sentence(sentence)
```

### 4.2 Alur ucap per kalimat

- Responder streaming baru di samping `LangGraphResponder`; kontrak `ConversationResponder` lama tetap untuk alur non-stream. Responder baru memakai `agent.astream(..., stream_mode="messages")`, hanya node `model`, cutoff `tool_end` — persis metode probe bukti 2.2.
- Tiap kalimat siap → `session.speak(kalimat)` berurutan dalam generasi sama; TTS per kalimat memakai synthesizer lama pada [voice.py](../../src/fakeyou/audio/voice.py); journal `synthesis_*` per kalimat beserta `sentence_index`.
- Urutan audio dijamin antre [playback.py](../../src/fakeyou/audio/playback.py); tidak ada ucap paralel.
- Timeout respons lama tetap membungkus seluruh giliran; stream di dalam timeout sama.

### 4.3 Persona, recall, fallback tetap utuh

- Recall tuntas sebelum kalimat pertama: tool call memblokir stream jawab; tidak ada ucap selama model masih memanggil tool; cutoff `tool_end` membuang teks pra-tool dari buffer (bukti s1-2: selisih 6,0 detik).
- Persona, prompt sistem, aturan kewenangan, provenance, ending rule: tidak berubah; `build_system_prompt` sama; `allowed_tools` sama.
- Fallback eksplisit via `ModelFallbackMiddleware` pada [assembly.py](../../src/fakeyou/orchestration/assembly.py) tetap; peran `reply_fallback` via Settings tetap; budget respons tetap.
- Cap dua kalimat tetap: kalimat ketiga serta sisanya tidak diucapkan; metrik cap tetap dicatat.

### 4.4 Pembatalan generasi usang per kalimat

- Sebelum tiap `speak` kalimat: cek `generation == self._generation` serta belum `close`; bila usang, batalkan `astream`, buang antre kalimat, kembali ke loop tanpa ucap — pola cek usang sama seperti alur lama sesudah `respond`.
- Pembatalan merambat: timeout respons, `close()`, transkrip baru — semua membatalkan stream berjalan.
- Journal: `agent_cancelled` tetap; tambah `sentence_dropped` (source `agent`, field `request_id`, `transcript_id`, `sentence_index`, `detail`; tanpa isi teks) bila kalimat dibuang karena usang.

### 4.5 Barge-in serta stop per kalimat

- `stop_playback` lama menghentikan audio berjalan; stream teks juga dibatalkan agar tidak ada kalimat susulan masuk antre setelah stop.
- Stop antar-kalimat: tidak ada ucap kalimat berikut setelah stop; tidak ada PCM/playback generasi usang setelah stop — kriteria sama seperti acceptance lama.
- Barge-in interim/final tetap memicu `stop_playback` saat `is_speaking`; tambah uji stop tepat pada jeda antar-kalimat.

### 4.6 Journal serta laporan

Event baru memakai kata kerja terkendali pada [taxonomy.py](../../src/fakeyou/foundation/taxonomy.py) (`emitted`, `dropped`; tanpa isi percakapan; durasi `_ms`):

- `answer_text_emitted` source `llm`: teks jawab pertama pasca-tool; field `request_id`, `transcript_id`.
- `answer_sentence_emitted` source `agent`: tiap kalimat siap diucapkan; field `request_id`, `transcript_id`, `sentence_index`, `chars`.
- `sentence_dropped` source `agent`: kalimat dibuang (usang/stop); field `request_id`, `transcript_id`, `sentence_index`, `detail`.

Tahap laporan baru pada [report.py](../../src/fakeyou/observability/report.py) (satuan `_seconds`, pola `_STAGE_BOUNDS` lama dipertahankan):

- `agent_to_first_text_seconds`, `agent_to_first_sentence_seconds`, `first_sentence_to_first_pcm_seconds`.
- Per giliran: `sentence_count`, `streamed` (nyala/mati).
- Alur lama: tahap baru bernilai `None` beserta hitung bukti dikecualikan beralasan; tahap lama tak berubah.

### 4.7 Komparabilitas replay

- Fixture sama (`acc-0930-replay.json`), run sama (3 run / 24 giliran), artefak sama (`summary.json`, `turns.json` via [eval replay](../../evals/test_replay_eval.py)) + field stream per giliran (`first_text_seconds`, `first_sentence_seconds`, `sentences_spoken`).
- Gate sama (pass ≥ 0,9; ekor tanya ≤ 20%; p95 ≤ 4 detik); tambah perbandingan delta kalimat-pertama lawan penuh sebagai bukti hemat, bukan gate baru.
- Manifest mencatat flag streaming (nyala/mati) agar artefak sebanding; hash fixture tetap SHA-256.

### 4.8 Rollback

- Flag `ConversationSettings.sentence_streaming: bool = False` pada [settings.py](../../src/fakeyou/config/settings.py) via Settings saja; mati = alur lama persis (`ainvoke` + satu `speak`).
- Uji regresi kedua jalur; rollback = matikan flag, tanpa deploy ulang source.

## 5 Rencana tugas

| Tugas | File milik | Acceptance |
| --- | --- | --- |
| 1. Responder streaming + buffer kalimat | `src/fakeyou/conversation/conversation.py`; `tests/test_conversation.py` | Fake `astream` multi-chunk menghasilkan kalimat sama seperti `cap_spoken_reply`; bahasa `id`/`en` lulus; tanpa `stream` tak ada perubahan perilaku |
| 2. Guard recall/tool + cap kalimat | `src/fakeyou/conversation/conversation.py`; `tests/test_conversation.py` | Teks pra-`tool_end` tak pernah diucapkan; recall gagal tetap fallback `unknown`; kalimat ketiga tak diucapkan + `reply_capped` tercatat |
| 3. Batal usang + stop antar-kalimat | `src/fakeyou/conversation/conversation.py`; `tests/test_conversation.py` | Transkrip baru di tengah stream membuang sisa tanpa ucap; stop antar-kalimat tanpa audio susulan; `sentence_dropped` tercatat |
| 4. Event journal baru | `src/fakeyou/conversation/conversation.py`; `tests/test_taxonomy.py` (otomatis) | Tiga event lolos cek verb, source, tanpa konten, `_ms`; metadata saja |
| 5. Tahap laporan baru | `src/fakeyou/observability/report.py`; `tests/test_report.py` | Tahap baru benar pada journal stream; `None` beralasan pada journal lama; tahap lama tak berubah |
| 6. Komparabilitas replay | `evals/test_replay_eval.py`; `tests/test_conversation_benchmark.py` | Field stream per giliran + flag pada manifest; gate sama; sampel 24/24 |
| 7. Flag Settings + rollback | `src/fakeyou/config/settings.py`; `tests/test_settings.py` | Default mati = alur lama bit-perilaku; nyala = alur stream; tanpa alias env lama |
| 8. Ukur live + putusan henti | Laporan bertanggal `docs/reports/` | ≥ 20 jawaban dua sesi; delta first-sentence vs penuh dilaporkan; putusan lanjut/henti eksplisit |

Urutan: 7 → 1 → 2 → 3 → 4 → 5 → 6 → 8. Tiap tugas: tulis regresi dulu (merah), implementasi minimal, gate lokal (`ruff check`, `ruff format --check`, `mypy`, `pytest -m 'not live and not container'`). Python Inggris tanpa komentar/docstring; tanpa sentuh `.env`, data `acc2`/`acc3`, backup, stash, persona privat; uji memori memakai namespace uji terpisah.

## 6 Target serta kejujuran gate

- Target live (median ≤ 3 detik, p95 ≤ 6 detik, ≥ 20 jawaban, dua sesi) berstatus belum terbukti sampai diukur ulang pada Tugas 8.
- Prediksi berdasar bukti 2.2: hemat median 0,074 detik tidak menutup gap p95 8,319 → 6 detik; eksperimen diprediksi gagal pada gate performa.
- Kriteria henti: bila delta kalimat-pertama → PCM tidak lebih baik dari alur lama pada fixture sama, hentikan; pertahankan flag mati; catat laporan negatif.
- Larangan: tanpa pelonggaran ambang, tanpa `xfail` baru untuk menyembunyikan gagal, tanpa hapus sampel lambat.

## 7 Risiko

- Chunking kasar provider (2–18 chunk) membuat kalimat siap hampir bersamaan balasan penuh; mitigasi: ukur dulu, kriteria henti tegas.
- TTS per kalimat menambah request (dua request pendek lawan satu); mitigasi: bandingkan mulai-sintesis → PCM per kalimat pada Tugas 8.
- Stream tak kompatibel pada fallback model tertentu; mitigasi: fallback ke `ainvoke` per giliran bila `astream` gagal, dicatat sebagai `agent_failed` stream lalu alur lama.

## 8 Koreksi pasca-acceptance (2 Oktober 2026)

Acceptance live menemukan `agent_finished` tercatat setelah seluruh speak sehingga pemetaan sintesis→giliran lewat `replying` mengaitkan PCM ke giliran berikut (e2e semu p50 ~15 dtk). Diperbaiki dengan `transcript_id` pada `session.speak` → `PlaybackEngine.speak` → `stream` → event `synthesis_started`/`synthesis_pcm_emitted`, dan report memakai tautan itu dengan fallback peta lama (commit `da09b49`, 5 regresi). Angka koreksi: streaming utuh n=18 median 2,918 dtk p95 8,046 dtk; baseline n=23 median 3,243 dtk p95 8,319 dtk. Putusan STOP per §6; flag tetap mati. Bukti: [acceptance streaming](../reports/2026-10-02-streaming-acceptance.md).
