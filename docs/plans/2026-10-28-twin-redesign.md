# Twin Redesign

Status: berjalan. A0 (#46) dan A1a (#47) selesai. Revisi 2 (2026-10-28):
setiap langkah diperiksa ulang terhadap library yang terpasang; lihat
"Riset library".
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

## Riset library

Revisi 1 ditulis dari audit kode tanpa memeriksa apa yang sudah disediakan
library. Revisi 2 memeriksa source versi terpasang dan dokumentasi resmi.
Aturannya: fitur library dipakai bila menjawab kebutuhan; kode sendiri hanya
bila tidak ada yang cocok, dan alasannya dicatat.

| Library | Fitur | Dipakai di | Keputusan |
| --- | --- | --- | --- |
| SQLAlchemy 2.1 + psycopg 3 | engine async `postgresql+psycopg_async`, `with_for_update(skip_locked=True)`, `RETURNING` | A1a-r, A2 | pakai; hapus `asyncio.to_thread` |
| tenacity 9.1 | `AsyncRetrying`, `wait_exponential`, `before_sleep_log` | A1a-r | pakai untuk retry tulis status |
| FastAPI / Starlette | lifespan state, `Depends`, `dependency_overrides` | A1a-r | pakai; hapus singleton `app.state` dan factory `ApiDeps.session_status` |
| redis-py 6.4 | `xautoclaim` | A1a-r | pakai; ganti `xpending_range` + `xclaim` |
| LangGraph 1.2 | checkpoint, durability `sync/async/exit`, `RetryPolicy`, `RunControl` | A1b, B1 | tidak untuk sesi meeting: resume hanya menjalankan ulang node dan tidak memulihkan browser, audio, atau admission; drain hanya di batas step. Tetap untuk debrief (`@entrypoint`/`@task`) dan composer |
| langchain middleware | `wrap_model_call` + `context_schema`, `ModelRetryMiddleware`, `ModelFallbackMiddleware` | B6 | tier model dari context plan; satu lapis retry |
| LangSmith 0.14 | `Client(hide_inputs=callable)`, mode `otel` | A0 | belum; dipakai bila ada backend trace. Journal tetap sumber, karena VAD, STT, TTS, dan playback bukan run LangChain |
| OpenTelemetry SDK | span lintas proses | D2 | ditambahkan saat api, worker, dan pod terpisah, untuk latency lintas proses |
| Playwright 1.63 | locator role/aria, `wait_for`, `aria_snapshot()` | A3, A4, A5 | pakai. `add_locator_handler` tidak cocok: hanya jalan saat ada action atau assertion. `page.on("websocket")` tidak melihat RTCDataChannel, jadi bridge RTC tetap |
| Pydantic 2.13 | `TypeAdapter.validate_json(..., experimental_allow_partial="trailing-strings")` | C2/C3 | pakai untuk klaim parsial yang tervalidasi |
| Graphiti 0.30 | `entity_types`/`edge_types`, `SearchFilters(edge_types=...)`, recipe `EDGE_HYBRID_SEARCH_RRF`, `center_node_uuid` | C1, C5, C6, C7 | pakai. Tidak sebagai owner model: ekstraksi LLM mengubah kata dan dapat menginvalidasi edge, tanpa versi atau approval |
| LangGraph `PostgresStore`, LangMem | store key-value, memori otomatis | C1, C5 | tidak: tanpa versi, approval, disclosure; LangMem menulis memori sendiri |
| langchain-core | `InMemoryRateLimiter` | C5 | pakai untuk batas laju recall |
| deepeval 4.2 | GEval | C2/C3 | judge offline owner-plausible, bukan runtime |
| Smart Turn v3.2 (Pipecat) | model ONNX akhir giliran berbasis prosodi, 23 bahasa termasuk Indonesia | B2 | vendor model, jalankan dengan `onnxruntime` yang sudah ada |
| Silero VAD v5 | sudah dipakai (port dari Pipecat) | B3 | tetap |
| Cartesia / ElevenLabs websocket | context, `continue`, flush, timestamp | B5b | pakai; `cancel` Cartesia membatalkan seluruh context, jadi stop membuang buffer lokal |
| `pasimple` atau `pacat` panjang | writer PulseAudio berumur panjang dengan flush | B5a | diputuskan lewat spike; `pasimple` 0.0.3 jarang dirawat |
| Pipecat / LiveKit Agents | framework voice penuh | B1-B6 | tidak dipakai sebagai dependency: turn controller mereka bertabrakan dengan kernel dan judge; LiveKit bagian terbaiknya cloud atau berlisensi khusus. Yang diambil hanya model Smart Turn yang di-vendor, dan source Pipecat sebagai rancangan acuan |
| RapidOCR 3.10, imagehash | OCR ONNX, hash persepsi | B7 | spike dulu; kunci sama/scroll/baru dari hash teks OCR |
| google-api-python-client | `events.list` + `syncToken` | D1 | pakai polling; push `watch` belum (butuh HTTPS publik, channel kedaluwarsa, tidak 100% andal) |
| kubernetes client | Job: `backoffLimit: 0`, `activeDeadlineSeconds`, `ttlSecondsAfterFinished` | D2 | pakai; kopf/CRD tidak, karena tabel sesi sudah desired state |
| pydantic-settings 2.15 | sub-model, `model_copy(update=...)`, `secrets_dir` | D3 | pakai; hapus salinan field |
| procrastinate | antrean job Postgres dengan retry, lock, stalled job | D4 | pakai untuk worker job; tidak untuk A2 karena membuat tabel status kedua |

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

Angka dari journal dan record di `main` sebelum plan ini, dihitung ulang
oleh `observability/baseline.py` (A0). Audit awal menyebut VAD stop →
final 2,64 s; hitungan ulang dengan pasangan VAD stop terakhir sebelum final
memberi 1,30 s. Kolom "cek"
menandai angka yang sudah dihitung ulang langsung dari journal; angka lain
berasal dari audit dan dihitung ulang otomatis oleh report A0 sebelum
dipakai sebagai gate.

| Metrik | Baseline | Sumber | Cek | Target |
| --- | --- | --- | --- | --- |
| VAD stop → transcript final | p50 1,30 s, p95 3,0 s | live-oue-4 | ya | commit lokal p50 ≤ 0,5 s |
| Final → PCM pertama | p50 2,08 s, p95 8,2 s | live-oue-4 | ya | p50 ≤ 1,0 s, p90 ≤ 2,0 s |
| Final → audio pertama | tidak terukur: journal oue-4 lebih tua dari A0, jadi event playback di sana belum membawa `transcript_id`; kodenya sudah membawa sejak A0 | live-oue-4 | ya | p50 ≤ 1,0 s, p90 ≤ 2,0 s |
| Orang selesai → twin bersuara | sekitar 3,4 s (1,30 + 2,08) | gabungan | ya | p50 ≤ 1,5 s, p90 ≤ 2,5 s |
| Barge-in → suara twin turun | p50 2,95 s dari onset VAD, dipasangkan per waktu karena journal lama tidak membawa turn yang dipotong | live-oue-4 | ya | ≤ 0,3 s |
| Barge-in ditolak | 44 | live-oue-4 | ya | diukur ulang setelah B3 |
| Early commit | 2 dari 30 turn | live-oue-4 | ya | diganti B2 |
| Jeda antar kalimat twin | 0,78-0,80 s, terpecah jadi 0,50 s tail playback dan 0,29 s first chunk TTS; `sentence_gap_ms` cuma mengukur komponen 0,50 s | live-oue-4 | ya, dihitung manual dari journal | ≤ 0,15 s |
| Final terikat ke speaker | 0 dari 236 | live-oue-4 | ya | ≥ 90% |
| Sesi `browser_failure` tanpa rejoin | 16 dari 54 | semua sesi | ya | 0 |
| `session.json` beda dengan journal | 51 dari 54 (oue-4: `null`) | semua sesi | sebagian | tidak ada salinan kedua |
| Kontribusi terucap | 0 dari 34 plan | semua journal | ya | ≥ 1 per 20 menit pada skenario relevan |
| Penolakan `invented_number` | 5 | live-oue-4 | ya | 0 untuk angka yang terlihat di layar |
| Screen read per satu kali share | 27 read, 8 duplikat | live-oue-4 | ya | ≤ 5 read, ≤ 3 s setelah settle |
| Memory prefetch | 0 | live-oue-4 | ya | recall set aktif sepanjang meeting |
| Prompt sistem | 2.730 kata, 41 "never" | live-oue-4 | belum | ≤ 700 kata |

## Track A: bertahan di meeting

### A0: Rantai causation, digest input, dan metrik

Dikerjakan pertama karena semua track lain divalidasi lewat journal.

- Kunci turn adalah `transcript_id` final pemicunya. Setiap event di
  rantai turn membawanya: `policy_decided`, `plan_selected`,
  `agent_started`/`agent_finished`, `model_route_selected`,
  `reply_plan_observed`, `answer_sentence_emitted`, `synthesis_*`, dan
  `playback_*`. Event per kalimat juga membawa `sentence_index`; action
  membawa `plan_id`; barge-in membawa `interrupted_transcript_id`.
- Setiap panggilan model sesi (reply, strong, fallback, judge, perception)
  dicatat oleh callback LangChain sebagai `model_input_recorded`: slot,
  model, digest system prompt, digest window, jumlah pesan dan karakter, dan
  `transcript_id`/`request_id` dari metadata bila ada. Teks prompt tidak
  disimpan. Panggilan judge belum punya kunci turn karena dinilai pada
  interim; digest system-nya cukup untuk membedakan judge, moment, dan
  grounding.
- `session_finished` membawa `detail`.
- `observability/baseline.py` menghitung metrik tabel baseline per sesi dan
  disertakan di report. Pasangan berbasis waktu hanya dipakai untuk VAD,
  yang memang tidak punya id.
- Thinking cue belum membawa kunci turn; B6 menggantinya dengan opener yang
  dibuat oleh turn.

Gate: untuk setiap ucapan di sesi uji, report menampilkan rantai lengkap
dari final sampai playback tanpa heuristik waktu; report menghitung ulang
baseline dari journal oue-1 sampai oue-4.

Revisi riset: journal tetap sumber kebenaran. `model_input_recorded` juga
mencatat `parent_run_id` agar panggilan model terhubung tanpa kunci sendiri.
Bila backend trace dipasang, digest dipindah ke `Client(hide_inputs=...)`
LangSmith dengan ekspor OTel, dan callback sendiri dihapus.

### A1a: Satu sumber status sesi

- Tabel Postgres `meeting_sessions` (migrasi `0002`): `meeting_id`,
  `owner_id`, `desired_state` (`created`, `joined`, `stopped`), `phase`,
  `terminal_reason`, `detail`, `attempt_id`, `created_at`, `updated_at`.
- API hanya membuat baris dan menulis `desired_state`. Worker satu-satunya
  penulis `phase`, `terminal_reason`, dan `detail`, lewat `StatusWriter`
  yang menulis berurutan di thread agar loop real-time tidak pernah
  menunggu database.
- Phase terminal hanya ditulis oleh hasil akhir sesi yang membawa alasan;
  phase live yang terminal diabaikan, dan baris terminal tidak dapat
  ditimpa. Ini menutup bug `terminal_reason` hilang (51 dari 54 sesi).
- `session.json` tinggal snapshot config, tanpa status. `set_phase`
  dihapus. `PublicPhase` dan `TERMINAL_PHASES` satu sumber di
  `foundation/taxonomy`.

Gate: tidak ada kode yang menulis phase selain worker; view API dan SSE
membaca tabel; hasil akhir sesi menyimpan alasan dan detail walau phase
live sudah `ended`.

### A1a-r: Koreksi A1a dari riset

- Repository status async dengan `create_async_engine`
  (`postgresql+psycopg_async`); `asyncio.to_thread` di API dan worker
  dihapus. Pemeriksaan skema lewat `connection.run_sync(require_current)`.
- Antrean berurutan `StatusWriter` tetap (tidak ada library untuk itu);
  loop retry diganti `tenacity.AsyncRetrying`.
- API: engine dibuat di lifespan Starlette, repository di-inject dengan
  `Depends`, test memakai `dependency_overrides`. Dihapus: singleton lazy di
  `app.state` dan `ApiDeps.session_status`.
- `StreamTransport.claim_stale` memakai `xautoclaim`.

Gate: tidak ada `asyncio.to_thread` untuk status; test API memakai
`dependency_overrides`.

### A1b: Lifecycle tanpa LangGraph

- Lifecycle sesi menjadi state machine enum in-process (`apply_transition`
  tetap). Dihapus: graph LangGraph lifecycle, checkpoint Postgres dan
  `DegradingCheckpointer`, `LifecycleThread`, `initialize_lifecycle`,
  `resume_lifecycle`, dan `RetryPolicy` LangGraph untuk join. Browser dan
  PulseAudio tidak bisa di-resume dari checkpoint, jadi durable workflow di
  sana tidak membeli apa-apa.
- Resume membaca baris `meeting_sessions`; forget menghapus baris itu
  sebagai ganti thread checkpoint. Checkpoint tetap hanya untuk chat API
  owner.

Gate: tidak ada import LangGraph di lifecycle sesi; resume dan forget lulus
tanpa checkpoint lifecycle.

Implementasi A5 (2026-10-31): dialog dibaca lewat `aria_snapshot()`; baris
role dihitung (`button`, dan role input: textbox, combobox, checkbox, radio,
switch, slider, spinbutton, searchbox, listbox, tab, option, treeitem, menu
item centang), dan fingerprint adalah hash kerangka role saja, sehingga
countdown yang berdetak atau form yang sedang diisi tetap satu dialog. Dialog tanpa input selalu dicoba ditutup dengan Escape; bila tidak
tertutup dan hanya punya satu tombol, tombol itu diklik (lewat xpath karena
klik CloakBrowser tidak menerima locator berantai). Dialog lain dicatat
`modal_observed` tanpa eskalasi. Dialog dengan input baru dieskalasi ke owner
jika status call bukan `in_call` atau `unknown`. Dialog dicari ulang menurut
fingerprint sebelum setiap aksi, karena indeks bergeser setelah satu dialog
ditutup.

### A2: Reconciler dan recovery

- Lease pindah ke baris `meeting_sessions` (`lease_owner`,
  `lease_expires_at`), diambil dan diperpanjang dengan update bersyarat.
  Lease Redis dihapus.
- Worker mengklaim baris yang `desired_state=joined`, belum terminal, dan
  lease-nya kosong atau kedaluwarsa. Join command di Redis stream dihapus;
  tabel adalah antrean kerja.
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

Revisi riset: klaim baris dalam satu statement `UPDATE ... WHERE
meeting_id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING`, dengan
jam database (`now()`), bukan jam worker. `attempt_id` menjadi fencing
token: perpanjangan lease dan `record_phase` difilter `lease_owner` dan
`attempt_id`, sehingga worker lama tidak dapat menimpa setelah diambil alih.
Advisory lock tidak dipakai (terikat satu koneksi). Bangun reconciler
dengan polling 2-5 s; LISTEN/NOTIFY psycopg opsional.

Implementasi A2a (2026-10-31): migration 0003 menambah `lease_owner`,
`lease_expires_at`, dan `attempts`. Worker mengklaim satu baris dengan
`UPDATE ... WHERE meeting_id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)`
memakai `now()` database; klaim memberi `attempt_id` baru dan menaikkan
`attempts`. Lease 30 detik diperpanjang tiap 2 detik; perpanjangan juga
membawa `desired_state`, jadi stop owner tidak lagi lewat Redis. Lease yang
direbut menghentikan sesi lokal segera; gagal menghubungi database baru
menghentikannya setelah lease lokal habis. `record_phase` difilter
`attempt_id`. Kegagalan yang bisa diulang (OSError, ProviderFault, record
terkunci) tidak melepas lease, sehingga klaim berikutnya menunggu lease
kedaluwarsa; lebih dari lima attempt berakhir `failed`/`infra_failure`.
Command join/stop/resume dan lease Redis untuk sesi dihapus; Redis tetap
untuk event SSE, override key, dan job ingestion/debrief/reembed. A2b
(rejoin browser di attempt yang sama) dan A2c (rehydrate kernel dari
`record.jsonl`) menyusul.

Implementasi A2b (2026-10-31): crash browser di dalam call membuka epoch
browser baru dan join lagi, maksimal tiga kali per sepuluh menit
(`REJOIN_LIMIT`, `REJOIN_WINDOW_SECONDS`); tiap rejoin mencatat event
`browser_rejoined`. Crash saat leaving tetap terminal, dan crash yang terjadi
setelah jatah habis mengakhiri sesi sebagai `browser_failure` dengan detail
crash terakhir. Siklus hidup sesi di-reset ke `created` tiap rejoin, jadi
phase, reason, dan detail terminal ikut hilang.

### A3: Join dan lobby

- Knock sekali. Hanya kegagalan sebelum knock (navigasi, nama, tombol join)
  yang di-retry. Menunggu di lobby adalah state, bukan kegagalan.
- Satu deadline lobby dari brief atau settings.
- Admission dideteksi dari protocol (status device sendiri, A4), bukan teks
  halaman.
- Dihapus: `timeout` di `JOIN_RETRYABLE_STATUSES`.

Gate: tes lobby dengan admit di menit ke-6 berhasil tanpa knock ulang.

Revisi riset: polling `page.content()` di `_await_admission` dan
`leave_meeting` diganti `asyncio.wait` atas sinyal protocol dan
`get_by_role(...).wait_for(state="visible"|"detached", timeout=...)`.

Implementasi (2026-10-31): `JoinResult.knocked` menandai hasil setelah tombol
join diklik; retry lifecycle hanya untuk `failed` yang belum knock, jadi
`timeout` dan kegagalan setelah knock tidak pernah knock ulang. Admission
dibaca dulu dari protocol (status device sendiri `1` dengan peer `connected`,
lihat probe A4: `3` = menunggu di lobby); fallback halaman tetap membaca
lobby karena di sana belum ada panel chat. Baca halaman yang gagal di lobby
baru dianggap gagal setelah lima kali berturut-turut.

### A4: Status call dari protocol

- `in_call`, `removed`, `ended`, dan `left` diturunkan dari status device
  sendiri di protocol dan event koneksi.
- DOM hanya probe tombol control bar dengan role atau aria-label, tidak
  pernah teks seluruh halaman, dan hanya sebagai fallback saat protocol
  stale.
- Dihapus: substring match `page.content()` di `observe_call_state`.

Gate: chat "the sprint has ended" dengan panel chat terbuka tidak mengubah
status call.

Revisi riset: hook RTCDataChannel (`assets/rtc_bridge.js`) tetap, karena
event websocket Playwright dan CDP tidak melihat data channel. Echo chat
sendiri dibaca dari field chat protocol bila tersedia, atau locator di dalam
daftar chat (`get_by_text(..., exact=True)`), bukan hitungan teks di seluruh
HTML.

Revisi implementasi (2026-10-31): protocol hanya pasti soal di dalam atau di
luar call. Status device sendiri `1` berarti di meeting; arti kode lain belum
terbukti (Attendee menebak `6` keluar, `7` di-remove). Karena itu protocol
memutuskan in/out (status device sendiri, ditambah `connectionState` semua
peer RTC), sedangkan alasan keluar (`removed`, `ended`, `left`) diambil dari
heading layar akhir. Kode status mentah dan state peer di-journal
(`perception_own_status_observed`, `perception_peer_state_observed`) untuk
probe live sebelum merge.

Hasil probe live 2026-10-31 (meeting a4-probe-1, a4-probe-2, a4-probe-4):
status device sendiri `3` saat menunggu di lobby, `1` setelah di-admit, dan
`7` baik saat host me-remove maupun saat host mengakhiri meeting untuk semua;
semua peer RTC menjadi `closed` di kedua kasus. Jadi `7` berarti dikeluarkan
oleh host, bukan khusus removed; alasannya hanya dari heading layar akhir:
"You've been removed from the meeting" dan "Your host ended the meeting for
everyone". Probe pertama berakhir `removed_by_host`, probe terakhir
`ended_by_host`. Status `3` dipakai A3 untuk admission. Fallback DOM membaca heading dan label tombol dari
satu snapshot yang diparse, tidak pernah teks bebas, sehingga deteksi
waiting room lewat teks div dibuang dari monitor; alur join tetap memakai
deteksinya sendiri sampai A3.

### A5: Kebijakan dialog menurut struktur

- Dialog dengan satu tombol acknowledge atau close, atau yang tertutup
  dengan Escape, ditutup otomatis dan dicatat.
- Eskalasi ke owner hanya jika dialog meminta input yang memblokir audio,
  presence, atau call, dan status call memburuk.
- Dihapus: allow-list teks Inggris persis di `watchdog.py`.

Gate: fixture skin tone picker, notice sign-in, dan countdown free-plan
tidak mengakhiri sesi.

Revisi riset: struktur dibaca dengan `locator.aria_snapshot()` dan hitungan
role (`button`, `textbox`, `combobox`); fingerprint dialog adalah hash
snapshot itu. Penutupan dengan `press("Escape")` lalu
`wait_for(state="hidden")`. `add_locator_handler` tidak dipakai karena hanya
berjalan sebelum action atau assertion, dan click cloakbrowser tidak lewat
jalurnya.

## Track B: ritme bicara manusia

### V0: Replay harness

Gate B dan C butuh replay yang tidak bergantung pada sesi live.

- Harness memutar ulang timeline observasi dari journal dan record (VAD,
  interim, final, chat, roster, layar) ke kernel dan floor actor dengan
  clock simulasi. Model bisa diganti fake deterministik atau model asli.
- Fixture: oue-1 sampai oue-4.

Gate: replay oue-4 dengan komponen sekarang mereproduksi metrik baseline
dalam toleransi 10%.

### B5a: Lookahead sintesis dan writer audio berumur panjang

Dikerjakan sebelum B1 karena barge-in (B3) butuh pause dan flush. B5a tidak
mengganti protokol TTS, jadi transport TTS yang ada tetap dipakai selama
B5a.

- LLM stream ditarik terus-menerus, bukan menunggu kalimat sebelumnya selesai
  diputar. Kalimat berikutnya disintesis sambil kalimat sekarang diputar.
- Satu writer audio berumur panjang dengan buffer lookahead, dipilih lewat
  spike antara `pasimple` (`pa_simple_flush`) dan `pacat` berumur panjang.
  Stop berarti flush buffer lokal.
- Dihapus: proses `paplay` per klip dan toggle mute per klip. Request TTS
  per kalimat tetap ada di B5a; penggantinya datang di B5b.

Gate: jeda antar kalimat ≤ 150 ms; stop sampai sunyi ≤ 100 ms; pause dan
resume tanpa artefak.

Alasan pemisahan, diukur dari journal `live-oue-4` (satu sesi, jadi indikasi
bukan bukti):

- Ada 9 jawaban multi-kalimat yang kalimatnya benar-benar keluar. Empat
  transcript lain punya kalimat kedua yang di-drop, jadi menurut niat ada 13.
  Dari 9 itu, 6 punya sintesis kalimat pertama selesai tercatat, dan 4 di
  antaranya punya jeda 2,7–4,3 s antara sintesis kalimat pertama selesai
  dan kalimat kedua keluar dari LLM. Dua sisanya lebih cepat: kalimat kedua
  keluar sebelum sintesis kalimat pertama selesai, jadi LLM sudah mendahului
  di sana. Jadi jeda tidak datang dari satu tempat saja.
- Jeda yang audible terukur di 3 kasus: 0,78–0,80 s antara
  `playback_finished` kalimat pertama dan PCM pertama kalimat berikutnya.
  Rinciannya 0,50 s tail menganggur di `PlaybackEngine._run`
  (`tail_seconds`) dan 0,29 s first chunk TTS.
- Penyebabnya di pipeline kita, bukan di protokol TTS, dan ada tiga. `_deliver`
  menarik chunk LLM berikutnya hanya setelah `sink.say` selesai, dan `say`
  baru selesai setelah playback kalimat itu selesai (atau di-drop), jadi
  kalimat berikutnya tidak pernah siap lebih awal. Audio klip ditarik malas:
  request TTS baru jalan saat klip mulai diputar, jadi first chunk TTS selalu
  ada di jalur kritis. `tail_seconds` 0,5 s menahan worker playback selama
  0,5 s tiap klip, dan queue justru paling sering kosong tepat saat itu.

Karena itu B5a netral provider: memperbaiki tiga hal itu tanpa ganti
protokol. First chunk TTS p50 519 ms untuk 24 sintesis yang punya
`transcript_id`, jadi selama sintesis belum mulai sebelum giliran playback,
gate 150 ms mustahil tercapai.

Batas bukti: semua angka di atas dari satu sesi dan dihitung manual dari
journal, bukan dari report. `sentence_gap_ms` di `observability/baseline.py`
sudah mengukur sisi playback, yaitu `playback_finished` ke
`playback_started`, jadi yang terukur baru komponen 0,50 s dan bukan jeda
audible 0,78–0,80 s yang utuh. Event playback di oue-4 belum membawa
`transcript_id` dan `sentence_index` karena sesi itu jalan sebelum A0, jadi
metrik ini nol di journal lama. B5a lewat satu sesi live baru untuk
mengukurnya dengan alat, bukan dengan tangan.

### B5b: Kontrak sesi streaming netral dan adapter provider

- Kontrak netral, menggantikan `SpeechSynthesizer` di `speech/contract.py`
  yang sekarang per kalimat: buka turn → kirim potongan teks → selesai atau
  batal → keluar chunk audio, plus timing kata kalau provider punya.
- Adapter websocket: Cartesia (satu context per turn, `continue: true` untuk
  potongan tambahan dan `continue: false` untuk potongan terakhir supaya
  context ditutup, `max_buffer_delay_ms: 0` karena kita membagi kalimat
  sendiri, `add_timestamps`) dan ElevenLabs (multi-context websocket, fitur
  setara).
- Adapter HTTP: OpenAI dan MiniMax, request per kalimat dengan lookahead,
  tanpa timing kata. Kualitas turun sedikit, tapi tetap jalan.
- Request TTS per kalimat yang dipakai di B5a dihapus, jadi adapter websocket
  dan HTTP jadi satu jalur.

Gate: timing kata masuk journal, jadi saat twin dipotong kita tahu kata
terakhir yang benar-benar kedengeran; prosodi nyambung antar kalimat; satu
provider mati di tengah turn dan sesi pindah ke provider lain.

Soal fallback: `speech/fallback.py` sekarang menaikkan error begitu primary
menghasilkan satu chunk pun, jadi provider mati setelah audio mulai tidak
bisa dipindah. B5b mengubahnya: pertukaran provider di tengah turn tetap
boleh, dan turn yang sudah menghasilkan audio tidak hilang karena itu.
Fallback ini belum ada sekarang, jadi ini pekerjaan baru, bukan sifat yang
dipelihara.

Yang benar-benar didapat dari websocket, dan bukan dari HTTP: prosodi
nyambung antar kalimat (kalimat kedua melanjutkan intonasi pertama, bukan
reset), timing kata per kata, dan overhead per kalimat lebih kecil karena
tidak membuat request baru tiap kalimat. Ketiganya nilai tambah, bukan
alasan utama. Cartesia hanya implementasi pertama karena itu provider yang
kita pakai sekarang; desainnya terikat ke kontrak netral di atas, bukan ke
Cartesia. Source Pipecat (`base_output.py`, `services/cartesia/tts.py`)
dipakai sebagai acuan.

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

Revisi riset: actor tetap milik kita. Pipecat dan LiveKit Agents tidak
diadopsi penuh karena turn controller mereka bertabrakan dengan kernel dan
judge, dan bagian terbaik LiveKit berada di cloud atau berlisensi khusus.
LangGraph keluar dari loop ini karena hanya bisa berhenti di batas step;
composer tetap `create_agent` dengan `stream_mode="messages"`.

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

Revisi riset: model Smart Turn v3.2 (BSD-2, ONNX int8 sekitar 8 MB, 23
bahasa termasuk Indonesia, inferensi di bawah 100 ms di CPU menurut
dokumentasi Pipecat) di-vendor seperti Silero dan dijalankan dengan
`onnxruntime` yang sudah ada, setelah Silero melaporkan stop. Sinyal prosodi
ini digabung dengan judge interim.

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

Revisi riset: tier model dibawa lewat `create_agent(context_schema=...)` dan
dibaca di `@wrap_model_call`, menggantikan `EvidenceRoutingMiddleware`.
Retry menjadi satu lapis: `ModelRetryMiddleware`; loop
`REPLY_STREAM_ATTEMPTS` dihapus dan `max_retries` SDK di jalur reply
menjadi 0.

### B7: Perception layar yang murah

- Frame dibaca setelah layar settle. OCR lokal dan hash teks menentukan
  sama, scroll, atau konten baru. Dokumen yang di-scroll adalah satu segmen
  yang teksnya bertambah. Pilihan model OCR (id dan en, onnxruntime)
  diputuskan lewat spike dengan ukuran image dan latency.
- LLM vision hanya untuk konten baru yang settle atau look eksplisit.
- Keyframe dipertahankan per segmen, bukan cache 48 frame global.

Gate: replay share spreadsheet oue-4 menghasilkan ≤ 5 read LLM; layar
terbaca ≤ 3 s setelah settle.

Revisi riset: spike RapidOCR 3.10 (model PP-OCRv6 multi; cakupan bahasa
Latin termasuk Indonesia belum terkonfirmasi dari dokumentasi resmi, jadi
dicek saat spike) melawan Tesseract 5.5 pada frame oue-4; kunci dedup
adalah hash teks OCR, `imagehash` hanya sebagai sinyal kasar.

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

Revisi riset: tetap tabel Postgres berversi. Graphiti tidak menjadi store
owner model karena ekstraksi LLM mengubah kata dan dapat menginvalidasi
edge tanpa approval; `PostgresStore` dan LangMem tidak punya versi,
approval, atau disclosure. C1 menyerap rantai `FakeyouOwnerNote`
(`memory/owner_notes.py`) agar tidak ada store ketiga. Item yang di-approve
ditulis juga sebagai episode Graphiti agar terambil recall.

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

Revisi riset: `SpeechPlan` menjadi `response_format` Pydantic lewat
`ProviderStrategy` yang sudah dipakai, dengan `claims` sebelum `text` di
schema. Klaim parsial divalidasi dengan
`TypeAdapter(SpeechPlan).validate_json(..., experimental_allow_partial=
"trailing-strings")`, menggantikan koersi manual `_act_of`. Gate per klaim
tetap milik kita: library guardrail (NeMo, Guardrails AI, HHEM) menilai
dukungan secara probabilistik dan tidak memeriksa ref, disclosure, atau
`speaks_as`. Judge owner-plausible memakai deepeval secara offline.

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

Revisi riset: `search_` Graphiti dengan recipe bawaan
`EDGE_HYBRID_SEARCH_RRF` (tanpa reranker LLM di listener) dan
`center_node_uuid` pada node owner dan peserta yang hadir. Batas laju dengan
`InMemoryRateLimiter`. Jendela bergulir dan pemicu pergantian topik tetap
milik kita.

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

Revisi riset: ingestion memberi `entity_types`/`edge_types` Graphiti
(`Commitment`, `Decision`, `OpenQuestion`), sehingga kandidat dari meeting
lalu diambil dengan `SearchFilters(edge_types=...)`. Deteksi live tetap di
kernel karena ingestion berjalan setelah meeting.

### C7: Belajar dari debrief

- Debrief menghasilkan proposal owner model (posisi baru, koreksi, aturan)
  dengan kutipan bukti. Owner approve atau tolak. Approval menaikkan versi.
- Replay eval berjalan per versi owner model untuk mengukur dampak.

Gate: proposal yang di-approve dipakai di meeting berikutnya; proposal yang
ditolak tidak pernah dipakai.

Revisi riset: bukti proposal diambil dengan `SearchFilters` dan
`get_nodes_and_edges_by_episode`. Approval, versi, dan replay per versi
tetap workflow Postgres kita. `build_communities` tidak dipakai.

## Track D: always-on

### D1: Desired state dan calendar

- Sync calendar owner (Google Calendar, izin OAuth owner) mengisi baris
  `meeting_sessions` dengan jadwal join, link Meet, brief default, dan
  deadline lobby.
- Controller mengubah `desired_state` sesuai jadwal; worker mengklaimnya
  lewat A2. API manual tetap menjadi sumber desired state lain.

Gate: meeting di calendar di-join tanpa perintah manual.

Revisi riset: `google-api-python-client` dengan `events.list` dan
`syncToken`, polling 1-2 menit, resync penuh saat 410.

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

Revisi riset: client `kubernetes` resmi membuat Job dengan
`backoffLimit: 0` (retry milik A2), `activeDeadlineSeconds`, dan
`ttlSecondsAfterFinished`. OpenTelemetry ditambahkan di sini untuk latency
lintas proses; konteks trace dibawa lewat field Job dan event.

### D3: Config yang menyusut

- `Settings` sub-model dioper langsung ke komponen. Kebijakan per meeting
  hanya di brief.
- Dihapus: `SessionConfig`, `JoinRequest` sebagai salinan settings, dan
  flag `enabled` untuk jalur yang sudah menjadi satu-satunya jalur.

Gate: jumlah field settings dan key `.env.example` turun; tidak ada field
yang disalin tangan antar lapisan.

Revisi riset: sub-model `Settings` dioper langsung, override per meeting
dengan `model_copy(update=...)`, secret Kubernetes lewat `secrets_dir`.

### D4: Worker job di Postgres

- Worker debrief, ingestion, dan reembed memakai procrastinate (retry dengan
  backoff, lock, deteksi job macet, task periodik) di Postgres yang sudah
  ada.
- Dihapus: antrean job Redis buatan sendiri (`pending_deliveries`,
  `claim_entry`, `touch_entry`, dead-letter).

Gate: job yang workernya dimatikan diambil ulang; Redis hanya membawa event
dan secret sekali pakai.

## Urutan PR

Urutan mengikuti ketergantungan: A membuat sesi bisa divalidasi, B membuat
twin terasa manusia, C membuat twin bernilai, D membuatnya berjalan
sendiri. Design C dan D boleh ditulis lebih awal; implementasinya menunggu
gate A.

1. A0 rantai causation dan metrik.
2. A1a satu sumber status sesi (selesai, #47), A1a-r koreksi dari riset
   (selesai, #49), lalu A1b lifecycle tanpa LangGraph. Catatan A1b: resume
   adalah join baru dari `created` yang di-gate baris `meeting_sessions`;
   worker yang di-cancel atau mati tidak menulis status terminal sampai
   reconciler A2 ada.
3. A4 status call dari protocol.
4. A3 join dan lobby (butuh A4).
5. A5 kebijakan dialog.
6. A2 lease Postgres, reconciler, dan recovery (butuh A1a, A1b).
7. Live validation A: tiga sesi multi-human 30 menit, satu dengan kill
   Chromium, satu dengan restart worker.
8. V0 replay harness.
9. B5a lookahead sintesis dan writer audio berumur panjang.
10. B5b kontrak sesi streaming netral dan adapter provider.
11. B1 floor actor.
12. B3 barge-in dari suara (butuh B5a, B1).
13. B2 commit giliran lokal (butuh B1).
14. B4 identitas dari stream.
15. B6 turn paralel.
16. B7 perception layar.
17. Live validation B: metrik latency dan barge-in di tabel baseline.
18. C1 owner model.
19. C2 dan C3 kontribusi dan gate per klaim.
20. C4 prompt peran (butuh C2 dan C3).
21. C5 memory listener.
22. C6 proactive dari kernel (butuh C1, C5, B1).
23. C7 belajar dari debrief (butuh C1).
24. Live validation C: skenario kontribusi dan review owner.
25. D3 config.
26. D1 calendar (butuh A2).
27. D2 pod per sesi.
28. D4 worker job di Postgres.

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
