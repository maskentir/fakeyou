# Riset Fase 2: Voiceprint, Diarisasi Offline, Verified Name, Pregen Lanjutan

Tanggal: 2026-10-07. Metode: 4 subagen riset paralel (read-only),
grounding repo + praktik eksternal terkini. Tanpa implementasi.

## Ringkasan eksekutif

| Jalur | Temuan kunci | Status |
| --- | --- | --- |
| Butir 17 voiceprint | sherpa-onnx + WeSpeaker/CAM++ (~26 MB,
dependensi opsional) termurah; **kebijakan dulu sebelum kode**
(UU PDP: voiceprint = data pribadi spesifik, perlu persetujuan
eksplisit) | Butuh 8 keputusan pemilik, lalu spike offline (S) |
| Butir 18 diarization | **Pramarat belum ada: audio tak pernah
disimpan** (hanya buffer 30 dtk in-memory); community-1 (GPU)
+ fallback sherpa CPU; sidecar `diarization.json` tanpa
ubah live record | Butuh keputusan persistensi audio
(privasi), lalu ukur offset jam |
| Butir 19 verified_name | **Bergantung pada 17**; klaim 0,98 butuh
≥190 solo tanpa-salah (korpus kini 5× terlalu kecil);
kalibrasi heuristik mengulang kegagalan gate (plafon 0,93–0,97) | Terblokir sampai 17 |
| Pregen lanjutan | **Fuzzy-final membeli ~nol** (bucket
mismatched kosong); alihkan ke penekanan restart (D1) +
telemetri dulu | 2× S, tanpa dep baru, tanpa blokir kebijakan |

Rekomendasi urutan: telemetri pregen + D1 dulu (murah, tak
terblokir, bertumpu pada mekanisme live yang terbukti),
sambil pemilik menjawab pertanyaan kebijakan voiceprint/audio.

## 1. Butir 17 — voiceprint store + enrollment eksplisit

Opsi embedding (CPU, Python): A. WeSpeaker ResNet34-LM via
sherpa-onnx (direkomendasikan: ~26 MB, Apache-2.0/CC-BY-4.0,
tanpa token, int8 tersedia); B. CAM++ via sherpa-onnx
(EER terbaik ~0,65%, lisensi paling bersih); C/D. SpeechBrain
ECAPA (lebih berat, 3× ukuran); E. TitaNet-small; F. Resemblyzer
(ditolak: tak terawat, di bawah bar); G. pyannote langsung
(ditolak untuk live: gated + berat).

Integrasi: `SpeakerRow.margin` (kini placeholder 1.0) menjadi
`s1-s2` kosinus; aturan `s1 < ambang → unknown`,
`s1-s2 < min_margin → unknown + konflik`; klaim vs terverifikasi
tetap terpisah (L4). Enrollment: CLI eksplisit, 3 frasa pendek,
verifikasi ulang, penghapusan + kill flag default OFF, gerbang
kualitas audio; **jangan simpan audio enrollment mentah**.

8 pertanyaan kebijakan prakode: bentuk persetujuan, residensi
(WNI, UU PDP), jadwal retensi/pemusnahan, scope pencocokan
(hanya terdaftar), keputusan dependensi opsional, sign-off
lisensi bobot, pemilik ambang + liabilitas salah-ikat, negatif
suara TTS twin. Estimasi: spike offline S (2–3 hari) → store+CLI
M → wiring registry M → kalibrasi live M; total ~3–4 minggu +
lead time kebijakan.

## 2. Butir 18 — pass diarization offline + koreksi RTTM

Opsi engine: community-1 pyannote 4.x (DER ~11,1%, GPU ~2–3,5
menit/jam-rapat, CC-BY-4.0 komersial OK + atribusi, gated —
JANGAN bobot 3.1 non-komersial) sebagai default; sherpa-onnx
CPU sebagai fallback (Apache-2.0, tanpa gate); Sortformer
ditolak (cap 4 speaker, GPU, lisensi); WhisperX ditolak
(kecuali butuh word-timestamp); API ditolak (privasi + biaya).

Arsitektur: audio campuran WAV 16 kHz ditulis saat sesi (baru,
~115 MB/jam, butuh kebijakan retensi) → job pasca-sesi di
container terpisah (`fakeyou diarize`, lock `diarization`) →
`diarization.rttm` mentah + sidecar `diarization.json`
skema `diarization_correction/v1` (stale-check `record_sha256`,
`corrected_speaker_uuid` namespace uuid5 terpisah) → baca via
`load_corrected_record` (satu choke point, `record.jsonl`
tetap identik bita). Risiko: skew jam `capture_ms` vs timeline
WAV (toleransi offset), tanpa end-timestamp STT (heuristik
durasi), token HF gated, atribusi CC-BY-4.0. Urutan: persistensi
audio + ukur offset → job sherpa → community-1 → adopsi
pembaca + kalibrasi (mengumpani butir 19).

## 3. Butir 19 — jalur verified_name + kalibrasi ambang

Definisi: `display_claim` = "nama N terpampang di tile yang
difusi ke uuid U" (tak pernah untuk keputusan); `verified_name`
= "uuid U skor ≥ θ terkunci terhadap voiceprint enrollment E
(opt-in eksplisit) + `display_claim` setuju sebagai faktor kedua"
(rantai bukti lengkap dijurnalkan; beda → null + fault).

Metodologi: bingkai minDCF ala NIST SRE (C_fa ≫ C_miss),
kalibrasi skor + s-norm, analisis margin (tolak dekat θ),
dev/eval speaker-disjoint. Ukuran sampel (Wilson 95%):
≥190 solo tanpa-salah atau ≥300 dengan ≤1 salah — korpus
kini ±66 solo, ~5× kurang; gate pada batas-bawah CI, bukan
estimasi titik. Protokol live: host + ≥3 manusia di ruang
beda, enrollment dulu, ~60–80 solo/skor per sesi (solo,
mute, overlap→tetap-unverified, rejoin, rename, nama-duplikat,
TTS twin tak boleh terverifikasi); kriteria ship: eval lolos +
overlap-unverified ≥0,95 + twin false-verify 0 + lembah margin
+ latensi tak berubah. Upaya M (logistik 4–6 sesi live
mendominasi); risiko: dependensi 17, domain shift, threshold rot.

## 4. Pregen lanjutan — fuzzy adoption + prefix reuse

Baseline live (f1p-11): 40 start → 17 adopted (42%),
23 superseded (58%), 0 mismatched; hemat p50 0,03 dtk,
p95 2,9 dtk; ~2,2 panggilan LLM per final.

Temuan kunci: bucket mismatched KOSONG sehingga fuzzy-final
tak mengangkat apa pun; boros ada di restart. Prioritas:
(1) D1 penekanan restart fuzzy — restart hanya bila delta
interim material (kata-isi berubah), bukan tiap drift
(S, murni `conversation.py`, memotong biaya DAN menaikkan
hemat; gerbang final tetap exact sehingga konten salah tak
bocor); (2) pakai-ulang komputasi superseded — caching prefiks
otomatis (OpenAI/Google sudah pasif; Anthropic butuh
`cache_control` eksplisit), revision seeding D2 (M, quality
play, tunda); D3 carryover spekulatif DITOLAK; (3) fuzzy-final
hanya sebagai jaring pengaman bila data nanti menunjukkan
mismatch (aturan allowlist kelas-diff, bukan ambang skalar —
satu kata bisa balik makna; "approve Monday" vs "Friday").

Prasyarat: telemetri Step-0 (S) — jurnal metadata
`dist`/`diff_class`/bucket tanpa teks + seksi `report.py`
pregen (kini angka dihitung manual); interim tak tersimpan
di mana pun sehingga distribusi tak bisa direkonstruksi
tanpa ini. D4 higiene thread fork (M–L): tiap pregen kini
menambah pesan interim yatim ke thread utama — batasi sebelum
start berlipat. Rekomendasi: telemetri + D1 (2× S, flag
terpisah default mati) + sesi ukur; D4 sebagai task sendiri.

## Keputusan yang diminta dari pemilik

1. Item Fase 2 mana dulu (rekomendasi: telemetri pregen + D1)?
2. Voiceprint: jawab 8 pertanyaan kebijakan + setujui dep opsional?
3. Diarisasi: setujui persistensi audio sesi + retensinya?
4. Ambang risiko fuzzy: tetap exact-only sampai data, atau izinkan
   kelas allowlist sempit sejak awal?
