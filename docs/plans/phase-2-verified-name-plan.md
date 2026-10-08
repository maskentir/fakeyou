# Phase 2 Item 19: verified_name + Kalibrasi — Implementation Plan

> **Untuk worker agen:** SUB-SKILL WAJIB: subagent-driven-development.
> Kerjakan plan task-per-task. Step memakai sintaks checkbox (`- [ ]`).

**Goal:** Jalur `verified_name` terkalibrasi (skor + enrollment +
claim setuju) dengan gate ship CI-bawah ≥0,98, atau tetap
display_claim-only tanpa relaksasi.

**Kriteria sukses:** Task 1–5 merge dengan gate hijau + review;
artefak kalibrasi terkunci dari dev; eval lolos semua gate ship
(CI-bawah ≥0,98, overlap-unverified ≥0,95, twin false-verify 0,
lembah margin, latensi nol) atau putusan tercatat tetap
display_claim-only.

**Architecture:** Stabilisasi batas klip deterministik;
fusi display_claim bind-once; putusan verified fail-closed
(tanpa artefak kalibrasi terkunci → selalu null); s-norm +
Platt + θ + lantai margin berversi; enrollment CLI +
challenge liveness; harness kalibrasi offline; kampanye
dev lalu eval speaker-disjoint.

**Tech Stack:** Python (aturan repo), sherpa-onnx extra yang
ada, STT worker yang ada, Postgres voiceprint yang ada,
pytest serial, ruff, mypy. Tanpa dependency baru.

**Spec:** [phase-2-verified-name-design.md](./phase-2-verified-name-design.md)
(6 seksi, disetujui). Pelaksana membaca spec + plan ini.

## Global Constraints

- Python Inggris tanpa komentar/docstring dan tanpa string Bahasa Indonesia
  di `.py`; dokumentasi Bahasa Indonesia.
- Tanpa dependency baru; tanpa protokol privat Meet; tanpa teks transkrip
  di jurnal/sidecar.
- Flag verified default mati; tanpa artefak kalibrasi → verified selalu null.
- Event jurnal baru wajib berakhiran `EventVerb`.
- Tes merah dulu; pytest serial (`-p no:cacheprovider`, tanpa `-n`).
- Tiap task direview reviewer terpisah sebelum commit lokal.

Keputusan pemilik yang mengikat: pendekatan A penuh; logistik live penuh
(host + ≥3 manusia, 4–6 sesi); DCF 10:1; ship semua-lolos atau tetap
display_claim-only.

---

### Task 1: Stabilisasi batas klip deterministik

**Files:**
- Modify: `src/fakeyou/voiceprint_match.py` (atau modul tap/segmen
  yang memotong klip — pilih yang memegang batas VAD, catat di laporan)
- Test: `tests/test_voiceprint_match.py` (tambah kasus)

**Interfaces:**
- Produces: batas klip terkuantisasi grid 100 ms + padding konteks
  tetap 200 ms tiap sisi; turn identik → byte klip identik antar worker.
- Jurnal: `clip_bounds_quantized` (kunci: `grid_ms`, `pad_ms`)? TIDAK —
  tanpa event baru; perilaku murni deterministik, diassert di tes.

- [ ] **Step 1: tulis tes merah**
- [ ] Kasus: (a) dua segmentasi VAD yang batasnya beda <100 ms →
  klip byte-identik; (b) padding tepat 200 ms walau segmen pendek
  (clamp di awal audio); (c) skor turn identik antar dua worker
  simulasi (fragmentasi VAD beda) → selisih skor 0.
- [ ] **Step 2: jalankan tes, pastikan gagal**
- [ ] **Step 3: implementasi minimal**
- [ ] **Step 4: jalankan tes, pastikan lulus**
- [ ] **Step 5: gate task** (file tes + ruff + mypy src diubah)
- [ ] **Step 6: review + commit**

```bash
git add <src-diubah> tests/test_voiceprint_match.py
git commit -m "feat(verified): deterministic clip bounds"
```

### Task 2: Fusi display_claim bind-once

**Files:**
- Modify: `src/fakeyou/identity.py`
- Test: `tests/test_identity.py` (tambah kasus)

**Interfaces:**
- Consumes: roster drain `[{id, name}]`, fusi slot↔device yang ada.
- Produces: `SpeakerRow.display_claim: str | None`,
  `SpeakerRow.claim_confidence: float`; bind-once per sesi
  (claim pertama menang, rename → claim baru null +
  `display_claim_changed` fault ringan, bukan verified fault);
  nama duplikat di roster → claim null semua uuid terkait.
- Render "unverified" di record/debrief (tak pernah keputusan).

- [ ] **Step 1: tulis tes merah**
- [ ] Kasus: (a) fusi kuat → claim terisi + confidence; (b) rename
  → null + fault; (c) nama duplikat → null; (d) bind-once:
  fusi kedua nama sama → tak berubah; (e) claim tak dipakai
  keputusan (assert tak ada jalur baca selain render + §Task 3).
- [ ] **Step 2: jalankan tes, pastikan gagal**
- [ ] **Step 3: implementasi minimal**
- [ ] **Step 4: jalankan tes, pastikan lulus**
- [ ] **Step 5: gate task**
- [ ] **Step 6: review + commit**

```bash
git add src/fakeyou/identity.py tests/test_identity.py
git commit -m "feat(verified): bind-once display claim fusion"
```

### Task 3: Putusan verified_name + rantai bukti

**Files:**
- Modify: `src/fakeyou/voiceprint_match.py`, `src/fakeyou/identity.py`
- Create: `src/fakeyou/verify.py` (s-norm + Platt terapkan, bukan fit)
- Test: `tests/test_verify.py` (baru)

**Interfaces:**
- Consumes: Task 1 (skor stabil), Task 2 (claim), artefak kalibrasi
  berversi (`theta_llr`, `margin_floor`, `snorm_cohort`,
  `platt_a/b`, `platt_version`, `report_id` — dimuat dari file;
  tak ada file → selalu null).
- Produces: `decide_verified(uuid, scores, claim, artifact) ->
  VerifiedDecision | None`; bind-once per sesi; mismatch →
  `voiceprint_misbind` fault + null; jurnal `name_verified`
  (kunci: enrollment_id, model_version, score_raw, snorm params
  + version, theta + report_id, claim_agree, uuid, slot).
- Overlap (flag turn overlap dari upstream bila ada; bila tak ada,
  multi-CSRC aktif → anggap overlap) → selalu null tanpa fault.

- [ ] **Step 1: tulis tes merah**
- [ ] Kasus: (a) skor lolos + claim setuju → verified + jurnal lengkap;
  (b) tanpa artefak → null; (c) claim beda → null + misbind;
  (d) margin di bawah lantai walau skor lolos → null tanpa fault
  (reject option); (e) overlap → null; (f) bind-once: putusan
  kedua orang beda → null + misbind, putusan lama utuh.
- [ ] **Step 2: jalankan tes, pastikan gagal**
- [ ] **Step 3: implementasi minimal** (termasuk pemutakhiran
  allowlist Task 2 kasus (e): `verify.py` jadi pembaca sah ketiga)
- [ ] **Step 4: jalankan tes, pastikan lulus**
- [ ] **Step 5: gate task**
- [ ] **Step 6: review + commit**

```bash
git add src/fakeyou/verify.py src/fakeyou/voiceprint_match.py src/fakeyou/identity.py tests/test_verify.py tests/test_identity.py
git commit -m "feat(verified): verified_name decision path"
```

### Task 4: Enrollment CLI + gate kualitas + liveness

**Files:**
- Modify: `src/fakeyou/cli/voiceprint.py`
  (`enroll` + subcommand `challenge`), store bila perlu
- Test: `tests/test_cli_voiceprint.py` (tambah kasus)

**Interfaces:**
- `fakeyou voiceprint challenge --meeting X` mencetak JSON
  `{"challenge": "<digit-acak-8>", "challenge_id": "<uuid>"}`;
  `enroll` wajib `--challenge-id` + klip/STT cocok edit-distance ≤1
  (STT via pemanggil yang ada; di tes: fake transkrip — batas
  modul, bukan mock antar-unit sendiri).
- Gate: durasi ≥30 dtk, SNR ≥ lantai provisonal (konstanta
  `PROVISIONAL_SNR_FLOOR_DB`, nilai dari korpus ronde-1, catat
  di laporan + finalisasi pasca-dev), ≥1 segmen baca + ≥1 spontan.
  Gagal gate → exit 2 + alasan JSON, tanpa tulis store.
- Jurnal: `enrollment_accepted` / `enrollment_rejected`
  (kunci: challenge_id, snr_db, seconds, segments — tanpa audio).

- [ ] **Step 1: tulis tes merah**
- [ ] Kasus: (a) challenge unik per panggilan; (b) enroll cocok →
  diterima; (c) transkrip beda ≥2 → ditolak exit 2; (d) durasi
  kurang → ditolak; (e) SNR rendah → ditolak; (f) tanpa segmen
  spontan → ditolak.
- [ ] **Step 2: jalankan tes, pastikan gagal**
- [ ] **Step 3: implementasi minimal**
- [ ] **Step 4: jalankan tes, pastikan lulus**
- [ ] **Step 5: gate task**
- [ ] **Step 6: review + commit**

```bash
git add src/fakeyou/cli/voiceprint.py <store-bila-diubah> tests/test_cli_voiceprint.py
git commit -m "feat(verified): enrollment gates and liveness"
```

### Task 5: Harness kalibrasi offline

**Files:**
- Create: `src/fakeyou/cli/calibrate.py`
  (`fakeyou calibrate lock --dev <jurnal...> --out <artefak>`,
  `fakeyou calibrate eval --eval <jurnal...> --artifact <f>`)
- Modify: `src/fakeyou/cli/main.py` (registrasi)
- Test: `tests/test_cli_calibrate.py` (baru, fixture skor sintetis
  deterministik — tanpa audio)

**Interfaces:**
- `lock`: s-norm per trial, fit Platt + reliabilitas, pindai θ
  minDCF 10:1, histogram margin + cek lembah (gagal → exit 3,
  tanpa artefak), tulis artefak JSON berversi.
  `--model-version` opsional dicatat sebagai provenance artefak.
- `eval`: terapkan artefak, penegakan disjoint (tolak id pembicara
  eval yang ada di dev → exit 2), penegakan model (digest
  `--model-version` wajib cocok dengan artefak → exit 2 bila beda),
  metrik: presisi + CI-bawah (Wilson), overlap-unverified, twin
  false-verify, lembah margin; cetak JSON + exit 0 lolos / 4 gagal
  gate.
- Tanpa teks transkrip di input; jurnal skor saja.

- [ ] **Step 1: tulis tes merah**
- [ ] Kasus: (a) fixture bimodal → artefak + θ di lembah;
  (b) fixture unimodal → exit 3; (c) eval disjoint lolos →
  JSON metrik + exit 0; (d) pembicara tumpang → exit 2;
  (e) eval gagal presisi → exit 4.
- [ ] **Step 2: jalankan tes, pastikan gagal**
- [ ] **Step 3: implementasi minimal**
- [ ] **Step 4: jalankan tes, pastikan lulus**
- [ ] **Step 5: gate task** (+ `tests/test_documented_commands.py`)
- [ ] **Step 6: review + commit**

```bash
git add src/fakeyou/cli/calibrate.py src/fakeyou/cli/main.py tests/test_cli_calibrate.py
git commit -m "feat(verified): offline calibration harness"
```

### Task 6: Dev-lock live (bukan kode)

Setelah Task 1–5 merge: enrollment per manusia + ≥2 sesi dev
≥150 solo (host + ≥3 manusia, skrip spec §5). Jalankan `lock`,
finalkan lantai SNR (enrollment di bawah diulang), commit artefak
kalibrasi berversi. Butuh link Meet dari pemilik saat siap.

### Task 7: Eval live + gate ship (bukan kode)

Sesi eval speaker-disjoint sampai ≥190 solo tanpa-salah (atau
≥300 dengan ≤1 salah). Jalankan `eval`; tulis adendum acceptance
+ putusan ship/tetap-display_claim-only. Latensi diukur
sebelum/sesudah (toleransi nol).

## Self-Review

- Cakupan: spec §1–§6 → Task 1–7; fuzzy eksplisit keluar.
- Placeholder: tak ada; grid/padding, DCF 10:1, edit-distance ≤1,
  exit code, nama event eksplisit. SNR provisonal terukur + aturan
  finalisasi. Satu titik diskresi (modul tap) dibatasi + dicatat.
- Konsistensi: artefak Task 5 dipakai verbatim Task 3; event dan
  nama CLI identik; fail-closed tanpa artefak di semua jalur.
