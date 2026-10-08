# Deep Research: Muse Voice Transcribe dan Dampaknya ke Roadmap

Tanggal: 2026-10-08. Bahasa: Indonesia. Semua klaim eksternal diverifikasi
dari koferensi publik dan dokumentasi Meta; ketidakpastian ditandai eksplisit.

## 1. Fakta terverifikasi

Model: `muse-voice-transcribe-1.0`, dirilis Meta Superintelligence Labs
1 September 2026. Satu model autoregressive untuk streaming ASR +
diarization 20+ speaker + endpointing sekali jalan, tanpa post-processing
wajib. Audio diproses per chunk 80ms; tiap chunk menjadi satu soft token;
model memilih lanjut dengar atau emit teks (adaptive delay, dilatih dengan
RL atas reward gabungan WER + delay).

Angka (laporan vendor + Artificial Analysis, snapshot 1 September 2026):

| Metrik | Nilai | Pembanding |
| --- | --- | --- |
| WER transkrip final | 3,1% @ 0,16 dtk setelah akhir bicara | Grok 2.0: 2,7% @ 0,49 dtk; Scribe v2: 3,6% @ 0,14 dtk |
| WER partial pertama | 3,6% @ 0,13 dtk | Grok 2.0: 3,4% @ 0,49 dtk |
| DER diarization | rata-rata 17,5% di evaluasi Meta | pyannote community-1: ~11% |

Harga: $3 per 1.000 menit audio ($0,18/jam), sama untuk streaming dan file.
Diarization termasuk, bukan add-on. Hanya hosted API
(`wss://api.meta.ai/v1/asr/realtime` + endpoint file); bobot tidak dirilis,
tidak ada jalur self-host.

Bahasa: dilatih 70+, tervalidasi ekstensif 25. Daftar 25 yang beredar
mencantumkan Indonesia dan Melayu berdampingan dengan Inggris
([uncertain]: daftar dari media pihak ketiga, bukan halaman resmi Meta;
perlu konfirmasi ke dokumen bahasa resmi sebelum klaim kontrak).
Code-switching native dan keyword/context biasing didukung.

Batasan terdokumentasi (docs resmi Meta + analisis independen):

- Timestamp hanya level giliran (turn), BUKAN level kata. Tidak ada
  confidence score, deteksi sound-event/emosi, atau reformat transkrip.
- Sesi streaming maks 60 menit; WebSocket baru = sesi baru; tanpa token
  resume (label diarization reset tiap sesi baru).
- Autentikasi lewat frame handshake JSON pertama (≤10 detik); header
  Authorization diabaikan.

## 2. Temuan kunci: kita sudah memakainya

Fakeyou memakai Muse Voice Transcribe sebagai SATU-SATUNYA engine STT
(`src/fakeyou/audio.py`: `ManagedMuseSTT(MetaSTTService)`;
`src/fakeyou/settings.py`: provider `muse`, model
`muse-voice-transcribe-1.0`). Mode default `ENDPOINTING`, diplambing dari
`settings.stt.mode` ke tiap slot (audio.py dua titik konstruksi).

Yang belum dipakai: mode `DIARIZATION` tersedia di literal settings tapi
tidak pernah disetel di mana pun. Pipecat mendukungnya (event `speaker`
+ label A/B pada hasil), tetapi label hanya menumpang di `frame.result`
mentah dan `user_id` statis — tidak ada kode kita yang membacanya.
Atribusi kita hari ini murni CSRC/slot/tenure, independen dari diarization
model.

Konsekuensi desain yang cocok: karena API memang tanpa timestamp kata,
arsitektur turn-level kita (merge transkrip, pregen diff, journal
`capture_ms`) sudah selaras — tidak ada yang perlu dibuang bila adopsi
fitur baru.

Konsekuensi biaya: arsitektur per-stream membuka satu sesi STT per slot,
jadi tagihan per meeting = $0,18 x jumlah slot aktif (±$0,72/jam untuk
4 slot). Sesi >60 menit memaksa sesi baru (reconnect + buffer kontinuitas
kita sudah menutup ini untuk teks; label diarization akan reset).

## 3. Dampak ke roadmap

Fase B diarization (butir 18) punya opsi baru yang mengubah trade-off riset
lama (sherpa CPU vs pyannote GPU):

- Opsi M (baru, direkomendasikan untuk spike): correction pass memakai
  endpoint file Muse yang sama. Nol dependency baru, nol GPU, nol vendor
  baru, harga sama. Kelemahan: DER 17,5% (lebih buruk dari pyannote ~11%)
  dan timestamp tetap level giliran — cukup untuk koreksi atribusi
  "siapa bilang apa", tidak cukup untuk forced alignment kata.
- Opsi S (riset lama): sherpa-onnx CPU lokal. Privasi penuh, tapi akurasi
  di bawah keduanya dan menambah beban CPU worker.
- Opsi P (riset lama): pyannote community-1. DER terbaik (~11%) tapi butuh
  GPU + token HF gated + atribusi CC-BY.

`verified_name` (butir 19) tidak terpengaruh oleh pilihan mana pun:
diarization hanya memisahkan suara anonim; pengikatan nama tetap butuh
rantai klaim roster + voiceprint. Pregen juga tidak terpengaruh (bermain
di level teks interim/final, bukan label speaker).

Oportunis kecil di luar Fase B: mode `DIARIZATION` live bisa menjadi sinyal
tambahan untuk jalur mixed (bukan per-stream), dibaca dari `frame.result`.
Perlu spike karena label reset tiap sesi dan semantiknya mid-turn, bukan
batas giliran.

## 4. Privasi: audio sudah keluar, yang penting tier-nya

Kekhawatiran "API = pembalikan kebijakan privasi" hanya separuh benar:
audio meeting SUDAH dikirim ke Meta Model API hari ini untuk transkripsi.
Yang menentukan nasib data adalah tier akun, bukan kode:

- Tier Contributor (diskon): Meta boleh memakai prompt/output untuk
  improvement termasuk training. Ini default murah yang berbahaya untuk
  audio meeting.
- Tier Standard: prompt/output tidak untuk training.
- Zero-data-retention: fitur enterprise, diajukan lewat sales
  ([uncertain]: berdasarkan koferensi, bukan dokumen kontrak publik).

Konteks posture: Juni 2026 Meta menghentikan sementara program internal
pengumpulan aktivitas karyawan untuk training setelah kebocoran akses
internal (Reuters/Business Insider). Ini bukan bukti soal API, tapi alasan
untuk menuntut jawaban tertulis soal retensi audio STT sebelum memakai
endpoint file untuk arsip sesi.

Tindakan wajib sebelum Fase B opsi M: (1) verifikasi tier akun di dashboard
dev.meta.ai adalah Standard; (2) minta pernyataan tertulis retensi audio
+ ketersediaan ZDR untuk workload STT.

## 5. Rekomendasi

1. Tunda implementasi Fase B sampai spike selesai (±1 sesi kerja):
   kirim 2-3 rekaman sesi lama (campuran ID-EN + overlap serumah) ke
   endpoint file mode DIARIZATION, ukur DER turn-level vs label live dan
   WER Bahasa Indonesia, bandingkan dengan baseline kini.
2. Dari angka spike, pilih M / S / P. Hipotesis awal: M menang kecuali DER
   live-nya jauh di bawah kebutuhan notulen.
3. Paralel non-blocking: verifikasi tier akun + pertanyaan retensi ke Meta.
4. Claim-fix live verification (butir 1 antrean) tetap jalan duluan dan
   independen — tidak menunggu spike ini.

## 6. Hasil spike endpoint file (2026-10-08 malam)

Enam klip 3 menit dari rekaman v19 (mix 2 slot + 1 solo), total 18 menit
(±$0,05), mode DIARIZATION. Cara pakai: `POST
https://api.meta.ai/v1/asr/transcribe`, multipart part `request` (JSON:
`model`, `mode`, `audioEncoding: "WAV"`) + part `audio`. Respons: JSON
dengan `turns[]` berisi `turnId/startMs/endMs/transcript/speaker`.

- Turn-taking bersih: purity label vs slot energi dominan 1,000
  (dev2a-mix 26 giliran, eval2a-mix 17 giliran, solo 13/13 satu speaker).
- Cross-talk berat (serumah): label stabil, tapi ada over-segmentasi —
  ekor kalimat ("yang berdebu.", "Pagi.", "satu") dipecah jadi speaker B
  palsu. Proxy energi ikut rusak (rasio 0,57-0,72 vs 0,99+ saat bersih).
- Overlap simultan: tidak ditemukan di rekaman (maks 4 dtk/180 dtk
  kedua-slot-panas, kemungkinan bleed) — tidak terukur. API mengemit
  satu speaker per giliran, jadi tabrakan sejati pasti collapse.
- Kualitas teks Indonesia: sangat baik, termasuk kolokial ("gue",
  "mic-nya mati", "enggak"). Satu artefak ("kodangan") + satu duplikat
  kalimat (bacaan ulang atau gema).
- WER absolut tak terhitung (naskah bacaan tidak tersimpan sebagai teks).

Kesimpulan spike: Opsi M tervalidasi untuk correction pass audio
giliran-bicara. Desain Fase B perlu heuristik gabung (turn pendek <2 dtk
yang bersebelahan) untuk melawan over-segmentasi cross-talk. Opsi P
(pyannote) hanya relevan bila nanti butuh separasi overlap sejati.

## 6b. Adendum validasi Fase B Task 5 (2026-10-09)

`fakeyou diarize run --meeting v19-dev2a` (±20 mnt, 4 chunk @5 mnt):
exit 0, 81 giliran (speaker A/B), `show` 74/80 entri tercakup status ok,
`record.jsonl` bita-identik (sha e6645753 sama sebelum/sesudah), sidecar
tanpa teks transkrip. Temuan operasional: chunk 10 menit kena HTTP 504
dua kali; produksi dipatok 5 menit (043802d). 6 entri tak tercakup
(di luar span + toleransi: twin speech dan entri tepi). Keputusan adopsi
konsumen (debrief/ingestion): follow-up di luar Fase B sesuai plan.

## 7. Pertanyaan terbuka

- Apakah Indonesia benar di daftar 25 tervalidasi resmi? (konfirmasi docs)
- Retensi audio dan ketersediaan ZDR untuk STT? (tanya Meta tertulis)
- Region serving API? (satu sumber menyebut single-region untuk kompetitor;
  untuk Muse belum ada bukti — [uncertain])
- Batas konkurensi/kunci per tim untuk endpoint realtime dan file?
  ([uncertain])

## Sumber utama

- Dokumentasi resmi: dev.meta.ai/docs/speech-to-text (di-fetch 2026-10-08)
- Meta research blog via liputan: marktechpost, slator, 9to5mac
- Analisis independen: theweightedaverage (timestamp trade-off), orcarouter
  (endpoint/limit), blog.9cv9 (daftar bahasa, WIP verifikasi)
- Kode repo: `src/fakeyou/audio.py`, `src/fakeyou/settings.py`,
  pipecat `services/meta/stt.py` di `.venv`
