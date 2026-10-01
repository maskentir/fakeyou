# Integrasi kinerja percakapan 2 Oktober 2026

Tanggal: 2 Oktober 2026. Cabang: `feat/conversation-performance` (lokal, tanpa push/merge). Acuan: [rencana pengembangan](../plans/next-development.md), [laporan Task 4](2026-10-01-conversation-performance.md), [runbook benchmark](../runbooks/conversation-benchmark.md).

## Ringkasan

Task 1–3 selesai dan terintegrasi: bukti PCM pertama yang bertahan saat interupsi, pemisahan antrean/request model/recall per transkrip, serta artefak benchmark yang sebanding. Task 4 selesai sebagai laporan negatif: konfigurasi lama dipertahankan. Task 5 baru mencakup gate lokal; build container dan acceptance live menunggu host.

## Perubahan per deliverable

- `feat: record first PCM before synthesis completes`: `src/fakeyou/voice.py` mencatat `synthesis_pcm_emitted` pada chunk nonkosong pertama (chunk kosong dilewati); `src/fakeyou/taxonomy.py` menambah kata kerja `emitted`; regresi pada `tests/test_voice.py` dan `tests/test_taxonomy.py`.
- `feat: report correlated model and recall timings`: `src/fakeyou/conversation.py` meneruskan `transcript_id` ke callback model/tool; `src/fakeyou/report.py` memetakan request sintesis/model/tool ke giliran pemiliknya, menambah tahap `final_to_agent_seconds`/`agent_to_reply_seconds` serta field `llm_*`/`recall_*`/`incomplete`, dan mengecualikan bukti tak sah dengan alasan; regresi pada `tests/test_conversation.py` dan `tests/test_report.py`.
- `test: persist comparable conversation benchmark results`: `evals/test_replay_eval.py` menambah `turns` pada `ReplayScore` dan `write_replay_artifacts` (manifest tersamar, hash fixture SHA-256, commit eksplisit); `tests/test_conversation_benchmark.py` serta runbook baru.
- `docs: record conversation performance outcome and integration`: laporan Task 4 tanggal 1 Oktober, pembaruan checkbox rencana, dan laporan ini.

Berkas bersama `report.py`/`test_report.py` mendarat pada commit Task 2 karena hunk pemetaan sintesis (Task 1) dan korelasi model (Task 2) berada pada fungsi yang sama. Tidak ada perubahan `.env.example`, README, schema provider, threshold, atau data `acc2`/`acc3`.

## Hasil fungsi, kualitas, dan performa

- Fungsi: instrumentasi baru bekerja dan seluruh perilaku lama dipertahankan (interupsi, retry TTS, cleanup iterator, pembatalan generasi, fallback eksplisit). Lulus.
- Kualitas: threshold replay tidak diubah (wajib ≥ 0,9; ekor tanya ≤ 20%); ketiga konfigurasi benchmark gagal gate kualitas/latensi, dicatat sebagai gagal tanpa `xfail`. Belum lulus.
- Performa: waktu model mendominasi (median agent 3,927 dtk pada sesi `acc3-1001-s2`); tidak ada kandidat yang memenuhi p95 replay ≤ 4 dtk maupun target live. Milestone belum lulus; desain streaming teks per kalimat diperlukan sebelum perubahan alur.

## Benchmark (dari Task 4, artefak terverifikasi ada)

Fixture `acc-0930-replay.json` identik, 3 run/24 giliran per konfigurasi, total 72 giliran. Artefak `summary.json` + `turns.json` ada pada `.runtime/benchmarks/reply-baseline`, `reply-candidate`, dan `reply-candidate2`.

| Konfigurasi | p50 | p95 | Lulus |
| --- | --- | --- | --- |
| `MiniMax-M3` dasar | 3,399 | 6,630 | tidak |
| `MiniMax-M2.7-highspeed` | 3,798 | 6,623 | tidak |
| `MiniMax-M3` + `reasoning_effort` rendah | 2,585 | 10,143 | tidak |

## Verifikasi integrasi (perintah dan hasil aktual)

```sh
uv run ruff check .                                   # All checks passed!
uv run ruff format --check .                          # 112 files already formatted (setelah merapikan cuplikan rencana)
uv run mypy                                           # Success: no issues found in 39 source files
uv run pytest -m 'not live and not container' -q      # 1460 passed, 16 skipped, 20 deselected (68 tes browser ikut)
```

Bukti merah: dengan `src/fakeyou` dan `evals` dikembalikan ke HEAD sementara, 4 tes regresi baru gagal dan modul benchmark gagal impor (`write_replay_artifacts` belum ada); setelah dikembalikan, semuanya lulus. Baseline rencana pada `ff60109` adalah 1.403 tes; kenaikan menjadi 1.460 berasal dari regresi baru.

## Batas bukti

- Atribusi model per giliran pada sesi lama memakai urutan waktu manual karena journal lama belum membawa `transcript_id`.
- Benchmark replay mengukur respons agent, bukan PCM pertama; target live (median ≤ 3 dtk, p95 ≤ 6 dtk, ≥ 20 jawaban, 2 sesi) belum diuji ulang di Meet.
- Perbandingan TTS kandidat dan skenario dua pembicara belum diuji; butuh verifikasi suara oleh host.
- Tes container dan sesi live Task 5 belum dijalankan.

## Checkbox yang belum dicentang dan alasannya

- Task 4 TTS dan antrean: kondisi pemicu tidak terpenuhi (model mendominasi; antrean hanya tekanan balik) dan perbandingan TTS butuh host.
- Task 5 kecuali gate lokal: butuh build image, dua sesi Meet baru, uji barge-in/stop, dan konfirmasi host.
