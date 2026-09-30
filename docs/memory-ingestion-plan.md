# Ingestion memori dengan provenance

Status: Task 1–5 selesai pada 30 September 2026 di branch `feat/memory-ingestion`, termasuk acceptance Meet dua sesi. Desain ingestion di dalam proses meeting di dokumen ini digantikan oleh ingestion dari catatan meeting setelah meeting (fase 2 [rencana jangka panjang](long-term-plan.md#status-fase-2-30-september-2026)); bagian di bawah tetap sebagai catatan tahap pertama. Acceptance menemukan kekurangan throughput, fakta pertanyaan, dan nama salah dengar; bukti dan rencana perbaikannya ada di [acceptance 30 September](acceptance-2026-09-30.md).

## Hasil yang dituju

Transkrip final dari meeting dan ucapan twin yang benar-benar selesai diputar masuk ke ruang Graphiti owner/twin (`profile.memory_group`) sebagai episode yang dapat ditelusuri asalnya: meeting, waktu kejadian, sumber, dan speaker. Recall mengembalikan fakta yang masih berlaku beserta asal dan waktunya, sehingga twin dapat membedakan fakta pemilik, klaim participant, dan ucapannya sendiri. Data disimpan tanpa batas waktu; operator dapat menambah catatan pemilik dan menghapus secara manual tanpa menyentuh data owner lain.

## Keputusan pengguna (30 September 2026)

1. Aplikasi tidak menyediakan mekanisme persetujuan peserta sebelum ingestion. Kepatuhan terhadap aturan rekaman menjadi tanggung jawab operator.
2. Tidak ada retensi otomatis. Memori disimpan sampai dihapus manual.
3. Ucapan twin ikut diingest, dengan batasan pada bagian "Ucapan twin".

## Keputusan desain

- Ingestion aktif hanya bila profil berisi `ingest_transcripts: true` dan `join --memory` dipakai. Field ini terpisah dari `allowed_tools` karena bukan tool model; model tidak dapat menyalakannya atau memilih apa yang disimpan.
- Hanya transkrip `finalized` dan nonblank yang diingest. Ucapan twin berasal dari `PlaybackEvent` `speak_finished`; `speak_dropped` dan `speak_failed` diabaikan karena bagian yang terdengar tidak diketahui.
- Transkrip adalah klaim participant, ucapan twin adalah ucapan twin, dan keduanya bukan fakta pemilik. Fakta pemilik adalah `verified_facts` profil dan catatan `memory-add`.
- Ingestion tidak memblokir capture atau conversation. Satu worker async memproses episode secara berurutan, sesuai anjuran docstring `Graphiti.add_episode`, dengan queue terbatas di luar `ConversationLoop`. Kegagalan dicatat sebagai fault dan tidak menghentikan sesi.
- Semua bagian memakai fitur library: `add_episode(source=EpisodeType.message, saga=..., custom_extraction_instructions=...)`, `SearchFilters` dengan `ComparisonOperator.is_null`, `EpisodicNode.get_by_uuids`, `retrieve_episodes(saga=...)`, `remove_episode`, dan `SagaNode.delete`. Tidak ada database, ledger, atau parser tambahan.

### Revisi dari draft rencana

| Draft | Implementasi | Alasan |
| --- | --- | --- |
| Ledger idempotensi di LangGraph store | Saga Graphiti `meeting:<meeting_id>` sebagai pengelompokan per meeting; dedupe `TranscriptEvent.id` di dalam proses | Satu sumber data. `meeting_id` yang sudah berjalan tidak dapat di-join ulang karena lifecycle menolaknya, sehingga duplikasi lintas proses tidak terjadi. Dua store dapat tidak sinkron. |
| Jendela campuran twin + participant | Episode dipisah per jenis speaker, lalu per 20 ucapan atau 60 detik | Provenance per episode menjadi pasti. Konteks percakapan tetap terbawa karena `add_episode` memberi extractor episode-episode sebelumnya di grup yang sama. |
| Speaker twin = nama tampilan | Speaker twin = `Digital twin`; nama Meet disebut di instruksi ekstraksi | Eval live: dengan fakta "Rani Twin said…", 6 dari 8 jawaban model menjadi "Rani bilang…". Dengan "Digital twin said…", 0 dari 8. Label tetap juga stabil saat nama twin diganti. |
| Participant tanpa identitas = `unknown` | `Unidentified participant`, instruksi ekstraksi melarang menggabungkannya dengan orang bernama | Prompt ekstraksi Graphiti selalu menjadikan speaker sebagai entity pertama. |
| Status `pending` untuk dilanjutkan | Drain saat close dibatasi 20 detik; sisa queue dicatat sebagai fault `memory_ingest`/`timeout` beserta jumlahnya | Belum ada command replay. Batas 20 detik berada di dalam `stop_grace_period` Compose 45 detik. |
| Koreksi lewat invalidasi temporal | Koreksi lewat urutan prioritas di prompt dan waktu di provenance | Live: owner note "dipindah ke Jumat" tidak meng-invalidate fakta "Unidentified participant setuju demo … Kamis", karena fakta berbentuk kejadian tetap benar sebagai sejarah. Recall kini menampilkan jam UTC dan prompt memprioritaskan `verified_facts`/owner note, lalu sumber terbaru. |
| Provenance tanggal saja | `YYYY-MM-DD HH:MM UTC` | Koreksi di hari yang sama perlu dapat diurutkan. |

## Ucapan twin

Ucapan twin diperlukan sebagai konteks: jawaban participant seperti "oke, setuju" hanya bermakna jika usulan twin sebelumnya ikut tersimpan. Risikonya adalah twin mengutip ulang tebakannya sendiri sebagai fakta.

- Yang disimpan adalah teks yang sudah dibatasi dua kalimat dan selesai diputar, dengan waktu saat event diterima.
- Speaker selalu `Digital twin`; `source_description` episode berakhiran `twin speech`.
- Ekstraksi live mempertahankan atribusi di teks fakta, misalnya "Digital twin mengusulkan agar demo customer … digeser ke hari Kamis." dan "Unidentified participant setuju demo customer … dipindah ke hari Kamis."
- Prompt: sumber `twin speech` atau fakta tentang `Digital twin` adalah ucapan twin sendiri, disebut "aku sempat bilang", bukan fakta pemilik, dan tidak cukup untuk komitmen.
- Sink audio terpisah mencegah suara twin masuk lagi lewat STT.

## Implementasi

| File | Isi |
| --- | --- |
| `src/fakeyou/ingestion.py` | `MeetingIngestion`: queue 500, batching per jenis speaker, 20 ucapan atau 60 detik, timeout episode 120 detik, drain 20 detik, journal `memory_episode` |
| `src/fakeyou/memory.py` | `meeting_saga`, `CURRENT_FACTS_FILTER`, `describe_facts`, `remember_owner_note`, `forget_meeting`, `forget_episode`, `RECALL_TIMEOUT_SECONDS = 15` |
| `src/fakeyou/persona.py` | `ingest_transcripts`, `load_persona(path, *, owner_id, twin_id)`, aturan sumber recall |
| `src/fakeyou/agent.py` | Guidance meeting: jawab langsung tanpa tawaran follow-up kecuali perlu konfirmasi |
| `src/fakeyou/cli.py` | `open_persona_memory`, ingestion di `join`, command `memory-add` dan `memory-forget` |

### Alur di `join`

1. Validasi profil. `--memory` memerlukan `recall_meeting_context` atau `ingest_transcripts`.
2. `open_persona_memory` membuka Graphiti dan menjalankan `verify_connectivity()` sebelum model, checkpoint, browser, dan audio. Kegagalan menghasilkan `infra_failure` dengan exit 3.
3. Bila `ingest_transcripts`, `MeetingIngestion` dibuat. Callback transcript memanggil `add_transcript` sebelum conversation; callback playback memanggil `add_twin_speech` untuk `speak_finished`.
4. Saat sesi selesai, conversation ditutup lebih dulu, lalu ingestion di-drain, lalu Graphiti ditutup, lalu journal.

### Recall

`recall_meeting_context` mencari dengan `CURRENT_FACTS_FILTER` (`invalid_at IS NULL`, `expired_at IS NULL`) dan batas waktu 15 detik di bawah deadline agent 60 detik. Setiap fakta diberi sumber dari episode asalnya:

```text
Unidentified participant setuju demo customer Orchid… dipindah ke hari Kamis. (source: Google Meet live-ingest-… participant transcript; 2026-09-30 07:52 UTC)
Rani memberi update bahwa demo customer Orchid… dipindah ke hari Jumat. (source: owner note; 2026-09-30 07:53 UTC)
```

Fakta tanpa episode yang dikenal diberi `(source: unknown)`. Recall yang gagal atau timeout dikembalikan sebagai `RECALL_UNAVAILABLE_RESULT` oleh `ToolErrorMiddleware` dan dicatat sebagai fault `memory`.

### Command operator

```sh
fakeyou memory-add "Update dari Rani: demo Orchid dipindah ke hari Jumat." --persona PATH --owner-id OWNER_ID --twin-id TWIN_ID
fakeyou memory-forget --meeting MEETING_ID --persona PATH
fakeyou memory-forget --episode EPISODE_UUID --persona PATH
```

- Identitas default diambil dari settings, seperti `join`; persona yang tidak cocok ditolak sebelum koneksi.
- `memory-add` mencetak `episode_uuid` dan fakta yang terekstrak. Jika tidak ada fakta, command memberi peringatan karena catatan itu tidak akan pernah muncul di recall.
- `memory-forget --meeting` mengambil episode saga per 100 lalu memanggil `remove_episode`, kemudian menghapus node saga. Loop berhenti dengan error bila sebuah episode masih ada setelah dihapus.
- `memory-forget --episode` hanya menghapus episode yang `group_id`-nya sama dengan persona; episode lain atau yang tidak ada ditolak dengan pesan yang sama.
- Neo4j yang tidak tersedia menghasilkan exit code 3.

## Task 1 — Kontrak event

- [x] Dedupe `TranscriptEvent.id` di dalam proses; event sama dua kali hanya diingest sekali. Transkrip interim dan kosong diabaikan.
- [x] Format body `"<speaker>: <text>"` satu baris per ucapan dengan whitespace dinormalisasi. `source_description` = `Google Meet <meeting_id> participant transcript|twin speech`.
- [x] `speak_finished` dari `on_playback_event` diteruskan ke ingestion; event playback lain diabaikan.

## Task 2 — Worker ingestion

- [x] Queue terbatas; saat penuh, ucapan dibuang tanpa menahan callback dan fault `memory_ingest_backlog`/`unavailable` dicatat.
- [x] Batching per jenis speaker, 20 ucapan, atau 60 detik (termasuk aliran tanpa jeda dan jendela yang sepi).
- [x] Satu meeting satu saga dengan `saga_previous_episode_uuid` berantai.
- [x] Episode yang gagal atau hang (120 detik) dicatat dan worker lanjut; close di-drain dengan batas 20 detik.
- [x] Ekstraksi kosong dicatat sebagai `outcome: "empty"`.

## Task 3 — Recall dengan provenance

- [x] Hanya fakta yang masih berlaku, beserta sumber dan waktu pertama episode asalnya.
- [x] Aturan prompt untuk `owner note`, `participant transcript`, `twin speech`, dan konflik antarsumber.
- [x] Recall dibatasi 15 detik; hang menjadi informasi belum diketahui, bukan `conversation_failure`.

## Task 4 — Koreksi dan penghapusan manual

- [x] `memory-add` untuk catatan pemilik; koreksi diselesaikan lewat prioritas sumber dan waktu (lihat revisi).
- [x] `memory-forget --meeting` dan `--episode`, tidak pernah berdasarkan pola grup atau `clear_data`.
- [x] Tidak ada retensi otomatis.
- [x] Command menolak persona yang tidak cocok dengan identitas.

## Task 5 — Bukti

- [x] Tes offline: 16 tes ingestion (dedupe, batching, backpressure, timeout, drain, fault, whitespace), tes provenance/forget/owner note di `test_memory.py`, dan tes `join`/operator di `test_cli.py`.
- [x] Tes live Graphiti: ingest ucapan twin dan participant → reopen → recall dengan provenance → owner note → recall → forget meeting dan episode → jumlah node grup kembali 0.
- [x] DeepEval live: delapan skenario, termasuk ucapan twin lama, klaim participant yang bertentangan, dan owner note yang mengoreksi meeting.
- [x] Acceptance Meet: `acc-0930-s1` → `acc-0930-s2` dengan owner/twin sama; keputusan kickoff dari sesi pertama dijawab benar di sesi kedua, tetapi asal meeting tidak disebut. Lihat [acceptance 30 September](acceptance-2026-09-30.md).

## Bukti pengujian 30 September 2026

| Pemeriksaan | Hasil |
| --- | --- |
| Suite offline `uv run pytest` | 596 lulus (setelah tahap persona 555) |
| Ruff check/format, strict mypy, DeepEval deterministik | Lulus; persona eval deterministik 3 lulus |
| Browser fixture `-m browser`, `uv build`, validasi Compose | 6 lulus; build dan Compose lulus |
| Provider live `tests/test_providers_live.py -m live` | 5/5 lulus pada tiga run berturut-turut setelah perbaikan fakta sintetis isolasi |
| DeepEval live `evals/test_persona.py -m live` dengan prompt final | 79/80 skenario-run lulus dalam 10 run; satu kegagalan `identity_override` (skor 0.62): model menolak identitas baru tetapi menawarkan konfirmasi ke owner alih-alih menolak tegas |
| Worker container | `memory-add` → `memory-forget --episode` terhadap service Neo4j berhasil; penghapusan kedua ditolak dengan exit 1; Neo4j kembali 0 node |

Ukuran live per episode (model bawaan Graphiti 0.30.2 `gpt-5.5` dengan small model `gpt-4.1-nano`, satu sampai dua ucapan): 7,1–12,4 detik dan 1–2 fakta. Dengan batching 20 ucapan/60 detik, meeting dengan participant saja menghasilkan sekitar satu episode per menit. Tanya-jawab yang sering melibatkan twin memecah episode lebih sering; bila backlog mulai terlihat di journal (`memory_episode.duration_ms` dan fault backlog), pertimbangkan jendela lebih panjang atau batching lintas speaker dengan label per baris.

Temuan live yang mengubah kode:

- Fakta sintetis "prefers espresso coffee" sering tidak diekstrak (prompt Graphiti melarang benda generik), sehingga tes isolasi gagal 3/3 pada satu sesi. Tes kini memakai relasi entity bernama ("works at Heronfield Analytics / Larkmoor Logistics") dan memeriksa node grup kembali 0.
- Model menambahkan tawaran follow-up yang menurunkan skor dua skenario ke 0,698–0,699. Guidance meeting kini meminta jawaban langsung tanpa tawaran follow-up kecuali perlu konfirmasi, yang juga memperpendek TTS.

## Sesudah tahap ini

1. Acceptance Meet gabungan untuk persona dan ingestion: satu sesi dengan pertanyaan fakta profil, recall, dan informasi yang belum ada; sesi kedua dengan owner/twin sama yang menanyakan keputusan sesi pertama. Catat latency tahap dan konfirmasi pendengaran.
2. Mata dan tangan pada halaman Meet yang sama: snapshot screenshare bertimestamp, observasi participant, chat, raise/lower hand, dan verifikasi hasil tindakan melalui Playwright/CloakBrowser.
3. Partisipasi multipihak: turn detection/VAD/interruption Pipecat native dan atribusi speaker. Nama participant yang terverifikasi otomatis menggantikan `Unidentified participant` di ingestion.
4. Operasi sesi: metrik latency/biaya ingestion, PostgreSQL checkpoint, kontrol start/stop, isolasi beberapa worker, lalu avatar/video bila diperlukan.

## Dokumentasi library

- [Graphiti adding episodes](https://help.getzep.com/graphiti/core-concepts/adding-episodes) dan [namespacing](https://help.getzep.com/graphiti/core-concepts/graph-namespacing).
- Source terpasang Graphiti 0.30.2 yang dicocokkan: `add_episode` (urutan, `uuid` hanya untuk memproses ulang episode yang ada, saga, previous episodes), prompt `extract_message` (speaker selalu menjadi entity), `retrieve_episodes(saga=...)`, `remove_episode` (hanya edge yang dibuat episode itu dan node yang hanya disebut episode itu), `SagaNode`, `SearchFilters`/`DateFilter`, dan `NodeNotFoundError` (bukan turunan `ValueError`).
