# Loop percakapan meeting

Status: direfactor dan diverifikasi offline pada 28 September 2026. Acceptance percakapan live masih terbuka. Spesifikasi: [status proyek](../reference/project-status.md), Tahap 3.

## Tujuan

Hubungkan transkrip final ke meeting agent LangGraph dan masukkan jawabannya ke antrean di meeting yang sama. Pertahankan setiap ucapan yang diterima dan buat akses ke checkpoint-nya berurutan (serial). Ini adalah fondasi percakapan; peserta meeting masih membutuhkan persona, turn-taking, interupsi, konteks visual, dan tool pada halaman Meet yang sebenarnya.

## Keputusan adapter

`processors/frameworks/langchain.py` milik Pipecat 1.12.0 yang terpasang telah diperiksa terhadap kontrak kita. `LangchainProcessor` hanya meneruskan `{transcript_key: latest_text}` bersama `session_id`, menerima token stream berupa string/`AIMessageChunk`, dan memerlukan extra `pipecat-ai[langchain]`. Dictionary update dari graph mentah menghasilkan token kosong melalui adapter tersebut. Adapter itu tidak meneruskan riwayat pesan maupun kontrak `thread_id` kita.

Keputusan: adapter tipis di luar pipeline suara, di `src/fakeyou/conversation.py`. Tidak ada extra Pipecat baru.

## Tanggung jawab

| Komponen | Tanggung jawab |
| --- | --- |
| `TranscriptEvent` / `transcript_event` | Event bertipe dan batas tipis dengan Pipecat; metadata sumber dan identitas payload |
| `LangGraphResponder` | Pesan native, konfigurasi checkpoint, callback tool, dan ekstraksi teks assistant |
| `ConversationLoop` | Satu task memproses ucapan yang tertunda secara berurutan; penahanan jawaban usang dan pembatalan saat close |
| `MeetingConversation` protocol | Runtime bergantung pada kontrak kesiapan, kegagalan, dan cleanup |
| CLI `open_conversation` | Memegang lifetime model, SQLite, Graphiti opsional, dan loop |
| Meeting worker | Admission, kesiapan mikrofon, stop, leave, dan terminal reason |

## Kontrak dan trade-off

- Hanya event final yang tidak kosong yang diterima. Sebelum bind, event ditampung di buffer tanpa menjalankan agent. Runtime melakukan bind setelah admission, kesiapan mikrofon, dan pengiriman sapaan opsional.
- Tepat satu invocation graph memakai thread meeting pada satu waktu. Transkrip final yang tiba selama invocation itu membentuk batch berikutnya; tidak ada yang dibuang dengan cara hanya mempertahankan teks terbaru.
- Transkrip final yang lebih baru membuat jawaban saat ini tidak berlaku sebelum pengiriman ucapan. Graph yang sedang berjalan boleh selesai agar hasil tool dan checkpoint tetap konsisten. Graph dibatalkan saat sesi ditutup atau saat deadline respons 60 detik tercapai, bukan dimulai ulang secara bersamaan.
- Generation guard tidak dapat mengurungkan tool call yang sudah selesai atau ucapan yang sudah dikirim. Tidak ada retry graph otomatis setelah timeout. Idempotensi aksi yang durable tetap menjadi pekerjaan terpisah. Barge-in menghentikan playback aktif melalui engine bila diaktifkan.
- Graph menyimpan jawaban assistant yang dihasilkan, termasuk draf yang ditahan. Event playback/delivery belum ditulis kembali ke riwayat tersebut; jangan perlakukan riwayat itu sebagai catatan dari semua yang didengar participant lain.
- Identitas thread diturunkan dari `owner_id`, `twin_id`, dan `meeting_id`. `HumanMessage.id` memakai id event; LangGraph memegang persistence riwayat dan penggantian pesan.
- Id event duplikat diabaikan selama lifetime loop ini. Adapter frame meng-hash sumber, timestamp asli, dan teks. Saat timestamp tidak ada, adapter memakai id frame lokal. Adapter Muse saat ini membuat timestamp waktu penerimaan, sehingga reconnect yang mentranskripsi ulang audio yang sama dapat menghasilkan id baru. Ini tidak menjamin deduplikasi lintas reconnect atau restart proses.
- `participant` tetap `unknown`: `user_id` Muse saat ini bukan identitas Meet yang terverifikasi. Timestamp event adalah waktu penerimaan, bukan waktu capture mikrofon yang terukur.
- Hanya `AIMessage` final tanpa tool call yang tertunda yang dapat menjadi ucapan. `AIMessage.text` native tidak menyertakan blok reasoning/non-teks. Pesan user dan hasil tool mentah tidak pernah diucapkan secara langsung.
- `acting` di-emit oleh callback tool-start native untuk generation saat ini. `queued` berarti pengiriman ke TTS; ini tidak mengklaim bahwa playback sudah dimulai atau participant remote sudah mendengarnya. `listening` menggambarkan kesiapan loop, bukan keheningan speaker.
- Runtime menutup task percakapan sebelum dependency audio dan agent saat stop, penolakan, removal, kegagalan browser, pembatalan, dan kegagalan startup awal. Kegagalan model/loop menghasilkan `conversation_failure`; error audio tetap `audio_failure`.
- Event yang tertunda dan riwayat dedupe berada di memori proses untuk satu sesi. Ada satu task respons dan deadline per respons, tetapi tidak ada inbox durable maupun kontrol admission multi-worker.

## Penggunaan

```sh
docker compose --profile audio build meeting-worker
docker compose --profile audio run --rm --no-deps meeting-worker \
  fakeyou join https://meet.google.com/xxx-yyyy-zzz \
  --audio --converse --twin-name "Fakeyou AI"
```

`--audio` saja mempertahankan transkripsi dan pengujian `--speak` opsional. `--converse` mengaktifkan jawaban atas transkrip final. `--memory` memerlukan `--converse` dan `--persona`; recall dan ingestion transkrip dibatasi pada namespace owner/twin milik persona. Lihat [persona dan memori](persona-memory.md) dan [ingestion memori](memory-ingestion.md).

Belum ada tool chat/raise-hand/vision yang dipasang pada meeting agent ini. Command browser MCP yang terpisah tidak otomatis menjadi tool pada halaman Meet milik worker.

## Tugas dan verifikasi

- [x] Tes batas bertipe dan duplicate delivery.
- [x] Tes responder serial, startup ber-buffer, timeout/cancel, dan ucapan yang hanya berasal dari assistant.
- [x] Tes riwayat checkpoint LangGraph native dan isolasi identitas.
- [x] Tes kepemilikan runtime, race stop pada sapaan, dan kegagalan yang berbeda-beda.
- [x] Komposisi CLI opt-in, lint, typing, build, regresi browser, dan review independen.
- [ ] Satu percakapan live terkontrol: dua pertanyaan berkaitan, jawaban terdengar, stop saat reasoning, dan hasil pendengaran remote yang diamati secara terpisah.

Bukti terbaru: [laporan refactor conversation](../reports/2026-09-28-conversation-refactor.md). Tugas ber-scope berikutnya: [akses persona dan memori](persona-memory-access.md).

## Dokumentasi yang dibaca

- [Persistence LangGraph](https://docs.langchain.com/oss/python/langgraph/persistence).
- [Short-term memory LangChain](https://docs.langchain.com/oss/python/langchain/short-term-memory).
- [Pembatalan task Python](https://docs.python.org/3.12/library/asyncio-task.html#task-cancellation).
- Pipecat 1.12.0 terpasang: `LangchainProcessor`, `MetaSTTService`, `PipelineWorker`, dan source frame.
- LangChain core terpasang: `AIMessage.text`, `HumanMessage`, dan `AsyncCallbackHandler`; reducer `add_messages` native.
