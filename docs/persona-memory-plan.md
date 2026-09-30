# Persona pemilik dan retrieval memori

Status: rencana berikutnya, belum diimplementasikan. Disusun 30 September 2026 setelah [refactor voice reliability](voice-reliability-refactor-2026-09-30.md). Dokumen ini menggantikan rencana persona tanggal 28 September di `docs/superpowers/plans/`.

## Hasil yang dituju

Twin mengikuti profil pemilik yang tervalidasi, mengingat konteks meeting melalui checkpoint yang sudah ada, dan dapat mengambil pengetahuan dari ruang Graphiti milik owner/twin yang sama. Ketika fakta atau kewenangan tidak tersedia, twin meminta klarifikasi. Ini tahap identitas dan pengetahuan; kemampuan melihat slide, memakai UI Meet, dan mengambil giliran bicara multipihak menyusul.

Fondasi guest, audio dua arah, dan conversation loop sudah tersedia. Live conversation pernah dijalankan pada 29–30 September; pengukuran first-audible audio dan stop latency belum tersedia. Unit persona dapat berjalan sambil melengkapi bukti media tersebut.

## Keputusan desain

- Gunakan Pydantic 2 untuk profil JSON, `create_meeting_agent(..., persona=...)` untuk prompt, dan Graphiti `group_id` untuk ruang memori. Tidak ada framework agent, parser JSON, atau database memori tambahan.
- Profil adalah konfigurasi operator. Teks participant, hasil retrieval, dan screenshot tidak boleh mengubah owner, namespace, atau daftar tool yang tersedia.
- Namespace berasal dari `owner_id` dan `twin_id`, stabil lintas meeting. Checkpoint conversation tetap memakai owner/twin/meeting agar konteks meeting berbeda tidak tercampur.
- Tahap ini hanya memberi tool recall kepada meeting agent. Penulisan memori, ingestion otomatis, koreksi, provenance lengkap, retensi, dan penghapusan menjadi tahap terpisah.
- Tool permission diberlakukan di composition root sebelum agent dibuat. Prompt saja bukan batas akses. Namespace lokal juga bukan pengganti autentikasi untuk deployment multiuser.
- Pertahankan queue conversation tunggal, batas dua kalimat untuk jawaban model, retry TTS tanpa replay PCM, dan cleanup audio/browser. Capture tetap berjalan ketika agent atau Graphiti bekerja.
- Baca dokumentasi serta source versi terkunci sebelum implementasi. Production Python tetap tanpa comment/docstring; kontrak dan alasan desain ditulis di dokumen ini.

## Peta perubahan

| File | Tanggung jawab |
| --- | --- |
| `src/fakeyou/persona.py` baru | Schema profil, validasi identitas, prompt, namespace |
| `src/fakeyou/cli.py` | Validasi sebelum resource eksternal; injeksi profil dan tool recall |
| `src/fakeyou/agent.py` | Pertahankan factory native; ubah hanya bila kontrak prompt memerlukan |
| `src/fakeyou/memory.py` | Pakai factory dan tools yang sudah ada; tidak menambah ingestion |
| `tests/test_persona.py` baru | Validasi, prompt, identitas, isolasi namespace |
| `tests/test_cli.py` | CLI, urutan startup, daftar tool nyata yang diterima agent |
| `tests/test_providers_live.py` | Retrieval dua namespace sintetis setelah reopen |
| `evals/test_persona.py` baru | Skenario persona, fakta tidak diketahui, batas kewenangan |
| `examples/persona.example.json` baru | Profil sintetis lengkap yang dapat dicoba |
| `README.md`, `docs/project-status.md` | Usage, bukti pengujian, dan batas kemampuan |

## Task 1 — Profil dan identitas

Interface: `PersonaProfile`, `load_persona(path: Path, session: SessionConfig) -> PersonaProfile`, `build_persona(profile: PersonaProfile) -> str`.

- [ ] Buat tes gagal untuk JSON rusak, field asing, teks wajib kosong, owner/twin tidak cocok, tool tidak dikenal, dan namespace yang tertukar. Nama tampilan berbeda tidak mengubah namespace; dua meeting untuk owner/twin sama memakai namespace sama.
- [ ] Pakai `ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)`, tipe `SessionId`/`TwinName` yang sudah ada, dan collection immutable. Field: `owner_id`, `twin_id`, `display_name`, `role`, `language`, `speaking_style`, `goals`, `verified_facts`, dan `allowed_tools: frozenset[Literal["recall_meeting_context"]]`.
- [ ] Validasi string isi profil sebagai nonblank dengan batas panjang/jumlah yang jelas; jangan membangun parser sendiri. Default bahasa `id`, gaya ringkas, dan permission kosong.
- [ ] Derivasikan `memory_group` sebagai `twin_` diikuti UUID5 dari JSON `[owner_id, twin_id]`. Jangan memasukkan nama, meeting ID, transkrip, atau parameter model.
- [ ] Gunakan `model_validate_json` dalam `load_persona`; jalankan file I/O melalui `asyncio.to_thread`. Tolak owner/twin mismatch sebelum model, database, browser, dan audio dibuka.
- [ ] Tentukan nama tampilan: tanpa override CLI, gunakan `display_name` profil; override eksplisit `--twin-name` tetap menang. Identitas owner/twin tetap harus cocok dengan `SessionConfig`, bukan diambil dari ucapan participant.
- [ ] Render prompt melalui JSON serialization native. Pisahkan fakta yang diberikan pemilik, klaim participant, dan inferensi. Sesuaikan guidance bahasa default agar tidak bertentangan dengan `language` profil. Jangan mengubah JSON profil menjadi instruksi bebas untuk memilih tool.
- [ ] Jalankan `uv run pytest tests/test_persona.py -q`, Ruff, dan mypy sampai lulus.

## Task 2 — Komposisi agent dan recall

Interface CLI: `join --audio --converse --persona PATH [--memory]`. `open_conversation` menerima profil opsional yang sudah tervalidasi.

- [ ] Tambah tes CLI: `--persona` memerlukan `--converse`; `join --memory` memerlukan persona; mismatch ditolak sebelum resource dibuka. Tanpa profil, default conversation tetap tersedia. `chat --memory` tetap menggunakan konfigurasi grup lokal yang eksplisit.
- [ ] Injeksi hasil `build_persona` ke factory agent. Terapkan filter tools sebelum `create_meeting_agent`, berdasarkan nama tool yang benar-benar ada dalam profil. Daftar tool meeting tidak pernah memuat `remember_meeting_fact` pada tahap ini.
- [ ] Bila `--memory` aktif tetapi recall tidak diizinkan, tolak konfigurasi dengan pesan jelas sebelum koneksi database. Jangan membuka Graphiti tanpa tool yang akan digunakan.
- [ ] Gunakan `profile.memory_group` pada `create_memory_tools`. Uji daftar tool dan argumen `group_ids` yang sampai ke client, bukan hanya isi prompt.
- [ ] Batas waktu retrieval mengikuti deadline invocation agent yang sudah ada. Gagal recall tidak boleh berubah menjadi fakta rekaan atau menutup capture. Uji cleanup bila database gagal saat startup dan perilaku conversation saat retrieval gagal.
- [ ] Rapikan startup optional memory: beri kegagalan koneksi alasan dan exit yang dapat diamati sebelum browser dibuka. Preflight DNS/TLS saat ini hanya memeriksa host model/STT/TTS; jangan mengklaim ia sudah mencakup Neo4j. Pakai kontrak lifecycle/fault yang ada, tanpa keluarga exception baru bila tidak perlu.
- [ ] Uji stop selama retrieval, startup failure, profil tanpa memory, serta batas dua kalimat. Jalankan `uv run pytest tests/test_cli.py tests/test_conversation.py tests/test_runtime_audio.py -q`.

## Task 3 — Bukti isolasi dan kualitas

- [ ] Uji offline dua owner dan dua twin; rename tidak memindahkan memori; meeting berbeda tidak berbagi checkpoint conversation. Pastikan prompt tidak dapat memilih grup lain melalui argumen tool.
- [ ] Extend tes Graphiti opt-in dengan dua namespace unik dan fakta sintetis yang bertentangan. Reopen client sebelum recall, pastikan masing-masing fakta ditemukan dan fakta owner lain tidak muncul. Bersihkan hanya episode UUID milik tes; jangan menghapus grup/database pengguna.
- [ ] Tambah skenario DeepEval: fakta profil yang diketahui, pertanyaan tanpa bukti, permintaan komitmen di luar kewenangan, recall relevan, serta transcript yang meminta mengganti identitas/namespace. Pisahkan tes wiring dengan respons terprogram dari penilaian kualitas model live; skor mock bukan bukti kualitas model.
- [ ] Jalankan suite offline, browser fixture, Ruff, strict mypy, DeepEval, build, dan validasi Compose. Catat hasil live Graphiti secara terpisah; jangan menandai lulus bila tidak dijalankan.
- [ ] Dokumentasikan contoh profil sintetis dan command lengkap. Profil personal dan credential tidak menjadi fixture repository.

## Task 4 — Acceptance Meet terkontrol

- [ ] Gunakan meeting uji yang sedang dihost dan identitas sesi baru. Twin menjawab satu fakta profil dan satu fakta Graphiti, lalu mengakui ketika informasi belum tersedia.
- [ ] Ajukan dua pertanyaan yang saling terkait; pastikan checkpoint dipakai dengan benar. Uji interupsi dan stop selama model/recall bekerja, tanpa jawaban terlambat setelah stop.
- [ ] Catat final transcript → model selesai → first PCM → audio yang benar-benar terdengar oleh participant. `speak_started` hanya dispatch playback lokal; first PCM dari provider juga belum membuktikan waktu suara di telinga participant.
- [ ] Ukur stop request → audio berhenti dan catat titik pemicu. Barge-in saat ini menunggu transkrip final, belum memakai onset speech native Pipecat.
- [ ] Simpan run ID, journal, hasil terminal, dan konfirmasi pendengaran terpisah. Ketiadaan host tidak menghalangi unit work; acceptance live tetap terbuka sampai benar-benar diuji.

## Sesudah tahap ini

1. Ingestion dengan provenance: event ID, waktu sumber, meeting/participant yang diketahui, koreksi, deduplikasi, dan retensi. Hindari menjadikan semua ucapan participant sebagai fakta pemilik.
2. Mata dan tangan pada halaman Meet yang sama: snapshot screenshare bertimestamp, observasi participant, chat, raise/lower hand, dan verifikasi hasil tindakan melalui Playwright/CloakBrowser.
3. Partisipasi multipihak: evaluasi turn detection/VAD/interruption Pipecat native untuk berhenti pada awal speech, pertanyaan kepada twin, overlap, dan identitas `unknown` bila ambigu.
4. Operasi sesi: metrik latency/biaya, PostgreSQL checkpoint, kontrol start/stop, isolasi beberapa worker, lalu avatar/video bila diperlukan.

## Dokumentasi library

- [Pydantic validation dan model configuration](https://docs.pydantic.dev/latest/concepts/models/).
- [LangChain native agent dan system prompt](https://docs.langchain.com/oss/python/langchain/agents).
- [Graphiti namespacing](https://help.getzep.com/graphiti/core-concepts/graph-namespacing).
- Sebelum implementasi, cocokkan dokumentasi dengan source terpasang: Graphiti 0.30.2 `search`/`add_episode`, LangChain 1.4.2 `create_agent`, dan contoh cleanup tes live repository. Jangan mengandalkan signature dari dokumentasi versi terbaru saja.
