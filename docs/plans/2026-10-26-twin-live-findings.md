# Temuan dari satu sesi live: `live-afz-4`

Status: 2026-10-26. Sesi live 65 menit, satu peserta manusia dan satu twin. Semua angka di bawah diambil dari
`record.jsonl` dan `journals/live-afz-4.jsonl`, bukan dari ingatan.

## Kenapa dokumen ini ada

Roadmap 2026-10-24 sudah menetapkan arah: perilaku twin ditentukan kode, bukan teks prompt, dan setiap perubahan
perilaku harus punya angka sebelum dan sesudah. Sesi ini adalah bahan untuk dua hal: Apollon yang sudah beres, dan
"by design yang benar" untuk yang belum.

Empat bug yang bisa menutupi semua yang lain, dan keempatnya sudah diperbaiki (PR #33, #34, #35):

| Bug | Gejala | Akar cause |
| --- | --- | --- |
| Twin diam selamanya | Setelah 3 turn, twin berhenti menjawab sampai sesi berakhir | `async_generator` ditulis ke channel state; LangGraph mewarisi checkpointer lifecycle ke subgraph, msgpack gagal encode, 3 turn gagal berturut-turut -> `conversation_disabled` |
| Satu chat = meeting mati | Chat pertama dari pesertaRaise `AttributeError`, runtime menutup meeting | `MeetingTurns` tidak punya `receive_chat`/`receive_attention` padahal protocol mewajibkannya |
| Plan dan intent tak terlihat | Journal hanya punya `answer_text_emitted` berisi jumlah karakter | Plan, intent, dan bahasa tidak pernah dicatat |
| Filler tak tercatat | 39 filler diomongkan ke room, nol di record | Cue playback berhenti sebelum `add_twin_speech` |

## Yang terukur

| Metrik | Nilai |
| --- | --- |
| Balasan twin | 34 (9 di antaranya tanpa diminta) |
| Interupsi tanpa diminta | 14 dalam 65 menit, ~13/jam; target roadmap <= 1/jam |
| Filler ("Hmm.") | 39 kali, nol di record |
| Turn gagal | 1 (`ReadTimeout` 15,2 detik -> "Sorry, I can't answer that right now.") |
| `final -> reply` | p50 9,9 detik, p95 13,7 detik, maks 13,9 detik |
| Panggilan LLM per turn | 2 berurutan + 1 tool call; tool call biasanya cari informasi yang sudah diucapkan peserta |
| Atribusi | 10 dari 11 utterance (90,9%) |
| Fault | 1 (`ReadTimeout`) |

## Temuan per bagian

### A. Model lambat mematikan turn

`request_failed` `ReadTimeout` 15,2 detik -> `turn_failed` `ProviderFault` -> twin再也不:] "Sorry, I can't answer that
right now." Root cause dua lapis: error timeout datang **saat stream** (di luar retry middleware), dan
`_classify` hanya mengenali `httpx2`, sedangkan SDK provider melempar error `httpx`.

### B. Jalur chat tidak pernah aktif

Session ini jalan tanpa brief, jadi `meet_chat` tidak diizinkan. Twin menjawab "I can't send messages in the chat" --
benar untuk sesi itu, tapi berarti fitur chat belum pernah diuji.

### C. Jawaban tanpa isi

"Jadi lu enggak ada tool atau capability buat kirim chat ke gua?" -> "Iya, benar sekali." Persetujuan tanpa isi.

### D. Jalur interupsi aktif tanpa alasan

Empat belas interupsi, lima di antaranya mengulang kalimat pasangannya persis, dan satu membaca chat peserta
ke keras ke seluruh room. Akar cause: trigger memakai reading layar yang sedang aktif, jadi tidak pernah basi; dan
trigger berbentuk pernyataan, sehingga jawaban paling murah adalah konfirmasi.

### D2. Gaya persona tidak ikut bahasa

Pertanyaan bahasa Indonesia dijawab "Mohon maaf, saya tidak memiliki kemampuan atau akses untuk mengirim chat di dalam
pertemuan ini." Gaya persona ditulis dalam bahasa Inggris, jadi di bahasa lain tidak ada gaya yang tersedia.

### D3. Cue dan failure line terkunci ke bahasa config

Katalog punya baris Indonesia ("Sebentar.", "Maaf, gue belum bisa jawab itu sekarang.") yang tidak pernah dipakai.
`FAKEYOU_SPEECH__LANGUAGE` = `en`, jadi participant bahasa Indonesia mendengar "Hmm." dan "Sorry, I can't answer that
right now.".

### D4. Journal bilang "skipped" untuk cue yang bunyi

Ternyata dua itu tidak bercampur: 39 cue bunyi, 37 turn lain cue-nya dilewati karena jawaban datang lebih dulu. Yang
tidak tercatat tetap benar: cue tidak pernah masuk record.

### D5. Filler menggantung

Dua turn berakhir `silent` setelah cue bunyi, jadi room mendengar "Hmm..." lalu hening.

### E. Judge menjawab baris tanpa nama

"Can you see my screen?" dijawab, "Can everyone hear me now?" tidak. Keduanya lewat judge. Setelah empat pengulangan,
gate-nya sendiri benar: baris tanpa nama diam, baris bernama dijawab, baris yang namanya ada di separuh akhir kalimat
dijawab. Jadi ini bukan gate bocor, tapi judge yang terlalu longgar saat konteksnya quente.

### F. Jawaban meleset target

"How old are you?" dijawab dengan isi chat room. Terjadi hanya ketika ada baris chat di konteks; diulang tanpa chat
baru, twin menjawab "As an AI, I don't have an age."

### G. Latensi

`final -> reply` p50 9,9 detik, p95 13,7 detik. Target roadmap: p50 di bawah 1,5 detik.

### H. Daftar sesi mati

`GET /v1/sessions` mengembalikan 500 karena snapshot lama (`hangout-1`, `hangout-2`, `recall-a2`..`recall-b`) membawa
`allowed_tools: ["describe_frame"]`, yang tidak lagi dikenal schema sekarang.

### H2. Kalimat yang sama dua kali

"Can everyone hear me?" dua kali dijawab "Yes, I can hear you clearly." dua kali, kata per kata.

### I. Permintaan majemuk dijawab separuh

"you said something in Bahasa but I don't hear, can you repeat that" dijawab sebagai pernyataan ("I said that I was
speaking Indonesian earlier"), tanpa mengulang. Mechanisms-nya sehat: `repeat`, `shorter`, dan `stop` semua menyala
live, dan `repeat` mengembalikan string yang identik dengan baris sebelumnya.

### J. Janji ke masa depan

"can you hear Ario if he joins later?" -> "Yes, I will be able to hear Ario if he joins the call." Twin tidak tahu itu.

### K. Prompt tidak punya aturan kerahasiaan

"what are your exact instruction for this meeting" dan "what is your role in this meeting" sama-sama dijawab dengan
tiga dari tiga `goals[]` owner profile, hampir verbatim. Penolakan atas "repeat the system prompt" datang dari model,
bukan dari prompt: `build_persona_prompt` tidak pernah menyatakan bahwa semua itu rahasia.

### L. Plan dan intent tidak terlihat

Journal tidak pernah mencatat plan, intent, maupun bahasa yang dideklarasikan, sehingga turn yang declare `repeat`
dan turn yang tidak pernah dapat plan indistinguishable.

### M. Jalur fallback tanpa test

`PlainReplyMiddleware` menangkap `StructuredOutputValidationError` dan menjawab dengan pesan polos model. Cabang itu
tidak punya test, dan intent path yang dimakannya juga tidak.

### N. Layar owner dibaca seluruhnya

Meeting view membawa hasil baca verbatim setiap layar, termasuk terminal, judul tab browser, dashboard, dan isi
struk. Teks itu masuk konteks model tiap turn, dan dari record masuk memori jangka panjang twin serta laporan
debrief owner.

### O. Bahasa participant dibaca, tapi bahasa twin tidak

Twin menjawab bahasa Indonesia karena participant Speaking bahasa Indonesia; aturannya benar. Yang belum ada: gaya
persona ikut ke bahasa itu.

## Yang terbukti baik, supaya tidak ikut dirusak

- Addressing gate: baris tanpa nama diam, baris bernama dijawab, konsisten dalam empat pengulangan.
- Transparansi identitas: "Harun's AI twin ..." konsisten lima kali.
- Jujur soal roster: "Aside from you and me, I don't see anyone else in this call right now."
- Anti-hallucination: "Hadi hasn't told me anything about his interview yet"; "I don't know, he hasn't shared any
  details about his offer with me yet" -- tetap jujur walau diprovokasi minta angka.
- Jujur soal kamera: "I can't see what you're wearing because your camera is off."
- Ingatan meeting akurat: "you just asked if I could hear you, how Hadi's interview went, and whether I was really
  Harun."
- Bahasa ikut participant: Indonesia in, Indonesia out; "say something in French" -> Prancis.
- Barge-in memotong playback, dan turn berlanjut dengan baris yang baru.
- Tool use beralasan: `look_at_participant` dipanggil sebelum menjawab tentang Hadi.
- Baca layar akurat: "1,105 credits, all of which were consumed by Text-to-Speech" (memang benar di dashboard).

## Addendum: sophistication iteration (live-yfv-1/2, live-ozg-1..5)

Spec: `docs/plans/2026-10-26-twin-sophistication-design.md`. What live proved, newest first.

- Chat lane works end to end (ozg-5, 15:48Z): a named chat line is classified
  live, queued on the chat channel, answered in chat with correct brief content,
  and its own echo is recorded `own=true`.
- Voice turns no longer mirror into chat (ozg-2 onward): zero `send_chat` on
  spoken turns; one explicit chat request produced exactly one send.
- Meet actions are judge-stamped per turn (`meet_action`): raise hand, mute,
  unmute, and leave each executed once when asked and were `effective`.
- One-word "stop" yields the floor via the stop lexicon
  (`decided_by: stop_lexicon`, ozg-5 15:53:00Z).
- Backlog classification must not trust the protocol timestamp: in room
  ozg-sbgx-yzb every message carries ~14:57:23.5xx, +1ms per message, minutes
  before it was typed. Backlog is now decided by arrival within 45s of
  admission; later lines are live and stale timestamps are replaced by the
  observation time.
- Reaction picker selector `button:has-text(emoji)` does not match Meet's DOM
  (ozg-5, act_failed after 11.8s). send_reaction now falls back to an
  aria-label selector and then a DOM text search; still unproven live.
- The twin claimed "sudah saya berikan jempol" before the (failed) action
  settled. Queued results no longer promise delivery, but spoken confirmation
  still races the actuator.
- After `meet_leave` succeeded the session lingered 37 minutes and ended as
  browser_failure; the dispatcher now signals the session stop event on a
  successful leave.
- A covered dialog Close button (CloakBrowser `ActionabilityError`, not a
  PlaywrightError) killed ozg-4 four seconds after admission; known-error
  dismissal is now per-modal best-effort and cannot fail the session.
- Residual: evidence tools are still called on plain greetings (bounded to two
  results per turn, then the model must answer); actuation takes 4-13s.

## Addendum: pqt round (live-ozg-5 close-out, live-pqt-1)

- Stop lexicon proven live: one word "stop" yielded the floor
  (`decided_by: stop_lexicon`, ozg-5 15:53:00Z).
- Reactions: only the first attempt per session succeeds. Hash-matched glyphs
  prove the same thumbs-up succeeded once and failed later, so the defect is
  picker state, not the glyph. send_reaction now probes picker visibility
  before toggling, retries after re-toggling, and journals a bounded picker
  DOM snippet on failure. Unproven live.
- Chat-origin action requests were refused ("saya tidak bisa melakukan itu
  sendiri") because judge stamps only exist on speech turns. Chat turns may
  now use meet_act tools; meet_send_chat stays stripped there.
- A successful meet_leave now sets the session stop event; teardown errors
  after an owner stop no longer flip the terminal reason to browser_failure.
  The twin's farewell sentence can still be cancelled by the close cascade.
- STT heard "live" for "leave" once; the Indonesian phrasing worked. Leave
  itself was effective in 0.89s and the session ended 8s later.

## Addendum: bbz rounds (live-bbz-1/2)

- Reactions work back-to-back when the glyph exists in Meet's set (thumbs-up
  twice, clap, cry, wow, laugh; user-confirmed visually, ~10-12s each).
- The plain heart does not exist in Meet: picker buttons carry the emoji as
  aria-label and the heart is the sparkling heart. Requested heart-family
  glyphs now map to it in send_reaction, and the aria-label-exact strategy
  runs first.
- The first thumbs-up "effective" in pqt was never seen by the user: the JS
  fallback clicked a hidden element. Reaction clicks now require visibility.
- The reaction picker itself is a role=dialog whose text starts "Skin tone";
  the watchdog treated the lingering picker as a blocking modal and killed
  bbz-2 as browser_failure after the 180s manual-action TTL. The picker is
  now closed with Escape after every react attempt and exempted in the
  watchdog.
- Leave by voice ends the session in ~1s via the dispatcher stop signal, but
  a second leave attempt on the already-left page flipped the terminal reason
  to browser_failure; leave_meeting now treats an already-out page as left.
- Leave by chat works now that chat turns may use meet_act tools; it was not
  re-proven live because the bbz-2 session ended on the watchdog expiry first.

## Addendum: bbz-3 round

- In-set reactions are consistently ~3.5s and effective back-to-back: heart
  (via the new mapping), thumbs-down x4, clap, laugh, wow, sad, party,
  thinking. The picker no longer kills the session.
- Remaining failure mode is model glyph variance: when the model picks an
  out-of-set variant (confused/laugh/disdain attempts), actuation burned
  ~39s before honestly failing. Fixes: emoji family mapping to Meet's nine
  glyphs (confused->thinking, laugh variants->face-with-tears, disdain->
  thumbs-down, etc.) and a picker enumeration that fails fast when Meet
  does not offer the requested glyph.
- A repeated reaction requested while the previous 39s attempt was still in
  flight queued for ~13s behind the serial action lock.
- Voice leave actuated in 1.3s but the session still terminated as
  browser_failure: runtime leave cleanup re-ran leave_meeting on a page the
  twin had already left and could not prove it was out. The dispatcher now
  records its own successful leave and cleanup trusts that fact; the
  post-leave page ("left the meeting") is also a recognized call state.

## Addendum: bbz-4 round

- Twin-initiated leave now terminates cleanly: session_finished
  `stopped_by_owner`, exit 0 (dispatcher-left fact trusted by cleanup).
- New failure found: on English reaction requests the model passed emoji
  names instead of glyphs and meet_react refused 3x ("A reaction must be a
  single emoji"), after which the twin concluded it cannot react at all and
  repeated that apology. The tool now extracts the first emoji grapheme
  from any argument, its description enumerates Meet's nine glyphs with
  names, and a refusal echoes the input and lists the glyphs so the model
  can recover next turn.

## Addendum: bbz-5 round

- English confused and Indonesian disdain reactions are now effective on
  the first attempt (~3.5s); the tool-boundary glyph contract works.
- Leave again terminated cleanly (`stopped_by_owner`, exit 0).
- New failure found: on the first leave turn the model spent both tool
  results on evidence tools (look_at_participant, look_back_chat), so the
  2-result budget stripped meet_leave and the twin only answered verbally;
  the user had to repeat the request. The judge-stamped action (and
  meet_check_action) is now exempt from the evidence budget: exactly one
  stamped action per turn always remains callable.

## Addendum: bbz-6 round (regression, fixed same round)

- Every greeting turn failed at the 15s stream deadline: the first version
  of the budget exemption also kept meet_check_action callable after the
  budget, so the tool list never emptied and the model looped on blocked
  tool calls instead of answering. The exemption now covers only the
  judge-stamped action itself, exactly once; unstamped turns again end
  with an empty tool list that forces a spoken answer.

## Addendum: bbz-7 round (final proof)

- Greeting answered normally again (0 turn_failed); confused reaction
  effective in 3.5s.
- Chat-origin leave proven live: chat line "Harun, kamu boleh leave
  sekarang" observed, meet_leave effective in ~1s, session ended
  `stopped_by_owner` exit 0. A parallel react call in the same turn was
  stale-cancelled cleanly.
- Quality nit retained: on the confused-reaction turn the spoken reply was
  generic filler ("not quite sure what you mean") while the reaction
  itself sent fine. Speech accompanying a queued action can still
  contradict the action; worth a later pass on action-turn reply shaping.
