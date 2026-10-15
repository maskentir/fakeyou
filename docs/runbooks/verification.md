# Verifikasi fondasi

Tanggal: 2026-09-27. Lingkungan: macOS ARM64, Python 3.12.13, uv 0.11.7. Status ini membedakan komponen yang diuji lokal dari integrasi provider/meeting live.

Dokumen ini adalah hasil historis fondasi. Hasil terbaru tersedia di [laporan refactor](../reports/2026-09-28-refactor.md), [verifikasi provider](../reports/2026-09-28-provider-verification.md), dan [pengujian guest runtime](../plans/meeting-runtime.md).

| Pemeriksaan | Hasil |
| --- | --- |
| Resolusi dan instalasi `uv.lock` | Berhasil; `uv pip check` menyatakan dependency kompatibel |
| `npm ci --ignore-scripts` | Berhasil dari lockfile |
| Ruff lint dan format | Lulus |
| Mypy strict | Lulus untuk 7 source files |
| Pytest default | 19 passed; browser dan live Muse dikecualikan sesuai marker |
| Browser smoke test terpisah | 1 passed; CloakBrowser + MCP asli, DOM, click, screenshot |
| DeepEval runner | 1 test passed; Tool Correctness 1.0 dengan argument matching |
| Build | Wheel dan source distribution berhasil |
| Docker Compose | Konfigurasi termasuk profile PostgreSQL valid |
| Neo4j 5.26.31 | Container healthy, hanya port loopback |
| Graphiti schema init | Berhasil; 33 indeks ONLINE |
| Graphiti startup chat | Konstruksi di worker, setup schema, koneksi Neo4j, dan cleanup lulus |
| Lifecycle Pipecat | Start, PCM, final transcript, shutdown, dan penolakan error upstream lulus secara lokal |
| Live Muse | 1 passed; audio sintetis Bahasa Indonesia dikirim real-time dan transkrip final cocok |
| Credential handling CLI | Chat tanpa key gagal sebelum membuka resource eksternal |

Schema Graphiti diuji dengan nilai key sementara yang tidak valid untuk layanan model; jalur inisialisasi yang diperiksa hanya menjalankan query Neo4j. Tidak ada panggilan model, embedding, atau reranker dalam pengujian schema ini.

DeepEval menggunakan respons model terprogram dan native LangGraph tool loop. Offline judge melempar error bila dipanggil. Skor tersebut memverifikasi kontrak tool dan argumen, bukan kualitas model live.

Tes native Pipecat memverifikasi konstruksi serta konfigurasi Muse dan TTS. Tes live mengirim audio sintetis ke Meta dan menerima transkrip final; TTS belum dipanggil. Pytest melaporkan deprecation warning dari dependency Graphiti/Pipecat; warning tidak disembunyikan.

Uji live Muse memiliki [runbook](live-muse-test.md) dan fixture sintetis Bahasa Indonesia sepanjang 9,076 detik, PCM mono 16-bit 24 kHz. Metadata Muse Code menunjukkan login OAuth, sementara credential bundle di macOS Keychain menyediakan `api_key` untuk Model API. Key dipakai hanya di memory saat pengujian dan disimpan lokal di `.env` dengan permission `600`; file tersebut di-ignore Git. Menurut [dokumentasi Meta](https://dev.meta.ai/help/subscriptions/subscribe-to-muse-code), credential onboarding Muse Code dan API key tambahan memiliki jalur penggunaan yang berbeda.

## Masalah integrasi yang ditemukan dan ditangani

- Adapter LangChain MCP 0.2.2 memakai FastMCP v1. Dependency MCP dibatasi `<2`, bukan mem-patch adapter.
- Schema Playwright MCP 0.0.82 menggunakan `browser_click.target`. Smoke test mengikuti schema versi terpasang.
- Graphiti Neo4jDriver membuat task indeks saat dibangun dalam event loop. Command `memory-init` membuat client sebelum loop; `chat --memory` membuat client lewat `asyncio.to_thread` dan menunggu setup schema sebelum memasang tools. Kedua jalur menutup koneksi saat selesai atau gagal.
- DeepEval harus dipanggil lewat `assert_test` agar runner menyimpan test case dan menampilkan metric; memanggil `measure` saja tidak cukup untuk report runner.

## Belum diuji

Guest join Google Meet, audio/video bridge, TTS live, reasoning model live, ingestion/retrieval Graphiti dengan model, turn-taking, interruption, serta avatar. PostgreSQL tersedia sebagai konfigurasi/dependency, belum sebagai backend CLI. CI sudah disiapkan tetapi belum dijalankan di GitHub.

Neo4j dibiarkan berjalan untuk pengembangan. `docker compose stop` menghentikannya tanpa menghapus named volume.

## Pemeriksaan supply chain dan container (2026-10-11)

Hasil pada branch `refactor/hardening`. Perintah yang sama dijalankan oleh `.github/workflows/check.yml`.

| Pemeriksaan | Perintah | Hasil |
| --- | --- | --- |
| Audit dependency Python | `uv export --frozen --no-hashes --all-groups --all-extras --no-emit-project`, lalu `uvx pip-audit==2.10.1 --no-deps --strict -r <berkas>` | 166 paket terkunci, 0 kerentanan |
| Lockfile npm | `npm ci --ignore-scripts` | Berhasil; 0 kerentanan |
| Audit npm produksi | `npm audit --omit=dev --audit-level=high` | 0 kerentanan |
| Pemindai secret pada riwayat git | `gitleaks git --log-opts="--all" --redact --exit-code 1` dengan gitleaks 8.30.1 | 3 temuan ditinjau dan tercatat di `.gitleaksignore`; 0 tersisa |
| Konfigurasi Compose | `docker compose --profile audio --profile embeddings config --quiet` | Lulus; tanpa `POSTGRES_PASSWORD` gagal dengan pesan `:?` |
| Image worker | `docker compose --profile audio build meeting-worker` | Lulus; tag `fakeyou-meeting-worker:local` |
| Tes container | `uv run pytest -m container -q -p no:cacheprovider` | 5 lulus |
| Browser headed dalam container | Peluncuran CloakBrowser di Xvfb dengan root filesystem read-only | Lulus; profil di tmpfs |
| Healthcheck Neo4j | `docker compose -p <proyek-uji> up -d --wait neo4j` | Healthy; `cypher-shell` membaca `NEO4J_USERNAME` dan `NEO4J_PASSWORD` dari env |

Catatan operator:

- Volume `postgres_data` yang sudah ada menerima password bawaan repo (`fakeyou-local-only`). Bila `POSTGRES_PASSWORD` di `.env` berbeda, autentikasi dari host gagal sampai salah satunya diubah. Jangan menghapus volume tanpa memastikan checkpoint tidak lagi diperlukan.
- Pemindaian gitleaks memakai `.gitleaksignore` dengan tiga fingerprint: dua baris pada `docs/refactor-plan.md` di riwayat yang sudah dihapus dari HEAD, dan satu kunci TLS self-signed untuk tes `tests/fixtures/preflight-localhost-key.pem`.
