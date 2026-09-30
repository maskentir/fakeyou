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
