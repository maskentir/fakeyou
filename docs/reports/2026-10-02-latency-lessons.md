# Pelajaran latensi dari Vexa dan Amazon LMA

Tanggal: 2 Oktober 2026. Lingkup: pembacaan source tiga repo dan penghitungan ulang artefak acceptance yang sudah ada. Tidak menjalankan provider, Meet, atau benchmark baru.

**Kesimpulan:** prioritas berikutnya ialah membuktikan manfaat streaming jawaban per kalimat, disertai pemisahan waktu model sampai teks pertama dan waktu TTS sampai PCM pertama. Mengganti STT belum menjadi prioritas berdasarkan sampel ini. Streaming belum terbukti memenuhi target; waktu sebelum teks pertama mungkin tetap mendominasi.

Acuan: [acceptance live](2026-10-02-live-acceptance.md), [benchmark konfigurasi](2026-10-01-conversation-performance.md), dan [rencana yang selesai dilaksanakan](../plans/next-development.md).

## Bukti fakeyou

Repo berada di `feat/conversation-performance`, HEAD `7268b16`. Sumber data: journal `perf-1002-s1` dan `perf-1002-s2`, dibaca ulang melalui `read_journal()` dan `build_session_report()`. Sampel hanya giliran `spoken` yang mempunyai bukti PCM pertama: 23 jawaban, 15 dari S1 dan 8 dari S2. Giliran interupsi, pembatalan, dan sunyi tetap tercatat dalam acceptance, tetapi tidak digabung ke distribusi jawaban utuh ini.

Persentil mengikuti `summarize()`: median dan kuantil inklusif. Angka berikut dalam detik; persentil masing-masing tahap tidak boleh dijumlahkan menjadi persentil ujung-ke-ujung.

| Tahap | Sampel | p50 | p95 | Maks |
| --- | --- | --- | --- | --- |
| Akhir ucapan → transkrip final | 23 | 0,355 | 0,568 | 0,638 |
| Transkrip final → agent mulai | 23 | 0,002 | 0,004 | 0,802 |
| Agent mulai → balasan lengkap | 23 | 1,498 | 6,497 | 6,758 |
| Balasan lengkap → PCM pertama | 23 | 1,145 | 2,468 | 2,538 |
| Akhir ucapan → PCM pertama | 23 | 3,243 | 8,319 | 8,930 |
| Jumlah durasi request model per giliran | 23 | 1,486 | 6,216 | 6,741 |
| Jumlah durasi recall per giliran | 23 | 0,000 | 0,177 | 0,247 |

Jumlah seluruh durasi agent ialah 55,071 detik: request model 54,165 detik, recall 0,517 detik, dan selisih 0,389 detik. Durasi request model setara 98,4% jumlah durasi agent. Ini menunjukkan lokasi waktu tunggu; belum memisahkan jaringan, antrean provider, penalaran, dan pembangkitan teks. Durasi request juga bukan metrik waktu komputasi eksklusif bila ada operasi paralel.

Tiga jawaban memakai recall, masing-masing melalui dua request model. Median agent pada kelompok itu 4,542 detik; pada 20 jawaban tanpa recall 1,449 detik. Sampel terlalu kecil untuk menyimpulkan pengaruh pertanyaan secara kausal, tetapi memperlihatkan biaya dua perjalanan model. Mempercepat database saja tidak menghilangkan biaya tersebut.

Contoh giliran paling lambat:

| Sesi / awal ID transkrip | Request model | Durasi model | Recall | Agent | Balasan → PCM | Ujung-ke-ujung |
| --- | --- | --- | --- | --- | --- | --- |
| S2 / `5a2385ac` | 2 | 6,427 | 0,247 | 6,714 | 1,883 | 8,930 |
| S1 / `a7648419` | 1 | 6,741 | 0,000 | 6,758 | 1,387 | 8,488 |
| S1 / `9f69452f` | 1 | 3,795 | 0,000 | 3,808 | 2,484 | 6,793 |

### Memisahkan permulaan TTS dari persiapan lokal

Setiap `synthesis_started` dihubungkan dengan giliran pemilik saat request dibuka; `synthesis_pcm_emitted` memakai `request_id` yang sama. Ditemukan tepat satu PCM pertama untuk masing-masing dari 23 jawaban utuh.

| Pengukuran | p50 | p95 | Maks |
| --- | --- | --- | --- |
| Request TTS mulai → PCM tersedia (`first_chunk_ms`) | 1,137 | 2,458 | 2,524 |
| Sisa balasan → PCM setelah durasi di atas dikurangi | 0,008 | 0,011 | 0,014 |

Selisih memakai laporan per giliran yang dibulatkan milidetik, sehingga ada ketelitian pembulatan sekitar 1 ms. Sisa mencakup antrean, membuka jalur playback, dan persiapan sebelum request TTS; belum memisahkan proses OS satu per satu. Angka ini tidak mendukung dugaan bahwa membuka `paplay` menjelaskan jeda satu sampai dua detik menuju PCM pertama.

PCM pertama dicatat sebelum chunk diserahkan ke playback. Timestamp itu membuktikan audio tersedia secara lokal; belum membuktikan saat suara keluar dari sink, masuk ke Chromium, atau sampai ke telinga host. Perbaikan buffer playback dapat memperbaiki suara dan waktu terdengar tanpa tampak pada metrik PCM pertama.

### Batas alur yang terbukti di source

- [Responder](../../src/fakeyou/conversation/conversation.py), `LangGraphResponder.respond()` baris 154–195, menunggu `agent.ainvoke()` selesai. `_respond_pending()` baris 395–447 baru memanggil `session.speak()` setelah balasan lengkap diperiksa dan dibatasi kalimatnya.
- [Synthesizer](../../src/fakeyou/audio/voice.py), `MiniMaxSpeechSynthesizer` baris 117–175, memakai satu klien HTTP sepanjang umur objek dan sudah meminta PCM streaming. Mengaktifkan flag streaming TTS lagi tidak mengubah penantian balasan lengkap.
- [Playback](../../src/fakeyou/audio/playback.py), `create_paplay_stream_runner()` baris 63–117, membuka satu proses untuk satu aliran jawaban, lalu menulis chunk berurutan dengan `drain()`. Tidak membuka proses baru untuk setiap chunk.
- Journal mengukur request model selesai dan PCM pertama; belum mempunyai bukti teks jawaban pertama, kalimat pertama siap, HTTP TTS terbuka, atau PCM pertama tertulis ke sink.

Alur sekarang: ucapan selesai → final STT → seluruh agent selesai → request TTS → PCM pertama. Bagian pembangkitan model dan permulaan TTS belum bisa tumpang tindih.

## Vexa: bagian stabil dikirim lebih awal, bagian sementara tetap bisa direvisi

Checkout yang dibaca: `/Users/pushmove/Desktop/jemboed/vexa`, HEAD `dba990b4`. Berkas sumber yang dirujuk bersih terhadap HEAD saat pemeriksaan. Temuan berlaku untuk revisi lokal ini.

| Bukti source relatif terhadap repo Vexa | Perilaku | Pelajaran bagi fakeyou |
| --- | --- | --- |
| `core/meetings/modules/mixed-pipeline/src/chunked-transcriber.ts:85–118`, `:484–529` | Pemotongan provisional bawaan 4 detik, tick pengajuan 2 detik, serta penutupan saat jeda; penantian tidak hanya bergantung pada akhir monolog | Pisahkan satuan yang dapat dikirim dari selesainya seluruh pekerjaan; nilai timer itu bukan target percakapan kita |
| Berkas yang sama, `:1124–1178` | `localAgreement()` mengonfirmasi bagian awal yang stabil; ekor masih sementara dan memakai ID tersendiri | Jangan memperlakukan teks sementara sebagai fakta yang sudah aman diucapkan atau disimpan |
| Berkas yang sama, `:1218–1256` | Penutupan menangani ekor yang tertinggal dan membersihkan status sementara | Aliran harus mempunyai penutupan eksplisit; kalimat terakhir tanpa tanda baca tidak boleh hilang |
| `core/meetings/services/transcription/src/transcription/main.py:255–274`, `:405–438` | Pekerjaan tertunda memiliki admission tersendiri; kapasitas realtime dicadangkan, antrean penuh ditolak | Saat beban bertambah, ukur kompetisi pekerjaan latar dan percakapan; ini belum menjadi penyebab terbukti pada acceptance kita |
| `core/meetings/services/bot/src/tts-playback.ts:55–96` | Request TTS menerima teks lengkap, lalu PCM langsung dipipe ke `paplay` | Jalur ini sebanding dengan streaming PCM kita; bukan contoh lengkap streaming token LLM ke TTS |
| `core/agent/worker/engine.py:333–342`, `:372–380` | Event hasil harness diteruskan sebelum seluruh giliran selesai, dengan `turn_id` | Identitas giliran harus mengikuti setiap potongan, bukan ditentukan ulang dari giliran aktif saat hasil tiba |

**Pembacaan saya:** pola yang dapat dipindahkan ialah membedakan bagian siap dikirim dari bagian yang masih berubah. LocalAgreement adalah algoritme stabilisasi STT Whisper; bukan aturan untuk menilai kebenaran kalimat hasil LLM. Jangan memindahkan jendela 2–4 detik atau menambah Whisper/diarisasi ke jalur Muse yang sudah mempunyai p95 finalisasi 0,568 detik pada sampel ini.

## Amazon LMA: aliran audio langsung dan satu penulis playback

Checkout yang dibaca: `/Users/pushmove/Desktop/jemboed/amazon-transcribe-live-meeting-assistant`, HEAD `a23420ad`. Berkas sumber yang dirujuk bersih terhadap HEAD saat pemeriksaan. Jalur Nova dan ElevenLabs berbeda; keberadaan satu jalur tidak berarti seluruh repo memakai perilaku yang sama.

| Bukti source relatif terhadap repo Amazon LMA | Perilaku | Pelajaran bagi fakeyou |
| --- | --- | --- |
| `lma-virtual-participant-stack/backend/src/nova-agent.ts:1224–1293` | Stream bidirectional diproses per event; `audioOutput` segera diberikan ke playback tanpa menunggu teks final | Sasaran perubahan ialah audio mulai ketika bagian jawaban sudah tersedia; arsitektur speech-to-speech ini tidak identik dengan tiga provider kita |
| Berkas yang sama, `:1730–1833` | `pacat` dipakai berulang selama sesi; playback bawaan 80 ms; chunk ditulis langsung | Pertahankan satu penulis audio dan kontinuitas antarbagian jawaban. Buffer terlalu kecil dapat menimbulkan suara pecah; angka 80 ms belum diuji pada worker kita |
| Berkas yang sama, `:1237–1271`, `:1806–1828` | Sinyal interupsi menutup playback dan menolak audio sampai blok respons baru | Interupsi harus menghentikan pengiriman dan membersihkan audio yang sudah mengantre; teks/audio usang tidak boleh masuk lagi |
| Berkas yang sama, `:1860–1885` | Koneksi dipersiapkan saat frasa pemicu masih parsial | Persiapkan resource sebelum jawaban diperlukan. Fakeyou sudah mempertahankan koneksi STT dan klien TTS; tambahan manfaat harus diukur, bukan diasumsikan |
| `lma-websocket-transcriber-stack/source/app/src/calleventdata/transcribe.ts:274–309`, `:455–487` | PCM besar dibagi maksimal 16 KiB tanpa membuang byte; sumber audio bersama tetap hidup saat sesi STT berganti | Pembatasan chunk dan kepemilikan resource menjaga pemulihan. Batas chunk bukan jadwal batching 512 ms dan bukan pengaturan yang bisa disalin ke Muse |
| `lma-virtual-participant-stack/backend/src/elevenlabs-agent.ts:489–568` | Jalur ElevenLabs mengantre chunk dan membuka `paplay` per chunk | Jangan mengambil semua pola repo sebagai acuan performa; fakeyou sudah membuka proses sekali per jawaban |

**Pembacaan saya:** jalur Nova memberikan contoh paling relevan tentang keluaran yang langsung dikonsumsi dan playback berkelanjutan. Namun fungsi `playAudio()` menulis ke stdin tanpa menunggu `drain()`; penyalinan literal dapat menambah antrean yang tidak terukur. Fakeyou harus mempertahankan tekanan balik, urutan chunk, pembatalan generasi, dan kepemilikan resource yang sudah ada.

Mengadopsi Nova atau layanan speech-to-speech lain merupakan pilihan arsitektur terpisah: persona, recall, kewenangan, kualitas Bahasa Indonesia, konfigurasi peran, dan replay harus dibuktikan lagi. Pembacaan source tidak membuktikan bahwa provider itu akan lebih cepat atau lebih benar untuk kasus kita.

## Urutan eksperimen yang disarankan

Ini rekomendasi berdasarkan bukti, bukan implementasi yang sudah berjalan atau janji kelulusan target.

### 1. Pisahkan waktu menuju teks pertama sebelum memilih perubahan

Ukur dengan provider, model, persona, dan fixture yang sama:

- Request model mulai → potongan teks jawaban pertama; penalaran tersembunyi, metadata, dan argumen tool tidak dihitung sebagai jawaban.
- Request model mulai → kalimat pertama siap; kalimat pertama siap → PCM pertama; request model mulai → jawaban lengkap.
- Perjalanan model sebelum recall, durasi recall, dan perjalanan model setelah recall diukur sendiri-sendiri.
- Request TTS mulai → header HTTP/SSE diterima → PCM tersedia; PCM tersedia → penulisan pertama ke sink. Journal hanya menyimpan metadata, ID, jumlah, dan waktu.

Hipotesis: bila kalimat pertama tersedia jauh sebelum agent selesai, menumpangtindihkan TTS dengan sisa pembangkitan model dapat mengurangi waktu menuju suara pertama. Bila hampir seluruh waktu sudah habis sebelum teks pertama, streaming kalimat saja tidak akan menutup p95. Data sekarang belum membedakan dua keadaan itu.

### 2. Uji satu jalur streaming jawaban per kalimat

Bandingkan alur lama dengan kandidat menggunakan konfigurasi identik. Kandidat mengirim bagian jawaban yang layak diucapkan setelah kalimat utuh terbentuk, sementara pembangkitan sisanya berlanjut. Pertahankan satu aliran playback untuk satu jawaban agar pergantian kalimat tidak membuka proses atau menutup mikrofon berulang kali.

Kontrak yang perlu diputuskan dalam desain sebelum implementasi:

- Teks dari putaran pemilihan tool belum boleh langsung disuarakan. Dalam stream agent, teks dapat datang sebelum tool call muncul. Resolusi tool dan fase jawaban yang boleh berbicara harus mempunyai batas yang terbukti; beralih ke `astream()` saja belum menciptakan batas itu.
- Jangan hanya memanggil `await session.speak(kalimat)` berurutan dalam pembaca token: itu menunggu seluruh audio kalimat pertama selesai dan menghentikan konsumsi token. Produksi teks dan konsumsi audio perlu terkoordinasi dengan antrean terbatas.
- Batas kalimat total, nama dan angka, penolakan kewenangan, teks kosong, ekor tanpa tanda baca, serta pembatasan ucapan harus setara dengan alur lama. Pemotongan berdasarkan jumlah karakter saja dapat memutus tanggal atau fakta.
- Interupsi interim sekarang menghentikan playback; alur streaming juga harus membatalkan producer model/TTS dan menolak setiap kalimat berikutnya dari generasi yang sama. Jangan menunggu transkrip final untuk membersihkan producer usang.
- Kegagalan sebelum PCM boleh mengikuti kebijakan fallback yang diuji. Sesudah sebagian jawaban terdengar, mengulang jawaban dari awal dapat menduplikasi ucapan; kebijakan harus eksplisit dan mempunyai tes.
- Catatan meeting membedakan jawaban yang direncanakan, bagian yang dikirim, ucapan yang selesai, dan interupsi. Jangan menyimpan seluruh jawaban sebagai ucapan lengkap setelah hanya satu bagian sempat terdengar.

Uji deterministik terlebih dahulu: kalimat pertama menghasilkan PCM ketika sisa model sengaja ditahan; tool call tidak bocor sebagai suara; akhir tanpa tanda baca tetap terkirim; interupsi saat pembangkitan dan playback menghapus sisa tanpa audio susulan. Setelah itu ukur provider dan Meet. Manfaat latensi dan kualitas harus dilaporkan terpisah.

### 3. Optimalkan TTS berdasarkan pemisahan waktu request

TTS masih mempunyai p95 permulaan 2,458 detik, jadi streaming model belum menghapus biaya ini. Ukur permintaan dengan kalimat pertama yang pendek, pembangkitan kalimat berikutnya, kontinuitas suara, dan jeda antarbagian. Periksa waktu header serta PCM untuk membedakan overhead koneksi dari waktu tunggu keluaran; jangan menambah permintaan pemanasan tanpa bukti.

Jika kandidat TTS lain tersedia, gunakan teks dan format PCM yang sama, lalu verifikasi Bahasa Indonesia, nama, tanggal, dan interupsi. Klien HTTP fakeyou sudah dipakai ulang. Playback persisten antarjawaban dalam satu sesi baru diprioritaskan bila pengukuran sink atau suara host memperlihatkan biaya yang berarti.

### 4. Kurangi perjalanan model hanya bila kualitas tetap terjaga

Recall sendiri memakan 83–247 ms pada tiga jawaban, sedangkan dua perjalanan model berjumlah 4,179–6,427 detik. Kandidat pengurangan perjalanan perlu membuktikan keputusan kapan recall diperlukan dan kesesuaian sumber fakta; pemanggilan recall wajib maupun melewati recall tidak boleh ditentukan sekadar demi angka latency.

Perbandingan konfigurasi berikut memakai urutan bergantian untuk mengurangi bias perubahan beban provider. Tetap jalankan gate kualitas replay yang sudah gagal; sapaan nama tanpa sumber juga memerlukan regresi tersendiri karena belum tercakup oleh gate persona. Kemajuan waktu menuju suara pertama tidak membuktikan kualitas jawaban lulus.

## Batas hasil dan verifikasi

- Target live tetap median ≤3 detik dan p95 ≤6 detik pada sedikitnya 20 jawaban dari dua sesi. Gate replay tetap berlaku; tidak ada ambang yang dilonggarkan.
- Sampel berasal dari dua sesi dengan satu host. Tidak mengukur speaker multipihak, beban bersamaan, atau waktu audio sampai ke host secara otomatis.
- Tidak ada benchmark pembanding Vexa/LMA pada mesin, provider, dan fixture yang sama; pembacaan mekanisme source tidak membuktikan keunggulan numerik kedua repo.
- Tidak mengubah source produksi, dependency, konfigurasi, `.env`, persona, atau data memori. Tidak menjalankan ulang sesi live, provider berbayar, atau ingestion.
- Verifikasi kajian: 95 tes dokumentasi lulus dengan tiga peringatan dependency; snippet penghitungan di bawah diparse dan dijalankan tanpa provider; `git diff --check` bersih. Hasil implementasi dan tes pada acceptance sebelumnya tetap merupakan bukti pekerjaan sebelumnya.

Penghitungan inti dapat diulang tanpa provider:

```sh
uv run --no-sync python - <<'PY'
from pathlib import Path
from fakeyou.report import build_session_report, read_journal, summarize

turns = []
for session in ("s1", "s2"):
    path = Path(".runtime/audio/journals") / f"perf-1002-{session}.jsonl"
    report = build_session_report(read_journal(path))
    turns.extend(
        turn for turn in report["turns"]
        if turn["outcome"] == "spoken"
        and turn["speech_stopped_to_first_pcm_seconds"] is not None
    )
for field in (
    "speech_stopped_to_final_seconds", "final_to_agent_seconds",
    "agent_to_reply_seconds", "reply_to_first_pcm_seconds",
    "speech_stopped_to_first_pcm_seconds", "llm_duration_seconds",
    "recall_duration_seconds",
):
    print(field, summarize([turn[field] for turn in turns if turn[field] is not None]))
PY
```
