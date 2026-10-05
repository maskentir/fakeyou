# Riset CSRC di CloakBrowser — 2026-10-05

Koreksi atas addendum di [laporan probe](2026-10-05-attribution-probe.md)
yang menyimpulkan CloakBrowser memasang perisai stats WebRTC tanpa syarat.
Kesimpulan itu salah.

**Verdict: CloakBrowser bisa seperti Chromium. CSRC per peserta tersedia di
Meet, di dalam container worker, lewat metadata frame audio terenkode.**

## Ringkasan temuan

1. **"getStats kosong" adalah bug probe.** `statEntries()` mengecek
   `report instanceof Map`, padahal `RTCStatsReport` maplike tetapi bukan
   `Map`. Probe lalu jatuh ke `Object.values()`, yang selalu `[]`.
   - Setelah diperbaiki (iterasi `forEach`), Meet di container memberi 37
     entri stats per peer.
   - `receiver.getStats()` berisi `inbound-rtp` dengan `audioLevel` per
     track (3816 baris dalam satu sesi).
2. **ICE CloakBrowser dibatasi policy, bukan dimatikan.**
   - Default-nya JS menerima 0 candidate, dan loopback lokal tetap di
     state `new` (sama dengan issue
     [#543](https://github.com/CloakHQ/CloakBrowser/issues/543)).
   - Tes loopback lama gagal karena koneksi tidak pernah terbentuk, bukan
     karena stats diblokir.
   - `--webrtc-ip-handling-policy=default` mengembalikan perilaku Chromium,
     baik di Mac 145.0.7632.109.2 maupun di Linux container
     146.0.7680.177.3: koneksi `connected`, `inbound-rtp` terisi, SSRC
     `audioLevel` 0,708.
   - `--force-webrtc-ip-handling-policy=default` sendirian tidak berpengaruh.
   - Di Meet, koneksi tetap jalan tanpa flag lewat ICE-TCP (`host/tcp` dan
     `prflx/tcp` di Mac).
3. **`getSynchronizationSources`/`getContributingSources` kosong di Meet
   container karena jalur media Meet sendiri.**
   - Hasilnya 0 dari 11.191 sampel, padahal RTP mengalir.
   - Meet memanggil `RTCRtpReceiver.createEncodedStreams()` 4 kali (insertable
     streams) dan membuat 2 `AudioWorkletNode`.
   - Track yang masuk WebAudio tidak sama dengan track receiver (0 dari 6
     overlap).
   - Percobaan menempelkan track ke `<audio>` senyap tidak membantu di Meet,
     jadi dibuang.
   - Di Mac headed, kedua API itu terisi (SSRC `6666`, CSRC `42` dan
     `1416569104`). Bedanya belum dijelaskan; sesi itu tidak mengukur
     pemakaian insertable streams.
4. **Metadata frame terenkode memberi identitas dan energi.**
   - Probe membungkus `createEncodedStreams()` dan melewatkan `readable`
     milik Meet melalui `TransformStream` pasif. Frame diteruskan utuh.
   - Tiap frame (±20 ms) membawa:
     - `synchronizationSource`: slot virtual Meet;
     - `contributingSources`: SSRC peserta yang sedang dirutekan ke slot;
     - `audioLevel`: level dari header extension RTP. Ini terverifikasi di
       loopback lokal, tetapi belum direkam di Meet live.
   - Ini metadata WebRTC standar (`RTCEncodedAudioFrameMetadata`), bukan
     protokol privat Meet.
5. **Hasil live dua orang (probe-csrc-7, container):**

   | Slot (receiver) | SSRC virtual | CSRC peserta |
   | --- | --- | --- |
   | 0 | `6666` | `1416569104` (A), ditambah `42` |
   | 1 | `6667` | `3231413436` (B), ditambah `42` |

   - Tiap slot hanya membawa satu CSRC peserta sepanjang sesi.
   - `42` muncul di semua slot dan diperlakukan sebagai penanda konstan Meet,
     bukan orang.
   - Kehadiran frame per CSRC belum cukup untuk menentukan siapa yang
     bicara. Saat B bicara, frame A ikut mengalir (porsi ±0,5), kemungkinan
     bocoran mic karena satu ruangan. Penentu bicara harus memakai
     `audioLevel` per frame, yang kini direkam probe.

## Sesi live

| Sesi | Isi | Hasil |
| --- | --- | --- |
| Mac headed (diag terpisah) | CloakBrowser default di Meet | `getStats` 41 entri; SSRC/CSRC terisi; peserta mendengar gema, jadi tidak diulang di Mac |
| probe-csrc-3 | reader stats diperbaiki | stats lengkap; SSRC/CSRC 0; satu fault STT `no_transcripts` lalu reconnect |
| probe-csrc-4 | render senyap | `blocked_by_platform` dalam 10 detik setelah join beruntun; retry 60 detik kemudian masuk |
| probe-csrc-5 | render senyap | SSRC/CSRC tetap 0; track WebAudio ≠ track receiver |
| probe-csrc-6 | penghitung jalur media | `createEncodedStreams` 4×, `AudioWorkletNode` 2×, tanpa transform/generator |
| probe-csrc-7 | keran frame terenkode, 2 orang | SSRC per slot, CSRC per peserta |

## Label suara anonim pada sesi live yang sama

Cek awal, belum acceptance. Pada probe-csrc-7 ada 9 transkrip: 5 berlabel
dan semuanya benar (A tiga kali, B dua kali), 4 `unknown(overlap)`, dan 0
salah. Overlap tinggi konsisten dengan bocoran antar-mic di satu ruangan.

## Dampak

- Penyatuan label lintas slot, dan lebih dari 3 peserta, menjadi mungkin
  tanpa embedding suara. Pembicara adalah CSRC dari slot yang dominan
  menurut `audioLevel` frame. CSRC stabil per join peserta, dan tidak
  bergantung pada slot.
- Nama tetap butuh pengikatan CSRC ke device roster (eliminasi seperti
  binder slot) dan harus lolos gate presisi 0,98 sebelum dinyalakan.
- Risiko:
  - Membungkus `createEncodedStreams` menambah satu hop JS per frame di
    jalur media Meet. Sesi probe-csrc-7 tetap normal (audio, STT, dan
    jawaban twin jalan), tetapi belum diuji lama.
  - Pembungkus prototype bisa terdeteksi.
  - Makna CSRC `42` belum diketahui.
- Langkah berikut:
  1. Sesi live yang merekam `audioLevel` frame, dua orang di ruangan
     berbeda.
  2. Replay aturan dominan per slot dengan pemetaan slot ke CSRC.
  3. Bila stabilitas per peserta ≥0,95, ganti sumber label suara ke CSRC.

## Verifikasi kode

- Tes browser baru merah sebelum perubahan dan hijau sesudahnya:
  - `test_probe_counts_entries_of_a_real_stats_report`;
  - `test_probe_reads_identity_and_level_from_encoded_audio_frames`.
- `pytest -m browser tests/test_attribution_probe.py`: 7 lolos.
