# Twin Redesign

Status: draft revisi 1, menunggu review owner.
Tanggal: 2026-10-28.
Dasar: audit design empat subsistem (voice real-time, otak, perception dan
kontrol Meet, orkestrasi dan platform) setelah PR #44, diverifikasi terhadap
kode di `main` (`ef2d7ab`) dan journal sesi live di
`.runtime/audio/journals/` serta `.runtime/audio/meetings/live-*/`.
Aturan: breaking change diizinkan, tidak ada backward compatibility, tidak
ada mode legacy/shadow, dan setiap PR menghapus jalur lama yang ia gantikan.

## Verdict

Kernel (PR #43, #44) membereskan sumber fakta, otorisasi, dan command. Tapi
audit menemukan bahwa banyak batas kemampuan twin bukan bug, melainkan
bentuk design sejak awal:

1. Twin secara struktur hanya bisa menjadi FAQ bot. Kontribusi wajib
   mengutip salah satu dari sekitar 20 item statis, jadi opini, usulan,
   follow-up, dan keberatan tidak punya bentuk. Dari 34 `reply_plan` di
   semua journal, 34 berisi `contribution: none`.
2. Twin tidak tahu apa yang owner pikirkan dan tidak pernah belajar.
   Pengetahuan owner adalah JSON statis tanpa posisi, preferensi, atau aturan
   keputusan, dan debrief tidak pernah kembali ke profil.
3. Twin terasa seperti mesin, bukan peserta. Akhir giliran diputuskan oleh
   timeout STT di cloud, barge-in menunggu 3 kata STT plus panggilan LLM,
   dan TTS adalah satu request HTTP per kalimat yang diputar lewat proses
   `paplay` baru.
4. Twin tidak tahu siapa yang bicara. Di `live-oue-4`, 236 dari 236 final
   `speaker_bound=false`. Capture per-stream yang memberi identitas dari
   stream sudah ada (fleet 4 slot), tetapi mati secara default dan belum
   pernah divalidasi live.
5. Twin tidak bertahan di meeting. Status sesi ada di empat tempat tanpa
   pemilik, join command di-ack begitu sesi bind, crash browser selalu
   terminal, dan tidak ada kode rejoin.
6. Twin tidak bisa always-on. Tidak ada scheduler atau calendar, satu proses
   hanya satu sesi karena PulseAudio memakai env global, dan Redis berjalan
   tanpa persistence sementara join command dan lease ada di Redis.

Plan ini mengganti bentuk-bentuk itu, bukan menambal gejalanya.

## Tujuan

Twin harus dapat:

- Tetap berada di meeting sampai meeting selesai, dan pulih sendiri dari
  crash browser, restart worker, atau gangguan provider.
- Bicara dan berhenti bicara dengan ritme manusia: mulai menjawab sekitar
  satu detik setelah orang selesai, dan berhenti dalam sepersekian detik
  saat dipotong.
- Tahu siapa yang bicara dari stream audio, bukan tebakan.
- Menyumbang seperti owner: menyatakan posisi owner, mengusulkan, bertanya
  lanjut, dan berkeberatan, dengan dasar yang jelas dan bisa diaudit.
- Menjadi lebih baik dari meeting ke meeting lewat owner model yang tumbuh
  dari debrief yang di-review owner.
- Join meeting dari calendar owner tanpa perintah manual, beberapa meeting
  sekaligus.
- Menjelaskan setiap ucapan dari transcript pemicunya sampai audio yang
  diputar.

## Non-goals

- Memberi twin kewenangan membuat komitmen atau keputusan baru atas nama
  owner di luar brief dan owner model yang di-approve.
- Mengganti Google Meet guest browser dengan API resmi.
- Klaim identitas dunia nyata dari display name saja.
- Belajar otomatis tanpa review owner. Semua perubahan owner model melalui
  approval.
- Latency di bawah ritme percakapan manusia. Target adalah terasa wajar,
  bukan instan.

## Fondasi yang dipertahankan

- `SessionKernel` sebagai single-writer fakta meeting, state speech
  deterministik, judge tunggal dengan output typed (termasuk cache judge
  pada interim), command ledger dengan boundary, record v4.
- Roster, presenter, kamera, dan chat dari Meet protocol.
- Capture per-stream (`StreamFleetSession`, 4 slot) dan rotasi sesi STT
  sebelum batas 60 menit.
- Debrief yang wajib menyertakan kutipan bukti, provenance Graphiti.

## Koreksi terhadap plan kernel

Empat keputusan di `2026-10-27-twin-meeting-kernel-design.md` salah dan
diganti oleh plan ini:

1. "Session supervisor yang sudah ada ... pemilik ... reconnect". Kode
   reconnect atau rejoin tidak ada. Recovery menjadi tanggung jawab
   reconciler di A2.
2. "Timeline masih sumber view dan pemicu proactive". Pergantian layar bukan
   alasan untuk bicara. Proactive dibangun kernel dari brief, owner model,
   dan dialog di C6.
3. "Memory tidak dicari untuk turn yang tidak membutuhkannya". Recall yang
   menunggu turn selalu terlambat dan selalu berada di critical path. Recall
   menjadi listener latar di C5.
4. Rewrite floor/lanes yang hanya menyentuh `turn_lifecycle` dan
   `turn_graph` terlalu sempit. State turn tersebar di `TranscriptDrain`,
   `EarlyTurns`, `BargeLatch`, `SpeechFloor`, `AdmittedFinals`,
   `PendingFinals`, `DeferredTurns`, `QuietFloor`, dan generation
   `PlaybackEngine`. B1 menggantinya dengan satu floor actor.

## Arsitektur setelah redesign

Setiap lapisan punya satu tanggung jawab dan satu jenis keputusan:

| Lapisan | Pemilik | Memutuskan | Tidak pernah |
| --- | --- | --- | --- |
| Supervisor sesi | baris `meeting_sessions` | join, rejoin, terminal | memutuskan ucapan |
| Adapter (browser, audio, STT, perception) | proses sesi | tidak ada; hanya observasi dan eksekusi | memanggil conversation |
| Kernel | `SessionKernel` | fakta meeting, otorisasi, plan turn, kandidat proactive, recall set | menunggu IO |
| Floor actor | proses sesi | kapan giliran di-commit, kapan bicara, kapan berhenti | memilih isi ucapan |
| Composer | model + gate | isi ucapan dalam batas plan | menjalankan action |
| Owner model | Postgres | posisi dan aturan owner yang di-approve | diubah tanpa approval |
| Memory | Graphiti | apa yang terjadi di meeting lalu, dengan provenance | menjadi posisi owner |

Alur satu turn:

1. Adapter mengirim observasi (VAD, interim, final, chat, roster, layar) ke
   kernel. Kernel mereduksi fakta dan meneruskan event giliran ke floor
   actor.
2. Floor actor meminta judge pada interim (cache), dan meng-commit giliran
   dari VAD lokal (B2).
3. Kernel membuat plan dari judgement dan otorisasi, termasuk evidence yang
   dibutuhkan dan model tier.
4. Floor actor menjalankan plan: action dan evidence paralel, opener bila
   perlu, lalu composer.
5. Composer menghasilkan klaim terstruktur lalu teks; gate memverifikasi
   klaim; kalimat yang lolos masuk antrean playback streaming.
6. Setiap langkah mencatat `causation_id` ke langkah sebelumnya.

## Invariant

1. Satu pemilik per state, sesuai tabel arsitektur. Tidak ada salinan yang
   ditulis paralel.
2. Apa yang dilakukan dan diucapkan twin hanya diputuskan oleh plan kernel
   dan composer di dalam batas plan. Floor actor hanya memutuskan waktu.
   Runtime, perception, dan audio hanya mengirim observasi; tidak ada jalur
   yang memanggil conversation langsung.
3. Setiap ucapan punya rantai `causation_id` dari observasi pemicu sampai
   playback, plus digest input setiap keputusan model.
4. Setiap klaim yang diucapkan punya basis bertipe. Klaim tanpa basis yang
   valid tidak diucapkan.
5. Twin berbicara sebagai owner hanya di atas posisi owner yang di-approve
   atau memory tentang owner yang punya provenance. Pengalaman pribadi tidak
   pernah dikarang.
6. Kebutuhan latency diselesaikan oleh arsitektur (streaming, paralel,
   lokal), bukan oleh timeout yang dipendekkan.
7. Kegagalan adapter menurunkan kemampuan, tidak mengakhiri sesi, kecuali
   meeting memang selesai, twin dikeluarkan, owner menghentikan, atau
   recovery habis.

## Baseline

Angka dari journal dan record di `main` sebelum plan ini. Kolom "cek"
menandai angka yang sudah dihitung ulang langsung dari journal; angka lain
berasal dari audit dan dihitung ulang otomatis oleh report A0 sebelum
dipakai sebagai gate.

| Metrik | Baseline | Sumber | Cek | Target |
| --- | --- | --- | --- | --- |
| VAD stop → transcript final | median 2,64 s | live-oue-4 | belum | commit lokal p50 ≤ 0,5 s |
| Final → audio pertama | median 2,57 s, ekor 13-22 s | live-oue-4 | belum | p50 ≤ 1,0 s, p90 ≤ 2,0 s |
| Orang selesai → twin bersuara | sekitar 5 s | gabungan | belum | p50 ≤ 1,5 s, p90 ≤ 2,5 s |
| Barge-in → suara twin turun | 2,2-2,9 s | live-oue-4 | belum | ≤ 0,3 s |
| Barge-in ditolak | 44 | live-oue-4 | ya | diukur ulang setelah B3 |
| Early commit | 2 dari 30 turn | live-oue-4 | ya | diganti B2 |
| Jeda antar kalimat twin | 0,78-0,80 s | live-oue-4 | belum | ≤ 0,15 s |
| Final terikat ke speaker | 0 dari 236 | live-oue-4 | ya | ≥ 90% |
| Sesi `browser_failure` tanpa rejoin | 16 dari 55 | semua sesi | belum | 0 |
| `session.json` beda dengan journal | 51 dari 54 (oue-4: `null`) | semua sesi | sebagian | tidak ada salinan kedua |
| Kontribusi terucap | 0 dari 34 plan | semua journal | ya | ≥ 1 per 20 menit pada skenario relevan |
| Penolakan `invented_number` | 5 | live-oue-4 | ya | 0 untuk angka yang terlihat di layar |
| Screen read per satu kali share | 27 read, 8 duplikat | live-oue-4 | ya | ≤ 5 read, ≤ 3 s setelah settle |
| Memory prefetch | 0 | live-oue-4 | ya | recall set aktif sepanjang meeting |
| Prompt sistem | 2.730 kata, 41 "never" | live-oue-4 | belum | ≤ 700 kata |

## Track A: bertahan di meeting

### A0: Rantai causation, digest input, dan metrik

Dikerjakan pertama karena semua track lain divalidasi lewat journal.

- Setiap observasi kernel membawa `event_id`. Setiap keputusan (judge, plan,
  reply, command, sentence, playback) membawa `causation_id` ke event yang
  memicunya dan `correlation_id` turn.
- Setiap panggilan model mencatat digest input: model, hash prompt, hash
  window, jumlah token, dan id fakta yang dipakai. Teks prompt tidak
  disimpan, kecuali flag debug owner.
- `playback_started`, `agent_finished`, dan `session_finished` selalu
  membawa `transcript_id` dan `detail`.
- `report.py` membaca rantai ini, bukan merekonstruksi dari lima timeline,
  dan menghitung semua metrik di tabel baseline.

Gate: untuk setiap ucapan di sesi uji, report menampilkan rantai lengkap
dari final sampai playback tanpa heuristik waktu; report menghitung ulang
baseline dari journal oue-1 sampai oue-4.

### A1: Satu sumber status sesi dan lease

- Tabel Postgres `meeting_sessions`: `meeting_id`, `desired_state`
  (`joined`, `stopped`), `phase`, `attempt_id`, `browser_epoch`,
  `lease_owner`, `lease_expires_at`, `terminal_reason`, `detail`,
  `updated_at`.
- Lease pindah ke baris ini: diambil dan diperpanjang dengan update
  bersyarat (`lease_owner` dan `lease_expires_at`). Lease Redis dihapus.
- API hanya menulis `desired_state`. Pemegang lease satu-satunya penulis
  `phase`, `terminal_reason`, dan `detail`.
- Lifecycle sesi menjadi state machine enum in-process. Dihapus: LangGraph
  lifecycle beserta checkpoint Postgres dan `DegradingCheckpointer`
  (`orchestration/lifecycle.py`, 742 baris), `session.json` sebagai status,
  dan `set_phase` di API. Browser dan PulseAudio tidak bisa di-resume dari
  checkpoint, jadi durable workflow di sana tidak membeli apa-apa.
- Journal dan event API adalah projection dari transisi tabel.

Gate: tidak ada kode yang menulis status sesi selain pemegang lease; API
`GET /sessions` membaca tabel; sesi uji menghasilkan `terminal_reason` yang
sama di tabel, journal, dan report.

### A2: Reconciler dan recovery

- Worker mengklaim baris `meeting_sessions` yang `desired_state=joined`,
  belum terminal, dan lease-nya kosong atau kedaluwarsa. Join command di
  Redis stream dihapus; tabel adalah antrean kerja.
- Lease yang gagal diperbarui menghentikan sesi lokal segera. Dua worker
  tidak pernah berada di meeting yang sama.
- Crash browser, page close, atau watchdog timeout di worker yang sama:
  browser epoch baru, rejoin dalam attempt yang sama, maksimal tiga kali per
  sepuluh menit.
- Worker mati dan worker lain mengambil alih: attempt baru. Kernel attempt
  baru di-rehydrate dari `record.jsonl` (dialog dan outcome command); plan
  yang belum terminal ditutup, bukan dilanjutkan, dan command yang
  `dispatching` ditutup `unknown` oleh ledger recovery yang sudah ada.
  World (roster, layar) diobservasi ulang dari Meet.
- Hanya `ended`, `removed`, owner stop, deadline lobby, atau recovery yang
  habis yang terminal.
- Kegagalan provider (`ProviderFault`, `OSError`) menurunkan mode, tidak
  melepas lease tanpa status.
- Dihapus: `POST /resume` manual sebagai satu-satunya jalan kembali.

Ketergantungan: pengambilalihan antar worker membutuhkan `record.jsonl`
yang bisa dibaca worker lain. Di compose ini volume bersama; D2 menggantinya
dengan unggahan segmen berkala.

Gate: tes fault injection untuk kill worker, kill browser, lease hilang,
dan Redis restart; setiap kasus berakhir dengan sesi yang kembali di meeting
atau status terminal yang jujur. Live: kill Chromium di tengah meeting dan
twin kembali dalam 60 detik.

### A3: Join dan lobby

- Knock sekali. Hanya kegagalan sebelum knock (navigasi, nama, tombol join)
  yang di-retry. Menunggu di lobby adalah state, bukan kegagalan.
- Satu deadline lobby dari brief atau settings.
- Admission dideteksi dari protocol (status device sendiri, A4), bukan teks
  halaman.
- Dihapus: `timeout` di `JOIN_RETRYABLE_STATUSES`.

Gate: tes lobby dengan admit di menit ke-6 berhasil tanpa knock ulang.

### A4: Status call dari protocol

- `in_call`, `removed`, `ended`, dan `left` diturunkan dari status device
  sendiri di protocol dan event koneksi.
- DOM hanya probe tombol control bar dengan role atau aria-label, tidak
  pernah teks seluruh halaman, dan hanya sebagai fallback saat protocol
  stale.
- Dihapus: substring match `page.content()` di `observe_call_state`.

Gate: chat "the sprint has ended" dengan panel chat terbuka tidak mengubah
status call.

### A5: Kebijakan dialog menurut struktur

- Dialog dengan satu tombol acknowledge atau close, atau yang tertutup
  dengan Escape, ditutup otomatis dan dicatat.
- Eskalasi ke owner hanya jika dialog meminta input yang memblokir audio,
  presence, atau call, dan status call memburuk.
- Dihapus: allow-list teks Inggris persis di `watchdog.py`.

Gate: fixture skin tone picker, notice sign-in, dan countdown free-plan
tidak mengakhiri sesi.

## Track B: ritme bicara manusia

### V0: Replay harness

Gate B dan C butuh replay yang tidak bergantung pada sesi live.

- Harness memutar ulang timeline observasi dari journal dan record (VAD,
  interim, final, chat, roster, layar) ke kernel dan floor actor dengan
  clock simulasi. Model bisa diganti fake deterministik atau model asli.
- Fixture: oue-1 sampai oue-4.

Gate: replay oue-4 dengan komponen sekarang mereproduksi metrik baseline
dalam toleransi 10%.

### B5: TTS dan playback streaming

Dikerjakan sebelum B1 karena barge-in (B3) butuh pause dan flush.

- Satu websocket TTS persisten per sesi dengan context continuation, satu
  writer audio berumur panjang dengan buffer lookahead, dan stop berarti
  flush buffer.
- LLM stream ditarik terus-menerus, bukan menunggu kalimat sebelumnya
  selesai diputar.
- Dihapus: request `/tts/bytes` per kalimat, proses `paplay` dan toggle
  mute per klip.

Gate: jeda antar kalimat ≤ 150 ms; stop sampai sunyi ≤ 100 ms; pause dan
resume tanpa artefak.

### B1: Floor actor

- Satu actor asyncio memiliki commit giliran, state barge-in, generation
  playback, dan lease bicara. Input: event VAD, interim, final, chat,
  keputusan judge, plan dari kernel. Output: perintah ke playback dan
  laporan ke kernel.
- Semua input giliran, termasuk chat, masuk lewat kernel. Runtime tidak lagi
  memanggil `conversation.receive_chat` atau jalur attention langsung.
- LangGraph dikeluarkan dari jalur real-time. Judge dan composer tetap
  panggilan model biasa yang dipanggil actor.
- Dihapus: `TranscriptDrain` arbitration, `EarlyTurns`, `BargeLatch`,
  `SpeechFloor` dan `AdmittedFinals` generation, `PendingFinals`,
  `DeferredTurns`, `QuietFloor`, `turn_graph` dan `participation_graph`
  sebagai StateGraph, serta `TurnRuntime`.

Gate: satu modul pemilik state turn; tes property untuk urutan event acak
tidak pernah menghasilkan dua playback aktif atau playback setelah barge-in
yang di-yield; replay V0 tidak memburuk.

### B3: Barge-in dari suara

- Audio capture hanya berisi suara remote, jadi VAD onset selama twin
  bicara adalah suara orang lain. Onset menurunkan volume atau menghentikan
  sementara playback dalam ≤ 300 ms.
- Judge memutuskan yield (buang sisa ucapan) atau resume setelah kata-kata
  masuk. Batuk dan "hmm" tidak menghentikan twin.
- Dihapus: syarat 3 kata STT (`barge_in_min_words`) sebelum aksi apa pun.

Gate: live, interupsi dengan kalimat penuh membuat twin diam dalam 300 ms;
backchannel tidak menghentikan twin.

### B2: Commit giliran lokal

- Floor actor meng-commit giliran saat VAD stop dan interim stabil, setelah
  sekitar 300-500 ms hening, jika judge (yang sudah berjalan pada interim)
  menyatakan baris selesai dan untuk twin. Setelah B4, VAD dan interim per
  slot.
- Final STT hanya merekonsiliasi: jika final menambah isi material sebelum
  twin bicara, plan dibatalkan dan dinilai ulang; jika sudah bicara, final
  menjadi konteks turn berikutnya.

Gate: replay V0 oue-4 menunjukkan commit p50 ≤ 0,5 s setelah VAD stop
dengan false commit ≤ 5%.

### B4: Identitas dari stream

- Fleet per-stream yang sudah ada menjadi jalur utama: satu STT per slot
  penerima, identitas dari CSRC yang dipetakan ke device lewat protocol.
  Slot bukan orang: pergantian CSRC di satu slot menjadi batas segmen.
  Voiceprint mengonfirmasi. Stream campuran menjadi fallback degraded.
- Cluster STT tidak di-reset per reconnect. Rotasi STT membuka socket baru
  sebelum menutup yang lama.
- Dihapus: flag `per_stream_enabled` dan atribusi tebakan dari level per
  slot yang di-poll (`voice_activity.py`) sebagai jalur utama.

Gate: ≥ 90% final terikat ke device pada sesi multi-human; tidak ada
deafness saat rotasi; biaya STT per menit dilaporkan.

### B6: Turn paralel

- Action dan evidence berjalan paralel. Opener pendek (dari
  `spoken_lines.json`, sesuai bahasa turn) diucapkan hanya ketika plan
  memuat action atau look yang diperkirakan lambat. Opener menggantikan
  thinking cue, bukan menambah lapisan kedua.
- Composer menerima evidence, fakta yang boleh disebut, dan angka yang
  di-ground sebelum generate, sehingga penolakan gate menjadi pengecualian.
- Tier model ditentukan saat plan, bukan setelah penolakan.
- Dihapus: `cue_when_slow` dan routing model setelah tool message.

Gate: final → audio pertama p50 ≤ 1,0 s; turn action-then-speech tidak
diam lebih dari 1,5 s.

### B7: Perception layar yang murah

- Frame dibaca setelah layar settle. OCR lokal dan hash teks menentukan
  sama, scroll, atau konten baru. Dokumen yang di-scroll adalah satu segmen
  yang teksnya bertambah. Pilihan model OCR (id dan en, onnxruntime)
  diputuskan lewat spike dengan ukuran image dan latency.
- LLM vision hanya untuk konten baru yang settle atau look eksplisit.
- Keyframe dipertahankan per segmen, bukan cache 48 frame global.

Gate: replay share spreadsheet oue-4 menghasilkan ≤ 5 read LLM; layar
terbaca ≤ 3 s setelah settle.

## Track C: otak

### C1: Owner model

- Tabel Postgres berversi milik owner: posisi per topik (stance, alasan,
  keyakinan, sumber, tanggal), preferensi, aturan keputusan, contoh gaya
  bicara, dan disclosure per item.
- Berbeda dari memory: owner model adalah apa yang owner pikirkan dan
  setujui; memory adalah apa yang terjadi di meeting, dengan provenance.
  Memory tidak pernah menjadi posisi owner tanpa approval (C7).
- Diedit owner lewat API dan CLI. Perubahan dari luar owner selalu berupa
  proposal yang menunggu approval.
- Persona JSON tinggal identitas dan suara. `verified_facts` yang berisi
  instruksi perilaku dipindah ke aturan keputusan; fakta tetap menjadi
  fakta.
- Sampai C5 ada, item yang masuk prompt dipilih berdasarkan topik brief
  dengan batas token; setelah C5, berdasarkan recall set.

Gate: owner dapat menambah posisi dan melihatnya dipakai di replay; prompt
tidak berisi item yang disclosure-nya `owner_only`.

### C2 dan C3: Kontribusi berbasis klaim dan gate per klaim

Satu PR, karena kontrak dan gate tidak bisa dipisah.

- `SpeechAct` diganti `SpeechPlan`: daftar klaim, masing-masing dengan
  `basis` bertipe (`owner_position`, `memory`, `brief_fact`,
  `meeting_live`, `screen`, `inference`) dan ref, `certainty` (`stated`,
  `guess`), plus `speaks_as` (`owner_proxy`, `twin`, `reporting`).
- Opini dan usulan sah jika basisnya posisi owner. Klaim berbasis
  `inference` wajib `certainty=guess`. Pertanyaan lanjut sah jika terkait
  objective brief atau posisi owner.
- Gate memverifikasi setiap klaim secara deterministik: ref ada, disclosure
  mengizinkan, angka dan tanggal cocok dengan teks basis, dan `speaks_as`
  sesuai basis. Grounding angka menjadi satu pemeriksaan per klaim, bukan
  gate tunggal.
- Layar menjadi sumber yang bisa dikutip (`screen:<ref>`) dengan atribusi
  presenter. Isi layar orang lain tidak bisa diucapkan sebagai orang pertama
  twin atau owner.
- Klaim orang pertama sebagai owner wajib berbasis posisi owner atau memory
  tentang owner. Pengalaman pribadi tanpa basis ditolak.
- Critic (model murah) hanya untuk kontribusi yang tidak diminta dan klaim
  opini: menilai daftar klaim yang keluar lebih dulu dari teks, apakah owner
  akan mengatakan ini dan apakah layak memotong, serta apakah dugaan
  diucapkan sebagai dugaan. Teks boleh di-generate sambil critic berjalan,
  tetapi playback menunggu verdict. Jawaban langsung tidak lewat critic.
- Dihapus: `uncited_contribution`, `question_not_in_brief`, dan katalog
  kontribusi dari `SourceRegistry` sebagai satu-satunya sumber.

Gate: kasus "baca baris 42" di layar lolos; spreadsheet orang lain sebagai
"kemarin saya ..." ditolak; "my day's been relaxed" ditolak; replay
skenario dengan posisi owner menghasilkan kontribusi terucap yang dinilai
judge independen owner-plausible ≥ 80%; nol klaim tanpa basis.

### C4: Prompt peran yang pendek

Setelah C2 dan C3, karena prosa hanya boleh dihapus setelah struktur yang
menegakkannya ada.

- Prompt sistem: peran, siapa owner, gaya owner, item owner model yang
  relevan, dan konteks meeting, ≤ 700 kata. Aturan yang ditegakkan struktur
  (gate, plan, otorisasi, critic) dihapus dari prosa.
- Gaya persona menang atas kode: follow-up question diizinkan bila critic
  setuju.
- Dihapus: `ENDING_RULE` dan penahanan kalimat tanya di `reply_planner`,
  kalimat kanned `CONFIDENTIALITY_RULE` yang bocor ke turn lain, dan aturan
  yang saling bertentangan di `SOURCE_RULES` dan `BRIEF_RULES`.

Gate: tes memastikan tidak ada aturan prosa yang menduplikasi gate; persona
eval dan trust eval lulus.

### C5: Memory sebagai listener

- Listener latar membaca final. Query recall adalah jendela bergulir
  beberapa final terakhir; recall dijalankan ulang saat jendela bergeser
  topik (jarak embedding) atau setiap beberapa final, dengan batas laju per
  menit.
- Hasilnya menjadi observasi `RecallObserved` dan recall set di kernel.
  Reply, judge, dan proactive membaca recall set; item owner model yang
  relevan dipilih dengan cara yang sama.
- Tidak ada recall yang memblokir turn.
- Dihapus: `memory_planned` dan prefetch middleware di jalur reply.

Gate: replay oue-4 menghasilkan recall untuk "update kemarin" sebelum
pertanyaan selesai; jalur reply tidak menunggu memory; biaya recall per jam
dilaporkan.

### C6: Proactive dari kernel

- Kandidat dibangun kernel dari objective dan pertanyaan brief, pertanyaan
  yang belum terjawab di dialog, ucapan yang bertentangan dengan posisi
  owner, recall set, dan commitment yang disebut.
- Kandidat lewat composer, gate, dan critic yang sama dengan C2.
- Quota dihitung hanya saat benar-benar diucapkan. Penyampaian menunggu
  celah giliran dari floor actor, bukan TTL tetap.
- Dihapus: `AttentionPrefilter` dengan trigger pergantian layar dan
  `offer_attention`/`_deliver_attention` di runtime.

Gate: replay oue-4 menyampaikan kontribusi proactive yang relevan saat
diskusi tentang tim owner; tidak ada proactive saat orang bicara.

### C7: Belajar dari debrief

- Debrief menghasilkan proposal owner model (posisi baru, koreksi, aturan)
  dengan kutipan bukti. Owner approve atau tolak. Approval menaikkan versi.
- Replay eval berjalan per versi owner model untuk mengukur dampak.

Gate: proposal yang di-approve dipakai di meeting berikutnya; proposal yang
ditolak tidak pernah dipakai.

## Track D: always-on

### D1: Desired state dan calendar

- Sync calendar owner (Google Calendar, izin OAuth owner) mengisi baris
  `meeting_sessions` dengan jadwal join, link Meet, brief default, dan
  deadline lobby.
- Controller mengubah `desired_state` sesuai jadwal; worker mengklaimnya
  lewat A2. API manual tetap menjadi sumber desired state lain.

Gate: meeting di calendar di-join tanpa perintah manual.

### D2: Satu pod per sesi

- Satu Kubernetes Job per sesi, dengan PulseAudio dan browser di dalam pod.
  Tidak ada env audio global.
- Record dan journal ditulis lokal di pod lalu diunggah ke object storage
  per segmen berkala (target ≤ 10 s) dan saat terminal, sehingga
  pengambilalihan A2 kehilangan paling banyak satu segmen. Registry sesi di
  Postgres, bukan listing direktori.
- Redis hanya untuk sinyal ephemeral.

Gate: dua meeting bersamaan berjalan di dua pod tanpa saling mengganggu
audio; pengambilalihan setelah pod dibunuh memakai record terakhir yang
terunggah.

### D3: Config yang menyusut

- `Settings` sub-model dioper langsung ke komponen. Kebijakan per meeting
  hanya di brief.
- Dihapus: `SessionConfig`, `JoinRequest` sebagai salinan settings, dan
  flag `enabled` untuk jalur yang sudah menjadi satu-satunya jalur.

Gate: jumlah field settings dan key `.env.example` turun; tidak ada field
yang disalin tangan antar lapisan.

## Urutan PR

Urutan mengikuti ketergantungan: A membuat sesi bisa divalidasi, B membuat
twin terasa manusia, C membuat twin bernilai, D membuatnya berjalan
sendiri. Design C dan D boleh ditulis lebih awal; implementasinya menunggu
gate A.

1. A0 rantai causation dan metrik.
2. A1 satu sumber status sesi dan lease.
3. A4 status call dari protocol.
4. A3 join dan lobby (butuh A4).
5. A5 kebijakan dialog.
6. A2 reconciler dan recovery (butuh A1).
7. Live validation A: tiga sesi multi-human 30 menit, satu dengan kill
   Chromium, satu dengan restart worker.
8. V0 replay harness.
9. B5 TTS dan playback streaming.
10. B1 floor actor.
11. B3 barge-in dari suara (butuh B5, B1).
12. B2 commit giliran lokal (butuh B1).
13. B4 identitas dari stream.
14. B6 turn paralel.
15. B7 perception layar.
16. Live validation B: metrik latency dan barge-in di tabel baseline.
17. C1 owner model.
18. C2 dan C3 kontribusi dan gate per klaim.
19. C4 prompt peran (butuh C2 dan C3).
20. C5 memory listener.
21. C6 proactive dari kernel (butuh C1, C5, B1).
22. C7 belajar dari debrief (butuh C1).
23. Live validation C: skenario kontribusi dan review owner.
24. D3 config.
25. D1 calendar (butuh A2).
26. D2 pod per sesi.

Setiap PR: tes regresi untuk perilaku yang diganti, gate penuh, review
independen atas diff, squash merge setelah CI hijau.

## Validation

- Replay (V0): journal dan record oue-1 sampai oue-4. Setiap PR yang
  mengubah jalur real-time atau composer melaporkan angka replay sebelum dan
  sesudah.
- Live: sesi multi-human dengan skenario tetap (menyebut nama twin,
  pertanyaan state, permintaan action, chat, pertanyaan layar, diskusi yang
  menyentuh posisi owner, interupsi, izin leave).
- Owner review: setiap kontribusi terucap di live validation C dinilai owner
  sebagai "akan saya katakan", "tidak akan saya katakan", atau "salah".
- Judge eval independen (`EVAL_JUDGE_*`), tidak pernah model yang dinilai.

## Alternatif yang ditolak

- Menurunkan timeout STT cloud untuk mempercepat akhir giliran: mengurangi
  latency tetapi memotong pembicara yang jeda, dan tetap bergantung pada
  provider.
- Menambah aturan prompt untuk opini dan identitas: prompt sudah 2.730 kata
  dengan aturan bertentangan; masalahnya bentuk kontrak, bukan kurang
  aturan.
- Mempertahankan LangGraph lifecycle dengan checkpoint: resource yang
  dijaganya tidak dapat di-resume.
- Mempertahankan join command dan lease di Redis dengan persistence
  dinyalakan: tetap dua sumber status (Redis dan Postgres) untuk satu sesi.
- Satu worker banyak sesi dengan PulseAudio virtual per sesi: mungkin, tapi
  isolasi pod lebih sederhana dan sesuai target Kubernetes.
- Critic untuk setiap jawaban: menambah latency pada jalur yang paling
  sensitif tanpa manfaat, karena jawaban langsung sudah dibatasi plan dan
  gate.
- Belajar otomatis dari transcript tanpa approval: risiko twin mengadopsi
  posisi yang bukan milik owner.

## Risiko

- Commit giliran lokal dapat memotong orang yang berhenti sejenak. Mitigasi:
  judge "finished" plus rekonsiliasi final, diukur false commit rate.
- Per-stream STT melipatgandakan biaya STT (hingga empat slot).
- Critic menambah latency pada kontribusi. Kontribusi menunggu celah
  giliran, jadi latency di sana lebih longgar daripada jawaban langsung.
- Rehydrate kernel dari record dapat menghidupkan plan yang sudah tidak
  relevan setelah jeda. Plan yang belum terminal saat pengambilalihan
  ditutup, tidak dilanjutkan.
- Owner model berisi posisi sensitif. Disclosure per item dan audit akses
  wajib.
- Meet dapat mengubah protocol. Adapter protocol tetap terisolasi dengan
  tes fixture.

## Kill criteria

Track dihentikan dan didesain ulang bila:

- A: setelah A2, lebih dari satu dari lima sesi live masih berakhir tanpa
  recovery yang jujur.
- B: setelah B1-B3, orang selesai → twin bersuara p50 masih di atas 2,5 s,
  atau false commit di atas 10%.
- C: setelah C2-C3, review owner menilai kurang dari 60% kontribusi "akan
  saya katakan", atau ada satu klaim orang pertama yang dikarang.
- D: pod per sesi tidak dapat join dalam 60 detik dari jadwal.
