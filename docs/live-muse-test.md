# Uji transkripsi Muse

Tes ini memakai `MetaSTTService`, `PipelineWorker`, dan `WorkerRunner` Pipecat asli. Inputnya file WAV berisi kalimat yang diketahui. Tidak ada audio mikrofon atau meeting yang direkam.

## Kredensial

Isi `FAKEYOU_MODEL_API_KEY` di `.env` dengan key dari [dashboard Meta Model API](https://dev.meta.ai/help/api-keys/create-api-key). Jangan menaruh key dalam argumen command atau source.

Login OAuth yang tersimpan di Muse Code bukan pengganti key ini. [Dokumentasi Meta](https://dev.meta.ai/help/subscriptions/subscribe-to-muse-code) membatasi kredensial onboarding tersebut untuk Muse Code; key Model API terpisah menggunakan penagihan berdasarkan pemakaian.

## Audio uji

Di macOS, periksa voice yang tersedia dengan `say -v '?'`. Pada mesin pengembangan ini tersedia Damayanti. Command berikut menghasilkan audio Bahasa Indonesia dan menambahkan dua detik hening agar endpointing bisa menyelesaikan ujaran.

```sh
mkdir -p .runtime/live-tests
say -v 'Damayanti (Indonesian (Indonesia))' -r 140 \
  -o .runtime/live-tests/muse-id.aiff \
  'Halo, ini pengujian suara untuk rapat digital. Besok kita membahas jadwal proyek.'
ffmpeg -hide_banner -loglevel error -nostdin -y \
  -i .runtime/live-tests/muse-id.aiff -af 'apad=pad_dur=2' \
  -ac 1 -ar 24000 -c:a pcm_s16le .runtime/live-tests/muse-id.wav
```

Untuk sistem lain, sediakan file WAV mono, PCM signed 16-bit, 24 kHz dengan kalimat yang diketahui dan hening di akhir. Audio sintetis yang dibuat pada 2026-09-27 berdurasi 9,076 detik. File audio berada di `.runtime`, yang diabaikan Git.

## Menjalankan tes

```sh
FAKEYOU_MUSE_TEST_AUDIO=.runtime/live-tests/muse-id.wav \
FAKEYOU_MUSE_EXPECTED_TEXT='Halo, ini pengujian suara untuk rapat digital. Besok kita membahas jadwal proyek.' \
uv run pytest tests/test_muse_live.py -m live -v
```

Tanpa key atau input audio, tes dilewati. Status `skipped` tidak membuktikan koneksi ke provider berhasil. Tes default dan CI tidak menjalankan tes berbayar ini.

## Batas verifikasi

Kelulusan menguji audio file → Pipecat → Muse → transkrip final. Ini belum memverifikasi capture Google Meet, atribusi peserta, TTS, pergiliran bicara, atau kemampuan menyela.

Gabungan transkrip final harus cocok dengan kalimat acuan setelah normalisasi huruf, tanda baca, dan spasi. Error upstream tetap menggagalkan tes walaupun transkrip cocok. Tes ini memeriksa satu fixture, bukan mengukur akurasi STT pada beragam suara.

Tes memakai `ENDPOINTING`, mengirim PCM mengikuti durasi audio, lalu menunggu transkrip final sebelum menutup pipeline. Mengirim seluruh file sekaligus dapat melanggar batas backlog layanan. Pada Pipecat 1.12.0, penghentian koneksi membatalkan receive task setelah `endStream`; karena itu tes tidak memakai shutdown sebagai pemicu finalisasi `PUSH_TO_TALK`.

Sumber: [Muse speech-to-text](https://dev.meta.ai/docs/speech-to-text), [realtime API](https://dev.meta.ai/docs/api-reference/voice/realtime), dan [adapter Pipecat 1.12.0](https://github.com/pipecat-ai/pipecat/blob/v1.12.0/src/pipecat/services/meta/stt.py).
