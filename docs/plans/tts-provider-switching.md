# Desain switching provider TTS dan suara twin

**Status:** desain final untuk breaking change. Implementasi dimulai dengan kontrak, registry, conformance suite, lalu migrasi adapter. Dibuat setelah verifikasi langsung ke API Cartesia dan ke kode yang ada. Breaking change diterima asal konfigurasi tetap eksplisit.

## 1. Masalah

- Provider TTS masih satu slot global: `FAKEYOU_TTS__PROVIDER` dan satu `FAKEYOU_TTS__API_KEY`. Ganti provider berarti ubah env dan restart, dan berlaku untuk semua twin. Dua key provider tidak bisa hidup berdampingan di `.env`.
- `.env` operator memilih `minimax`, tetapi image runtime tidak punya `/workspace/.env` dan default settings memilih `openai`. Worker container juga tidak menerima variabel TTS saat dicek. Jadi rapat live `kyf-live-3` kemungkinan besar berjalan dengan OpenAI `gpt-4o-mini-tts` dan suara `alloy`, bukan MiniMax. Angka latensi dan kualitas dari rapat itu harus dibaca dengan catatan ini.
- Kontrak TTS (`SpeechSynthesizer.stream(text, sentence_index, transcript_id)`) tidak membawa bahasa, suara, atau gaya. Provider baru akan memaksa perubahan di banyak tempat.
- Suara tidak terikat ke twin. Suara adalah konfigurasi global.
- Latensi jawaban lebih banyak ditentukan oleh LLM (4,7 sampai 13,5 detik dari transkrip final ke playback) daripada TTS (median sekitar 1 detik sampai PCM pertama). Desain switching TTS tidak menyelesaikan itu, tetapi desain harus menyediakan budget latensi untuk seluruh pipeline.

## 2. Fakta yang sudah diverifikasi

- **Cartesia:** key valid (HTTP 200 pada `/voices`). `sonic-3` menerima `language: "id"`, dan `/tts/bytes` mengembalikan PCM s16le 24 kHz mono. Ada sembilan voice dengan `language=id`, termasuk Intan, Ratna, Safira, Galang, dan Anisa. Voice tanpa filter bahasa semuanya Inggris, jadi filter perlu dipakai. Latensi full response untuk kalimat pendek 0,57 sampai 0,87 detik (tiga kali ukur). Kualitas bicara bahasa Indonesia belum dinilai.
- **Kode:**
  - `SpeechSynthesizer` di `src/fakeyou/audio/playback.py:31` adalah seam yang sudah ada. Playback hanya memanggil `stream` dan `aclose`.
  - `build_speech_synthesizer` di `src/fakeyou/speech/registry.py` memilih adapter dengan `isinstance`.
  - `TTSSettings` di `src/fakeyou/config/settings.py:214` adalah union dari OpenAI dan MiniMax.
  - `Settings` memakai `extra="forbid"`, sehingga variabel `FAKEYOU_*` yang tidak dikenal menggagalkan startup.
- **Pipecat 1.12** punya TTS service untuk Cartesia, ElevenLabs, MiniMax, OpenAI, Azure, Google, dan Deepgram. Service itu berbasis frame (`run_tts` menghasilkan frame), bukan stream bytes.

## 3. Prinsip

1. Provider adalah detail di tepi sistem. Otak twin (LLM, persona, memori, turn-taking) tidak tahu provider mana yang dipakai.
2. Suara milik twin. Provider hanya menyediakan mesin suara. Klon tidak bisa dipindah antar provider.
3. Ganti provider atau suara berlaku antar-rapat, tidak di tengah rapat.
4. Setiap provider lolos gate yang sama (conformance dan benchmark) sebelum boleh dipakai twin.
5. Breaking change boleh, tetapi konfigurasi harus eksplisit dan terbaca di `.env`.

## 4. Desain

### 4.1 Kontrak

```
SpeechRequest:  text, language ("id" | "en"), voice (VoiceRef),
                request_id, sentence_index, transcript_id

SpeechSynthesizer (Protocol):
    stream(request) -> AsyncIterator[bytes]   # PCM s16le, mono, 24 kHz
    aclose()

ProviderCapabilities:  name, languages, streaming, cloning, max_chars, api_version
```

- `stream` harus bisa dibatalkan. Menutup iterator harus menutup koneksi HTTP atau WebSocket, karena barge-in memanggil itu di tengah kalimat.
- Kesalahan provider dipetakan ke `ProviderFault` dengan `kind`: `auth`, `rate_limit`, `timeout`, `invalid_voice`, atau `unknown`.

### 4.2 Registry dan konfigurasi

- Satu registry memetakan nama provider ke model settings, factory adapter, dan capabilities. `TTSSettings` tetap union yang didiskriminasi oleh `provider`, dan itu sudah berjalan sekarang. Menambah provider berarti menambah satu anggota union dan satu entri registry.
- **Tahap 1, dinamis, tanpa breaking change:** `FAKEYOU_TTS__PROVIDER` memilih provider aktif, dan `FAKEYOU_TTS__API_KEY` berisi key untuk provider itu. Field `api_key` ada di `TTSBase`, jadi semua provider memakai slot yang sama. Ganti provider berarti mengganti dua variabel itu. Cocok untuk satu operator dengan satu worker.
- **Tahap 2, ketika provider dipilih per twin atau per user:** key dipindah ke penyimpanan rahasia per profil suara dan dirujuk lewat id, tidak lagi di `.env`. Satu slot global tidak bisa menampung key banyak provider untuk banyak twin, jadi tahap ini dibangun saat fitur per-user benar-benar dibutuhkan.
- Tahap 1 tidak memerlukan perubahan nama variabel, jadi `.env` operator tidak perlu dimigrasi.

### 4.3 Profil suara per twin

- Tabel `twin_voice_profile(id, twin_id, provider, voice_ref, language, status, version, created_at, gate_report_id)`.
- Twin tanpa profil memakai default operator.
- Ganti suara berarti membuat profil baru dengan status `pending`. Setelah lolos gate, profil menjadi `active`. Profil lama disimpan untuk rollback.
- Worker mem-pin id profil saat start. Perubahan profil tidak berlaku untuk rapat yang sedang berjalan.

### 4.4 Gate sebelum aktif (`fakeyou voice test`)

- Korpus tetap berisi sepuluh kalimat Indonesia. Isinya angka, tanggal, kata Inggris, pertanyaan, dan kalimat panjang.
- Yang diukur: time to first audio (TTFA), format PCM, kesalahan, dan durasi audio.
- Syarat lolos: tidak ada kesalahan pada korpus, dan p50 TTFA per kalimat di bawah 1,5 detik. Sisa budget dipakai untuk LLM dan STT.
- Gate ini tidak mengukur kualitas suara. Kualitas perlu penilaian manusia (skor sederhana) dan disimpan sebagai data.

### 4.5 Runtime

- `ConversationLoop` dan `PlaybackEngine` hanya berubah untuk menerima `SpeechRequest`.
- Journal mencatat per kalimat: provider, `voice_ref`, TTFA, jumlah karakter, dan jenis kesalahan.
- Retry sekali pada provider yang sama untuk kesalahan sementara (`timeout`, `rate_limit`).
- Fallback ke `fallback_provider` hanya jika profil punya fallback yang sudah lolos gate. Fallback mengganti suara di tengah jawaban, jadi defaultnya nonaktif sampai keputusan 6.2.

### 4.6 Klon

- Kemampuan `cloning` ada di capabilities. Alur consent dan liveness dari desain enrollment dipakai apa adanya.
- Klon menghasilkan `voice_ref` untuk provider tertentu, dan profil baru dibuat untuk provider itu.

## 5. Perilaku yang diharapkan di berbagai kondisi

| Kondisi | Perilaku twin |
|---|---|
| Provider lambat, melewati budget | Tetap menunggu sampai batas timeout. Journal mencatat keterlambatan. Filler belum ada, lihat bagian 8. |
| Error sementara | Retry sekali. Jika masih gagal dan ada fallback yang lolos gate, pakai fallback. Jika tidak, turn-recovery yang sudah ada berjalan dan twin mengucapkan kalimat `turn_failed`. |
| Key salah atau kuota habis | Gagal cepat dengan `auth` atau `rate_limit`. Tidak retry berulang. |
| Barge-in di tengah kalimat | Stream ditutup, koneksi dibatalkan, dan tidak ada sisa audio. |
| Campuran Indonesia dan Inggris | Bahasa dipilih per kalimat. Diuji di korpus gate. |
| Angka, tanggal, singkatan | Normalisasi dilakukan provider. Diuji di korpus gate. |
| Tidak ada jawaban yang perlu diucapkan | Tidak ada TTS. |
| Jaringan putus | Pause, seperti yang sudah ada. |

## 6. Keputusan terbuka

1. **Adapter.** Pakai HTTP tipis sendiri, seperti pola yang sudah ada, atau pakai Pipecat service? Pipecat sesuai kebijakan library, tetapi berbasis frame dan membutuhkan lifecycle start dan stop. Rekomendasi: adapter tipis untuk tahap awal, dengan conformance test yang sama untuk semua provider. Pipecat dievaluasi lagi jika kita butuh WebSocket.
2. **Fallback default.** Nonaktif atau aktif? Rekomendasi: nonaktif sampai ada suara pengganti yang lolos gate.
3. **Key.** Tahap 1: `FAKEYOU_TTS__PROVIDER` dan `FAKEYOU_TTS__API_KEY` dinamis di `.env`. Tahap 2: penyimpanan rahasia per profil saat fitur per-user dibutuhkan. Rekomendasi: tahap 1 sekarang.
4. **Budget latensi.** TTS 1,5 detik, LLM 2 detik, sisanya untuk STT dan endpointing. Disetujui atau tidak?
5. **Provider peluncuran.** OpenAI dan MiniMax sudah ada. Cartesia sudah diverifikasi key dan latensinya, tetapi kualitas suara belum dinilai. ElevenLabs belum diuji.

## 7. Urutan implementasi, test-first

1. Kontrak `SpeechRequest` dan `ProviderCapabilities`, dengan conformance suite untuk adapter OpenAI dan MiniMax yang sudah ada.
2. Registry dinamis di atas union yang ada, tanpa breaking change.
3. Adapter Cartesia memakai `/tts/bytes`. Streaming lewat SSE dipelajari lagi setelah itu.
4. Tabel profil suara per twin, pin saat worker start, dan journal per kalimat.
5. Perintah `fakeyou voice test` dan laporan gate.
6. Retry dan fallback sesuai keputusan 6.2.
7. Klon sebagai capability, mengikuti desain enrollment.

## 8. Di luar TTS

Agar twin terasa seperti manusia, TTS hanya sebagian. Latensi LLM adalah penyebab terbesar: 4,7 sampai 13,5 detik dibanding TTS sekitar 1 detik. Model reply yang lebih cepat, reasoning yang lebih rendah, pembatasan tool call, dan filler untuk giliran yang lambat perlu dokumen dan keputusan sendiri.

## 9. Keputusan final dan breaking change

Breaking change diterima. Tidak ada shim kompatibilitas, dan tidak ada jalur lama yang dipertahankan.

### 9.1 Prinsip pondasi

- Satu kontrak di `fakeyou/speech/contract.py`. Adapter membungkus provider. Playback, percakapan, dan twin tidak mengandung logika provider.
- Setiap provider mendeklarasikan `ProviderCapabilities`: bahasa yang didukung, kemampuan klon, dan jenis voice ref. Konfigurasi divalidasi terhadap capabilities saat startup, sehingga kesalahan konfigurasi gagal sebelum rapat dimulai.
- Setiap provider lolos conformance suite yang sama.
- Format keluaran dikunci ke PCM s16le mono 24 kHz. Adapter menormalkan format; playback tidak tahu format provider.
- Open-source TTS (F5-TTS, Fish Speech/OpenAudio, CosyVoice, Chatterbox, XTTS-v2) tidak masuk image worker. Setiap model berjalan sebagai sidecar dengan protokol HTTP yang sama, dan adapternya hanya klien HTTP. Dengan begitu dependensi torch dan bobot model tidak membengkakkan worker. Lisensi tiap model perlu dicek sebelum dipakai untuk komersial, karena beberapa bobot dirilis dengan lisensi non-komersial.

### 9.2 Daftar breaking change

| # | Sebelum | Sesudah | Dampak |
|---|---|---|---|
| 1 | `audio/voice.py` (sebelum iterasi STT) memuat `OpenAISpeechSynthesizer`, `MiniMaxSpeechSynthesizer`, dan `create_speech_synthesizer` | TTS pindah ke `speech/`. `audio/voice.py` hanya memuat STT | Import internal berubah. Tidak ada pemakai di luar repo. |
| 2 | `SpeechSynthesizer` didefinisikan di `audio/playback.py` | Didefinisikan di `speech/contract.py` | Import berubah. |
| 3 | Factory memakai rantai `isinstance` | `speech.registry.build_speech_synthesizer` memakai tabel `ADAPTERS` | Menambah provider cukup satu entri tabel. |
| 4 | Journal `synthesis_*` tidak memuat provider | Ditambah field `provider` | Event bersifat aditif. Laporan bisa dipecah per provider. |
| 5 | `OpenAITTS.voice` divalidasi terhadap `VALID_VOICES` saat konstruksi | Validasi voice pindah ke adapter. Setiap provider memvalidasi voice ref-nya sendiri | Kesalahan konfigurasi muncul per provider. |
| 6 | `speech.language` diterima apa pun provider-nya | `speech.language` harus ada di `capabilities.languages` provider aktif. Jika tidak, startup gagal | Konfigurasi tidak valid gagal lebih awal. |
| 7 | Error HTTP provider dipetakan secara ad hoc | Dipetakan ke taksonomi `ProviderFault.kind` yang sudah ada (`unauthorized`, `payment_required`, `rate_limited`, `unavailable`, `bad_request`, `unknown`) lewat `classify_http`. Tidak ada taksonomi baru. | Laporan fault konsisten antar provider. |
| 8 | Settings TTS terdiri dari dua class | Empat class: OpenAI, MiniMax, Cartesia, ElevenLabs. Nama variabel env tidak berubah | Tidak perlu migrasi `.env` operator. |
| 9 | Tes TTS tersebar per file | `tests/test_speech_conformance.py` diparametrisasi dari `ADAPTERS` | Tes lebih sedikit dan lebih tepat sasaran. |

### 9.3 Yang tidak berubah

- Otak twin: LLM, persona, memori, dan turn-taking.
- `PlaybackEngine` hanya mengganti tipe kontrak yang dipakai.
- Nama variabel env, termasuk `FAKEYOU_TTS__PROVIDER`, `FAKEYOU_TTS__API_KEY`, `FAKEYOU_TTS__MODEL`, `FAKEYOU_TTS__VOICE`, dan `FAKEYOU_TTS__BASE_URL`.

### 9.4 Urutan implementasi

1. Kontrak, registry, dan conformance suite. Tes ditulis dulu dan gagal.
2. Pindahkan OpenAI dan MiniMax ke `speech/` tanpa mengubah perilaku. Tes lama harus tetap lulus setelah import diperbarui.
3. Adapter Cartesia memakai `/tts/bytes`, dengan header `X-API-Key` dan `Cartesia-Version`. Sudah terverifikasi langsung dengan key.
4. Adapter ElevenLabs memakai `POST /v1/text-to-speech/{voice_id}/stream?output_format=pcm_24000`, dengan header `xi-api-key`. Endpoint dan `pcm_24000` terkonfirmasi dari dokumentasi. Header belum terverifikasi live, jadi tesnya memakai fake transport sampai key tersedia.
5. Validasi capabilities saat startup.
6. Regenerasi `docs/reference/environment.md` dari catalog.
7. Open-source sidecar: protokolnya didokumentasikan sekarang, implementasinya nanti.

## 10. Status implementasi

- **Selesai:** paket `fakeyou/speech` (kontrak, streaming bersama, registry dengan `match` dan `assert_never`), adapter OpenAI, MiniMax, Cartesia, dan ElevenLabs. Pemilihan provider dinamis lewat `FAKEYOU_TTS__PROVIDER` dan `FAKEYOU_TTS__API_KEY`. Validasi bahasa saat load settings. Tes per adapter dan tes registry. Generator catalog env menggabungkan baris discriminator menjadi satu baris.
- **Terverifikasi langsung:** Cartesia. Key valid, `sonic-3` menerima `id`, PCM 24 kHz diterima, dan jalur penuh (env, settings, registry, API) berjalan. Time to first chunk 0,24 sampai 0,68 detik untuk kalimat pendek.
- **Belum terverifikasi live:** ElevenLabs. Endpoint dan `pcm_24000` terkonfirmasi dari dokumentasi. Header `xi-api-key` dan parameter `language_code` belum diuji dengan key nyata.
- **Belum dikerjakan:** open-source sidecar (F5-TTS, Fish Speech/OpenAudio, CosyVoice, Chatterbox, XTTS-v2). Protokolnya belum dibuat. Lisensi tiap model perlu dicek, karena beberapa bobot dirilis non-komersial.
- **Belum dikerjakan:** klon suara lintas provider, profil suara per twin, gate `fakeyou voice test`, fallback per kalimat, dan penyimpanan key per profil.
