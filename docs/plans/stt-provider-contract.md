# Kontrak provider STT dan keputusan Muse

**Status:** Muse memakai klien sendiri di `transcription/muse.py` dengan mode `DIARIZATION` sebagai default. Probe live pada klip 22,5 detik menunjukkan label speaker per giliran. Provider STT lain mengikuti kontrak ini, dan belum ada. Pelepasan Pipecat dicatat di [rencana lepas Pipecat](drop-pipecat.md).

## 1. Bentuk kontrak

Domain tidak bergantung pada provider. Dua tipe berada di lapisan bawah:

- `TranscriptUpdate` (`foundation/transcript.py`): `text`, `final`, `timestamp`, `language`, `voice_clusters`, `stream_id`, `capture_seq`, `capture_ms`. Immutable. Field `speaker` sudah dihapus.
- `TranscriptSink`, `TranscriptionError`, dan `SpeechSinks` (`transcription/contract.py`): callback konsumen, error STT, dan `on_lost` yang menerima error saat socket putus.

Setiap provider STT wajib memenuhi lima hal berikut.

1. **Konversi di tepi.** Pesan provider diubah menjadi `TranscriptUpdate` di satu titik. Untuk Muse titik itu adalah `MuseClient` di `transcription/muse.py`. Konsumen di luar `transcription/` tidak melihat pesan provider.
2. **Pacing ke provider.** Backlog audio yang belum terkirim dibatasi. Untuk Muse, batas provider 5 detik dan buffer klien `MUSE_MAX_BACKLOG_SECONDS = 4`, sehingga burst setelah rekonek tetap di bawah batas. Audio yang melewati buffer dibuang dari depan dan dicatat sebagai `stt_audio_dropped` (`dropped_ms`, `reason`).
3. **Keheningan saat idle (keepalive).** Audio harus terus mengalir pada laju real-time, termasuk saat tidak ada suara. Klien mengirim silence 100 ms setiap 100 ms ketika tidak ada audio masuk selama 0,5 detik. Hitungan idle memakai waktu audio, bukan jam dinding. Event `stt_keepalive_started` (`idle_ms`) dicatat sekali per episode. Tanpa keepalive, Muse menolak sesi dengan `Ingress audio slower than real-time` sekitar 15 detik setelah audio habis, dan turn tidak pernah final.
4. **Rekonek dimiliki `MeetingAudioSession`.** `MuseClient` tidak merekonek sendiri. Saat socket putus, ia mencatat `stt_socket_failed` lalu memanggil `on_lost`. `MeetingAudioSession` (`audio/audio.py`) memutuskan: antrean audio selama socket belum siap, backoff 5 detik yang dikali dua dengan jitter, maksimal tiga percobaan, dan batas 120 detik.
5. **Laju sampel.** Handshake memakai `audioEncoding` `PCM_16KHZ` dari `sample_rate` klien. Silence keepalive dihitung dari laju yang dikirim, bukan laju masuk.

## 2. Bukti live: DIARIZATION

Probe memakai klip 22,5 detik dengan empat giliran bergantian, dua suara sintetis, dan tanpa overlap. Ground truth ada di `diar_truth.json`. Probe mentah mencatat setiap pesan server secara berurutan di `muse_raw_diarization.jsonl`. Kedua berkas adalah berkas kerja di luar repository.

- **Label.** `A, B, A, B` sesuai urutan giliran dan sesuai suara ground truth. A selalu suara pertama, B suara kedua.
- **Urutan per giliran.** `speechStart` (dengan `turnId`), transkrip parsial kumulatif (`final: false`), `speaker` (label), `speechEnd`, lalu `speechComplete` yang membawa teks final. `speaker`, `speechEnd`, dan `speechComplete` datang dalam satu burst dengan timestamp yang sama, sehingga label sudah tersedia saat final.
- **Pesan `speaker` tidak membawa `turnId`.** Label karena itu dikaitkan ke giliran yang sedang terbuka.
- **Teks final.** `speechComplete.transcript` memakai spasi di awal dan akhir, dan klien men-strip-nya. Teks keempat giliran sama dengan ground truth kecuali tanda baca, kontraksi ("I'm" untuk "I am"), dan satu nama: pada giliran kedua Muse menulis "Arun" untuk "Harun". Handshake membawa `keywords` dari `speech.stt_keywords` untuk kasus seperti ini. Efeknya belum diukur ulang.
- **Flush akhir.** Satu-satunya transkrip dengan `final: true` adalah flush kosong di akhir sesi, dan diabaikan.
- **Koreksi.** Catatan probe sebelumnya menyebut label datang sekitar 0,5 detik setelah giliran selesai. Di run ini itu tidak terjadi. Pesan `speaker` tetap bisa datang terlambat, dan label seperti itu dicatat sebagai `stt_speaker_dropped`, tidak diteruskan.

Tes live `MUSE_DIARIZATION_AUDIO` (`tests/test_muse_client.py`) mengulang pengecekan ini pada socket Muse sungguhan. Runbook ada di [live-muse-test](../runbooks/live-muse-test.md).

## 3. Keputusan

- **Mode default `DIARIZATION`.** Default `FAKEYOU_STT__MODE` berubah dari `ENDPOINTING` ke `DIARIZATION`. `ENDPOINTING` dan `PUSH_TO_TALK` tetap nilai yang valid.
- **Label dilampirkan ke giliran terbuka.** Label `speaker` masuk `TranscriptUpdate.voice_clusters` pada final giliran itu. Label ini cluster per sesi (`A`, `B`), bukan identitas participant. Final tidak ditahan, karena label sudah datang sebelum final dalam burst yang sama (bagian 2). Label yang tiba setelah giliran ditutup dicatat sebagai `stt_speaker_dropped`.
- **Field `TranscriptUpdate.speaker` dihapus.** Field itu tidak dibaca kode produksi dan selalu kosong. Breaking change ini diterima.
- **Pre-roll dihapus.** `PreRollBuffer`, setting `STTSettings.preroll_ms`, dan tesnya dihapus. STT menerima audio kontinu, sehingga memutar ulang ring-nya hanya menggandakan audio.
- **Satu pemilik rekonek.** `MuseClient` melaporkan kegagalan lewat `on_lost`, dan `MeetingAudioSession` memutuskan rekonek. Base class Pipecat yang merekonek sendiri sudah tidak ada.
- **Event journal Muse.** Nama event memakai verba yang terkendali: `stt_socket_failed`, `stt_speaker_dropped`, `stt_turn_skipped`, `stt_final_observed`, `stt_message_skipped`, dan `stt_message_dropped`. Nama lama (`stt_socket_lost`, `stt_speaker_after_turn`, `stt_empty_turn`, `stt_final_without_turn`, `stt_unhandled_message`, `stt_unparsable_message`) tidak dipakai lagi.
- **Klien dibuat langsung.** `MuseClient` dibuat langsung di `audio/audio.py`.
- **Keywords dan bahasa.** `keywords` diisi dari `speech.stt_keywords` dan berisi nama twin. Daftar nama peserta hanya ditambahkan jika datang dari brief yang diizinkan, tidak dikarang. Bahasa mengikuti `speech.language`, dan handshake memakai nama bahasa Inggris, bukan kode.
- **Skema field journal.** Durasi memakai milidetik (`*_ms`).

## 4. Belum

- **Provider STT kedua.** Kontraknya sudah ada, implementasinya belum. Riset vendor belum menjawab dukungan bahasa Indonesia per vendor, kebijakan retensi dan pemakaian data untuk pelatihan, batas sesi selain Meta, dan latensi terukur.
- **Registry STT.** Seperti registry TTS di `speech/`. Saat ini hanya ada satu provider.
- **Identitas participant.** Label cluster belum dipetakan ke participant, dan `voice_clusters` berisi label sesi, bukan nama. Akurasi label saat bicara bersamaan belum diukur, dan belum dibandingkan dengan atribusi per slot di rekaman rapat sungguhan.
- **Rapat Google Meet sungguhan.** Belum diuji. Menunggu link.
- **Sesi Muse 60 menit.** Dokumen Meta menyebut sesi ditutup setelah 60 menit dengan close code 1011. Batas ini belum diuji live. Jalur rekonek untuk kasus ini ada, tetapi buffer 4 detik lebih pendek dari jeda rekonek terpanjang. Belum ada keputusan apakah buffer diperpanjang atau audio dibiarkan hilang saat rekonek panjang.
