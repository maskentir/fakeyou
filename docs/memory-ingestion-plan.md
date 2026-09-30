# Ingestion memori dengan provenance

Status: rencana berikutnya, belum diimplementasikan; keputusan pengguna sudah masuk. Disusun 30 September 2026 setelah [persona dan retrieval memori](persona-memory-plan.md) selesai untuk Task 1–3. Task 4 rencana itu (acceptance Meet) tetap terbuka dan dapat berjalan paralel.

## Hasil yang dituju

Transkrip final dari meeting dan ucapan twin yang benar-benar selesai diputar masuk ke ruang Graphiti owner/twin (`profile.memory_group`) sebagai episode yang dapat ditelusuri asalnya: meeting, waktu kejadian, sumber, dan speaker (`twin`, participant yang diketahui, atau `unknown`). Recall mengembalikan fakta beserta asal dan masa berlakunya, sehingga twin dapat menjawab "itu dibahas di meeting tanggal X" dan membedakan fakta pemilik, klaim participant, serta ucapan twin sendiri. Ingestion ulang tidak menggandakan episode. Data disimpan tanpa batas waktu; operator dapat mengoreksi dan menghapus secara manual tanpa menyentuh data owner lain.

## Keputusan pengguna (30 September 2026)

1. Aplikasi tidak menyediakan mekanisme persetujuan peserta sebelum ingestion. Kepatuhan terhadap aturan rekaman menjadi tanggung jawab operator.
2. Tidak ada retensi otomatis. Memori disimpan sampai dihapus manual.
3. Ucapan twin ikut diingest, dengan batasan pada bagian "Ucapan twin" di bawah.

## Keputusan desain

- Ingestion aktif hanya bila profil mengizinkannya lewat field baru `ingest_transcripts: bool = False` dan `join --memory` dipakai. Ini bukan tool model, jadi tidak dimasukkan ke `allowed_tools`; model tidak dapat menyalakan atau memilih apa yang disimpan. Tidak ada flag CLI terpisah dan tidak ada pengumuman persetujuan (keputusan pengguna 1).
- Hanya event `finalized` yang diingest. Interim transcript tetap di konteks aktif saja.
- Transkrip adalah klaim participant, bukan fakta pemilik. Ucapan twin adalah ucapan twin, juga bukan fakta pemilik. `verified_facts` profil tetap satu-satunya fakta pemilik. Pengetahuan yang diberikan pemilik masuk lewat command operator terpisah dengan `source_description` yang berbeda.
- Ingestion tidak boleh memblokir capture atau conversation. Jalankan sebagai worker async dengan queue terbatas, di luar jalur `ConversationLoop`. Kegagalan dicatat sebagai fault `memory_ingest`, tidak menghentikan sesi.
- Pakai fitur Graphiti dan LangGraph yang ada: `add_episode(source=EpisodeType.message, reference_time=..., saga=...)`, invalidasi edge temporal bawaan (`valid_at`/`invalid_at`/`expired_at`), `remove_episode`, `EpisodicNode.get_by_uuids`, serta `langgraph.store.sqlite`/`postgres` untuk ledger idempotensi. Tidak ada database atau parser tambahan.
- Tidak menulis custom extraction pipeline. Bila kualitas ekstraksi kurang, gunakan `custom_extraction_instructions`, `entity_types`, atau `excluded_entity_types` milik Graphiti terlebih dahulu.

## Fakta dari source terpasang yang membentuk rencana

Diverifikasi pada Graphiti 0.30.2, 30 September 2026:

- `add_episode(uuid=...)` mengambil episode yang sudah ada lewat `EpisodicNode.get_by_uuid`; parameter ini untuk memproses ulang, bukan create idempoten. Dedupe tidak boleh bergantung padanya.
- `add_episode` menerima `saga` dan `saga_previous_episode_uuid` untuk merangkai episode berurutan. Ini kandidat untuk "satu meeting = satu saga".
- `EntityEdge` membawa `episodes` (UUID sumber), `valid_at`, `invalid_at`, `expired_at`, dan `reference_time`. `EpisodicNode` membawa `source_description`, `valid_at`, dan `episode_metadata`. `add_episode` tidak menerima `episode_metadata`, jadi provenance terstruktur perlu disimpan lewat `source_description`/`name` atau ledger; cek ulang sebelum memutuskan.
- Ekstraksi LLM nondeterministik. Tes live tahap persona menemukan episode yang menghasilkan `edges: []`. Ingestion harus mencatat hasil kosong dan tidak menganggapnya sukses diam-diam.
- `Neo4jDriver.__init__` menjadwalkan build index bila dipanggil di event loop berjalan; factory tetap lewat `asyncio.to_thread`.

## Ucapan twin

Ucapan twin diperlukan sebagai konteks: jawaban participant seperti "oke, setuju" hanya bermakna jika usulan twin sebelumnya ikut tersimpan. Risikonya adalah twin mengutip ulang tebakannya sendiri sebagai fakta di meeting berikutnya. Batasannya:

- Sumbernya `PlaybackEvent` `speak_finished`, yaitu teks yang sudah dibatasi dua kalimat dan selesai diputar, bukan jawaban mentah model. `speak_dropped` (interrupted) dan `speak_failed` tidak diingest karena bagian yang terdengar tidak diketahui. `PlaybackEvent` belum membawa waktu; ingestion memberi timestamp saat event diterima.
- Speaker selalu `twin`. Ucapan twin dan participant disusun berurutan waktu dalam satu jendela episode agar percakapan tetap utuh, sehingga atribusi per ucapan ada di prefix speaker pada body, bukan di `source_description` episode. Sumber tiap event (`meeting_audio` atau `twin_playback`) dicatat di ledger.
- Ekstraksi Graphiti dapat menghilangkan atribusi dari teks fakta (misalnya "twin: demo hari Kamis" menjadi fakta "Demo is on Thursday"). Karena itu recall wajib menelusuri `edge.episodes` ke episode sumber dan menampilkan speaker-nya. Kewajiban ini berlaku juga untuk klaim participant.
- Prompt persona: fakta yang hanya didukung ucapan twin disebut sebagai "yang pernah twin sampaikan", tidak pernah sebagai fakta pemilik, dan tidak boleh menjadi satu-satunya dasar komitmen.
- Loopback audio twin ke STT sudah dicegah oleh sink terpisah, sehingga ucapan twin tidak masuk dua kali lewat transkrip.

## Peta perubahan

| File | Tanggung jawab |
| --- | --- |
| `src/fakeyou/persona.py` | Field `ingest_transcripts: bool = False`, terpisah dari `allowed_tools` |
| `src/fakeyou/ingestion.py` baru | Queue terbatas, batching per jendela, ledger idempotensi, pemanggilan `add_episode` |
| `src/fakeyou/playback.py` | Tidak berubah; callback `on_playback_event` yang sudah ada meneruskan `speak_finished` ke ingestion |
| `src/fakeyou/memory.py` | Recall menyertakan provenance (tanggal, meeting, sumber) dan hanya fakta yang masih berlaku |
| `src/fakeyou/cli.py` | Ingestion di `join --memory` bila profil mengizinkan; command operator `memory add` dan `memory forget` |
| `src/fakeyou/conversation.py` | Tidak berubah; ingestion menerima `TranscriptEvent` dari callback transcript yang sama |
| `tests/test_ingestion.py` baru | Dedupe, batching, backpressure, cleanup, fault |
| `tests/test_providers_live.py` | Ingest → reopen → recall dengan provenance; koreksi; forget |
| `evals/test_persona.py` | Skenario "kapan itu dibahas", klaim participant vs fakta pemilik, ucapan twin lama yang keliru, fakta yang sudah dikoreksi |

## Task 1 — Kontrak event dan ledger

- [ ] Definisikan kunci idempotensi dari `TranscriptEvent.id` (sudah hash dari source, timestamp, dan text) ditambah `memory_group`. Ucapan twin memakai hash dari meeting ID, waktu `speak_finished`, dan teks. Tulis tes: event sama dua kali (termasuk setelah reconnect STT) hanya menghasilkan satu episode.
- [ ] Pilih penyimpanan ledger: `AsyncSqliteStore` untuk CLI lokal, `AsyncPostgresStore` untuk worker production. Namespace store `("ingestion", memory_group)`, value berisi episode UUID, meeting ID, waktu, dan status (`pending`, `ingested`, `empty`, `failed`).
- [ ] Tentukan format body episode: satu baris per ucapan, `"<speaker>: <text>"`, berurutan waktu. Speaker participant `unknown` bila tidak diketahui; ucapan twin selalu `twin`. `source_description` episode memuat meeting ID dan rentang waktu jendela; sumber per event (`meeting_audio` atau `twin_playback`) ada di ledger. Jangan memakai nama tampilan twin sebagai speaker participant.
- [ ] Sambungkan `speak_finished` dari callback `on_playback_event` ke ingestion; uji bahwa `speak_dropped` dan `speak_failed` tidak diingest.

## Task 2 — Worker ingestion

- [ ] Worker async dengan `asyncio.Queue(maxsize=...)`. Ketika penuh, catat fault dan hitung event yang terlewat; jangan menahan callback transcript.
- [ ] Batching per jendela waktu atau jumlah ucapan untuk menekan biaya LLM Graphiti. Ukur dulu biaya dan latency satu episode per ucapan vs per jendela 60 detik sebelum memilih default.
- [ ] Satu meeting satu saga (`saga=meeting:<meeting_id>`) bila hasil uji menunjukkan urutan episode membantu ekstraksi; bandingkan dengan `previous_episode_uuids` default.
- [ ] Close: drain dengan batas waktu, lalu tandai sisa event sebagai `pending` di ledger agar dapat dilanjutkan tanpa duplikasi. Stop owner tidak menunggu ingestion selesai.
- [ ] Hasil ekstraksi kosong dicatat sebagai `empty`, bukan `ingested`.

## Task 3 — Recall dengan provenance

- [ ] Recall mengembalikan fakta yang `invalid_at`/`expired_at`-nya kosong, beserta tanggal `valid_at` dan `source_description` episode sumber (`EpisodicNode.get_by_uuids(edge.episodes)`). Format tetap ringkas agar muat di jawaban dua kalimat.
- [ ] Prompt persona: fakta dari transkrip disebut sebagai "dibahas di meeting tanggal X oleh Y/unknown", dan fakta yang hanya didukung ucapan twin disebut sebagai "yang pernah twin sampaikan". Keduanya bukan fakta pemilik.
- [ ] Tambahkan batas waktu khusus recall di bawah deadline agent 60 detik, sehingga Graphiti yang hang dikembalikan sebagai informasi belum diketahui lewat middleware yang sama, bukan `conversation_failure`. Ini sisa dari tahap persona.

## Task 4 — Koreksi dan penghapusan manual

- [ ] `memory add --persona PATH TEXT` untuk pengetahuan yang diberikan pemilik (sumber `owner`), terpisah dari transkrip. Koreksi pemilik mengandalkan invalidasi temporal Graphiti; uji bahwa fakta lama menjadi `invalid_at` dan recall hanya mengembalikan yang baru.
- [ ] `memory forget --persona PATH --meeting MEETING_ID` menghapus episode milik meeting itu lewat UUID di ledger dan `remove_episode`. Jangan pernah menghapus berdasarkan pola group atau `clear_data`.
- [ ] Tidak ada retensi otomatis atau command prune (keputusan pengguna 2). Ledger tetap menyimpan UUID episode agar penghapusan manual per meeting selalu dapat dilakukan.
- [ ] Semua command menolak persona yang owner/twin-nya tidak cocok dengan argumen identitas, seperti `join`.

## Task 5 — Bukti

- [ ] Tes offline: dedupe, backpressure, cleanup, provenance recall, hanya `speak_finished` yang diingest, dan `ingest_transcripts` diberlakukan di composition root.
- [ ] Tes live Graphiti opt-in: ingest transkrip sintetis dua owner → reopen → recall dengan provenance → koreksi → forget. Cleanup hanya UUID episode tes; hitung node sebelum dan sesudah.
- [ ] DeepEval live: pertanyaan "kapan itu dibahas", klaim participant yang bertentangan dengan fakta pemilik, ucapan twin lama yang keliru dan tidak boleh dikutip sebagai fakta, serta fakta yang sudah dikoreksi. Catat skor dan flake ekstraksi terpisah.
- [ ] Acceptance Meet: dua meeting berurutan dengan owner/twin sama; pertanyaan di meeting kedua tentang keputusan di meeting pertama dijawab beserta asalnya.

## Sesudah tahap ini

1. Mata dan tangan pada halaman Meet yang sama: snapshot screenshare bertimestamp, observasi participant, chat, raise/lower hand, dan verifikasi hasil tindakan melalui Playwright/CloakBrowser.
2. Partisipasi multipihak: turn detection/VAD/interruption Pipecat native, pertanyaan kepada twin, overlap, dan atribusi speaker yang tetap `unknown` bila ambigu. Atribusi ini juga meningkatkan kualitas provenance ingestion.
3. Operasi sesi: metrik latency/biaya, PostgreSQL checkpoint dan store, kontrol start/stop, isolasi beberapa worker, lalu avatar/video bila diperlukan.

## Dokumentasi library

- [Graphiti adding episodes](https://help.getzep.com/graphiti/core-concepts/adding-episodes) dan [namespacing](https://help.getzep.com/graphiti/core-concepts/graph-namespacing).
- [LangGraph persistence dan store](https://docs.langchain.com/oss/python/langgraph/persistence).
- Cocokkan dengan source terpasang Graphiti 0.30.2 (`add_episode`, `add_episode_bulk`, `retrieve_episodes`, `remove_episode`, saga) dan `langgraph-checkpoint-sqlite` sebelum implementasi.
