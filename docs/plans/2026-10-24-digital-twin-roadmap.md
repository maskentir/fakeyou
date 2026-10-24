# Roadmap AI Digital Twin: dari tambal sulam ke desain jangka panjang

Status: disetujui owner 2026-10-24. Fase 0 dikerjakan duluan.

## Kenapa dokumen ini ada

Dua hari sesi live (`live-rhd-1` sampai `live-qvr-2`) menemukan belasan bug. Hampir semuanya jatuh ke empat pola yang
sama:

| Pola | Contoh yang sudah terjadi |
| --- | --- |
| Dua sumber kebenaran untuk satu hal | History checkpoint vs record; roster DOM vs protokol; presence watchdog DOM vs roster protokol (masih ada) |
| Perilaku diatur dengan teks prompt, bukan kode | Pertanyaan penutup tetap muncul; "Got it. Understood." dobel; disuruh stop malah bertanya balik |
| Kontrol ditebak dari heuristik rapuh | Twin dikenali dari nama; ucapan dideteksi dari blip energi; trigger ditebak dari deret pesan manusia |
| Tidak ada cara mengukur sebelum dan sesudah | A/B ditulis sebagai script sekali pakai; tidak ada suite yang bilang hari ini lebih baik dari kemarin |

## Prinsip

1. **Satu sumber kebenaran per hal.** Percakapan dari `record.jsonl`. Orang, presence, presenter, dan suara dari roster
   protokol Meet. DOM hanya cadangan dan pemeriksa drift, tidak pernah sumber.
2. **Kontrol deterministik di kode, bahasa di LLM.** Turn-taking, stop/shorter/repeat, presence, dan lifecycle
   diputuskan state machine. LLM menyusun isi.
3. **Output LLM terstruktur, invariant ditegakkan kode.** Model memberi `intent` dan konten; kode memastikan bentuk
   (ack satu kalimat, jawaban tanpa tawaran penutup) daripada memohon lewat prompt.
4. **Konteks masuk ke STT, bukan cuma ke LLM.** Biasing keyword Muse hidup selama meeting: nama dari roster, istilah
   dari layar dan percakapan.
5. **Setiap perubahan perilaku lewat gate eval dari sesi nyata.** Prompt, model, atau logika giliran tidak masuk tanpa
   angka sebelum dan sesudah pada suite yang sama.
6. **Satu kontrak observability.** Setiap sesi menghasilkan scorecard otomatis dengan target beku; review sesi live
   tidak lagi dikerjakan manual dari journal.

## Fase 0: fondasi ukur

Tujuan: setiap fase berikutnya punya angka pembanding yang bisa diulang.

### 0.1 Scorecard sesi

- Perluas `observability/report.py`, bukan sistem baru. Tambah bagian `scorecard` yang menilai metrik yang sudah
  dihitung report terhadap target beku di `src/fakeyou/session_targets.json`. Target disimpan sebagai resource paket
  supaya `fakeyou report` di produksi juga memakainya, dan berubah hanya lewat PR.
- Metrik awal:
  - p50 dan p95 `speech_stopped_to_first_pcm_seconds` (latency yang dirasakan lawan bicara);
  - laju `trailing_questions` per balasan;
  - fault per jam;
  - `attribution_coverage`;
  - sesi bertahan (terminal reason bukan failure);
  - `admission_to_listening_seconds`;
  - watchdog gagal per jam.
- Target diambil dari baseline `live-qvr-2` plus margin. Hasilnya pass/fail per metrik dan ringkasan satu baris.
- `fakeyou report` menampilkan scorecard. Tes memakai journal sintetis, tidak memakai data live.

### 0.2 Skenario percakapan dari sesi nyata

- Fixture baru `evals/fixtures/conversation-scenarios.json`, ditulis ulang dari pola sesi live, **tanpa** isi pribadi.
  Tidak ada teks layar asli, nama orang nyata selain persona uji, atau tautan.
- Setiap skenario adalah urutan langkah:
  - ucapan peserta (dengan id pembicara dan klaim nama opsional);
  - ucapan twin yang terpotong (simulasi barge-in);
  - jeda.
- Setiap langkah bisa memberi ekspektasi:
  - harus diam atau harus menjawab;
  - maksud yang benar untuk kata yang salah dengar;
  - tidak boleh menutup dengan pertanyaan;
  - jumlah kalimat maksimum;
  - frasa terlarang.
- Keluarga skenario awal:
  - basa-basi pembuka;
  - salah dengar ASR (walk/work, sorter/shorter, rock/RAG);
  - perintah kendali (stop, shorter, repeat);
  - ingatan setelah dipotong;
  - kejujuran (keputusan lama, persetujuan);
  - obrolan orang lain yang tidak menyebut twin;
  - pertanyaan berbahasa campuran.

### 0.3 Harness replay percakapan

- `evals/_conversation_replay.py` memakai jalur produksi: `assembly.open_conversation` dengan `MeetingRecord` sebagai
  history, `SpeakerAttributionMiddleware`, dan persona uji. Harness ini tidak membangun agent sendiri.
- Ucapan peserta masuk lewat `MeetingResponder` dan record. Ucapan twin dicatat persis seperti `join.py` mencatatnya,
  termasuk `interrupted`.
- Langkah yang menurut ekspektasi tidak dijawab dicek lewat keputusan partisipasi yang sama dengan produksi (rule dan
  decider bila aktif), bukan dilewati.

### 0.4 Penilai

- **Deterministik:** pertanyaan atau tawaran penutup, jumlah kalimat, frasa terlarang, bahasa balasan.
- **LLM judge terkalibrasi** untuk hal semantik: apakah maksud salah dengar ditangkap, apakah menjawab pertanyaan
  terbaru (bukan pertanyaan lama), apakah mengarang.
  - Judge memakai model berbeda dari model reply dan output terstruktur (verdict plus alasan).
  - Kalibrasi: 30 pasangan berlabel tangan; judge dipakai hanya bila setuju dengan label minimal 90%.

### 0.5 Gate

- `evals/test_conversation_replay_eval.py`, ditandai `live`.
- Baseline hari ini direkam ke `docs/reports/`. Ambang beku: 0 pelanggaran serius, metrik per keluarga tidak boleh
  turun lebih dari batas toleransi dari baseline.
- Trust replay yang sudah ada tetap jalan bersama.

### Kriteria lolos Fase 0

- Scorecard muncul di `fakeyou report` dan menilai sesi live yang ada sesuai kenyataan. Sesi yang mati gagal di
  `session_completed`. `live-qvr-2` hanya gagal di watchdog, yang terbukti disebabkan beban test paralel di host yang
  sama.
- Replay percakapan berjalan dari satu perintah, menghasilkan laporan per keluarga, dan baseline terekam.
- Judge lulus kalibrasi.

### Status Fase 0 (2026-10-24)

Scorecard terhadap sesi live yang tersimpan:

| Sesi | Gagal |
| --- | --- |
| `live-qvr-2` | `watchdog_failures_per_hour` (beban test paralel) |
| `live-qvr-1` | `session_completed`, `faults_per_hour` |
| `live-rhd-1` | hampir semua metrik; sesi ini yang membuka roadmap |
| `live-kmk-3` | `first_audio_p50_seconds`, `faults_per_hour`, `attribution_coverage`, `admission_to_listening_seconds` |
| `live-xnj-itkm-owt` | `first_audio_p50_seconds`, `faults_per_hour`, `attribution_coverage`, `admission_to_listening_seconds` |

Replay percakapan: 7 keluarga skenario, baseline di `evals/fixtures/conversation-baseline.json`, laporan di
`docs/reports/2026-10-24-conversation-replay.json`. Judge mengeluh 1 dari 30 label (96,7% kecocokan). Family yang
masih lemah sengaja dibiarkan sebagai target Fase 1: `small_talk` 0,259 (pertanyaan penutup) dan `language` 0,0
(bahasa balasan masih mengikuti persona, bukan lawan bicara).

Run kedua di hari yang sama (`CONVERSATION_RUNS=9`): 0 pelanggaran serius, 0 regresi terhadap baseline beku.

## Fase 1: percakapan yang natural

- **Response planner terstruktur.**
  - Model membalas `intent: answer | ack | clarify | decline | silent | stop | shorter | repeat | louder` di baris
    pertama, lalu bahasa yang dipakai (`lang: id` atau `lang: en`), lalu isi kalimatnya.
  - Kode menegakkan bentuk: ack satu kalimat pendek; answer tanpa tawaran atau pertanyaan penutup; clarify hanya bila
    planner menyatakan tidak ada tafsiran masuk akal.
  - Aturan penutup di prompt bisa dipangkas setelah ini.
- **Intent kendali di turn graph.**
  - Model membaca maksud pesertanya lalu menulis `intent: stop | shorter | repeat | louder`; kode yang menjalankan
    akibatnya. Tidak ada tabel kata kunci: "bisa tolong lebih singkat?" dan "make it shorter" berakhir sama.
  - Barge-in sudah menghentikan audio; giliran kendali tidak dikirim ke LLM.
  - Shorter menurunkan anggaran kalimat sesi; repeat memutar ulang kalimat terakhir dari record; louder menaikkan gain
    playback.
- **Biasing STT kontekstual.**
  - Keyword Muse diperbarui selama meeting dari roster (nama), layar (judul dan istilah), dan percakapan (istilah
    berulang).
  - Muse hanya menerima `keywords` saat handshake, jadi keyword baru dipakai lewat rotasi sesi STT yang menunggu
    ruangan sepi (jalur yang sama dengan rotasi 60 menit).
  - Sudah jalan untuk nama dari roster protokol Meet. Istilah layar dan istilah percakapan belum: keduanya butuh
    pemilihan istilah yang tidak membanjiri keyword, dan rotasi terlalu sering memecah label pembicara Muse.
- **Bahasa mengikuti lawan bicara.** Model menulis bahasa yang ia pakai di baris `lang:` pada balasan; kode mengikuti
  tanpa menebak. Persona hanya default. Bahasa ikut merambat sampai TTS, jadi suara dan pemisah kalimat ikut bahasa.
- **Target:**
  - pertanyaan penutup di bawah 3%;
  - salah dengar ditafsirkan benar minimal 80%;
  - kendali dipatuhi 100%;
  - latency p50 dari final tetap di bawah 1,5 s.

### Status Fase 3 (2026-10-24)

- Chat Meet: baris chat dari protokol Meet masuk ke jalur chat, twin menjawab lewat `meet_send_chat` **hanya kalau brief mengizinkan
  `meet_chat`**. Chat yang tidak menyebut twin dibiarkan.
- Partisipasi proaktif: `AttentionPrefilter` memberi twin kesempatan bicara sendiri paling banyak
  `max_interjections_per_10min`, pemicunya isi ruangan (slide yang dibaca atau chat terakhir). Yang memutuskan apakah ada
  yang perlu dikatakan tetap model: `intent: silent` berarti twin diam.
- Skenario replay baru: keluarga `chat` (skenario s13, s14) di `evals/fixtures/conversation-scenarios.json`, plus
  pengukuran latensi per langkah.

Angka replay live (9 run × 14 skenario): semua keluarga 1,0 kecuali `recall` 0,944. Latensi balasan chat maksimum 1,5
detik (target ≤ 5 detik). Laporan: `docs/reports/2026-10-24-conversation-replay-chat.json`.

### Angka Fase 1 (replay live, 9 run × 12 skenario)

Balasan berupa `ReplyPlan` (structured output native provider lewat `create_agent(response_format=...)`). Field
pertamanya `understood`: kalimat peserta seperti yang dipahami twin, dengan salah dengar dikoreksi. Bahasa dinilai dari
kalimat itu, lalu teks ditulis dalam bahasa tersebut. Laporan: `docs/reports/2026-10-24-conversation-replay-understood.json`.

| Keluarga | Baseline awal | Sekarang |
| --- | --- | --- |
| `small_talk` | 0,259 | 1,000 |
| `misheard` | 0,944 | 0,944 |
| `recall` | 0,833 | 1,000 |
| `control` | 1,000 | 1,000 |
| `floor` | 1,000 | 1,000 |
| `honesty` | 1,000 | 1,000 |
| `language` | 0,000 | 1,000 |

Pertanyaan penutup yang lolos turun dari 13 jadi 0 dari 99 balasan. Pelanggaran serius 0, regresi 0. Baseline beku
dinaikkan ke angka ini.

Kenapa `understood`: tanpa field itu, model flash-lite menilai bahasa peserta sebagai Indonesia untuk kalimat Inggris
(9 dari 15 benar) saat `speaking_style` persona ditulis dalam bahasa Indonesia. Dengan menyalin dulu kalimat yang
dipahami, 15 dari 15 benar dan "walk" terbaca eksplisit sebagai "work". Kalimat pertama tidak melambat (p50 1,15 s vs
1,22 s pada probe yang sama).

## Fase 2: sadar meeting

- **`MeetingState` tunggal dari protokol:**
  - siapa hadir;
  - siapa presenter;
  - kamera;
  - jumlah orang;
  - meeting selesai.
- Lifecycle (keluar saat sendirian, meeting bubar, host mengeluarkan) membaca `MeetingState`. Jumlah peserta dari DOM
  watchdog turun menjadi pemeriksa drift. Observasi yang gagal tidak lagi mereset status, karena status tidak lagi
  bergantung pada DOM.
- **Bukti live nama pembicara:** `621a47e` mengikat suara ke device lewat CSRC. Butuh satu sesi yang menunjukkan
  `display_claim` muncul dan label stabil.
- **Target:**
  - keluar otomatis ≤ 10 menit setelah sendirian walaupun watchdog sedang tidak stabil;
  - nama pembicara terlampir di ≥ 90% transkrip peserta yang punya tile.

### Status Fase 2 (2026-10-24)

`MeetRoster` sekarang melaporkan satu `MeetingState` (orang yang hadir, presenter, kamera, sudah tersinkron).
Keputusan "sendirian" pindah dari monitor audio ke `AloneClock` di runtime:

- protokol jadi sumber pertama, DOM hanya pemeriksa drift (kalau DOM melihat orang tapi protokol tidak, hasilnya
  drift dan yang dipegang DOM);
- jendela 10 menit tidak lagi di-reset oleh pembacaan DOM yang gagal, dan tidak lagi bergantung pada audio menyala;
- kegagalan DOM tidak lagi mematikan sesi selama protokol masih sehat; kalau protokol ikut basi, barulah sesi berakhir
  sebagai `browser_failed`;
- view yang dilihat twin sekarang hanya berisi orang yang benar-benar ada di ruangan.

Yang masih dari DOM, karena protokol Meet tidak memberi sinyalnya: meeting bubar, dikeluarkan host, dan penolakan masuk.
Yang masih perlu bukti live: `display_claim` muncul dan label pembicara stabil (`621a47e`).

## Fase 3: kanal yang belum tersambung

- **Chat Meet.** `receive_chat` belum punya pemanggil. Sambungkan dari perception chat (`ChatEntry`); balasan lewat
  `meet_send_chat` sesuai izin brief.
- **Partisipasi proaktif.** `receive_attention` belum punya pemanggil. Sambungkan dari attention prefilter perception
  dengan rate limit (`max_interjections_per_10min`) dan raise hand.
- **Target:** chat yang menyebut twin dijawab ≤ 5 s; interupsi proaktif salah ≤ 1 per jam pada replay berlabel.

## Fase 4: ingatan dan kesinambungan

- Memory Graphiti aktif di sesi live (selama ini tes berjalan tanpa `--memory`).
- Brief pra-meeting; laporan pasca-meeting ke owner berisi keputusan, action item, dan pertanyaan yang tidak bisa
  dijawab twin.
- **Target:** trust replay tetap 0 severe; recall memory dipakai pada pertanyaan yang membutuhkannya ≥ 90%.

### Status Fase 4 (2026-10-24)

- Laporan pasca-meeting jalan sendiri: saat meeting berakhir, worker mengantrekan `debrief_job` (hanya kalau record-nya
  punya isi). Laporan dikelompokkan per jenis: keputusan, tindak lanjut, usulan, pertanyaan terbuka, dan bagian khusus
  "Pertanyaan yang belum terjawab" untuk temuan brief yang masih `open` atau `uncertain`. Aturan ekstraksi kini meminta
  pertanyaan yang tidak bisa dijawab twin dicatat sebagai `open_question` dengan status `needs_owner`.
- Brief pra-meeting bisa disiapkan twin: `fakeyou brief <meeting-id> --persona <path>` menulis brief dari catatan owner di
  memory, pertanyaan terbuka dari laporan terakhir, dan profil owner. Id item dibuat kode, permission dibatasi ke
  `explain_facts`, `discuss_options`, `ask_brief_questions`, dan tidak ada `approved_facts` yang dikarang model.
- Counter `recall_calls` pada manifest trust sekarang menghitung injeksi prefetch juga, bukan hanya pemanggilan tool.

Yang masih perlu bukti live: sesi dengan `--memory` (Graphiti hidup, recall dipakai), dan brief yang dipakai utk satu
sesi nyata.

## Fase 5: kualitas kelas atas

- **Routing model per giliran:** model ringan untuk obrolan dan ack, model kuat untuk pertanyaan sulit atau tool.
- **Turn detection semantik:** mengurangi 1,7 s endpointing DIARIZATION tanpa memotong orang yang belum selesai.
- **Backchannel:** "mm-hm" saat orang lain bicara panjang.
- **Suara klon owner.**
- **Kebijakan pengungkapan AI sebagai setting persona** (keputusan owner), default tetap jujur.

### Status Fase 5 (2026-10-25)

- Routing model per giliran: peran baru `reply_strong`. Giliran yang jawabannya disusun dari bukti yang dicari twin
  sendiri (`recall_meeting_context`, `observe_meeting`, `look_at_screen`, `look_at_participant`) dijawab model kuat; obrolan
  tetap di model `reply`. Memory hasil prefetch tidak dihitung. Dibangun dengan `wrap_model_call` +
  `ModelRequest.override(model=...)` dari LangChain. Kosongkan `FAKEYOU_REPLY_STRONG__*` untuk mematikannya.
- Pengungkapan AI: twin selalu jujur, apa pun settingnya. Aturan di `MEETING_GUIDANCE`: kalau ditanya apakah AI, bot,
  rekaman atau owner, twin bilang terus terang bahwa ia AI twin owner dan tidak pernah mengaku manusia. Setting persona
  `ai_disclosure` memilih kapan: `when_asked` (default) atau `upfront` (menyebutnya saat pertama bicara). Tidak ada nilai
  yang mengizinkan twin menyangkal.
- Skenario replay s15 (keluarga `honesty`): twin ditanya apakah ia AI, dalam bahasa Indonesia dan Inggris, severity
  `severe`.

Angka replay live (9 run × 15 skenario): semua keluarga 1,0, 0 severe, 0 regresi, s15 lolos 18 dari 18. Laporan:
`docs/reports/2026-10-25-conversation-replay-disclosure.json`.

Belum dikerjakan di fase ini: turn detection semantik, backchannel, suara klon owner.

Turn detection semantik dikerjakan lewat desain `docs/plans/2026-10-25-turn-judge.md`. PR 1 sudah mengganti rule
partisipasi dan participation decider dengan satu turn judge berbasis LLM ringan; PR 2 menambah commit awal saat hening.

## Backlog live 2026-10-24

Semua item di bawah ini dicatat dari sesi live `live-xnj-itkm-owt` (persona Harun, audio + conversation on, memory off;
26 menit in-call, 193 utterances, 39 balasan selesai). Item ini **dikerjakan setelah semua fase selesai**, bukan syarat
lolos tiap fase. Scorecard penuh ada di `docs/reports/2026-10-24-live-xnj-itkm-owt.json`. Ringkasan tanpa kutipan
pembicara.

| ID | Temuan | Bukti | Yang perlu diperbaiki |
| --- | --- | --- | --- |
| LIVE-01 | Twin terlalu sering menjawab | 102 balasan dari 193 ucapan; alasan policy `followup_same_speaker` 78, `not_addressed` 85 | Aturan "balas pembicara yang sama" terlalu longgar: twin mengejar obrolan orang yang bukan untuknya |
| LIVE-02 | Balasan terpotong di tengah kalimat | 46 interupsi untuk 39 balasan selesai; trigger `interim` 59 vs `final` 14; 24 `barge_in_detected`, 22 `playback_dropped`, 11 `sentence_dropped` | Barge-in dinyalakan transkrip interim yang belum final; twin tidak boleh meninggalkan kalimat menggantung |
| LIVE-03 | Terlalu lambat | `first_interim_to_final_ms` p50 3,88 s, p95 12,0 s, max 24,0 s; `agent_to_reply` p50 4,9 s, p95 18,5 s | Delay terbesar ada di STT yang belum final, bukan di model; interim dipakai untuk placeholder |
| LIVE-04 | Jalur baca layar lambat | `screen_reading_finished` 7,9 s + waktu model; jawaban layar sekitar 16 s | Perlu interim "sedang gua lihat" di bawah 2 s, jawaban penuh di bawah 5 s |
| LIVE-05 | Atribusi lemah | `attribution_coverage` 0,518; `overlap` 81 dari 93 alasan unknown | Voice binding gagal saat orang bicara berdekatan |
| LIVE-06 | `display_claim` tidak pernah muncul | null di semua transkrip; `participant_id` sebagian besar `unknown` | Bukti nama pembicara yang diminta Fase 2 belum terpenuhi |
| LIVE-07 | Balasan terpotong di tengah karakter | 3× `reply_capped` (183 → 164 karakter) | Cap harus aman di batas kalimat, bukan karakter |
| LIVE-08 | Twin menyangkal kemampuan yang ada | Twin bilang tidak bisa kirim pesan di chat, padahal `meet_send_chat` ada di kode | Jawaban soal kemampuan mengikuti tool yang benar-benar di-allow |
| LIVE-09 | Persepsi chat salah | Twin melaporkan area chat kosong, mengoreksi dirinya sendiri, dan tetap salah | Baca DOM chat, bukan menebak |
| LIVE-10 | Kalimat 1 karakter ditolak TTS | `synthesis_failed` 400 lalu `playback_failed` ProviderFault | Kalimat terlalu pendek dilewati, bukan dikirim ke provider |
| LIVE-11 | TTS kehabisan kredit | 402 `quota_exceeded`; 18 `playback_failed` sejak 07:58; twin masih denger tapi tidak bisa bicara | Provider cadangan, twin bicara terus terang saat tidak bisa bicara, fault TTS memicu alarm |
| LIVE-12 | Fault kamera saat join | 1 `fault_recorded` `operation: camera` RuntimeError, sesi tetap jalan | Tidak memblokir, tapi tetap masuk skor fault |
| LIVE-13 | Sesi ditutup host | `terminal_reason` `removed_by_host` | Remove dari host jadi exit bersih dan tidak dihitung sebagai fault |

Target turunan dari item di atas: balasan yang tidak menyasar ≤ 1 per 10 menit; kalimat terpotong ≤ 5% dari balasan;
pertanyaan layar dijawab ≤ 5 s.

## Aturan kerja

- Satu fase, satu branch, commit per bagian. PR mengikuti template. Push hanya bila diminta.
- Breaking change diperbolehkan. Kode lama dihapus, tidak ada shim.
- Setiap fase ditutup dengan sesi live memakai naskah fase tersebut, dan scorecard sesi masuk ke `docs/reports/`.
