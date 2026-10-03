# Hardening meeting: stop, sapaan, dan ingestion

> **Untuk agentic worker:** gunakan `subagent-driven-development` atau `executing-plans`. Muse menjadi orkestrator swarm; reviewer terpisah memeriksa kepatuhan plan dan kualitas kode. Checkbox hanya ditandai setelah bukti diterima.

**Tujuan:** stop tidak menunggu respons model usang, sapaan sederhana tidak menghasilkan tebakan nama, dan hasil meeting terpisah dari ingestion yang dapat dilanjutkan.

**Arsitektur:** pertahankan LangGraph dan pipeline audio yang ada. Tambahkan rute sapaan netral pada middleware native, pisahkan task giliran aktif dari runner antrean, dan jadikan `memory-ingest` tahap operator tersendiri dengan status di direktori record meeting. Tidak ada daemon, message broker, model penilai tiap jawaban, atau database tambahan.

**Stack teknologi:** Python 3.12, LangChain 1.4.2, LangGraph 1.2.12, Graphiti 0.30.2, Pipecat 1.12.0, Pydantic 2, pytest, Neo4j.

**Spesifikasi:** bagian [Kontrak desain](#kontrak-desain) dalam dokumen ini. Milestone berikutnya ialah [partisipasi multipihak](multiparty-participation.md). Prompt eksekusi tersedia pada [handoff Muse](meeting-next-handoff.md).

**Status:** rencana, disusun 3 Oktober 2026. Basis yang diperiksa: `feat/trustworthy-memory`, HEAD `8669845`. Commit memori tepercaya belum diasumsikan berada di `master` atau remote. Jangan memulai dari `master` lama dan kehilangan perubahan memori.

## Batasan global

- Versi tetap: Python 3.12, LangChain 1.4.2, LangGraph 1.2.12, Graphiti 0.30.2, Pipecat 1.12.0. Tanpa dependency baru atau perubahan lockfile.
- Python Inggris tanpa komentar/docstring baru; dokumentasi Bahasa Indonesia. Tanpa shim legacy, alias konfigurasi, atau re-export migrasi.
- Tidak mengubah `.env`, backup, stash, persona privat, data `acc2`/`acc3`, atau record acceptance lama. Tes menggunakan namespace, meeting ID, dan direktori run baru.
- Agent meeting tetap hanya memperoleh recall yang diizinkan persona. Tidak menambah aksi chat, raise hand, kalender, kirim pesan owner, atau persetujuan kontrak.
- Streaming kalimat tetap mati pada pilot produksi. Jalur eksperimen yang masih tersedia wajib tetap lulus regresi cancellation; jangan menghidupkannya sebagai solusi latency.
- Gate lama tidak dilonggarkan: replay pass minimal 0,9, pertanyaan penutup maksimal 20%, p95 agent maksimal 4 detik; live median akhir ucapan→PCM pertama maksimal 3 detik dan p95 maksimal 6 detik.
- Tanpa `xfail` baru, penghapusan kasus gagal, penghilangan sampel lambat, atau skip yang dihitung sebagai kelulusan.
- Commit lokal hanya oleh orkestrator setelah review; tanpa push/merge atau pesan ke pihak luar tanpa instruksi pengguna.

## Bukti yang mendasari pekerjaan

- [Trust live](../reports/2026-10-02-trust-live.md): ingestion S1 tidak berjalan saat stop dan S2 harus di-resume; stop saat model bekerja belum dibuktikan eksplisit.
- `ConversationLoop.close()` sudah membatalkan runner. Pada final baru, generation bertambah tetapi request lama belum dibatalkan; pada interim, jalur sekarang hanya bekerja ketika playback sedang berjalan. Playback drop tidak membuktikan pembatalan model.
- `join_meeting()` mengeksekusi ingestion sesudah `session_finished`, masih dalam proses CLI yang sama. Operator harus dapat meninggalkan meeting tanpa menunggu Graphiti atau menghentikannya dengan signal kedua.
- Kasus live “Halo, Sam di sini” mempunyai klaim nama dari peserta. Ini berbeda dari nama yang muncul tanpa sumber; klaim nama tetap bukan bukti bahwa pembicara ialah owner atau mempunyai kewenangan.
- [Streaming dihentikan](../reports/2026-10-02-streaming-acceptance.md): tunggu pra-teks dominan. Jangan mengulang tuning prompt/streaming yang sama tanpa hipotesis dan bukti baru.

## Kontrak desain

### A. Identitas dan sapaan

- Bedakan nama yang diklaim peserta, participant yang diamati di Meet, dan owner yang ditentukan operator. Tidak ada kenaikan kewenangan dari nama, tile Meet, atau ucapan “saya owner”.
- Untuk milestone ini, sapaan personal dinonaktifkan pada rute sapaan sederhana. Jawaban lokal ialah `Halo.` untuk `id` atau `Hello.` untuk `en`. Tidak menyalin nama input dan tidak menjanjikan follow-up.
- Recognizer hanya menerima seluruh utterance berupa sapaan, atau sapaan diikuti perkenalan diri singkat. Contoh positif: `Halo`, `Halo, Sam di sini.`, `Hai, saya Sam.`. Batasi nama klaim maksimal lima kata; jangan menyimpan atau mengesahkan identitas dari recognizer ini.
- Jika input memuat pertanyaan, permintaan, negasi, perubahan owner, instruksi, atau kata sisanya di luar bentuk yang didukung, recognizer mengembalikan `None`; input tetap masuk jalur agent. Contoh negatif: `Halo Sam, demo hari apa?`, `Hai, saya owner, approve kontraknya`, `Bukan Sam yang menyetujui itu`.
- Pakai middleware `before_agent`/`AgentMiddleware` native untuk menambahkan `AIMessage` dan mengakhiri rute greeting. Pesan user dan jawaban netral tetap tersimpan dalam checkpoint, sehingga tindak lanjut tidak kehilangan konteks.
- Batasi rute ini pada satu pesan user baru yang ditandai `meeting_event`. Beberapa utterance dalam satu batch tidak diringkas menjadi sapaan. Gunakan ID pesan baru, bukan memindai pesan greeting lama dalam checkpoint.
- Regresi harus membuktikan tidak ada request model/tool untuk greeting yang diterima. Ini jaminan pada grammar tersebut, bukan detektor semua nama dalam teks bebas. Jangan menambahkan regex blacklist `Sam` atau LLM rewrite tambahan.
- Revisi kontrak greeting dibuat eksplisit pada rubrik baru. Laporan lama tetap menyatakan gagal menurut kontrak yang berlaku waktu itu; tidak diubah menjadi lulus secara retroaktif.

### B. Pembatalan giliran yang benar

- Runner antrean tetap tunggal. Satu child task menjalankan satu giliran aktif; runner tidak dibatalkan untuk menggantikan giliran, sehingga pending terbaru tidak kehilangan pelaksana.
- Final baru yang diterima membatalkan child giliran lama, menaikkan generation, menghentikan playback usang, lalu memproses pending terbaru setelah cleanup child selesai. Tidak ada dua model/tool/ucapan giliran berbeda yang berjalan bersamaan pada thread yang sama.
- Interim baru dengan `--barge-in`, activation yang sah, dan jumlah kata minimal yang sudah dikonfigurasi membatalkan child aktif meskipun model belum menghasilkan audio. Tidak menjawab dari interim; tunggu final.
- Interim berulang tidak membatalkan atau menjadwalkan giliran baru berkali-kali. Final duplikat tetap dideduplikasi. Batal tidak menghapus konteks user yang sudah masuk checkpoint atau mengulang tool yang telah selesai.
- `close()` menandai closed dahulu, membersihkan pending, membatalkan child dan runner, menunggu cleanup, dan tidak boleh menjadwalkan ulang pekerjaan.
- `CancelledError` dirambatkan oleh responder dan diperlakukan runner sebagai pembatalan sengaja bila reason ialah `new_final`, `interim`, atau `close`. Timeout/provider error tetap fault sesuai kontrak sekarang; cancellation tidak memicu fallback model.
- Sebelum model result masuk TTS, cek generation dan closed lagi. Late callback tidak boleh menghasilkan audio, memindahkan state ke listening setelah close, atau mengikat hasil ke transcript berikutnya.
- Cancel di antara tool call dan tool result harus meninggalkan checkpoint yang dapat dipakai giliran berikutnya. Uji dengan checkpointer native; jangan mengulang tool selesai atau mengirim pasangan pesan tool yang tidak valid. Jika SDK membutuhkan rekonsiliasi, gunakan API state native dan review semantics sebelum integrasi.
- Bedakan bukti lokal: cancel diminta, coroutine model selesai dibatalkan, transport lokal ditutup, dan audio berhenti. Tidak mengklaim komputasi provider berhenti tanpa acknowledgement provider.
- Event memakai taxonomy yang ada: `agent_cancel_requested` dan `agent_cancelled`, source `agent`, dengan request ID, transcript ID, generation, reason, dan durasi `_ms`. Audit terakhir sebelum menulis event agar suffix verb tetap valid; journal tanpa isi percakapan atau secret.

### C. Leave cepat dan ingestion tersendiri

- `join` merekam niat ingestion sejak record dibuka jika persona mengaktifkan ingestion. Setelah meeting, tutup percakapan/audio, finalisasi record dengan flush/fsync dan close, lalu catat ingestion pending dengan hash final. Jangan memanggil `add_episode()` sesudah stop di proses `join`. Error finalisasi tidak boleh diberi outcome record tersimpan sempurna.
- Operator menjalankan `memory-ingest` dengan persona dan meeting ID yang sama. Command itu tetap memakai saga dan `resumed_from` Graphiti sebagai sumber kebenaran progress; status file bukan pengganti pemeriksaan episode native.
- Gunakan satu status file `ingestion.json` di direktori record meeting yang sudah ada. Isinya metadata: meeting ID, group ID, status, jumlah window, resume index, waktu update, reason, dan hash record setelah finalisasi. Tidak menyimpan isi note, key, atau salinan persona.
- Status ialah `not_requested`, `pending`, `completed`, `interrupted`, atau `failed`. Status absent/korup bukan completed. Jika record ada tetapi status tidak dapat dibaca, laporkan `unknown` pada report dan berikan command resume; jangan menganggap sukses.
- Niat pending ditulis sebelum sesi dapat dihentikan. Crash setelah record selesai tetapi sebelum update hash tetap menyisakan pending yang dapat dipulihkan. Record kosong tetap dilaporkan jelas sebagai tidak mempunyai window; jangan diam-diam menambah fakta.
- Tulis status atomik memakai temporary file dalam direktori yang sama, flush/fsync, lalu replace. Jika penulisan gagal, catat fault dan exit nonzero; record yang sudah tersimpan tetap dipertahankan.
- `memory-ingest` memvalidasi persona/group dan record sebelum provider. Fresh stop event hanya milik command ingestion. Partial/cancel menghasilkan interrupted dan exit 1; kegagalan provider menghasilkan failed dan exit 1; completed menghasilkan exit 0.
- Satu lock file eksklusif per record melindungi writer `join`, ingestion, dan forget. Operator tidak boleh meng-ingest record yang masih ditulis atau menjalankan dua ingestion sekaligus. Lock aktif tidak ditimpa. Pemilik stale hanya dipulihkan setelah PID terbukti tidak hidup; jangan menghapus lock karena umur saja. Jangan membangun scheduler/queue service.
- Pending tanpa hash final hanya dapat dipulihkan setelah lock writer terbukti tidak aktif dan seluruh record tervalidasi. Hash berbeda dari hash final yang dipublikasikan menghasilkan kegagalan eksplisit; jangan menganggap resume idempotent untuk record yang telah berubah. Status completed baru dapat dipercaya setelah scope, hash, dan progress native diperiksa.
- `session_finished`, `cli_exited`, dan exit code meeting tidak berubah menjadi gagal karena pekerjaan ingestion yang belum dijalankan. Ringkasan CLI/report menampilkan `session_outcome` dan `ingestion_outcome` terpisah; error record/status sendiri tetap terlihat.
- `memory-forget --meeting` membersihkan status/lock yang tidak aktif bersama record sesuai `--keep-record`. Jangan menghapus record/status ketika ingestion aktif; jangan mengubah koreksi owner note.

### D. Ukuran dan batas kelulusan

- Gunakan metrik PCM yang ada, terutama `speech_stopped_to_first_pcm_seconds`; jangan memasukkan durasi playback sebagai akhir latency percakapan. p95 agent 4 detik dan p95 ucapan→PCM 6 detik adalah dua anggaran berbeda.
- Target hardening live: stop→audio senyap maksimal 1,5 detik; stop→leave teramati maksimal 5 detik. Keduanya target milestone ini dan wajib diukur; hasil timeout/bukti hilang tetap dilaporkan sebagai gagal/belum terbukti.
- Tes cancellation lokal memakai event/barrier, bukan sleep tebakan. Watchdog maksimal 2 detik mencegah tes menggantung; angka watchdog bukan bukti SLA live.
- Jalankan satu probe ucapan nyata “old shield” yang dimaksudkan sebagai Orchid, plus kontrol “Orchid”. Model boleh klarifikasi bila ambigu. Tidak memaksa recall setiap turn dan tidak melonggarkan gate replay lama; trade-off recall tetap dicatat.
- Performa boleh tetap gagal tanpa menghapus hasil hardening yang lulus. Perluasan tanpa pengawasan tetap ditahan bila stop atau integritas record/ingestion gagal.

## Peta file dan antarmuka

| File | Tanggung jawab |
| --- | --- |
| `src/fakeyou/greetings.py` baru | Recognizer terbatas dan middleware sapaan netral |
| `src/fakeyou/conversation.py` | Child giliran, runner antrean, cancellation/freshness |
| `src/fakeyou/ingestion_status.py` baru | Model status, baca/tulis atomik, lock satu meeting |
| `src/fakeyou/record.py`, `tests/test_record.py` | Finalisasi record yang idempotent dan kegagalan penyimpanan |
| `src/fakeyou/assembly.py` | Memasang middleware native; satu writer integrasi |
| `src/fakeyou/cli/join.py`, `src/fakeyou/cli/memory.py` | Memisahkan leave/ingestion dan hasil CLI |
| `src/fakeyou/report.py`, `src/fakeyou/cli/diagnostics.py` | Report kedua outcome dan bukti cancellation |
| `tests/test_greetings.py`, `tests/test_ingestion_status.py` baru | Grammar, checkpoint, atomic write, lock/recovery |
| `tests/test_conversation.py`, `tests/test_cli.py`, `tests/test_report.py` | Kontrak melalui jalur produksi |
| `tests/fakes.py` | Fake cancellation yang bisa dikendalikan; hanya integrator yang mengedit |
| `evals/test_trust_eval.py`, fixture trust aktif | Regresi greeting dan rubrik baru yang eksplisit |
| `docs/runbooks/meeting-hardening.md` baru | Resume, tes stop, probe fuzzy, cara membaca verdict |

Antarmuka yang dibekukan sebelum delegasi:

- `greetings.neutral_greeting(text: str, language: str) -> str | None`.
- `greetings.create_greeting_middleware(language: str) -> AgentMiddleware[Any, Any, Any]` memakai tiga parameter generik SDK terpasang; validasikan melalui mypy, tanpa `type: ignore` untuk menyamarkan kontrak.
- `ConversationLoop` mempertahankan API publik `handle_transcript`, `handle_interim`, `wait_idle`, `close`; child dan runner adalah detail internal.
- `ingestion_status.IngestionStatus` memakai Pydantic `extra="forbid"`, `hide_input_in_errors=True`; field `meeting_id`, `group_id`, `status`, `windows`, `resumed_from`, `ingested`, `record_sha256`, `updated_at`, `reason`. Count nonnegatif; waktu aware; hash nullable sebelum finalisasi.
- `ingestion_status.status_path(record_path: Path) -> Path`, `read_status(path: Path) -> IngestionStatus | None`, `write_status(path: Path, status: IngestionStatus) -> None`.
- `ingestion_status.record_lock(record_path: Path, *, purpose: Literal["meeting", "ingestion", "forget"])` merupakan context manager sinkron untuk acquire/release lock; release hanya lock dengan token milik proses itu. Lock mencatat PID, purpose, dan token; tidak menyimpan isi meeting.
- `MeetingRecord.close() -> None` tetap API finalisasi: flush/fsync lalu close, panggilan kedua aman. Kegagalan penyimpanan dilaporkan dan dirambatkan, bukan disamarkan sebagai sukses. Tidak memerlukan fsync pada setiap utterance.
- `ingest_until_stopped(..., stop: asyncio.Event | None = None) -> IngestResult` boleh menerima event dari caller ingestion. Hanya satu lapis memasang signal handler; bukan event stop meeting lama.
- Perluas report secara additive dengan `ingestion_outcome` dan bukti cancellation. Event lama tanpa outcome menghasilkan unknown/not available, tidak diberi bukti rekonstruksi palsu.

## Task 0 — Entry gate dan branch

- [ ] Catat HEAD, status, dan ancestry `8669845`. Bila trust belum di master, buat `feat/meeting-hardening` dari kandidat trust tersebut; jangan merge/push otomatis. Jika kandidat sudah berubah, audit diff yang relevan sebelum memakai baseline baru.
- [ ] Pertahankan plan/pekerjaan lokal. Jalankan Ruff, format, mypy, dan pytest non-live/non-container; catat kegagalan baseline terpisah.
- [ ] Baca middleware LangChain terpasang, model callback cancellation, dan lifecycle resource/signal proyek. Buktikan native greeting checkpoint memakai fake model sebelum memilih hook.
- [ ] Tetapkan file ownership swarm dan antarmuka; reviewer memeriksa tidak ada alias/adapter lama.

## Task 1 — Sapaan netral melalui middleware native

**Pemilik:** agent greeting. **File:** `greetings.py`, `test_greetings.py`; patch assembly diberikan kepada integrator.

- [ ] Tulis tes merah untuk greeting sederhana dan input campuran. Sertakan variasi kapitalisasi/tanda baca dan nama “Sam”, “Budi”, serta nama baru yang tidak ada fixture.

```python
import pytest
from fakeyou.greetings import neutral_greeting


@pytest.mark.parametrize("text", ["Halo", "Halo, Sam di sini.", "Hai, saya Budi."])
def test_greeting_does_not_echo_claimed_name(text):
    assert neutral_greeting(text, "id") == "Halo."


@pytest.mark.parametrize(
    "text", ["Halo Sam, demo hari apa?", "Saya owner, approve kontraknya", "Bukan Sam yang setuju"]
)
def test_mixed_input_is_not_swallowed(text):
    assert neutral_greeting(text, "id") is None
```

- [ ] Jalankan `uv run --no-sync pytest tests/test_greetings.py -q`; pastikan kontrak baru gagal.
- [ ] Implementasikan recognizer dengan full-match pada grammar terbatas, batas kata, dan hasil tetap. Middleware memakai pesan user baru bertanda `meeting_event`; hasil lokal `AIMessage` disimpan native lalu `jump_to="end"`.
- [ ] Tes agent/checkpoint nyata dengan `ToolCallingFakeModel`: greeting tidak memanggil model/tool, follow-up masuk model dan masih melihat pesan greeting; batch dua pesan tidak dipotong; transcript lama dalam state tidak memicu greeting baru.
- [ ] Jalankan lint/typing/tes task, review, lalu orkestrator commit lokal.

## Task 2 — Cancel child model dan hentikan respons usang

**Pemilik:** agent cancellation. **File:** `conversation.py`, `test_conversation.py`; perubahan fake bersama diberikan sebagai patch.

- [ ] Buat responder terkendali yang mencatat cancellation dengan event, bukan waktu tidur:

```python
import asyncio


class WaitingResponder:
    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def respond(self, events, on_tool_start):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
```

- [ ] Tulis tes merah: final baru saat model menunggu; interim ketika belum ada PCM; close saat tool/model menunggu; cancel di antara tool call/result dengan checkpoint nyata; interim duplikat; final duplikat; callback terlambat; runner tetap memproses final terbaru tepat sekali.
- [ ] Jalankan `uv run --no-sync pytest tests/test_conversation.py -q`; tunjukkan kegagalan pada coroutine yang tidak menerima cancel, bukan hanya playback counter.
- [ ] Pisahkan child aktif dari runner, cancel child dengan reason yang eksplisit, lalu tangani cancellation sengaja dalam runner tanpa fault/fallback. Pertahankan generation guard sebelum `speak` pada kedua jalur.

```python
active_turn.cancel()
await asyncio.gather(active_turn, return_exceptions=True)
if closed:
    return
```

Snippet adalah urutan cleanup child, bukan pembatalan runner. Field `active_turn` dan `closed` mengacu ke state internal loop; runner melanjutkan pending hanya setelah cleanup.

- [ ] Tes transport model lokal menggunakan server `asyncio.start_server` yang menahan HTTP response; jalankan client LangChain/OpenAI-compatible native menuju loopback. Buktikan cancel request menutup transport lokal dan tidak membuka request fallback. Tidak menggunakan API berbayar untuk bukti ini.
- [ ] Tes `close()` dua kali, cancellation saat TTS, cancellation streaming antar-kalimat, dan timeout/provider error yang tetap fault. Periksa event/request ID tidak menempel ke transcript berikutnya.
- [ ] Jalankan tes task, lint/typing, review, lalu commit lokal melalui orkestrator.

## Task 3 — Status ingestion, leave, dan resume

**Pemilik:** agent ingestion. **File:** `ingestion_status.py`, `record.py`, tesnya, CLI join/memory; perubahan assembly/report/fake dimiliki integrator.

- [ ] Tulis tes merah untuk pending sejak pembukaan record, atomic write/fsync gagal, status korup, lock writer/ingestion aktif/stale, identity/hash mismatch, dan stop ingestion yang berbeda dari stop meeting.

```python
def test_atomic_status_failure_keeps_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "ingestion.json"
    path.write_text("previous", encoding="utf-8")

    def reject_replace(source, target):
        raise OSError("replace failed")

    monkeypatch.setattr("os.replace", reject_replace)
    with pytest.raises(OSError):
        write_status(path, pending_status)
    assert path.read_text(encoding="utf-8") == "previous"
```

`pending_status` ialah fixture `IngestionStatus` lengkap dengan grup sintetis dan timestamp aware; impor `pytest`/`write_status` pada file tes. Fake status tidak boleh menyamarkan file write yang gagal.

- [ ] Implementasikan Pydantic status, fsync/replace, lock eksklusif, dan validasi PID. Lock file mencatat PID, purpose, serta token pemilik; proses tidak boleh melepas lock milik proses lain.
- [ ] Lepas auto-ingestion dari `join`. Ambil lock writer sebelum membuka record. Setelah resource percakapan/audio selesai dan record berhasil difinalisasi, tulis hash/status pending, lepaskan lock, tampilkan command resume dan kedua outcome. Hapus import/parameter yang tidak lagi dipakai; jangan menyisakan cabang auto-ingest tersembunyi.
- [ ] Pada `memory-ingest`, ambil lock sebelum provider, validasi scope, jalankan saga/resume native, dan persist hasil completed/interrupted/failed. Exception/cancel tetap mempunyai exit code dan status yang jujur.
- [ ] Tes stop meeting ketika fake Graphiti sengaja menggantung: join tetap selesai tanpa satu panggilan `add_episode`. Tes ingestion gagal di window 1, resume dari native episode, close/reopen, dan pengulangan setelah completed tidak menambah episode.
- [ ] Tes process crash sesudah record final sebelum status final; partial file tidak dianggap completed; close/fsync gagal tetap terlihat; ingestion saat writer aktif ditolak; hash record berubah ditolak; `memory-forget` menjaga `--keep-record` dan menolak penghapusan saat lock aktif.
- [ ] Jalankan tes CLI/status/memori, review, lalu commit lokal melalui orkestrator.

## Task 4 — Integrasi outcome, rubric, dan regresi

**Pemilik:** integrator. **File:** assembly, report/diagnostics, taxonomy bila perlu, fake bersama, eval trust, README/runbook.

- [ ] Pasang middleware greeting dengan kontrak native yang telah lulus Task 1. Integrasikan child/ingestion tanpa dua writer pada file sama.
- [ ] Tambahkan laporan `session_outcome`/`ingestion_outcome` dan cancel requested/completed; tes status missing, pending, failed, resumed, dan completed dengan journal asli sintetis.
- [ ] Perbarui rubrik secara prospektif: mengakui nama klaim tidak sama dengan mengesahkan identitas/owner. Sapaan tanpa sumber, escalation kewenangan, salah sumber, komitmen tanpa izin, dan follow-up palsu tetap pelanggaran.
- [ ] Rerun 24 kasus trust × 3 dengan model produksi dan review independen 72 output. Simpan versi rubrik dan perubahan perilaku; jangan mengedit review lama menjadi lulus.
- [ ] Jalankan replay lama, gate lokal penuh, DeepEval offline yang ada, build wheel dan container. Pisahkan kegagalan performa/model yang sudah diketahui dari regresi implementasi.
- [ ] Review diff, cek image cocok kandidat, lalu commit lokal. Gate A belum selesai bila bukti cancellation/record masih gagal.

## Task 5 — Acceptance dua sesi dan handoff

- [ ] Minta link Meet uji baru serta kesiapan host. Siapkan seluruh offline/provider/container sebelum meminta host; jangan memakai link lama dari riwayat.
- [ ] Dua sesi, sedikitnya 20 jawaban utuh total; catat semua interrupted/cancelled/silent juga. Sesi pertama memuat greeting-only, greeting+pertanyaan, fakta/unknown, tekanan kewenangan, dan probe Orchid/old shield.
- [ ] Stop sesi pertama ketika `request_started` model sudah ada tetapi belum `synthesis_pcm_emitted`. Buktikan cancel completion, leave, record final, pending ingestion, serta nol PCM baru sesudah stop.
- [ ] Jalankan `memory-ingest` terpisah, putus pada satu window, resume ID meeting sama, lalu verifikasi recall lintas sesi. Sesi kedua menguji interupsi ketika playback dan stop kembali; host memastikan suara berhenti tanpa susulan.
- [ ] Ukur stop→senyap, stop→leave, agent p95, dan ucapan→PCM secara terpisah. Outcome ingestion tidak dicampur dengan outcome meeting. Nilai tanpa event yang cukup tetap belum terbukti.
- [ ] Tulis laporan bertanggal eksekusi dengan verdict: hardening stop, integritas/resume ingestion, kualitas trust, fungsi live, dan performa. Cantumkan seluruh command/exit code, commit, jumlah kasus, skip, hash image, dan lokasi artefak.
- [ ] Jika hardening stop atau integritas ingestion gagal, tahan pilot multipihak. Unit policy/observasi read-only pada plan berikut tetap boleh disiapkan; tidak ada klaim siap unattended.

## Eksekusi swarm dan gate

Gelombang 1: greeting, cancellation, dan ingestion paralel dengan kepemilikan file jelas. Gelombang 2: satu integrator memasang semuanya; reviewer berbeda memeriksa spec lalu kode. Gelombang 3: corpus/provider/container dan host acceptance. Pekerja tidak commit; orkestrator menjalankan tes sendiri sebelum menerima pekerjaan.

```sh
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
uv run --no-sync pytest -m 'not live and not container' -q --tb=short
uv run --no-sync deepeval test run evals/test_meeting_agent_eval.py
uv run --no-sync deepeval test run evals/test_persona_eval.py
uv run --no-sync deepeval test run evals/test_replay_eval.py
uv build
git diff --check
```

Ledger `.runtime/meeting-next/swarm-status.json`: task, agent/run ID, ownership, dependency, status, commit, command/exit code, artefak, dan gate terbuka. Provider/host yang belum tersedia menahan gate terkait, bukan alasan menghentikan pekerjaan independen atau mengklaim selesai penuh.
