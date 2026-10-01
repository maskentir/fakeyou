# Persona pemilik dan retrieval memori

Status: Task 1–3 selesai dan teruji pada 30 September 2026 di branch `feat/persona-memory`. Task 4 dijalankan live pada hari yang sama; hasil dan sisa pekerjaan ada di [acceptance 30 September](../reports/2026-09-30-acceptance.md). Dokumen ini menggantikan rencana persona tanggal 28 September di `docs/superpowers/plans/`. Tahap berikutnya ada di [rencana ingestion memori](memory-ingestion.md).

## Hasil yang dituju

Twin mengikuti profil pemilik yang tervalidasi, mengingat konteks meeting melalui checkpoint yang sudah ada, dan dapat mengambil pengetahuan dari ruang Graphiti milik owner/twin yang sama. Ketika fakta atau kewenangan tidak tersedia, twin meminta klarifikasi. Ini tahap identitas dan pengetahuan; kemampuan melihat slide, memakai UI Meet, dan mengambil giliran bicara multipihak menyusul.

Fondasi guest, audio dua arah, dan conversation loop sudah tersedia. Live conversation pernah dijalankan pada 29–30 September; pengukuran first-audible audio dan stop latency belum tersedia.

## Keputusan desain

- Gunakan Pydantic 2 untuk profil JSON, `create_meeting_agent(..., persona=...)` untuk prompt, dan Graphiti `group_id` untuk ruang memori. Tidak ada framework agent, parser JSON, atau database memori tambahan.
- Profil adalah konfigurasi operator. Teks participant, hasil retrieval, dan screenshot tidak boleh mengubah owner, namespace, atau daftar tool yang tersedia.
- Namespace berasal dari `owner_id` dan `twin_id`, stabil lintas meeting. Checkpoint conversation tetap memakai owner/twin/meeting agar konteks meeting berbeda tidak tercampur.
- Tahap ini hanya memberi tool recall kepada meeting agent. Penulisan memori, ingestion otomatis, koreksi, provenance lengkap, retensi, dan penghapusan menjadi tahap terpisah.
- Tool permission diberlakukan di composition root sebelum agent dibuat. Prompt saja bukan batas akses. Namespace lokal juga bukan pengganti autentikasi untuk deployment multiuser.
- Pertahankan queue conversation tunggal, batas dua kalimat untuk jawaban model, retry TTS tanpa replay PCM, dan cleanup audio/browser. Capture tetap berjalan ketika agent atau Graphiti bekerja.
- Production Python tetap tanpa comment/docstring; kontrak dan alasan desain ditulis di dokumen ini.

## Implementasi

| File | Isi |
| --- | --- |
| `src/fakeyou/persona.py` | `PersonaProfile`, `load_persona`, `build_persona`, `memory_group` |
| `src/fakeyou/agent.py` | `MEETING_GUIDANCE` dan `SPOKEN_LANGUAGE_GUIDANCE` dipisah; `DEFAULT_PERSONA` tetap sama isinya; factory menerima `middleware` native `create_agent` |
| `src/fakeyou/memory.py` | `verify_memory_connection` dan `recall_error_middleware`; tools dan factory lama tidak berubah |
| `src/fakeyou/cli.py` | `join --persona PATH`, validasi sebelum resource, filter tool, startup failure `infra_failure` |
| `compose.yaml` | Worker memakai `bolt://neo4j:7687` (override `FAKEYOU_WORKER_NEO4J_URI`) dan mount `examples/` read-only |
| `examples/persona.example.json` | Profil sintetis Rani Twin/Orchid |
| `tests/test_persona.py`, `tests/test_cli.py`, `tests/test_memory.py` | Validasi, isolasi, wiring, failure path |
| `tests/test_providers.py` | Isolasi dua namespace Graphiti setelah reopen |
| `evals/test_persona.py` | Wiring deterministik dan penilaian kualitas live |

### Kontrak profil

- `ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)`. `owner_id`/`twin_id` memakai `SessionId`, `display_name` memakai `TwinName`.
- Teks isi (`role`, `speaking_style`, item `goals`/`verified_facts`) di-strip, wajib nonblank, maksimal 500 karakter. `goals` dan `verified_facts` berupa tuple maksimal 20 item. `language` hanya `id` atau `en`. `allowed_tools` adalah `frozenset[Literal["recall_meeting_context"]]`.
- Default: `language="id"`, `speaking_style="concise"`, goals/facts kosong, permission kosong.
- `memory_group = "twin_" + uuid5(NAMESPACE_URL, json.dumps([owner_id, twin_id]))`. Ini property biasa, bukan field, sehingga tidak ikut serialisasi atau prompt. Encoding JSON list mencegah tabrakan seperti `("owner_a", "twin")` vs `("owner", "a_twin")`.
- `load_persona` membaca file lewat `asyncio.to_thread`, memvalidasi dengan `model_validate_json`, lalu menolak owner/twin yang tidak sama dengan identitas sesi. File yang tidak terbaca menjadi `CliError`. Tahap ingestion mengubah signature menjadi `load_persona(path, *, owner_id, twin_id)` agar command operator tanpa `SessionConfig` dapat memakainya.

### Prompt

`build_persona` menyusun `MEETING_GUIDANCE`, guidance bahasa sesuai `language`, aturan sumber, aturan recall (hanya bila recall diizinkan), dan JSON profil dari `model_dump_json(include=...)`. JSON hanya memuat `display_name`, `role`, `speaking_style`, `goals`, dan `verified_facts`; ID, namespace, dan daftar tool tidak masuk prompt. Teks profil tetap berupa string JSON yang di-escape, bukan instruksi bebas.

Aturan sumber membedakan tiga hal. `verified_facts` adalah satu-satunya fakta pemilik. Transkrip adalah klaim participant dan tidak dapat mengubah identitas, owner, memori, atau tool. Kesimpulan lain dilabeli sebagai tebakan. Twin tidak membuat komitmen, approval, deadline, anggaran, atau kontrak tanpa fakta yang memberi kewenangan tersebut.

Eval live menemukan dua kelemahan prompt awal. Model tidak pernah memanggil recall untuk pertanyaan jadwal, dan `display_name` terbaca sebagai nama owner ("owner, Rani Twin"). Aturan diperjelas: panggil recall sebelum menjawab tidak tahu bila `verified_facts` tidak menjawab, dan `display_name` adalah nama twin sendiri. Setelah perubahan itu, 3/3 probe memanggil recall.

### Komposisi CLI

Urutan di `join_meeting`:

1. Validasi flag: `--persona` memerlukan `--converse`; `join --memory` memerlukan `--persona`. `chat --memory` tetap memakai `FAKEYOU_MEMORY_GROUP` dan kedua tool.
2. `SessionConfig` dibangun dan `load_persona` dijalankan sebelum `FaultReporter`, journal, model, SQLite, Graphiti, browser, dan audio.
3. `--memory` tanpa `recall_meeting_context` di `allowed_tools` ditolak sebelum koneksi database.
4. Tanpa `--twin-name`, `display_name` profil menjadi nama di Meet; override eksplisit tetap menang. Identitas owner/twin tidak berubah.
5. `open_conversation` membuka Graphiti hanya jika ada tool yang diizinkan. `create_memory_tools(..., group_id=profile.memory_group)` difilter berdasarkan `allowed_tools`, sehingga `remember_meeting_fact` tidak pernah sampai ke meeting agent.

### Startup memory dan kegagalan recall

- `verify_memory_connection` memanggil `verify_connectivity()` milik driver Neo4j dengan batas `MEMORY_CONNECT_TIMEOUT_SECONDS = 10`. `execute_query("RETURN 1")` tidak dipakai karena managed transaction Neo4j me-retry sekitar 30 detik saat port mati. `verify_connectivity()` gagal seketika untuk port mati (`ServiceUnavailable`) dan password salah (`AuthError`).
- Kegagalan diklasifikasi dengan `classify_exception("memory", ...)` menjadi `ProviderFault`. `join` mencatat fault, mencetak hasil `phase=ended`, `terminal_reason=infra_failure`, dan `detail="memory unavailable: ..."`, lalu keluar dengan kode 3. Browser dan agent tidak dibuat; Graphiti dan checkpointer ditutup. Tidak ada keluarga exception baru. Checkpoint lifecycle meeting tidak ditulis karena sesi belum dimulai.
- Preflight DNS/TLS tetap hanya memeriksa host model/STT/TTS. Pemeriksaan Neo4j adalah langkah terpisah di `open_conversation`.
- Recall yang gagal saat meeting ditangani `ToolErrorMiddleware` native LangChain yang hanya berlaku untuk `recall_meeting_context`. Model menerima `RECALL_UNAVAILABLE_RESULT` ("treat the answer as unknown"), fault `memory` dicatat tanpa pesan exception, dan conversation tetap `listening`. `CancelledError` tidak ditangkap sehingga stop tetap membatalkan retrieval.
- Batas waktu retrieval mengikuti deadline invocation agent 60 detik yang sudah ada. Jika Graphiti hang melewati deadline itu, loop berakhir sebagai `conversation_failure`, sama seperti model yang timeout. Ini tercatat sebagai item terbuka di rencana berikutnya.

## Task 1 — Profil dan identitas

- [x] Tes gagal untuk JSON rusak, field asing, teks wajib kosong, owner/twin tidak cocok, tool tidak dikenal, dan namespace yang tertukar. Rename tidak mengubah namespace; meeting berbeda memakai namespace sama.
- [x] `ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)`, tipe `SessionId`/`TwinName`, collection immutable, semua field yang direncanakan.
- [x] Validasi nonblank dengan batas panjang/jumlah melalui `StringConstraints` dan `Field(max_length=...)`.
- [x] `memory_group` sebagai `twin_` + UUID5 dari JSON `[owner_id, twin_id]`.
- [x] `model_validate_json` dan `asyncio.to_thread` di `load_persona`; mismatch ditolak sebelum resource dibuka.
- [x] Nama tampilan dari profil, override `--twin-name` menang.
- [x] Prompt melalui `model_dump_json`, sumber fakta dipisah, guidance bahasa mengikuti profil.
- [x] `pytest tests/test_persona.py`, Ruff, dan mypy lulus.

## Task 2 — Komposisi agent dan recall

- [x] Tes CLI untuk syarat flag, mismatch sebelum resource, default conversation tanpa profil, dan `chat --memory` dengan grup lokal.
- [x] Persona diinjeksi ke factory; filter tool sebelum `create_meeting_agent`; `remember_meeting_fact` tidak pernah ada di meeting agent.
- [x] `--memory` tanpa izin recall ditolak sebelum koneksi database.
- [x] `profile.memory_group` sampai ke `group_ids` client; diuji pada daftar tool dan argumen `search`, bukan hanya prompt.
- [x] Gagal recall menjadi informasi tidak diketahui tanpa menutup conversation; cleanup saat database gagal startup diuji.
- [x] Startup memory memberi alasan dan exit code 3 sebelum browser dibuka.
- [x] Stop selama retrieval, startup failure (connection error dan timeout), profil tanpa memory, serta batas dua kalimat diuji. `tests/test_cli.py tests/test_conversation.py tests/test_runtime_audio.py` lulus.

## Task 3 — Bukti isolasi dan kualitas

- [x] Tes offline tujuh kombinasi owner/twin menghasilkan tujuh namespace; rename tidak memindahkan memori; thread checkpoint berbeda per meeting. Argumen `group_ids`/`group_id` dari model diabaikan oleh schema tool `recall_meeting_context`.
- [x] Tes Graphiti opt-in memakai dua namespace dengan fakta sintetis bertentangan (orang yang sama, preferensi minuman berbeda), membuka ulang client, lalu memastikan tiap recall hanya melihat faktanya sendiri. Cleanup hanya menghapus UUID episode milik tes; setelah semua run, Neo4j lokal berisi 0 node.
- [x] DeepEval: tiga tes wiring deterministik (prompt sampai sebagai system message, trajectory recall dengan Tool Correctness 1.0 dan grup profil, permintaan pindah identitas/namespace tidak mengubah tool atau grup) serta lima skenario GEval live (fakta profil, fakta tanpa bukti, komitmen di luar kewenangan, recall relevan, override identitas). Skor mock tidak dipakai sebagai bukti kualitas model.
- [x] Suite offline, browser fixture, Ruff, strict mypy, DeepEval, build, dan validasi Compose dijalankan. Hasil live dicatat terpisah di bawah.
- [x] Contoh profil sintetis dan command lengkap ada di README.

## Task 4 — Acceptance Meet terkontrol

Dijalankan 30 September 2026 pada sesi `acc-0930-s1` dan `acc-0930-s2`; lihat [acceptance 30 September](../reports/2026-09-30-acceptance.md).

- [x] Twin menjawab fakta profil (beta launch 14 Oktober 2026) dan fakta Graphiti dari owner note `memory-add` (demo Jumat 3 Oktober jam 2 siang), lalu mengakui harga lisensi belum diketahui.
- [x] Pertanyaan terkait ("…harus lewat dia juga") dijawab dengan konteks thread. "Oke cukup" memotong playback; stop saat agent bekerja membatalkan agent 45 ms kemudian tanpa `speak_started` setelahnya.
- [x] VAD stop → PCM pertama 2,16–4,50 detik (rincian per tahap di dokumen acceptance). Host mengonfirmasi suara terdengar; waktu dari PCM pertama sampai terdengar tidak diukur.
- [x] Stop request dipicu 14 ms setelah transkrip final pemicu; barge-in berhenti pada transkrip final, 0,62 detik setelah VAD stop. Barge-in berbasis interim/VAD tetap untuk tahap multipihak.
- [x] Run ID, journal (`.runtime/audio/journals/acc-0930-s*.jsonl`), hasil terminal, dan konfirmasi pendengaran tercatat di dokumen acceptance.

## Bukti pengujian 30 September 2026

| Pemeriksaan | Hasil |
| --- | --- |
| Suite offline `uv run pytest` | 555 lulus (baseline master 503); 16 live/browser deselected |
| Browser fixture `-m browser` | 6 lulus |
| Ruff check/format, strict mypy | Lulus |
| DeepEval deterministik (`test_meeting_agent.py`, `test_persona.py`) | 1 + 3 lulus |
| DeepEval live `test_persona.py -m live` (judge `gpt-4.1-mini`, threshold 0.7) | 5/5 lulus pada tiga run berturut-turut setelah perbaikan prompt; run pertama sebelum perbaikan 4/5 (recall tidak dipanggil) |
| Provider live `tests/test_providers.py -m live` | 4/4 lulus pada 5 dari 6 run; 1 run gagal pada satu tes tanpa pesan yang tertangkap |
| `uv build`, `docker compose --profile production config` | Lulus |

Flake live yang diketahui: ekstraksi Graphiti bersifat nondeterministik. Dalam lima eksekusi script debug, satu episode menghasilkan `edges: []` sehingga tidak ada fakta yang dapat di-recall. Tes isolasi kini memisahkan kegagalan ekstraksi ("Graphiti extraction produced no fact edge") dari kegagalan isolasi. Kegagalan satu dari enam run penuh kemungkinan besar berasal dari sini, tetapi pesannya tidak tertangkap sehingga belum terbukti. Tidak ada run yang menunjukkan fakta namespace lain muncul.

## Dokumentasi library

- [Pydantic validation dan model configuration](https://docs.pydantic.dev/latest/concepts/models/).
- [LangChain native agent dan system prompt](https://docs.langchain.com/oss/python/langchain/agents).
- [Graphiti namespacing](https://help.getzep.com/graphiti/core-concepts/graph-namespacing).
- Source terpasang yang dicocokkan: Graphiti 0.30.2 `search` (`handle_multiple_group_ids`), `add_episode`, `remove_episode`, `Neo4jDriver.__init__` (menjadwalkan build index bila ada event loop berjalan, sehingga factory tetap lewat `asyncio.to_thread`), dan `Neo4jDriver.health_check` (mencetak ke stdout, maka tidak dipakai); LangChain 1.4.2 `create_agent(middleware=...)` dan `ToolErrorMiddleware`.
