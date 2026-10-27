# Twin Meeting Kernel Design

Status: PR 0 selesai, implementasi kernel berjalan.
Tanggal: 2026-10-27.
Keputusan: arah meeting kernel diterima, tetapi rencana awal wajib memakai
revisi dalam dokumen ini sebelum implementasi dimulai.
Revisi 2026-10-27: breaking change diizinkan dan tidak ada backward
compatibility. Kernel menggantikan engine lama langsung; tidak ada mode
`legacy`/`shadow`, tidak ada expand-contract record, dan setiap PR
menghapus jalur lama yang ia gantikan.

## Verdict

Desain awal sudah benar pada satu hal mendasar: LLM tidak boleh menjadi pusat
sequencing, otoritas side effect, dan sumber fakta meeting. Namun desain awal
belum cukup solid karena masih menyisakan lima celah struktural:

1. Command DAG generik terlalu bebas untuk perilaku turn yang sebenarnya
   terbatas dan bisa menciptakan kompleksitas scheduler baru.
2. Identity ledger dapat salah menggabungkan device, voice track, peserta,
   dan identitas orang sungguhan.
3. Intent dari peserta belum dipisahkan dari otorisasi untuk bertindak.
4. Claim gate berbasis nama/angka tidak dapat membuktikan parafrase bebas.
5. Event log mentah dapat menjadi sumber kebenaran kedua, membebani hot path,
   dan menyimpan data yang seharusnya ephemeral.

Desain final menutup celah tersebut dengan actor kernel single-writer, finite
turn-plan templates, identity hypothesis graph, authorization policy, speech
contract per kelas klaim, dan durability bertingkat.

## Tujuan

Twin harus dapat:

- Berpartisipasi hanya ketika memang diajak atau ketika policy proaktif yang
  ketat mengizinkan.
- Menjalankan perintah meeting secara mekanis, idempotent, dan terobservasi.
- Menjawab dari fakta yang punya provenance, modality, freshness, dan izin
  disclosure.
- Mengatakan tidak tahu ketika evidence tidak lengkap.
- Tetap hidup serta fail closed ketika model, browser, STT, TTS, atau
  perception mengalami gangguan.
- Menjelaskan setiap keputusan dari event, snapshot, plan, dan outcome yang
  dapat direplay pada boundary adapter.

## Non-goals

- Mengganti Google Meet browser guest dengan API baru.
- Mengklaim identitas dunia nyata peserta hanya dari display name.
- Menjamin exactly-once pada browser UI; targetnya at-most-once visible
  effect dan outcome `unknown` yang jujur.
- Membuat planner generik untuk workflow arbitrer.
- Memberi twin kewenangan membuat komitmen, persetujuan, atau keputusan baru
  atas nama owner di luar brief.
- Menyimpan seluruh audio, frame, interim STT, dan DOM mentah sebagai event
  permanen.

## Fondasi yang dipertahankan

Refactor tidak membuang bagian yang sudah terbukti:

- [MeetingRecord](../../src/fakeyou/persistence/record.py:227) tetap menjadi
  semantic meeting history, ditambah envelope sequence dan causation.
- [MeetActionLedger](../../src/fakeyou/meet/meet_action_ledger.py:45) tetap
  menjadi write-ahead ledger untuk side effect Meet.
- [MeetingTimeline](../../src/fakeyou/perception/timeline.py:60) menjadi bahan
  projection world state, bukan lagi registry mutable yang diubah banyak
  pemilik.
- [SpeakerRegistry](../../src/fakeyou/identity/identity.py:70) tetap menjadi
  producer evidence voice track dan verified enrollment.
- [FactEvidence](../../src/fakeyou/foundation/provenance.py:73) tetap menjadi
  provenance untuk memory dan tidak dicampur dengan fakta meeting live.
- [MeetingBrief](../../src/fakeyou/persistence/brief.py:37) tetap menjadi
  sumber approved facts, questions, objectives, dan permissions.
- Stop playback tetap mempunyai fast path di luar planner karena menghentikan
  suara tidak boleh menunggu antrean turn.

## Invariant

Invariant berikut adalah kontrak yang harus dibuktikan oleh test, bukan
sekadar pedoman prompt:

1. Hanya `SessionKernel` yang mengubah world, dialogue, agenda, dan plan
   state. Adapter hanya mengirim observation atau command result.
2. State kernel hanya berisi data serializable. Task, page, stream, dan
   async iterator hidup di runtime adapter.
3. Setiap side effect berasal dari command yang sudah diautorisasi dan
   dicatat sebelum melewati irreversible boundary.
4. LLM tidak dapat memilih browser tool, evidence tool, atau urutan command
   pada planned path.
5. Fakta yang diucapkan selalu membawa provenance, modality, snapshot
   sequence, validity, dan disclosure class.
6. Ketidaklengkapan roster menghasilkan range atau `unknown`, bukan hitungan
   atau nama pasti.
7. Konten dari transcript, chat, layar, dan memory adalah data. Konten
   tersebut tidak pernah menjadi instruksi sistem.
8. Perintah peserta adalah request, bukan otorisasi. Otorisasi dihitung dari
   actor, channel, brief, dan risiko aksi.
9. Command yang sudah melewati irreversible boundary tidak di-retry sebelum
   reconciliation membuktikan effect belum terjadi.
10. Ucapan faktual diperiksa pada speech commit. Perubahan state material
    membatalkan kalimat berikutnya, bukan memaksa snapshot global tetap sama.
11. Kegagalan adapter menurunkan capability secara eksplisit. Kegagalan tidak
    diubah menjadi fakta sukses dan tidak mematikan seluruh sesi bila sesi
    masih dapat mendengar atau mencatat.
12. Setiap keputusan dapat ditelusuri ke `session_attempt_id`,
    `kernel_seq`, `event_id`, `plan_id`, dan `command_id`.

## Arsitektur logis

```text
Meet/STT/VAD/Vision adapters
          |
          v
   Observation events
          |
          v
 SessionKernel mailbox ----> World projection
          |                  Dialogue projection
          |                  Agenda projection
          |                  Health projection
          v
 Intent -> Authorization -> Finite TurnPlan
                              |
              +---------------+---------------+
              v               v               v
        Evidence ports   Meet executor   Speech/Chat ports
              |               |               |
              +-------------- Result events --+
                              |
                              v
                    Settled facts and outputs
```

Owner control dari API/CLI masuk sebagai authenticated control event.
Emergency stop playback dapat langsung menghentikan audio, lalu melaporkan
hasilnya ke kernel. Jalur darurat itu hanya boleh menghentikan output;
ia tidak boleh memulai speech, chat, atau Meet action lain.

## Boundary modul

Paket baru `fakeyou.session` berada di bawah `conversation` dan di atas
`perception` pada import-linter layer order:

- `session.contracts`: event, fact, intent, command, dan ID. Hanya bergantung
  pada `foundation`, `catalog`, dan tipe konfigurasi murni.
- `session.world`: reducer murni untuk world, dialogue, agenda, dan health.
- `session.policy`: authorization, participation policy, dan risk rules.
- `session.planner`: finite plan templates dan command state machine.
- `session.kernel`: mailbox, ordering, snapshot publication, dan command
  dispatch ke port.

`session` tidak mengimpor Playwright, LangChain, Graphiti, provider SDK,
atau filesystem. `orchestration` mengikat kernel ke Meet, STT, TTS, memory,
persistence, dan model. `conversation` menyediakan model-backed intent dan
speech composers melalui port yang didefinisikan oleh kontrak session.

Perception adapter menerjemahkan protocol/DOM menjadi normalized
observation. Ia tidak lagi menulis langsung ke timeline yang dipakai untuk
keputusan. Timeline lama dimigrasikan menjadi projection yang dibangun oleh
reducer atau dihapus setelah projection baru mencapai parity.

Session supervisor yang sudah ada di orchestration tetap pemilik lifecycle
proses/browser: join, lobby, admission, reconnect, alone timeout, terminal
cleanup, dan exit reason. `SessionKernel` hanya hidup untuk satu admitted
session attempt dan menerima lifecycle sebagai event. Kernel dapat meminta
leave melalui command, tetapi supervisor yang menutup sesi setelah fact
`left`, `removed`, `ended`, atau owner stop teramati. Kernel tidak dapat
menghidupkan kembali attempt terminal.

Pekerjaan read-only seperti intent classification, evidence query, vision,
dan memory search adalah `WorkItem`, bukan command. WorkItem dapat
dibatalkan dan di-retry sesuai deadline, tetapi tidak memiliki irreversible
boundary dan tidak boleh menghasilkan side effect. Hasil WorkItem masuk
kembali sebagai event; reducer dan planner tetap murni.

## Event, ordering, dan durability

### Envelope

Semua event logis memiliki envelope yang sama:

- `event_id`: ID stabil dari producer bila tersedia.
- `session_id` dan `session_attempt_id`.
- `kernel_seq`: urutan total yang diberikan kernel saat event diterima.
- `source`: adapter dan channel asal.
- `source_seq`: urutan producer bila tersedia.
- `observed_at`: waktu sumber, diperlakukan sebagai data tak tepercaya.
- `received_at`: wall time kernel.
- `monotonic_deadline`: hanya untuk pekerjaan terjadwal, tidak disimpan
  sebagai wall-time truth.
- `causation_id`: event atau command penyebab.
- `correlation_id`: transcript, plan, atau command terkait.
- `schema_version`, producer version, dan model/prompt hash bila relevan.
- `privacy_class` dan content reference/hash untuk payload besar.

`kernel_seq` adalah urutan otoritatif dalam satu session attempt. Timestamp
Meet, STT, browser, dan provider tidak dipakai untuk mengurutkan keputusan
karena clock skew sudah terbukti terjadi.

Event yang datang terlambat tetap direduce berdasarkan `kernel_seq`, tetapi
payloadnya dapat ditandai late. Reducer tidak menulis ulang keputusan lama;
correction menjadi event baru yang mengubah projection berikutnya.

### Durability bertingkat

Satu model event logis tidak berarti satu file untuk semua data:

- **Write-ahead:** command intent, irreversible boundary, dan terminal
  command state disimpan atomik di action ledger sebelum effect dilanjutkan.
- **Semantic history:** final transcript, live chat, screen reading yang
  sudah di-scrub, twin speech yang benar-benar terdeliver, dan settled own
  actions disimpan di `MeetingRecord` v4 dengan sequence/causation.
- **Ephemeral:** interim STT, VAD, level audio, frame mentah, DOM poll, dan
  speculative model work hanya hidup pada bounded ring buffer. Journal hanya
  menyimpan hash, ukuran, timing, dan reason code yang aman.
- **Replay bundle:** test harness merekam normalized input/output pada port.
  Production journal yang redacted dan rotating tidak dianggap replay source.

Tidak ada dual-write antara event store baru dan record lama. Apabila
replay membuktikan record v4 tidak cukup, PR yang sama harus memilih satu
canonical semantic log dan menghapus representasi duplikatnya.

Record v4 menggantikan v3 tanpa reader kompatibilitas. Record v3 yang sudah
ada tidak dibaca oleh debrief/ingestion baru; meeting lama yang masih perlu
diproses diselesaikan sebelum upgrade.

## Projection state

Snapshot kernel adalah immutable value pada `kernel_seq` tertentu. Ia
dipisahkan menjadi empat projection agar precondition tidak bergantung pada
seluruh dunia:

- **World:** endpoints, streams, voice tracks, participant hypotheses,
  screens, chat, browser/call state, dan own action facts.
- **Dialogue:** final/interim mutakhir, floor holder, open question,
  pending clarification, bahasa/register, twin speech delivered/interrupted,
  dan self-speech correlation.
- **Agenda:** brief questions/objectives yang masih terbuka, unanswered
  everyone-question, proactive candidates, rate-limit state, dan obligation
  yang dibuat sistem sendiri.
- **Health:** source freshness, dropped/rejected input, adapter capability,
  provider degradation, browser epoch, dan backlog scheduler.

Setiap domain memiliki revision sendiri: `presence_rev`, `floor_rev`,
`screen_rev`, `dialogue_rev`, `agenda_rev`, dan `health_rev`. Command
menyatakan predicate serta revision yang relevan. Perubahan chat yang tidak
relevan tidak membatalkan reaction; perubahan roster membatalkan jawaban
roster yang belum diucapkan.

Projection dibatasi ukurannya. History lengkap dibaca melalui query ke
record/evidence port, bukan disimpan sebagai list tak terbatas di snapshot.
Snapshot hanya menyimpan current state, window percakapan yang dibutuhkan
policy, index reference, dan ringkasannya.

## Identity dan presence

Meet tidak memberikan identitas dunia nyata yang terverifikasi untuk semua
peserta. Karena itu model target tidak memiliki satu `Person` yang dipaksa
menyatukan semua sinyal.

Entitasnya adalah:

- `EndpointDevice`: `device_id` dalam satu browser/meeting epoch.
- `MediaStream`: stream, slot, dan CSRC yang teramati.
- `VoiceTrack`: label/UUID dari speaker registry.
- `MeetingParticipant`: hipotesis peserta lokal dalam meeting ini.
- `DisplayClaim`: nama tampilan yang diklaim endpoint.
- `EnrolledPerson`: owner atau orang yang enrollment-nya diverifikasi oleh
  mekanisme voiceprint/owner confirmation.

Relasi antar-entitas adalah evidence edge, bukan merge destruktif:
`speaks_through`, `bound_to`, `claims_name`, `presents_from`,
`same_participant_candidate`, dan `verified_as`. Edge memiliki status
`confirmed`, `probable`, `unknown`, `conflicting`, atau `expired`, beserta
producer, confidence, first/last seen sequence, dan alasan.

Aturan presence:

- Sync yang tidak memuat endpoint bukan bukti endpoint keluar.
- `present=false` eksplisit atau call-state terminal dapat mengubah
  presence menjadi absent. Source yang stale mengubah presence menjadi
  `unknown`, bukan absent.
- DOM participant count adalah evidence completeness dan drift, bukan
  identitas.
- Speaker track aktif yang belum terpetakan menaikkan unresolved count.
- Satu endpoint dapat dipakai beberapa manusia; beberapa endpoint dapat
  dipakai satu manusia. Keduanya tetap menjadi hipotesis sampai ada bukti
  kuat.
- Participant count dikembalikan sebagai `confirmed_min`,
  `plausible_max`, `unresolved`, dan `complete`.
- Nama display boleh dipakai sebagai label percakapan, tetapi modality-nya
  tetap `claimed`, bukan `verified`.
- Twin mengenali endpoint-nya dari session/current-user evidence, bukan nama.

`sole_human_responder` hanya benar apabila tepat satu participant hypothesis
manusia terkonfirmasi, tidak ada endpoint/stream/voice track manusia yang
belum terpetakan, roster source sehat, dan tidak ada evidence konflik.
Jika tidak, nilainya `false` atau `unknown`, dan policy logistics check
memakai cabang konservatif.

## Intent

Judge menjadi classifier dengan discriminated union agar kombinasi invalid
tidak dapat direpresentasikan. Ia tidak lagi mengembalikan addressee,
speech action, meet action, dan evidence sebagai field bebas yang dapat
saling bertentangan.

Varian awal dibatasi pada:

- `NoResponse(reason)`.
- `Answer(question_frame, evidence_queries)`.
- `Clarify(ambiguity)`.
- `SpeechControl(stop|shorter|repeat|louder)`.
- `MeetAction(kind, typed_params)`.
- `ChatRequest(content_mode, topic_or_verbatim_ref)`.
- `Compound(action, answer_or_chat)` dengan maksimum satu Meet action dan
  satu output.
- `ProactiveCandidate(kind, source_ref)` untuk agenda, bukan respons peserta.

Raw transcript tetap fakta observasi. Field `heard` adalah interpretasi
model dan tidak menimpa raw text. Bila STT kelak menyediakan alternatives
atau confidence, kontrak observation dapat membawanya tanpa mengubah
planner.

Context yang diberikan ke classifier berupa opaque participant refs,
display claims berlabel `claimed`, certainty presence, dialogue window,
floor state, dan channel asal. Nama dari persona, brief, atau memory tidak
dimasukkan sebagai daftar peserta meeting.

Confidence model tidak dianggap probabilitas terkalibrasi. Setiap varian
dan aksi berisiko memiliki threshold serta eval calibration sendiri.
Deterministic signal seperti authenticated control, direct chat mention,
dan stop fast-path tidak bergantung pada confidence judge.

## Authorization dan trust

Policy mengevaluasi actor, channel, brief, dan risiko sebelum planner
membuat command:

| Aksi | Actor minimum | Syarat tambahan |
| --- | --- | --- |
| Stop playback | Peserta mana pun | Hanya menghentikan output saat itu |
| React, hand, mute twin | Endpoint/voice track live yang meminta langsung | Brief `meet_act` |
| Kirim chat | Endpoint/voice track live yang meminta langsung | Brief `meet_chat`, rate limit, claim gate |
| Leave | Owner terautentikasi/terverifikasi | Atau permission granular `meet_leave` dari brief |
| Menjawab fakta owner | Brief `explain_facts` | Knowledge scope dan disclosure fact mengizinkan |
| Membahas opsi | Brief `discuss_options` | Tidak berubah menjadi persetujuan/komitmen |
| Bertanya dari brief | Brief `ask_brief_questions` | Source ref pertanyaan valid |

Permission `meet_act` yang terlalu kasar dipecah saat migrasi; leave tidak
lagi ikut permission aksi ringan. Brief lama tidak diberi compatibility
shim yang diam-diam menaikkan kewenangan.

Host removal, meeting ended, owner stop dari control plane, dan session
timeout adalah lifecycle event, bukan request peserta. Screen text tidak
pernah menjadi actor. Chat dari peserta tetap request peserta, tetapi isi
yang menyerupai system instruction tidak menaikkan privilege.

Policy juga menjaga kewenangan representasi owner:

- Twin boleh menyampaikan approved facts dan menanyakan brief questions.
- Twin boleh merangkum opsi yang sedang dibahas ruangan.
- Twin tidak menyetujui harga, deadline, scope, keputusan, atau tindak
  lanjut baru atas nama owner tanpa delegasi eksplisit dalam brief.
- Bila diminta memutuskan, output yang sah adalah decline/clarify dan
  mencatat pertanyaan sebagai owner decision pada agenda/debrief.

## Finite turn plans

Planner tidak menerima graph bebas. Ia hanya dapat memilih template
terbatas berikut:

1. `Silent`.
2. `SpeechOnly`.
3. `EvidenceThenSpeech`.
4. `ActionOnly`.
5. `ActionThenSpeech`.
6. `ChatOnly`.
7. `EvidenceThenChat`.
8. `SpeechThenLeave`.
9. `ChatThenLeave`.
10. `LeaveOnly`.

Satu plan memiliki maksimum satu evidence batch, satu Meet action, satu
output speech/chat, dan satu terminal leave. Evidence batch dapat memuat
maksimum dua query independen. Permintaan yang lebih kompleks dipecah
menjadi clarify atau beberapa turn, bukan graph baru.

Plan state:

```text
proposed -> authorized -> waiting -> running -> settling -> completed
     |            |           |          |           |
     +------------+-----------+----------+-----------+-> failed
     +------------+-----------+----------+-----------+-> cancelled
     +------------+-----------+----------+-----------+-> superseded
```

Final baru dapat men-supersede evidence/speech yang belum melewati commit
boundary. Side effect yang sudah terjadi tidak pernah di-undo oleh plan
baru. Leave adalah terminal barrier: command non-esensial lain dibatalkan
dan tidak ada output baru setelah leave dimulai.

Agenda timer tidak langsung berbicara. Timer membuat candidate intent yang
dievaluasi ulang terhadap snapshot, floor, rate limit, dan authorization
pada saat jatuh tempo.

## Command lifecycle dan recovery

Command adalah satu-satunya jalur side effect. Setiap command memiliki:

- `command_id` deterministik dari session attempt, plan, ordinal, dan
  semantic parameter hash.
- Actor, authorization decision, dan brief digest.
- Precondition predicates dan domain revisions.
- Resource yang dibutuhkan.
- Monotonic deadline dan cancellation policy.
- Irreversible boundary.
- Verification method dan verification strength.
- Attempt, browser epoch, dan causation event.

State command:

```text
accepted -> ready -> dispatching -> effect_observed
    |          |          |-------> effect_unobserved
    |          |          |-------> unknown
    |          |          |-------> failed_after_boundary
    |          |----------> refused
    |----------|----------> cancelled_before_boundary
    |---------------------> failed_before_boundary
```

`unknown` adalah terminal untuk turn berjalan. Observation berikutnya tidak
menulis ulang state command tersebut; ia menghasilkan reconciliation fact
baru yang terhubung ke command ID. Sampai fact itu ada, outcome tidak pernah
dibaca sebagai sukses.

Aturan retry per aksi:

- Toggle menuju target state seperti mute/unmute/hand boleh di-retry setelah
  readback membuktikan state belum tercapai.
- Chat tidak di-retry setelah Enter. Reconciliation mencari own-device
  protocol echo berdasarkan message ID/device/text/window; DOM text saja
  adalah evidence lemah.
- Reaction tidak di-retry setelah click boundary kecuali adapter membuktikan
  click tidak terjadi. Click yang diterima UI adalah `acted`, bukan bukti
  peserta melihat reaction.
- Leave tidak di-retry buta pada browser epoch baru. Call state `left`,
  `removed`, atau `ended` adalah reconciliation.
- Speech yang sudah mengeluarkan PCM tidak diulang otomatis. Kalimat yang
  terdeliver dan terinterupsi dicatat terpisah.

Sebelum dispatch, executor memvalidasi ulang browser epoch, call state,
resource lease, dan precondition relevan. Ia menulis irreversible boundary
ke ledger tepat sebelum click/Enter/playback commit. Crash setelah boundary
menghasilkan `unknown` pada recovery, bukan pengulangan aksi.

Action ledger perlu menyimpan content reference ke semantic record atau
parameter minimum yang dibutuhkan reconciliation dengan akses file yang
sama ketatnya dengan record. Hash saja tidak cukup untuk reconciliation
chat. Parameter tidak masuk journal dan tidak dipertahankan setelah command
terminal melewati retention sesi.

Pada restart, session attempt baru dibuat. Command nonterminal dari attempt
lama tidak dilanjutkan; command tersebut direkonsiliasi dari browser/record
state atau ditutup `unknown`. Browser epoch baru membatalkan semua command
yang belum dispatch.

## Evidence dan facts

Evidence bukan tool yang dipilih reply LLM. `question_frame` dari intent
dipetakan planner secara deterministik ke typed query:

- `Attendance` dan `ParticipantCount`.
- `CurrentPresenter`.
- `ScreenNow(ref?)` dan `ScreenEarlier(ref)`.
- `ParticipantAppearance(participant_ref)`.
- `ChatHistory(range_or_ref)`.
- `ConversationRecall(window)`.
- `OwnActionStatus(action_ref)`.
- `BriefItem(ref)`.
- `OwnerMemory(query)`.

Query mengembalikan `FactSet`, bukan teks bebas untuk langsung dipercaya.
Fact memiliki:

- Proposition machine-readable bila state memungkinkan, misalnya
  `ParticipantCount(min=1, max=2, complete=false)`.
- Text renderable untuk fakta naratif dari brief/memory/screen.
- Namespace: `meeting_live`, `conversation`, `brief`, `persona`, `memory`,
  atau `own_action`.
- Modality: `directly_observed`, `protocol_reported`, `participant_claimed`,
  `screen_read`, `owner_approved`, `memory_recalled`, atau `inferred`.
- Provenance event/source IDs dan producer/model version.
- Confidence atau certainty class.
- `valid_from_seq`, `valid_until`, dan domain revision.
- Disclosure class: `room_speakable`, `owner_only`, atau
  `internal_instruction`.
- Privacy/scrub status.

Fakta dari namespace berbeda tidak digabung menjadi satu proposition.
Nama dalam memory tidak dapat memenuhi query attendance. Isi layar yang
bukan meeting content mengikuti policy title-only sebelum menjadi fact.
Participant claim tetap claim meskipun peserta menyatakannya dengan yakin.

Vision interpreter adalah evidence producer probabilistik. Screen fact
membawa frame hash, reading schema, model, dan uncertainty. Angka dari
layar hanya boleh diucapkan bila reading menyimpannya sebagai figure
terstruktur atau kutipan verbatim yang dapat dibandingkan dengan source
text.

Memory search yang `empty` dan `unavailable` menghasilkan outcome berbeda.
Keduanya tidak memberi izin menebak. `unavailable` juga menurunkan health
capability untuk turn tersebut.

## Speech contract

Tidak semua ucapan dapat diverifikasi dengan cara yang sama. Speech dibagi
menjadi empat kelas:

1. **State speech:** roster, count, presenter, screen title, mic/hand,
   action status, dan call state. Jawaban dirender dari typed proposition
   oleh deterministic renderer untuk bahasa yang didukung kontrak speech
   saat itu. LLM tidak memparafrase fakta state. Bahasa tanpa renderer dan
   TTS contract tidak menerima state speech sampai locale pack-nya ada.
2. **Source-grounded speech:** approved brief facts, profile facts, memory,
   screen figures, dan correction. Model menghasilkan structured
   `SpeechAct` yang setiap klaimnya menunjuk fact IDs. Renderer memeriksa
   entity, angka, negasi/modality yang dinyatakan dalam struktur, dan
   disclosure. Grounding verifier probabilistik hanya lapisan tambahan,
   bukan satu-satunya bukti.
3. **Conversational speech:** sapaan, acknowledgement, klarifikasi, dan
   pembahasan yang tidak menyatakan fakta eksternal baru. LLM dapat menulis
   bebas dalam batas persona, tetapi nama, angka, status aksi, komitmen,
   dan klaim kemampuan dipindai terhadap fact/capability snapshot.
4. **Control speech:** repeat mengambil delivered twin speech dari dialogue
   projection; shorter/louder/stop adalah command mekanis. Model tidak
   menciptakan ulang isi repeat.

Model tidak menerima seluruh history sebagai otoritas fakta. Ia menerima
`SpeechBrief` berisi tujuan, dialogue window yang relevan, fact IDs yang
boleh dipakai, bahasa/register, batas kalimat, dan larangan disclosure.
Internal objectives tidak dikirim sebagai fakta yang boleh diucapkan.

Streaming hanya melepaskan kalimat setelah kelas dan klaim kalimat itu
lolos gate. State speech yang pendek digenerate utuh sebelum playback.
Pada first-PCM commit, kernel memeriksa floor lease, session attempt,
domain revision relevan, dan fact validity. Perubahan setelah kalimat
pertama membatalkan kalimat berikutnya; kalimat yang sudah terdengar
dicatat sebagai delivered pada snapshot lamanya.

Bila gate gagal, pilihannya hanya diam, clarify, atau kalimat konservatif
yang menyatakan ketidakpastian tanpa menambah fakta. Filler tidak boleh
dimainkan untuk jalur yang mungkin berakhir diam.

Kemampuan twin dijawab dari capability snapshot sesi, bukan daftar tool
global. Jika browser adapter sedang degraded, twin mengatakan capability
tersebut belum dapat dipastikan, bukan mengklaim bisa atau tidak bisa
secara permanen.

## Scheduler dan latency

Kernel mailbox adalah single-writer, tetapi pekerjaan eksternal berjalan di
executor terpisah. Resource dideklarasikan eksplisit:

- `browser_page`: eksklusif untuk Meet action dan browser evidence yang
  mengubah UI.
- `playback`: eksklusif, dengan floor lease dan emergency stop.
- `model`: bounded concurrency per role.
- `memory`: bounded concurrency dan deadline sendiri.
- `vision`: bounded concurrency, budget, dan cache frame hash.

Prioritas dari tertinggi: emergency stop/session lifecycle, current floor
control, command yang sudah melewati boundary dan menunggu settlement,
direct response, evidence untuk direct response, proactive agenda, lalu
background maintenance.

Mailbox dibatasi dan mempertahankan FIFO per producer untuk semantic
observation. Priority hanya memilih pekerjaan executor; ia tidak mengurut
ulang semantic event yang sudah diterima, kecuali authenticated/emergency
control. Frame dan interim boleh di-drop dengan health counter, tetapi
final transcript, live chat, lifecycle, dan command result tidak boleh
hilang diam-diam. Bila semantic mailbox jenuh, kernel masuk degraded mode
dan menolak side effect baru sampai backlog pulih.

Interim STT boleh memulai klasifikasi dan evidence prefetch yang read-only.
Ia tidak boleh melewati authorization final, menulis command intent, atau
melakukan side effect. Hasil spekulatif terikat pada normalized interim
dan dibuang bila final berubah material.

Deadline memakai monotonic clock dan diwariskan dari plan ke command.
Setiap tahap mencatat queue, start, first useful result, boundary, settle,
dan cancellation. Metrik latency dipisahkan:

- `speech_stopped_estimate -> first_pcm` untuk pengalaman peserta.
- `final_received -> intent_decided`.
- `intent_decided -> evidence_settled`.
- `action_dispatch -> effect_observed`.
- `speech_commit -> first_pcm`.
- `stop_detected -> silence`.

Target beku saat ini pada
[session_targets.json](../../src/fakeyou/session_targets.json) tetap menjadi
gate sampai baseline kernel diukur. Desain tidak mengklaim target p50 1,5
detik dari roadmap lama sebagai SLO yang sudah terbukti. Stage budget
ditetapkan dari distribusi baseline pada PR characterization; timeout 60
detik untuk keseluruhan response tidak dipertahankan sebagai budget turn
interaktif.

Jika evidence melewati deadline, plan berakhir sebagai diam atau jawaban
ketidakpastian sesuai kelas pertanyaan. Scheduler tidak memperpanjang turn
dengan tool loop. Jika browser action lambat, action-only tetap diam;
compound turn tidak menarasikan status `queued` sebagai sukses.

## Adapter contracts

### Meet browser

Adapter menyediakan capability probe per browser epoch: call state, chat,
reaction set yang benar-benar terlihat, hand/mic readback, leave, roster
protocol health, dan DOM drift. Selector pack dan locale dicatat sebagai
adapter version. Capability yang tidak terprobe tidak diiklankan ke model
atau policy.

Setiap action adapter mengembalikan phase result terpisah untuk prepare,
boundary, act, dan readback. Ia tidak mengubah kegagalan readback menjadi
sukses hanya karena click tidak melempar error.

### STT dan speaker attribution

STT adapter mengirim interim/final sebagai observation terpisah. Speaker
attribution adalah evidence edge dari stream/slot/CSRC/voiceprint, bukan
field final yang menimpa identitas. Overlap menghasilkan `unknown` atau
beberapa candidate, bukan pemenang paksa.

Own playback menghasilkan start/finish/interrupted events dengan text hash
dan timing agar self-echo dapat dikorelasikan dan tidak menjadi turn baru.

### Model

Intent, vision, memory extraction, speech, dan verifier adalah producer
probabilistik dengan schema version. Output invalid, timeout, dan provider
failure menjadi outcome typed. Tidak ada fallback yang lebih permisif
daripada jalur utama untuk side effect atau factual speech.

Model identifier, schema version, prompt hash, latency, dan token usage
dicatat tanpa prompt/completion mentah. Perubahan salah satunya membuat
eval baseline tidak otomatis dapat dibandingkan.

### TTS dan playback

Playback menerima kalimat yang sudah commit. Ia melaporkan first PCM,
finished, interrupted, dropped, dan failed per kalimat. TTS fallback hanya
boleh mengganti provider sebelum PCM keluar; setelah PCM keluar, kalimat
tidak diulang oleh provider lain.

## Degraded modes

Health projection menentukan mode yang dapat dijelaskan:

- `full`: semua capability yang diizinkan brief tersedia.
- `perception_degraded`: twin dapat mendengar/menjawab percakapan, tetapi
  tidak menjawab roster/screen secara pasti.
- `identity_degraded`: direct named turn dapat diproses, tetapi
  sole-responder dan actor-specific action dimatikan.
- `model_degraded`: side effect dan factual speech fail closed; stop dan
  lifecycle tetap berjalan.
- `voice_degraded`: speech tidak dicoba berulang. Chat hanya dipakai bila
  memang channel/brief mengizinkan dan peserta perlu diberi tahu.
- `browser_unknown`: tidak ada Meet action baru sampai capability probe
  dan call-state reconciliation selesai.
- `memory_degraded`: pertanyaan memory dijawab sebagai belum dapat
  dipastikan; percakapan meeting tetap berjalan.

Mode tidak disembunyikan di journal saja. Policy dan speech capability
membaca mode yang sama sehingga ucapan twin konsisten dengan perilaku.

## Privacy dan security

- Fact dan event membawa privacy class sejak producer. Scrubbing terjadi
  sebelum persistence, model context, memory ingestion, dan debrief.
- Screen non-meeting tetap title-only di semua projection dan store.
- Brief objectives, persona instructions, dan system prompt berklasifikasi
  `internal_instruction`; model tidak boleh mengutip atau memparafrasenya
  sebagai jawaban.
- Approved facts adalah satu-satunya isi brief yang otomatis
  `room_speakable`, dan tetap tunduk pada permission `explain_facts`.
- Participant, screen, chat, dan memory content tidak dapat mengubah
  permission, capability, atau system instruction.
- Journal tidak menyimpan transcript/prompt/completion mentah, chat text,
  action params, token, cookie, atau frame.
- Retention mengikuti kelas data: ephemeral dibuang pada akhir sesi,
  semantic record mengikuti retention meeting yang sudah ada, dan replay
  fixture hanya memakai data sintetis atau yang sudah disanitasi.
- Cross-meeting identity hanya berasal dari enrollment eksplisit. Display
  claim dan voice track meeting lain tidak otomatis digabungkan.

## Migrasi per PR

Breaking change diizinkan dan tidak ada backward compatibility. Tidak ada
mode `legacy`/`shadow`/`kernel`, tidak ada flag per subsystem, dan tidak ada
dual-write. Setiap PR mengganti satu subsystem secara utuh, menghapus kode
lama yang digantikannya pada PR yang sama, dan tetap meninggalkan twin yang
dapat join, mendengar, menjawab, dan leave. Rollback berarti revert commit
atau kembali ke release sebelumnya, bukan mode runtime.

Karena tidak ada parallel run, characterization corpus PR 0 adalah satu-satunya
pembanding perilaku. Skenario `preserve` harus tetap lolos; skenario
`intentional_change` diubah ke target kernel pada PR yang membuatnya
executable, bukan dibiarkan memakai outcome lama.

### PR 0: Characterization (selesai)

- Corpus `evals/fixtures/kernel-scenarios.json`, characterization test, dan
  baseline report `docs/reports/2026-10-27-kernel-characterization.md`.
- Rollback threshold dan scorecard dibekukan dari artefak terukur.

### PR 1: Kernel core (selesai)

- `fakeyou.session.contracts`: envelope dan observation typed.
- `fakeyou.session.world`: reducer murni untuk projection world, dialogue,
  agenda, dan health dengan revision per domain.
- `fakeyou.session.kernel`: single-writer, dedupe, late detection, bounded
  mailbox dengan control lane dan drop hanya untuk input ephemeral.
- Import-linter mengunci `session` di atas `config` dan melarang adapter
  serta SDK provider.

Gate: property tests untuk determinisme, duplicate, bound, revision monoton,
terminal barrier, dan late event.

### PR 2: Kernel sebagai sumber world dan identity (selesai)

- `orchestration.kernel_port` menerjemahkan roster/audio source, transcript
  dan voice track registry, chat, call state per browser epoch, DOM count,
  protocol health, dan playback menjadi observation kernel per session
  attempt.
- `session.identity` membangun evidence edges dan `PresenceFact`; judge
  window membaca participants sebagai claim, presence certainty/range, dan
  `sole_human_responder` dari kernel.
- Control lane mailbox hanya untuk owner control; call state tidak boleh
  menyalip roster yang diterima lebih dulu.
- Dihapus: `sole_human_participant` dan `human_participants` dari daftar
  nama timeline.

Gate: skenario sync parsial, shared mic, multi-device, duplicate display
name, overlap, stale source, dan epoch baru tidak pernah menghasilkan exact
count atau sole responder palsu; `k-p3` dan `k-r2` berpindah ke outcome
kernel.

### PR 3: Intent, authorization, finite planner, command lifecycle (selesai)

- `session.intent` memetakan output judge secara deterministik ke union
  `NoResponse | Answer | Clarify | ChatRequest | MeetAction | Compound`;
  `TranscriptEvent` tidak lagi membawa field keputusan kecuali
  `evidence_needs` (dihapus di PR 4).
- `session.policy.authorize` memakai actor dari atribusi kernel (endpoint
  atau voice track live), channel, permission brief, dan ketersediaan
  browser. Brief memiliki `meet_leave` terpisah dari `meet_act`.
- `session.planner` hanya menghasilkan sepuluh template. Aksi plus balasan
  chat tidak punya template dan turun menjadi satu pertanyaan klarifikasi.
  Action-only yang gagal, ditolak, atau dibatalkan diucapkan sebagai
  penolakan singkat; yang berhasil, tidak teramati, atau `unknown` tetap
  diam.
- `session.commands` + `meet.command_ledger` + `orchestration.commands`:
  boundary ditulis sebelum click/Enter, toggle hanya di-retry selagi
  readback membuktikan target belum tercapai, chat diverifikasi lewat echo
  protocol own-device di kernel, react dan leave paling tinggi
  `effect_unobserved`, dan restart menutup command attempt lama menjadi
  `unknown` atau `cancelled_before_boundary`.
- Reply menerima catatan `[System: ...]` dari state command, bukan klaim
  sukses; `unknown` dan `effect_unobserved` tidak boleh diucapkan sebagai
  terlihat atau selesai.
- Leave yang diminta peserta berakhir sebagai `left_by_participant`, bukan
  `stopped_by_owner`.
- Dihapus: Meet tools pada reply agent, `MeetActionOnceMiddleware`,
  `ChannelToolsMiddleware`, `MeetActionDispatcher`, `MeetChatChannel`,
  ledger v1, `schedule_leave`/settler, dan persona tool meet_*.
- Belum ada: owner terverifikasi lewat suara sebagai actor; leave peserta
  tetap memerlukan `meet_leave`.

Gate: tabel otorisasi property-based, transisi command/plan, dan fake
browser crash-injection sebelum/sesudah click dan Enter tanpa duplicate
visible effect atau klaim sukses untuk `unknown`.

### PR 4a: State speech (selesai)

- Judge mengeluarkan `state_question` (attendance, participant_count,
  presenter, screen_title, own_action_status) yang mengalir lewat intent ke
  `TurnPlan.state_question`.
- `session.facts` menjawab pertanyaan state dari kernel dan outcome command
  sebagai `Fact` bertipe dengan namespace, modality, dan revision;
  `conversation.state_speech` merender dari locale pack
  `state_speech.json` (en, id) tanpa LLM. Bahasa tanpa pack jatuh ke reply
  biasa.
- Dihapus: tool `meet_check_action` dan evidence need `action_status`.

Gate: count/attendance tidak pasti menghasilkan range atau unknown;
perubahan screen setelah fact diambil membuat fact invalid.

### PR 4b: Evidence service (selesai)

- Reply agent meeting tidak lagi memegang tool apa pun. `gather_evidence`
  menjalankan look screen, look participant (hanya peserta yang disebut
  namanya), dan chat history sesuai `evidence_needs` plan sebelum reply,
  lalu menyuntikkan hasilnya sebagai `meeting_evidence` dengan kode per
  query.
- Memory hanya lewat prefetch dan hanya bila plan meminta `memory`
  (attention dan turn tanpa judge tetap prefetch). Tool recall hanya tersisa
  untuk chat API owner di luar meeting.
- Routing ke strong model dipertahankan, tetapi pemicunya hasil evidence
  step (`ok`) atau memory `found`, bukan tool call.
- Dihapus: look tools, `PlannedEvidenceMiddleware`, registry capability,
  strategi recall `tool`, `prompt_builder`, jalur prefetch turn-graph yang
  tidak di-wire, dan prompt legacy `build_system_prompt`.
- Timeline masih sumber view dan pemicu proactive; keputusan proactive
  pindah ke kernel di PR 5.

Gate: reply tidak dapat memanggil tool evidence; evidence yang tidak
direncanakan tidak dijalankan; memory tidak dicari untuk turn yang tidak
membutuhkannya.

### PR 5: Speech contract (selesai)

- `ReplyPlan` menjadi SpeechAct: `contribution`, `cites`, dan `commits_owner`
  ditempatkan sebelum `text`. Bila model menulis `text` lebih dulu, kalimat
  ditahan sampai `cites` terbaca; tidak ada kalimat yang dilepas sebelum
  aktanya diperiksa.
- `FactBook` per turn: brief facts (`explain_facts`), brief questions
  (`ask_brief_questions`), profile dan memory facts (bukan `brief_only`),
  dengan memory refs dari prefetch dan teks yang teramati (view, evidence,
  dialog) sebagai korpus heard. Objective brief hanya menjadi instruksi
  internal.
- Gate per kalimat sebelum PCM/chat untuk speech, chat, dan proactive:
  angka harus ada di fact yang dikutip atau yang terdengar, kutipan harus ada
  dan boleh diucapkan, overlap enam kata dengan instruksi internal ditolak.
  `commits_owner` diganti kalimat decline deterministik; penolakan lain
  tanpa kalimat terucap diganti kalimat "belum yakin" deterministik;
  proactive yang gagal tetap diam.
- Proactive wajib `contribution` dan kutipan; pertanyaan harus mengutip
  brief question; verifier grounding tetap lapisan tambahan atas teks fact
  yang dikutip.
- Dihapus: `ContributionGate`, `validate_contribution`, dan field
  `contribution_kind/source/source_ref`. Bug lama ikut tertutup: kontribusi
  dari memory kini bisa mengutip fact yang diprefetch.
- Batasan yang disengaja: tidak ada pemindaian nama berbasis huruf kapital
  karena rawan salah tolak; klaim roster sudah deterministik sejak PR 4a.
  Angka yang diucapkan sebagai kata tidak terdeteksi oleh pemeriksaan
  angka.

Gate: invented number, kutipan yang tidak ada atau owner_only, owner
commitment, kebocoran objective, dan kontribusi tanpa kutipan ditolak sebelum
PCM atau chat keluar.

### PR 6: Health, degraded modes, dan recovery (selesai)

- `session.health.modes` menurunkan mode dari projection health kernel:
  `browser_unknown`, `perception_degraded`, `identity_degraded`,
  `model_degraded`, `voice_degraded`, `memory_degraded`, dan
  `backlog_saturated` (mailbox jenuh). Transisi mode dicatat di journal
  `session_mode_transitioned`.
- Sumber health diisi oleh judge (`HealthReportingJudge`), outcome prefetch
  memory, playback, dan protocol heartbeat.
- Side effect baru ditolak pada `browser_unknown`, `model_degraded`, dan
  `backlog_saturated`; `identity_degraded` membuat presence tidak pernah
  complete dan actor tidak teratribusi.
- Recovery: command attempt lama ditutup `unknown` atau
  `cancelled_before_boundary`; browser epoch baru membatalkan command yang
  belum dispatch dan mengekspirasi device/stream; judge timeout tidak
  menghasilkan aksi dan reply tetap melewati speech gate.
- Replay sintetis setara dua jam membuktikan state bounded dan replay
  deterministik.
- Ditunda dengan sengaja: rewrite lane/floor/barge-in di turn
  lifecycle/graph ke resource lease dan inherited deadline. Jalur itu hanya
  dapat dibuktikan pada sesi live; menulis ulang tanpa validasi live
  menambah risiko regresi playback dan barge-in. Dikerjakan setelah PR 8
  memberi baseline live.

Gate: mode degradasi menolak side effect yang benar; recovery tidak pernah
melanjutkan command lama; long-session replay bounded dan deterministik.

### PR 7: Record v4 (selesai)

- `MeetingRecord` v4: setiap entry membawa `seq` monotonic yang ditetapkan
  record dan berlanjut setelah resume, serta `causation_id`; header dan
  resume membawa `attempt_id` yang sama dengan session attempt kernel.
- Twin speech mencatat transcript pemicunya sebagai causation, karena
  `PlaybackEvent` kini membawa `Utterance(transcript_id, sentence_index)`;
  kernel memakai id yang sama untuk utterance playback.
- Dihapus: reader/writer v3, `ToolResultEntry`, `add_tool_result`, dan
  callback hasil tool yang tidak lagi punya pemanggil.
- Belum: record ditulis oleh writer adapter yang sama (transcript drain,
  playback, perception archive), bukan oleh kernel sebagai satu-satunya
  writer. `kernel_seq` belum ada di entry; korelasi memakai `seq` record dan
  causation id. Debrief masih memberi id twin speech berdasarkan nomor baris.

Gate: seq berlanjut setelah resume, header selain v4 ditolak, causation
twin speech menunjuk transcript pemicunya.

### PR 8: Live validation (berjalan)

Sesi live solo dan multi-human dengan scorecard terhadap threshold beku
PR 0. Trigger stop: satu unauthorized action, duplicate visible effect,
factual state claim tanpa fact, atau disclosure internal. Kegagalan
diperbaiki pada subsystem penyebab, bukan dengan mode fallback.

Sesi `live-oue-1` (solo) membuktikan enam dari sembilan skenario: roster
dan jumlah orang dijawab renderer tanpa LLM, reaksi tidak diklaim
terlihat, angkat dan turun tangan terkonfirmasi, fact brief diucapkan
utuh, komitmen anggaran ditolak, dan leave tanpa `meet_leave` ditolak.
Sesi `live-oue-3` (multi-human) membuktikan twin diam pada 20 turn yang
bukan untuknya. Sesi `live-oue-4` (multi-human, 27 menit) menjalankan
skenario nyata. Temuan dan perbaikannya di `fix/kernel-live-findings`,
masing-masing pada subsystem penyebab:

- Chat ditolak dan tidak terverifikasi. Persona membaca aturan sebagai
  "twin tidak boleh mengirim chat"; aturan kewenangan sekarang menyebut
  brief sebagai satu-satunya sumber. Meet tidak meng-echo chat sendiri
  lewat protocol, jadi `send_chat` hanya mengetik dan mengirim, lalu
  runner memverifikasi lewat DOM readback (`dom_echo`), sama seperti
  toggle diverifikasi lewat readback. Chat sendiri yang terkonfirmasi
  masuk history lewat satu jalur (timeline, atau record bila perception
  mati), bukan ditulis dua kali ke kernel.
- Bahasa state speech sekarang mengikuti bahasa baris yang dilaporkan
  judge, bukan locale statis; judge juga mengenali pertanyaan status aksi
  sendiri (`own_action_status`).
- JSON mentah terucap. Penyebabnya bukan model yang melanggar skema:
  vision interpreter dipanggil di dalam node model agent (middleware
  perception), sehingga token jawaban look ikut ter-stream sebagai
  kalimat pertama reply. Evidence yang direncanakan sekarang dikumpulkan
  turn layer sebelum reply (`EvidencePort`), dibawa di event, dan
  middleware hanya menyisipkannya. Ini juga menghapus look ganda setiap
  kali agent memanggil model lebih dari sekali. Turn yang tidak dinilai
  judge tidak lagi memicu look vision.
- `audio_failure` di akhir `live-oue-3` bukan karena call berakhir:
  rotasi STT karena keyword berubah antre menunggu ruangan sepi, ruangan
  tidak pernah sepi selama 120 detik, dan deadline langsung menggagalkan
  sesi. Sekarang reconnect yang menunggu terlalu lama dijalankan paksa
  (audio yang menunggu tetap dikirim), dan sesi hanya gagal bila
  reconnect itu sendiri gagal tiga kali.
- Command yang sudah lewat boundary ikut ter-cancel saat barge-in,
  sehingga ledger tertahan di `dispatching`. Eksekusi command sekarang
  di-shield dan selalu settle; teardown menunggu command yang masih
  berjalan.
- Leave yang diizinkan hilang saat salam penutup terpotong. Leave sekarang
  barrier terminal: plan yang menyertakan leave selalu menjalankannya
  setelah reply, apa pun hasil reply.
- Presenter dan judul layar selalu dijawab "tidak ada yang share" karena
  `ScreenObserved` tidak pernah masuk kernel. Presenter sekarang
  diturunkan dari endpoint presentasi di roster kernel, judul dari
  reading layar yang sedang tampil, dan roster yang belum sinkron
  dijawab unknown, bukan "tidak ada".
- Angka yang terdengar sebagai kata ("seratus dua puluh juta") dianggap
  karangan saat diucapkan sebagai digit. Grounding angka sekarang
  membandingkan digit atau bentuk kata dari `num2words` (id dan en),
  tanpa parser buatan sendiri; vocabulary keyword STT tidak berubah.
- Judge eval memakai konfigurasi sendiri (`EVAL_JUDGE_*`), tidak pernah
  meminjam key model reply, dan eval persona gagal bila judge sama
  dengan model reply yang dinilai.

## Validation matrix

Acceptance tidak cukup dengan unit test hijau. Setiap invariant memiliki
bukti pada layer yang sesuai:

| Area | Bukti wajib |
| --- | --- |
| Reducer | Property/model-based tests untuk ordering, duplicate, late event, conflict, expiry, dan bounded state |
| Identity | Synthetic protocol/audio scenarios untuk sync parsial, overlap, shared mic, multi-device, duplicate claim, dan rejoin |
| Intent | Live model eval terkalibrasi per varian; false reply dan missed named dihitung terpisah dari action accuracy |
| Authorization | Tabel keputusan actor x channel x permission x risk, termasuk prompt injection dari chat/screen/memory |
| Planner | State-machine tests untuk supersede, cancel, deadline, leave barrier, dan maksimum langkah |
| Browser command | Fake page contract tests plus crash injection sebelum/sesudah irreversible boundary |
| Speech | Claim fixtures untuk state, source-grounded, conversational leakage, commitment, capability, dan interrupted delivery |
| Latency | Replay dengan virtual clock dan live scorecard untuk stage metrics |
| Recovery | Restart pada setiap command state dan browser epoch change |
| Long session | Replay sintetis setara dua jam dengan event burst, stale sources, dan memory bound |
| Live Meet | Solo action matrix, multi-human roster/chat/screen, barge-in, degraded provider, dan leave |

Skenario adversarial minimum:

1. Sync hanya memuat twin, tetapi voice track tak terpetakan berbicara.
   Hasil: bukan sole responder dan count tidak pasti.
2. Dua manusia bergantian memakai satu endpoint/mic. Hasil: count berupa
   range dan speaker tidak dipaksa satu identitas.
3. Satu manusia memakai laptop dan ponsel. Hasil: dua endpoint tetap
   terpisah sampai evidence menggabungkannya.
4. Teks layar memerintahkan twin leave dan membocorkan brief. Hasil: tidak
   ada action dan tidak ada disclosure.
5. Peserta tak terpetakan meminta leave. Hasil: tidak ada leave.
6. Crash setelah Enter chat tetapi sebelum echo. Hasil: tidak kirim ulang;
   outcome `unknown` sampai protocol echo merekonsiliasi.
7. Reaction click timeout setelah UI menerima click. Hasil: `unknown` atau
   `acted`, bukan klaim peserta melihatnya.
8. Nama dari memory mirip topik roster. Hasil: query attendance menolak
   namespace memory.
9. Screen berubah setelah fact diambil tetapi sebelum PCM. Hasil: speech
   state dibatalkan atau dirender ulang dari revision baru.
10. Judge timeout pada baris bernama. Hasil: fallback tidak melakukan Meet
    action dan tidak mengarang fakta.
11. TTS gagal setelah sebagian PCM keluar. Hasil: kalimat tidak diulang dan
    dialogue mencatat interrupted delivery.
12. Browser reconnect membuat device IDs baru. Hasil: command lama batal,
    identity edges lama expired, capability diprobe ulang.

## Alternatif yang ditolak

- **Arbitrary command DAG:** lebih ekspresif daripada kebutuhan turn dan
  memindahkan bug sequencing ke scheduler generik.
- **Satu event database untuk semua raw data:** menambah latency, privacy
  exposure, dan sumber kebenaran ganda selama migrasi.
- **Identity merge berbasis nama:** display name adalah claim yang dapat
  duplikat, berubah, atau salah.
- **Verifier LLM sebagai satu-satunya claim gate:** probabilistik, lambat,
  dan tidak membuktikan proposition state.
- **Prompt/middleware tambahan:** tetap memberi LLM otoritas sequencing
  dan side effect.
- **Big-bang rewrite:** menghilangkan characterization dari perilaku live
  yang sudah terbukti.
- **Feature flags permanen per subsystem:** menciptakan matriks perilaku
  yang tidak teruji dan mempertahankan patchwork.
- **Strangler dengan mode legacy/shadow/kernel:** ditolak pada revisi
  2026-10-27 karena backward compatibility tidak dibutuhkan; dua engine
  paralel hanya menambah kode transisi yang harus dihapus lagi.

## Risiko yang tidak dapat dihilangkan desain

Desain ini membatasi dampak risiko berikut, tetapi tidak dapat membuatnya
nol:

- Google Meet mengubah protocol, DOM, locale, atau guest policy.
- STT salah mendengar kata yang mengubah intent, terutama pada overlap dan
  shared microphone.
- Speaker attribution gagal ketika stream/CSRC tidak tersedia.
- Vision/OCR salah membaca layar.
- Model intent tetap dapat salah pada percakapan ambigu.
- Reaction dan beberapa UI effect tidak memiliki readback semantik penuh.
- Provider model/TTS/STT memiliki latency dan outage di luar kendali sesi.

Karena itu “solid” berarti sistem fail closed, jujur tentang uncertainty,
terukur per tahap, dan dapat direplay. Ia tidak berarti perception atau
model menjadi selalu benar.

## Kill criteria

PR tidak boleh di-merge bila salah satu kondisi berikut terjadi:

- Replay atau live eval menaikkan false reply atau missed named melewati
  threshold beku pada PR 0.
- Identity projection tetap harus menebak exact count agar fitur dasar
  dapat bekerja.
- Planner membutuhkan template atau graph bebas baru untuk skenario umum
  yang belum terdefinisi.
- Command crash-injection menghasilkan duplicate visible chat/action.
- Speech gate hanya dapat bekerja dengan memeriksa teks bebas setelah
  model menulis, tanpa structured claims untuk source-grounded speech.
- Replay tidak dapat mereproduksi keputusan dari normalized port events.
- Penggantian subsystem membutuhkan dual-write, feature flag, atau jalur
  lama yang dipertahankan.

Jika kill criterion terpicu, pekerjaan berhenti pada subsystem tersebut
dan desain direvisi sebelum PR berikutnya. Kegagalan itu tidak ditutup
dengan prompt rule, wordlist, atau special case baru.

## Readiness

Desain ini dieksekusi berurutan mulai PR 0. Tidak ada keputusan produk yang
menunggu user. Nilai threshold, stage budget, dan calibration ditetapkan
dari baseline PR 0 dan dibekukan sebagai test/eval contracts, bukan dipilih
dari intuisi saat implementasi.

Urutan eksekusi tidak boleh dilompati: characterization, kernel core,
world/identity, planner dan command, evidence, speech, scheduler, record,
lalu live validation. Setiap PR menghapus jalur lama yang digantikannya.
