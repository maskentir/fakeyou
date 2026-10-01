# Rencana pengembangan berikutnya: jawaban cepat dengan bukti kualitas

> **Untuk pelaksana:** Gunakan skill `executing-plans` untuk mengerjakan task secara berurutan. Langkah memakai checkbox untuk pelacakan. Delegasi dilakukan bila diminta pengguna.

**Tujuan:** Mengukur penyebab jeda jawaban, memilih konfigurasi jawaban yang lulus kualitas dan latensi, lalu membuktikannya di Google Meet.

**Arsitektur:** Pertahankan loop LangGraph dan TTS streaming yang sudah berjalan. Lengkapi metadata waktu pada journal, hubungkan request model serta recall ke transkrip sumber, dan gunakan laporan yang sama untuk membandingkan konfigurasi. Optimasi pertama memakai skema peran yang sudah ada; perubahan alur streaming teks membutuhkan desain tersendiri bila konfigurasi belum cukup.

**Stack teknologi:** Python `>=3.12,<3.13`, LangChain 1.4.2, LangGraph 1.2.12, Pipecat 1.12.0, Graphiti 0.30.2, pytest, DeepEval, Ruff, dan mypy.

**Spesifikasi:** [Acceptance 1 Oktober](../reports/2026-10-01-refactor-acceptance.md), [rencana jangka panjang](long-term.md), serta kontrak dan kriteria dalam dokumen ini.

**Status:** Task 1–3 selesai dan terintegrasi pada 2 Oktober 2026 (gate lokal hijau); Task 4 selesai sebagai laporan negatif; Task 5 menunggu build container dan acceptance live bersama host.

## Dasar keputusan

Refactor utama sudah digabung ke `master` pada `6926e9b`. Cleanup sesudahnya berada di `feat/legacy-config-cleanup`: `8a06fee`, `cdf0038`, dan `ff60109`; ketiganya masih lokal ketika rencana ini disusun. Baseline terakhir pada `ff60109`: 1.403 tes non-live dan non-container lulus, termasuk browser lokal; Ruff, format, dan mypy bersih. Angka ini bukti regresi lokal, bukan pengukuran performa baru.

Acceptance `acc3-1001-s2` membuktikan startup, recall lintas sesi, konteks kata ganti, interupsi, dan stop tanpa suara terlambat. Host mengonfirmasi suara utuh. Gap yang masih nyata:

| Bukti | Hasil terakhir | Implikasi |
| --- | --- | --- |
| Akhir ucapan → PCM pertama, delapan giliran | Median 5,727 detik; p95 16,895 detik; maksimum 18,080 detik | Gap terbesar pada pengalaman percakapan |
| Lima pertanyaan utama | 4,557–14,695 detik | Jawaban sederhana maupun recall masih lambat |
| Awal interupsi → playback berhenti | Median 1,618 detik; maksimum 1,833 detik | Pertahankan sebagai pembanding selama optimasi |
| Eval replay MiniMax sebelum acceptance | Belum memenuhi seluruh ambang kualitas dan latensi | Fungsi live yang berhasil belum menutup gate eval |
| Identitas speaker | Satu host; belum ada bukti audio per participant | Milestone tersendiri setelah jawaban responsif |
| Memori | Recall pulih; tanggal provenance owner note bergeser, prefix atribusi bercampur, satu duplikat | Perbaikan berikutnya pada sumber dan ekstraksi |

Jalur sekarang menunggu `LangGraphResponder.respond()` selesai sebelum `session.speak()`. TTS sudah streaming PCM; mengaktifkan ulang flag TTS streaming tidak menyelesaikan jeda model. Belum ada bukti bahwa satu tahap tertentu menjelaskan seluruh p95, sehingga pengukuran diprioritaskan.

## Pilihan dan urutan

| Pilihan | Nilai | Batas |
| --- | --- | --- |
| **Ukur tahap, lalu optimalkan konfigurasi jawaban — disarankan** | Menutup gap yang sudah terukur dengan perubahan kecil dan bisa dibandingkan | Provider yang tersedia mungkin belum memenuhi target |
| Audio dan identitas per participant | Memperbaiki atribusi dalam meeting multipihak | Memerlukan bukti capture dan korelasi baru; tidak memperbaiki latency saat ini |
| Provenance dan koreksi memori | Menjaga waktu sumber asli dan mengurangi fakta ganda | Tidak langsung mempercepat jawaban yang tidak memakai recall |

Dokumen ini mendetailkan pilihan pertama. Speaker dan memori tetap berada dalam antrean pengembangan di bagian akhir, dengan acceptance sendiri.

## Kontrak dan batasan global

- Default serta pilihan provider tetap berasal dari `Settings`; tidak ada alias env lama, peminjaman key lintas peran, atau pemilihan provider dari nama model/URL.
- Python memakai bahasa Inggris tanpa komentar dan docstring; dokumen memakai Bahasa Indonesia serta folder `docs/plans`, `docs/reports`, `docs/reference`, atau `docs/runbooks`.
- Journal menyimpan metadata: ID, waktu, jumlah, durasi, status. Teks transkrip, jawaban, prompt, hasil recall, dan secret tidak ditambahkan ke journal. Catatan meeting tetap menjadi sumber isi.
- Durasi journal memakai `_ms`; konfigurasi dan laporan memakai `_seconds`. Event mengikuti kosakata `taxonomy.py`.
- Capture produksi tetap PCM s16le mono 16 kHz; playback tetap 24 kHz. Mode percakapan tetap `ENDPOINTING`; probe diarisasi sebelumnya menemukan tambahan latency dan giliran yang hilang.
- Pertahankan batas kalimat, aturan kewenangan persona, isolasi memori, timeout, fallback eksplisit, pembatalan generasi usang, dan kepemilikan resource.
- Tidak ada perubahan atau re-ingest data `acc2`/`acc3`, `.env` pengguna, stash, atau backup. Live memakai profil dan namespace uji terpisah.
- Tidak mengubah dependency terkunci atau membangun sistem tracing baru. Gunakan `Journal`, callback LangChain, fixture, dan transport tes yang sudah ada.
- Ambang replay tetap: pass rate pemeriksaan wajib minimal 0,9; pertanyaan penutup maksimal 20%; p95 respons agent maksimal 4 detik. Kegagalan dicatat sebagai kegagalan, bukan diubah menjadi `xfail` baru atau ambang yang lebih longgar.
- Target live **yang diusulkan untuk milestone ini**: median akhir ucapan → PCM pertama maksimal 3 detik dan p95 maksimal 6 detik pada sedikitnya 20 jawaban dari dua sesi. Ini berbeda dari batas 4 detik pada replay agent; keduanya wajib dilaporkan.
- PCM pertama adalah bukti lokal kesiapan audio, belum timestamp suara yang sampai ke host. Pendengaran host tetap memverifikasi kelengkapan dan interupsi.

## Peta file dan tanggung jawab

| File | Tanggung jawab perubahan |
| --- | --- |
| `src/fakeyou/voice.py` | Mencatat PCM pertama ketika benar-benar tersedia, termasuk stream yang kemudian dibatalkan |
| `src/fakeyou/taxonomy.py` | Menambahkan kata kerja `emitted` untuk event baru |
| `src/fakeyou/conversation.py` | Membawa `transcript_id` ke metadata callback model dan tool |
| `src/fakeyou/report.py` | Menghubungkan request, memisahkan antrean dari waktu agent, dan menandai bukti yang tidak lengkap |
| `evals/test_replay_eval.py` | Menyimpan hasil replay per giliran dan manifest konfigurasi yang disamarkan |
| `tests/test_voice.py`, `tests/test_report.py`, `tests/test_conversation.py` | Regresi tanpa provider untuk event dan korelasi |
| `tests/test_conversation_benchmark.py` (baru) | Regresi tanpa provider untuk artefak serta denominasi benchmark; nama sesuai taksonomi tes |
| `tests/test_taxonomy.py`, `tests/test_documentation.py` | Menjaga kontrak metadata dan dokumentasi |
| `docs/runbooks/conversation-benchmark.md` | Cara mengulang pembandingan dan acceptance dengan fixture yang sama |
| `docs/reports/YYYY-MM-DD-conversation-performance.md` | Hasil, konfigurasi terpilih, kegagalan, dan batas bukti saat pelaksanaan |

Tidak perlu memecah modul atau menambah paket untuk pekerjaan ini. Nama file laporan menggunakan tanggal pelaksanaan yang sebenarnya.

## Task 1 — Bukti PCM pertama yang bertahan saat interupsi

**File:** Ubah `src/fakeyou/voice.py`, `src/fakeyou/taxonomy.py`, `src/fakeyou/report.py`; tes `tests/test_voice.py`, `tests/test_report.py`, dan `tests/test_taxonomy.py`.

**Antarmuka:** `OpenAISpeechSynthesizer.stream(text)` dan `MiniMaxSpeechSynthesizer.stream(text)` tetap menghasilkan `AsyncIterator[bytes]`. Event baru `synthesis_pcm_emitted` memakai `source="tts"`, `request_id` yang sama dengan `synthesis_started`, `first_chunk_ms`, serta jumlah byte; tanpa isi audio atau teks. `build_session_report(rows)` tetap menjadi pembaca laporan.

- [x] Tambahkan regresi yang membaca satu chunk, memeriksa event pertama sebelum stream selesai, lalu membatalkan/menutup stream. Contoh memakai fixture asli yang sudah tersedia:

```python
async def test_first_pcm_is_reported_before_synthesis_finishes(openai_client, journal):
    synthesizer = OpenAISpeechSynthesizer(
        client=openai_client(chunks=[b"\x00\x01", b"\x02\x03"]), journal=journal
    )
    stream = synthesizer.stream("Hello.")
    try:
        assert await anext(stream) == b"\x00\x01"
        [first] = journal.events("synthesis_pcm_emitted")
        assert first["first_chunk_ms"] >= 0
        assert first["bytes_out"] == 2
        assert journal.events("synthesis_finished") == []
    finally:
        await stream.aclose()
        await synthesizer.aclose()
```

- [x] Jalankan tes tersebut dan pastikan gagal karena event belum tercatat. Tambahkan kasus dua chunk, chunk kosong, gagal sebelum chunk pertama, dan pembatalan setelah chunk pertama; event pertama tepat sekali per request yang mempunyai PCM.
- [x] Di `_journaled_stream`, lewati chunk kosong, perbarui jumlah byte, lalu catat event saat chunk nonkosong pertama diterima. Nyatakan `source="tts"` secara eksplisit agar pemeriksaan AST tetap berlaku. Pertahankan `first_chunk_ms` pada event selesai untuk laporan lama.

```python
if not chunk:
    continue
timing.fields["bytes_out"] += len(chunk)
if timing.fields["first_chunk_ms"] is None:
    timing.fields["first_chunk_ms"] = timing.elapsed_ms
    journal.record(
        "synthesis_pcm_emitted",
        source="tts",
        request_id=timing.fields["request_id"],
        first_chunk_ms=timing.fields["first_chunk_ms"],
        bytes_out=timing.fields["bytes_out"],
    )
yield chunk
```

Cuplikan menggantikan isi loop pembaca chunk; jangan menyisakan penambahan byte atau penetapan waktu lama setelahnya.

- [x] Pada laporan, simpan hubungan request sintesis dengan giliran saat request dimulai. Event akhir atau PCM yang datang terlambat menggunakan hubungan itu, bukan variabel jawaban terbaru. Jika hubungan ambigu atau awal request hilang karena rotasi, tandai bukti tidak lengkap.
- [x] Untuk journal lama, rekonstruksi PCM pertama hanya bila `first_chunk_ms` tersedia dan sah. `synthesis_finished` tanpa bukti chunk tidak boleh dipakai sebagai waktu PCM pertama. Giliran tanpa audio tetap memiliki latency `None`.
- [x] Timestamp hilang, selisih negatif, atau durasi nonfinite tidak menjadi sampel latency. Tambahkan regresi untuk input tersebut dan laporkan jumlah bukti yang dikeluarkan beserta alasannya.
- [x] Jalankan `uv run pytest tests/test_voice.py tests/test_report.py tests/test_taxonomy.py -q`. Pastikan interupsi, retry TTS, dan cleanup iterator tetap lulus; commit sebagai `feat: record first PCM before synthesis completes`.

**Selesai ketika:** Suara yang sempat menghasilkan PCM lalu disela tetap terukur, sementara kegagalan tanpa audio tidak menghasilkan angka latency palsu.

## Task 2 — Pisahkan antrean, request model, dan recall

**File:** Ubah `src/fakeyou/conversation.py`, `src/fakeyou/report.py`; tes `tests/test_conversation.py`, `tests/test_report.py`, dan `tests/test_taxonomy.py`.

**Antarmuka:** `ConversationResponder.respond(events, on_tool_start)` tidak berubah. `_ToolActivity` menerima keyword opsional `transcript_id: str | None = None`; `LangGraphResponder` mengisinya dari event terakhir yang benar-benar dikirim. ID request model dari LangChain tetap berbeda dari ID request agent.

- [x] Tambahkan tes callback melalui agent/fake yang sudah dipakai suite: dua giliran, tool recall, request model gagal, dan pembatalan. Periksa metadata hasil journal serta tidak adanya isi percakapan; jangan menguji hanya jumlah pemanggilan mock.
- [x] Jalankan tes baru sebelum perubahan. Bukti merah yang diharapkan: metadata request/tool belum memuat hubungan dengan transkripnya.
- [x] Teruskan `transcript_id` pada `request_started`, `request_finished`, dan `request_failed` dari callback model. Tambahkan `tool_started` pada konteks `journal.timed` tool; simpan ID transkrip yang sama pada awal dan hasilnya.

```python
callbacks = [
    _ToolActivity(
        on_tool_start,
        self.journal,
        self.on_tool_result,
        transcript_id=events[-1].id if events else None,
    )
]
```

Constructor callback menyimpan keyword tersebut sebagai `self._transcript_id`. Pada awal model, tambahkan `transcript_id=self._transcript_id` ke `request_started`; pada akhir model:

```python
self._journal.request_finished(str(run_id), source="llm", transcript_id=self._transcript_id)
```

Jalur gagal meneruskan metadata yang sama ke `request_failed` beserta tipe error. Konteks tool memakai `started="tool_started"` dan `transcript_id=self._transcript_id`; signature callback library tidak diubah.

- [x] Tambahkan `agent_at` pada state internal laporan. Field tahap baru: `final_to_agent_seconds` dan `agent_to_reply_seconds`; pertahankan empat field latency lama.
- [x] Tambahkan field per giliran: `llm_count`, `recall_count`, `llm_duration_seconds`, `recall_duration_seconds`, serta `incomplete_request_count`. Durasi provider adalah jumlah durasi request selesai/gagal yang diketahui; nilainya bukan pembagian eksklusif waktu agent karena request dapat bertumpang tindih. Request tanpa akhir dihitung tidak lengkap, tidak diberi durasi nol.
- [x] Contoh regresi laporan, dengan `row()` dan `answered_turn()` dari `tests/test_report.py`:

```python
def test_report_correlates_model_time_to_its_transcript():
    rows = answered_turn(0, "t1", "The demo is on Friday.")
    rows += [
        row(0.5, "request_started", source="llm", request_id="llm-1", transcript_id="t1"),
        row(
            1.5,
            "request_finished",
            source="llm",
            request_id="llm-1",
            transcript_id="t1",
            duration_ms=1000,
        ),
    ]
    report = build_session_report(sorted(rows, key=lambda entry: entry["at"]))
    turn = report["turns"][0]
    assert turn["llm_count"] == 1
    assert turn["llm_duration_seconds"] == 1.0
    assert turn["final_to_agent_seconds"] == 0.0
    assert turn["agent_to_reply_seconds"] == 1.2
```

- [x] Tambahkan kasus tool paralel, hasil lama setelah agent baru mulai, ID transkrip tidak dikenal, journal terpotong, serta giliran gagal/dibatalkan. Tidak ada metadata yang dialihkan ke giliran terakhir hanya karena urutan event.
- [x] Jalankan `uv run pytest tests/test_conversation.py tests/test_report.py tests/test_journal.py tests/test_taxonomy.py -q`; commit sebagai `feat: report correlated model and recall timings`.

**Selesai ketika:** Laporan dapat menunjukkan apakah jeda berasal dari antrean, pekerjaan agent/model, recall, atau sintesis, dengan jumlah bukti lengkap dan tidak lengkap.

## Task 3 — Benchmark kualitas dan latency yang dapat diulang

**File:** Ubah `evals/test_replay_eval.py`; buat `tests/test_conversation_benchmark.py` dan `docs/runbooks/conversation-benchmark.md`.

**Antarmuka:** Pertahankan `ReplayScore`, `score_turn`, dan `replay(model_factory, fixture, runs)`. Tambahkan `turns: list[dict[str, Any]]` pada `ReplayScore` tanpa mengubah kontrak skor; `summary()` tetap menghasilkan rate dan p50/p95. Tambahkan `write_replay_artifacts(directory: Path, *, score: ReplayScore, settings: Settings, fixture_path: Path, source_commit: str) -> None` pada modul eval; tes mengimpor fungsi dari modul tersebut. Tidak membuat command CLI benchmark baru.

- [x] Tambahkan regresi offline untuk manifest secret, hasil per giliran, dan denominasi skor; gunakan `ToolCallingFakeModel`, fixture replay yang sudah di-commit, dan clock terkontrol. Pastikan jumlah run/giliran serta latency tidak berubah saat satu jawaban kosong atau recall gagal.
- [x] Jalankan `uv run pytest tests/test_conversation_benchmark.py evals/test_replay_eval.py -m 'not live' -q` untuk membuktikan tes baru gagal sebelum perubahan.
- [x] Simpan `summary.json` dari tes live pada `tmp_path`: commit source, hash fixture, jumlah run, provider/model utama dan fallback, timeout/retry, bahasa, serta hasil ambang. Serialisasi key memakai masking pydantic; jangan menyimpan nilai secret atau `.env` mentah.
- [x] Simpan `turns.json`: ID sesi dan giliran sintetis, latency, pemeriksaan kualitas, dan apakah recall dipanggil. Pertahankan gate kualitas yang ada. Recall fixture harus sama antar kandidat; jangan mengganti fakta agar kandidat terlihat lebih baik.
- [x] Regresi artefak memakai interface yang dinyatakan di atas. Isi `fixture_path` dengan salinan fixture sintetis yang di-commit dan gunakan commit uji tetap; pastikan tiap pemeriksaan juga mencatat jumlah sampel yang memang memenuhi syarat.

```python
def test_replay_artifacts_mask_role_credentials(tmp_path, settings):
    score = ReplayScore()
    score.add("answered", True)
    score.latencies.append(1.0)
    score.turns.append(
        {
            "meeting_id": "fixture",
            "run_index": 0,
            "turn_index": 0,
            "latency_seconds": 1.0,
            "checks": {"answered": True},
        }
    )
    fixture = tmp_path / "fixture.json"
    fixture.write_text('{"sessions": []}')
    write_replay_artifacts(
        tmp_path,
        score=score,
        settings=settings,
        fixture_path=fixture,
        source_commit="test-commit",
    )
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["source_commit"] == "test-commit"
    assert summary["reply"]["api_key"] == "**********"
    assert settings.reply.api_key.get_secret_value() not in (tmp_path / "summary.json").read_text()
    assert (tmp_path / "turns.json").exists()
```

- [x] Bangun manifest memakai `settings.model_dump(mode="json")` yang sudah menyamarkan secret, lalu pilih bagian peran dan budget yang diperlukan. Hash fixture memakai SHA-256 atas byte file; commit menjadi input eksplisit agar tes tidak membaca Git atau clock nyata. Berkas disimpan sebelum pemeriksaan ambang dilakukan, sehingga hasil gagal tetap dapat dibandingkan.
- [x] Model dump menyamarkan `SecretStr`, tetapi URL dan `extra_body` dapat membawa string biasa. Hapus userinfo/query/fragment URL dari manifest dan samarkan field credential di `extra_body` secara rekursif; tambahkan tes dengan marker sintetis pada kedua tempat itu. Jangan menganggap serialisasi pydantic menyamarkan seluruh string arbitrer.
- [x] Gunakan `--basetemp` berbeda agar artefak baseline dan kandidat tidak tertimpa. Runbook mencantumkan command yang benar-benar dapat dijalankan:

```sh
uv run --env-file .env pytest evals/test_replay_eval.py -m live -v -s \
  --basetemp=.runtime/benchmarks/reply-baseline
```

- [x] Ulangi dengan satu konfigurasi kandidat yang tersedia, direktori `reply-candidate`, fixture identik, tiga run atau 24 giliran, dan urutan baseline/kandidat bergantian. Perubahan hanya pada field `FAKEYOU_REPLY__*`/`FAKEYOU_REPLY_FALLBACK__*` yang sesuai variannya. Ketersediaan key dan kredit diperiksa ketika eksekusi; jangan merencanakan OpenAI atau Anthropic sebagai prasyarat.
- [x] Catat kandidat yang dilewati karena key/kredit tidak tersedia sebagai belum diuji. Judge persona membutuhkan `EVAL_JUDGE_API_KEY` sendiri; replay deterministik tetap dapat berjalan tanpa judge. Status skip atau judge gagal tidak dianggap kelulusan kualitas.
- [x] Jalankan tes offline, pemeriksaan dokumentasi, dan gate statis; commit sebagai `test: persist comparable conversation benchmark results`.

**Selesai ketika:** Baseline dan setiap kandidat memiliki artefak yang menjelaskan apa yang dibandingkan, sampel yang dipakai, serta gate yang lulus atau gagal.

## Task 4 — Terapkan optimasi dari bukti

**File:** Hasil pada laporan tanggal pelaksanaan; ubah `.env.example`, README, dan schema provider hanya bila keputusan memang mengubah default publik. Pengaturan deployment pengguna dicatat terpisah dan diterapkan secara sengaja saat eksekusi.

- [x] Urutkan giliran lambat berdasarkan tahap dan jumlah request. Pisahkan giliran tanpa recall, dengan recall, timeout/fallback, dan interupsi; jangan menutup hasil buruk dengan agregat campuran.
- [x] Bila waktu model mendominasi, bandingkan konfigurasi model/parameter provider yang tersedia dengan Task 3. Pilih hanya konfigurasi yang melewati seluruh gate, termasuk tool call, provenance, pertanyaan penutup, dan kewenangan persona.
- [x] Bila recall mendominasi, ulangi query yang sama pada namespace uji dan pisahkan waktu embedding, pencarian, serta reranker menggunakan batas client yang sudah tersedia. Pertahankan hasil relevansi; pengurangan jumlah fakta atau mematikan recall tidak dianggap perbaikan.
- [ ] Bila TTS mendominasi, bandingkan waktu chunk pertama pada kalimat fixture yang sama, voice dan format PCM yang sama. Provider TTS kandidat harus memperdengarkan kalimat utuh dan tetap berhenti saat disela.
- [ ] Bila antrean mendominasi, tambahkan regresi deterministik pada kasus yang terbukti, lalu perbaiki kasus itu. Dilarang berbicara dari jawaban generasi usang atau memotong guard stop agar angka lebih kecil.
- [x] Rekomendasi yang hanya mengubah konfigurasi tidak memerlukan refactor source. Bila tidak ada kandidat yang memenuhi gate, pertahankan konfigurasi lama dan laporkan milestone performa belum lulus. Buat desain terpisah untuk streaming teks per kalimat hanya setelah bukti menunjukkan menunggu respons lengkap sebagai penyebab dominan.

**Selesai ketika:** Ada kandidat yang layak untuk acceptance, atau laporan negatif yang menjelaskan batas provider/alur serta langkah desain berikutnya. Laporan negatif tidak menutup acceptance performa.

## Task 5 — Acceptance live dan penutupan milestone

**File:** Buat laporan performa bertanggal; perbarui [status proyek](../reference/project-status.md) dan bagian status dalam rencana ini berdasarkan hasil sebenarnya.

- [x] Jalankan gate lokal: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, dan `uv run pytest -m 'not live and not container' -q`.
- [ ] Bangun image dengan `docker compose --profile audio build meeting-worker`. Cocokkan hash source image dengan commit yang diuji. Jalankan tes container sesuai marker proyek; jangan menukar hasil image lama sebagai bukti commit baru.
- [ ] Setelah host siap, buat dua sesi baru memakai profil/namespace uji dan pertanyaan tetap: fakta sederhana, owner note, recall meeting sebelumnya, pertanyaan lanjutan dengan kata ganti, pertanyaan yang tidak diketahui, serta permintaan di luar kewenangan.
- [ ] Kumpulkan sedikitnya 20 giliran jawaban. Pisahkan giliran interupsi/stop dari distribusi jawaban utuh, tetapi tetap laporkan jumlahnya; jangan menghapus jawaban lambat dari sampel.
- [ ] Uji barge-in sedikitnya tiga kali dan stop ketika model bekerja maupun ketika audio berjalan. Tidak ada PCM/playback generasi usang setelah stop. Target pembanding interupsi: median maksimal 1,618 detik dan maksimum maksimal 1,833 detik pada skenario yang setara.
- [ ] Jalankan `fakeyou report --meeting ID` pada journal masing-masing sesi. Simpan laporan, commit, konfigurasi yang disamarkan, jumlah sampel/missing evidence, serta konfirmasi host tentang suara utuh.
- [ ] Nyatakan hasil fungsi, kualitas, dan performa secara terpisah. Milestone performa lulus hanya bila gate replay dan target live keduanya terpenuhi. Sampel yang kurang atau event hilang menghasilkan bukti belum cukup.
- [ ] Commit laporan dan pembaruan status. Push/merge mengikuti instruksi pengguna pada saat pelaksanaan.

## Milestone sesudah jawaban responsif

| Urutan | Hasil yang akan dibangun | Acceptance sebelum diperluas |
| --- | --- | --- |
| 2. Atribusi participant | Capture/korelasi speaker yang mempertahankan `unknown` ketika bukti ambigu | Dua pembicara, overlap, penutur kembali, dan reconnect; tidak ada nama tebakan atau penambahan latency yang melampaui budget |
| 3. Provenance dan koreksi memori | Waktu sumber owner note terpisah dari waktu input ulang, atribusi konsisten, koreksi fakta yang dapat diuji | Recall sebelum/sesudah koreksi dan re-ingest tetap benar, provenance asli terjaga, namespace lain tidak berubah |
| 4. Tindakan Meet | Raise/lower hand dan chat dengan observasi hasil serta kewenangan persona | Aksi berhasil teramati, ditolak bila kewenangan kurang, dan tidak diulang saat retry/restart |
| 5. Operasi worker | Start/stop/status, job ingestion terpisah, persistence serta isolasi untuk beberapa sesi | Stop jarak jauh, crash/restart, dua meeting tanpa campur audio/data, dan ingestion dapat dilanjutkan |
| 6. Konteks visual | Screenshare/slide dengan timestamp yang terhubung ke pembicaraan | Jawaban merujuk visual yang benar dan meminta klarifikasi bila konteks ambigu |

Setiap milestone mendapat desain dan rencana sendiri sebelum implementasi. Avatar, kamera virtual, Kubernetes, dashboard, dan kalender menunggu kebutuhan yang konkret serta bukti operasi worker.
