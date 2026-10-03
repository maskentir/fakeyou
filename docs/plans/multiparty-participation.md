# Partisipasi meeting multipihak: identitas speaker dan giliran

> **Untuk agentic worker:** gunakan `subagent-driven-development` atau `executing-plans`. Jalankan task berdasarkan dependensi; jangan melewati gate media dengan tebakan nama. Prompt orkestrasi ada pada [handoff Muse](meeting-next-handoff.md).

**Tujuan:** twin mengetahui kapan pertanyaan ditujukan kepadanya, tetap diam saat peserta lain berbicara, dan mengatribusikan speaker hanya ketika bukti sesi mencukupi.

**Arsitektur:** observasi Meet dan waktu audio menyediakan evidence, resolver pure mempertahankan unknown pada kondisi ambigu, lalu policy pure memilih reply/silent/clarify sebelum model dipanggil. Semua transkrip tetap direkam; identitas yang diamati tidak memberikan kewenangan owner.

**Stack teknologi:** Python 3.12, Playwright/CloakBrowser yang terpasang, Pipecat 1.12.0, LangChain 1.4.2, LangGraph 1.2.12, Graphiti 0.30.2, Pydantic 2, pytest.

**Spesifikasi:** bagian [Kontrak desain](#kontrak-desain). Prasyarat pilot ialah gate stop dan ingestion pada [meeting-hardening](meeting-hardening.md). Baseline implementasi memakai commit hasil hardening yang telah diverifikasi; bukan `master` lama yang belum memuat trust.

**Status:** rencana, disusun 3 Oktober 2026. Gate media belum dijalankan. PCM produksi sekarang campuran mono; timestamp final STT bukan otomatis waktu audio sumber. Jangan mengklaim channel per participant sudah tersedia.

## Batasan global

- Tidak menambah dependency, memperbarui lockfile, menyalin seluruh stack Vexa, mem-patch private SDK, atau mengganti transport audio dalam plan ini.
- Python Inggris tanpa komentar/docstring baru; dokumentasi Bahasa Indonesia. Tidak menambah legacy alias/re-export.
- Tidak mengubah `.env`, backup, stash, persona privat, record lama, atau namespace `acc2`/`acc3`. Semua probe memakai host yang setuju dan sesi/grup sintetis baru.
- Tidak ada speaker name dari tebakan model, speaker label STT disamakan dengan nama orang, roster list disamakan dengan pembicara aktif, atau pemetaan owner dari display name.
- Streaming tetap mati; cap dua kalimat, fallback/timeout provider, namespace dan stop hardening dipertahankan. Tanpa LLM tambahan untuk memilih giliran.
- Tidak ada chat, raise hand, kamera/avatar, approval kontrak, remote control owner, atau operasi multi-meeting otomatis.
- Tidak ada push/merge, komunikasi pihak luar, xfail baru, penghapusan kasus gagal, atau pelonggaran threshold.

## Kontrak desain

### 1. Level identitas dan kewenangan

- `claimed_name`: isi yang diklaim peserta pada transcript; tidak membuktikan participant ID maupun owner.
- `observed_participant`: ID dan display name yang diamati pada sesi Meet, beserta epoch dan waktu pengamatan. Display name dapat berubah, duplikat, atau meniru owner; ID sesi bukan identitas dunia nyata.
- `configured_owner`: identitas dari konfigurasi/operator. Tidak dikaitkan ke participant hanya karena nama cocok. Binding owner terautentikasi tidak dibangun dalam milestone ini.
- Evidence yang ambigu, stale, overlap, atau beda epoch menghasilkan `unknown`. Unknown adalah hasil yang sah; tetapi sistem yang selalu unknown tidak boleh lulus gate coverage.
- Nama yang diklaim tidak dipersist sebagai verified owner fact. Prompt/Graphiti tetap membedakan klaim peserta dan fakta owner.

### 2. Gate media sebelum memberi nama

- Baca source Vexa lokal sebagai referensi read-only: `gmeet-speakers.ts`, `gmeet-capture-v1.ts`, `gmeet-channel-binder.ts` di `core/meetings/modules/gmeet-capture/src/`. Nama CSS obfuscated/auto-learning bukan fondasi yang diterima.
- Vexa mempunyai channel-energy binding; worker ini belum mempunyai channel per participant. Jangan mengimplementasikan port binder yang menerima channel palsu dari PCM campuran.
- Probe roster, active-speaker hint, marker self, serta awal/akhir audio source menggunakan clock yang sama. Catat selektor/atribut yang benar-benar teramati, provider endpoint mode, epoch reconnect, dan ground truth host.
- Nama roster saja tidak cukup. Final STT dapat tiba ketika speaker berikutnya sudah aktif; jangan mengambil tile saat final tiba untuk memberi nama ucapan sebelumnya.
- Gate lulus hanya jika awal/akhir audio yang sesuai dengan satu transcript dapat dikorelasikan ke timeline speaker dalam epoch yang sama tanpa buffering yang menambah latency di luar gate. VAD bisa menggabungkan beberapa utterance provider; raw VAD interval saja bukan bukti otomatis satu transcript.
- Bila SDK/audio tidak menyediakan alignment yang cukup, produksi mempertahankan `unknown`. Tulis laporan negatif yang menjelaskan kontrak hilang; jangan membuat adapter private atau migrasi per-channel dalam plan ini. Policy pure dan routing konservatif tetap dapat diuji/dipasang, tetapi feature atribusi live belum selesai.

### 3. Resolver konservatif

- Simpan timeline ring-buffer paling lama 120 detik per sesi; polled snapshot maksimal tiap 250 ms. Tidak menyimpan screenshot/video/audio mentah untuk evidence rutin.
- Tiap `SpeechWindow` mempunyai start/end source-clock, epoch, serta transcript ID. Penamaan hanya jika sepanjang interval teramati tepat satu participant non-self yang sama, tidak ada overlap, coverage minimal 95%, dan gap antar-observasi maksimal 500 ms. Boundary awal/akhir yang tidak cukup diamati menahan atribusi.
- Threshold 95%/500 ms adalah kontrak pilot yang ditetapkan sebelum pengukuran; kegagalan tidak ditangani dengan menurunkan angka sesudah hasil diketahui.
- Roster hilang, participant leave, duplicate display names, self marker hilang, clock mismatch, interval negatif/nonfinite, atau reconnect reset menghasilkan unknown. ID dipakai sebagai key; display name bukan key.
- Source `dom_hint` menghasilkan atribusi observed dengan batas bukti, bukan identitas terverifikasi. Jangan mengubah `speaker_verified` menjadi true hanya karena UI menampilkan nama.
- Label STT pada mode diarization tetap label anonim. Mode `ENDPOINTING` produksi tidak diganti tanpa eksperimen terpisah karena probe lama mencatat latency dan giliran hilang.

### 4. Policy kapan menjawab

- Transkrip final selalu direkam sebelum policy; silent tidak menghapus ucapan peserta dari record/ingestion.
- Pertanyaan yang secara eksplisit dialamatkan ke twin dapat dijawab walaupun speaker unknown, tetapi sapaan tetap netral dan tidak ada kenaikan kewenangan.
- Alamat eksplisit berarti vocative di awal utterance dengan nama twin/alias yang dikonfigurasi operator; penyebutan nama dalam kutipan atau kalimat tentang twin bukan alamat. Grammar dibatasi dan diuji, tanpa classifier LLM.
- Participant lain sedang berbicara atau overlap menghasilkan silent/defer; jangan menyela agar bisa klarifikasi. Keputusan baru dibuat dari final berikutnya/turn boundary yang sah; jangan menjadwalkan timer yang berbicara spontan.
- Pertanyaan lanjutan dalam activation window hanya memakai konteks participant ID yang sama. Participant berbeda tidak mewarisi konteks pertanyaan orang sebelumnya.
- Jika follow-up beralamat eksplisit tetapi identitas/referennya ambigu dan floor kosong, pilih satu klarifikasi netral: `Maksudnya demo atau kickoff?`/padanan yang sesuai konteks. Unknown tanpa alamat eksplisit tetap silent; jangan memetakannya ke participant terakhir. Klarifikasi harus berasal dari rute agent yang sudah ada dengan konteks ambigu, bukan template yang mengarang kandidat.
- Tanpa alamat atau follow-up yang sah, silent. Jangan menambahkan raise hand/chat atau proaktivitas umum.
- `participant_policy_enabled: bool = False` pada `ConversationSettings` menjadi flag pilot eksplisit. Default mempertahankan baseline sampai gate lulus; flag true tidak boleh mengaku speaker known ketika media gate gagal. Manifest/report mencatat flag. Tidak menambah nama env alias.

## Model dan antarmuka

Modul pure baru `src/fakeyou/participants.py` memiliki tipe berikut:

```python
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class Participant:
    id: str
    display_name: str
    epoch: str
    is_self: bool


@dataclass(frozen=True, slots=True)
class SpeakerSample:
    at_ms: float
    epoch: str
    active_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SpeechWindow:
    transcript_id: str
    epoch: str
    started_ms: float
    ended_ms: float


@dataclass(frozen=True, slots=True)
class Attribution:
    participant_id: str | None
    display_name: str | None
    status: Literal["observed", "unknown"]
    reason: str
    coverage: float
```

- `participants.attribute_speaker(window: SpeechWindow, samples: Sequence[SpeakerSample], roster: Mapping[str, Participant]) -> Attribution` memakai threshold tetap pada desain. Clock memakai monotonic source-ms yang sama; sample UTC tidak dicampur tanpa transformasi yang terbukti.
- `src/fakeyou/participant_observer.py` baru menyediakan `ParticipantObserver(page: Page, *, epoch: str, monotonic: Callable[[], float])`, `async start() -> None`, `async close() -> None`, serta `snapshot() -> tuple[tuple[SpeakerSample, ...], Mapping[str, Participant]]`. Snapshot timeline/roster immutable dan konsisten pada epoch yang sama. Clock injectable mengembalikan detik monotonic, dikonversi ke source-ms melalui transformasi yang dibuktikan Task 0. Observer read-only; kehilangan sinyal menghasilkan unknown, tidak menjalankan pemulihan CSS otomatis.
- `src/fakeyou/participation.py` baru menyediakan `ParticipationContext` immutable: `addressed`, `followup`, `same_participant`, `ambiguous`, `others_speaking`, `activation_valid` berupa bool. `ParticipationDecision` mempunyai `action: Literal["reply", "silent", "clarify"]` dan `reason: str`.
- `participation.choose_participation(context: ParticipationContext) -> ParticipationDecision` murni, tanpa IO/model call. Keputusan reply/clarify menjalankan responder existing hanya ketika floor tersedia.
- Tambahkan `participant_id: str | None = None` dan `speaker_evidence_id: str | None = None` pada `TranscriptEvent`/`TranscriptEntry`; field `participant` yang ada tetap merupakan label yang dapat dibaca atau `unknown`. Record historis tanpa field baru tetap valid tanpa ditebak ulang.
- Evidence observer disimpan sebagai metadata append-only terpisah dari isi transkrip dalam direktori record sesi; event journal hanya ID, method, status, reason, coverage, dan timestamp. Tidak menyimpan nama/teks mentah pada journal umum.
- `_request_messages()` meneruskan participant ID/evidence/status sebagai data `meeting_event`; data tidak boleh menulis owner ID, tool list, atau namespace.

## Peta file

| Pemilik | File |
| --- | --- |
| Agent resolver | `participants.py`, `tests/test_participants.py` baru |
| Agent policy | `participation.py`, `tests/test_participation.py` baru |
| Agent observasi | `participant_observer.py`, `tests/test_participant_observer.py` baru; patch `meet.py` diajukan ke integrator |
| Integrator | `conversation.py`, `assembly.py`, `cli/join.py`, `audio.py` bila public hook sumber tersedia, `record.py`, `ingestion.py`, `settings.py`, `report.py`, `persona.py` dan tes terkait |
| Reviewer/eval | `evals/fixtures/multiparty-scenarios.json`, `evals/test_multiparty_eval.py`, runbook baru `docs/runbooks/multiparty-acceptance.md` |

## Task 0 — Baseline dan gate media read-only

- [ ] Buat `feat/multiparty-participation` dari kandidat hardening yang memuat trust; pertahankan pekerjaan lokal. Catat commit dan hasil gate stop/ingestion. Jika gate A belum live, unit/observasi tetap boleh maju tetapi pilot B menunggu.
- [ ] Baca callback audio/VAD/STT native yang tersedia. Catat bahwa `transcript_event()` sekarang tidak mengikat user_id/frame diarization ke nama participant, dan timestamp final bukan interval source.
- [ ] Dengan host baru, jalankan probe dua manusia bergantian, satu overlap, satu reconnect. Simpan timeline ground truth dan source-clock, bukan hanya screenshot ketika final muncul.
- [ ] Tulis laporan gate media dengan contoh berhasil/gagal, kontrak yang tersedia, dan selector evidence. Jika interval transcript↔audio tidak dapat dibuktikan, tandai atribusi belum terbukti dan jangan memasang penamaan produksi.
- [ ] Bekukan struktur evidence dan kepemilikan file. Jangan melanjutkan dengan confidence skor buatan agar coverage terlihat tinggi.

## Task 1 — Resolver dan identity model pure

- [ ] Tulis tes merah untuk satu speaker stabil, pergantian speaker sebelum final STT, overlap, self, stale sample, duplicate name, leave, reconnect, dan clock mismatch.

```python
from fakeyou.participants import Participant, SpeakerSample, SpeechWindow, attribute_speaker


def test_overlap_preserves_unknown():
    roster = {
        "a": Participant("a", "Budi", "e1", False),
        "b": Participant("b", "Rani", "e1", False),
    }
    samples = [SpeakerSample(t, "e1", ("a", "b")) for t in range(0, 1001, 250)]
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), samples, roster)
    assert result.status == "unknown"
    assert result.participant_id is None
```

- [ ] Jalankan `uv run --no-sync pytest tests/test_participants.py -q`; pastikan kontrak baru gagal.
- [ ] Implementasikan coverage berbasis durasi interval, bukan jumlah snapshot; periksa freshness, epoch, finite clock dan roster ID. Jumlah poll yang tinggi tidak boleh memalsukan coverage interval yang hilang.
- [ ] Tes urutan sample terbalik/duplikat dan nilai metadata bertentangan: reject/unknown eksplisit, bukan memilih nama terakhir. Nama sama pada ID berbeda tidak digabung.
- [ ] Lint/typing, review, lalu commit lokal oleh orkestrator.

## Task 2 — Policy reply/silent/clarify pure

- [ ] Tulis matriks merah: explicit-address unknown, pertanyaan untuk peserta lain, menyebut twin dalam kutipan, follow-up speaker sama/berbeda/unknown, overlap, activation expired, dan ambiguous reference.

```python
from fakeyou.participation import ParticipationContext, choose_participation


def test_another_speaker_does_not_inherit_activation():
    context = ParticipationContext(
        addressed=False,
        followup=True,
        same_participant=False,
        ambiguous=False,
        others_speaking=False,
        activation_valid=True,
    )
    assert choose_participation(context).action == "silent"
```

- [ ] Jalankan `uv run --no-sync pytest tests/test_participation.py -q`; implementasikan priority: occupied floor→silent; explicit address→reply/clarify menurut ambiguity; follow-up sah speaker sama→reply/clarify; selain itu→silent.
- [ ] Context `same_participant=True` hanya dapat dibuat dari dua ID observed yang sama pada epoch yang sama. Dua nilai `unknown` tidak dianggap speaker sama. Tambahkan invariant test pada builder context integrasi.
- [ ] Tes policy tidak memanggil model/tool dan tidak mengubah identitas atau record. Lint/typing, review, commit lokal oleh orkestrator.

## Task 3 — Observer read-only dan lifecycle

- [ ] Tulis DOM fixture sesuai selector yang benar-benar ditemukan Task 0, bukan CSS tebakan. Tes empty roster, self marker, nama duplikat, participant leave, element stale, dan poll exception.
- [ ] Implementasikan observer dengan public Playwright Page dan clock injectable. Ring-buffer dibatasi 120 detik; cleanup membatalkan polling dan tidak melakukan click, membuka mic, atau mengubah perangkat.
- [ ] Gunakan pengamatan active-speaker sebagai hint; jangan menghubungkannya ke transcript tanpa SpeechWindow yang lolos gate media. Jika tidak ada alignment, keluaran tetap unknown.
- [ ] Tes `close()` dua kali, cancellation saat page read, epoch reset, polling tidak membocorkan task setelah leave, dan exception observer tidak menghentikan audio.
- [ ] Jalankan `uv run --no-sync pytest tests/test_participant_observer.py -q`, lint/typing, review, commit lokal oleh orkestrator.

## Task 4 — Integrasi recording dan routing

- [ ] Satu integrator mengedit file bersama. Pasang flag `participant_policy_enabled` canonical dan validasi configured twin aliases sebelum runtime; jangan membuat alias env lain.
- [ ] Rekam semua final sebelum routing. Builder context memakai clock/evidence dari sesi yang sama; unknown tidak berubah menjadi last known speaker. Policy silent berarti nol responder/TTS call.
- [ ] Pertahankan cancellation hardening: utterance yang tidak ditujukan ke twin tidak menjadwalkan jawaban, tetapi speech onset yang sah tetap menghentikan playback ketika barge-in aktif. Floor occupied tidak menghasilkan klarifikasi baru.
- [ ] Perbarui record/ingestion untuk membawa participant ID dan label observed tanpa menganggapnya owner. Nama display yang diubah peserta tidak mengubah namespace, kewenangan, atau sumber owner note.
- [ ] Tambahkan report keputusan policy, coverage atribusi, alasan unknown, jumlah utterance direkam, dan jumlah jawaban. Semua utterance silent tetap tercatat; tidak mengurangi denominator agar precision terlihat bagus.
- [ ] Tes proses produksi dengan fake page/audio/agent: silent tidak memanggil model, explicit question unknown dijawab netral, follow-up peserta B tidak memakai referen A, stop tidak menghasilkan observer/audio susulan.
- [ ] Jalankan suite offline penuh, eval lama/trust, build dan tes image. Verifikasi source image sama dengan kandidat; commit lokal setelah review.

## Task 5 — Acceptance multipihak

- [ ] Dua sesi uji dengan dua pembicara manusia, ≥60 utterance ground truth total dan ≥20 jawaban twin utuh. Host menandai speaker sebenarnya, target pertanyaan, overlap, dan waktu; label model bukan ground truth.
- [ ] Sertakan gantian cepat sebelum final STT, overlap, nama sama pada dua participant, rename, participant leave/rejoin, reconnect, pertanyaan ke peserta lain, pertanyaan langsung ke twin, dan follow-up berpindah penutur.
- [ ] Gate atribusi pada sampel: nol salah participant pada output observed; coverage observed ≥80% pada subset solo dengan sumber yang cukup; overlap/ambiguitas tetap unknown. Laporkan seluruh denominator dan alasan exclusion. Jika Task 0 gagal alignment, gate ini tetap belum terbukti walaupun policy lulus.
- [ ] Gate policy pada sampel: nol jawaban untuk target peserta lain dan occupied floor; ≥90% pertanyaan explicit-address yang dapat dijawab memperoleh reply/clarify yang sesuai; silent universal tidak dapat lulus.
- [ ] Uji stop saat model dan audio, resume ingestion, serta recall speaker/source setelah re-ingest namespace uji. Tetap ukur stop SLA dari plan A dan latency lama; jangan mengubah endpointing atau membuang sampel lambat.
- [ ] Review semua output, klaim kewenangan, dan atribusi. Catat verdict terpisah: media alignment, atribusi, policy, kualitas trust, stop/ingestion, fungsi live, dan performa.
- [ ] Tulis laporan bertanggal eksekusi dan status proyek. Jika binding memerlukan perubahan capture per-channel atau SDK, akhiri dengan laporan negatif serta kebutuhan desain berikut; jangan menyebut implementasi atribusi selesai.

## Swarm dan gate lokal

Setelah gate source diperiksa, resolver dan policy dapat paralel; observer bekerja pada file sendiri. Integrasi memiliki satu writer. Reviewer berbeda memeriksa spec lalu kode; orkestrator menjalankan tes sendiri. Ledger tetap pada `.runtime/meeting-next/swarm-status.json`, dengan prefix task B dan commit baseline A yang persis.

```sh
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
uv run --no-sync pytest -m 'not live and not container' -q --tb=short
uv build
git diff --check
```

Jangan menyatakan siap menggantikan owner tanpa pengawasan dari zero-error pada corpus/sampel kecil. Perluasan berikutnya memerlukan brief/kewenangan granular, tindakan teramati, serta penanganan kegagalan yang telah diuji.
