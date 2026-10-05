# Laporan Gate Penamaan Speaker — 2026-10-06

**Verdict: GATE TASK 1 GAGAL. Berhenti tanpa memasang nama produksi.**
Sesuai [plan](../plans/speaker-names.md): gate butuh penanda presisi
≥0,98 + recall ≥0,5, atau eliminasi join+mute dengan presisi nama
≥0,98. Dua sesi live container gagal di kedua jalur. Tidak ada kode
penamaan yang dipasang ke jalur live; modul replay tetap alat
analisis offline di balik flag default mati.

## Sesi

| | SPK-1 | SPK-2 |
| --- | --- | --- |
| Meet (container `fakeyou-worker-SPK-N`) | `wfi-iahb-vat` | `hko-ekoy-kim` |
| Peserta | Harun Nur Rasyid, Annisa, Rani Twin | sama |
| Transkrip manusia | 17 giliran truth (12 solo, 5 overlap) | 21 giliran truth (20 solo, 1 overlap) |
| Selaras | 17/17 | 21/21 |
| Probe | 27 MB, 642 baris | 81 MB |
| Catatan | Annisa rejoin 6x; tak pernah mute | skrip dipatuhi: segmen mute 2x, join→bicara cepat, 20 solo |

Kebenaran SPK-1 dipetakan dari label suara + konfirmasi host
(turn 4 = Annisa). Kebenaran SPK-2 dari ucapan yang menyebut nama
sendiri, tanpa tebakan.

## Metrik gate

Penanda (token turunan tile vs aktivitas CSRC):

| Sesi | Ambang | Token terbaik | Presisi | Recall |
| --- | --- | --- | --- | --- |
| SPK-1 | 0,05 | `125a5923` | 0,77 | 0,25 |
| SPK-1 | 0,01 | `125a5923` | 0,36 | 0,36 |
| SPK-2 | 0,05 | `125a5923` | 0,93 | 0,16 |
| SPK-2 | 0,01 | `125a5923` | 0,74 | 0,12 |

Kandidat lama: `es310d` 0,0 di kedua sesi; `knownClass` 0,5
(SPK-1) lalu 0,95 (SPK-2) pada akurasi jendela — tidak stabil
lintas sesi dan tetap di bawah 0,98.

Eliminasi (replay `join`+`mute`):

| Sesi | Ambang | Solo | Bernama | Presisi | Cakupan |
| --- | --- | --- | --- | --- | --- |
| SPK-1 | 0,05 / 0,01 | 12 | 0 | — | 0,0 |
| SPK-2 | 0,05 / 0,01 | 20 | 0 | — | 0,0 |

Overlap menjadi `unknown`: 1,0 di kedua sesi pada replay
(karena tidak ada binding sama sekali, bukan karena deteksi
overlap bekerja).

## Akar penyebab

1. **Tidak ada penanda DOM yang cukup baik.** Token terbaik
   lintas sesi stabil (`125a5923`, tanpa hint mentah) tetapi
   presisi maksimal 0,93 dengan recall 0,16. Ambang 0,01
   (paritas produksi) justru menurunkan presisi karena
   frame-bisik menambah noise.
2. **Eliminasi join tak pernah mengikat.** Jeda alami
   join→bicara 10,7 dtk, ±17 dtk, dan 23,6 dtk melawan
   jendela ±5 dtk. Nol bind di 32 giliran solo. Melebarkan
   jendela berisiko ikatan kebetulan: SPK-1 menunjukkan
   6 rejoin menciptakan lotre koinsidensi.
3. **Sinyal mute tidak ada di data live.** Dari 23.556
   observasi tile di dua sesi, nol tile tercatat `muted`,
   padahal kedua manusia mute ±30 detik di SPK-2.
   `tileMuted()` probe buta terhadap cara Meet saat ini
   merender status mute. Seluruh lapis eliminasi mute
   tidak pernah bisa menyala.
4. **Ambang datar + buang-shared rapuh.** Pada 0,05,
   separuh jendela SPK-2 kosong (bisikan hilang) tetapi
   pemisahan bersih; pada 0,01, CSRC Annisa bocor ke
   jendela Harun dan ikut dibuang. Produksi memakai
   aturan margin-dominan per jendela, bukan ambang datar.

Satu bind benar sempat terbentuk di SPK-1 (mute-eliminasi
saat Annisa absen), tetapi sesudah 12 giliran solo
dinilai sehingga cakupan tetap 0.

## Yang terbukti bekerja

- Pemisahan CSRC per pembicara bersih pada frame panas:
  tiap pembicara tepat satu CSRC, tanpa kontaminasi
  silang di kedua sesi.
- Label suara produksi 100% benar pada seluruh giliran
  solo SPK-2 (a=Annisa, b=Harun, c=Annisa rejoin).
- Kehadiran device akurat: 6 rejoin Annisa tercatat
  sebagai interval terpisah tanpa tile hantu.

## Bug pengukuran yang diperbaiki selama analisis

- Jendela gabungan (satu segmen VAD diklaim dua
  pembicara) meracuni deteksi CSRC-bersama dan menghapus
  CSRC kedua pembicara. Kini jendela gabungan
  dikecualikan dari perhitungan kepemilikan di
  `csrc_sets_by_speaker` dan `_shared_csrcs`.
  Tes: `test_csrc_sets_by_speaker_ignores_merged_window_for_ownership`,
  `test_replay_ignores_merged_window_for_csrc_ownership`.
- Ambang frame replay 0,05 → 0,01 menyamai
  `ACTIVE_LEVEL` produksi yang sudah terverifikasi live.
  Dua frame uji below-threshold dimigrasi 0,01 → 0,005
  agar tetap di bawah ambang (maksud uji tidak berubah).
  Tes: `test_replay_names_quiet_speech_at_production_active_level`.
- Referensi proyek luar dihapus dari kode; provenance
  pelajaran tetap di plan dan laporan.

## Perintah dan hasil

| Perintah | Hasil |
| --- | --- |
| `pytest -m 'not live and not container and not browser'` | 3140 passed, 40 skipped, 48 deselected (exit 0; satu run sempat 1 gagal lalu hijau di 3 run ulang — flake env, bukan regresi) |
| replay probe-attr-8 + csrc-live-3 | 2 passed (exit 0) |
| `pytest -m browser tests/test_attribution_probe.py tests/test_voice_activity.py` | 10 passed (exit 0, pra-perubahan analisis) |
| `ruff check` | All checks passed (exit 0) |
| `ruff format --check` | bersih (exit 0) |
| `mypy src` | no issues, 55 files (exit 0) |
| `worker start/stop SPK-1`, `SPK-2` | conversation → stopped_by_owner |

## Risiko dan tindak lanjut

- Nama produksi tidak dipasang. Bendera
  `FAKEYOU_CONVERSATION__SPEAKER_NAMES_ENABLED` tetap
  default mati; modul replay tidak terhubung ke jalur
  live (satu-satunya rujukan adalah definisi flag).
- Penamaan butuh milestone riset sinyal mute DOM
  tersendiri sebelum eliminasi dapat diuji.
- Aturan kepemilikan CSRC replay perlu margin-dominan
  ala produksi bila analisis dilanjutkan.
- Label suara anonim (flag terpisah, sudah
  terverifikasi live) tidak terpengaruh vonis ini.

## Rekomendasi

- Biarkan `speaker_names_enabled` default mati.
- Jangan kejar penanda DOM lebih jauh tanpa bukti
  sinyal baru; dua sesi dengan 32 solo cukup.
- Pelajaran CSRC per pembicara tetap sah untuk
  atribusi label anonim.
