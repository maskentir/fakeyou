# Kemampuan Digital Twin di meeting

Targetnya peserta yang merespons dan bertindak dalam konteks meeting. Transcription dan rangkuman hanya sebagian kemampuan.

| Kemampuan | Fondasi | Kontrak tahap partisipasi |
| --- | --- | --- |
| Mendengar | Muse melalui Pipecat | Transcript partial/final, bahasa campuran, overlap, timestamp sumber, reconnect sebelum batas sesi |
| Mengenali pembicara | Diarization dan pelajaran Vexa | Speaker label dipetakan ke participant ID dengan confidence; unknown tidak ditebak |
| Melihat | Screenshot dan DOM Playwright MCP | Frame screenshare/camera, perubahan slide, timestamp; pemahaman memakai model multimodal |
| Mengingat | LangGraph + Graphiti | State meeting terpisah dari fakta lintas meeting; provenance, koreksi, namespace pemilik |
| Berpikir sebagai pemilik | LangChain + persona | Profil, gaya bicara, tujuan, pengetahuan, kewenangan, pengakuan ketidakpastian |
| Berbicara | TTS Pipecat | Streaming ke mic virtual; stop saat disela; audio yang dibatalkan tidak diputar ulang |
| Memilih giliran | VAD/endpointing dan LangGraph | Mengenali pertanyaan untuk twin, peserta lain berbicara, waktu raise hand, diam atau klarifikasi |
| Menggunakan tangan | Input tools Playwright MCP | Raise/lower hand, chat, mute/unmute, perangkat, leave; observasi hasil setelah tindakan |
| Mengikuti keadaan meeting | Participant/UI/media events | Join/leave, host action, meeting selesai, screenshare berubah, disconnect |
| Menampilkan diri | Provider video/avatarnya dipilih kemudian | Kamera virtual yang benar-benar mengirim media ke Meet |

## Prinsip

1. Media menggunakan runtime Pipecat. Ekstraksi memori dan evaluasi tidak memblokir audio.
2. Event membawa meeting ID, timestamp, sumber, dan status final. Transcript partial tidak langsung dijadikan fakta permanen.
3. Pengetahuan pemilik dibedakan dari ucapan peserta. Instruksi pada slide/chat adalah data meeting, bukan otomatis perintah pemilik.
4. UI automation memakai tool dan observasi. Click generik belum menjamin alur raise hand Meet selesai.
5. Bicara dan chat menghasilkan efek eksternal. Resume checkpoint memeriksa apakah aksi sudah terjadi sebelum mengulang.
6. Worker menyediakan stop dari pemilik, cleanup saat disconnect, deadline lobby, dan batas durasi meeting.

## Bukti yang masih diperlukan

Muse sudah lulus uji live dengan satu fixture audio sintetis Bahasa Indonesia. Kualitas pada percakapan multipihak, TTS live, latency percakapan, dan barge-in belum terverifikasi. Klik/screenshot lokal belum membuktikan guest admission. Evaluasi deterministik membuktikan eksekusi tool, bukan kualitas persona. Media bridge dan kebijakan partisipasi merupakan pekerjaan berikutnya; lihat [status dan urutan pembangunan](project-status.md).
