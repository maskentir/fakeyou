# Rencana jangka panjang: memori, suara, dan bukti kualitas

Disusun 30 September 2026 dari seluruh temuan persona, ingestion, dan [acceptance Meet](acceptance-2026-09-30.md). Tujuannya menghentikan pola "lulus tes lalu gagal di live" dengan memperbaiki penyebab yang berulang, bukan setiap gejala.

## Pola penyebab

Masalah yang muncul sejauh ini jatuh ke enam pola. Setiap pola sudah muncul lebih dari sekali.

| Pola | Kejadian |
| --- | --- |
| **A. Perilaku hanya dikendalikan prosa prompt** | Recall tidak dipanggil; `display_name` dibaca sebagai owner; "Rani bilang…" untuk ucapan twin; tawaran follow-up 8/8 di live; asal fakta tidak disebut |
| **B. Eval tidak mereplikasi kondisi live** | Eval satu giliran lulus, thread live gagal; konteks recall sintetis; satu sampel per skenario sehingga skor goyang di sekitar threshold |
| **C. Default library dan perilaku tak terdokumentasi dianggap benar** | Model Graphiti ternyata `gpt-5.5`; `execute_query` me-retry 30 detik; Pipecat membuang label speaker; `add_episode(uuid=...)` bukan create idempoten; dokumentasi bulk berbeda dari source; `keywords` STT tidak diteruskan; host preflight model di-hardcode `api.openai.com` walaupun `FAKEYOU_CHAT_MODEL` dapat diganti |
| **D. Data turunan diperlakukan sebagai data utama** | Episode Graphiti dibuat sekali di dalam proses meeting; ucapan hilang saat drain, fakta pertanyaan, entity terpecah, dan fakta tanpa atribusi tertanam permanen tanpa cara memproses ulang |
| **E. Pekerjaan lambat terikat umur proses meeting** | Ingestion 3–11 detik per episode bersaing dengan stop, drain 20 detik, dan `stop_grace_period`; entrypoint lama bahkan membunuhnya |
| **F. Bukti dikumpulkan ad hoc** | Timestamp playback harus ditambah dari host; latency dihitung dengan script sementara; hasil recall tidak tercatat; bug entrypoint dan command README baru terlihat saat live |

## Keputusan jangka panjang

### 1. Catatan meeting sebagai sumber kebenaran; Graphiti sebagai indeks turunan (pola D, E)

Selama meeting, proses hanya menulis **catatan meeting** append-only per `meeting_id`: transkrip final (id, waktu, teks, label speaker), ucapan twin `speak_finished`, dan hasil tool (recall). Menulis satu baris JSONL murah dan tidak bergantung pada provider.

Memori Graphiti diturunkan dari catatan itu oleh command `fakeyou memory-ingest`:

- Jendela ditentukan secara deterministik dari catatan (lintas speaker, 20 ucapan atau 60 detik), sehingga menjalankan ulang menghasilkan jendela yang sama.
- Nama episode `<meeting_id>:w<index>`. Saga meeting berfungsi sebagai offset: ingestion membaca episode terakhir saga (`retrieve_episodes(saga=..., last_n=1)`) lalu melanjutkan jendela berikutnya. Tidak ada ledger kedua.
- `join` menjalankannya setelah leave secara best-effort. Bila proses dihentikan paksa, catatan tetap ada dan command yang sama dapat melanjutkan. Worker Kubernetes nanti dapat menjalankannya sebagai job terpisah.
- Perbaikan instruksi ekstraksi tidak lagi hanya berlaku untuk meeting berikutnya: `memory-forget --meeting` lalu `memory-ingest --meeting` memproses ulang dari catatan.
- `memory-forget --meeting` juga menghapus catatan meeting.

Konteks meeting yang sedang berjalan tetap berasal dari thread LangGraph, yang sudah memuat semua transkrip. Graphiti hanya diperlukan untuk meeting sebelumnya dan catatan pemilik, sehingga ingestion setelah meeting tidak mengurangi kemampuan twin di meeting yang sama.

Trade-off: transkrip mentah tersimpan di disk sampai dihapus manual, sesuai keputusan retensi pengguna. Fakta dari meeting yang sedang berjalan baru masuk Graphiti setelah meeting selesai.

### 2. Perilaku penting diukur, bukan hanya diminta (pola A)

- Aturan yang dapat diperiksa mesin dicatat sebagai metrik runtime di journal: jumlah kalimat, pertanyaan penutup, tool dipanggil atau tidak, dan ejaan istilah `vocabulary`.
- Aturan penutup dipindah ke akhir system prompt; probe menunjukkan 6/6 menjadi 0/6.
- Bila metrik penutup tetap tinggi di live, jalur berikutnya adalah structured output native `create_agent(response_format=...)` (`ToolStrategy`/`ProviderStrategy` tersedia di LangChain 1.4.2) dengan field jawaban dan pertanyaan konfirmasi terpisah, sehingga kode yang memutuskan apa yang diucapkan.
- Provenance dibawa di data (label sumber dan waktu di hasil recall, atribusi speaker di fakta), bukan disimpulkan model.

### 3. Eval replay dari sesi nyata (pola B, F)

Setiap sesi live menghasilkan catatan meeting yang dapat diputar ulang:

- `evals/replay` memutar transkrip participant dari catatan ke agent saat ini dalam satu thread, memakai hasil recall yang tercatat agar tidak memerlukan Graphiti, lalu menjalankan pemeriksaan deterministik dan GEval.
- Skor kualitas memakai beberapa sampel per skenario dan pass rate, bukan satu sampel melawan threshold 0,7.
- Transkrip `acc-0930-s1` dan `acc-0930-s2` dari log hari ini menjadi fixture pertama. Hasil recall sesi itu tidak tercatat, sehingga fixture ini memakai recall terhadap memori demo saat ini; fixture berikutnya berasal dari catatan meeting lengkap. Regresi seperti tawaran follow-up akan gagal di replay sebelum live.

### 4. Konfigurasi eksplisit dan tes kontrak library (pola C)

- Semua pilihan model dan provider dinyatakan di `Settings` atau profil, bukan diambil dari default library: model LLM dan small model Graphiti, embedder, mode/keyword/sample rate STT, model dan voice TTS. `fakeyou check` mencetaknya.
- Tes kontrak kecil untuk setiap perilaku library yang kita andalkan: label `speaker` Muse ditangkap oleh `ManagedMuseSTT`, `NodeNotFoundError` bukan `ValueError`, saga menyimpan urutan, `verify_connectivity` gagal cepat, filter `is_null` diterapkan. Upgrade versi terkunci wajib melewati tes ini; [sumber library](library-sources.md) mencatat checklist upgrade.

### 4a. Model per peran lewat LangChain

`init_chat_model` dan `ChatOpenAI` membuat penggantian model cukup lewat konfigurasi. Meta Model API kompatibel OpenAI (`https://api.meta.ai/v1`), sehingga Muse Spark dapat dipakai dengan `langchain-openai` yang sudah terpasang dan key `FAKEYOU_MODEL_API_KEY`. Namun perilaku dan latency berbeda jauh, jadi pilihan model adalah keputusan terukur per peran, bukan satu model untuk semua.

Replay enam giliran sesi 1 dalam satu thread (30 September):

| Model | Latency median / maks | Pertanyaan penutup, prompt sekarang | Pertanyaan penutup, aturan di akhir |
| --- | --- | --- | --- |
| `gpt-4.1-mini` | 0,8–1,7 s / 3,0 s | 6/6 | 1/6 |
| `muse-spark-1.3` default | 11,5–16,2 s / 23,4 s | 1/6 (klarifikasi sah) | 0/6 |
| `muse-spark-1.3` `reasoning_effort=minimal` | 3,3 s / 11,5 s | — | 0/6 |
| `muse-spark-1.2` `reasoning_effort=minimal` | 3,6 s / 6,9 s | — | 0/6 |

Semua model memanggil recall untuk pertanyaan demo. Muse lebih patuh gaya dan lebih berhati-hati soal kewenangan, tetapi 2–4 kali lebih lambat pada effort terendah.

- **Jawaban lisan:** model tercepat yang lulus eval replay dalam anggaran latency. Saat ini `gpt-4.1-mini` dengan aturan penutup di akhir prompt.
- **Pekerjaan latar** (ekstraksi Graphiti, ringkasan setelah meeting, laporan): kualitas lebih penting daripada latency; model dan provider Graphiti dinyatakan eksplisit dan dapat memakai Muse lewat `base_url`.
- **Ketahanan provider:** `ModelFallbackMiddleware` LangChain memindahkan jawaban ke model cadangan saat provider utama gagal, misalnya OpenAI ke Muse `reasoning_effort=minimal`, sehingga meeting tidak berakhir karena satu provider.
- Setiap peran menyatakan model, provider/`base_url`, dan parameter provider seperti `reasoning_effort`. Host preflight diturunkan dari konfigurasi itu.
- Mengganti model wajib melewati eval replay dan anggaran latency yang sama.

### 5. Laporan sesi standar (pola F)

- Semua event runtime, termasuk playback dan hasil tool, masuk journal dengan `at`, sehingga tidak ada event tanpa waktu di stdout.
- `fakeyou report --meeting ID` menghitung latency per tahap (VAD stop → final → model → PCM pertama), interupsi, stop, ingestion, fault, dan tawaran penutup dari journal dan catatan meeting. Laporan acceptance tidak lagi bergantung pada script sementara.
- Tes container menjalankan stop bersinyal terhadap image asli, dan tes dokumentasi memastikan setiap command `fakeyou …` di README diterima parser.

### 6. Identitas speaker berlapis (pola D, A)

1. Sekarang: label diarisasi Muse (`A`, `B`) ditangkap `ManagedMuseSTT` dan dicakup per meeting (`Participant A of <meeting_id>`). Latency mode `DIARIZATION` diukur terlebih dahulu.
2. Jangka panjang: audio per participant dari halaman Meet dengan korelasi tile aktif dan batas keyakinan, mengikuti pelajaran Vexa di [reference lessons](reference-lessons.md). Label diarisasi menjadi fallback.
3. Selalu `unknown` bila keyakinan rendah; label speaker membawa sumber keyakinannya ke catatan meeting.

### 7. Hot path yang tahan meeting panjang

- Latency model per giliran perlu diukur terhadap panjang thread. Bila naik, gunakan `SummarizationMiddleware` LangChain (trigger berdasarkan ukuran konteks) atau model dengan konteks panjang; prompt caching provider membantu karena system prompt dan awal thread tetap.
- Barge-in berbasis transkrip interim memakai `MinWordsUserTurnStartStrategy` Pipecat yang diberi status bicara dari `PlaybackEngine`. Playback tetap di luar pipeline karena TTS di dalam pipeline sebelumnya patah di live Meet ([voice](voice.md)).

## Status fase 1 (30 September 2026)

Selesai di branch `feat/memory-ingestion`:

- Catatan meeting `MeetingRecord` (`src/fakeyou/record.py`): transkrip final, ucapan twin `speak_finished`, dan hasil tool, append-only per `meeting_id`, dibaca kembali dengan `TypeAdapter` bertipe diskriminan.
- Journal lengkap: `speech_started`/`speech_stopped` dari VAD, `transcript_final`, `conversation_transition`, event playback, `tool_finished`/`tool_failed` (callback LangChain), dan `session_result`, semuanya dengan `at`.
- `fakeyou report --meeting ID`. Divalidasi pada log `acc-0930-s1` yang dikonversi: angka per giliran sama dengan perhitungan manual di [acceptance](acceptance-2026-09-30.md).
- Model per peran di `Settings` (`FAKEYOU_CHAT_*`, `FAKEYOU_MEMORY_*`, `FAKEYOU_STT_MODEL`), `create_chat_model`, host preflight dari endpoint chat, dan bagian `models` di `fakeyou check`. Tes `.env.example` menjaga nilai contoh tetap sama dengan default.
- Tes kontrak library (`tests/test_library_contracts.py`, termasuk bukti Pipecat 1.12 membuang label diarisasi), tes command dokumentasi (`tests/test_documented_commands.py`), dan tes stop bersinyal pada image worker asli (`tests/test_worker_image.py`, marker `container`).

## Status fase 2 (30 September 2026)

Selesai di branch `feat/memory-ingestion`:

- Ingestion di dalam proses meeting dilepas. `ingest_meeting` (`src/fakeyou/ingestion.py`) membaca catatan meeting, menyusun jendela lintas speaker secara deterministik (20 ucapan atau 60 detik), dan menulis episode `<meeting_id>:w<index>`. Episode terakhir saga menjadi penanda lanjutan; jendela yang gagal atau timeout menghentikan proses agar urutan tetap utuh, dan stop bersinyal berhenti di antara jendela.
- `join` menjalankan ingestion setelah `session_result`; `fakeyou memory-ingest --meeting ID` melanjutkan atau memproses ulang.
- `memory-forget --meeting` menghapus episode, catatan meeting, journal (termasuk rotasi), dan thread percakapan (`AsyncSqliteSaver.adelete_thread`); `--keep-record` hanya menghapus episode agar meeting dapat diingest ulang.
- `vocabulary` di profil: dikirim sebagai keyword Muse bersama nama twin, dan dipakai instruksi ekstraksi untuk ejaan.
- Instruksi ekstraksi mewajibkan speaker sebagai subjek fakta dan melarang fakta dari pertanyaan atau tawaran. Recall menghapus teks fakta kembar sebelum membatasi lima hasil.

Bukti:

- Replay data nyata `acc-0930-s1` (15 entri catatan dari log live): ingestion lama 13 episode (±100 detik, 3 kosong, 4 fakta pertanyaan); ingestion baru 2 episode (24,7 detik, 0 kosong). Dengan instruksi final lewat CLI: 0 fakta pertanyaan, 9/9 fakta menyebut speaker, dan "kick off vendor or chill" menjadi "kickoff vendor Orchid". "Old Shield" tetap tidak dinormalkan karena bunyinya terlalu jauh. Proses ulang (`--keep-record` lalu `memory-ingest`) dan penghapusan penuh diverifikasi; namespace replay kembali 0 entity.
- Tes live Graphiti: 3 ucapan lintas speaker menjadi 1 episode dalam 12,9 detik dengan 3 fakta; ingest kedua menambah 0 episode; owner note muncul pertama; node grup kembali 0 setelah forget.
- Aturan prompt "gunakan ejaan vocabulary" diuji dan dicabut: 0/18 probe untuk "old shield" pada tiga varian (termasuk di akhir prompt), dan 3/6 dibanding 2/6 tanpa aturan untuk salah dengar yang mirip. `gpt-4.1-mini` mengulang istilah penanya; perbaikannya ada di keyword STT dan ekstraksi. Skenario eval `misheard_project_name` ditandai `xfail` dengan alasan itu.
- Eval persona live dengan label sumber baru: 9 skenario lain stabil (4 run); satu kegagalan `known_profile_fact` 0,69 karena tawaran follow-up, target fase 3.

## Status fase 3 (30 September 2026)

Selesai di branch `feat/memory-ingestion`:

- Aturan penutup (`ENDING_RULE`) menjadi bagian terakhir prompt default dan persona. Aturan asal fakta (`PROVENANCE_RULE`) ditempatkan tepat sebelumnya bila recall diizinkan; kalimat yang sama di tengah prompt dicabut.
- Eval replay `evals/test_replay.py` memutar transkrip STT nyata `acc-0930-s1`/`s2` (termasuk salah dengar) lewat `LangGraphResponder` produksi, tiga kali, dengan memori per sesi dari hasil replay ingestion. Pemeriksaan deterministik: jawaban ada, fakta wajib/terlarang, recall dipanggil, maksimal dua kalimat, pertanyaan penutup ≤ 20%, dan latency p95 ≤ 4 detik. Pass rate wajib 0,9. Logika penilaian diuji offline dan berjalan di CI.
- Model cadangan `FAKEYOU_FALLBACK_CHAT_*` lewat `ModelFallbackMiddleware`. Timeout panggilan model jawaban turun dari 60 ke 20 detik; model utama tidak di-retry bila cadangan aktif; host cadangan tidak ikut preflight.

Bukti:

| Pengukuran | Sebelum | Sesudah |
| --- | --- | --- |
| Pertanyaan penutup, live Meet | 8/8 | — (diukur ulang pada acceptance berikutnya) |
| Pertanyaan penutup, replay 24 giliran | — | 4/24 (16,7%) |
| Penyebutan asal meeting (no. 9) | 0/3 replay, 0/6 probe | 5/6 probe; `must_mention_any` replay 0,75 → 0,917 |
| Latency replay p50 / p95 | — | 1,02 s / 1,96 s |
| Eval persona live | 7–8 lulus + satu kegagalan follow-up | 8 lulus + 1 `xfail`, dua run berturut-turut |
| Fallback saat provider utama mati | — | 7,49 s, tool call berhasil (Muse Spark 1.2 `minimal`) |

Temuan: `gpt-4.1-mini` mematuhi instruksi yang berada di akhir system prompt dan mengabaikan instruksi yang sama di tengah (penutup 6/6 → 0/6, asal fakta 0/6 → 5/6). Aturan perilaku yang harus dipatuhi ditempatkan di akhir; aturan di tengah hanya konteks. Menggeser aturan ejaan vocabulary ke akhir tidak membantu (0/6), karena model mengulang istilah penanya.

## Status fase 4 (30 September 2026)

### Diarisasi Muse diukur dan tidak dipakai di jalur percakapan

Probe tiga giliran dua suara TTS (A, B, A), dialirkan real-time, keyword aktif:

| Mode | Transkrip final setelah akhir ucapan | Giliran hilang | Label |
| --- | --- | --- | --- |
| `ENDPOINTING` (saat ini) | median 0,39 s, maks 0,89 s (n=6) | 0 dari 6 | — |
| `DIARIZATION`, jeda akhir 3 s | median 0,81 s, maks 2,77 s | ucapan ketiga hilang di 2 dari 2 run | A, B |
| `DIARIZATION`, jeda akhir 8 s | median 2,29 s, maks 3,07 s | ucapan ketiga hilang di 1 dari 3 run | A, B, A (penutur kembali dikenali) |

Label diarisasi akurat, tetapi menambah sekitar dua detik ke setiap jawaban (budget VAD stop → PCM pertama saat ini sekitar tiga detik) dan kadang kehilangan giliran. Jalur percakapan tetap `ENDPOINTING`. Identitas speaker jangka panjang tetap audio per participant dari halaman Meet; label diarisasi tidak dipasang sebagai setelan agar tidak ada mode yang dapat menjatuhkan giliran.

### Barge-in dari transkrip interim

- `ConversationLoop.handle_interim`: saat twin berbicara dan barge-in aktif, transkrip interim dengan minimal `BARGE_IN_MIN_WORDS = 3` kata langsung menghentikan playback. Nilai tiga mengikuti contoh `MinWordsUserTurnStartStrategy` Pipecat dan menahan backchannel satu atau dua kata; interupsi pendek tetap ditangkap barge-in transkrip final. Wake phrase dihormati.
- Logika hitung kata ditulis langsung di loop karena strategi Pipecat bergantung pada frame `BotStartedSpeakingFrame` di pipeline, sedangkan playback berjalan lewat `paplay` di luar pipeline.
- Journal `barge_in` mencatat pemicu (`interim` atau `final`) dan jumlah kata; `fakeyou report` menghitung `interruptions.speech_to_stop_s` dari VAD `speech_started` setelah playback dimulai sampai `speak_dropped`. Nilai tiga kata dievaluasi dari laporan ini pada acceptance berikutnya.

### Panjang thread

`gpt-4.1-mini` dengan prompt persona dan riwayat sintetis:

| Pesan di thread | Token input | Token dari cache | Latency median / maks |
| --- | --- | --- | --- |
| 0 | 680 | 0 | 1,27 s / 1,43 s |
| 100 | 2.306 | 2.176 | 1,08 s / 1,13 s |
| 400 | 7.186 | 7.040 | 1,14 s / 1,62 s |
| 1000 | 16.946 | 16.768 | 1,49 s / 1,82 s |

Prompt caching provider menangkap sekitar 99% token karena system prompt stabil dan thread append-only. `SummarizationMiddleware` tidak dipasang; latency per giliran di `fakeyou report` menjadi sinyal untuk meninjau ulang bila meeting jauh lebih panjang.

## Urutan

| Fase | Isi | Mencegah |
| --- | --- | --- |
| 1. Fondasi bukti | Catatan meeting, journal lengkap dengan waktu, `fakeyou report`, konfigurasi model/provider per peran dengan host preflight turunan, tes kontrak dan dokumentasi | C, E, F |
| 2. Memori turunan | `memory-ingest` dari catatan dengan jendela lintas speaker, aturan atribusi, dedupe fakta, `vocabulary` untuk keyword STT dan ekstraksi; ingestion di dalam meeting dilepas | D, E |
| 3. Kualitas terukur | Aturan penutup di akhir prompt, metrik runtime, eval replay dengan pass rate dan anggaran latency, eval asal fakta, `ModelFallbackMiddleware` | A, B |
| 4. Identitas dan giliran | Label diarisasi, barge-in interim, pengukuran panjang thread | A, D |
| 5. Acceptance ulang | Skrip yang sama, dua meeting ID baru, dua pembicara bila memungkinkan, dibandingkan dengan laporan standar | Semua |
| Berikutnya | Audio per participant, structured output bila metrik penutup tetap buruk, job ingestion Kubernetes | A, D, E |

Fase 1 dan 2 mengganti butir 2–5 di urutan [dokumen acceptance](acceptance-2026-09-30.md#urutan-implementasi-yang-diusulkan). Fase 3 mengambil butir 1 dan 6.

## Keputusan pengguna (30 September 2026)

1. Disetujui: catatan meeting menyimpan transkrip mentah di disk (`.runtime/meetings/<meeting_id>/record.jsonl` di worker lokal) sampai dihapus manual, dan ingestion Graphiti pindah ke setelah meeting.
2. Model dipilih per peran lewat abstraksi LangChain. Keputusan 27 September untuk memakai Muse diterapkan sebagai kandidat yang diukur dengan eval yang sama; untuk jawaban lisan, `gpt-4.1-mini` tetap dipakai sampai model lain lulus eval replay dalam anggaran latency.
