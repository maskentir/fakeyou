# Spike Voiceprint Offline: CAM++ (3D-Speaker) via sherpa-onnx

Tanggal: 2026-10-07. Metode: spike offline read-only
terhadap repo (tanpa perubahan `src/`/`tests/`/`.env`/`.runtime`);
repronya satu-satunya artefak repo dan dibiarkan UNCOMMITTED.
Persetujuan pemilik: sherpa-onnx sebagai calon dependensi
opsional, model embedding CAM++ 3D-Speaker voxceleb sejak awal.

## Ringkasan eksekutif

CAM++ 3D-Speaker voxceleb (ONNX 28,2 MiB) berjalan murah di
CPU (~5,5 mdetik per detik audio, satu thread) dan memisahkan
3 speaker uji dengan benar pada segmen penuh lewat skor
sentroid-enrollment (6/6 benar). Batas tegasnya: **ujaran
1 detik tidak dapat dipakai** (2/6 benar, skor ≈ rata-rata
beda-speaker) dan **3 detik marginal** (4/6 benar); gerbang
durasi minimum wajib. Operasi precision-first yang disarankan
sementara: ambang kosinus θ ∈ [0,55, 0,65] terhadap sentroid
enrollment + margin minimum ∈ [0,10, 0,20] + tolak segmen
uji <3 detik + unknown-by-default. Semua angka n-kecil
(3 speaker, 15 klip, semuanya bernada rendah) — kalibrasi
live butir 19 tetap wajib sebelum ship.

## 1. Versi dan artefak

| Artefak | Nilai |
| --- | --- |
| sherpa-onnx (+ core) | 1.13.8 saat spike (versi ship: 1.12.38, cocok ORT 1.24.x pipecat) |
| Python / numpy / soundfile | 3.12.13 / 2.5.3 / ok |
| Host ukur | Apple M4 (arm64), macOS; angka waktu = aproksimasi vs worker Linux |
| Model | `3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx`, dim 512 |
| URL model | `https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx` |
| Ukuran / sha256 | 29.596.978 byte (~28,2 MiB) / `357a834f…4129b` |
| Data uji | `openslr/librispeech_asr`, config `clean`, split `test` (test-clean, domain publik) |
| Speaker × klip | 1320 / 5639 / 6930 × 5 klip 16 kHz (bab: 1320→2 bab, 6930→3 bab, 5639→1 bab) |
| Lokasi spike | `/tmp/f2-spike` (model + 15 wav + `run.py`/`run2.py`; fana, bukan artefak repo) |

Klip (id — durasi): 1320-122612-0000/0001/0002 (13,5/9,5/7,5 dtk),
1320-122617-0000/0001 (7,8/14,1 dtk); 5639-40744-0000…0004
(15,8/12,4/8,9/27,1/12,5 dtk); 6930-75918-0000/0001 (3,5/14,2 dtk),
6930-76324-0000/0001 (3,0/3,2 dtk), 6930-81414-0000 (12,9 dtk).
Estimasi F0 median per klip: 1320 ~121 Hz, 5639 ~139 Hz,
6930 ~172 Hz — ketiganya rentang laki-laki, jadi set
beda-speaker ini kasus-tersulit (satu gender), bukan kasus
rata-rata. Metadata gender OpenSLR tak bisa diambil (404).

## 2. Waktu CPU per klip (ms, provider CPU)

| Beban | n | mean | p50 | p95 |
| --- | --- | --- | --- | --- |
| 1 thread, klip penuh (3–27 dtk) | 75 | 60,8 | — | 147,7 |
| 1 thread, segmen 1/3 dtk | 30 | 13,0 | — | 18,5 |
| 1 thread, semua | 105 | 47,2 | 43,1 | 86,6 (maks 152,8) |
| 4 thread, klip penuh | 75 | 33,8 | — | 81,1 |
| 4 thread, segmen 1/3 dtk | 30 | 8,4 | — | 11,5 |

Laju ~5,5 mdetik per detik audio satu-thread (≈0,55%
waktu-nyata): enrollment 3 klip ≈ <0,5 detik,
skor satu segmen live ≈ belasan ms. Latensi bukan risiko.

## 3. Distribusi kosinus pasangan penuh-vs-penuh

| Pasangan | n | mean | std | min | p50 | maks |
| --- | --- | --- | --- | --- | --- | --- |
| Sama speaker | 30 | 0,672 | 0,173 | 0,379 | 0,737 | 0,969 |
| Sama speaker, satu bab | 16 | 0,697 | 0,197 | 0,379 | 0,766 | 0,969 |
| Sama speaker, lintas-bab | 14 | 0,643 | 0,135 | 0,381 | 0,612 | 0,835 |
| Beda speaker | 75 | 0,334 | 0,200 | 0,010 | 0,305 | 0,837 |
| Beda 1320-vs-5639 | 25 | 0,226 | 0,148 | 0,010 | 0,238 | 0,566 |
| Beda 1320-vs-6930 | 25 | 0,327 | 0,122 | 0,084 | 0,323 | 0,659 |
| Beda 5639-vs-6930 | 25 | 0,449 | 0,241 | 0,023 | 0,510 | 0,837 |

Sama-vs-beda tumpang tindih (ekor beda mencapai 0,837 pada
satu pasangan speaker mirip) — ambang tunggal tak pernah
sempurna; margin + gerbang durasi wajib mendampingi.

Sapuan ambang (pandangan klasifikasi-pasangan, kasar, n kecil):

| θ | presisi | recall |
| --- | --- | --- |
| 0,50 | 0,585 | 0,800 |
| 0,55 | 0,636 | 0,700 |
| 0,60 | 0,667 | 0,600 |
| 0,65 | 0,739 | 0,567 |
| 0,70 | 0,810 | 0,567 |

## 4. Robustness ujaran pendek (segmen tengah klip)

| Skor | n | mean | min | p50 | maks |
| --- | --- | --- | --- | --- | --- |
| Klip-sama penuh-vs-1 dtk | 15 | 0,262 | 0,014 | 0,235 | 0,557 |
| Klip-sama penuh-vs-3 dtk | 15 | 0,588 | 0,103 | 0,563 | 0,996 |
| Lintas-klip penuh-vs-1 dtk | 60 | 0,257 | −0,047 | 0,249 | 0,591 |
| Lintas-klip penuh-vs-3 dtk | 60 | 0,562 | 0,120 | 0,552 | 0,884 |

Segmen 1 detik mengandung energi wicara normal (rms ≈
penuh) — degradasi ini keterbatasan model, bukan artefak
hening. 1 detik ≈ skor beda-speaker: tak terpakai.

## 5. Skor sentroid-enrollment (wakil pemakaian nyata)

Enrollment = sentroid 3 klip/speaker; uji = 2 klip
tahan-luar; margin = s1−s2:

| Uji | benar | s1 speaker-benar (mean, min–maks) | margin benar | margin salah |
| --- | --- | --- | --- | --- |
| Penuh | 6/6 | 0,816 (0,568–0,933) | 0,027–0,618 | — |
| 3 dtk | 4/6 | 0,668 (0,499–0,854) | 0,025–0,234 | 0,113–0,123 |
| 1 dtk | 2/6 | 0,302 (0,159–0,499) | 0,184–0,208 | 0,021–0,240 |

Pada uji penuh, satu kasus benar nyaris seri (margin
0,027) — presisi menuntut margin minimum walau s1 tinggi.
Pada uji 3 detik, margin salah (0,11–0,12) menumpuk margin
benar — n=6 terlalu kecil untuk memisahkan; butuh korpus
kalibrasi besar.

## 6. Rentang operasi yang disarankan (precision-first)

Sementara, menunggu kalibrasi live butir 19:

- θ kosinus sentroid ∈ [0,55, 0,65]; mulai dari 0,60.
- Margin minimum s1−s2 ∈ [0,10, 0,20]; mulai dari 0,15.
- Gerbang durasi: tolak segmen uji <3 detik (kembalikan
  unknown); enrollment ≥3 klip, tiap klip ≥3 detik,
  idealnya lintas ≥2 sesi/bab.
- Unknown-by-default: gagal satu saja dari tiga pintu
  (θ, margin, durasi) → unknown + konflik bila seri.

## 7. Kaveat dan tindak lanjut

n=3 speaker (satu gender, wicara-baca bersih 16 kHz) —
bukan rapat far-field; angka Mac ≈ worker Linux; ambang
berkarat lintas domain (threshold rot). Tindak lanjut:
kalibrasi live speaker-disjoint ala butir 19 (≥190 solo
tanpa-salah untuk klaim 0,98, kembar TTS tak boleh
terverifikasi, overlap → unverified), lalu kunci θ +
margin pada data live, bukan LibriSpeech.
