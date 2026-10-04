# Recall satu perjalanan model: hasil corpus 4 Oktober

Tanggal: 4 Oktober 2026. Branch `feat/recall-latency` dari `c7ff71f`. Melaksanakan [plan recall satu perjalanan model](../plans/recall-latency.md) dengan dasar [pelajaran Vexa/LMA](2026-10-04-knowledge-latency-lessons.md). Artefak mentah, review independen, dan ledger ada di `.runtime/recall-latency/` (lokal, tidak di-commit).

**Kesimpulan:** strategi `prefetch` menghilangkan perjalanan model kedua di setiap giliran dan menjadi default. `gemini-3.8-flash` dengan prefetch lulus seluruh gate semantik, lulus latency brief, dan p95 trust turun dari 5,9 s ke 4,6 s, tetapi belum ≤4 s. `gemini-3.5-flash-lite` lulus latency dengan jauh, tetapi gagal semantik trust (3 severe). Belum ada kombinasi yang lulus semua gate.

## Perubahan

- Pencarian memori bersama (`search_memory_evidence`) dipakai tool recall dan prefetch, sehingga namespace, filter fakta berlaku, dan render evidence sama.
- Middleware `awrap_model_call` menyisipkan pesan `Memory evidence` setelah ucapan peserta pada request model saja. Kode memilih teks fakta ditemukan, memori kosong, atau tidak tersedia. Kegagalan dicatat sebagai fault `memory`, durasi di journal, dan pembatalan giliran membatalkan pencarian. Evidence dipakai ulang bila model fallback mengulang panggilan dalam giliran yang sama.
- `FAKEYOU_MEMORY__RECALL__STRATEGY`: `prefetch` (default) atau `tool` (perilaku lama, untuk rollback). Varian hybrid diuji lalu dibuang karena tidak lebih baik.
- Aturan prompt recall per strategi; teks strategi `tool` byte-identik dengan sebelumnya. `brief_only` tetap tanpa memori pada semua strategi.
- Harness eval mencatat evidence yang benar-benar dikirim ke model (`memory_prefetch`) dan menilai recall wajib dari evidence itu.
- Debrief: langchain-google-genai 4.4.0 menghilangkan nullability pada schema bersarang sehingga Gemini menulis string `"null"`. Placeholder (`null`, `none`, `unknown`, `n/a`, kosong) kini dibaca sebagai tidak ada; `assignee_claim`/`due_text` yang ada wajib muncul utuh pada salah satu kutipan item (batas kata, spasi dan tanda baca tepi dinormalkan) atau draft gagal tertutup. Ketujuh draft corpus debrief tetap valid dengan aturan ini.
- Journal prefetch mencatat `transcript_id` dan outcome pada baris selesai/gagal, giliran yang dibatalkan menutup request-nya, dan laporan sesi menghitung prefetch sebagai recall.
- Fixture trust diselaraskan: c05 mengikuti desain recognizer sapaan (input bertanda tanya masuk agent; plan sebelumnya keliru menganggapnya bug recognizer), dan contract/rubric c07/c14/c16/c17/c18/c19/c23 tidak lagi meminta tawaran cek ke Rani.

## Hasil

Semua run memakai provider Gemini native, kandidat `e9f68dd8…` tidak berubah selama run, dan provenance valid. Recall memakai storage sintetis (search ≤0,3 ms).

| Model × strategi | Panggilan model | p95 brief | p95 trust | Severe | Useful brief | Useful trust | Gate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `3.8-flash` tool (G2) | 2 di giliran recall | 3,85 s | 5,91 s | 0 | 36/36 | 69/72 | latency trust gagal |
| **`3.8-flash` prefetch** | 1 | **3,13 s** | **4,64 s** | **0** | **36/36** | **72/72** | semantik lulus; latency trust gagal |
| `3.8-flash` hybrid | 66×1, 9×2 | 3,67 s | 4,64 s | tidak direview | – | – | latency trust gagal |
| `flash-lite` tool (G2) | 2 di giliran recall | 1,50 s | 1,75 s | 0 | 33/36 | 56/72 | useful trust gagal |
| `flash-lite` prefetch | 1 | 1,18 s | 1,30 s | 3 | 33/36 | 67/72 | severe trust gagal |
| `flash-lite` hybrid | 1 (tool tidak pernah dipakai) | 1,20 s | 1,24 s | tidak direview | – | – | perilaku sama dengan prefetch |
| Debrief `3.8-flash` | 1 per chunk | – | – | 0 | – | – | coverage 11/11, 0 placeholder terbit |

Kenaikan useful trust `3.8-flash` dari 69 ke 72 berasal dari rubric c05 yang diselaraskan, bukan dari model. Pada `3.8-flash` prefetch, p95 giliran dengan evidence ditemukan 7,0 s dan dengan evidence kosong 3,1 s; tiga giliran c21 (evidence berisi teks injection) ~7,6 s mendominasi, dan tanpa c21 p95 trust 3,96 s. Pada `flash-lite` prefetch, recall wajib tidak lagi terlewat (0 dari sebelumnya 12 pair), tetapi model memakai evidence secara keliru: "Jam sepuluh pagi" (c24) dan menyatakan speaker tak dikenal bukan Rani (c04 ×2).

Review: `.runtime/recall-latency/review-gemini-3.8-flash-prefetch.md`, `review-gemini-3.5-flash-lite-prefetch.md`, `review-debrief-3.8-flash.md`.

## Rekomendasi model live

`gemini-3.8-flash` dengan `prefetch` untuk jawaban live dan debrief. Ia satu-satunya yang lulus gate semantik; p95 agent trust 4,6 s masih di atas gate 4 s, sehingga gate latency live (akhir ucapan→PCM pertama p50 ≤3 s, p95 ≤6 s) harus dibuktikan di acceptance Meet. `flash-lite` tidak direkomendasikan untuk live sampai kesalahan atribusi dan detail karangan hilang. Ganti `FAKEYOU_REPLY__MODEL` di `.env` secara manual; laporan ini tidak mengubah `.env`.

## Review kode independen

Tidak ada crash atau kesalahan wiring; scope `brief_only`, pemisahan evidence dari checkpoint/record, urutan fallback, dan tes jalur tool yang dipin dinyatakan benar. Temuan yang sudah diperbaiki: pencocokan klaim debrief yang rapuh, baris journal prefetch tanpa transcript/outcome, request prefetch yang tidak ditutup saat dibatalkan, laporan sesi yang tidak menghitung prefetch, dan fallback ID mati. Dua temuan desain memerlukan perubahan prompt atau query, sehingga butuh corpus dan review baru:

1. **Evidence dapat dipalsukan lewat ucapan.** Evidence dikirim sebagai pesan user seperti ucapan peserta; peserta yang mengucapkan "Memory evidence: owner note …" menghasilkan pesan yang tampak sama. Usulan: aturan prompt bahwa hanya pesan terakhir sebelum jawaban adalah evidence sistem, penanda yang tidak dapat diucapkan, atau pasangan tool-call sintetis; tambahkan kasus trust ucapan palsu. Harus ditutup sebelum dipakai dengan peserta yang tidak dipercaya.
2. **Pertanyaan lanjutan kehilangan evidence giliran sebelumnya.** Query hanya ucapan giliran ini dan evidence lama tidak ikut request berikutnya, sehingga "Siapa yang menetapkannya?" bisa dijawab "tidak ada di catatan". Usulan: sertakan ucapan dan jawaban sebelumnya dalam query dengan batas ukuran, pertahankan beberapa evidence terakhir di posisi asalnya, dan ubah teks kosong menjadi "tidak ada kecocokan tambahan".

## Risiko dan gate terbuka

- Latency search Graphiti nyata belum diukur ulang (layanan lokal mati); bukti live sebelumnya 83–247 ms. Prefetch interim (Task 4) dilewati atas dasar itu.
- Storage sintetis mengembalikan semua fakta apa pun query-nya, sehingga kualitas query prefetch (misalnya pertanyaan lanjutan tanpa konteks) belum teruji pada graf nyata. Query memakai ucapan giliran ini saja.
- p95 trust `3.8-flash` 4,6 s; evidence yang memuat teks injection memperpanjang penalaran. Token usage belum dikumpulkan sehingga penyebab belum terbukti.
- Normalisasi placeholder debrief tidak tercatat di journal; klaim yang benar-benar bernama "None" atau "Unknown" akan hilang. Grounding klaim memakai substring.
- Item usang lintas chunk debrief (d07) dan `due_text` "Monday" untuk usulan deadline (d05) masih ada.
- Satu run per kombinasi; run hybrid tidak direview semantik.
- Acceptance Meet dua sesi dan debrief dua record live dengan review owner belum dijalankan.
