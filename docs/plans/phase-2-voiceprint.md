# Phase 2 Item 17: Voiceprint Store + Explicit Enrollment — Implementation Plan

> **Untuk worker agen:** SUB-SKILL WAJIB: subagent-driven-development.
> Kerjakan plan task-per-task. Step memakai sintaks checkbox (`- [ ]`).

**Goal:** Enrollment voiceprint eksplisit + pencocokan embedding
yang membuat `SpeakerRow.margin` jujur (`s1−s2`), di balik flag
default mati.

**Architecture:** Store Postgres (`voiceprints`, koneksi memakai
ulang DSN checkpoint, tanpa dep baru) + ekstraktor ERes2Net via
`sherpa-onnx` (extra opsional baru yang disetujui) + CLI
`voiceprint` file-based + adapter skor yang disuntik ke
`SpeakerRegistry`; ambang θ=0,60, margin-min 0,15, tolak
segmen <3 dtk (dari spike), unknown-by-default.

**Tech Stack:** Python, psycopg (sudah dep), sherpa-onnx 1.12.38
(extra baru), pytest serial, ruff, mypy.

**Spec:** [2026-10-07-voiceprint-spike.md](../reports/2026-10-07-voiceprint-spike.md),
[2026-10-07-fase-2-research.md](../reports/2026-10-07-fase-2-research.md)
(bagian 1 + Keputusan yang diambil),
[long-term-architecture.md](long-term-architecture.md) butir 17.
Pelaksana membaca ketiganya plus plan ini.

## Global Constraints

- Python Inggris tanpa komentar/docstring dan tanpa string Bahasa Indonesia
  di `.py`; dokumentasi Bahasa Indonesia.
- Dep baru HANYA `sherpa-onnx==1.12.38` sebagai extra `voiceprint`
  (sudah disetujui pemilik; 1.12.x terakhir yang cocok onnxruntime
  1.24.x milik pipecat — sherpa ≥1.13.8 butuh ORT ≥1.28);
  semua kode impornya opsional dengan pesan error jelas bila
  extra belum dipasang.
- Tanpa protokol privat Meet; tanpa teks transkrip di jurnal.
- Semua flag/perilaku baru default mati; flag-mati = perilaku
  hari ini identik (tes lama hijau tak tersentuh).
- Event jurnal baru wajib berakhiran kata-kerja kosakata
  `EventVerb` (`taxonomy.py`): `voiceprint_recorded`,
  `voiceprint_cleared`, `voiceprint_expired`,
  `voiceprint_decided` (bawa kunci `outcome`).
- Tes merah dulu; pytest serial (`-p no:cacheprovider`, tanpa `-n`).
- Tiap task direview reviewer terpisah sebelum commit lokal.

Keputusan pemilik yang mengikat (Q1–Q8): persetujuan lisan +
jurnal; store Postgres; retensi 90 hari + hapus manual;
scope SEMUA peserta; salah-ikat = fault + jurnal; enroll
negatif TTS; model ERes2Net (`3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx`,
dim 192, 26.485.263 byte, sha256
`c59158379255ad66e161679cca6af8d52d51e389e3224ab7d7a7baae295c2db5`).
CAM++ dibatalkan 2026-10-08: ekspor ONNX-nya kacau terhadap panjang
audio (konten identik ±0,3 dtk → cosine 0,295), ERes2Net lolos uji
ketahanan panjang dan 9/9 take diagnosis benar.

---

### Task 1: Settings + VoiceprintStore Postgres

**Files:**
- Modify: `src/fakeyou/settings.py` (`IdentitySettings`)
- Create: `src/fakeyou/voiceprint_store.py`
- Test: `tests/test_voiceprint_store.py` (baru)

**Interfaces:**
- Consumes: `CheckpointSettings.url` (DSN dipakai ulang),
  `EventVerb` untuk nama event.
- Produces (dipakai Task 3 dan 4 apa adanya):
  `ensure_voiceprint_schema(conn) -> None`,
  `class VoiceprintStore` dengan
  `save(record: VoiceprintRecord) -> None`,
  `load_all() -> list[VoiceprintRecord]`,
  `delete(person_id: str) -> bool`,
  `purge_expired(now, retention_days: int) -> list[str]`,
  `touch_used(person_id: str, now) -> None`;
  `@dataclass(frozen=True, slots=True) class VoiceprintRecord`
  dengan kunci persis `person_id: str`, `embedding: tuple[float, ...]`,
  `dim: int`, `n_utterances: int`, `consent_ts: str`,
  `policy_version: str`, `is_negative: bool`,
  `enrolled_at: str`, `last_used_at: str`.
  Konstruktor `VoiceprintStore(connect: Callable[[], Any])`
  menerima factory koneksi (seam untuk fake di tes,
  cerminkan pola `FakePostgresSaver` di
  `tests/test_checkpoint.py:31`).

Skema (satu tabel, tanpa pgvector — N kecil, kosinus di Python):

```sql
CREATE TABLE IF NOT EXISTS voiceprints (
    person_id TEXT PRIMARY KEY,
    embedding BYTEA NOT NULL,
    dim SMALLINT NOT NULL,
    n_utterances INTEGER NOT NULL,
    consent_ts TIMESTAMPTZ NOT NULL,
    policy_version TEXT NOT NULL,
    is_negative BOOLEAN NOT NULL DEFAULT FALSE,
    enrolled_at TIMESTAMPTZ NOT NULL,
    last_used_at TIMESTAMPTZ NOT NULL
)
```

Embedding disimpan float32 little-endian (`struct.pack`).
`purge_expired` menghapus baris dengan
`last_used_at < now - retention_days` dan mengembalikan
`person_id` yang dihapus (penelepon yang menjurnalkan
satu `voiceprint_expired` per id).

Settings (`IdentitySettings`, nama persis):
`voiceprint_enabled: bool = False`,
`voiceprint_threshold: float = 0.60`,
`voiceprint_min_margin: float = 0.15`,
`voiceprint_min_seconds: float = 3.0`,
`voiceprint_retention_days: int = 90`,
`voiceprint_model_path: str = ""`,
`voiceprint_model_sha256: str = "c59158379255ad66e161679cca6af8d52d51e389e3224ab7d7a7baae295c2db5"`,
`voiceprint_policy_version: str = "v1"`.

- [ ] **Step 1: tulis tes merah store + settings**

Kasus (`tests/test_voiceprint_store.py`, fake koneksi yang
merekam SQL + result set yang diprogram, tanpa server PG):
(a) `ensure_voiceprint_schema` mengeksekusi DDL dengan
sembilan kolom di atas; (b) roundtrip save→load_all
memulihkan record identik (embedding float presisi penuh);
(c) `delete` True bila ada + False bila tak ada;
(d) `purge_expired` hanya menghapus yang kedaluwarsa dan
mengembalikan id-nya; (e) `touch_used` memperbarui
`last_used_at`. Kasus settings: default ctor mengikuti
`DEFAULTS.identity` (cerminkan pola
`test_sentence_streaming_default_follows_settings_flag`).

- [ ] **Step 2: jalankan tes, pastikan gagal**

Run:
`.venv/bin/python -m pytest tests/test_voiceprint_store.py -q -p no:cacheprovider`
Expected: FAIL (modul belum ada).

- [ ] **Step 3: implementasi minimal settings + store**

Tambah delapan field di `IdentitySettings`; tulis
`voiceprint_store.py` (DDL + CRUD + purge + touch;
tanpa logika skor — itu Task 4).

- [ ] **Step 4: jalankan tes, pastikan lulus**

Run: perintah Step 2. Expected: PASS.

- [ ] **Step 5: gate task**

Run: file tes baru penuh, `ruff check` + `ruff format --check`
pada file yang diubah, `mypy` pada
`src/fakeyou/settings.py src/fakeyou/voiceprint_store.py`.
Expected: semua hijau.

- [ ] **Step 6: review + commit**

Reviewer terpisah memeriksa diff Task 1 saja. Lalu:

```bash
git add src/fakeyou/settings.py src/fakeyou/voiceprint_store.py tests/test_voiceprint_store.py
git commit -m "feat(voiceprint): settings and postgres store"
```

### Task 2: Ekstraktor embedding + extra opsional

**Files:**
- Create: `src/fakeyou/voiceprint_embed.py`
- Modify: `pyproject.toml` (extra `voiceprint`), `uv.lock`
- Test: `tests/test_voiceprint_embed.py` (baru)

**Interfaces:**
- Consumes: `IdentitySettings.voiceprint_model_path/_sha256`.
- Produces (dipakai Task 3 dan 4 apa adanya):
  `load_embedder(model_path: str, expected_sha256: str) -> SpeakerEmbedder`;
  `SpeakerEmbedder.embed(utterances: Sequence[Sequence[float]]) -> tuple[float, ...]`
  (masing-masing ujaran = PCM mono 16 kHz float −1..1;
  mengembalikan sentroid ternormalisasi L2);
  `cosine(a: Sequence[float], b: Sequence[float]) -> float`.
  `load_embedder` menolak bila sha256 berkas ≠ harapan
  (`ValueError` dengan kata `sha256`, tanpa path absolut
  di pesan) dan menolak ujaran kosong.
  Impor `sherpa_onnx` terjadi HANYA di dalam fungsi
  (lazy); bila tak terpasang, raise `RuntimeError`
  dengan kata `voiceprint` + `pip install` + nama extra.

- [ ] **Step 1: tulis tes merah ekstraktor**

Kasus: (a) `cosine` bernilai 1,0 untuk vektor sama,
0,0 untuk ortogonal (±1e-6); (b) sha salah → `ValueError`
berkata `sha256` (pakai berkas kecil `tmp_path`, tanpa
model asli); (c) ujaran kosong → `ValueError`;
(d) bila `sherpa_onnx` tak terimpor, `load_embedder`
me-raise `RuntimeError` (simulasikan dengan monkeypatch
`sys.modules`, JANGAN uninstall); (e) bila terpasang
(`pytest.importorskip("sherpa_onnx")`), embed dua ujaran
sintetis (sinus 440 Hz vs 880 Hz, 3 dtk, 16 kHz) dan
assert hasil panjang 192 + ternormalisasi L2
(norm ≈ 1,0 ±1e-3). Tes (e) menandai bahwa API
sherpa benar-benar terpanggil, bukan stub.

- [ ] **Step 2: jalankan tes, pastikan gagal**

Run:
`.venv/bin/python -m pytest tests/test_voiceprint_embed.py -q -p no:cacheprovider`
Expected: FAIL (modul belum ada).

- [ ] **Step 3: implementasi minimal + extra**

Tulis `voiceprint_embed.py` (lazy import, cek sha,
instansiasi recognizer sherpa ERes2Net, rata-rata
utterance-embedding → normalisasi L2, `cosine` murni).
Tambah extra di `pyproject.toml`:

```toml
voiceprint = ["sherpa-onnx==1.12.38"]
```

Jalankan `uv lock` dan sertakan `uv.lock` yang
diperbarui. Verifikasi gate default tak menarik
sherpa: `.venv/bin/python -c "import fakeyou.voiceprint_embed"`
harus sukses TANPA `sherpa_onnx` terimpor
(assert `"sherpa_onnx" not in sys.modules`).

- [ ] **Step 4: jalankan tes, pastikan lulus**

Run: perintah Step 2. Expected: PASS (termasuk (e)
di `.venv` kini karena spike memasang sherpa).

- [ ] **Step 5: gate task**

Run: file tes baru penuh, `ruff check` + `ruff format --check`
pada file yang diubah, `mypy` pada
`src/fakeyou/voiceprint_embed.py`. Expected: hijau.

- [ ] **Step 6: review + commit**

Reviewer terpisah (orang berbeda dari Task 1). Lalu:

```bash
git add src/fakeyou/voiceprint_embed.py tests/test_voiceprint_embed.py pyproject.toml uv.lock
git commit -m "feat(voiceprint): embedding extractor and optional extra"
```

### Task 3: CLI voiceprint (file-based v1)

**Files:**
- Create: `src/fakeyou/cli/voiceprint.py`
- Modify: `src/fakeyou/cli/main.py` (registrasi)
- Test: `tests/test_cli_voiceprint.py` (baru)

**Interfaces:**
- Consumes: Task 1 (`VoiceprintStore`, `VoiceprintRecord`),
  Task 2 (`load_embedder`, `cosine`), `Journal`,
  `deaf.rms_level` untuk gerbang energi, stdlib `wave`.
- Produces: subcommand `fakeyou voiceprint` dengan
  `enroll --name --clip A.wav --clip B.wav --clip C.wav [--negative]`,
  `verify --name --clip T.wav`, `delete --name`,
  `list`, `purge-expired`.
  Aturan terkunci: WAV 16 kHz mono 16-bit SAJA (tolak
  lainnya dengan exit 2 + pesan menyebut format);
  enroll butuh ≥3 klip, tiap klip ≥3,0 dtk DAN energi
  rms di atas ambang `VOICEPRINT_MIN_RMS = 0.01`
  (di bawah itu tolak sebagai hening);
  `verify` menghitung kosinus lalu MENJURNALKAN
  `voiceprint_decided` (`outcome` = `verified` bila
  skor ≥ threshold DAN margin terpenuhi terhadap print
  lain, `conflict` bila margin gagal, `unknown` bila
  di bawah threshold); `enroll` menjurnalkan
  `voiceprint_recorded` (kunci: `person_id`,
  `n_utterances`, `is_negative`, tanpa embedding);
  `delete` menjurnalkan `voiceprint_cleared` bila ada;
  `purge-expired` menjurnalkan satu `voiceprint_expired`
  per id. Enrollment in-call DITUNDA (v1 file-based);
  negatif TTS (Q8) = enroll klip TTS dengan
  `--negative` (pemilik merekam dari sesi twin).

- [ ] **Step 1: tulis tes merah CLI**

Kasus (WAV sintetis via stdlib `wave` di `tmp_path`,
store fake Task 1, embedder fake yang mengembalikan
vektor tetap per berkas): (a) enroll 3 klip valid →
exit 0 + satu `voiceprint_recorded` + record tersimpan
(`n_utterances == 3`, `policy_version == "v1"`,
`consent_ts` terisi ISO); (b) enroll 2 klip → exit 2;
(c) klip hening (rms < 0,01) → exit 2; (d) WAV 8 kHz →
exit 2 menyebut format; (e) verify skor-tinggi →
`voiceprint_decided` outcome `verified`; (f) verify
skor-rendah → `unknown`; (g) dua print seri (margin
< 0,15) → `conflict`; (h) print negatif menang →
`unknown` + tanpa fault (negatif = pengecualian diri,
bukan konflik); (i) delete ada/tak-ada;
(j) purge-expired menjurnalkan per id.

- [ ] **Step 2: jalankan tes, pastikan gagal**

Run:
`.venv/bin/python -m pytest tests/test_cli_voiceprint.py -q -p no:cacheprovider`
Expected: FAIL (subcommand belum ada).

- [ ] **Step 3: implementasi minimal CLI**

Tulis `cli/voiceprint.py` (parse WAV, gerbang,
panggil Task 1+2, jurnal) + registrasi di `main.py`
cerminkan pola subcommand `memory_*`.

- [ ] **Step 4: jalankan tes, pastikan lulus**

Run: perintah Step 2. Expected: PASS.

- [ ] **Step 5: gate task**

Run: file tes baru penuh + `tests/test_documented_commands.py`
(kemungkinan mendaftar subcommand),
`ruff check` + `ruff format --check`, `mypy` pada
`src/fakeyou/cli/voiceprint.py src/fakeyou/cli/main.py`.
Expected: hijau. Bila `test_documented_commands`
menuntut dokumen perintah, perbarui dokumen itu dalam
task ini (Bahasa Indonesia).

- [ ] **Step 6: review + commit**

Reviewer terpisah (orang berbeda dari Task 1–2). Lalu:

```bash
git add src/fakeyou/cli/voiceprint.py src/fakeyou/cli/main.py tests/test_cli_voiceprint.py
git commit -m "feat(voiceprint): file-based enrollment CLI"
```

### Task 4: Adapter skor + wiring registry + sadap audio turn

**Files:**
- Create: `src/fakeyou/voiceprint_match.py`
- Modify: `src/fakeyou/identity.py` (skor per-baris +
  margin jujur), `src/fakeyou/audio.py` (sadap klip turn),
  `src/fakeyou/assembly.py` (wiring flag),
  `src/fakeyou/conversation.py` ATAU `cli/join.py`
  (panggil skor saat final; pilih SATU titik yang
  melihat final + audio, catat pilihan di laporan)
- Test: `tests/test_voiceprint_match.py` (baru),
  tambah kasus di `tests/test_identity.py`,
  `tests/test_audio.py`

**Interfaces:**
- Consumes: Task 1–2, `SpeakerRow`/`SpeakerRegistry`,
  `Journal`, `FaultReporter`.
- Produces:
  `@dataclass(frozen=True, slots=True) class VoiceScores`
  (`person_id: str | None`, `s1: float`, `s2: float`,
  `outcome: Literal["verified", "unknown", "conflict", "negative"]`);
  `class VoiceprintMatcher` dengan
  `__init__(store, *, threshold=0.60, min_margin=0.15)`
  dan `match(embedding) -> VoiceScores`:
  s1/s2 kosinus atas SEMUA print non-negatif (scope
  Q4 semua-peserta) + print negatif; bila pemenang
  adalah negatif → `unknown` hasil `negative`;
  bila s1 < threshold → `unknown`; bila s1−s2 <
  min_margin → `conflict`; selain itu `verified`.
  `SpeakerRegistry` mendapat `note_voice_scores(uuid, scores)`
  (abaikan diam-diam bila uuid tak dikenal) dan
  `_snapshot` memakai `margin = s1 − s2` bila skor
  ada, else `1.0` (placeholder lama dipertahankan
  untuk baris tanpa skor).
  Aturan salah-ikat (Q7): bila outcome `verified`
  untuk person P pada baris yang device-nya terikat
  ke pemilik terdaftar Q ≠ P → `FaultReporter`
  fault `voiceprint_misbind` + jurnal
  `voiceprint_decided` outcome `mismatched_owner`,
  baris TAK di-rebind (unknown-only-upgrade: suara
  tak pernah memindah binding sendirian).
  `TurnAudioTap(FrameProcessor)` di `audio.py`:
  akumulasi PCM 16 kHz mono di antara
  `VADUserStartedSpeakingFrame` dan
  `VADUserStoppedSpeakingFrame`, `take_clip()`
  mengembalikan bytes turn terakhir + mengosongkan;
  klip < `voiceprint_min_seconds` dibuang pemanggil
  (unknown, tanpa skor). Seluruh jalur hidup HANYA
  bila `voiceprint_enabled` (default mati); bila mati,
  tap tidak dipasang dan skor tak pernah dipanggil.

- [ ] **Step 1: tulis tes merah matcher + registry**

Kasus matcher (store fake, vektor buatan):
(a) s1 di atas threshold + margin cukup → `verified`
dengan person benar; (b) s1 di bawah → `unknown`;
(c) seri → `conflict`; (d) negatif menang → `negative`
+ outcome konsumsi `unknown`; (e) store kosong →
`unknown` (s1 = −1,0, s2 = −1,0).
Kasus registry: (f) `note_voice_scores` lalu snapshot
margin == s1−s2 persis; (g) tanpa skor margin tetap 1,0;
(h) uuid asing diabaikan. Kasus tap (i): start→frame→stop
menghasilkan klip; (j) stop-tanpa-start tak meledak.
Kasus misbind (k): verified P pada device Q≠P →
fault `voiceprint_misbind` + jurnal + binding utuh.

- [ ] **Step 2: jalankan tes, pastikan gagal**

Run: tiga file tes (`test_voiceprint_match.py`,
`test_identity.py`, `test_audio.py`) full.
Expected: FAIL pada kasus baru.

- [ ] **Step 3: implementasi minimal**

Tulis `voiceprint_match.py`; tambah `_voice_scores:
dict[str, VoiceScores]` + `note_voice_scores` +
cabang margin di `_snapshot`; tulis `TurnAudioTap`;
wiring `assembly.py` (pasang tap + matcher hanya bila
flag nyala); satu titik panggil saat final
(dokumentasikan pilihan di commit message body).

- [ ] **Step 4: jalankan tes, pastikan lulus**

Run: perintah Step 2. Expected: PASS.

- [ ] **Step 5: gate task**

Run: tiga file tes penuh, `ruff check` + `ruff format --check`,
`mypy` pada semua `src` yang diubah. Expected: hijau.

- [ ] **Step 6: review + commit**

Reviewer terpisah (orang berbeda dari Task 1–3). Lalu:

```bash
git add src/fakeyou/voiceprint_match.py src/fakeyou/identity.py src/fakeyou/audio.py src/fakeyou/assembly.py <titik-panggil> tests/test_voiceprint_match.py tests/test_identity.py tests/test_audio.py
git commit -m "feat(voiceprint): matcher, honest margin, turn audio tap"
```

### Task 5: Kalibrasi live + keputusan (bukan kode)

Setelah Task 1–4 merge: enroll pemilik + ≥2 manusia
(persetujuan lisan + jurnal, Q1), sesi live ala butir 19
untuk mengunci θ + margin pada data live, TTS twin tak
boleh terverifikasi (negatif Q8), overlap → unknown.
Butuh link Meet + manusia di ruang beda dari pemilik.
Kriteria ship mengikuti riset butir 19 (CI bawah ≥0,98).
Promosi `verified_name` (display_claim vs verified)
TETAP butir 19, di luar plan ini.

## Self-Review

- Cakupan: store+retensi (Q2/Q3) → Task 1; extra+model
  (Q5/Q6) → Task 2; enroll/verify/delete + negatif
  (Q1/Q8) → Task 3; skor+margin+misbind (Q4/Q7) +
  sadap audio → Task 4; kalibrasi live → Task 5.
  Tak ada gap; in-call enrollment eksplisit ditunda.
- Placeholder: tak ada; semua signature, SQL, event,
  ambang, dan perintah eksplisit. Satu titik diskresi
  implementer (panggil-saat-final) dibatasi dua opsi +
  wajib dicatat.
- Konsistensi: `VoiceprintRecord`/`VoiceprintStore`
  Task 1 dipakai verbatim Task 3–4; `VoiceScores`/
  `VoiceprintMatcher` Task 4 mengonsumsi keduanya;
  nama event jurnal identik di semua task; ambang
  0,60/0,15/3,0 dtk konsisten dengan spike.
