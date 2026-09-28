# Rencana setup fondasi

Tujuan: dependency reproducible dan composition library yang bisa dijalankan serta diuji, dengan batas integrasi live yang jelas.

Spesifikasi: [architecture.md](architecture.md).

- [x] Kunci dependency Python 3.12 dan server Playwright MCP resmi; siapkan environment dan Compose untuk Neo4j serta PostgreSQL opsional.
- [x] Susun factory agent LangChain/LangGraph dan Graphiti memakai API resmi; uji checkpoint dan pemisahan namespace memori.
- [x] Susun layanan Muse dan TTS melalui Pipecat; periksa dukungan pada versi release yang terpasang.
- [x] Hubungkan CloakBrowser ke Playwright MCP dalam sesi persisten; uji DOM, klik dan screenshot pada halaman lokal.
- [x] Tambahkan CLI pemeriksaan setup, inisialisasi Graphiti, dan percakapan berbasis checkpoint; dokumentasikan integrasi.
- [x] Jalankan Ruff, mypy, pytest, DeepEval, build package, dan validasi Compose; catat batas pengujian eksternal.

Hasil: [verification.md](verification.md). Neo4j berjalan dan indeks sudah dibuat; PostgreSQL baru divalidasi konfigurasinya. Partisipasi meeting dan media bridge berada di luar scope fondasi ini.

Source tetap tanpa komentar/docstring. Konfigurasi dan wrapper sederhana cukup diperiksa lewat tooling atau smoke test; tes perilaku difokuskan pada isolasi state, pengambilan keputusan, serta cleanup resource.
