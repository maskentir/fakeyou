# Kinerja percakapan 1 Oktober 2026

Tanggal: 1 Oktober 2026. Status: optimasi konfigurasi belum lulus; konfigurasi lama dipertahankan; tanpa perubahan source maupun default publik. Acuan: [rencana pengembangan](../plans/next-development.md), [runbook benchmark](../runbooks/conversation-benchmark.md), [acceptance refactor](2026-10-01-refactor-acceptance.md).

## Keputusan

Konfigurasi jawaban tetap `MiniMax-M3` pada endpoint kompatibel yang sama. Dua kandidat yang diuji memakai fixture identik tidak melewati seluruh gate: `MiniMax-M2.7-highspeed` tidak lebih cepat serta gagal pemeriksaan kualitas, sedangkan `MiniMax-M3` memakai `reasoning_effort` rendah memperbaiki median tetapi merusak ekor latensi serta kualitas. Berkas `.env.example`, README, serta schema provider tidak diubah. Laporan ini bersifat negatif sesuai rencana: milestone performa belum lulus.

## Bukti tahap pada sesi acceptance

Journal `acc3-1001-s2` dihitung ulang memakai kode laporan Task 1–3. Distribusi tahap dalam detik:

| Tahap | Sampel | p50 | p95 | Maks |
| --- | --- | --- | --- | --- |
| Akhir suara → transkrip final | 12 | 0,337 | 6,070 | 12,281 |
| Final → agent mulai | 12 | 0,002 | 3,810 | 4,783 |
| Agent → balasan | 11 | 3,927 | 12,562 | 13,253 |
| Final → balasan | 11 | 4,658 | 14,425 | 15,596 |
| Balasan → PCM pertama | 8 | 1,683 | 2,326 | 2,330 |
| Akhir suara → PCM pertama | 8 | 5,727 | 16,895 | 18,080 |

Rincian per giliran (antrean, agent, TTS, ujung-ke-ujung dalam detik; tanda `-` berarti bukti tidak ada):

| Giliran | Hasil | Recall | Antrean | Agent | TTS | Ujung-ke-ujung |
| --- | --- | --- | --- | --- | --- | --- |
| `f537b23b` | sunyi | tidak | 0,001 | 8,602 | `-` | `-` |
| `0715074c` | ucap | tidak | 3,014 | 3,927 | 1,820 | 9,137 |
| `9258aa41` | sunyi | tidak | 0,001 | 11,872 | `-` | `-` |
| `8df1444b` | sela | tidak | 4,783 | 10,813 | 2,330 | 18,080 |
| `19345680` | ucap | ya | 0,006 | 4,652 | 1,888 | 6,891 |
| `4ec07494` | ucap | tidak | 0,002 | 3,046 | 1,181 | 4,557 |
| `b2eadb23` | ucap | tidak | 0,002 | 2,028 | 0,849 | 3,351 |
| `4f36784d` | ucap | tidak | 0,002 | 13,253 | 1,118 | 14,695 |
| `23fbcbd2` | ucap | tidak | 0,001 | 2,008 | 2,319 | 4,563 |
| `c66dc819` | sela | tidak | 0,001 | 2,958 | `-` | `-` |
| `8e89359d` | sela | tidak | 0,001 | 2,268 | 1,546 | 4,076 |
| `b410fc1c` | batal | tidak | 0,002 | `-` | `-` | `-` |

Temuan pemilahan:

- Waktu model mendominasi. Durasi request model mentah per giliran: 8,583; 3,911; 11,859; 10,796; 2,504 + 1,954 pada giliran recall; 3,034; 2,013; 13,238; 1,995; 2,944; 2,256 detik. Median total model per giliran 3,911 detik, nyaris sama pada median agent 3,927 detik. Atribusi ini manual berdasar urutan waktu karena journal lama belum membawa `transcript_id` pada request model; laporan baru mencatat `llm_count` nol serta 14 bukti dikecualikan sebagai `missing_transcript`.
- Recall tidak mendominasi. Satu-satunya pemanggilan recall selesai dalam 0,156 detik pada giliran berdurasi agent 4,652 detik. Giliran recall tidak lebih lambat daripada giliran tanpa recall.
- TTS penyumbang kedua. Median balasan menuju PCM pertama 1,683 detik pada 8 sampel. Perbandingan provider TTS kandidat membutuhkan verifikasi suara utuh serta interupsi oleh host di Meet, sehingga belum dilakukan pada tahap ini.
- Antrean hanya pada dua giliran (3,014 serta 4,783 detik). Keduanya menunggu agent giliran sebelumnya yang masih bekerja karena respons model lambat. Ini tekanan balik dari model lambat, bukan cacat antrean tersendiri; tidak ada regresi antrean baru yang terbukti.
- Tidak ada timeout, fallback, maupun fault. Tidak ada `request_failed` atau `fault_recorded`. Satu-satunya pembatalan adalah giliran terakhir saat stop, request dibatalkan setelah 0,100 detik tanpa suara susulan. Interupsi tetap sebagai pembanding: median 1,618 detik serta maksimum 1,833 detik pada 3 sampel.

## Benchmark konfigurasi jawaban

Tiga konfigurasi diuji memakai fixture sama (`acc-0930-replay.json`, SHA-256 `1fa0197b234c36a6b2a12bc121e41ab37fa037b52ec0fec14959127a32a8ada6`), masing-masing 3 run atau 24 giliran, total 72 giliran. Perubahan hanya pada field `FAKEYOU_REPLY__*`. Urutan eksekusi berurutan (baseline, kandidat model, kandidat parameter); bias urutan tidak diukur.

| Konfigurasi | p50 | p95 | Terjawab | Recall | Sebut wajib | Sebut salah satu | Larangan | Ekor tanya | Dua kalimat | Lulus |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `MiniMax-M3` dasar | 3,399 | 6,630 | 1,000 | 0,833 | 0,857 | 0,833 | 1,000 | 0,083 | 0,958 | tidak |
| `MiniMax-M2.7-highspeed` | 3,798 | 6,623 | 1,000 | 1,000 | 1,000 | 0,750 | 1,000 | 0,125 | 0,958 | tidak |
| `MiniMax-M3` + `reasoning_effort` rendah | 2,585 | 10,143 | 1,000 | 0,667 | 0,857 | 0,750 | 1,000 | 0,250 | 0,958 | tidak |

Ambang: pass rate pemeriksaan wajib minimal 0,9; pertanyaan penutup maksimal 20%; p95 respons agent maksimal 4 detik. Ketiga konfigurasi gagal p95. Konfigurasi dasar gagal tiga pemeriksaan kualitas; kandidat model gagal satu pemeriksaan kualitas; kandidat parameter gagal empat gate sekaligus.

Artefak sebanding tersimpan pada direktori berikut:

- `.runtime/benchmarks/reply-baseline/test_live_replay_meets_quality0/`
- `.runtime/benchmarks/reply-candidate/test_live_replay_meets_quality0/`
- `.runtime/benchmarks/reply-candidate2/test_live_replay_meets_quality0/`

Masing-masing direktori memuat `summary.json` serta `turns.json`. Manifest memakai penyamaran secret; nilai secret tidak tersimpan.

Daftar model kandidat diperoleh dari endpoint jawaban yang dikonfigurasi, bukan dari tebakan nama: `MiniMax-M3`, `MiniMax-M2.7`, `MiniMax-M2.7-highspeed`, `MiniMax-M2.5`, `MiniMax-M2.5-highspeed`, `MiniMax-M2.1`, `MiniMax-M2.1-highspeed`, `MiniMax-M2`. Varian highspeed yang diuji tidak menunjukkan keunggulan latensi (p50 3,798 berbanding 3,399 detik), sehingga varian highspeed lain yang lebih lama tidak diuji.

## Perintah yang dijalankan

```sh
uv run pytest tests/test_voice.py tests/test_report.py tests/test_conversation.py tests/test_conversation_benchmark.py tests/test_taxonomy.py evals/test_replay_eval.py -m 'not live' -q
uv run --env-file .env pytest evals/test_replay_eval.py -m live -v -s --basetemp=.runtime/benchmarks/reply-baseline
FAKEYOU_REPLY__MODEL=MiniMax-M2.7-highspeed uv run --env-file .env pytest evals/test_replay_eval.py -m live -v -s --basetemp=.runtime/benchmarks/reply-candidate
FAKEYOU_REPLY__REASONING_EFFORT=low uv run --env-file .env pytest evals/test_replay_eval.py -m live -v -s --basetemp=.runtime/benchmarks/reply-candidate2
uv run ruff check .
uv run mypy
```

Hasil: 252 tes offline lulus, 1 tidak dipilih; ketiga benchmark live gagal gate tetapi artefak tersimpan; `ruff check` serta `mypy` bersih. Pemeriksaan format menandai satu berkas rencana Task 1–3 yang sudah ada sebelumnya (`docs/plans/next-development.md` baris 139); berkas tersebut di luar lingkup Task 4 sehingga tidak diubah.

## Batas bukti

- Journal acceptance lama belum memuat hubungan request menuju transkrip, sehingga atribusi durasi model per giliran memakai urutan waktu manual. Pengulangan memakai instrumentasi Task 2 akan memberi atribusi langsung.
- Benchmark replay mengukur respons agent, bukan PCM pertama. Target live (median maksimal 3 detik serta p95 maksimal 6 detik pada sedikitnya 20 jawaban dari dua sesi) belum diuji ulang di Meet.
- Gate recall hanya memiliki 6 sampel; satu kegagalan membuat nilai 0,833. Ambang tidak dilonggarkan sesuai kontrak.
- Kandidat TTS serta skenario dua pembicara belum diuji.

## Langkah desain berikut

Bukti di atas menunjukkan alur menunggu respons model lengkap sebagai penyebab dominan: agent nyaris identik pada satu request model, lalu `speak` dimulai. Karena tidak ada konfigurasi yang memenuhi gate, rencana mensyaratkan desain terpisah untuk streaming teks per kalimat sebelum perubahan alur dilakukan. Desain tersebut berada di luar Task 4. Acceptance live Task 5 membutuhkan keputusan tuan rumah: mengulang sesi memakai konfigurasi lama akan mengukur ulang gap yang sama, bukan menutupnya.
