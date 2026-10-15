# Fondasi suara

Jalur suara tidak memakai Pipecat. Modul `audio/` dan `transcription/` menjalankan graf audio dan klien Muse sendiri. Alasan dan urutan penggantian ada di `docs/plans/drop-pipecat.md`.

## Capture

Ada dua sumber PCM mono 16 kHz, dan keduanya masuk ke antrean `PcmQueue` yang sama:

- Chromium di slot Meet mengirim PCM lewat `StreamAudioInput` (`audio/stream_input.py`). Audio yang datang sebelum sesi siap disimpan sampai 5 detik.
- Mode native membaca source PulseAudio dengan `parec` (`audio/capture.py`). Source default-nya `meeting_audio.monitor` (`PULSE_SOURCE` di image audio). `parec` mengikuti jalur yang sama dengan `paplay` untuk output.

Kedua sumber dibaca oleh `AudioGraph` (`audio/graph.py`). Urutan per chunk: aktivitas dan probe atribusi, turn tap (untuk klip voiceprint), kirim ke STT, lalu VAD dan event bicara. Pre-roll sudah dihapus. STT menerima audio kontinu, sehingga replay pre-roll hanya menggandakan audio.

## VAD

`audio/vad.py` memakai model Silero v5 (ONNX) yang di-vendor di `audio/models/`. Lapisannya:

- `VadAnalyzer`: confidence model dan volume per jendela 512 sampel. Volume memakai BS.1770 dari paket `loudness` (400 ms, dinormalisasi ke 0..1, dihaluskan dengan faktor 0,2).
- State machine: `QUIET`, `STARTING`, `SPEAKING`, `STOPPING`, dengan `start_secs` dan `stop_secs` dari `FAKEYOU_VAD__*`.
- `VadController`: event `started`, `stopped`, dan `activity` setiap 0,2 detik audio selama bicara. Jika tidak ada audio selama 1 detik saat bicara, controller menghentikan bicara dan mengembalikan analyzer ke quiet.

Hasil VAD dicocokkan per jendela dengan golden dari Pipecat 1.12 (`tests/fixtures/vad-golden-standup-16k.json`). Confidence dan volume selisih paling besar 1e-5, dan state serta urutan event identik. Ada dua perbedaan yang disengaja. Reset state model berjalan setiap 5 detik audio, bukan waktu dinding, sehingga hasil replay sama dengan live. Idle timeout juga mengembalikan analyzer ke quiet, sehingga bicara berikutnya selalu diumumkan.

## Speech-to-text Muse

`transcription/muse.py` memiliki socket. Klien ini tidak merekonek sendiri.

- Handshake: `mode` (default `DIARIZATION`, lihat `FAKEYOU_STT__MODE`), `partialMode` `CUMULATIVE`, `audioEncoding` `PCM_16KHZ`, `languageBias` dengan nama bahasa Inggris (`English` atau `Indonesian`, bukan kode), dan `keywords` dari `speech.stt_keywords`.
- Event giliran: `speechStart`, transkrip parsial, `speaker`, `speechEnd`, `speechComplete`. Teks final diambil dari `speechComplete.transcript` dan di-strip. Final pada `transcript` dengan `final: true` yang berisi teks hanya diteruskan bila tidak ada giliran terbuka. Flush kosong di akhir sesi diabaikan.
- Label `speaker` dilampirkan ke giliran yang sedang terbuka sebagai `TranscriptUpdate.voice_clusters`. Label ini adalah cluster per sesi (`A`, `B`), bukan identitas participant. Label yang tiba setelah giliran ditutup dicatat sebagai `stt_speaker_dropped`.
- Backlog ke Muse dibatasi 4 detik. Audio yang melewati batas dibuang dari depan dan dicatat sebagai `stt_audio_dropped`. Muse menolak aliran yang lebih lambat dari real-time, jadi saat capture diam klien mengirim silence 100 ms setiap 100 ms setelah 0,5 detik tanpa audio (`stt_keepalive_started`).
- Socket yang putus dilaporkan lewat `on_lost`. `MeetingAudioSession` memutuskan rekonek: antrean, backoff 5 detik lalu dikali dua dengan jitter, maksimal tiga percobaan, dan batas 120 detik.

Batas sesi Muse 60 menit berasal dari dokumen Meta dan belum diuji live. Jalur rekonek untuk kasus ini sudah ada, tetapi buffer 4 detik lebih pendek dari jeda rekonek terpanjang.

## Sintesis ucapan

Output dipilih lewat `FAKEYOU_TTS__PROVIDER`: `openai`, `minimax`, `cartesia`, atau `elevenlabs`. Setiap adapter ada di `speech/` dan berbicara HTTP langsung dengan `httpx2` atau SDK OpenAI. Engine menangani satu retry untuk timeout sintesis. Stream yang macet melewati batas waktu dilaporkan sebagai fault `tts` `timeout`. Retry streaming hanya diizinkan sebelum chunk PCM pertama yang tidak kosong, dan stream parsial yang gagal dihentikan tanpa replay. Pembatalan menutup iterator provider dan membersihkan proses.

`speech.language` menentukan instruksi gaya bicara OpenAI, `language_boost` MiniMax, dan bahasa yang diminta Cartesia dan ElevenLabs. Bahasa yang didukung adalah `id` dan `en` (`speech/languages.py`).

## Batas kalimat

Balasan agent dipotong sampai `FAKEYOU_CONVERSATION__MAX_SPOKEN_SENTENCES` kalimat (default 2) oleh `sentence_end` di `foundation/sentences.py`. Fungsi itu memakai `sentencex` dan menjaga batas untuk singkatan, desimal, dan tanda kutip penutup. Golden-nya diambil dari `pipecat.utils.string.match_endofsentence` sebelum Pipecat dihapus. Teks `--speak` eksplisit dari operator dikecualikan. Balasan lengkap tetap ada di riwayat graph, termasuk draf yang tidak pernah terdengar.

## Barge-in dan giliran

VAD tetap menjadi urusan graf audio. Kebijakan interupsi berada di loop percakapan. Barge-in dipicu oleh transkrip interim yang memiliki minimal `BARGE_IN_MIN_WORDS` kata saat twin sedang bicara. Interjeksi pendek menghentikan playback tanpa membalas. Ack pendek hanya dijawab jika twin baru saja bertanya.

## Heartbeat capture

Heartbeat capture dan energi adalah sinyal yang terpisah. Frame dengan RMS nol dan kehadiran participant yang baru terdeteksi menghasilkan event `capture_silence_detected`, bukan bukti bahwa capture rusak. Frame yang hilang dan ucapan tanpa final mengikuti kebijakan fault dan grace yang sudah ada.

## Referensi

- [Meta Muse Voice Transcribe API](https://dev.meta.ai/docs/speech-to-text)
- [Cookbook streaming Meta](https://dev.meta.ai/docs/cookbook/voice-api-fundamentals)
- [Silero VAD](https://github.com/snakers4/silero-vad) (MIT)
- [Smoke test live Muse](../runbooks/live-muse-test.md)
