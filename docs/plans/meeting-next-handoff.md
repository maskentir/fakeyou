# Handoff Muse: hardening lalu partisipasi multipihak

Disusun 3 Oktober 2026. Dokumen ini mengoordinasikan dua milestone yang dapat direview terpisah; tidak menjalankan Muse ketika plan ditulis.

1. [Hardening meeting](meeting-hardening.md): cancellation model, sapaan netral, leave cepat, status ingestion, resume, dan acceptance.
2. [Partisipasi multipihak](multiparty-participation.md): level identitas, bukti speaker/source-clock, policy giliran, observasi read-only, dan acceptance multipihak.

Baseline yang diperiksa ialah branch `feat/trustworthy-memory` pada `8669845`. Jangan mengasumsikan trust sudah di `master`. Pertahankan plan dan pekerjaan lokal; kedua milestone membuat branch sendiri dari kandidat yang memuat prasyaratnya. Tidak ada push/merge dalam prompt ini.

## Prompt siap salin

```text
Kerjakan docs/plans/meeting-hardening.md, lalu docs/plans/multiparty-participation.md sesuai gate dan dependensinya. Baca kedua dokumen penuh serta docs/plans/meeting-next-handoff.md sebelum delegasi. Repo: /Users/pushmove/Desktop/jemboed/fakeyou.

Baseline terakhir yang diperiksa ialah feat/trustworthy-memory pada 8669845; jangan mulai dari master lama yang belum memuat trust. Catat HEAD/status/ancestry, pertahankan perubahan lokal, dan buat feat/meeting-hardening dari kandidat yang benar. Milestone B memakai feat/multiparty-participation dari hasil A yang diverifikasi. Jangan reset, membuang perubahan, menghapus stash, atau merge/push otomatis.

Jadilah orkestrator swarm dengan maksimal tiga pekerja implementasi aktif. Pada A: greeting, cancellation, dan ingestion boleh paralel. Pada B: resolver, policy, dan observer punya file masing-masing. Bekukan antarmuka dahulu; satu integrator memiliki file bersama. Pekerja tidak commit/push/merge. Review spec dan kode memakai reviewer terpisah, lalu orkestrator menjalankan tes sendiri sebelum menerima hasil.

A harus membuktikan pembatalan coroutine/request lokal ketika model belum mengeluarkan audio, tidak ada fallback/respons usang sesudah cancel, record tetap tersimpan, leave tidak menunggu ingestion Graphiti, dan memory-ingest dapat resume. Bedakan audio drop dari cancel model dan jangan mengklaim komputasi provider berhenti. Greeting-only ditangani netral melalui grammar terbatas dan middleware native; jangan menambah blacklist Sam atau LLM rewrite tiap jawaban.

B harus membedakan claimed name, participant observed, dan configured owner. Roster/tile saat final STT tiba bukan bukti speaker ucapan sebelumnya. Jalankan gate source-clock/audio alignment. Jika binding belum terbukti, pertahankan unknown, lanjutkan policy/observasi yang independen, dan laporkan batasnya; jangan mem-port binder Vexa dengan channel palsu, memakai private SDK, atau mengganti capture dalam scope ini.

Jangan mengubah .env, backup, stash, persona privat, acc2/acc3, atau record acceptance lama. Tanpa dependency, shim legacy, alias konfigurasi, re-export migrasi, komentar/docstring baru, atau xfail. Streaming tetap mati. Semua gate lama tetap; jangan menghapus kasus gagal/sampel lambat atau melonggarkan threshold.

Catat progres di .runtime/meeting-next/swarm-status.json: task A/B, agent/run ID, ownership, dependency, status, commit, command/exit code, artifact path, dan gate terbuka. Setelah tiap task/gate, laporkan hasil konkret agar Codex dapat memantau. Setelah restart/compaction, baca ledger, plan, dan status repo sebelum melanjutkan.

Selesaikan offline/provider/container dan skrip uji sebelum meminta link Meet baru serta kesiapan host. Jangan memakai link lama atau menghubungi peserta asli. Pilot B menunggu gate stop dan integritas ingestion A lulus; pekerjaan pure/read-only B boleh maju sambil gate live A menunggu. Gunakan namespace uji baru dan ground truth host untuk speaker/target pertanyaan.

Jalankan corpus trust 24x3 dan review independen. Pada A buktikan dua sesi dan minimal 20 jawaban; pada B dua manusia, minimal 60 utterance dan 20 jawaban. Pisahkan verdict hardening, ingestion, trust, media alignment, atribusi, policy, fungsi live, dan performa. Gate tanpa bukti tetap terbuka; performa gagal tidak disamarkan oleh keberhasilan feature lain.

Commit lokal hanya perubahan yang direview. Tanpa push/merge. Akhiri dengan file/commit, hasil command aktual, artefak, risiko, serta gate yang belum terbukti. Jangan menyatakan selesai penuh atau siap unattended jika acceptance wajib belum lulus.
```

## Menjalankan dan memantau nanti

Muse CLI terpasang menyediakan `exec --json --prompt-file`, `--session-id`, dan `trace inspect`. Saat eksekusi, periksa help kembali, salin blok prompt ke `.runtime/meeting-next/muse-prompt.txt`, lalu jalankan dari repo:

```sh
muse exec --workspace /Users/pushmove/Desktop/jemboed/fakeyou --json --prompt-file .runtime/meeting-next/muse-prompt.txt
```

Simpan stdout JSONL dan session ID yang nyata. Stdout event bukan otomatis session log trace; ambil path session log dari run sebenarnya. Gunakan `muse trace inspect --session-log PATH --all-runs --render-mode compact` atau `--causal` bila tersedia untuk melihat hubungan run pekerja.

Jangan otomatis memakai flag yang menonaktifkan sandbox/approval. Jika primitive swarm tidak tersedia, laporkan dan jalankan task sequential dengan review, tanpa mengarang bahwa ada agent paralel.

Codex memeriksa checkpoint saat task/gate berubah: ledger, diff file ownership, command/exit code, artefak, dan bukti review. Klaim worker “tes hijau” bukan pengganti verifikasi. Hindari polling agresif atau menjalankan beberapa orkestrator pada working tree yang sama.

## Keputusan yang tidak diubah oleh handoff

- Laporan trust live sebelumnya tetap gagal menurut kontrak waktu itu; perubahan rubrik identitas/greeting dicatat pada evaluasi baru.
- Performa tetap pekerjaan terpisah: agent p95 ≤4 detik berbeda dari akhir ucapan→PCM p95 ≤6 detik. Streaming telah dihentikan; tidak diulang tanpa hipotesis baru.
- Fuzzy “old shield” diuji dengan ucapan sungguhan dan klarifikasi yang sesuai, bukan dipaksa dengan prompt churn atau recall tiap giliran.
- Tidak ada kewenangan owner dari nama atau roster; komitmen dan aksi sensitif tetap membutuhkan desain delegasi tersendiri.
