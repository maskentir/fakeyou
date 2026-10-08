# Butir 19: Jalur verified_name + Kalibrasi Ambang — Design Spec

Status: disetujui pemilik per seksi (2026-10-08). Pendekatan A penuh.
Riset pengikat: [2026-10-07-fase-2-research.md](../reports/2026-10-07-fase-2-research.md)
(§3 + f2-verified.md). Tergantung butir 17 (selesai, merge).

## 1. Cakupan + pentahapan

Tiga tahap berurutan dengan gate:

1. Kode: `display_claim` + `verified_name` + rantai bukti +
   gate kualitas enrollment + liveness challenge-response +
   stabilisasi skor (batas klip deterministik).
2. Dev-lock: harness kalibrasi offline + ≥2 sesi dev
   (≥150 solo) → kunci θ + id laporan kalibrasi.
3. Eval: sesi eval speaker-disjoint → gate ship atau tetap
   display_claim-only.

Flag verified default mati sampai gate ship lolos.
`display_claim` tampil berlabel "unverified", tak pernah
masuk keputusan. Fuzzy adoption di luar cakupan.

## 2. Jalur keputusan + rantai bukti

`SpeakerRegistry` per uuid: `display_claim` (nama + confidence
fusi, boleh null) dan `verified_name` (nama + enrollment_id,
atau null). Urutan putusan per turn bersuara:

1. Skor embedding vs semua enrollment (target + negatif twin);
   s-norm atas kohort (= semua enrollment lain + negatif twin);
   Platt → LLR; ambil top-1.
2. `verified_name` hanya bila: LLR ≥ θ DAN margin top1−top2 ≥
   lantai margin DAN `display_claim == owner_name` top-1 DAN
   enrollment opt-in aktif. Bind-once per sesi; turn berikut
   re-afirmasi atau fault.
3. Ketidaksepakatan (skor lolos tapi claim beda, rename, rejoin
   uuid baru, nama duplikat, overlap) → null + fault
   `voiceprint_misbind` + display_claim "unverified".

Jurnal per keputusan: enrollment_id, versi model, lolos gate
kualitas, skor mentah + param s-norm + versi Platt, θ + id
laporan kalibrasi, flag kesepakatan claim, uuid + slot +
waktu. Tanpa teks transkrip.

## 3. Enrollment + gate kualitas + liveness

CLI supervised (host hadir), Postgres, retensi 90 hari +
hapus manual, persetujuan lisan + jurnal (Q1/Q2/Q3).
Materi: sesi live solo per manusia; klip dipotong dari WAV
persistensi Fase A per slot.

Gate kualitas (lolos semua atau ditolak): durasi ≥30 dtk,
SNR ≥ lantai, ≥1 segmen baca + ≥1 segmen spontan. Lantai
SNR provisonal dari korpus ronde-1, difinalkan setelah dev
(persentil-5 SNR klip enrollment dev); enrollment di bawah
lantai final diulang.

Liveness challenge-response: CLI menerbitkan string digit
acak per enrollment; host menyampaikannya; pendaftar
membaca; STT atas klip wajib cocok edit-distance ≤1.
Anti-spoof sinyal akustik di luar cakupan (risiko tercatat).

## 4. Harness kalibrasi offline

Perintah offline atas skor terjurnal dev: s-norm per trial,
fit Platt + kurva reliabilitas, pindai θ pada minDCF
(C_fa:C_miss = 10:1, tercatat di laporan), histogram margin
dengan cek lembah (θ wajib di lembah; unimodal = tripwire
berhenti). Lantai margin = tepi bawah lembah dev.

Keluaran berversi di repo: θ, param s-norm/Platt, id laporan.
Perubahan model/param wajib rekalibrasi + regression atas
korpus dev yang diabadikan (embedding tersimpan, tanpa audio
mentah). Harness menolak eval yang pembicaranya muncul di dev.
Gerbang pada batas-bawah CI (Wilson/Clopper-Pearson).

## 5. Kampanye live

Host + ≥3 manusia di ruangan berbeda, twin aktif, hanya
container worker. Urutan: enrollment per manusia (+TTS twin
negatif); ≥2 sesi dev ≥150 solo (skrip riset: solo pra/pasca
twin-join, solo bernomor, mute, 2 overlap→unverified, 1 rejoin
nama-sama→re-verify, 1 rename→tripwire, 1 nama-duplikat→
unverified, turn TTS→tak-terverifikasi); kunci θ; eval sampai
≥190 solo tanpa-salah (atau ≥300 dengan ≤1 salah). Ground
truth: log host per turn + artefak probe; fixture disamarkan.

## 6. Gate ship + kegagalan + pengujian

Ship bila SEMUA lolos di eval: CI-bawah presisi ≥0,98 +
overlap-unverified ≥0,95 + twin false-verify = 0 + lembah
margin lolos + latensi tak berubah (toleransi nol). Gagal satu
→ display_claim-only, flag mati, lapor, tanpa relaksasi ambang.
Lembah dihitung pada trial non-overlap saja: trial overlap
dihitung oleh metriknya sendiri dan skor suaranya memang boleh
di mana saja karena aturan overlap memaksanya unverified.

Pengujian: merah-dulu per task SDD + reviewer terpisah; fixture
regresi dev; gate hijau; tanpa xfail.
