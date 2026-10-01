# Streaming jawaban per kalimat: implementasi serta hasil

Tanggal: 2 Oktober 2026. Cabang: `feat/conversation-performance`. Acuan: [desain](../plans/sentence-streaming.md), [pelajaran latensi](2026-10-02-latency-lessons.md), [acceptance live](2026-10-02-live-acceptance.md).

**Status:** eksperimen terkendali terimplementasi beserta flag rollback; default mati sehingga alur lama tidak berubah. Hasil ukur bersifat negatif: hemat median 0,074 detik tidak menutup gap p95. Acceptance Live Meet di luar lingkup laporan ini; dijalankan orangtua secara interaktif bersama host.

## Pengukuran

### Journal live S1+S2: 23 jawaban utuh

| Tahap | p50 (dtk) | p95 (dtk) | Maks (dtk) |
| --- | --- | --- | --- |
| Agent → balasan | 1,498 | 6,497 | 6,758 |
| Balasan → PCM pertama | 1,145 | 2,468 | 2,538 |
| Akhir ucapan → PCM pertama | 3,243 | 8,319 | 8,930 |

Target live (median ≤ 3 detik, p95 ≤ 6 detik) tidak terpenuhi. Durasi request model mencapai 98,4% durasi agent. Pemisahan tahap TTS sahih: mulai-sintesis → PCM pertama berp50 1,137 detik serta berp95 2,458 detik; residu agregat hanya 0,008/0,011 detik.

### Probe stream live: 6 giliran fixture replay

Metode: `astream` mode pesan + pewaktu callback, peran jawab via Settings, fixture `acc-0930-replay`, deteksi kalimat via `match_endofsentence`, teks pra-tool dikecualikan via cutoff `tool_end`. Artefak: `/tmp/measure_stream.py`, `/tmp/stream-measure.json`, `/tmp/stream-measure-2.json`; metadata saja.

| Giliran | Balasan penuh (dtk) | Teks pertama (dtk) | Kalimat pertama (dtk) | Hemat (dtk) |
| --- | --- | --- | --- | --- |
| s1-0 | 4,401 | 4,198 | 4,395 | 0,006 |
| s1-2 | 13,210 | 12,669 | 12,855 | 0,355 |
| s1-1 | 3,995 | 3,518 | 3,992 | 0,003 |
| s2-0 | 4,042 | 3,838 | 4,018 | 0,024 |
| s1-3 | 1,910 | 1,650 | 1,733 | 0,177 |
| s2-1 | 2,705 | 2,503 | 2,581 | 0,124 |

Teks jawab pertama muncul pada median 93,7% durasi balasan penuh; kalimat pertama siap pada median 98,4%; hemat overlap median 0,074 detik, maks 0,355 detik. Dua jalur ukur independen sepakat dalam 1 mdet pada giliran bersih. Cutoff `tool_end` terbukti perlu (s1-2: selisih 6,0 detik pra-jawab).

## Keputusan desain

- Pemisah kalimat memakai `match_endofsentence` yang sama seperti `cap_spoken_reply`; tanpa pemisah baru.
- `StreamingLangGraphResponder` baru di samping `LangGraphResponder`; kontrak lama utuh. Stream hanya node `model`, mode pesan, cutoff `tool_end`.
- Recall tuntas sebelum kalimat pertama; teks pra-tool tidak pernah diucapkan; persona, prompt, kewenangan, provenance tidak berubah.
- Cap dua kalimat tetap; kalimat ketiga tidak diucapkan; `reply_capped` tetap dicatat.
- Pembatalan generasi usang dicek sebelum tiap `speak`; kalimat usang dicatat sebagai `sentence_dropped` (metadata saja) beserta `agent_cancelled`.
- Stop antar-kalimat: tidak ada ucap susulan; tidak ada PCM generasi usang setelah stop.
- Gagal `astream` sebelum kalimat pertama menghasilkan fallback ke `ainvoke` per giliran, dicatat sebagai `agent_failed` stream.
- Flag `ConversationSettings.sentence_streaming: bool = False` via Settings saja; mati berarti alur lama persis; rollback tanpa deploy ulang source.
- Event journal baru memakai kata kerja taksonomi lama (`emitted`, `dropped`): `answer_text_emitted`, `answer_sentence_emitted`, `sentence_dropped`; tanpa isi percakapan.
- Tahap laporan baru: `agent_to_first_text_seconds`, `agent_to_first_sentence_seconds`, `first_sentence_to_first_pcm_seconds`; per giliran `sentence_count` dan `streamed`; alur lama bernilai `None` beralasan.

## Perubahan

- `src/fakeyou/conversation.py`: responder streaming, buffer kalimat, guard tool, `_speak_streamed` dengan cek usang/stop/tutup, timeout seluruh giliran.
- `src/fakeyou/assembly.py`: memilih responder berdasar flag; meneruskan flag ke loop.
- `src/fakeyou/settings.py`, `.env.example`: flag `sentence_streaming` default mati.
- `src/fakeyou/audio.py`, `src/fakeyou/playback.py`, `src/fakeyou/session.py`: parameter `sentence_index` opsional pada `speak`/`stream`.
- `src/fakeyou/voice.py`: journal `synthesis_*` dan `synthesis_pcm_emitted` membawa `sentence_index`.
- `src/fakeyou/report.py`: tiga tahap stream, `sentence_count`, `streamed`.
- `tests/`: regresi responder streaming, guard recall, cap, batal usang, stop antar-kalimat, tahap laporan, wiring flag; `fakes.py` mencatat indeks kalimat.
- `docs/plans/sentence-streaming.md`: desain; `docs/reports/2026-10-02-latency-lessons.md`: kajian ukur; `docs/plans/next-development.md`: penunjuk ke kajian.

## Hasil fungsi

- `ruff check .`: lulus. `ruff format --check .`: 116 berkas sudah format. `mypy`: bersih pada 39 berkas source.
- `pytest -m 'not live and not container'`: 1548 lulus, 18 dilewati, 20 tidak dipilih. Baseline rencana 1.403; selisih +145 berasal dari regresi streaming baru.
- Berkas tersentuh saja: 340 lulus.
- Batas kalimat, otoritas persona, perilaku recall, fallback eksplisit, pembatalan generasi usang, dan stop tanpa audio susulan dipertahankan beserta regresinya.
- Journal hanya metadata: jumlah karakter, indeks, ID, waktu; tanpa teks percakapan, hasil recall, audio, prompt, atau secret.

## Hasil kualitas

- Gate replay tidak berubah: pass ≥ 0,9; ekor tanya ≤ 20%; p95 ≤ 4 detik. Tanpa pelonggaran ambang, tanpa `xfail` baru, tanpa hapus sampel lambat.
- Uji replay offline: 2 lulus, 1 live tidak dipilih.
- Uji replay live dan acceptance Live Meet belum dijalankan; di luar lingkup alur ini.
- Field stream per giliran dan flag pada manifest replay (Tugas 6 desain) belum diimplementasi; perbandingan delta kalimat-pertama lawan penuh menunggu Tugas 8.

## Hasil performa

Hasil negatif, dilaporkan apa adanya:

- Hemat overlap median 0,074 detik tidak menutup gap p95 8,319 → 6 detik. Tunggu dominan berada sebelum teks pertama (~94% waktu agent), bukan pada ekor kalimat.
- Prediksi: eksperimen diprediksi gagal pada gate performa live sampai dibuktikan sebaliknya pada ukur ulang.
- Kriteria henti berlaku: bila delta kalimat-pertama → PCM tidak lebih baik dari alur lama pada fixture sama, hentikan eksperimen dan pertahankan flag mati.

## Kesiapan live

- Siap sebagai eksperimen terkendali: default mati, rollback via Settings, kontrak perilaku lama utuh.
- Belum siap sebagai klaim kelulusan target: butuh ≥ 20 jawaban dua sesi dengan flag nyala, dilaporkan terpisah fungsi/kualitas/performa.

## Batas

- Ucap kalimat dilakukan sekuensial (`await speak` per kalimat): konsumsi token berhenti selama audio kalimat pertama diputar. Desain memilih ini demi urutan dan stop yang pasti; overlap penuh butuh antrean producer-konsumen sebagai tindak lanjut.
- Chunking provider kasar (2–18 chunk pesan) membuat kalimat siap hampir bersamaan balasan penuh.
- TTS per kalimat menambah request (dua pendek lawan satu); bandingkan mulai-sintesis → PCM per kalimat pada ukur live.
- Sampel probe kecil (6 giliran, satu fixture); sampel journal dari dua sesi satu host; tanpa speaker multipihak atau beban bersamaan.
