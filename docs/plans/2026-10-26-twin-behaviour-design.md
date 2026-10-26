# Bentuk twin yang benar: dari temuan live ke perilaku

Status: 2026-10-26. Pasangan dokumen: `2026-10-26-twin-live-findings.md`.

Dokumen ini menjawab satu pertanyaan untuk tiap temuan: **seharusnya twinnya bagaimana?** Prinsipnya mengikuti
roadmap 2026-10-24: kontrol deterministik di kode, bahasa di LLM, setiap perubahan perilaku punya gate eval.

## Enam bentuk twin yang harus berlaku

1. **Tidak pernah mati.** Fault provider, serialisasi, atau socket tidak boleh menghentikan turn. Room tidak boleh
   mendengar kegagalan sistem.
2. **Perintah eksplisit dikerjakan secara mekanis.** "Say that again", "be shorter", "stop talking" adalah perintah,
   bukan percakapan.
3. **Bicara saat diminta, dan diam selain itu.** Baris tanpa nama milik ruangan. Twin tidak mengisi jeda.
4. **Hanya mengatakan hal yang bisa dibuktikan.** Kalau tidak tahu, bilang tidak tahu, dalam satu kalimat, lalu
   berhenti.
5. **Tidak pernah membocorkan wiring-nya.** Bukan system prompt, bukan profile owner, bukan brief, bukan goals --
   dalam wording apa pun.
6. **Setiap turn bisa dijelaskan.** Intent, bahasa, tool, dan latensinya tercatat, sehingga perilaku yang berubah
   bisa ditelusuri kembali.

## Temuan -> perilaku yang benar

| Temuan | Perilaku yang benar | Lapisan | Bukti |
| --- | --- | --- | --- |
| A. Timeout mematikan turn | Twin yang lambat diberi kesempatan kedua sebelum turn menyerah; kalau tetap gagal, ia bilang "Sorry, I missed that." | Kode + katalog | `test_a_slow_model_is_asked_again_before_the_turn_dies`, `test_a_read_timeout_from_a_provider_is_a_retryable_timeout` |
| B. Jalur chat mati | Sesi yang mengizinkan `meet_chat` menjawab lewat chat, di turn yang sama | Kode + brief | `test_meet_chat_tool_binds_only_with_explicit_authority` |
| C. Jawaban tanpa isi | Persetujuan selalu berisi isi: twin menyebut apa yang dia setuju dan apa yang dia tahu | Prompt | `REQUIRED_EVERY_PROMPT_CLAUSES` |
| D. Interupsi aktif tanpa alasan | Layar yang sama ditawarkan sekali; hanya perubahan baru yang bisa ditawarkan; baris tanpa nama tidak dijawab | Kode | `test_the_same_screen_is_never_offered_twice`, `test_the_same_attention_is_never_offered_twice` |
| D2. Gaya tidak ikut bahasa | Gaya persona dibawa ke bahasa participant, register participant dipertahankan | Prompt | `REQUIRED_EVERY_PROMPT_CLAUSES` |
| D3. Cue terkunci ke bahasa config | Cue dan failure line mengikuti bahasa participant | Kode | `test_the_cue_follows_the_language_the_participant_used`, `test_the_failure_line_follows_the_language_the_participant_used` |
| D4. Cue tak tercatat | Cue membawa teksnya ke journal | Kode | `test_join_playback_events_are_journaled_and_finished_speech_recorded` |
| D5. Filler menggantung | Jalur yang boleh berakhir diam tidak membuka dengan filler; filler tanpa jawaban dilaporkan | Kode | `test_the_attention_lane_plays_no_cue`, `test_a_cue_without_an_answer_is_reported` |
| E. Judge terlalu longgar | Baris tanpa nama milik ruangan; twin menjawab kalau ada yang jelas mengarah ke dia | Judge | `test_a_chat_that_names_the_twin_is_answered_in_chat` |
| F. Jawaban meleset target | Yang dijawab adalah baris terakhir, bukan isi meeting view | Prompt | `REQUIRED_EVERY_PROMPT_CLAUSES` |
| G. Latensi 9,9 detik | P50 `final -> reply` di bawah 1,5 detik; model besar hanya untuk turn yang butuh bukti | Konfigurasi + prompt | Aturan "jawab dari yang sudah dikatakan" |
| H. Daftar sesi mati | Data lama dibuang, bukan diberi jalur kompatibilitas | Data | Snapshot test sudah dibuang dari runtime |
| H2. Kalimat identik dua kali | Jawaban kedua untuk pertanyaan yang sama ditulis dengan kalimat lain | Prompt | `REQUIRED_EVERY_PROMPT_CLAUSES` |
| I. Permintaan majemuk | Dua permintaan dijawab dua-duanya | Prompt | `REQUIRED_EVERY_PROMPT_CLAUSES` |
| J. Janji ke masa depan | Twin menjawab dari yang diketahui sekarang, tidak menjanjikan | Prompt | `REQUIRED_EVERY_PROMPT_CLAUSES` |
| K. Goal bocor | Instruction, profile, brief, dan goals tidak pernah dibocorkan, termasuk dengan diparafrase | Prompt | `REQUIRED_EVERY_PROMPT_CLAUSES` |
| L. Plan tak terlihat | Plan, intent, bahasa, dan penggantian intent tercatat | Kode | `test_the_plan_read_is_reported_with_its_intent_and_language` |
| M. Fallback tanpa test | Plan invalid terlihat di journal dan punya test | Kode + test | `test_a_reply_plan_that_does_not_validate_falls_back_to_the_plain_message` |
| N. Layar owner dibaca | Layar yang bukan konten meeting menyimpan judulnya saja, di view, di memori, dan di debrief | Kode | `test_a_screen_that_is_not_meeting_content_keeps_its_title_only`, `test_a_screen_that_is_not_meeting_content_keeps_only_its_title` |
| O. Bahasa participant | Bahasa jawaban mengikuti bahasa participant | Kode | `test_the_declared_language_reaches_the_turn` |

## Keputusan yang mengubah arah, dan alasannya

### Jalur interupsi proaktif mati secara bawaan

Roadmap memberi jalur ini target "interupsi proaktif salah <= 1 per jam". Sesi live mengukurnya di ~13/jam, dan replay
sekarang punya skenario untuk mengukurnya di 1,0 selama sembilan run.

Pengukuran itu juga menunjukkan batas mechanismenya. Trigger datang sebagai baris yang harus dijawab twin, jadi
jawabannya adalah pengakuan: "Got it, I see the launch slide now." Tiga bentuk trigger menghasilkan hal yang sama,
karena bentuk turn menentukan bentuk jawaban, bukan kata-katanya.

Jalur yang hanya bisa mengiya-kan adalah suara, dan suara lebih buruk dari diam di dalam meeting. Jadi:

- `FAKEYOU_ATTENTION__ENABLED` sekarang `false` secara bawaan.
- Kode, trigger, aturan kesegaran, dan skenario replay tetap ada, supaya versi yang bisa membuat remark yang layak
  terdengar bisa dinyalakan dan diukur terhadap target yang sama.
- Layar tetap bisa dibaca twin saat peserta bertanya, lewat tool yang sudah ada.

### State harus bisa diserialisasi

Aturan LangGraph: yang di-checkpoint harus bisa di-encode. Speech graph menyalin iterator-nya ke dalam channel state,
lalu LangGraph mewarisi checkpointer lifecycle ke subgraph dan gagal encode. Stream reply sekarang hidup di runtime
context; state hanya mencatat bahwa ada jawaban.

### Kegagalan checkpoint tidak boleh mematikan meeting

`DegradingCheckpointer` sekarang menurunkan write yang gagal ke laporan, bukan melempar error ke turn. Turn yang
gagal menulis checkpoint tetap berjalan; yang hilang hanya fidelitas resume.

### Data lama dibuang, bukan accommodated

Snapshot sesi lama membawa `allowed_tools` yang tidak lagi dikenal schema sekarang. Snapshot itu dihapus dari runtime
dan kodenya tidak diberi jalur kompatibilitas: schema yang berubah意味着 data lamanya tidak lagi bisa dibaca, dan
itu memang keputusan.

## Sisa pekerjaan, urut

Semua lima butir di bawah sudah dikerjakan pada 2026-10-26 (sesi lanjutan). Yang tersisa hanya bukti live yang
memang butuh sesi meeting sungguhan.

1. **B -- selesai di kode, bukti live menyusul.** Akar masalahnya ternyata bukan sekadar belum pernah diuji:
   `meet_actions_enabled` tidak pernah dinyalakan dari brief, jadi dispatcher tidak pernah dibangun dan jalur ini
   mati total untuk brief mana pun. Sekarang flag itu diturunkan dari izin `meet_act`/`meet_chat` di brief
   (`test_join_meet_actions_gate.py`), otoritas lane reply chat disatukan ke grant tool persona, dan ada test
   assembled sampai ke halaman Meet palsu. Sisa: satu sesi live Meet untuk membuktikan selector dan echo asli.
2. **G -- selesai di kode, angka live menyusul.** Peran `reply` sekarang model kecil untuk setiap panggilan;
   `reply_strong` mengambil panggilan yang menyusun jawaban dari bukti (tool hasil, atau memori prefetch turn ini
   yang menemukan fakta). Meeting view dipangkas (batas 3000 karakter, chat/signal/layar lama dibatasi), view
   sintetis per panggilan tidak lagi menaikkan turn biasa ke model besar, dan report punya metrik
   `final_to_first_sentence` plus split `routed_strong` supaya klaim 1,5 detik bisa diukur jujur per jalur.
3. **E -- selesai dan terukur live.** Keputusan judge sekarang ditegakkan kode: harus `addressee == twin`,
   selesai, dan bukan backchannel; baris tanpa nama memakai ambang lebih tinggi
   (`FAKEYOU_TURN_JUDGE__UNNAMED_REPLY_THRESHOLD`). Replay live 105 penilaian: false-reply 1,4%, tidak ada
   panggilan bernama yang terlewat (`docs/reports/2026-10-26-turn-judge.json`). Satu-satunya false reply adalah
   j28, baris layar berbahasa Inggris yang asli dari temuan F1, di 1 dari 3 run.
4. **D -- selesai, dengan arsitektur berbeda dari draf awal.** Jalur interupsi menyala lagi secara bawaan, tapi
   bentuknya bukan daftar kata acknowledgement. Momen dideteksi semantik: prefilter embedding memilih item
   profile/brief yang dekat, judge momen terstruktur memutuskan jenis momen dan sumbernya, kontribusi wajib
   mengutip sumber terdaftar, lalu verifier grounding memeriksa klaimnya terhadap sumber itu sebelum diucapkan.
   Replay proaktif live (`docs/reports/2026-10-26-proactive-replay.json`): nol kontribusi salah dari jalur ini
   di semua run; kontrol murni selalu diam.
5. **N -- selesai: judul saja di semua lapisan.** Layar bukan konten meeting sekarang dicuci saat disimpan ke
   timeline (`title_only`), dan tidak bisa lagi bocor lewat record JSONL, arsip frame JPG, tool lihat-layar,
   gambar pending yang dulu menempel di pesan pemicu, atau trigger perhatian. Rekaman lama yang sudah terlanjur
   berisi tidak dihapus; guard hilir tetap dipertahankan untuk mereka.
