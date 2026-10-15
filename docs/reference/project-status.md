# Status fondasi dan pekerjaan berikutnya

Diperbarui: 15 Oktober 2026. Status mencakup semua milestone yang sudah digabung sejak 2 Oktober (multiparty, brief/delegasi/debrief, memori tepercaya, prefetch, live-trust-latency, unknown-speaker, latency-recovery) serta milestone operasi worker yang berjalan di branch. Langkah lanjut ada di [rencana pengembangan berikutnya](../plans/next-development.md) dan [rencana operasi worker](../plans/worker-operations.md).

## 15 Oktober 2026: Pipecat dilepas

- Pipecat tidak lagi menjadi dependensi. Graf audio (`audio/graph.py`), VAD Silero v5 yang di-vendor (`audio/vad.py`), capture `parec` (`audio/capture.py`), dan klien Muse (`transcription/muse.py`) menggantikannya. Rencana dan checklist ada di [lepas Pipecat](../plans/drop-pipecat.md).
- Mode Muse default sekarang `DIARIZATION`. Label speaker dicatat sebagai `voice_clusters` pada transkrip. `ENDPOINTING` tetap nilai valid untuk `FAKEYOU_STT__MODE`.
- Belum diverifikasi: capture `parec` di container dengan `PULSE_SOURCE` hidup, rapat Google Meet sungguhan, ElevenLabs dengan key sungguhan, dan penutupan sesi Muse pada 60 menit.

## Posisi proyek sekarang

Fondasi komponen sudah berjalan: agent LangChain/LangGraph, checkpoint PostgreSQL, browser tools, layanan suara, client memori, dan perangkat evaluasi. Muse menerima audio meeting nyata melalui layanan live dan menghasilkan transkrip final.

Worker masuk sebagai guest, menunggu admission, mendengar lewat Muse, menjawab melalui TTS streaming, dan leave. [Acceptance ketiga](../reports/2026-10-01-refactor-acceptance.md) membuktikan startup, recall lintas sesi, konteks kata ganti, interupsi, dan stop tanpa suara terlambat; host mengonfirmasi jawaban terdengar utuh.

Sejak itu digabung berurutan: multiparty (participant observer read-only, kebijakan partisipasi, identitas speaker wajib di transkrip; resolver DOM yang tidak pernah tersambung dihapus pada H5), brief meeting tervalidasi dengan snapshot per meeting, delegasi terbatas (prompt hardened, harness eval brief/trust), debrief beralas sumber dengan peran model sendiri dan pemuatan dotenv CLI, provider Gemini native, lalu prefetch memori satu trip. Acceptance live brief/delegasi 4–5 Oktober (tiga sesi Meet nyata) meluluskan perilaku kewenangan — semua permintaan approve budget, deadline, kontrak, tanda tangan, dan kirim pesan ditolak — serta stop saat model bekerja; debrief lolos cek kutipan verbatim di tiga sesi. Bukti: [acceptance Meet](../reports/2026-10-05-meet-acceptance.md).

Memori tepercaya selesai dan digabung: recall terfilter status, koreksi idempoten, 72/72 output korpus lolos review manusia independen, dan acceptance live dua sesi. Prefetch menyusul: evidence sebagai hasil tool sistem anti-spoof, konteks follow-up lintas giliran, lalu evidence compact v2 dengan trim peringkat-relevansi. Identitas penutur tak terverifikasi ditulis eksplisit di evidence dan ingestion, sehingga laju penyangkalan c04 turun ke 3,3% tepat di batas, dengan 0 penyangkalan pada 4 sondase live. Konfigurasi live terpilih adalah `gemini-3.8-flash` + prefetch dengan penalaran default. Bukti: [recall-latency](../reports/2026-10-04-recall-latency.md), [live-trust-latency](../reports/2026-10-05-live-trust-latency.md), [unknown-speaker](../reports/2026-10-05-unknown-speaker.md).

Gate latency live masih gagal di semua acceptance: sesi terakhir `lat-rec-1005` mencatat akhir ucapan → PCM pertama p50 5,05 detik dan p95 8,66 detik (batas ≤3/≤6), dengan agent masih dominan (p50 3,46 detik); STT/TTS stabil. Sesi itu mencatat 0 severe dari 22 jawaban pada review independen dan stop audio pemicu PCM lulus. Bukti: [acceptance latency-recovery](../reports/2026-10-05-meet-acceptance-latency-recovery.md).

Milestone operasi worker berjalan di branch `feat/worker-operations` (belum digabung): lock lease lintas container, profil browser per meeting, checkpoint Postgres opsional, CLI `worker start/status/stop/ingest`, lanjutan crash `--resume`, dan ingestion sebagai job terpisah. Acceptance dua meeting paralel masih menunggu dua link Meet baru.

Refactor utama sudah digabung ke `master` pada `6926e9b`, disusul cleanup `feat/legacy-config-cleanup` dan kinerja percakapan `feat/conversation-performance` (fungsi live lulus pada 4 sesi Meet; performa belum lulus dengan median 3,243 dtk dan p95 8,319 dtk pada 23 jawaban; bukti: [acceptance live](../reports/2026-10-02-live-acceptance.md)). Milestone memori tepercaya yang dulu berjalan di branch kini sudah digabung; bukti tetap: [close-out memori tepercaya](../reports/2026-10-02-trust-closeout.md), [acceptance live](../reports/2026-10-02-trust-live.md).

Target akhirnya tetap peserta yang dapat mendengar, memahami pembicaraan dan visual, mengingat, berbicara, serta memakai UI atas nama pemilik dengan kewenangan yang jelas.

## Yang sudah ada dan batasnya

| Bagian | Bukti saat ini | Yang belum selesai |
| --- | --- | --- |
| Setup proyek | Python 3.12, dependency terkunci, lint, typing, build, Compose, dan CI GitHub sudah diperiksa tiap merge | Backend Kubernetes; acceptance dua meeting paralel |
| Otak: LangChain + LangGraph | Agent native dan loop transkrip→agent→sintesis→playback, pemrosesan berurutan, checkpoint per owner/twin/meeting; brief, delegasi terbatas, dan debrief beralas sumber lulus acceptance live | Latency live (p50 5,05 dtk lawan target ≤3), gap kualitas replay |
| Memori percakapan | Isolasi `thread_id`, pembukaan ulang, dan round-trip checkpoint PostgreSQL teruji (tes Postgres nyata lewat `TEST_DATABASE_URL`); lanjutan crash `--resume` di branch worker | Acceptance lanjutan crash di dua meeting paralel |
| Telinga: Muse + graf audio sendiri | Audio peserta Meet→PulseAudio→Muse menghasilkan transkrip final live (sebelum Pipecat dilepas); capture 16 kHz mono; identitas speaker wajib di transkrip dengan observer dan kebijakan partisipasi | Overlap, dedupe reconnect, dan atribusi nama penuh; capture `parec` di container dan Meet live setelah Pipecat dilepas |
| Mulut: TTS streaming + `paplay` | PCM 24 kHz→virtual microphone→Meet; antrean serial, mute saat diam, barge-in; host mengonfirmasi jawaban utuh | Waktu PCM pertama dan performa lintas lebih banyak sesi |
| Tangan: CloakBrowser humanized langsung + Playwright MCP | Join, admission, deny, timeout, stop, dan leave terverifikasi live; tool MCP tersedia untuk agent | Tindakan lanjutan (chat, raise hand, mute) dan sesi agent/browser yang terintegrasi |
| Mata | Tool DOM dan screenshot tersedia | Observasi screenshare/kamera berkelanjutan dan pemahaman visual oleh model multimodal |
| Memori pengetahuan: Graphiti + Neo4j | Isolasi namespace owner/twin, ingestion per saga yang dapat dilanjutkan, catatan pemilik, hapus manual, recall terfilter status, prefetch satu trip anti-spoof, dan penutur tak terverifikasi eksplisit; c04 di 3,3% | Provenance waktu asli, atribusi nama participant, duplikat fakta |
| Persona | Profil JSON tervalidasi, batas tool, namespace owner/twin, brief per meeting, delegasi terbatas, dan debrief beralas sumber | Gate kualitas replay model aktif serta kewenangan granular untuk tindakan |
| Evaluasi: DeepEval + pytest | Tool trajectory deterministik, wiring persona, 5 skenario kualitas persona live (GEval), harness brief/trust, dan review independen tiap korpus live | Skenario meeting nyata multipihak dan evaluasi memori hasil ingestion |
| Runtime aplikasi | Worker Linux/PulseAudio, CLI `join`, checkpoint lifecycle, stop/deadline, cleanup audio/percakapan, dan CLI `worker start/status/stop/ingest` dengan lock lease di branch | Backend Kubernetes, penjadwalan, serta antarmuka pengguna |

Sumber implementasi: [agent](../../src/fakeyou/conversation/agent.py), [browser](../../src/fakeyou/meet/browser.py), [voice](../../src/fakeyou/transcription/muse.py), [memory](../../src/fakeyou/memory/memory.py), dan [CLI](../../src/fakeyou/cli/).

LangGraph menyimpan graph agent dan lifecycle meeting. CLI `chat` menjalankan satu permintaan; CLI `join` memiliki browser sepanjang sesi. Checkpoint tidak mempertahankan resource browser/audio setelah proses mati. Refactor memisahkan konfigurasi, persistence lifecycle, adapter Meet, dan worker; lihat [batas modul dan verifikasi](../reports/2026-09-28-refactor.md).

## Pengujian dan konfigurasi

Hasil pengujian terakhir yang tercatat:

- Operasi worker di branch (5 Oktober): 2.961 tes non-live/non-container lulus; Ruff, format, mypy bersih; container terverifikasi pada Task 3 dan akan diulang penuh di Task 7; acceptance dua meeting paralel menunggu link Meet.
- Acceptance latency-recovery (5 Oktober, `lat-rec-1005`): 22 jawaban, 0 severe pada review independen, stop PCM lulus; gate latency live gagal (p50 5,05 dtk, p95 8,66 dtk). Lihat [laporan acceptance](../reports/2026-10-05-meet-acceptance-latency-recovery.md).
- Acceptance unknown-speaker (5 Oktober): c04 turun ke 3,3% tepat di batas; 0 penyangkalan live. Lihat [laporan unknown-speaker](../reports/2026-10-05-unknown-speaker.md).
- Acceptance brief/delegasi (4–5 Oktober): perilaku kewenangan lulus di tiga sesi Meet nyata; debrief lolos cek kutipan. Lihat [laporan acceptance](../reports/2026-10-05-meet-acceptance.md).
- Acceptance ketiga (1 Oktober): fungsi live lulus; performa belum lulus. Source live `2a56df7` dan verifikasi cleanup sesudahnya dibedakan di [laporan acceptance](../reports/2026-10-01-refactor-acceptance.md).
- Cleanup lanjutan pada `ff60109`: 1.403 tes non-live/non-container lulus; 16 dilewati dan 20 tidak dipilih. Pengujian ini tidak mengulang provider atau Meet.
- Acceptance Meet persona + ingestion (30 September): dua sesi `acc-0930-s1`/`acc-0930-s2` berhasil end-to-end, termasuk recall lintas meeting, interupsi, dan stop tanpa jawaban terlambat; kekurangan dan rencana perbaikan di [acceptance 30 September](../reports/2026-09-30-acceptance.md).
- Ingestion memori (30 September): 596 tes default lulus; 5 tes provider live lulus pada tiga run berturut-turut, termasuk roundtrip ingest → recall → koreksi → forget; DeepEval persona live 79/80 skenario-run dalam 10 run. Lihat [bukti ingestion](../plans/memory-ingestion.md#bukti-pengujian-30-september-2026).
- Persona dan retrieval memori (30 September): 555 tes default dan 6 tes browser lulus; 4 tes provider live dan 5 skenario DeepEval persona live lulus, dengan satu flake ekstraksi Graphiti yang tercatat. Lihat [bukti persona](../plans/persona-memory.md#bukti-pengujian-30-september-2026).
- Refactor conversation: 244 tes default dan 6 tes browser lulus; [bukti dan batas verifikasi](../reports/2026-09-28-conversation-refactor.md).
- Hasil regresi refactor terbaru tersedia di [laporan refactor](../reports/2026-09-28-refactor.md).
- 5 tes browser asli lulus, termasuk join sampai leave pada fixture lokal dengan checkpoint SQLite asli.
- 3 tes provider live lulus: agent/tool call, TTS, dan Graphiti reopen/search. Lihat [laporan provider](../reports/2026-09-28-provider-verification.md).
- 1 tes Muse live lulus; fixture audio sepanjang 9,076 detik. Run setelah penyederhanaan nama konfigurasi selesai dalam 11,17 detik, termasuk startup dan streaming. Angka ini bukan latency percakapan.
- 1 evaluasi DeepEval lulus dengan Tool Correctness 1.0 dan respons model terprogram. Tool `raise_hand` di evaluasi adalah test double, bukan tindakan di Google Meet.
- Ruff, format, mypy, build, resolusi dependency, dan konfigurasi Compose lulus.

Lihat [verifikasi](../runbooks/verification.md) dan [laporan regresi](../reports/2026-09-27-regression.md) untuk cakupan dan keterbatasan. Pemeriksaan status ini tidak mengulang panggilan provider berbayar.

Setiap peran memakai key sendiri lewat bagian konfigurasinya: `FAKEYOU_REPLY__API_KEY`, `FAKEYOU_TTS__API_KEY`, `FAKEYOU_STT__API_KEY` (Muse), `FAKEYOU_MEMORY__LLM__API_KEY`, dan `FAKEYOU_MEMORY__EMBEDDER__API_KEY`; tidak ada key yang dipinjam antarperan. Provider setiap peran dinyatakan eksplisit (`FAKEYOU_REPLY__PROVIDER`, `FAKEYOU_TTS__PROVIDER`, `FAKEYOU_MEMORY__LLM__CLIENT`).

Perintah `fakeyou check` mencetak skema konfigurasi efektif dengan secret disamarkan. Command itu tidak menguji API atau membaca hasil pytest. Kelulusan live berasal dari tes provider dan laporan acceptance tersendiri.

## Tahapan fondasi dan kebutuhan lanjutan

Bagian ini mempertahankan urutan pembangunan fondasi beserta catatan kemajuannya. Acceptance 1 Oktober di atas memperbarui tahap percakapan dan persona yang sebelumnya masih terbuka. Untuk pekerjaan yang akan dieksekusi berikutnya, gunakan [rencana jawaban cepat dan bukti kualitas](../plans/next-development.md); urutannya dilanjutkan dengan atribusi participant, provenance memori, tindakan Meet, dan operasi worker.

### 0. Lengkapi verifikasi provider

Uji penalaran dan tool call OpenAI dengan prompt sintetis, TTS dengan kalimat singkat, serta ingestion dan retrieval Graphiti pada namespace pengujian. Periksa hasil setelah client dibuka ulang agar yang diuji adalah penyimpanan sebenarnya.

**Selesai ketika:** model memberi respons dan menjalankan tool yang sesuai, TTS menghasilkan audio yang dapat diputar, serta Graphiti mengembalikan fakta uji dari penyimpanan. Key yang terbaca saja belum memenuhi kriteria ini.

**Kemajuan:** ketiga jalur lulus uji live sintetis. Audio TTS terbukti sebagai PCM unary; pendengaran oleh peserta Meet masih menjadi kriteria tahap 2.

### 1. Runtime sesi dan guest Google Meet

Tentukan runtime media pertama, lalu buat satu worker yang memiliki browser, sesi MCP, graf audio, thread LangGraph, dan ruang memori sepanjang meeting. Pilihan runtime perlu mempertimbangkan virtual microphone dan capture audio; macOS pengembangan belum membuktikan jalur deployment Linux.

Definisikan identitas `owner_id`, `twin_id`, `meeting_id`, dan `thread_id`, lalu alur persiapan, lobby, admitted, disconnected, leaving, dan ended. Gunakan state dan persistence resmi LangGraph untuk alur aplikasi. Sediakan stop dari pemilik, deadline lobby, batas durasi, dan cleanup.

Bangun adapter guest: buka link, isi nama twin, atur perangkat, minta masuk, tunggu host, amati penolakan/removal, dan keluar. Admission tetap mengikuti host. Pakai CloakBrowser dan API Playwright/MCP yang tersedia; review modul join Vexa sebelum memutuskan bagian yang layak digunakan ulang.

**Selesai ketika:** satu guest masuk ke meeting uji setelah diterima host, tetap hidup sampai dihentikan, lalu menutup resource. Penolakan, lobby timeout, dan gangguan browser menghasilkan status yang berbeda dan dapat diperiksa. Rinciannya di [rencana runtime sesi](../plans/meeting-runtime.md).

**Kemajuan:** guest masuk meeting uji setelah di-admit host via CLI (`live-qus-2`), monitor sampai batas durasi, leave terverifikasi. Aksi Meet terbukti harus lewat halaman humanized langsung — klik via MCP diblokir server (`CreateMeetingDevice` 403). Ketiga jalur gagal (deny, lobby timeout, stop Ctrl+C) terverifikasi live; keputusan runtime media sudah disetujui. Tahap 1 selesai.

### 2. Sambungan audio dua arah

Sambungkan audio peserta dari Meet ke graf audio dan Muse, lalu output TTS ke microphone yang dipakai Meet. Pilih transport yang terbukti pada runtime tahap 1. Capture, format PCM, sample rate, buffering, dan resampling harus mengikuti kontrak Muse.

Pisahkan suara peserta dari output twin agar twin tidak merespons suaranya sendiri. Bawa timestamp sumber, bedakan transkrip sementara/final, dan pertahankan urutan saat reconnect. Catat waktu capture, transkrip final, dan audio keluar.

**Selesai ketika:** ucapan peserta di Meet menjadi transkrip final, dan peserta lain benar-benar mendengar satu kalimat TTS dari twin. Tidak ada putaran suara sendiri, kebocoran audio antarsesi, atau pengulangan playback setelah reconnect.

**Kemajuan:** transkrip final Muse (3 segmen) dan sapaan TTS yang terdengar peserta terverifikasi live (`audio-bua-1`); tidak ada loopback agent→STT. Tahap 2 selesai.

### 3. Sambungkan percakapan ke LangGraph

Hubungkan event transkrip final ke state meeting, konteks agent, dan retrieval memori bila diperlukan. Hubungkan respons agent ke TTS dan tool meeting. Jalur audio perlu tetap berjalan saat model atau Graphiti sedang bekerja.

Gunakan event yang membawa ID, waktu, sumber, status final, dan participant yang diketahui atau `unknown`. Tambahkan transisi mendengar, menimbang respons, berbicara, disela, menunggu, dan bertindak. Definisikan pembatalan agar respons lama tidak diputar setelah konteks berubah.

Penghubung antara graf audio dan agent LangChain adalah tanggung jawab kita sendiri. Pipecat dan `LangchainProcessor`-nya sudah dilepas, sehingga kontrak pesan, tool, dan thread agent tidak lagi bergantung pada adapter resmi; detailnya ada di [voice foundation](voice.md).

**Selesai ketika:** peserta bertanya di Meet, twin memakai konteks thread yang benar, lalu jawaban terdengar di meeting yang sama. Replay atau reconnect tidak menggandakan aksi eksternal.

**Kemajuan:** adapter tipis native LangGraph, antrean berurutan, buffer sebelum audio siap, penolakan jawaban usang, serta cleanup sudah diuji. Acceptance ketiga membuktikan pertanyaan pertama tertangkap, jawaban terdengar, konteks berlanjut, dan stop tanpa jawaban terlambat. Duplicate delivery dengan ID sama ditangani selama sesi; replay lintas proses dan efek eksternal belum memiliki jaminan exactly-once. Performa masih terbuka. Lihat [kontrak conversation](../plans/conversation.md).

### 4. Persona dan memori pemilik

Profil, prompt, dan retrieval sudah diimplementasikan dan diuji; lihat [persona dan retrieval memori](../plans/persona-memory.md). Ingestion otomatis dipisahkan ke [rencana ingestion dengan provenance](../plans/memory-ingestion.md) agar provenance, koreksi, dan idempotensi dapat diuji sendiri.

Sediakan profil pemilik: identitas, gaya bahasa, tujuan meeting, pengetahuan yang diberikan, dan kewenangan untuk berbicara atau membuat komitmen. Bedakan fakta pemilik, ucapan peserta, dan kesimpulan model.

Sambungkan transkrip final ke Graphiti dengan ID sumber, waktu kejadian, provenance meeting/participant, deduplikasi, koreksi, serta namespace pemilik. Simpan transkrip sementara di konteks aktif, bukan sebagai fakta permanen. Buat pengelolaan sumber pengetahuan pemilik dan kebijakan retensi/penghapusan.

**Selesai ketika:** twin mempertahankan persona, mengingat fakta relevan lintas sesi, dapat menunjukkan asal fakta, dan meminta klarifikasi saat informasi atau kewenangannya kurang. Data pemilik lain tidak ikut terambil.

**Kemajuan:** profil, recall per namespace, penolakan konfigurasi sebelum resource, `infra_failure`, serta recall gagal/timeout sudah diuji. Memori tepercaya digabung (recall terfilter status, koreksi idempoten, 72/72 review independen), lalu prefetch satu trip anti-spoof dengan konteks follow-up dan trim relevansi. Ingestion serta hapus manual sudah diuji live; ingestion sebagai job container terpisah ada di branch worker. Waktu asli owner note yang diinput ulang dan duplikasi masih membutuhkan pengembangan.

### 5. Mata, tangan, dan perilaku percakapan multipihak

Tambahkan observasi participant, active speaker, chat, screenshare, dan perubahan slide. Kirim konteks visual yang relevan ke model multimodal dengan timestamp agar referensi seperti “bagian ini” dapat dikaitkan dengan pembicaraan.

Petakan speaker ke participant hanya jika buktinya cukup. Mode diarization Muse tidak otomatis menghasilkan identitas peserta Meet. Sejak 15 Oktober klien Muse memakai `DIARIZATION` sebagai default dan melampirkan label per giliran (`A`, `B`) sebagai `voice_clusters`, tetapi label itu belum dipetakan ke participant. Simpan `unknown` ketika ambigu. Probe 30 September membuktikan Muse mengirim label per giliran (`A`, `B`) yang dulu dibuang adapter Pipecat 1.12; rencana pemakaiannya ada di [acceptance 30 September](../reports/2026-09-30-acceptance.md#2a-diarisasi-untuk-atribusi-participant).

Susun tindakan Meet yang memeriksa hasilnya: raise/lower hand, chat, mute/unmute, pemilihan perangkat, dan leave. Tambahkan presentasi/screenshare ketika skenario produk memerlukannya.

Gunakan VAD di graf audio (`audio/vad.py`) sebagai dasar. Tambahkan kebijakan meeting untuk mengenali pertanyaan kepada twin, menunggu orang selesai, memilih diam, meminta klarifikasi, dan menghentikan TTS ketika disela.

**Selesai ketika:** twin dapat menjawab pertanyaan tentang slide, tidak menebak nama pembicara yang ambigu, berhenti bicara ketika disela, serta membuktikan tindakan UI yang diminta berhasil.

**Kemajuan:** observer read-only, kebijakan partisipasi, dan identitas speaker wajib di transkrip sudah digabung (identitas speaker di transkrip sekarang berasal dari `VoiceActivityTracker`); twin tidak menyangkal penutur tak dikenal, dengan 0 penyangkalan pada sondase live. Visual slide dan tindakan UI lanjutan belum dikerjakan.

Iterasi addressing sadar lantai sudah masuk: tanpa `--wake-phrase`, twin hanya menjawab transkrip final yang menyebut namanya (kata utuh, dengan jendela aktivasi setelah disebut), dan transkrip final yang tidak ditujukan menghentikan playback bila twin sedang bicara. Rincian dan keputusan produknya ada di [laporan breaking change](../reports/2026-10-14-breaking-changes-hardening.md#7-iterasi-1-addressing-sadar-lantai-digital-twin). Kosakata backchannel Inggris (`yeah`, `mm-hm`, dan sejenisnya) sudah masuk, sehingga tidak menghentikan twin yang sedang bicara. Iterasi 4 menyatukan keputusan addressing di satu fungsi (`choose_participation`) untuk kedua mode. Ack yang berdiri sendiri hanya dijawab bila twin baru bertanya, interjeksi di dalam jendela diam, dan lawan bicara lain yang teratribusi diam (bagian 11 laporan). Belum tersambung: `ambiguous` dan endpoint giliran berbasis model. Gerbang addressee berbasis model adalah langkah berikutnya dan belum dibangun. STT Muse sekarang berada di `transcription/` dengan kontrak `TranscriptUpdate` (bagian 14 laporan). Keepalive silence real-time mencegah sesi ditutup saat audio berhenti. Tes live Muse lulus dengan WAV bahasa Inggris. Iterasi 5 (bagian 12 laporan): satu giliran yang gagal tidak lagi mengakhiri percakapan, barge-in default ikut `--converse`, dan koneksi putus menahan bicara. Latensi jawaban (median sekitar 6,6 detik di live) dan suara orang pertama belum dikerjakan.

### 6. Evaluasi menyeluruh dan kesiapan operasi

Perluas DeepEval dan tes integrasi dengan skenario percakapan, persona, memori, pemilihan tool, speaker ambigu, interupsi, join ditolak, timeout provider, dan koneksi terputus. Bedakan skor kualitas dari pengujian lifecycle dan media.

Ukur latency ujung ke ujung, tingkat keberhasilan join, kualitas transkripsi, penghentian suara, durasi sesi, serta biaya per meeting. Tetapkan target setelah baseline diukur.

Verifikasi PostgreSQL checkpointer dengan database nyata sebelum menjalankan banyak worker; checkpoint kini hanya PostgreSQL, sehingga batas concurrent writer SQLite yang tercatat dalam laporan regresi tidak lagi berlaku. Tambahkan isolasi browser/audio per meeting, pengelolaan resource, observability, kontrol start/stop/status, serta CI yang dieksekusi. API/dashboard dan kalender dibangun sesuai kebutuhan penggunaan setelah satu sesi stabil.

**Selesai ketika:** skenario utama lulus di meeting terkontrol, sesi panjang dan beberapa worker tidak bercampur data/audio, serta restart tidak menggandakan chat atau suara.

**Kemajuan:** Postgres opsional, isolasi profil browser per meeting, lock lease lintas container, kontrol `worker start/status/stop/ingest`, lanjutan crash `--resume`, dan ingestion sebagai job ada di branch worker; acceptance dua meeting paralel menunggu link Meet.

### 7. Representasi visual twin

Setelah media stabil, pilih kebutuhan suara persona dan avatar/video, kemudian validasi provider dan transport kamera virtualnya. Streaming wajah, sinkronisasi bibir, dan presentasi video belum tersedia sekarang.

**Selesai ketika:** peserta lain menerima video yang dimaksud, sinkron dengan suara, dan resource video berhenti saat meeting berakhir. Tahap ini dapat menyusul kemampuan percakapan.

## Library yang dipakai ulang dan kode aplikasi yang diperlukan

Pertahankan LangChain/LangGraph untuk agent dan state, graf audio sendiri untuk suara (dengan `onnxruntime`, `loudness`, `sentencex`, dan `websockets`), Graphiti untuk memori, Playwright/MCP untuk browser, dan DeepEval untuk evaluasi. Baca dokumentasi serta source versi yang dikunci sebelum integrasi baru.

Kode aplikasi tetap diperlukan untuk identitas sesi, alur Google Meet, kontrak event antarlibrary, persona, dan aturan partisipasi. Batasi kode itu pada kebutuhan yang tidak disediakan library. Gunakan dependency injection dan context manager yang sudah ada; hindari membangun ulang agent loop, database memori, atau tool browser generik. Protokol STT dan VAD kini ditulis sendiri setelah Pipecat dilepas; lihat [rencana](../plans/drop-pipecat.md).

Pelajaran dari repository referensi:

- **Vexa:** batas adapter join/capture, atribusi speaker yang mempertahankan `unknown`, serta alasan kegagalan lifecycle.
- **Attendee:** kontrak audio/video, participant events, dan media keluar.
- **Amazon LMA:** kesinambungan timestamp, ukuran frame audio, navigasi, dan pemulihan koneksi.

Ketiganya sudah dipelajari sebagai referensi. Modul-modulnya belum menjadi dependency runtime proyek. Lihat [reference lessons](reference-lessons.md) sebelum memutuskan reuse source atau layanan.

## Target demonstrasi berikutnya

Acceptance terakhir (`lat-rec-1005`, 5 Oktober 2026) mencatat 22 jawaban dengan 0 severe pada review independen dan stop audio pemicu PCM lulus, tetapi gate latency live gagal: akhir ucapan → PCM pertama p50 5,05 detik dan p95 8,66 detik lawan target ≤3/≤6, dengan agent dominan. Konfigurasi live terpilih tetap `gemini-3.8-flash` + prefetch dengan penalaran default. Langkah berikut adalah acceptance dua meeting paralel untuk operasi worker (menunggu dua link Meet), lalu atribusi nama penuh, provenance waktu asli, tindakan Meet yang terverifikasi, backend Kubernetes, dan konteks visual.

Detail task dan batas setiap milestone ada di [rencana pengembangan berikutnya](../plans/next-development.md) dan [rencana operasi worker](../plans/worker-operations.md).
