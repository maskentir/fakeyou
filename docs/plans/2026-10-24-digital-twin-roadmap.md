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

Replay percakapan: 7 keluarga skenario, baseline di `evals/fixtures/conversation-baseline.json`, laporan di
`docs/reports/2026-10-24-conversation-replay.json`. Judge mengeluh 1 dari 30 label (96,7% kecocokan). Family yang
masih lemah sengaja dibiarkan sebagai target Fase 1: `small_talk` 0,259 (pertanyaan penutup) dan `language` 0,0
(bahasa balasan masih mengikuti persona, bukan lawan bicara).

Run kedua di hari yang sama (`CONVERSATION_RUNS=9`): 0 pelanggaran serius, 0 regresi terhadap baseline beku.

## Fase 1: percakapan yang natural

- **Response planner terstruktur.**
  - Model membalas `{intent: answer | ack | clarify | decline | silent, text}`.
  - Kode menegakkan bentuk: ack satu kalimat pendek; answer tanpa tawaran atau pertanyaan penutup; clarify hanya bila
    planner menyatakan tidak ada tafsiran masuk akal.
  - Aturan penutup di prompt bisa dipangkas setelah ini.
- **Intent kendali di turn graph.**
  - Stop, shorter, repeat, dan louder dikenali deterministik (kosakata JSON id/en, pola yang sama dengan ack
    vocabulary).
  - Barge-in sudah menghentikan audio; giliran kendali tidak dikirim ke LLM.
  - Shorter mengubah anggaran kalimat sesi; repeat memutar ulang kalimat terakhir dari record.
- **Biasing STT kontekstual.**
  - Keyword Muse diperbarui selama meeting dari roster (nama), layar (judul dan istilah), dan percakapan (istilah
    berulang).
  - Ini butuh verifikasi apakah Muse menerima update keyword di tengah sesi; bila tidak, keyword dipakai saat rotasi
    sesi STT.
- **Bahasa mengikuti lawan bicara.** Bahasa balasan ditentukan per giliran dari bahasa trigger, persona hanya default.
- **Target:**
  - pertanyaan penutup di bawah 3%;
  - salah dengar ditafsirkan benar minimal 80%;
  - kendali dipatuhi 100%;
  - latency p50 dari final tetap di bawah 1,5 s.

### Temuan live 2026-10-24 (`live-xnj-itkm-owt`)

Sesi live dengan persona Harun (bahasa Inggris, audio + conversation on, memory off). 41 balasan terukur. Ringkasan
tanpa kutipan pembicara.

| Temuan | Bukti | Dampak |
| --- | --- | --- |
| Barge-in memotong balasan di tengah kalimat | 24 `barge_in_detected`, 15 `agent_cancelled`, 22 `playback_dropped`, 11 `sentence_dropped` | Twin harus tahu kalimatnya belum selesai sebelum mulai bicara, dan tidak boleh meninggalkan kalimat menggantung |
| Twin menjawab yang bukan untuk dia | Ucapan sapaan tanpa nama dijawab twin; pertanyaan yang tidak menyasar twin tetap dijawab | Wake state terlalu lengket atau decider terlalu longgar |
| Jalur baca layar lambat | `screen_reading_finished` 7,9 s + waktu model; pertanyaan tentang isi layar dijawab sekitar 16 s, dan peserta mengeluh delaynya | Butuh interim "sedang gua lihat" di bawah 2 s, jawaban penuh di bawah 5 s |
| Balasan terpotong di tengah karakter | 3× `reply_capped` (183 → 164 karakter) | Cap harus aman di batas kalimat, bukan karakter |
| Twin menyangkal kemampuan yang memang ada | Twin bilang tidak bisa kirim pesan di chat, padahal `meet_send_chat` ada di kode | Jawaban soal kemampuan harus diturunkan dari tool yang di-allow |
| Persepsi chat tidak bisa dipercaya | Twin melaporkan area chat kosong, lalu mengoreksinya sendiri dan tetap salah | Layar chat dibaca dari DOM chat, bukan tebakan |
| Kalimat 1 karakter ditolak provider TTS | `synthesis_failed` `provider_status` 400 lalu `playback_failed` ProviderFault (07:39:17) | Kalimat terlalu pendek harus dilewati, bukan dikirim ke provider |
| Fault kamera saat join | 1 `fault_recorded` `operation: camera` RuntimeError (07:35:13), sesi tetap jalan | Tidak memblokir, tapi tetap tercatat di skor fault |

Latency jalur chat biasa sehat: `speech_stopped` ke PCM pertama p50 1,69 s, p95 3,08 s, max 5,25 s.

Target baru: balasan yang tidak menyasar ≤ 1 per 10 menit; kalimat terpotong ≤ 5% dari balasan; pertanyaan layar dijawab ≤ 5 s.

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
  - Sesi `live-xnj-itkm-owt` belum memenuhi: `display_claim` null di semua transkrip, `participant_id` sebagian
    besar `unknown` (sisanya `speaker-a`/`speaker-b`). Twin menebak jumlah peserta dari DOM, bukan dari roster.
- **Target:**
  - keluar otomatis ≤ 10 menit setelah sendirian walaupun watchdog sedang tidak stabil;
  - nama pembicara terlampir di ≥ 90% transkrip peserta yang punya tile.

## Fase 3: kanal yang belum tersambung

- **Chat Meet.** `receive_chat` belum punya pemanggil. Sambungkan dari perception chat (`ChatEntry`); balasan lewat
  `meet_send_chat` sesuai izin brief.
  - Bukti live `live-xnj-itkm-owt`: satu `chat_line_observed` (07:44:58) masuk backlog, tidak ada balasan apa pun.
    Twin juga menyatakan tidak bisa kirim pesan di chat, padahal tool-nya ada. Tiga hal yang harus beres:
    `receive_chat` punya pemanggil, `meet_send_chat` ada di `allowed_tools` persona, dan jawaban twin soal
    kemampuan chat mengikuti tool yang benar-benar aktif.
- **Partisipasi proaktif.** `receive_attention` belum punya pemanggil. Sambungkan dari attention prefilter perception
  dengan rate limit (`max_interjections_per_10min`) dan raise hand.
- **Target:** chat yang menyebut twin dijawab ≤ 5 s; interupsi proaktif salah ≤ 1 per jam pada replay berlabel.

## Fase 4: ingatan dan kesinambungan

- Memory Graphiti aktif di sesi live (selama ini tes berjalan tanpa `--memory`).
- Brief pra-meeting; laporan pasca-meeting ke owner berisi keputusan, action item, dan pertanyaan yang tidak bisa
  dijawab twin.
- **Target:** trust replay tetap 0 severe; recall memory dipakai pada pertanyaan yang membutuhkannya ≥ 90%.

## Fase 5: kualitas kelas atas

- **Routing model per giliran:** model ringan untuk obrolan dan ack, model kuat untuk pertanyaan sulit atau tool.
- **Turn detection semantik:** mengurangi 1,7 s endpointing DIARIZATION tanpa memotong orang yang belum selesai.
- **Backchannel:** "mm-hm" saat orang lain bicara panjang.
- **Suara klon owner.**
- **Kebijakan pengungkapan AI sebagai setting persona** (keputusan owner), default tetap jujur.

## Aturan kerja

- Satu fase, satu branch, commit per bagian. PR mengikuti template. Push hanya bila diminta.
- Breaking change diperbolehkan. Kode lama dihapus, tidak ada shim.
- Setiap fase ditutup dengan sesi live memakai naskah fase tersebut, dan scorecard sesi masuk ke `docs/reports/`.
