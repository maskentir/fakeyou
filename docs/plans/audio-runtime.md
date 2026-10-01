# Runtime audio meeting

Status: selesai dan terverifikasi live (`audio-bua-1`). Spesifikasi: [rencana runtime meeting](meeting-runtime.md), Tahap 2.

## Tujuan

Menjalankan satu guest Google Meet per container Linux, mentranskripsikan audio participant lewat Muse, dan memutar satu kalimat TTS yang diminta secara eksplisit melalui microphone guest. Admission oleh host tetap diperlukan. Kebijakan percakapan menjadi bagian Tahap 3.

## Kontrak

- Gunakan ulang image CloakBrowser resmi, perangkat PulseAudio native, serta `LocalAudioTransport`, `PipelineWorker`, dan `WorkerRunner` milik Pipecat.
- Output browser masuk ke `meeting_audio`; Muse menangkap `meeting_audio.monitor` pada 16 kHz mono PCM16.
- Sintesis OpenAI tetap pada sample rate native 24 kHz. `PlaybackEngine` serial mengalirkan PCM langsung ke `paplay` pada `agent_output`; `agent_mic` me-remap monitor tersebut untuk Chromium. Engine hanya melakukan mute pada sink output di antara ucapan dan membiarkan source microphone tidak di-mute agar Meet tidak menampilkan dialog OS-mute. Synthesizer tanpa dukungan streaming memakai playback buffered.
- Sumber kebenaran untuk nama sink/source dan sample rate adalah `src/fakeyou/audio.py`; `compose.yaml`, `docker/audio/Dockerfile`, `docker/audio/default.pa`, dan `docker/audio/daemon.conf` menyimpan salinan literal yang dijaga tetap sinkron, dan entrypoint memastikan (assert) sink, rate, serta default yang diharapkan saat startup.
- Tidak ada loopback yang menghubungkan output agent ke input Muse. Setiap meeting memiliki server PulseAudio dan lock profile browsernya sendiri.
- Worker memulai audio setelah admission, mengaktifkan microphone, lalu mengantrekan satu kalimat eksplisit yang opsional. Kegagalan audio mengakhiri sesi dengan alasannya sendiri dan melepaskan resource.
- Override environment PulseAudio untuk browser hanya berlaku pada proses anak. Tidak ada perangkat audio fisik milik host yang di-mount.
- API key masuk saat runtime melalui `.env` yang sudah ada; build context tidak menyertakan kredensial maupun rekaman lokal.
- Sintesis membawa timeout per request (default SDK akan menimpa client HTTP), dan engine juga membatasi setiap panggilan sintesis, sehingga satu request yang mati menggagalkan ucapan itu alih-alih membuat ucapan berikutnya macet. Menghentikan playback membatalkan sintesis yang sedang berjalan dengan cara yang sama seperti mematikan audio yang aktif.
- Sintesis memakai streaming HTTP OpenAI native dan instruksi penyampaian dalam bahasa Indonesia santai. Timeout di-retry satu kali sebelum PCM apa pun dikeluarkan; stream parsial tidak pernah diputar ulang. Lihat [keandalan voice](voice-reliability.md) dan laporan refactor-nya.
- Worker melewati (bypass) DNS forwarder tertanam milik Docker Desktop dengan resolver publik; forwarder itu berhenti me-resolve di tengah panggilan pada run live.
- Jaga agar kode tetap terbaca tanpa komentar atau docstring. Gunakan adapter tipis di sekitar API native.

## Tugas dan verifikasi

1. Tambahkan image Linux yang dipin, konfigurasi PulseAudio, adapter default ALSA, dan worker Compose opt-in. Verifikasi startup, pemetaan sink/source, dan penghentian proses di ARM64.
2. Implementasikan audio context Pipecat dengan callback transkrip final, ucapan eksplisit, deadline startup, serta propagasi kegagalan provider/latar belakang. Uji pipeline native lewat tes offline.
3. Integrasikan context tersebut dengan admission, stop, pembatalan, status microphone, flag CLI, dan checkpoint lifecycle. Verifikasi bahwa perilaku guest default tidak berubah.
4. Uji audio container yang sungguhan: output browser ke capture, output TTS ke microphone browser, dan isolasi antara kedua jalur. Jalankan Muse terhadap fixture yang diketahui dengan transport native.
5. Upayakan satu verifikasi Meet live yang terkontrol ketika host tersedia. Catat bukti lokal secara terpisah dari konfirmasi bahwa participant remote benar-benar mendengar.
6. Jalankan lint, typing, tes offline, probe container, dan review. Perbarui runbook dan status proyek dengan keterbatasan yang tersisa.

## Dokumentasi yang diperiksa

- [Dockerfile CloakBrowser](https://github.com/CloakHQ/cloakbrowser/blob/main/Dockerfile) dan [entrypoint](https://github.com/CloakHQ/cloakbrowser/blob/main/bin/docker-entrypoint.sh).
- [Konfigurasi client PulseAudio](https://manpages.debian.org/trixie/pulseaudio/pulse-client.conf.5.en.html) dan [konfigurasi daemon](https://manpages.debian.org/trixie/pulseaudio/pulse-daemon.conf.5.en.html).
- [Pipecat OpenAI TTS](https://docs.pipecat.ai/api-reference/server/services/tts/openai), serta source transport, worker, runner, STT, dan TTS Pipecat 1.12.0 yang terpasang.
- Entrypoint lokal dan tes idempotensi audio milik LMA untuk pelajaran routing; tidak ada kode aplikasi yang disalin.
