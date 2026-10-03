# Handoff Muse: brief, delegasi, dan debrief

Disusun 3 Oktober 2026. Baseline penulisan: master `21af795`, working tree bersih. Desain disetujui pengguna; eksekusi Muse belum dijalankan pada penulisan dokumen ini.

1. Baca [desain](meeting-delegation-design.md) penuh.
2. Eksekusi [brief meeting](meeting-brief.md) pada branch feat/meeting-brief.
3. Eksekusi [debrief berbukti](meeting-debrief.md) pada branch feat/meeting-debrief dari kandidat A yang diverifikasi.

## Prompt siap salin

```text
Kerjakan fitur brief meeting, batas delegasi, dan debrief berbukti di repo /Users/pushmove/Desktop/jemboed/fakeyou.

Baca penuh docs/plans/meeting-delegation-design.md, docs/plans/meeting-brief.md, docs/plans/meeting-debrief.md, dan docs/plans/meeting-delegation-handoff.md sebelum delegasi. Ikuti antarmuka, scope, batasan global, task dan acceptance pada dokumen itu; jangan memperluasnya menjadi chat/email, approval budget/kontrak, dashboard atau scheduler.

Baseline terakhir yang diperiksa master 21af795 sudah memuat trust/hardening/routing multipihak. Catat HEAD/status/ancestry dan pertahankan perubahan lokal termasuk plan ini. Jika HEAD berbeda, audit diff terlebih dahulu. Buat feat/meeting-brief dari kandidat benar; B memakai feat/meeting-debrief dari hasil A yang diverifikasi. Jangan reset, membuang perubahan/stash, atau push/merge.

Jadilah orkestrator swarm dengan maksimal tiga pekerja implementasi aktif. A: model/snapshot, prompt/delegasi, eval. B: source/validator, draft/renderer, eval. Bekukan schema/interface sebelum task paralel; satu integrator mengedit CLI/assembly/lock/report/fake bersama. Pekerja tidak commit. Reviewer spec lalu kode terpisah dari penulis; orkestrator menjalankan command verifikasi sendiri sebelum menerima hasil dan commit lokal.

Brief berlaku untuk satu owner/twin/meeting dan dibekukan sebelum model menerima transcript. Brief-only tidak boleh memperoleh persona facts/goals/recall; --memory yang bertentangan ditolak sebelum provider/browser. Permission dari fact, recall, nama, atau instruksi peserta tidak dapat memperluas tools. Tanpa izin keputusan baru, jangan approve budget/deadline/kontrak atau mengaku menghubungi owner. Bedakan pembatasan tool deterministik dari semantik ucapan yang harus diuji. Tidak ada LLM router/rewriter/structured output baru pada setiap jawaban live.

Debrief berjalan sebagai command operator terpisah, bukan setelah stop di join. Gunakan structured output native yang diprobe, source snapshot byte yang sama untuk hash/parse, reference dan quote asli tervalidasi, metadata speaker unknown tetap unknown. Kutipan valid belum menjamin ringkasan benar: review seluruh item secara independen. Pending owner decision adalah daftar lokal, bukan bukti owner dihubungi. Debrief tidak di-ingest otomatis menjadi fakta owner, tidak memutasi Graphiti, tidak mengirim pesan.

Jangan mengubah .env, backup, stash, persona privat, acc2/acc3 atau record acceptance lama. Tanpa dependency baru/lockfile, legacy alias, shim/re-export migrasi, komentar/docstring baru, atau xfail. Streaming tetap mati; semua gate lama dipertahankan. Jangan menghapus kasus gagal, membuang sampel lambat atau melonggarkan threshold.

Catat progres di .runtime/meeting-delegation/swarm-status.json: task A/B, agent/run ID, ownership/dependency, status/commit, command/exit code, artifact path, gate terbuka. Setelah tiap task/gate laporkan perubahan konkret. Setelah restart/compaction baca ledger, plan dan status repo sebelum lanjut. Klaim pekerja tes hijau bukan bukti tanpa hasil aktual.

Selesaikan offline/provider/container, skrip dan pertanyaan acceptance sebelum meminta link Meet baru/kesiapan host. Tidak memakai link lama atau menghubungi peserta asli. A: korpus brief 12x3 dan trust lama 24x3, review independen, dua sesi minimal 20 jawaban. B: delapan record sintetis dengan expected items/failure, lalu debrief dua record live A dan review owner. Pure B boleh maju sambil live A menunggu, gate live tetap terbuka tanpa sumber/host.

Pisahkan verdict brief/scope/tools, semantik delegasi, debrief/provenance, trust, stop/ingestion, fungsi live, dan performa. Atribusi speaker masih unknown dan latency masih gagal sebelum milestone ini; jangan menyebutnya selesai. Provider/host tanpa bukti bukan pass. Akhiri dengan file/commit, command aktual, artefak, risiko dan gate yang belum terbukti. Commit lokal hanya hasil yang direview; tanpa push/merge.
```

## Eksekusi dan pemantauan nanti

Periksa `muse exec --help`, salin blok prompt ke `.runtime/meeting-delegation/muse-prompt.txt`, lalu jalankan:

```sh
muse exec --workspace /Users/pushmove/Desktop/jemboed/fakeyou --json --prompt-file .runtime/meeting-delegation/muse-prompt.txt
```

Simpan stdout JSONL/session ID dan path session log yang nyata. Primitive swarm yang tidak tersedia harus dilaporkan; task sequential dengan review tetap dapat dijalankan, tanpa mengaku ada swarm. Jangan otomatis menonaktifkan sandbox/approval. Codex memeriksa ledger, diff ownership, hasil command dan artefak pada checkpoint; tidak menjalankan orkestrator kedua pada working tree yang sama.
