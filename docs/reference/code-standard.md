# Standar Kode FakeYou

Dokumen ini mengikat semua perubahan kode. Setiap fase refactor memperbarui dokumen ini bila aturan bertambah. Pelanggaran standar diperbaiki pada fase yang memilikinya, bukan ditambal di tempat.

## Struktur dan layering

Kode domain tinggal di paket domain, bukan modul datar. Paket `cli` adalah adapter tipis: parsing argumen, pemanggilan satu service, lalu emit hasil. Aturan layering ditegakkan oleh `import-linter`:

- `catalog` tidak mengimpor `cli` atau `db`.
- `db` tidak mengimpor `cli`.
- Modul perintah di `cli` saling independen, kecuali reuse `diarize_run` oleh `memory-ingest` yang dihapus pada pemisahan handler-service (P6).
- Setelah P3, kontrak `layers` memetakan urutan paket domain. Kontrak `forbidden` melarang `catalog` dan `db` mengimpor `cli`. Paket lain dijaga oleh `layers` karena `cli` berada di lapisan paling atas.

## Error

Error yang sampai ke pengguna berupa `FakeYouError` dengan kode dari `ErrorCode`. Pesan dirender dari template katalog; daftar kode dan exit code ada di [katalog error](error-catalog.md).

- Tanpa `except` buta: tangkap exception spesifik yang memang bisa terjadi. Pengecualian hanya di modul observability (`cleanup`, `faults`, `journal`), yang diatur gate `BLE001`.
- Tanpa `suppress(Exception)` atau `suppress(BaseException)` di `src`; dijaga oleh `test_suppress_never_takes_a_broad_exception`. Penutupan best-effort (`aclose`, teardown) memakai `close_safely`; panggilan sink memakai `guard_observer` atau `guard_consumer`. Helper ini mencatat fault, sehingga kegagalan tetap terlihat. Hanya I/O sempit yang boleh di-`suppress`, misalnya `OSError` pada pipa yang sudah tertutup.
- Boundary `try` hanya di entrypoint CLI dan worker; boundary harus meneruskan `FakeYouError` tanpa mengubah kode.
- Pengecualian tercatat: `run_debrief` di `debrief/debrief_service.py` menangkap `Exception` di dalam lock rekaman, mencatat percobaan gagal (`debrief_attempt.json` dan journal `debrief_failed`), meneruskan error domain apa adanya, dan memetakan error non-domain ke `DEBRIEF_FAILED` dengan `from`. Alasan: catatan percobaan memuat hash record, konteks, dan koreksi yang hanya diketahui service di tengah proses dan harus ditulis sebelum lock dilepas. CLI tidak memiliki state itu; mengirimkannya lewat exception atau objek bersama membuat CLI ikut memegang logika percobaan. Memindah pemetaan ke CLI juga mengubah kode error kegagalan sebelum lock (lock rekaman dan pembukaan journal), karena CLI tidak bisa membedakannya tanpa mengetahui batas lock. Pengecualian ini hanya untuk `run_debrief`; boundary lain tetap di CLI dan worker.
- Tanpa silent swallow: tidak ada `except` yang diam-diam `pass`, `continue`, atau mengembalikan nilai kosong tanpa jejak observability. Detail error untuk pengguna hanya memakai nama tipe, bukan teks mentah exception; teks lengkap masuk journal.
- Kode katalog baru hanya untuk error yang bisa dialami pengguna; pelanggaran kontrak internal tetap builtin + traceback (contoh: transisi lifecycle invalid, guard `embed` yang sudah divalidasi di boundary).
- Rantai exception baru dengan `from`.
- Pelanggaran kontrak internal memakai builtin exception dengan pesan literal dan tampil sebagai traceback, bukan kode katalog.
- `ValueError` dari validator pydantic dan konverter argparse dipertahankan karena framework mensyaratkannya.

## Tipe

`mypy strict` wajib bersih untuk `src/fakeyou`, `tests`, dan `evals`. Library tanpa tipe diberi stub lokal di `typings/` (`cloakbrowser`, `sherpa_onnx`); `ignore_missing_imports` tidak dipakai. `Any` hanya di batas IO dengan alasan yang jelas; tanpa `cast` tanpa alasan. Protokol (`typing.Protocol`, runtime-checkable bila dipakai untuk `isinstance`) menggantikan pengecekan atribut dengan `getattr` berdasarkan nama.

## Bentuk fungsi dan API

- Kompleksitas siklomatis maksimal 10.
- Maksimal 5 parameter; kelompokkan argumen terkait ke `dataclass`. Pengecualian yang disadari: perintah typer (`cli/main.py`, bentuknya ditentukan framework) dan test double yang menerima knob opsional lewat `TypedDict` + `Unpack`. Knob test double dihitung sebagai satu bag kwargs; parameter wajib tetap positional.
- Flag boolean selalu keyword-only.
- Tanpa parameter yang tidak terpakai dan tanpa akses member private lintas modul. Tes memakai seam publik; pengecualian hanya untuk penamaan yang memang kontrak modul (misalnya konstanta query penyimpanan).

## Output

`stdout` memuat hasil saja dalam satu format JSON (`indent=2`, `ensure_ascii=False`). `stderr` memuat progres dan error. Semua output CLI lewat `cli/output.py`; tidak ada `print` di luar modul itu kecuali echo `Journal` yang memang ke `stderr`.

## Tes

- Satu modul tes per domain; tanpa file di atas sekitar 800 baris.
- Fake bersama tinggal di modul domain `tests/_<domain>_fakes.py`: `_audio_fakes` (audio, STT, playback), `_meet_browser_fakes` (DOM Meet dan halaman), `_memory_fakes` (Graphiti), `_conversation_fakes` (model, responder, percakapan), `_visual_fakes`, `_voiceprint_fakes`, `_persistence_fakes` (journal, rekaman), `_runtime_fakes` (sesi), dan `_fakes_common` untuk konstanta dan clock bersama. Fixture tetap di `conftest.py`. Tidak ada impor dari `tests.fakes` (modul itu sudah dihapus). Tes tidak mengimpor dari tes lain; modul dukungan boleh saling mengimpor tanpa siklus.
- `pytest.raises` untuk `FakeYouError` dan subclass yang membawa kode katalog (`LedgerError`, `DiarizeError`, `WorkerError`, dan lainnya) wajib `match`. Pola pesan diambil dari katalog lewat `tests/_error_support.message_pattern`, dan asersi `.code` tetap ada. Exception tanpa kode katalog (`MeetingAudioError`, `ProviderFault`, `ConversationError`) diuji lewat atribut atau `__cause__`.
- Helper tes bersama boleh berada di `tests/_*_support.py`. Dukungan untuk `evals` ada di `evals/_*.py`, termasuk `evals/_budgets.py` untuk ambang yang juga dipakai tes laporan.
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

Aturan lint yang aktif (`pyproject.toml`): `E`, `F`, `I`, `UP`, `B`, `ASYNC`, `T20`, `RUF021`, `FURB110`, `PLC0207`, `SIM114`, `FBT`, `ARG`, `SLF`, `C901`, `PLR0913`, `PT` (termasuk `PT011` untuk exception domain yang disebut di atas), `TRY002`, `TRY004`, `TRY201`, `TRY203`, `TRY300`, `TRY400`, `TRY401`, `S110`, `S112`, `RUF006`, dan `BLE001`. Pengecualian per-file hanya tersisa untuk `cli/main.py` (`PLR0913`), `cli/output.py` (`T201`), `observability/journal.py` (`T201` untuk sink stderr), dan modul observability `cleanup`, `faults`, dan `journal` (`BLE001`). Tes dan evals tidak punya pengecualian.

Aturan yang sengaja tidak diaktifkan: `TRY003`, `EM101`, `EM102`, `TRY301`, dan `SIM105` (bertentangan dengan aturan pesan literal atau mendorong `suppress(CancelledError)`). `S` selain `S110` dan `S112` tidak diaktifkan karena false positive pada nama enum dan pola yang sudah diputuskan (`assert` invarian, `sha1` fingerprint non-keamanan, `urlopen` dengan base URL konfigurasi).

Tidak ada impor nama privat lintas modul di tes setelah H6. Helper dukungan bersama memakai nama publik di `tests/_*_support.py`. Nama privat (berawalan `_`) tidak diimpor lintas modul. Modul dukungan `tests/_*.py` diimpor dengan nama publik di dalamnya. `getattr` dengan nama tidak dipakai untuk atribut yang bisa dinyatakan sebagai `Protocol`; sisanya hanya membaca data dinamis dari batas IO atau pustaka pihak ketiga (contoh: atribut error SDK, atribut respons HTTP, dan field dataclass di `tests/_runtime_fakes.py`).
