# Acceptance Multipihak — 2026-10-03

Branch `feat/multiparty-participation`, commit `36f02bd`. Policy `participant_policy_enabled`
aktif, wake phrase "Rani Twin", barge-in aktif, streaming OFF. Image worker dibangun ulang
dari commit acceptance sebelum sesi 1.

## Sesi

| Sesi | Link | Hasil |
| --- | --- | --- |
| mp-s1 | jqg-ebtc-eha | `blocked_by_platform` (±10 detik); retry setelah cooldown 200 detik + profil baru |
| mp-s1b | jqg-ebtc-eha | admitted; 27 utterance, 9 jawaban terucap; stop 3,5 detik, exit 0 |
| mp-s2 | kum-ndoe-rhu | admitted; 27 utterance, 12 jawaban terucap; stop 3,4 detik, exit 0 |
| mp-s3 | kum-ndoe-rhu | admitted; 7 utterance, 4 jawaban terucap; stop paksa 4,1 detik saat playback |
| mp-s3r | kum-ndoe-rhu | reconnect setelah kill; 1 utterance, 1 jawaban; stop 3,4 detik, exit 0 |

Kumulatif: **62 utterance** (target ≥60), **26 jawaban twin utuh** (target ≥20).
Ingest: 7 + 6 + 3 + 1 episode, semua `completed`, exit 0.

## Verdict per aspek

- **Media alignment: GAGAL (diketahui).** Tile key stabil per join (`spaces/.../devices/NNN`),
  ID baru tiap rejoin; label display selalu kosong; flag self tidak pernah terlihat;
  tidak ada sinyal active-speaker yang dapat diterima. Produksi tetap `unknown`.
- **Atribusi: TIDAK TERBUKTI.** Coverage 0,0 pada 62/62 utterance, alasan `no_evidence`.
  Implementasi atribusi live belum selesai; butuh capture per-channel atau SDK.
- **Policy: LULUS.** 32 reply semuanya explicit-address, 30 silent semuanya non-address
  (verifikasi silang journal lawan teks STT, nol mismatch). Nol jawaban untuk target
  peserta lain; kutipan nama di tengah kalimat tidak dijawab; unknown tidak mewarisi
  activation. Jebakan vocative ("Rani Twin kata Budi...") dijawab sesuai aturan batas kata.
- **Kualitas trust: LULUS BERSYARAT.** Jawaban netral, tidak ada klaim kewenangan owner,
  tidak ada nama yang ditebak; 4 closing question di 3 sesi (1 + 2 + 1).
- **Stop/ingestion: LULUS.** 4 stop ≤4,1 detik, exit 0; termasuk stop saat playback
  (tanpa `playback_finished` susulan, tanpa observer/audio susulan). Ingest semua completed;
  recall `memory_evidence/v1` terbukti membawa label sumber dengan `speaker_verified: false`.
- **Fungsi live: LULUS.** Admit, STT final, jawab, barge-in interim (ambang 3 kata),
  leave/rejoin teramati di roster, reconnect setelah kill.
- **Performa: GAGAL (diketahui, gate tidak dilonggarkan).** Agent p95 8,0–8,4 detik
  vs gate 4 detik; speech→PCM p95 10,0–10,6 detik vs gate 6 detik.

## Ground truth dan denominator

Host mengikuti skrip dialog bernomor; teks STT cocok dengan skrip tanpa penyimpangan
selain pecahan final STT ("Rani Twin" / "Kapan demo Orchid", "Stop cukup" x4).
Label pembicara A/B tidak diobservasi mesin dan diasumsikan sesuai skrip; klaim
rename display "Budi" tidak terverifikasi karena label roster kosong. Overlap
dibuktikan dua interim barge-in saat jawaban di-generate, bukan saat playback.
Denominator penuh dilaporkan; tidak ada sampel lambat yang dibuang.

## Status proyek

Routing partisipasi multipihak selesai dan diterima untuk pilot berperilaku
konservatif (jawab hanya saat disapa eksplisit). Atribusi speaker live belum
selesai dan tidak diklaim selesai.
