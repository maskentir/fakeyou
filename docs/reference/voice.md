# Fondasi suara

Modul voice memakai adapter Pipecat native untuk capture. `create_muse_stt` membuat `MetaSTTService` untuk Muse Voice Transcribe dengan endpointing oleh model; bahasa mengikuti `FAKEYOU_SPEECH__LANGUAGE` (`id` atau `en`), dan pemanggil dapat meneruskan beberapa language bias, keyword, atau mode `PUSH_TO_TALK` / `DIARIZATION`. Output ucapan dipilih lewat `FAKEYOU_TTS__PROVIDER`: `OpenAISpeechSynthesizer` memakai API streaming response OpenAI native, dan `MiniMaxSpeechSynthesizer` memakai SSE `t2a_v2` MiniMax; keduanya menghasilkan PCM mentah 24 kHz. Hanya jalur streaming yang tersedia; jalur buffered dihapus karena tidak pernah dipakai di produksi.

Implementasi Pipecat `1.12.0` yang sudah dirilis adalah adapter Muse yang didukung. Pasang dengan `pipecat-ai[meta]`; extra tersebut kosong pada rilis itu karena adapter memakai dependency WebSocket inti Pipecat. Sintesis memakai SDK OpenAI atau `httpx2` secara langsung, di luar pipeline Pipecat. `speech.language` juga menentukan instruksi gaya bicara OpenAI dan `language_boost` MiniMax.

Pipeline voice bersifat capture-only: `transport.input()`, STT, lalu transcript observer yang meneruskan frame transkripsi final ke sesi meeting. Contoh resmi Pipecat `voice-openai.py` merangkai loop percakapan yang lebih lengkap (`transport.input()`, STT, user aggregator, LLM, TTS, `transport.output()`, assistant aggregator); proyek ini menjaga orkestrasi dan playback tetap di luar Pipecat setelah TTS di dalam pipeline sebelumnya menghasilkan suara patah-patah pada run Meet live. `PlaybackEngine` serial kini mengalirkan PCM ke dalam `paplay` pada `agent_output`. Engine ini melakukan mute pada sink output di antara ucapan dan membiarkan source microphone tidak di-mute.

Engine menangani satu retry untuk timeout sintesis; client speech milik synthesizer menonaktifkan retry SDK. Stream yang macet melewati batas waktu dilaporkan sebagai fault `tts` `timeout`, bukan timeout playback. Retry streaming hanya diizinkan sebelum chunk PCM pertama yang tidak kosong. Stream parsial yang gagal dihentikan tanpa replay. Pembatalan menutup iterator provider dan membersihkan proses player (reap). Dependency journal dan fault diinjeksikan secara eksplisit lewat factory audio.

Balasan agent memakai `match_endofsentence` milik Pipecat untuk menerapkan batas kalimat yang diucapkan (`FAKEYOU_CONVERSATION__MAX_SPOKEN_SENTENCES`, default 2), dengan menjaga batas singkatan, desimal, dan tanda kutip penutup. Teks `--speak` eksplisit dari operator dikecualikan. Balasan lengkap yang dihasilkan tetap ada di riwayat graph, termasuk draf yang tidak pernah terdengar.

Komposisi ini melakukan capture lewat transport audio yang sungguhan. CloakBrowser dan Playwright mengoperasikan halaman Meet dan UI browser; keduanya tidak menyediakan objek transport Pipecat. Audio remote dari Meet masuk lewat source PulseAudio `meeting_audio.monitor`, dan playback mencapai microphone browser melalui remap `agent_mic` dari `agent_output`.

Mode `ENDPOINTING` Muse mengeluarkan batas ucapan yang dideteksi oleh model dan frame transkrip final. `PUSH_TO_TALK` menyerahkan penentuan batas giliran kepada client. `DIARIZATION` menambahkan atribusi speaker dalam lingkup sesi; mode ini tidak memberikan identitas participant yang terverifikasi. VAD tetap menjadi urusan pipeline; kebijakan interupsi berada di loop percakapan. Barge-in dipicu oleh transkrip interim dengan minimal `FAKEYOU_CONVERSATION__BARGE_IN_MIN_WORDS` kata (default 3) dan oleh transkrip final; keduanya menghentikan playback yang aktif/antre, dan satu interupsi dicatat sekali sebagai `barge_in_detected`.

Heartbeat capture dan energi adalah sinyal yang terpisah. Frame dengan RMS nol dan kehadiran participant yang baru terdeteksi menghasilkan event journal `capture_silence_detected`, bukan bukti bahwa capture rusak. Frame yang hilang dan ucapan tanpa final mengikuti kebijakan fault/grace yang sudah ada. Lihat laporan verifikasi untuk cakupan yang terukur dan batasan live.

Pada Pipecat 1.12.0, adapter Muse mencatat event `speaker`, `speechStart`, dan `speechEnd` secara internal. Adapter tidak memetakan event speaker ke identitas participant pada frame transkripsi. Pipeline di hilir tidak dapat memperlakukan `user_id` transkripsi saat ini sebagai diarisasi Muse yang terverifikasi. [Smoke test live](../runbooks/live-muse-test.md) memeriksa transkripsi final dalam mode `ENDPOINTING`.

Pipecat 1.12.0 juga menyertakan `LangchainProcessor` di `pipecat.processors.frameworks.langchain`. Processor ini menerima `Runnable` LangChain, hanya mengekstrak pesan plain-text terbaru dari sebuah `LLMContextFrame`, dan mengalirkan output `AIMessageChunk` atau string sebagai objek `TextFrame`. Ini adalah jembatan ringan text-in/text-out: ia tidak meneruskan daftar pesan lengkap, tool call, atau konten multimodal ke runnable. Ia juga memerlukan extra terpisah `pipecat-ai[langchain]`, yang bukan bagian dari kumpulan dependency fondasi ini. Untuk agent LangGraph/LangChain yang sudah ada, pertahankan orkestrasi di luar pipeline voice sampai ada adapter khusus yang memegang kontrak pesan dan tool. Jalur layanan LLM native Pipecat adalah opsi yang kompatibel untuk pipeline percakapan yang lengkap.

Referensi:

- [Source Muse Pipecat 1.12.0](https://raw.githubusercontent.com/pipecat-ai/pipecat/v1.12.0/src/pipecat/services/meta/stt.py)
- [Metadata package Pipecat 1.12.0](https://raw.githubusercontent.com/pipecat-ai/pipecat/v1.12.0/pyproject.toml)
- [Contoh pipeline voice Pipecat](https://github.com/pipecat-ai/pipecat/blob/main/examples/voice/voice-openai.py)
- [LangchainProcessor Pipecat 1.12.0](https://raw.githubusercontent.com/pipecat-ai/pipecat/v1.12.0/src/pipecat/processors/frameworks/langchain.py)
- [Meta Muse Voice Transcribe API](https://dev.meta.ai/docs/speech-to-text)
- [Cookbook streaming Meta](https://dev.meta.ai/docs/cookbook/voice-api-fundamentals)
