# Acceptance ketiga setelah refactor

Tanggal: 1 Oktober 2026. Status: pemeriksaan fungsi live lulus pada percobaan kedua; target latensi belum tercapai. Rencana terkait: [refactor](../plans/refactor.md) dan [rencana jangka panjang](../plans/long-term.md).

## Lingkungan

- Acceptance live memakai source commit `2a56df7` pada branch `feat/explicit-config-refactor`. Pengujian dilanjutkan di repo utama setelah pengguna memindahkan branch dari worktree.
- Saat acceptance dimulai, hash seluruh file Python dalam image `fakeyou-meeting-worker:local` cocok dengan source repo utama. Neo4j dan embeddings healthy.
- Jawaban memakai MiniMax-M3, TTS MiniMax, STT Muse, ekstraksi memori `muse-spark-1.2`, dan embedding Granite lokal. Dimensi konfigurasi serta nilai yang dibaca Graphiti saat import sama-sama 384. Tidak ada fallback jawaban aktif.
- Persona `Rani Twin` memakai Bahasa Indonesia, identitas `acc2_owner`/`acc2_twin`, tool recall, dan ingestion transkrip. Episode `acc2` serta owner note yang sudah dipulihkan pengguna dipertahankan.
- Link Meet: [epe-abys-zev](https://meet.google.com/epe-abys-zev).

## Percobaan pertama

Meeting ID: `acc3-1001-s1`. Worker dijalankan dengan audio, percakapan, barge-in, persona, dan memori; batas lobby 180 detik serta durasi sesi 600 detik.

| Bukti journal | Waktu WIB / hasil |
| --- | --- |
| `audio_opened` | 20:51:31,738; durasi membuka audio 1,736 detik |
| `join_requested` | 20:51:31,745 |
| `join_finished` | 20:54:46,790; `status=timeout` |
| `session_finished` | 20:54:47,144; `terminal_reason=lobby_timeout_reached` |
| Catatan meeting | 0 transkrip, 0 ucapan twin, 0 hasil tool |
| Keluar worker | Kode 0; teardown selesai |

Urutan event membuktikan pembuka audio selesai sebelum permintaan masuk. Percobaan ini belum membuktikan ucapan pertama tertangkap setelah admission, suara terdengar di host, recall live, atau interupsi karena twin tidak masuk ke meeting.

Artefak lokal berada di `.runtime/audio/journals/acc3-1001-s1.jsonl`, `.runtime/audio/meetings/acc3-1001-s1/record.jsonl`, dan `.runtime/acceptance-2026-10-01/`. Journal baru menyimpan metadata; isi percakapan tetap menjadi tanggung jawab catatan meeting.

## Percobaan kedua

Meeting ID: `acc3-1001-s2`. Host meminta pengulangan dan menerima twin. Image masih memakai source commit `2a56df7`; perbaikan cleanup dari review akhir dikerjakan setelah image ini dibuat.

| Bukti journal | Waktu WIB / hasil |
| --- | --- |
| `audio_opened` | 21:10:11,197; durasi 1,661 detik |
| `join_requested` | 21:10:11,202 |
| `join_finished` | 21:10:32,611; `status=admitted`, audio siap |
| `conversation_bound` | 21:10:33,852; 1,241 detik setelah admission |
| Transkrip pertama | 21:10:34,133; fragmen “Ini” |
| Pertanyaan beta launch | 21:10:39,723; kalimat lengkap tercatat |
| SIGINT | 21:13:37,733; 0,047 detik setelah transkrip kalimat stop |
| `agent_cancelled` | 21:13:37,787; 0,055 detik setelah SIGINT |
| `session_finished` | 21:13:40,626; `left_by_owner`, kode 0 |
| `memory_ingest_finished` | 21:16:38,622; tiga jendela selesai |
| `cli_exited` | 21:16:38,623; kode 0 |

Audio siap sebelum admission. Fragmen pertama dan pertanyaan lengkap tertangkap; startup tidak membuang pertanyaan beta launch pada sesi ini.

| Pertanyaan | Jawaban yang tercatat | Dari akhir ucapan ke PCM pertama |
| --- | --- | --- |
| Kapan beta launch Orchid? | 14 Oktober 2026 | 9,137 detik |
| Demo customer Orchid jadinya hari apa? | Jumat, 3 Oktober 2026, jam dua siang | 6,891 detik |
| Kickoff vendor Orchid kapan dan siapa PIC-nya? | Selasa jam 10 pagi, Budi | 4,557 detik |
| Siapa yang pegang checklist launch? | Rani | 14,695 detik |
| Persetujuannya harus lewat dia juga? | Budget dan kontrak butuh persetujuan tertulis Rani | 4,563 detik |

Tool recall selesai sekali dan mengambil owner note serta episode `acc2`. Jawaban kickoff memakai konteks yang sudah diambil; pertanyaan kata ganti tetap merujuk Rani. Ada 12 transkrip, enam ucapan twin yang selesai, satu hasil tool, dan tiga interupsi yang tercatat. Fragmen atau percakapan tambahan host ikut masuk dalam hitungan ini.

Host mengonfirmasi jawaban terdengar utuh, suara berhenti ketika “Oke cukup, berhenti dulu”, dan tidak ada suara baru setelah “Terakhir cek memori vendor”. Journal mencatat tiga pemicu interupsi dari transkrip interim; median dari awal suara pemotong sampai playback dihentikan 1,618 detik, maksimum 1,833 detik. Kalimat stop mengirim SIGINT saat request model masih berjalan. Tidak ada `playback_started` setelah sinyal tersebut, request dibatalkan, dan twin keluar dengan normal.

Ingestion selesai tanpa fault: tiga episode, tujuh fakta dalam hasil jendela, dan nol kegagalan. Durasi per jendela 50,519; 67,530; dan 59,939 detik, seluruhnya di bawah batas 120 detik. Tujuh fakta pada hasil ingestion tidak berarti tujuh fakta unik baru. Pemeriksaan baca Neo4j setelah selesai menemukan 17 fakta dalam grup persona, semuanya berlaku; hasil deduplikasi dapat menggunakan fakta yang sudah ada.

Median latensi akhir ucapan ke PCM pertama pada delapan giliran yang mempunyai bukti PCM adalah 5,727 detik, p95 16,895 detik, dan maksimum 18,080 detik. Lima pertanyaan utama semuanya melampaui target sekitar tiga detik. Hasil ini membuktikan fungsi startup, recall, konteks, interupsi, dan stop pada sesi ini; penerimaan performa tetap belum lulus.

Artefak lokal: `.runtime/audio/journals/acc3-1001-s2.jsonl`, `.runtime/audio/meetings/acc3-1001-s2/record.jsonl`, `.runtime/acceptance-2026-10-01/report-s2.json`, serta `stop-evidence-s2.json` dalam direktori yang sama. Isi percakapan berada pada catatan meeting; journal menyimpan metadata.

## Verifikasi lokal

Sebelum gelombang cleanup, suite non-live dan non-container pada commit `2a56df7` lulus: 1.381 tes, 16 dilewati, dan 20 tidak dipilih. Pemeriksaan dokumentasi tambahan lulus: 107 tes dan 15 dilewati. Empat peringatan pada suite penuh terdiri atas tiga peringatan API dependency yang akan dihapus serta satu transport subprocess tes yang ditutup setelah event loop selesai.

Commit cleanup `ec956b1` dan `a845400` menutup SDK HTTP milik konteks memori dan iterator PCM milik engine, termasuk ketika consumer gagal atau dibatalkan. Konstruksi serta disposal memori menunggu resource selesai ditutup meskipun caller dibatalkan berturut-turut. Fixture subprocess diperbaiki agar tidak menyisakan turunan shell. Pengecualian tiga batas pemilik penutupan percakapan dicatat sesuai kontrak API tanpa mengubah urutan shutdown. Review akhir menyatakan ketiga temuan selesai, tanpa masalah baru.

Verifikasi source terakhir `a845400`:

- Suite non-live dan non-container: 1.408 lulus, 16 dilewati, 20 tidak dipilih, dengan tiga peringatan dependency. Pengujian browser lokal dan eval offline termasuk dalam suite ini.
- Ruff, pemeriksaan format, dan mypy bersih. Regresi ownership membuktikan kegagalan sebelum perbaikan dan penutupan resource setelah perbaikan, termasuk pembatalan berulang saat konstruksi maupun disposal.
- Image runtime terbaru dibangun; hash seluruh 37 file Python cocok dengan source. `fakeyou check` pada image tersebut selesai dengan kode 0.
- Tes stop bersinyal pada image runtime terbaru lulus. Tes audio native browser → worker → browser lulus pada image `ec956b1`; playback tidak berubah dalam koreksi `a845400`. Peringatan tambahan pada tes native hanya mengenai direktori cache pytest yang tidak dapat ditulis.
- Pemeriksaan baca memori pada source terbaru tetap menemukan owner note untuk Jumat, 3 Oktober, jam dua siang. SDK LLM, embedding, dan reranker semuanya sudah tertutup ketika konteks selesai. Pemeriksaan ini tidak menambah atau menghapus fakta.

Bukti lokal review dan verifikasi ada di `.runtime/acceptance-2026-10-01/`, termasuk `cleanup-fix-report.md`, `cleanup-rereview.md`, `final-suite-a845400.log`, `final-image-verification.json`, dan `final-memory-readback.log`.

## Batas hasil

Sesi Meet memakai source `2a56df7`; perbaikan cleanup sesudahnya diverifikasi terpisah melalui tes, image, dan pemeriksaan baca memori di atas. Image terbaru belum diuji ulang di Meet. Sesi ini memakai satu host; atribusi per speaker dan pengujian dua pembicara belum dibuktikan. Branch belum di-push atau di-merge.

Eval replay MiniMax yang gagal sebelum acceptance tetap menjadi keterbatasan terpisah: ambang kualitas tidak dilonggarkan dan model jawaban tidak diganti dalam pengujian ini. Kredit judge OpenAI yang habis tidak dipakai untuk penerimaan Meet.
