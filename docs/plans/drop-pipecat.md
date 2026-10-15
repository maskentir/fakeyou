# Lepas Pipecat: graf audio dan klien Muse sendiri

**Status:** S1 sampai S5 selesai di kode dan lulus gate lokal (suite default dan database, mypy seluruh proyek, ruff, lint-imports, deptry, tes container, `fakeyou check`, dan tes stage di image test). Belum diuji: Meet sungguhan, `parec` di container dengan `PULSE_SOURCE` hidup, ElevenLabs dengan key, dan sesi Muse 60 menit. Checklist di bagian 5 dicentang per tahap.

## 1. Kenapa

Pipecat dipakai di tujuh file produksi. Yang dipakai:

- Model frame dan `FrameProcessor`, `Pipeline`, `PipelineWorker`, `WorkerRunner`
- VAD: `SileroVADAnalyzer`, `VADProcessor`, `VADController`
- STT: `MetaSTTService` sebagai basis `ManagedMuseSTT`
- Transport lokal: `LocalAudioTransport` (pyaudio, Linux saja)
- Utilitas kecil: `Language`, `match_endofsentence`, `calculate_audio_volume`, `VALID_VOICES`, `ErrorCategory`

Masalah yang sudah terbukti di kode dan probe live:

1. **Label speaker Muse tidak sampai ke frame.** Pipecat v1.12 hanya mencatatnya ke log. Kita harus membaca ulang protokol untuk diarisasi.
2. **Dua pemilik rekonek.** Base class merekonek sendiri, sementara `MeetingAudioSession` punya kebijakan rekonek sendiri (antrean, backoff, batas percobaan, jeda saat bicara). Keduanya bisa bertabrakan.
3. **Waktu dinding.** Reset model VAD, aktivitas bicara, dan idle timeout memakai `time.time()` dan `time.monotonic()`. Hasilnya tidak deterministik dan tidak bisa diuji dengan replay audio.
4. **Pre-roll menduplikasi audio.** `PreRollBuffer` meneruskan semua audio ke STT lalu memutar ulang ring-nya saat bicara dimulai. Tes `test_preroll_replays_ring_before_speech_start` mengunci duplikasi itu. Muse menerima audio kontinu, jadi pre-roll tidak punya fungsi di jalur ini.
5. **Biaya upgrade.** API worker berubah antar versi (`PipelineWorker`, `WorkerRunner`). Kita hanya memakai sebagian kecil API itu.

## 2. Target

| Modul baru | Isi | Menggantikan |
| --- | --- | --- |
| `audio/vad.py` | `SileroVAD` (onnxruntime, model di-vendor), `VadStateMachine`, `VadController` (aktivitas bicara, idle timeout, waktu dari jumlah sampel) | `SileroVADAnalyzer`, `VADProcessor`, `VADController` |
| `audio/graph.py` | `AudioGraph`: satu jalur per sesi. Urutan per chunk: aktivitas dan probe, turn tap, kirim ke STT, VAD, dispatch event. Tanpa frame dan tanpa direction | `Pipeline`, `PipelineWorker`, `WorkerRunner`, `FrameProcessor` |
| `transcription/muse.py` | `MuseClient` di atas `websockets`: handshake dengan `mode=DIARIZATION`, loop penerimaan, parser pesan, keepalive dari waktu audio, backlog cap, `endStream` saat tutup. Rekonek hanya dimiliki `MeetingAudioSession` | `MetaSTTService`, `ManagedMuseSTT` |
| `audio/capture.py` | Capture native lewat `parec` (monitor Pulse), sejalan dengan `paplay` untuk output | `LocalAudioTransport` (pyaudio) |
| `foundation/transcript.py` | `TranscriptUpdate` tanpa field `speaker`. Ditambah `voice_clusters` (label diarisasi Muse yang muncul dalam rentang giliran) | field `speaker` yang mati |

Dependensi yang berubah:

- Hapus: `pipecat-ai[silero,webrtc]`, extra `audio` (`pipecat-ai[local]`, pyaudio).
- Tambah langsung (sudah ada di venv, tidak ada teknologi baru): `onnxruntime~=1.24.3`, `loudness`, `sentencex==1.0.31`, `websockets` (sudah ada).
- Model `silero_vad.onnx` (2,3 MB, lisensi MIT dari Silero) di-vendor di `src/fakeyou/audio/models/`, dengan README yang mencatat sumber, versi, dan sha256.

## 3. Keputusan

1. **Mode Muse default `DIARIZATION`.** `ENDPOINTING` tidak dipakai lagi.

   Probe mentah (`muse_raw_diarization.jsonl`, klip 22,5 detik, 4 giliran, dua suara, ground truth di `diar_truth.json`) menunjukkan:
   - Label `A, B, A, B` sesuai urutan giliran.
   - Urutan per giliran: `speechStart(turnId, audioProcessedMs)`, transkrip parsial kumulatif, `speaker(label)`, `speechEnd(turnId)`, `speechComplete(turnId, transcript)`. `speaker`, `speechEnd`, dan `speechComplete` datang dalam satu burst, jadi label sudah tersedia saat final. Tidak perlu menahan final.
   - Teks final ada di `speechComplete.transcript` dengan spasi di awal dan akhir. Satu-satunya `transcript` dengan `final: true` adalah flush kosong di akhir sesi.
   - Koreksi: catatan sebelumnya bilang label datang 0,5 detik setelah `speechComplete`. Itu tidak terjadi di run ini. Pesan `speaker` tetap bisa datang terlambat, jadi label yang tiba setelah final dicatat ke journal, tidak diteruskan.
   - Muse salah dengar nama "Harun" jadi "Arun" di satu giliran. Kata kunci (`keywords`) menutup itu, dan itu jalur yang sudah ada.
2. **`TranscriptUpdate.speaker` dihapus.** Field itu tidak dibaca kode produksi. Breaking change diterima sesuai standar refactor.
3. **Pre-roll dihapus,** termasuk setting `STTSettings.preroll_ms` dan tesnya.
4. **Satu pemilik rekonek.** `MuseClient` tidak merekonek sendiri. Ia melaporkan kegagalan, dan `MeetingAudioSession` yang memutuskan.
5. **VAD diuji ekuivalen dengan Pipecat 1.12 sebelum Pipecat dihapus.** Golden dibuat dari rekaman standup 16 kHz: urutan event `started`, `stopped`, dan `activity` harus identik. Setelah Pipecat dihapus, golden itu tetap jadi tes.
6. **Reset model VAD memakai waktu audio** (jumlah sampel), bukan jam dinding. Perilaku ini sengaja berbeda dari Pipecat dan dicatat di tes.
7. **Keepalive Muse** mengirim 100 ms silence setiap 100 ms saat tidak ada audio selama 0,5 detik. Angka ini sama dengan implementasi sekarang, karena sudah terbukti live.

## 4. Urutan kerja

Setiap tahap: tes merah dulu, lalu implementasi, lalu gate yang relevan. Tidak ada commit kecuali diminta.

- **S1. Klien Muse dan DIARIZATION.** Tes merah untuk handshake, parser pesan (`transcript`, `speechStart`, `speechEnd`, `speechComplete`, `speaker`, `audioProgress`, `error`), korelasi label ke giliran, keepalive, backlog cap. Hapus `TranscriptUpdate.speaker`.
- **S2. VAD.** Tes ekuivalensi terhadap golden dari Pipecat (dibuat sebelum Pipecat disentuh). Lalu tes idle timeout dan reset model berbasis audio.
- **S3. Graf audio.** Ganti `Pipeline`, `PipelineWorker`, `WorkerRunner`, dan semua `FrameProcessor` di `audio.py`. Hapus pre-roll. `MeetingAudioSession` tetap, tetapi menerima graf sendiri. `StreamAudioInput` berhenti mewarisi `BaseInputTransport`. Capture native pindah ke `parec`.
- **S4. Utilitas.** `SPEECH_LANGUAGES` berbasis string. `match_endofsentence` pindah ke `sentencex` langsung. Volume memakai `loudness` langsung. `VALID_VOICES` jadi konstanta. `ErrorCategory` diganti klasifikasi `ProviderFault` yang sudah ada.
- **S5. Buang Pipecat.** Hapus dependensi, konfigurasi deptry, kontrak importlinter, Dockerfile, dan dokumen yang menyebut Pipecat. Lalu gate penuh dan tes live.

## 5. Checklist

- [x] S1 klien Muse, DIARIZATION, hapus `speaker`. Klien baru (kini di `transcription/muse.py`), 21 tes mock, 1 tes live (`MUSE_DIARIZATION_AUDIO`) lulus: empat final dengan label bergantian. Adapter lama (Pipecat) masih dipakai pipeline sampai S3.
- [x] S2 VAD dan golden ekuivalensi. `audio/vad.py` dengan 12 tes. Cocok per jendela dengan golden Pipecat (confidence dan volume dalam 1e-5, state dan event identik). Golden: `tests/fixtures/vad-golden-standup-16k.json`, dibuat dengan reset model dimatikan.
- [x] S3 graf audio, capture `parec`, hapus pre-roll. Ada di `src/`: `audio/graph.py`, `audio/capture.py`, dan `audio/stream_input.py`. Tidak ada impor `pipecat` dan tidak ada `PreRollBuffer` atau `preroll_ms` di `src/`. Tes lama yang masih mengimpor Pipecat tercatat di sisa bawah.
- [x] S4 utilitas dan dependensi langsung. `SPEECH_LANGUAGES` berbasis string (`speech/languages.py`), `match_endofsentence` diganti `sentencex` (`foundation/sentences.py`), volume memakai `loudness` (`audio/vad.py`), dan `Language` serta `ErrorCategory` sudah tidak ada di `src/`. Dependensi langsung ada di `pyproject.toml`.
- [x] S5 dependensi dan konfigurasi: `pipecat` tidak ada di `pyproject.toml` dan `uv.lock`; extra `audio` dan pyaudio dihapus; deptry dan importlinter tidak menyebut Pipecat; `docker/` tidak menyebut Pipecat, pyaudio, atau portaudio.
- [x] S5 gate penuh lulus dan tes live Muse lulus. Tes live memakai klip dua pembicara (`MUSE_DIARIZATION_AUDIO`), bukan rekaman standup. Tes end-to-end `tests/test_audio_graph_live.py` juga lulus. Angka ada di gate, bukan di dokumen ini.
- [x] Dokumen diperbarui: `library-sources.md`, `stt-provider-contract.md`, `project-status.md`, laporan breaking change, serta `architecture.md`, `capabilities.md`, dan `reference-lessons.md`.
- [x] Sisa pembersihan selesai: tidak ada impor `pipecat` di tes, `join_preflight` memeriksa `parec` (bukan `pyaudio`), `verification.md` dan `code-standard.md` diperbarui.

## 6. Belum diuji di sini

- Capture `parec` di container dengan `PULSE_SOURCE` yang hidup. Butuh rebuild image dan kerja di worker live.
- Rapat Meet sungguhan. Menunggu link dari pengguna.
- ElevenLabs dengan key sungguhan. Saat ini hanya mock.
- Sesi Muse 60 menit. Meta menyebut close code 1011; belum diuji live.

## 7. Hasil

Jumlah tes dan hasil gate tidak dicantumkan di sini. Lihat gate.

- VAD (`audio/vad.py`) cocok per jendela dengan golden Pipecat 1.12. Selisih confidence dan volume paling besar 1e-5, dan state serta urutan event identik. Dua perbedaan disengaja: reset model memakai waktu audio, dan idle timeout mengembalikan analyzer ke quiet.
- Muse DIARIZATION: probe pada klip 22,5 detik menghasilkan label `A, B, A, B` sesuai urutan giliran. Label datang sebelum final dalam burst yang sama. Teks final cocok dengan ground truth kecuali satu nama ("Harun" ditulis "Arun"). Detail ada di [kontrak STT](stt-provider-contract.md#2-bukti-live-diarization).
- Tes live `MUSE_DIARIZATION_AUDIO` tercatat lulus pada S1.
- Dependensi: `pipecat` tidak ada di `pyproject.toml` dan `uv.lock`. Versi terkunci: `loudness` 0.2.0, `sentencex` 1.0.31, `onnxruntime` 1.24.4, dan `websockets` 16.1.1.
- Pemeriksaan statis 15 Oktober 2026: tidak ada `PreRollBuffer`, `preroll_ms`, `ErrorCategory`, `ManagedMuseSTT`, `create_muse_stt`, `MetaSTTService`, atau `LocalAudioTransport` di `src/`. Satu-satunya rujukan `pyaudio` di `src/` ada di `orchestration/join_preflight.py` (lihat sisa di bagian 5).
- Gate penuh dan tes live S5: lihat gate.
