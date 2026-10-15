# Uji diarisasi Muse

Tes `tests/test_muse_client.py::test_muse_diarization_splits_turns_and_labels_voices_live` memakai `MuseClient` asli (tanpa Pipecat) dan socket Muse sungguhan. Inputnya file WAV mono 16 kHz, PCM signed 16-bit, berisi empat giliran bicara dari dua pembicara yang bergantian. Tidak ada audio mikrofon atau meeting yang direkam.

## Kredensial

Isi `FAKEYOU_STT__API_KEY` di `.env` dengan key dari [dashboard Meta Model API](https://dev.meta.ai/help/api-keys/create-api-key). Jangan menaruh key dalam argumen command atau source.

Login OAuth yang tersimpan di Muse Code bukan pengganti key ini. [Dokumentasi Meta](https://dev.meta.ai/help/subscriptions/subscribe-to-muse-code) membatasi kredensial onboarding tersebut untuk Muse Code; key Model API terpisah.

## Audio uji

Klip harus memuat empat giliran, bergantian antara dua pembicara, dengan jeda singkat di antaranya. Tes memeriksa:

1. Tepat empat transkrip final.
2. Setiap final membawa tepat satu label pembicara.
3. Label giliran 1 dan 3 sama, label giliran 2 dan 4 sama, dan kedua label berbeda.
4. Final pertama memuat kata `migration`, final ketiga memuat kata `blockers`.

Naskah yang dipakai di pengembangan:

1. Pembicara A: "Good morning everyone. Let us start with the migration status."
2. Pembicara B: "Thanks Harun. The migration finished last night, and the billing tests are green."
3. Pembicara A: "Great. Any blockers on your side today?"
4. Pembicara B: "Only the flaky test in the billing module. I am fixing it this morning."

Klip dapat direkam dari dua orang atau disintesis dengan TTS yang memiliki dua voice. Pastikan jeda sekitar satu detik di antara giliran. Untuk mengonversi ke format tes dengan `ffmpeg`:

```sh
mkdir -p .runtime/live-tests
ffmpeg -hide_banner -loglevel error -nostdin -y \
  -i .runtime/live-tests/diarization-source.wav \
  -ac 1 -ar 16000 -c:a pcm_s16le .runtime/live-tests/diarization-16k.wav
```

File audio berada di `.runtime`, yang diabaikan Git.

## Menjalankan tes

```sh
MUSE_DIARIZATION_AUDIO=.runtime/live-tests/diarization-16k.wav \
uv run pytest tests/test_muse_client.py -m live -v
```

Tanpa key atau input audio, tes dilewati. Status `skipped` tidak membuktikan koneksi ke provider berhasil. Tes default dan CI tidak menjalankan tes berbayar ini.

## Batas verifikasi

Kelulusan menguji file audio → `MuseClient` → Muse → transkrip final dengan label diarisasi. Ini belum memverifikasi capture Google Meet, pemetaan label ke identitas participant, TTS, pergiliran bicara, atau kemampuan menyela.

Tes memutar audio dengan pacing real-time (80 ms per chunk), lalu menunggu 8 detik agar Muse menyelesaikan giliran terakhir. Mengirim seluruh file sekaligus dapat melanggar batas backlog layanan.
