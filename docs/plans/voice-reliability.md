# Rencana keandalan voice loop

Status: diimplementasikan pada 29–30 September 2026 dan direfactor pada 30 September; lihat laporan refactor dan verifikasi. Run live historis: `027fe902e88d` dan `8581ccc34446`. Log lokal keduanya menunjukkan aktivitas percakapan, tetapi tidak mengukur latency first-audible dan tidak secara mandiri membuktikan setiap kriteria acceptance di bawah. Implementasi berikutnya: [persona dan memori ber-scope](persona-memory.md).

## Tujuan

Buat voice loop pada fase admitted menjadi cepat, tangguh, dan mudah didiagnosis: jawaban mulai dalam hitungan detik, kegagalan TTS sementara tidak menghilangkan giliran, barge-in terverifikasi secara live, dan jalur capture yang mati dapat dibedakan dari ruangan yang sepi.

Bukan tujuan: mengganti cascade STT→LLM→TTS dengan provider speech-to-speech (spike hanya jika cascade masih belum memenuhi standar setelah streaming), atribusi speaker, perekaman, dan ingestion memori.

## Kriteria keberhasilan

- Audio jawaban pertama yang terdengar mulai dalam hitungan detik setelah transkrip final pada giliran normal (tidak ada lagi jeda `kok diam`).
- Nol dialog `Microphone muted by system` sepanjang sesi live penuh.
- Satu timeout TTS sementara tidak lagi menghilangkan giliran.
- Over-talk yang disengaja dengan `--barge-in` menghentikan playback dan menghasilkan jawaban baru.
- Suite penuh, `mypy --strict`, dan `ruff` tetap hijau; keheningan total dari capture menjadi verdict yang diatribusikan (item tindak lanjut), bukan ketiadaan bukti.

## Pendekatan

Terapkan perbaikan struktural termurah lebih dulu, dengan urutan berikut: alirkan audio TTS langsung ke paplay (pelajaran Vexa — engine sudah memiliki cabang streaming, hanya jalur full-buffer yang tersambung); batasi jawaban agent yang diucapkan menjadi dua kalimat secara deterministik (persona sudah meminta ini tetapi model melanggarnya saat live, sehingga penegakannya tidak boleh bergantung pada kepatuhan model); retry sintesis TTS tepat satu kali saat timeout; verifikasi barge-in secara live sebelum menulis kode interupsi baru; lalu tambahkan liveness capture berbasis energi sebagai tindak lanjut. Tidak ada penggantian provider dan tidak ada lapisan abstraksi baru dalam rencana ini.

## Langkah

1. Streaming TTS → paplay.
   - Tambahkan `stream(text)` ke synthesizer speech OpenAI di `src/fakeyou/voice.py`: async iterator berisi chunk PCM s16le melalui API speech streaming milik client (verifikasi bentuk chunk terhadap SDK terpasang selama implementasi).
   - Teruskan `stream_audio=create_paplay_stream_runner(...)` dan aktifkan streaming di `open_meeting_audio` (`src/fakeyou/audio.py`). Pertahankan full-buffer sebagai fallback saat `stream` tidak ada (engine sudah bercabang berdasarkan keberadaannya).
   - Perlakukan `EPIPE` pada stdin paplay saat cancel sebagai cleanup yang diharapkan (pola LMA), bukan fault.
   - Tes: urutan/cancel/fallback streaming di `tests/test_voice.py`, `tests/test_playback.py`.
2. Batas dua kalimat yang deterministik pada jawaban agent di `src/fakeyou/conversation.py`, sebelum ucapan dikirim. Teks `--speak` dikecualikan (input eksplisit operator). Sisanya dibuang untuk suara dan tidak pernah dimasukkan ke antrean; teks lengkap tetap ada di riwayat checkpoint. Tes: batas 1/2/5 kalimat ditambah pengecualian `--speak`.
3. Retry sintesis TTS satu kali saat `TimeoutError` atau timeout provider yang terklasifikasi, di `PlaybackEngine.speak` (`src/fakeyou/playback.py`), dengan satu budget terbatas per percobaan. Nonaktifkan retry SDK tambahan pada client speech milik kita. Untuk streaming, retry hanya sebelum ada PCM tidak kosong yang dikeluarkan; jangan pernah memulai ulang ucapan yang sudah terdengar. Catat `synth_retry` di journal. Tidak ada retry untuk error auth/validasi/playback. Tes: timeout lalu berhasil, timeout ganda, stream parsial, pembatalan, dan wiring journal.
4. Bangun ulang image `meeting-worker`; jalankan suite penuh beserta `mypy --strict`, `ruff check`, `ruff format --check`.
5. Verifikasi live ronde 2 pada link uji: admit → sapaan terdengar → dua pertanyaan berkaitan → over-talk yang disengaja dengan `--converse --barge-in` → run wake-phrase → leave. Kumpulkan transkrip, event speak, transisi, journal, checkpoint.
6. Aktivitas capture: ukur RMS pada frame audio input, lacak startup, heartbeat frame, energi, dan kehadiran remote terbaru. RMS nol saat ada participant menghasilkan observasi journal `capture_silent` yang tidak fatal; bisa jadi semua orang sedang di-mute. Ini tidak membuktikan capture mati dan tidak men-degrade STT. Frame yang hilang tetap `capture_fault`; ucapan tanpa final dapat menghasilkan `stt_fault`. Pertahankan kehadiran positif terbaru melewati update unknown sampai TTL-nya berakhir.
7. Tambahkan temuan voice-loop ke `docs/reference-lessons.md` sebagai bagian bertanggal.

## Rencana validasi

- Unit: `uv run pytest -q -p no:cacheprovider` (diharapkan hijau semua), `uv run mypy --strict src`, `uv run ruff check src tests`, `uv run ruff format --check src tests`.
- Offline: probe container yang sudah ada tetap lulus (RMS render browser tinggi, `agent_mic` unmuted setelah siklus muter); jalur streaming baru dicakup tes unit dengan paplay palsu di `PATH` (pola yang sudah mapan di `tests/test_playback.py`).
- Live ronde 2: skrip pada langkah 5. Catat pendengaran remote secara terpisah dari `speak_started`, yang merupakan dispatch/unmute lokal sebelum PCM pertama. Latency first-chunk di journal hanya mengukur jalur provider. Over-talk seharusnya menghasilkan stop + `speak_dropped` + jawaban baru; verifikasi penghentian yang terdengar secara terpisah. Periksa bukti UI/log untuk `Microphone muted by system` dan lifecycle yang sudah ended.
- Jika ronde 2 masih menunjukkan jawaban yang memerlukan beberapa puluh detik, rencana beralih ke spike speech-to-speech dengan batas waktu alih-alih tuning cascade lebih lanjut.

## Risiko / pertanyaan terbuka

- Ukuran chunk TTS streaming OpenAI dan latency first-byte adalah perilaku provider. Sintesis buffered adalah jalur kompatibilitas saat synthesizer tidak mendukung streaming, bukan pemulihan otomatis setelah stream parsial yang macet. Memutar ulang ucapan yang sebagian sudah dikeluarkan akan membuat ucapan terulang.
- Pemotongan dua kalimat dapat memutus jawaban di tengah gagasan; diterima untuk suara (checkpoint menyimpan teks lengkap), dicatat di sini agar reviewer dapat memveto panjang batasnya.
- Langkah barge-in tidak menulis kode sampai celah live teramati; jika tidak ada celah yang muncul, langkah 5 menutupnya hanya dengan bukti.
- Topologi tetap terisolasi per worker. Hanya sink playback yang di-mute di antara ucapan; me-mute sumber mikrofon memicu dialog system-mute Meet. Pertahankan kegagalan utama audio/percakapan jika cleanup leave juga gagal; kegagalan leave pada jalur normal berbasis durasi/stop owner tetap dilaporkan sebagai `browser_failure`.

## Bukti live historis

Run `8581ccc34446` memiliki 13 invocation agent, 3 jawaban yang dibatasi, serta event log lokal berupa 13 speech start, 9 finish, dan 4 drop. Run berakhir dengan `duration_limit`, exit 0. Log yang diperiksa tidak memuat pesan literal `Microphone muted by system`. Ini bukan bukti bahwa dialog yang tidak tercatat di log tidak pernah muncul.

Journal historis tidak memiliki event `synth_*` karena playback produksi tidak menerima dependency journal; refactor memperbaiki wiring tersebut. Durasi di journal itu adalah timing agent/LLM, bukan latency audio remote. Run acceptance baru pada Meet yang di-host sebaiknya mengumpulkan event sintesis yang kini sudah tersambung, ditambah pendengaran remote dan timing interupsi.

## Sumber

- https://github.com/Vexa-ai/vexa
- https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant
