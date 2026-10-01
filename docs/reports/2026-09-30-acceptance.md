# Acceptance Meet 30 September 2026 dan rencana perbaikan

Dokumen ini mencatat acceptance live untuk [persona dan retrieval memori](../plans/persona-memory.md) (Task 4) dan [ingestion memori](../plans/memory-ingestion.md) (Task 5), lalu riset perbaikan untuk kekurangan yang terlihat di log. Status: fase 1–4 diimplementasikan, acceptance kedua selesai, dan perbaikan temuan acceptance kedua diimplementasikan; bukti live untuk urutan startup baru menunggu acceptance ketiga.

## Setup

- Meeting `https://meet.google.com/fgd-khyx-hao`, dihost pengguna, tanpa peserta lain. Twin masuk sebagai guest `Rani Twin` dan di-admit host.
- Worker `fakeyou-meeting-worker:local` dibangun dari branch `feat/memory-ingestion` setelah perbaikan entrypoint. Flag: `--audio --converse --barge-in --persona examples/persona.example.json --memory`, identitas `demo_owner`/`demo_twin`.
- Seed sebelum sesi: owner note "Update dari Rani: demo customer Orchid dipindah ke hari Jumat, 3 Oktober 2026, jam dua siang." lewat `fakeyou memory-add` di worker (episode `560b16cb-b3f2-4218-bfc6-46802722fdc6`).
- Sesi `acc-0930-s1` lalu `acc-0930-s2` dijalankan berurutan. Stop dipicu otomatis oleh watcher ketika transkrip final memuat kalimat pemicu, lalu `docker kill --signal=SIGINT`.
- Setiap baris log diberi timestamp host. Journal ada di `.runtime/audio/journals/acc-0930-s1.jsonl` dan `acc-0930-s2.jsonl`.
- Konfirmasi pendengaran dari host: semua jawaban twin terdengar.

## Perbaikan yang ditemukan saat persiapan

1. **Entrypoint worker tidak menunggu proses Python selesai setelah sinyal.** `wait` yang diinterupsi trap langsung diikuti `exit`, sehingga `init` container berhenti dan proses Python terbunuh di tengah leave dan drain ingestion. Tes lama justru mengunci perilaku itu (exit 128+signo). Perbaikan: `wait` diulang sampai child benar-benar keluar, dan exit code mengikuti child. Tes baru memakai child yang shutdown selama satu detik. Diverifikasi di image asli (dash): child selesai, container exit 0.
2. **Command worker di README tanpa `fakeyou`.** `docker compose run --rm meeting-worker memory-add ...` gagal dengan `exec: memory-add: not found`; yang benar `meeting-worker fakeyou <command>`.

## Hasil

| # | Transkrip | Jawaban twin | Hasil |
| --- | --- | --- | --- |
| 1 | "Rani Twin kapan beta launch or chill" | "Beta launch Orchid dijadwalkan 14 Oktober 2026, jadi kita belum chill…" | Fakta profil benar; STT salah mendengar "Orchid" dan model ikut bercanda |
| 2 | "demo customer old shield jadinya hari apa" | Recall dipanggil; "…dipindah ke Jumat, 3 Oktober 2026 jam 2 siang" | Fakta owner note benar; nama proyek diulang salah ("Old Shield") |
| 3 | "Berapa harga lisensi orchid per user?" | "Aku belum tahu…, aku cek dulu ke Rani ya." | Benar |
| 4–5 | "Siapa yang pegang checklist…" / "…harus lewat dia juga." | "Rani…" / "…persetujuan tertulis dari Rani." | Benar; referensi "dia" dari checkpoint thread |
| 6 | "…kick off vendor or chill kita putuskan hari Selasa jam sepuluh pagi PIC-nya Budi." | "Aku catat…" | Diingest (episode participant, 2 fakta) |
| 7 | "Oke cukup" saat twin berbicara | `speak_dropped interrupted` pada saat transkrip final | Barge-in bekerja setelah transkrip final |
| 8 | "Terakhir cek memori vendor" | Tidak ada | Stop 14 ms setelah transkrip; agent dibatalkan 45 ms kemudian; tidak ada `speak_started` setelah stop; twin leave |
| 9 | "…kickoff vendor or kilt diputuskan kapan dan siapa PIC-nya?" | Recall dipanggil; "…hari Selasa jam 10 pagi, dan Budi yang jadi PIC-nya." | Recall lintas meeting berhasil; asal "meeting sebelumnya" tidak disebut |
| 10 | "itu sudah disetujui Rani." | "…belum ada konfirmasi bahwa Rani menyetujui…" | Benar |
| 11 | "Oke sesi kedua selesai" | Tidak ada | Stop dan leave |

Tidak ada transkrip yang berasal dari suara twin, sehingga tidak ada tanda echo dari perangkat host. Ucapan twin yang terpotong tidak diingest: jumlah episode twin sama dengan jumlah `speak_finished`.

### Latency per giliran

Diukur dari event VAD "User stopped speaking" sampai PCM pertama dari TTS.

| Tahap | Rentang |
| --- | --- |
| VAD stop → transkrip final | 0,30–0,62 s |
| Transkrip final → jawaban model, tanpa recall | 0,97–1,52 s; giliran pertama sesi 1 2,83 s |
| Transkrip final → jawaban model, dengan recall | 2,42–3,16 s |
| Jawaban model → PCM pertama | 0,80–1,64 s |
| VAD stop → PCM pertama | 2,16–4,50 s |

Waktu dari PCM pertama sampai terdengar di perangkat host tidak diukur; host hanya mengonfirmasi bahwa suara terdengar.

### Ingestion

- Sesi 1: 13 episode dalam 168 detik (satu per ucapan karena twin dan participant bergantian), 3,3–11,0 detik per episode, 3 episode kosong. Batch terakhir tersimpan 8 detik setelah stop berkat perbaikan entrypoint.
- Sesi 2: 5 episode diharapkan; 4 tersimpan, 3 di antaranya setelah stop. Episode terakhir ("Oke sesi kedua selesai") hilang karena drain 20 detik habis. Fault menulis "0 queued utterances were not stored" padahal batch yang sedang diproses ikut hilang.
- Recall setelah sesi berisi fakta pertanyaan seperti "Unidentified participant bertanya kapan kickoff…", yang menempati peringkat pertama. Nama proyek terpecah menjadi entity "kick off vendor or chill" dan "Kickoff vendor or kilt".
- Hasil recall saat meeting tidak tercatat di journal, sehingga isi yang dilihat twin untuk no. 9 tidak dapat dibuktikan.

## Riset perbaikan

Setiap usulan diuji dengan probe terkontrol atau dicocokkan dengan source versi terkunci.

### 1. Ingestion tertinggal dari laju tanya-jawab

- Dokumentasi Graphiti untuk episode `message` memakai percakapan multi-turn dalam satu episode (`{role/name}: {message}` per baris). Pemisahan per jenis speaker di implementasi sekarang tidak diperlukan oleh library.
- Probe: jendela campuran 6 ucapan diproses dalam 9,2–12,7 detik, hampir sama dengan satu ucapan (7–11 detik). Batching lintas speaker menurunkan biaya per ucapan sekitar enam kali.
- Model LLM Graphiti 0.30.2 bawaan adalah `gpt-5.5` (small model `gpt-4.1-nano`), bukan `gpt-4.1-mini` seperti tercatat sebelumnya. `gpt-4.1-mini` hanya sedikit lebih cepat (9,2–11,3 detik), menghasilkan fakta berbahasa Inggris, dan kehilangan atribusi. Model bawaan dipertahankan.
- Jendela campuran menghilangkan label `twin speech` per episode. Probe `gpt-5.5` tanpa aturan tambahan menulis "Rani memegang checklist launch Orchid" untuk ucapan twin. Dengan instruksi "every fact must keep its speaker as the subject", 14/14 fakta dari dua run menyebut speaker ("Digital twin mengatakan bahwa …").
- Instruksi tersebut menghasilkan fakta kembar dalam satu episode (dua dari tujuh). Recall perlu menghapus teks fakta yang identik sebelum membatasi hasil.
- Pertanyaan tidak menjadi fakta di jendela campuran, dengan atau tanpa aturan khusus. Fakta pertanyaan pada live run berasal dari episode yang hanya berisi pertanyaan.
- Graphiti 0.30.2 `add_episode_bulk` menjalankan ekstraksi paralel dan, menurut source terpasang, tetap melakukan invalidasi edge. Dokumentasi web menyebut sebaliknya; source versi terkunci dipakai sebagai acuan. Bulk berguna untuk menguras backlog saat close, tetapi perlu diuji terhadap urutan saga.

**Usulan:** jendela lintas speaker (20 ucapan atau 60 detik), `source_description` `Google Meet <meeting_id> transcript`, aturan atribusi di instruksi ekstraksi, dedupe teks fakta di recall, dan fault drain yang menghitung ucapan dalam batch yang sedang diproses. Pertimbangkan `add_episode_bulk` untuk sisa queue saat close setelah diuji.

### 2. Nama salah dengar dan kemampuan Muse Voice

Menurut [pengumuman Meta](https://dev.meta.ai/resources/blog/meet-muse-voice-transcribe-streaming-speech-to-text/) dan [cookbook resmi](https://github.com/meta-models/meta-model-cookbook/tree/main/06_muse_voice/01_voice_api_fundamentals), Muse Voice Transcribe mendukung code-switching bebas, diarisasi 20+ speaker, keyword biasing, dan audio panjang. Cookbook menyebut `languageBias` dipakai "when you already know the language", `keywords` untuk "names, jargon, and product words the model would otherwise mis-hear", dan keduanya hanya "nudge the model rather than constrain it". Streaming dibatasi 60 menit per sesi (close code `1011`); `MetaSTTService` Pipecat reconnect otomatis. Audio native engine adalah 24 kHz; 16 kHz juga diterima.

`MetaSTTSettings` di Pipecat 1.12 meneruskan `keywords`, `language_bias`, dan `mode`. `create_muse_stt` sudah menerimanya, tetapi `open_meeting_audio` tidak meneruskan `keywords`, dan capture kita 16 kHz dengan bias `Language.ID`.

Probe dengan audio TTS sintetis (satu sampel per kalimat per konfigurasi):

| Konfigurasi | Hasil |
| --- | --- |
| Code-switching ID/EN ("pull request", "breaking change", "but we still need written approval") | Benar pada semua konfigurasi, termasuk bias `Language.ID` saat ini |
| Tanpa `keywords` (bias ID, auto, atau ID+EN) | "Rani" menjadi "Ronnie" di setiap konfigurasi; bias ID+EN bahkan mengubah "Rani" di akhir kalimat menjadi "Ronnie" |
| Dengan `keywords` (`Orchid`, `Rani`, `Budi`, `kickoff`, `PIC`) | "Rani", "PIC-nya", dan "kickoff" benar; kata umum seperti "naik" tetap bisa salah ("Nike") |
| 16 kHz dibanding 24 kHz | 16 kHz menambah kesalahan ("pool request", "following follow-up Gavendor"); bukti masih lemah karena satu sampel dan bukan audio Meet |

Salah dengar live ("or chill", "old shield", "or kilt") kemungkinan berasal dari pelafalan dan codec Meet; efek keyword pada kasus itu harus dibuktikan dengan acceptance ulang.

**Usulan:** field profil `vocabulary` (nama dan istilah penting) yang dipakai untuk `keywords` Muse, disebut di instruksi ekstraksi Graphiti agar entity tidak terpecah, dan disebut di prompt persona agar model memakai ejaan profil ketika transkrip salah dengar. Bias bahasa dibiarkan `Language.ID` karena code-switching sudah native dan bias ID+EN tidak lebih baik. Capture 24 kHz diuji terpisah dengan audio Meet sebelum mengubah topologi PulseAudio.

### 2a. Diarisasi untuk atribusi participant

Probe mode `DIARIZATION` dengan dua suara TTS berbeda:

- Muse mengirim event `speaker` dengan label `A` untuk giliran pertama dan `B` untuk giliran kedua, di antara `speechStart` dan `speechComplete` giliran yang sama.
- Final tidak ganda: dua giliran menghasilkan dua `TranscriptionFrame`.
- `MetaSTTService._handle_message` di Pipecat 1.12 hanya mencatat label itu di level trace. `speechComplete` yang menjadi `frame.result` hanya berisi `transcript`, `turnId`, `audioProcessedMs`, dan `type`, sehingga label tidak sampai ke `transcript_event`.
- Label hanya berlaku dalam satu sesi streaming, tidak stabil setelah reconnect 60 menit, dan bukan identitas peserta Meet. Cookbook: diarisasi hanya untuk ucapan yang tidak tumpang tindih.

**Usulan:** `ManagedMuseSTT` (subclass yang sudah ada) menyimpan label dari event `speaker` untuk giliran berjalan dan menaruhnya di `user_id` frame final; `transcript_event` memetakannya menjadi participant. Di ingestion, label harus dicakup per meeting (misalnya `Participant A of acc-0930-s1`) agar Graphiti tidak menggabungkan orang berbeda lintas meeting. Latency mode `DIARIZATION` dibanding `ENDPOINTING` perlu diukur dulu.

### 3. Tawaran follow-up tetap muncul

- Live: 8/8 jawaban ditutup tawaran atau pertanyaan balik, meskipun `MEETING_GUIDANCE` melarangnya dan eval satu-giliran lulus.
- Probe enam giliran dalam satu thread: 6/6 jawaban diakhiri pertanyaan dengan prompt sekarang; mengubah goal profil menurunkannya hanya sebagian. Aturan penutup eksplisit di akhir prompt ("stop right after the answer…", dengan contoh kalimat yang dilarang) menghasilkan 0/6 pada dua varian, dan jawaban menjadi lebih pendek.

**Usulan:** pindahkan aturan penutup ke akhir system prompt (persona dan default), lalu tambahkan eval multi-giliran yang menghitung pertanyaan penutup.

### 4. Bukti recall tidak tercatat

`_ToolActivity` sudah merupakan `AsyncCallbackHandler` LangChain untuk `on_tool_start`. Callback `on_tool_end` dan `on_tool_error` dapat mencatat nama tool, durasi, jumlah fakta, dan label sumber ke journal tanpa mengubah tool.

**Usulan:** event journal `tool_finished`/`tool_failed` dengan jumlah baris hasil dan label sumber; teks fakta tidak dicatat di journal.

### 5. Asal fakta tidak disebut

Jawaban no. 9 menyatakan keputusan sebagai fakta tanpa "di meeting sebelumnya". Aturan prompt sudah meminta hal itu untuk sumber transkrip. Setelah jendela campuran, label sumber menjadi `transcript` dan waktu tetap tersedia.

**Usulan:** tambah skenario eval live yang mewajibkan penyebutan asal meeting untuk fakta transkrip, lalu ukur sebelum mengubah prompt lagi.

### 6. Barge-in menunggu transkrip final

Twin baru berhenti ketika transkrip final "Oke cukup" tiba, 0,62 detik setelah VAD mendeteksi pengguna berhenti. Pipecat 1.12 menyediakan `MinWordsUserTurnStartStrategy` (`pipecat.turns.user_start`), yang dapat memicu dari transkrip interim ketika jumlah kata minimum terpenuhi saat bot berbicara. `open_meeting_audio` sudah memiliki callback `on_interim`. Strategi ini memakai frame `BotStartedSpeakingFrame`/`BotStoppedSpeakingFrame`, sedangkan playback kita berjalan lewat `paplay` di luar pipeline, sehingga integrasinya perlu desain tersendiri.

**Usulan:** masuk tahap partisipasi multipihak, bukan batch perbaikan ini.

## Urutan implementasi yang diusulkan

Urutan ini digantikan oleh [rencana jangka panjang](../plans/long-term.md), yang mengelompokkan perbaikan berdasarkan penyebab berulang. Daftar di bawah tetap sebagai catatan riset.

1. Aturan penutup di akhir prompt dan eval multi-giliran.
2. Jendela ingestion lintas speaker, aturan atribusi, dedupe fakta recall, dan detail fault drain.
3. `vocabulary` profil untuk keyword Muse, instruksi ekstraksi, dan prompt.
4. Label diarisasi dari `ManagedMuseSTT` ke participant dan ingestion, setelah latency mode `DIARIZATION` diukur.
5. Journal hasil tool.
6. Eval penyebutan asal fakta transkrip.
7. Acceptance live ulang dengan skrip yang sama, dua meeting ID baru, dan dua pembicara bila memungkinkan; bandingkan jumlah episode, backlog saat stop, salah dengar nama, label speaker, dan tawaran follow-up. Uji capture 24 kHz pada run yang sama bila topologi audio diubah.

Data uji tetap di namespace `demo_owner`/`demo_twin` (owner note dan saga `meeting:acc-0930-s1`, `meeting:acc-0930-s2`) sampai dihapus manual dengan `fakeyou memory-forget`.

## Acceptance kedua setelah fase 1–4

Sesi `acc2-0930-s1` dan `acc2-0930-s2` di `https://meet.google.com/qco-uoxa-qrs`, identitas baru `acc2_owner`/`acc2_twin` dengan profil contoh (termasuk `vocabulary`), skrip yang sama, image worker dengan semua perubahan fase 1–4. Semua angka berasal dari `fakeyou report` dan catatan meeting, tanpa script analisis tambahan.

| Ukuran | Acceptance pertama | Acceptance kedua |
| --- | --- | --- |
| Jawaban ditutup pertanyaan | 8/8 | 1/7 (klarifikasi yang sah) |
| "Orchid" ditranskrip benar | 0 dari 4 transkrip yang menyebut proyek ("or chill", "old shield", "or kilt") | 9 dari 9 |
| Interupsi: VAD mulai bicara → playback berhenti | 1,04 s dan 2,07 s (transkrip final) | 0,79 s (interim, 4 kata) |
| VAD stop → PCM pertama p50 / maks | 3,13 s / 4,50 s | 3,01 s / 4,92 s |
| Ingestion sesi 1 | 13 episode, 3 kosong, 4 fakta pertanyaan | 2 episode, 0 kosong, 0 fakta pertanyaan; 17,4 s per episode setelah leave |
| Atribusi fakta memori | tidak konsisten | semua fakta menyebut speaker |
| Recall lintas meeting (no. 9) | benar | benar |
| Asal meeting disebut (no. 9) | tidak | tidak |
| Fault | 1 drain timeout | 0 |

### Masalah baru: awal ucapan pertama hilang

- Sesi 2: STT tersambung 13:32:26,8; VAD mendeteksi suara 0,7 detik kemudian dan transkrip dimulai dari "kickoff vendor Orchid…", sehingga "Rani Twin, di meeting sebelumnya" hilang. Acceptance pertama sesi 2 kehilangan bagian yang sama.
- Sesi 1: STT siap 13:29:09, suara pertama 13:29:21 hanya 0,4 detik dan ditranskrip "kickoff". Pertanyaan no. 1 tidak ada di catatan meeting; twin meminta klarifikasi.
- Penyebab yang terbukti di kode: `_MeetingWorker.run_admitted` baru menonaktifkan kamera, membuka pipeline audio, dan menyambungkan STT setelah admission. Ucapan participant selama jeda itu tidak pernah ditangkap.
- Belum terbukti: apakah "kickoff" berasal dari ucapan pendek yang ditarik ke keyword. Journal juga tidak mencatat admission dan kesiapan audio, sehingga jeda admission → siap tidak dapat diukur dari laporan.

### Catatan lain

- Fakta ekstraksi kali ini berbahasa Inggris walaupun meeting berbahasa Indonesia; recall tetap berfungsi.
- Satu interupsi menghasilkan dua event `barge_in` interim karena `is_speaking` masih benar sesaat setelah stop pertama.
- Twin menyebut "sesuai fakta yang sudah terverifikasi", istilah dari prompt yang ikut terucap.

### Perbaikan setelah acceptance kedua

| Temuan | Perbaikan | Bukti |
| --- | --- | --- |
| Awal ucapan pertama hilang | Pipeline audio dan STT dibuka sebelum twin meminta masuk. Saat admission, jam grace capture di-reset (`mark_admitted`), lalu kamera dimatikan, mikrofon diaktifkan, dan conversation diikat. Transkrip yang tiba sebelum conversation diikat diantrekan lalu dijawab. Gagal membuka audio berakhir `infra_failure` tanpa mengetuk meeting; ditolak host atau stop di lobby menutup audio. | Tes urutan runtime (`audio_started` sebelum `admitted`, penolakan, stop saat startup audio, gagal startup, gagal setelah admission). Bukti live menunggu acceptance ketiga. |
| Jeda admission → siap tidak terukur | Journal mencatat `audio_ready` (lama membuka audio), `join_requested`, `join_result` (status, lama di lobby, audio sudah siap), `microphone_enabled`, dan `conversation_bound`. `fakeyou report` punya bagian `startup`. | Tes laporan untuk admission dan penolakan. |
| Dua event `barge_in` untuk satu interupsi | Interim menghentikan playback sekali per jawaban; penanda direset sebelum jawaban berikutnya diputar. | Tes conversation: satu stop per interupsi, aktif lagi untuk jawaban berikutnya. |
| Fakta berbahasa Inggris | Instruksi ekstraksi menambahkan "Write each fact in the language the speakers used." | Re-ingest `acc2-0930-s1` dengan `--keep-record`: 7 dari 7 fakta baru berbahasa Indonesia dan tetap menyebut speaker. Satu fakta berbahasa Inggris berasal dari ingestion `acc2-0930-s2` sebelum perbaikan; dedupe Graphiti menautkan episode baru ke fakta itu. |
| "Fakta yang sudah terverifikasi" terucap | Aturan bicara natural di akhir prompt, sebelum aturan penutup: istilah internal (verified facts, profil, pencarian memori, tool) tidak disebut. | Probe: 0 dari 22 jawaban menyebut istilah internal. |
| Asal meeting tidak disebut (no. 9) | Output recall dimulai dengan sumber dan waktu: `From <sumber> on <waktu> UTC: <fakta>`. | Probe: 6 dari 6 jawaban recall menyebut asal. |

Contoh frasa di prompt sekarang berbahasa Inggris, dan data uji berbahasa Indonesia pindah ke fixture JSON (`evals/fixtures/persona-scenarios.json`, `tests/fixtures/live-ingestion-meeting.json`). Setelah contoh aturan penutup diganti, probe mencatat 2 dari 16 jawaban diakhiri pertanyaan. Probe di tabel diukur sebelum kata "recall" di aturan bicara natural diganti "memory lookups", karena persona tanpa tool recall tidak boleh menyebut recall. Eval persona live, eval replay, dan tes ingestion live belum dijalankan ulang setelah perubahan terakhir karena kredit API OpenAI habis (`insufficient_quota`).

Ditemukan saat verifikasi: watchdog menutup notice "This meeting is being recorded" dan dialog "Camera not found" lewat locator `get_by_role`, tetapi lapisan humanize CloakBrowser 0.5.11 menolak locator itu (`UnsupportedHumanizeSelectorError`). Notice rekaman tidak akan tertutup, dan sesi berakhir sebagai `browser_failure` setelah 180 detik menunggu tindakan manual. Klik sekarang memakai satu selector `xpath=` yang didukung humanize; tes browser dengan halaman fixture lulus.
