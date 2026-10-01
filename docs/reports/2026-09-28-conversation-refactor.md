# Refactor conversation — 28 September 2026

Refactor ini mengganti pemanggilan graph yang tumpang tindih dengan satu consumer serial dan mempertahankan transkrip awal sampai meeting siap. LangGraph native tetap memegang riwayat pesan, Pipecat tetap memegang audio, dan adapter hanya menghubungkan kontrak keduanya.

## Perubahan perilaku

- Event transkrip bertipe dipisahkan dari objek frame native. Payload identik yang terkirim memakai id yang sama; ucapan yang diulang pada timestamp lain tetap menjadi ucapan baru.
- Setiap thread ditulis oleh satu invocation graph. Transkrip final yang tertunda diproses bersama, dengan urutannya dipertahankan. Jawaban usang ditahan sebelum dikirim ke TTS.
- Owner, twin, dan meeting menentukan checkpoint percakapan. Id pesan dan metadata native menjaga provenance transkrip.
- Ekstraksi teks assistant native menggantikan parsing content block kustom. Callback tool native menggantikan pemindaian seluruh riwayat untuk mencari tool call lama.
- Startup percakapan menunggu kesiapan mikrofon. Stop membatalkan sapaan yang terblokir dan mencegah percakapan melakukan bind sesudahnya.
- Semua jalur keluar runtime menutup loop sebelum dependency audio/client-nya. Kegagalan percakapan memiliki terminal reason tersendiri dan exit code CLI bukan nol.
- `--converse` mengaktifkan respons secara eksplisit; smoke test khusus audio yang sudah ada tetap tersedia.

## Verifikasi

- 244 tes default lulus; 12 tes opt-in deselected.
- 6 tes CloakBrowser asli lulus, termasuk lifecycle guest dan kontrol mikrofon pada fixture lokal.
- Image tes audio Linux berhasil dibangun; seluruh 64 tes conversation, runtime-audio, dan CLI lulus di dalamnya. Tes dan fixture konfigurasi repository di-mount read-only.
- Evaluasi tool-correctness DeepEval deterministik yang sudah ada lulus; ini bukan skor kualitas persona live.
- Ruff, format, mypy strict pada 13 file source, build wheel/sdist, dan validasi Compose lulus.
- Review baca-saja yang terpisah tidak menemukan cacat penting yang dapat ditindaklanjuti pada kode hasil refactor.

Pemeriksaan ini memakai fixture lokal dan checkpoint LangGraph native. Pemeriksaan ini tidak membuktikan bahwa participant telah mendengar jawaban hasil model di Meet live. Bukti audio live Tahap 2 yang lebih awal tetap berlaku dan terpisah.

## Keterbatasan yang tersisa

Generation guard menahan jawaban usang; ia tidak me-rollback tool dan tidak menarik kembali ucapan yang sudah masuk antrean. Deadline model membatalkan graph dan mengakhiri sesi ini; operasi eksternal tetap memerlukan idempotensi sendiri. Dedupe hanya berlaku di dalam satu loop, dan Muse tidak mengekspos id ucapan yang stabil melalui adapter terpasang. Turn detection native, interupsi, pelacakan delivery, pemetaan participant, dan observasi visual berkelanjutan tetap menjadi pekerjaan mendatang.

Riwayat graph memuat jawaban yang dihasilkan, termasuk draf yang ditahan sebelum diucapkan. Ini adalah riwayat penalaran, bukan ledger delivery; pekerjaan partisipasi berikutnya harus membedakan output yang dihasilkan, masuk antrean, diputar, dan diinterupsi.

Tugas berikutnya adalah [persona tervalidasi dan akses memori ber-scope](../plans/persona-memory-access.md), dimulai dari gate acceptance percakapan yang masih terbuka.
