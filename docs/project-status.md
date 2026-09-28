# Status fondasi dan pekerjaan berikutnya

Diperbarui: 28 September 2026. Status mencakup fondasi, guest runtime, verifikasi provider, dan refactor. Bukti historis live dipisahkan dari pengujian regresi terbaru.

## Posisi proyek sekarang

Fondasi komponen sudah berjalan: agent LangChain/LangGraph, checkpoint, browser tools, layanan suara, client memori, dan perangkat evaluasi. Muse sudah menerima audio sintetis melalui layanan live dan menghasilkan transkrip final yang sesuai.

Worker sudah dapat masuk sebagai guest, menunggu admission, memantau sesi, dan leave. Digital twin belum mendengar atau berbicara di meeting: sambungan audio, percakapan, serta perilaku peserta menjadi pekerjaan berikutnya.

Target akhirnya tetap peserta yang dapat mendengar, memahami pembicaraan dan visual, mengingat, berbicara, serta memakai UI atas nama pemilik dengan kewenangan yang jelas.

## Yang sudah ada dan batasnya

| Bagian | Bukti saat ini | Yang belum selesai |
| --- | --- | --- |
| Setup proyek | Python 3.12, dependency terkunci, lint, typing, build, dan Compose sudah diperiksa | Deployment worker meeting dan CI yang benar-benar berjalan di GitHub |
| Otak: LangChain + LangGraph | Agent native, persona/tools/checkpoint, tool loop deterministik, serta respons dan tool call OpenAI live | Percakapan dari transkrip meeting dan graph perilaku partisipasi |
| Memori percakapan | Isolasi `thread_id` dan pembukaan ulang checkpoint SQLite teruji | Pemulihan sesi meeting beserta resource browser/audio; checkpoint PostgreSQL belum dipakai CLI |
| Telinga: Muse + Pipecat | Uji live WAV sintetis Bahasa Indonesia, mono PCM 16-bit 24 kHz, lulus | Capture audio Google Meet, percakapan multipihak, overlap, reconnect, dan pengukuran latency per tahap |
| Mulut: TTS Pipecat | `OpenAITTSService` menghasilkan frame audio dalam uji live | Playback ke microphone browser dan stop saat disela |
| Tangan: CloakBrowser humanized langsung + Playwright MCP | Join, admission, deny, timeout, stop, dan leave terverifikasi live; tool MCP tersedia untuk agent | Tindakan lanjutan (chat, raise hand, mute) dan sesi agent/browser yang terintegrasi |
| Mata | Tool DOM dan screenshot tersedia | Observasi screenshare/kamera berkelanjutan dan pemahaman visual oleh model multimodal |
| Memori pengetahuan: Graphiti + Neo4j | Tools scoped, schema, cleanup, serta ingestion dan retrieval fakta sintetis setelah client dibuka ulang teruji live | Ingestion transkrip otomatis dengan provenance dan deduplikasi |
| Persona | Agent menerima string persona; ada prompt default | Profil pemilik, gaya bicara, tujuan, pengetahuan, serta batas kewenangan per twin |
| Evaluasi: DeepEval + pytest | Tes perilaku lokal dan satu tool trajectory deterministik tersedia | Skenario meeting nyata, kualitas jawaban/persona, relevansi memori, interupsi, dan kegagalan media |
| Runtime aplikasi | CLI `join` foreground, checkpoint lifecycle, validasi sesi, stop/deadline, dan cleanup | Audio worker, kontrol jarak jauh, pemulihan crash, penjadwalan, serta antarmuka pengguna |

Sumber implementasi: [agent](../src/fakeyou/agent.py), [browser](../src/fakeyou/browser.py), [voice](../src/fakeyou/voice.py), [memory](../src/fakeyou/memory.py), dan [CLI](../src/fakeyou/cli.py).

LangGraph menyimpan graph agent dan lifecycle meeting. CLI `chat` menjalankan satu permintaan; CLI `join` memiliki browser sepanjang sesi. Checkpoint tidak mempertahankan resource browser/audio setelah proses mati. Refactor memisahkan konfigurasi, persistence lifecycle, adapter Meet, dan worker; lihat [batas modul dan verifikasi](refactor-2026-09-28.md).

## Pengujian dan konfigurasi

Hasil pengujian terakhir yang tercatat:

- Hasil regresi refactor terbaru tersedia di [laporan refactor](refactor-2026-09-28.md).
- 5 tes browser asli lulus, termasuk join sampai leave pada fixture lokal dengan checkpoint SQLite asli.
- 3 tes provider live lulus: agent/tool call, TTS, dan Graphiti reopen/search. Lihat [laporan provider](provider-verification-2026-09-28.md).
- 1 tes Muse live lulus; fixture audio sepanjang 9,076 detik. Run setelah penyederhanaan nama konfigurasi selesai dalam 11,17 detik, termasuk startup dan streaming. Angka ini bukan latency percakapan.
- 1 evaluasi DeepEval lulus dengan Tool Correctness 1.0 dan respons model terprogram. Tool `raise_hand` di evaluasi adalah test double, bukan tindakan di Google Meet.
- Ruff, format, mypy, build, resolusi dependency, dan konfigurasi Compose lulus.

Lihat [verifikasi](verification.md) dan [laporan regresi](regression-2026-09-27.md) untuk cakupan dan keterbatasan. Pemeriksaan status ini tidak mengulang panggilan provider berbayar.

`FAKEYOU_OPENAI_API_KEY` dan `FAKEYOU_MODEL_API_KEY` sudah terbaca oleh settings. `FAKEYOU_MODEL_API_KEY` dipakai integrasi Muse saat ini; nama generik tidak berarti adapter STT otomatis mendukung semua provider. OpenAI masih menjadi provider yang disusun untuk penalaran, komponen Graphiti, dan TTS.

Perintah `fakeyou check` memeriksa instalasi dan keberadaan key. Field `live_services_verified` saat ini selalu `false`; command itu tidak menguji API atau membaca hasil pytest. Bukti Muse live berasal dari tes tersendiri.

## Pekerjaan yang perlu dibangun

### 0. Lengkapi verifikasi provider

Uji penalaran dan tool call OpenAI dengan prompt sintetis, TTS dengan kalimat singkat, serta ingestion dan retrieval Graphiti pada namespace pengujian. Periksa hasil setelah client dibuka ulang agar yang diuji adalah penyimpanan sebenarnya.

**Selesai ketika:** model memberi respons dan menjalankan tool yang sesuai, TTS menghasilkan audio yang dapat diputar, serta Graphiti mengembalikan fakta uji dari penyimpanan. Key yang terbaca saja belum memenuhi kriteria ini.

**Kemajuan:** ketiga jalur lulus uji live sintetis. Audio TTS terbukti sebagai frame PCM; pendengaran oleh peserta Meet masih menjadi kriteria tahap 2.

### 1. Runtime sesi dan guest Google Meet

Tentukan runtime media pertama, lalu buat satu worker yang memiliki browser, sesi MCP, pipeline Pipecat, thread LangGraph, dan ruang memori sepanjang meeting. Pilihan runtime perlu mempertimbangkan virtual microphone dan capture audio; macOS pengembangan belum membuktikan jalur deployment Linux.

Definisikan identitas `owner_id`, `twin_id`, `meeting_id`, dan `thread_id`, lalu alur persiapan, lobby, admitted, disconnected, leaving, dan ended. Gunakan state dan persistence resmi LangGraph untuk alur aplikasi. Sediakan stop dari pemilik, deadline lobby, batas durasi, dan cleanup.

Bangun adapter guest: buka link, isi nama twin, atur perangkat, minta masuk, tunggu host, amati penolakan/removal, dan keluar. Admission tetap mengikuti host. Pakai CloakBrowser dan API Playwright/MCP yang tersedia; review modul join Vexa sebelum memutuskan bagian yang layak digunakan ulang.

**Selesai ketika:** satu guest masuk ke meeting uji setelah diterima host, tetap hidup sampai dihentikan, lalu menutup resource. Penolakan, lobby timeout, dan gangguan browser menghasilkan status yang berbeda dan dapat diperiksa. Rinciannya di [rencana runtime sesi](meeting-runtime-plan.md).

**Kemajuan:** guest masuk meeting uji setelah di-admit host via CLI (`live-qus-2`), monitor sampai batas durasi, leave terverifikasi. Aksi Meet terbukti harus lewat halaman humanized langsung — klik via MCP diblokir server (`CreateMeetingDevice` 403). Ketiga jalur gagal (deny, lobby timeout, stop Ctrl+C) terverifikasi live; keputusan runtime media sudah disetujui. Tahap 1 selesai.

### 2. Sambungan audio dua arah

Sambungkan audio peserta dari Meet ke input Pipecat/Muse, lalu output TTS ke microphone yang dipakai Meet. Pilih transport yang terbukti pada runtime tahap 1. Capture, format PCM, sample rate, buffering, dan resampling harus mengikuti kontrak library.

Pisahkan suara peserta dari output twin agar twin tidak merespons suaranya sendiri. Bawa timestamp sumber, bedakan transkrip sementara/final, dan pertahankan urutan saat reconnect. Catat waktu capture, transkrip final, dan audio keluar.

**Selesai ketika:** ucapan peserta di Meet menjadi transkrip final, dan peserta lain benar-benar mendengar satu kalimat TTS dari twin. Tidak ada putaran suara sendiri, kebocoran audio antarsesi, atau pengulangan playback setelah reconnect.

### 3. Sambungkan percakapan ke LangGraph

Hubungkan event transkrip final ke state meeting, konteks agent, dan retrieval memori bila diperlukan. Hubungkan respons agent ke TTS dan tool meeting. Jalur audio perlu tetap berjalan saat model atau Graphiti sedang bekerja.

Gunakan event yang membawa ID, waktu, sumber, status final, dan participant yang diketahui atau `unknown`. Tambahkan transisi mendengar, menimbang respons, berbicara, disela, menunggu, dan bertindak. Definisikan pembatalan agar respons lama tidak diputar setelah konteks berubah.

Pipecat `LangchainProcessor` versi terpasang belum meneruskan seluruh kontrak pesan, tool, dan thread agent kita. Periksa adapter resmi sebelum menulis penghubung tipis untuk kontrak yang belum tercakup; detailnya ada di [voice foundation](voice.md).

**Selesai ketika:** peserta bertanya di Meet, twin memakai konteks thread yang benar, lalu jawaban terdengar di meeting yang sama. Replay atau reconnect tidak menggandakan aksi eksternal.

### 4. Persona dan memori pemilik

Sediakan profil pemilik: identitas, gaya bahasa, tujuan meeting, pengetahuan yang diberikan, dan kewenangan untuk berbicara atau membuat komitmen. Bedakan fakta pemilik, ucapan peserta, dan kesimpulan model.

Sambungkan transkrip final ke Graphiti dengan ID sumber, waktu kejadian, provenance meeting/participant, deduplikasi, koreksi, serta namespace pemilik. Simpan transkrip sementara di konteks aktif, bukan sebagai fakta permanen. Buat pengelolaan sumber pengetahuan pemilik dan kebijakan retensi/penghapusan.

**Selesai ketika:** twin mempertahankan persona, mengingat fakta relevan lintas sesi, dapat menunjukkan asal fakta, dan meminta klarifikasi saat informasi atau kewenangannya kurang. Data pemilik lain tidak ikut terambil.

### 5. Mata, tangan, dan perilaku percakapan multipihak

Tambahkan observasi participant, active speaker, chat, screenshare, dan perubahan slide. Kirim konteks visual yang relevan ke model multimodal dengan timestamp agar referensi seperti “bagian ini” dapat dikaitkan dengan pembicaraan.

Petakan speaker ke participant hanya jika buktinya cukup. Mode diarization Muse tidak otomatis menghasilkan identitas peserta Meet; adapter Pipecat saat ini juga belum memetakan event speaker ke identitas peserta pada transkrip. Simpan `unknown` ketika ambigu.

Susun tindakan Meet yang memeriksa hasilnya: raise/lower hand, chat, mute/unmute, pemilihan perangkat, dan leave. Tambahkan presentasi/screenshare ketika skenario produk memerlukannya.

Gunakan VAD dan strategi giliran Pipecat sebagai dasar. Tambahkan kebijakan meeting untuk mengenali pertanyaan kepada twin, menunggu orang selesai, memilih diam, meminta klarifikasi, dan menghentikan TTS ketika disela.

**Selesai ketika:** twin dapat menjawab pertanyaan tentang slide, tidak menebak nama pembicara yang ambigu, berhenti bicara ketika disela, serta membuktikan tindakan UI yang diminta berhasil.

### 6. Evaluasi menyeluruh dan kesiapan operasi

Perluas DeepEval dan tes integrasi dengan skenario percakapan, persona, memori, pemilihan tool, speaker ambigu, interupsi, join ditolak, timeout provider, dan koneksi terputus. Bedakan skor kualitas dari pengujian lifecycle dan media.

Ukur latency ujung ke ujung, tingkat keberhasilan join, kualitas transkripsi, penghentian suara, durasi sesi, serta biaya per meeting. Tetapkan target setelah baseline diukur.

Hubungkan PostgreSQL checkpointer sebelum menjalankan banyak worker; SQLite CLI saat ini memiliki batas concurrent writer yang tercatat dalam laporan regresi. Tambahkan isolasi browser/audio per meeting, pengelolaan resource, observability, kontrol start/stop/status, serta CI yang dieksekusi. API/dashboard dan kalender dibangun sesuai kebutuhan penggunaan setelah satu sesi stabil.

**Selesai ketika:** skenario utama lulus di meeting terkontrol, sesi panjang dan beberapa worker tidak bercampur data/audio, serta restart tidak menggandakan chat atau suara.

### 7. Representasi visual twin

Setelah media stabil, pilih kebutuhan suara persona dan avatar/video, kemudian validasi provider dan transport kamera virtualnya. Streaming wajah, sinkronisasi bibir, dan presentasi video belum tersedia sekarang.

**Selesai ketika:** peserta lain menerima video yang dimaksud, sinkron dengan suara, dan resource video berhenti saat meeting berakhir. Tahap ini dapat menyusul kemampuan percakapan.

## Library yang dipakai ulang dan kode aplikasi yang diperlukan

Pertahankan LangChain/LangGraph untuk agent dan state, Pipecat untuk pipeline suara, Graphiti untuk memori, Playwright/MCP untuk browser, dan DeepEval untuk evaluasi. Baca dokumentasi serta source versi yang dikunci sebelum integrasi baru.

Kode aplikasi tetap diperlukan untuk identitas sesi, alur Google Meet, kontrak event antarlibrary, persona, dan aturan partisipasi. Batasi kode itu pada kebutuhan yang tidak disediakan library. Gunakan dependency injection dan context manager yang sudah ada; hindari membangun ulang agent loop, protokol STT, VAD, database memori, atau tool browser generik.

Pelajaran dari repository referensi:

- **Vexa:** batas adapter join/capture, atribusi speaker yang mempertahankan `unknown`, serta alasan kegagalan lifecycle.
- **Attendee:** kontrak audio/video, participant events, dan media keluar.
- **Amazon LMA:** kesinambungan timestamp, ukuran frame audio, navigasi, dan pemulihan koneksi.

Ketiganya sudah dipelajari sebagai referensi. Modul-modulnya belum menjadi dependency runtime proyek. Lihat [reference lessons](reference-lessons.md) sebelum memutuskan reuse source atau layanan.

## Target demonstrasi berikutnya

Satu guest masuk dengan nama twin, mendengar pertanyaan peserta, menghasilkan jawaban sesuai persona dasar, dan memperdengarkan jawabannya di Google Meet. Uji juga satu tindakan seperti raise hand, interupsi oleh peserta, serta keluar dan cleanup.

Urutan dependensinya: verifikasi provider → runtime dan guest join → audio dua arah → percakapan LangGraph → persona/memori dan observasi/tindakan → operasi beberapa sesi. Evaluasi ditambahkan pada setiap tahap; avatar menyusul setelah jalur media stabil.
