# Status fondasi dan pekerjaan berikutnya

Diperbarui: 2 Oktober 2026. Status mencakup refactor yang sudah digabung, cleanup lokal, acceptance ketiga, dan acceptance live kinerja percakapan. Rencana pengembangan berikutnya selesai dilaksanakan; langkah lanjut ada di [rencana tersebut](../plans/next-development.md).

## Posisi proyek sekarang

Fondasi komponen sudah berjalan: agent LangChain/LangGraph, checkpoint, browser tools, layanan suara, client memori, dan perangkat evaluasi. Muse sudah menerima audio sintetis melalui layanan live dan menghasilkan transkrip final yang sesuai.

Worker sudah dapat masuk sebagai guest, menunggu admission, mendengar lewat Muse, menjawab melalui TTS streaming, dan leave. [Acceptance ketiga](../reports/2026-10-01-refactor-acceptance.md) membuktikan startup, recall lintas sesi, konteks kata ganti, interupsi, dan stop tanpa suara terlambat; host mengonfirmasi jawaban terdengar utuh. Performa belum lulus: median akhir ucapan → PCM pertama 5,727 detik dan p95 16,895 detik pada delapan giliran. Atribusi per participant belum terbukti karena sesi ini memakai satu host.

Refactor utama sudah digabung ke `master` pada `6926e9b`. Cleanup dan rencana terukur mendarat di `feat/legacy-config-cleanup` (`15faf58`), lalu implementasi kinerja percakapan di `feat/conversation-performance`: bukti PCM pertama antri-interupsi, korelasi model/recall per transkrip, artefak benchmark tersamar, laporan negatif Task 4, dan acceptance live Task 5. Gate lokal hijau (1.463 tes non-live/non-container termasuk browser; Ruff, format, mypy bersih), image terverifikasi cocok dengan source, dan container lulus. Fungsi live lulus pada 4 sesi Meet (recall lintas sesi, kata ganti, 11 interupsi, stop saat model dan audio, tanpa audio susulan); milestone performa belum lulus dengan median 3,243 dtk dan p95 8,319 dtk pada 23 jawaban. Bukti: [acceptance live](../reports/2026-10-02-live-acceptance.md). Branch belum di-push atau digabung.

Target akhirnya tetap peserta yang dapat mendengar, memahami pembicaraan dan visual, mengingat, berbicara, serta memakai UI atas nama pemilik dengan kewenangan yang jelas.

Milestone memori tepercaya berjalan di branch `feat/trustworthy-memory` (belum digabung). Fondasi kontrak selesai: modul bukti `memory_evidence/v1`, owner note dengan koreksi idempoten, recall terfilter status, CLI `memory-add`/`memory-correct`, aturan persona baru, korpus 24 kasus, dan runbook acceptance. Gate offline hijau (1.716 tes; Ruff, format, mypy bersih), dua tes roundtrip live lulus dengan residu nol (diulang independen oleh orkestrator), dan 72 output produksi korpus trust lulus review manusia independen (`trust_gate` True). Klaim diagnosis awal soal protokol UUID terbukti keliru. Acceptance live Meet selesai 2 sesi/24 jawaban: fungsi live terbukti (interupsi bersih, koreksi dihormati), tetapi kualitas trust live gagal (1 sapaan "Sam" tanpa sumber) dan performa gagal (p95 agent ±9 detik lawan anggaran 4 detik). Trade-off recall fuzzy pada replay lama terdokumentasi. Bukti: [close-out memori tepercaya](../reports/2026-10-02-trust-closeout.md), [acceptance live](../reports/2026-10-02-trust-live.md).

## Yang sudah ada dan batasnya

| Bagian | Bukti saat ini | Yang belum selesai |
| --- | --- | --- |
| Setup proyek | Python 3.12, dependency terkunci, lint, typing, build, Compose, dan CI GitHub saat merge refactor sudah diperiksa | Operasi worker terjadwal dan deployment untuk beberapa sesi |
| Otak: LangChain + LangGraph | Agent native dan loop transkrip→agent→sintesis→playback, pemrosesan berurutan, checkpoint per owner/twin/meeting; jawaban dan konteks lanjutan lulus acceptance | Latency, gap kualitas replay, dan kebijakan partisipasi multipihak |
| Memori percakapan | Isolasi `thread_id` dan pembukaan ulang checkpoint SQLite teruji | Pemulihan sesi meeting beserta resource browser/audio; checkpoint PostgreSQL belum dipakai CLI |
| Telinga: Muse + Pipecat | Audio peserta Meet→PulseAudio→Muse menghasilkan transkrip final live; capture 16 kHz mono | Identitas speaker, overlap, dedupe reconnect, dan pengukuran latency per tahap |
| Mulut: TTS streaming + `paplay` | PCM 24 kHz→virtual microphone→Meet; antrean serial, mute saat diam, barge-in; host mengonfirmasi jawaban utuh | Waktu PCM pertama dan performa lintas lebih banyak sesi |
| Tangan: CloakBrowser humanized langsung + Playwright MCP | Join, admission, deny, timeout, stop, dan leave terverifikasi live; tool MCP tersedia untuk agent | Tindakan lanjutan (chat, raise hand, mute) dan sesi agent/browser yang terintegrasi |
| Mata | Tool DOM dan screenshot tersedia | Observasi screenshare/kamera berkelanjutan dan pemahaman visual oleh model multimodal |
| Memori pengetahuan: Graphiti + Neo4j | Isolasi namespace owner/twin, ingestion per saga, catatan pemilik, hapus manual, dan recall `acc2` pada acceptance ketiga teruji; 17 fakta masih berlaku setelah ingestion | Provenance waktu asli, atribusi nama participant, duplikat fakta, dan koreksi/invalidasi yang konsisten |
| Persona | Profil JSON tervalidasi, batas tool, namespace owner/twin, dan jawaban persona pada acceptance ketiga | Gate kualitas replay model aktif serta kewenangan granular untuk tindakan |
| Evaluasi: DeepEval + pytest | Tool trajectory deterministik, wiring persona, dan 5 skenario kualitas persona live (GEval) | Skenario meeting nyata, interupsi, kegagalan media, dan evaluasi memori hasil ingestion |
| Runtime aplikasi | Worker Linux/PulseAudio, CLI `join`, checkpoint lifecycle, stop/deadline, cleanup audio/percakapan | Kontrol jarak jauh, pemulihan crash, penjadwalan, serta antarmuka pengguna |

Sumber implementasi: [agent](../../src/fakeyou/agent.py), [browser](../../src/fakeyou/browser.py), [voice](../../src/fakeyou/voice.py), [memory](../../src/fakeyou/memory.py), dan [CLI](../../src/fakeyou/cli/).

LangGraph menyimpan graph agent dan lifecycle meeting. CLI `chat` menjalankan satu permintaan; CLI `join` memiliki browser sepanjang sesi. Checkpoint tidak mempertahankan resource browser/audio setelah proses mati. Refactor memisahkan konfigurasi, persistence lifecycle, adapter Meet, dan worker; lihat [batas modul dan verifikasi](../reports/2026-09-28-refactor.md).

## Pengujian dan konfigurasi

Hasil pengujian terakhir yang tercatat:

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

Tentukan runtime media pertama, lalu buat satu worker yang memiliki browser, sesi MCP, pipeline Pipecat, thread LangGraph, dan ruang memori sepanjang meeting. Pilihan runtime perlu mempertimbangkan virtual microphone dan capture audio; macOS pengembangan belum membuktikan jalur deployment Linux.

Definisikan identitas `owner_id`, `twin_id`, `meeting_id`, dan `thread_id`, lalu alur persiapan, lobby, admitted, disconnected, leaving, dan ended. Gunakan state dan persistence resmi LangGraph untuk alur aplikasi. Sediakan stop dari pemilik, deadline lobby, batas durasi, dan cleanup.

Bangun adapter guest: buka link, isi nama twin, atur perangkat, minta masuk, tunggu host, amati penolakan/removal, dan keluar. Admission tetap mengikuti host. Pakai CloakBrowser dan API Playwright/MCP yang tersedia; review modul join Vexa sebelum memutuskan bagian yang layak digunakan ulang.

**Selesai ketika:** satu guest masuk ke meeting uji setelah diterima host, tetap hidup sampai dihentikan, lalu menutup resource. Penolakan, lobby timeout, dan gangguan browser menghasilkan status yang berbeda dan dapat diperiksa. Rinciannya di [rencana runtime sesi](../plans/meeting-runtime.md).

**Kemajuan:** guest masuk meeting uji setelah di-admit host via CLI (`live-qus-2`), monitor sampai batas durasi, leave terverifikasi. Aksi Meet terbukti harus lewat halaman humanized langsung — klik via MCP diblokir server (`CreateMeetingDevice` 403). Ketiga jalur gagal (deny, lobby timeout, stop Ctrl+C) terverifikasi live; keputusan runtime media sudah disetujui. Tahap 1 selesai.

### 2. Sambungan audio dua arah

Sambungkan audio peserta dari Meet ke input Pipecat/Muse, lalu output TTS ke microphone yang dipakai Meet. Pilih transport yang terbukti pada runtime tahap 1. Capture, format PCM, sample rate, buffering, dan resampling harus mengikuti kontrak library.

Pisahkan suara peserta dari output twin agar twin tidak merespons suaranya sendiri. Bawa timestamp sumber, bedakan transkrip sementara/final, dan pertahankan urutan saat reconnect. Catat waktu capture, transkrip final, dan audio keluar.

**Selesai ketika:** ucapan peserta di Meet menjadi transkrip final, dan peserta lain benar-benar mendengar satu kalimat TTS dari twin. Tidak ada putaran suara sendiri, kebocoran audio antarsesi, atau pengulangan playback setelah reconnect.

**Kemajuan:** transkrip final Muse (3 segmen) dan sapaan TTS yang terdengar peserta terverifikasi live (`audio-bua-1`); tidak ada loopback agent→STT. Tahap 2 selesai.

### 3. Sambungkan percakapan ke LangGraph

Hubungkan event transkrip final ke state meeting, konteks agent, dan retrieval memori bila diperlukan. Hubungkan respons agent ke TTS dan tool meeting. Jalur audio perlu tetap berjalan saat model atau Graphiti sedang bekerja.

Gunakan event yang membawa ID, waktu, sumber, status final, dan participant yang diketahui atau `unknown`. Tambahkan transisi mendengar, menimbang respons, berbicara, disela, menunggu, dan bertindak. Definisikan pembatalan agar respons lama tidak diputar setelah konteks berubah.

Pipecat `LangchainProcessor` versi terpasang belum meneruskan seluruh kontrak pesan, tool, dan thread agent kita. Periksa adapter resmi sebelum menulis penghubung tipis untuk kontrak yang belum tercakup; detailnya ada di [voice foundation](voice.md).

**Selesai ketika:** peserta bertanya di Meet, twin memakai konteks thread yang benar, lalu jawaban terdengar di meeting yang sama. Replay atau reconnect tidak menggandakan aksi eksternal.

**Kemajuan:** adapter tipis native LangGraph, antrean berurutan, buffer sebelum audio siap, penolakan jawaban usang, serta cleanup sudah diuji. Acceptance ketiga membuktikan pertanyaan pertama tertangkap, jawaban terdengar, konteks berlanjut, dan stop tanpa jawaban terlambat. Duplicate delivery dengan ID sama ditangani selama sesi; replay lintas proses dan efek eksternal belum memiliki jaminan exactly-once. Performa masih terbuka. Lihat [kontrak conversation](../plans/conversation.md).

### 4. Persona dan memori pemilik

Profil, prompt, dan retrieval sudah diimplementasikan dan diuji; lihat [persona dan retrieval memori](../plans/persona-memory.md). Ingestion otomatis dipisahkan ke [rencana ingestion dengan provenance](../plans/memory-ingestion.md) agar provenance, koreksi, dan idempotensi dapat diuji sendiri.

Sediakan profil pemilik: identitas, gaya bahasa, tujuan meeting, pengetahuan yang diberikan, dan kewenangan untuk berbicara atau membuat komitmen. Bedakan fakta pemilik, ucapan peserta, dan kesimpulan model.

Sambungkan transkrip final ke Graphiti dengan ID sumber, waktu kejadian, provenance meeting/participant, deduplikasi, koreksi, serta namespace pemilik. Simpan transkrip sementara di konteks aktif, bukan sebagai fakta permanen. Buat pengelolaan sumber pengetahuan pemilik dan kebijakan retensi/penghapusan.

**Selesai ketika:** twin mempertahankan persona, mengingat fakta relevan lintas sesi, dapat menunjukkan asal fakta, dan meminta klarifikasi saat informasi atau kewenangannya kurang. Data pemilik lain tidak ikut terambil.

**Kemajuan:** profil, recall per namespace, penolakan konfigurasi sebelum resource, `infra_failure`, serta recall gagal/timeout sudah diuji. Acceptance ketiga memakai owner note dan memori meeting terdahulu dalam jawaban yang terdengar. Ingestion serta hapus manual sudah diuji live; retensi tanpa batas mengikuti keputusan pengguna. Waktu asli owner note yang diinput ulang, konsistensi atribusi, duplikasi, dan koreksi/invalidasi memori masih membutuhkan pengembangan.

### 5. Mata, tangan, dan perilaku percakapan multipihak

Tambahkan observasi participant, active speaker, chat, screenshare, dan perubahan slide. Kirim konteks visual yang relevan ke model multimodal dengan timestamp agar referensi seperti “bagian ini” dapat dikaitkan dengan pembicaraan.

Petakan speaker ke participant hanya jika buktinya cukup. Mode diarization Muse tidak otomatis menghasilkan identitas peserta Meet; adapter Pipecat saat ini juga belum memetakan event speaker ke identitas peserta pada transkrip. Simpan `unknown` ketika ambigu. Probe 30 September membuktikan Muse mengirim label per giliran (`A`, `B`) yang dibuang adapter Pipecat 1.12; rencana pemakaiannya ada di [acceptance 30 September](../reports/2026-09-30-acceptance.md#2a-diarisasi-untuk-atribusi-participant).

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

Percakapan persona dan recall dua sesi dengan 23 jawaban terukur sudah dilaksanakan pada 2 Oktober 2026: median akhir ucapan → PCM pertama 3,243 detik dan p95 8,319 detik, sehingga target 3/6 detik belum tercapai; gate kualitas replay juga belum lulus. Interupsi serta stop lulus (11 pemicu dalam batas pembanding, tanpa audio susulan). Langkah berikut adalah desain streaming teks per kalimat sebelum perubahan alur, lalu atribusi participant, provenance memori, tindakan Meet yang terverifikasi, operasi beberapa sesi, dan konteks visual.

Setelah performa percakapan, lanjutkan atribusi participant, provenance memori, tindakan Meet yang terverifikasi, operasi beberapa sesi, dan konteks visual. Detail task dan batas setiap milestone ada di [rencana pengembangan berikutnya](../plans/next-development.md).
