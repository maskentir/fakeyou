# Audit sisa kompatibilitas setelah refactor

Tanggal: 2 Oktober 2026. Basis pemeriksaan: HEAD `29e144c` di `feat/conversation-performance`, termasuk eksperimen streaming kalimat yang baru masuk. Perubahan penghapusan peta migrasi dan kajian latency yang masih belum di-commit dipertahankan.

## Temuan dan perubahan

| Temuan | Perubahan |
| --- | --- |
| `LEGACY_KEYS` dan modul `config_migration.py` menyimpan seluruh daftar konfigurasi lama | Modul, import, cabang error khusus, tes pemetaan, dan tabel migrasi README dihapus. Key di luar skema aktif mengikuti `Unknown configuration key` |
| Enam konstanta audio diekspor ulang dari `audio.py` | Pemanggil produksi dan tes mengimpor langsung dari `invariants.py`; tiga import yang ternyata hanya untuk ekspor ulang ikut dihapus |
| `SessionId` dan `TwinName` diekspor ulang dari `session.py` | Pemanggil memakai `invariants.py`; import internal tidak lagi mendeklarasikan re-export |
| `ConversationError` diekspor ulang dari `conversation.py` | Tes dan pemanggil memakai pemilik tipe, `session.py` |
| Alias CLI `--owner` masih diterima pada lima command | Dihapus; hanya `--owner-id` diterima. Singkatan otomatis argparse dinonaktifkan pada seluruh parser agar alias yang dihapus tidak hidup lagi sebagai prefix |
| CLI membaca `getattr(result, "faults", None)` walaupun kontrak `SessionResult` mempunyai field itu | Mengakses `result.faults` langsung. Fake umum dan enam fake hasil sesi di tes memakai `SessionResult` asli |
| Preflight menebak `https://` untuk input tanpa skema | Penebakan di parser endpoint dan sanitasi label dihapus; endpoint wajib mempunyai skema eksplisit |
| Kategori fault mencoba `error_category` dan enum `value` bila atribut utama tidak ada | Nama alternatif dihapus; pembacaan mengikuti `category.name` yang dipakai Pipecat 1.12.0 |
| Validasi nama episode mengasumsikan semua input salah berasal dari format lama | Error menjelaskan kontrak aktif `<meeting_id>:w<index>`; validasi tetap menolak data yang tidak memenuhi pola itu |
| Tes kontrak SDK masih memakai prefix provider dalam nama model dan import kategori melalui modul frame | Nama model dan `model_provider` kini terpisah; `ErrorCategory` diimpor langsung dari `pipecat.utils.errors` |

Konsekuensi yang terlihat: command harus memakai nama flag lengkap, terutama `--owner-id`; preflight tidak menerima hostname tanpa skema. Contoh aktif di README dan rencana runtime sudah diperbarui. Tidak ada penggantian atau migrasi otomatis nilai konfigurasi.

## Cakupan audit

- Memeriksa 36 modul Python produksi, import dan pemanggil dalam `tests`/`evals`, parser CLI, pembacaan environment, Compose, Docker, serta dokumentasi aktif.
- Mencari re-export, alias field/env, jalur kompatibilitas, nama atribut alternatif, akses opsional pada kontrak wajib, dukungan versi lama, dan kandidat kode mati.
- Scan AST sesudah perubahan menemukan nol re-export dengan `import Nama as Nama`, nol import melalui sembilan jembatan lama, serta nol fungsi/kelas tingkat modul tanpa referensi statis dalam source produksi.
- Metode yang tidak terlihat pada pencarian pemanggil statis diperiksa sebagai callback LangChain, validator Pydantic, hook sumber settings, dan override reconnect Pipecat. Semuanya mempunyai pemanggil melalui framework.
- Scan statis bukan bukti bahwa setiap cabang dapat dijangkau pada semua kondisi runtime; klaim audit dibatasi pada kode dan kontrak yang diperiksa.

## Hasil yang tetap mempunyai fungsi aktif

- `openai_compatible` adalah pilihan protokol provider aktif untuk MiniMax/Muse, bukan alias konfigurasi lama. Provider tetap dinyatakan eksplisit.
- `reply_fallback` merupakan provider cadangan yang dikonfigurasi sendiri, dengan key dan budget sendiri. Tidak ada peminjaman key lintas peran atau inferensi provider.
- Jalur respons utuh dan jalur streaming kalimat masih diperlukan oleh eksperimen dengan flag bawaan mati; milestone performa belum lulus. Menghapus baseline akan menghilangkan pembanding eksperimen.
- Guard reconnect menolak STT yang tidak mempunyai reconnect sesuai kebijakan; tidak beralih ke metode reconnect versi lama. Nama fake `LegacySTT` di tes adalah input negatif untuk penolakan, bukan adapter produksi.
- Tes exception `httpx` lama membuktikan exception di luar kontrak tidak mendapat klasifikasi khusus; produksi memakai `httpx2`. Tes itu tidak menambah dukungan library lama.
- `PULSE_SOURCE`, `PULSE_SINK`, prefix `FAKEYOU_COMPOSE_*`, dan `EMBEDDING_DIM` adalah kontrak audio, Compose, dan Graphiti yang masih aktif. Default domain seperti persona dan sinyal Meet juga tetap dipakai.
- Penutupan iterator yang mempunyai `aclose()` dan penghentian backend audio opsional adalah pengelolaan resource pada kontrak async/transport saat ini.
- Peringatan deprecation dependency saat import tetap berasal dari library terpasang; menghapus kode aplikasi tidak memperbarui library itu.

## Verifikasi

Tes penolakan alias pada lima command dan endpoint tanpa skema gagal pada implementasi sebelum cleanup, lalu lulus setelah perubahan. Penggantian akses hasil sesi juga mengungkap fake yang tidak memenuhi kontrak; fake diperbaiki pada sumbernya.

Perintah regresi:

```sh
uv run --no-sync pytest -m 'not live and not container' -q --tb=short
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
git diff --check
```

Verifikasi ulang sebelum commit, di atas HEAD terbaru `a8b03c1`: 1.562 tes lulus, 18 dilewati, 20 tidak dipilih, dengan tiga peringatan dependency, dalam 67,88 detik. Ruff bersih, 118 berkas memenuhi format, dan mypy memeriksa 38 berkas tanpa masalah. Fake audio yang diperbarui juga mencatat `transcript_id` di `speak()`, bukan mengakses variabel yang tidak tersedia di `stop_playback()`.

Pada verifikasi cleanup sebelumnya, `.env` aktif berhasil divalidasi tanpa mencetak nilainya. `uv build` berhasil; wheel tidak membawa modul `config_migration` yang dihapus. `git diff --check` bersih.

Tidak menjalankan provider berbayar, container, ingestion, atau Meet baru. Tidak mengubah `.env`, persona, data memori, stash, atau backup. Branch tujuan commit dan push: `feat/conversation-performance`.
