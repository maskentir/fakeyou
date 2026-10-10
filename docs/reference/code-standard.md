# Standar Kode FakeYou

Dokumen ini mengikat semua perubahan kode. Setiap fase refactor memperbarui dokumen ini bila aturan bertambah. Pelanggaran standar diperbaiki pada fase yang memilikinya, bukan ditambal di tempat.

## Struktur dan layering

Kode domain tinggal di paket domain, bukan modul datar. Paket `cli` adalah adapter tipis: parsing argumen, pemanggilan satu service, lalu emit hasil. Aturan layering ditegakkan oleh `import-linter`:

- `catalog` tidak mengimpor `cli` atau `db`.
- `db` tidak mengimpor `cli`.
- Modul perintah di `cli` saling independen, kecuali reuse `diarize_run` oleh `memory-ingest` yang dihapus pada pemisahan handler-service (P6).
- Setelah P3, kontrak `layers` memetakan urutan paket domain dan kontrak `forbidden` melarang seluruh kode library mengimpor `cli`.

## Error

Error yang sampai ke pengguna berupa `FakeYouError` dengan kode dari `ErrorCode`. Pesan dirender dari template katalog; daftar kode dan exit code ada di [katalog error](error-catalog.md).

- Tanpa `except` buta: tangkap exception spesifik yang memang bisa terjadi. Pengecualian hanya untuk tiga kasus, masing-masing wajib meninggalkan jejak observability (fault record, counter, baris journal, atau hasil terminal): sink observability (`Journal`, `FaultReporter`) yang tidak boleh raise, path teardown/close best-effort, dan panggilan eksternal yang terkungkung (browser/playwright, LLM, driver database) yang tipe exception-nya tidak bisa dispesifikkan secara jujur.
- Boundary `try` hanya di entrypoint CLI dan worker; boundary harus meneruskan `FakeYouError` tanpa mengubah kode.
- Tanpa silent swallow: tidak ada `except` yang diam-diam `pass`, `continue`, atau mengembalikan nilai kosong tanpa jejak observability. Detail error untuk pengguna hanya memakai nama tipe, bukan teks mentah exception; teks lengkap masuk journal.
- Kode katalog baru hanya untuk error yang bisa dialami pengguna; pelanggaran kontrak internal tetap builtin + traceback (contoh: transisi lifecycle invalid, guard `embed` yang sudah divalidasi di boundary).
- Rantai exception baru dengan `from`.
- Pelanggaran kontrak internal memakai builtin exception dengan pesan literal dan tampil sebagai traceback, bukan kode katalog.
- `ValueError` dari validator pydantic dan konverter argparse dipertahankan karena framework mensyaratkannya.

## Tipe

`mypy strict` wajib bersih untuk `src/fakeyou`, `tests/fakes.py`, dan `tests/conftest.py`. Target akhir mencakup `tests` dan `evals`. `Any` hanya di batas IO dengan alasan yang jelas; tanpa `cast` tanpa alasan.

## Bentuk fungsi dan API

- Kompleksitas siklomatis maksimal 10.
- Maksimal 5 parameter; kelompokkan argumen terkait ke `dataclass`.
- Flag boolean selalu keyword-only.
- Tanpa parameter yang tidak terpakai dan tanpa akses member private lintas modul.

## Output

`stdout` memuat hasil saja dalam satu format JSON (`indent=2`, `ensure_ascii=False`). `stderr` memuat progres dan error. Semua output CLI lewat `cli/output.py`; tidak ada `print` di luar modul itu kecuali echo `Journal` yang memang ke `stderr`.

## Tes

- Satu modul tes per domain; tanpa file di atas sekitar 800 baris.
- Fake dan fixture bersama tinggal di `tests/fakes.py` dan `conftest.py`; tanpa impor antar-modul tes.
- `pytest.raises` selalu menyebut kode error atau `match`.
- Tes baru yang memanggil `main()` wajib snapshot dan restore `os.environ` karena export dotenv bocor antar tes dalam satu proses.

## Penamaan

Tanpa nama `legacy` atau `compat`; nama menggambarkan perilaku domain. Nama anggota enum mengikuti nilainya, misalnya `config.unknown_key` menjadi `CONFIG_UNKNOWN_KEY`.

## Dependensi

Dependensi baru wajib mencatat manfaatnya. `deptry` bersih: setiap paket yang diimpor langsung dideklarasikan langsung. Versi terkunci di `uv.lock`.

## Dokumentasi

Setiap fase memperbarui `README`, runbook, dan referensi agar cocok dengan kode. Tanpa nama basi di luar `docs/plans` dan `docs/reports` yang memang historis. Dokumen referensi berbahasa Indonesia.

## Gerbang

| Perintah | Tujuan |
|---|---|
| `uv run ruff check .` | lint, termasuk aturan baru per fase di bawah |
| `uv run ruff format --check .` | format |
| `uv run mypy` | type check strict |
| `uv run lint-imports` | kontrak layering |
| `uv run deptry .` | hygiene dependensi |
| `uv run pytest` | suite penuh tanpa database |
| `TEST_DATABASE_URL=... uv run pytest` | suite penuh dengan PostgreSQL |
| `docker compose --profile audio build meeting-worker` + `pytest -m container` | image dan tes container |

Perluasan seleksi ruff mengikuti fase: P5 mengaktifkan `C90`, `PLR`, `ARG`, `FBT`, dan `SLF`; P6 mengaktifkan sisa `TRY` dan `EM` untuk kode baru. Aturan yang sudah aktif hari ini adalah `E`, `F`, `I`, `UP`, `B`, `ASYNC`, `T20`, `RUF021`, `FURB110`, `PLC0207`, dan `SIM114`. `BLE` dan `S` dievaluasi pada P4 dan sengaja tidak diaktifkan: `BLE` menandai 82 situs yang setelah triase semuanya broad secara sah (ketiga kasus di atas) sehingga gate-nya hanya menambah puluhan `noqa`; `S` memberi false positive pada nama enum dan menandai pola yang sudah diputuskan (`assert` invarian, `sha1` fingerprint non-keamanan, `urlopen` dengan base URL konfigurasi).
