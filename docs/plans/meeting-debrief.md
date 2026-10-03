# Plan debrief meeting dengan sumber yang dapat diperiksa

> **Untuk agentic worker:** gunakan `subagent-driven-development` atau `executing-plans` sesuai dependensi. Reviewer memeriksa makna item beserta sumber, bukan hanya validitas JSON.

**Tujuan:** owner memperoleh hasil meeting dan keputusan yang perlu ditindaklanjuti, dengan kutipan asli dan batas atribusi yang jelas.

**Arsitektur:** command operator membaca record immutable, membangun indeks sumber/chunk deterministik, meminta structured draft native, memvalidasi referensi, lalu menerbitkan JSON canonical serta Markdown render. Tidak berjalan pada jalur leave, tidak memutasi Graphiti, dan tidak menghubungi owner.

**Stack teknologi:** Python 3.12, LangChain 1.4.2, LangGraph 1.2.12, Graphiti 0.30.2, Pipecat 1.12.0, Pydantic 2, pytest.

**Spesifikasi:** [desain brief/delegasi/debrief](meeting-delegation-design.md), bagian B. Snapshot brief berasal dari [plan A](meeting-brief.md), tetapi record valid tanpa brief tetap dapat diringkas dengan batas sumber yang terlihat.

**Status:** rencana, 3 Oktober 2026. Branch `feat/meeting-debrief` berasal dari kandidat brief yang diverifikasi; pure source/model dapat maju paralel, integrasi baru setelah antarmuka A stabil.

## Batasan global

- Versi tetap: Python 3.12, LangChain 1.4.2, LangGraph 1.2.12, Graphiti 0.30.2, Pipecat 1.12.0. Tanpa dependency baru atau perubahan lockfile.
- Python Inggris tanpa komentar/docstring baru; dokumentasi Bahasa Indonesia. Tanpa shim legacy, alias konfigurasi, atau re-export migrasi.
- Tidak mengubah `.env`, backup, stash, persona privat, data `acc2`/`acc3`, atau record acceptance lama. Tes menggunakan namespace, meeting ID, dan direktori run baru.
- Streaming kalimat tetap mati pada pilot. Cancellation, stop, record lock/finalisasi, namespace, cap dua kalimat, dan routing multipihak dipertahankan.
- Gate lama tidak dilonggarkan: replay pass minimal 0,9, pertanyaan penutup maksimal 20%, p95 agent maksimal 4 detik; live median akhir ucapan→PCM pertama maksimal 3 detik dan p95 maksimal 6 detik.
- Tanpa `xfail` baru, penghapusan kasus gagal, penghilangan sampel lambat, atau skip yang dihitung sebagai kelulusan.
- Commit lokal hanya oleh orkestrator setelah review; tanpa push/merge atau pesan ke pihak luar tanpa instruksi pengguna.

## File dan antarmuka

| File | Tanggung jawab |
| --- | --- |
| `src/fakeyou/debrief_schema.py`, `tests/test_debrief_schema.py` baru | Schema/error bersama, dibekukan sebelum source/extractor paralel |
| `src/fakeyou/debrief_sources.py`, `tests/test_debrief_sources.py` baru | Snapshot byte, parse/indeks, chunk, validasi references |
| `src/fakeyou/debrief.py`, `tests/test_debrief.py` baru | Schema draft/report, panggilan native, merge, publikasi/render |
| `src/fakeyou/cli/debrief.py` baru, `cli/main.py`, `cli/memory.py`, `cli/diagnostics.py` | Command, scope, lock, forget/report stale; integrator |
| `src/fakeyou/ingestion_status.py`, `tests/test_ingestion_status.py` | Tambah purpose lock debrief tanpa lock kedua |
| `tests/test_cli.py`, `tests/test_report.py`, `tests/test_library_contracts.py` | Wiring/operator/kontrak provider |
| `evals/fixtures/debrief-scenarios.json`, `evals/test_debrief_eval.py` baru | Delapan record/expected items dan review gate |
| `docs/runbooks/meeting-debrief.md` baru, README | Operator/render dan batas sumber |

Tipe Pydantic/error berikut berada di debrief_schema.py, frozen/extra forbid/hide_input_in_errors. SourceEntry/RecordSnapshot berada di debrief_sources.py sebagai dataclass immutable. debrief_schema.py hanya memakai import sumber di TYPE_CHECKING jika diperlukan; modul schema tidak mengimpor extractor/CLI. Tidak ada re-export alias:

- `SourceEntry(id: str, text: str, kind: Literal["participant", "twin"], participant_id: str, at: datetime)`; datetime aware. `RecordSnapshot(record_sha256: str, sources: tuple[SourceEntry, ...])`.
- `EvidenceRef(entry_id: str, quote: str)`; quote 1–1.000 karakter. `DebriefItem(kind, text, status, evidence: tuple[EvidenceRef, ...], assignee_claim: str | None = None, due_text: str | None = None)`; kind/status literals desain, teks 1–1.000 karakter, evidence 1–10.
- `ObjectiveFinding(objective_id: str, status: Literal["addressed", "open", "uncertain"], evidence: tuple[EvidenceRef, ...])`; addressed wajib evidence. Pertanyaan memakai finding yang sama dengan ID `question:<id>`, objective memakai `objective:<id>`.
- `DebriefDraft(items: tuple[DebriefItem, ...], findings: tuple[ObjectiveFinding, ...])`; maksimal 100 item/40 finding per chunk. Model tidak mengisi participant ID, hash, source clock, atau review verdict.
- `DebriefReport`: schema_version `meeting_debrief/v1`, owner_id/twin_id/meeting_id, record_sha256, context_sha256 nullable, generated_at aware, provider/model, source_count, chunk_count, status literal completed, review_status literal unreviewed, items/findings, source limitations. Sumber yang dirujuk disertakan sebagai indeks evidence dengan metadata asli; jangan menyalin tool output.
- `DebriefValidationError(ValueError)` untuk referensi/kontrak output invalid.
- `load_record_snapshot(path: Path) -> RecordSnapshot` membaca satu snapshot byte, hash dan parse dari byte yang sama. Parser menggunakan RecordEntry existing tanpa redefinisi schema transcript.
- `load_context_snapshot(path: Path) -> tuple[MeetingContext | None, str | None]` di debrief_sources.py membaca byte sekali, memvalidasi MeetingContext/digest brief dan SHA-256 dari byte yang sama. Missing menghasilkan (None, None), corrupt menghasilkan ValueError; caller memeriksa scope. Context digest bukan persona hash atau brief hash.
- `chunk_sources(sources: Sequence[SourceEntry], *, max_entries: int = 40, max_characters: int = 16000, overlap: int = 4) -> tuple[tuple[SourceEntry, ...], ...]`; reject empty/entry terlalu panjang/>20 chunk sebelum provider.
- `validate_evidence(evidence: Sequence[EvidenceRef], sources: Mapping[str, SourceEntry]) -> None`; unknown ID/quote mismatch/empty evidence menghasilkan DebriefValidationError. `validate_draft(draft: DebriefDraft, *, sources: Mapping[str, SourceEntry], brief: MeetingBrief | None) -> None` juga memeriksa finding IDs/status.
- `generate_debrief(model: BaseChatModel, snapshot: RecordSnapshot, *, brief: MeetingBrief | None, stop: asyncio.Event, fallback: BaseChatModel | None = None, journal: Journal | None = None) -> DebriefDraft` async; client utama/fallback dibuat caller dari factory existing, tidak membuka browser/memory.
- `merge_drafts(drafts: Sequence[DebriefDraft]) -> DebriefDraft`; `render_debrief(report: DebriefReport) -> str`; `write_debrief(record_path: Path, report: DebriefReport) -> None` membuat debrief.json/md atomik, caller memegang record lock.
- `debrief_report_status(record_path: Path) -> Literal["available", "stale", "missing", "invalid"]` di debrief.py memeriksa artefak/schema/hash tanpa provider. Laporan lama setelah attempt gagal tetap available hanya bila hash masih cocok.
- `debrief_meeting(meeting_id: str, persona_path: Path, owner_id: str | None, twin_id: str | None, force: bool, settings: Settings) -> None` async handler; parser memakai dest persona_path/meeting_id/force yang sama.

## Task 0 — Entry gate dan probe structured output

- [ ] Catat kandidat A/HEAD/status, pertahankan perubahan pengguna, buat branch `feat/meeting-debrief` saat eksekusi. Baca seluruh desain dan modul record/ingestion_status/provenance existing.
- [ ] Probe fake dahulu, kemudian model produksi peran reply dengan structured output native + cancellation. Pilihan native `create_agent(..., response_format=ToolStrategy(DebriefDraft, handle_errors=False))`; baca SDK terpasang untuk tipe yang benar, tanpa JSON regex/fence parser.
- [ ] Bila provider utama/fallback tidak mendukung kontrak, laporkan gate provider gagal. Jangan otomatis mengganti provider/key, menambahkan role env baru, atau menerbitkan free-text sebagai laporan tervalidasi.

## Task 1 — Sumber, chunk, dan validator

**File:** debrief_sources.py, tesnya. **Input:** RecordEntry existing dan debrief_schema.py yang dibekukan sebelum delegasi. **Output:** snapshot/chunk/validator pada kontrak.

- [ ] Tulis tes merah hash byte konsisten, transcript ID duplikat bertentangan, nomor baris ucapan twin, unknown, quote palsu, partial JSON, entry terlalu panjang, chunk coverage/overlap, dan >20 chunk.

```python
from datetime import UTC, datetime

import pytest

from fakeyou.debrief_schema import DebriefValidationError, EvidenceRef
from fakeyou.debrief_sources import SourceEntry, validate_evidence


def test_quote_must_exist_in_the_referenced_entry():
    source = SourceEntry(
        id="transcript:t1",
        text="Please ask the owner about the new deadline.",
        kind="participant",
        participant_id="unknown",
        at=datetime(2026, 10, 3, tzinfo=UTC),
    )
    with pytest.raises(DebriefValidationError):
        validate_evidence(
            [EvidenceRef(entry_id=source.id, quote="The owner approved Monday.")],
            {source.id: source},
        )
```

- [ ] Jalankan `uv run --no-sync pytest tests/test_debrief_sources.py -q`; buktikan validasi palsu belum terjaga.
- [ ] Implementasikan parse melalui TypeAdapter RecordEntry, hash dari byte yang sama, ID canonical, source kind participant/twin. ToolResultEntry tidak menjadi sumber ucapan. Duplicate transcript ID dengan teks/metadata berbeda ditolak; duplikat identik dideduplikasi jelas.
- [ ] Hitung coverage dari union ID sumber yang benar-benar masuk chunk, bukan jumlah item/chunk overlap. Setiap sumber harus masuk minimal sekali. Validator memakai substring persis, tanpa “normalisasi” yang menerima quote karangan.

```python
def validate_evidence(evidence, sources):
    if not evidence:
        raise DebriefValidationError("missing evidence")
    for reference in evidence:
        source = sources.get(reference.entry_id)
        if source is None or not reference.quote or reference.quote not in source.text:
            raise DebriefValidationError("invalid evidence reference")
```

Implementasi produksi memakai signature typed dan limit kontrak. debrief_sources.py mengimpor schema/error dari debrief_schema.py; debrief.py memakai kedua modul. Tidak ada circular import atau redefinisi schema transcript.

- [ ] Tes setiap chunk mempunyai batas tepat, overlap tidak menyebabkan loop/infinite chunk, quote dari chunk lain ditolak pada draft chunk itu, dan semua sumber unknown tetap unknown di evidence metadata.
- [ ] Jalankan tes task, lint/typing, review; commit lokal `feat: index debrief evidence from immutable records`.

## Task 2 — Draft native, merge dan render

**File:** debrief.py, test_debrief.py; schema/test_debrief_schema.py dibekukan sebelum Task 1/2 paralel. **Input:** snapshot/chunk/validator. **Output:** draft/report/renderer/publikasi pada kontrak.

- [ ] Tulis tes merah native ToolStrategy dengan ToolCallingFakeModel, no tool execution, invalid schema, absent structured_response, satu chunk gagal, stop saat model menunggu, konflik, dan artefak tidak menimpa sukses lama.
- [ ] Jalankan `uv run --no-sync pytest tests/test_debrief.py -q`; pastikan failure membuktikan kontrak output, bukan kegagalan setup.
- [ ] Implementasikan model schema dan native extraction di satu call per chunk; recursion_limit dibatasi, handle_errors false agar repair tidak menjadi loop call tanpa batas. Caller memakai timeout/retry/fallback eksplisit existing; jangan fallback ke teks mentah.

```python
from langchain.agents import create_agent
from langchain.agents.middleware import ModelFallbackMiddleware
from langchain.agents.structured_output import ToolStrategy


async def _extract_chunk(model, prompt, messages, fallback):
    middleware = [ModelFallbackMiddleware(fallback)] if fallback is not None else []
    agent = create_agent(
        model=model,
        tools=[],
        system_prompt=prompt,
        middleware=middleware,
        response_format=ToolStrategy(DebriefDraft, handle_errors=False),
    )
    result = await agent.ainvoke({"messages": messages}, config={"recursion_limit": 4})
    return result["structured_response"]
```

Snippet ialah helper privat generate_debrief; produksi memakai typing lengkap dan import DebriefDraft dari debrief_schema. `model`/`fallback` ialah client dari factory existing, `messages` berisi chunk/snapshot context, `prompt` memuat aturan desain. Parser tidak mengambil messages[-1] sebagai laporan. Panggilan dibungkus task cancellation mengikuti stop; SIGTERM tidak menunggu semua chunk. Fallback tidak boleh mengubah CancelledError menjadi retry.

- [ ] Prompt menjaga transcript sebagai klaim, unknown/overlap, budget/deadline needs_owner, relatif waktu, ucapan twin bukan keputusan owner, dan instruksi peserta bukan perintah extractor. Quote valid tidak menjadi jaminan makna: jangan memberi flag verified kepada ringkasan model.
- [ ] Validasi masing-masing chunk terhadap sumber chunk sebelum merge. Dedupe hanya item identik; reject >100 item gabungan. Finding tiap ID: uncertain dengan bukti konflik mengungguli addressed; addressed berbukti mengungguli open/uncertain tanpa bukti. Pertahankan evidence gabungan; item keputusan bertentangan tetap terpisah. Setelah merge, setiap ID brief yang hilang diberi uncertain/evidence kosong. Tanpa brief findings wajib kosong.
- [ ] Renderer memakai JSON canonical, menampilkan unknown, PIC/deadline sebagai klaim, kebutuhan keputusan owner, item open/uncertain, hash dan status unreviewed. Quote dirender aman sebagai teks; jangan membuat link/scripting dari markup peserta.
- [ ] Publication fsync/replace dan JSON terakhir; rekonstruksi Markdown dari JSON dapat dilakukan tanpa provider. Gagal/cancel tidak menghapus laporan lama. Record hash/context hash diperiksa kembali sebelum publikasi.
- [ ] Jalankan tes validator/draft/render/atomic failure, lint/typing, review; commit lokal `feat: generate source-backed meeting debriefs`.

## Task 3 — Operator CLI, lock dan cache

**File:** CLI debrief/main/memory/diagnostics, ingestion_status, tes CLI/report/status. **Input:** A MeetingContext dan B generate/write. **Output:** debrief command dan report state available/stale/missing.

- [ ] Tulis tes merah scope salah sebelum provider, writer/ingest/debrief lock aktif, cache invalid, force gagal menjaga laporan lama, record berubah, missing context vs corrupt context, dan keep-record.
- [ ] Jalankan tes CLI/report/status targeted. Tambahkan parser debrief dengan --meeting/--persona wajib, owner-id/twin-id opsional, --force default false. Tambah purpose debrief pada lock existing dan validasi owner lock; tidak membuat mekanisme lock lain.
- [ ] Validasi operator persona/IDs, ambil record lock, panggil load_record_snapshot/load_context_snapshot, validasi ID/hash. Tanpa context hanya data record, tidak memakai goals persona saat ini seolah brief lama. Corrupt context menolak command. Hash persona saat ini tidak mengganti brief historis; scope yang cocok cukup untuk membaca sumber, sedangkan join tetap mensyaratkan persona/context persis.
- [ ] Buat model dari create_chat_model dan fallback existing hanya bila configured/probe lulus; tidak membuka memory/STT/TTS/browser. Daur hidup stop handler satu lapis milik command; cancellation/invalid source/provider failure exit nonzero.
- [ ] Cache dipakai hanya source/context/schema cocok; --force membuat attempt baru. Print JSON metadata/path/status, bukan secret. Journal `debrief_started`, `debrief_finished`/`debrief_failed`, source agent atau cli, hanya IDs/counts/hash/duration/error class.
- [ ] Report menambahkan status debrief terpisah dari session/ingestion; stale terlihat jika hash berubah. Forget menghapus artefak ketika record dihapus, keep-record mempertahankannya, lock aktif menolak operasi.
- [ ] Tes join stop tetap tidak memanggil extractor; native cancellation meninggalkan lock dapat dipakai run selanjutnya; tidak ada pesan terkirim atau fakta memori baru.
- [ ] Jalankan gate penuh/build/container, verifikasi source image dan review; commit lokal `feat: expose operator debrief command`.

## Task 4 — Korpus, runbook dan acceptance B

**File:** debrief fixture/eval, runbook, README, laporan bertanggal. **Input:** command produksi dan source A. **Output:** verdict semantik/provenance yang dapat diperiksa.

- [ ] Buat delapan record sintetis persis kategori desain beserta expected required items/quotes dan expected failure untuk corrupt source. Fixture source terpotong tidak mempunyai TwinSpeechEntry completed; jangan mengarang teks bagian yang tidak terekam.
- [ ] Gate review menolak review hilang/duplikat, ref palsu, klaim owner approval/aksi selesai/nama unknown yang ditebak, dan required-item coverage <90%. Tidak hanya menghitung JSON valid atau substring output.
- [ ] Jalankan produksi terhadap fixture valid dengan model tersedia, simpan source/draft/report/hash dan review seluruh item independen. Corrupt/oversize/lock cases harus gagal sebelum provider. Latency/biaya ekstraksi dilaporkan terpisah dari latency meeting.
- [ ] Runbook memuat command parser aktual, cache/force, record tanpa brief, unknown, kegagalan model, limits chunk, serta cara owner memeriksa kutipan. Tidak menawarkan kirim notifikasi/approval yang belum ada.
- [ ] Buat debrief dua record live A, review owner terhadap keputusan/usulan/action item/open question/owner_decision dan status tujuan. Semua item mempunyai bukti; tidak ada klaim kontak owner otomatis. Jika source A belum tersedia, gate live B tetap terbuka.
- [ ] Ulangi command pada sumber sama dan force dengan model gagal; tidak menggandakan fakta, tidak menghapus artefak sukses. Jalankan gate dokumen/statis/non-live/build/container yang relevan setelah perubahan terakhir.
- [ ] Tulis laporan verdict terpisah, update status proyek dengan batas speaker/performa tetap terbuka, review dan commit lokal. Tidak ada push/merge otomatis atau klaim siap menggantikan owner tanpa pengawasan.

## Swarm dan gate

Bekukan schema/error pada satu pemilik sebelum source-validator dan draft-renderer paralel; agent eval menyiapkan fixture/review gate. Integrator mengedit CLI/lock/report/fake bersama. Reviewer spec/kode bukan penulis; orkestrator menjalankan tes sendiri. Ledger `.runtime/meeting-delegation/swarm-status.json` menyimpan task B dan commit baseline A persis.

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
