# Milestone 4 — Tindakan Meet (Raise/Lower Hand + Chat)

## Tujuan

Milestone 4 menambahkan tindakan tulis twin di dalam rapat Google Meet: angkat tangan (`raise_hand`), turunkan tangan (`lower_hand`), dan kirim chat (`send_chat`).

Penerimaan milestone mengikuti `docs/plans/next-development.md:299`:

1. Tindakan berhasil secara teramati — klaim sukses wajib didukung bukti DOM yang dibaca kembali (pergantian label, ikon tile, gema chat), bukan sekadar klik terkirim.
2. Tindakan ditolak saat otoritas persona tidak cukup — alat tidak diikat, model memberi penolakan singkat netral tanpa menyebut internal, dan penolakan dicatat di jurnal.
3. Tindakan tidak pernah digandakan saat retry atau restart — niat dicatat sebelum klik, amati-sebelum-bertindak, dan rekonsiliasi saat `--resume`.

Ruang lingkup ini tidak mencakup mikrofon, kamera, presentasi, perekaman, atau keluar rapat di luar verifikasi pola yang sudah ada.

## Batasan Global

Semua tugas dalam berkas ini wajib mematuhi batasan berikut:

1. Bahasa kode Python berbahasa Inggris, tanpa komentar dan tanpa docstring. Bahasa dokumentasi berbahasa Indonesia.
2. DILARANG memakai API Meet/Media privat apa pun. Otomasi UI hanya lewat perkakas peramban yang sudah ada di repo: `src/fakeyou/browser.py` (`launch_persistent_context_async`, `_prepared_browser_context`, `open_direct_page`), `src/fakeyou/meet.py` (`_click_when_ready`, `_toggle_control`, `observe_call_state`), dan pola `page.evaluate` dari `src/fakeyou/participant_observer.py` serta `src/fakeyou/attribution_probe.py`. Jalur MCP (`open_browser_tools`) hanya untuk observasi, tidak untuk aksi tulis.
3. Dilarang menambah dependensi baru. Tetap pada `cloakbrowser==0.5.11` (`pyproject.toml:8`) dan `@playwright/mcp 0.0.82` (`package.json:12`).
4. Dilarang push atau merge. Kerja di cabang `feat/meet-actions` saja.
5. Setiap tugas direview oleh reviewer terpisah. Satu tugas, satu reviewer berbeda.
6. Tes merah dulu untuk setiap tugas. Tulis tes gagal sebelum implementasi, buktikan gagal, lalu implementasikan sampai hijau.
7. Dilarang menyebut nama Vexa, LMA, atau nama kompetitor lain di dalam kode `fakeyou`.
8. Catat kemajuan SDD di `.superpowers/sdd/meet-actions/progress.md` setiap selesai satu tugas.
9. Selektor humanized hanya pola yang didukung: CSS, `xpath=`, `text=`, `/test-id/`, `placeholder`, `alt`, `title`, `text`, `label`, plus `first/nth/last`. Dilarang `get_by_role`, chain, dan frame. Selalu tunggu-visible sebelum klik. Setiap klik wajib retry resolusi-ulang 3 upaya dengan locator segar dan timeout pendek.
10. Jurnal tidak boleh memuat isi chat. Hanya metadata: `chars`, `action`, `action_id`, `attempt`, `attempts`, `duration_ms`, `observed`, `observed_state`, `reason`. Lapangan `text`, `prompt`, `transcript`, `completion`, `body` dilarang oleh `tests/test_taxonomy.py:84`.
11. Anggaran latensi yang sudah ada tidak boleh dilonggarkan: `p95 agent <= 4 detik`, live `p50 <= 3 detik` dan `p95 <= 6 detik`, `stop->senyap <= 1,5 detik`, `stop->leave <= 5 detik`. Tindakan tulis wajib async dan tidak menambah latensi jalur jawab.
12. Tanpa timer spontan atau proaktivitas yang menerobos lantai bicara. Cek generation dan status tertutup sebelum tiap eksekusi, dan batalkan sisa usang.

## Tugas 0 — Probe Gate DOM Meet Langsung

### Tujuan

Mengamati dan mengunci pemilih DOM Meet asli untuk tombol tangan, status tangan milik sendiri, panel chat, kolom input chat, dan gema pesan terkirim, sebelum implementasi apa pun. Tugas ini adalah gerbang: Tugas 1, 4, dan 5 tidak boleh mulai sebelum gerbang ini lulus.

### Berkas dan Fungsi Tepat

- Baca: `src/fakeyou/meet.py:60-69` (`MeetSignals`), `src/fakeyou/meet.py:103-115` (`with_pinned_meet_locale`), `src/fakeyou/meet.py:355-370` (pasangan prefiks `aria-label` mic/kamera), `src/fakeyou/browser.py:27-35,213` (pin `locale="en-US"`), `src/fakeyou/participant_observer.py:45-99` (`OBSERVE_JS`), `src/fakeyou/watchdog.py:247-268` (`read_participant_count`), `src/fakeyou/watchdog.py:31-35` (xpath tombol dialog).
- Tulis: `docs/reports/2026-10-09-meet-actions-probe.md` (hasil probe), ditambah skrip probe sekali-pakai di `/tmp` (bukan di repo).
- Jangan ubah `src/fakeyou/meet.py` pada tugas ini.

### Tes Merah Dulu

Tidak ada tes unit baru. Gerbang ini memakai probe manual gaya gate media (`docs/reports/2026-10-03-media-gate.md:36-52`) dengan kriteria lulus tertulis di laporan probe:

1. Probe memindai seluruh atribut `aria-label` halaman dan menyimpan cuplikan sebelum dan sesudah tiap aksi.
2. Setiap pemilih yang diklaim lulus wajib lolos dua kali pemindaian berurutan pada sesi yang sama.

### Ambang Penerimaan

1. Laporan probe mencatat jawaban pasti untuk lima hal tak terjawab Scope 3: atribut aria tombol tangan (`aria-label` Raise/Lower hand dan ada-tidaknya `aria-pressed`), mekanisme observasi status tangan sendiri (tukar label lawan `aria-pressed` lawan ikon tile lawan toast), pemicu kirim chat (Enter lawan tombol kirim) plus struktur DOM gema pesan sendiri, pemilih deteksi panel chat terbuka-tertutup, dan pintasan non-macOS bila ada.
2. Setiap pemilih memakai pencocokan prefiks dengan flag `i`, tidak pernah sama-persis, karena akhiran belum-dibaca (`Chat with everyone - New message`) dan status riwayat membalik label input.
3. Kolom input chat dicocokkan lewat peran atau struktur (`textarea[maxlength="4000"]` atau `[contenteditable][role="textbox"]`), tidak lewat `aria-label`.
4. Semua klik humanized memakai `xpath=` (pelajaran `docs/reports/2026-09-30-acceptance.md:184`).
5. Probe mematuhi throttle identitas: maksimal sekitar 5 join dan 15 load per jam, dengan jeda antar join karena kegagalan join transien sekitar 10 detik pernah teramati.
6. Fondasi yang ditolak tetap ditolak: tidak memakai swap class ter-obfuscate (`sxlEM` lawan `h8UR3d`), tidak memakai `[data-self="true"]`, tidak mengandalkan penanda glow DOM (laporan menyebut 0 dari 36).

### Perintah Verifikasi

```sh
FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false .venv/bin/pytest tests/test_meet.py -q -p no:cacheprovider
.venv/bin/ruff check src tests evals
.venv/bin/ruff format --check src tests evals
.venv/bin/mypy src
```

Tugas ini lulus hanya bila laporan probe ada dan keempat perintah di atas hijau.

### Di Luar Lingkup

- Dilarang mengubah kode `src/`.
- Dilarang menambah `MeetSignals` atau modul aksi.
- Dilarang memakai hasil cuplikan pencarian web sebagai pengganti pengamatan langsung.

## Tugas 1 — Modul Aksi Peramban `meet_actions.py`

### Tujuan

Membangun operasi raise hand, lower hand, dan kirim chat di atas halaman humanized langsung, dengan pola klik-ulang dan verifikasi status yang sama seperti mic/kamera dan leave.

### Berkas dan Fungsi Tepat

- Baru: `src/fakeyou/meet_actions.py` berisi `MeetActionSignals` (string label tangan dan chat, injectable seperti `MeetSignals` di `src/fakeyou/meet.py:60-72`), dataclass hasil (`succeeded`, `refused`, `failed` plus `detail`), dan fungsi `raise_hand(page, signals, sleep, monotonic)`, `lower_hand(page, signals, sleep, monotonic)`, `send_chat(page, signals, sleep, monotonic)`.
- Pakai ulang: `src/fakeyou/meet.py:173-194` (`_click_when_ready`: locator segar tiap upaya, tunggu visible 2 detik, klik 5 detik, jeda 1 detik, 3 upaya), `src/fakeyou/meet.py:373-393` (`_toggle_control`: cek status diharap terlihat lalu no-op, klik, tunggu status diharap 10 detik atau `RuntimeError`), `src/fakeyou/meet.py:273-334,337-352` (`request_guest_join`, `observe_call_state` untuk gate belum-diakui), `src/fakeyou/meet.py:396-430` (`leave_meeting`: pola hitung tombol lalu verifikasi penanda hilang).
- Panggil dari worker lewat `src/fakeyou/runtime.py:155-164` (`_until_stopped`) dan `src/fakeyou/runtime.py:715-736` (jalur buka satu `Page` via `open_direct_page`). Siklus hidup konteks tetap milik `src/fakeyou/browser.py:186-234` (`_prepared_browser_context`).
- Tes: baru `tests/test_meet_actions.py` memakai `tests/fakes.py:42-71,86-120` (`FakePage`, `FakeLocator`: hitungan, tunggu, klik flaky). Satu tes live bertanda `@pytest.mark.browser` (default pytest mengecualikan mark ini menurut `pyproject.toml:61`).

### Tes Merah Dulu

Tulis dahulu di `tests/test_meet_actions.py` sebelum implementasi:

1. `raise_hand` no-op saat pemilih status diharap sudah terlihat (tidak ada klik).
2. `raise_hand` klik lalu sukses saat status diharap muncul dalam 10 detik.
3. `raise_hand` gagal dengan `RuntimeError` saat status tak kunjung muncul.
4. `_click_when_ready` dicoba ulang maksimal 3 kali dengan locator segar saat klik flaky.
5. `send_chat` membuka panel saat input belum terlihat, mengetik, lalu sukses hanya bila gema pesan sendiri muncul dalam timeout; tanpa gema berarti gagal dan tidak kirim ulang membabi-buta.
6. Semua aksi gagal cepat dengan detail `observe_call_state` saat belum diakui ke rapat.

Buktikan keenam tes gagal, lalu implementasikan.

### Ambang Penerimaan

1. Semua klik humanized memakai `xpath=` dan selalu diawali tunggu-visible.
2. Batas upaya: 3 kali untuk klik umum, 10 kali hanya untuk kolom lambat mengikuti `src/fakeyou/meet.py:54`; verifikasi status akhir maksimal 10 detik.
3. Observasi-sebelum-bertindak: tidak ada klik kedua saat status sudah sesuai (syarat tidak-ganda saat retry).
4. Lokalisasi dipin `?hl=en` lewat `with_pinned_meet_locale`; pencocokan prefiks case-insensitive.
5. Kedipan render di bawah 3 detik ditoleransi sebagai flicker, bukan kegagalan.
6. Tidak ada enumerasi `.pages`, tidak ada pencarian tab, tidak ada `bring_to_front` — worker hanya punya satu `Page` (`browser.py:100,256` adalah dua-satunya `new_page()`).
7. Tidak ada jalur MCP untuk aksi; `open_browser_tools` (`browser.py:294-335`) tetap hanya dipakai CLI chat dan diagnostik (`cli/chat.py:44-53`, `cli/diagnostics.py:22-29`).

### Perintah Verifikasi

```sh
FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false .venv/bin/pytest tests/test_meet_actions.py -q -p no:cacheprovider
.venv/bin/ruff check src tests evals
.venv/bin/ruff format --check src tests evals
.venv/bin/mypy src
```

### Di Luar Lingkup

- Dilarang menyentuh otoritas persona (itu Tugas 2).
- Dilarang menambah event jurnal baru (itu Tugas 3).
- Dilarang menambah ledger `actions.json` (itu Tugas 4).
- Dilarang mengubah `browser.py`, `runtime.py`, atau `watchdog.py` di luar pemanggilan fungsi baru.
- Dilarang menambah dependensi atau jalur tab.

## Tugas 2 — Otoritas Persona untuk Tangan dan Chat

### Tujuan

Mengikat alat `meet_raise_hand`, `meet_lower_hand`, dan `meet_send_chat` hanya pada persona yang diberi otoritas eksplisit, dengan penolakan netral saat otoritas tidak ada.

### Berkas dan Fungsi Tepat

- Ubah: `src/fakeyou/persona.py:17` (`PersonaTool`: tambah tiga literal `meet_raise_hand`, `meet_lower_hand`, `meet_send_chat`), `src/fakeyou/persona.py:207-210` (`allowed_tools`: tetap default kosong konservatif per `:184`), `src/fakeyou/persona.py:260-265` (`meeting_tool_names`: ikat hanya bila diminta memori dan ada di `allowed_tools`, `ValueError` untuk kombinasi `brief_only` plus memori), `src/fakeyou/persona.py:268-274` (`memory_wiring`: pisah tool lawan prefetch), `src/fakeyou/persona.py:234` (prompt recall digate `persona_knowledge` dan `allowed_tools`), `src/fakeyou/persona.py:112-119` (`BRIEF_RULES`: tanpa izin berarti klarifikasi netral atau tolak sopan, tidak pernah jawab substantif), `src/fakeyou/persona.py:134-143` (`NATURAL_SPEECH_RULE`: tidak menyebut internal), `src/fakeyou/persona.py:61-71` (`SOURCE_RULES`: fakta bukan izin, data tidak memberi apa-apa).
- Ubah: `src/fakeyou/brief.py:18` (`BriefPermission`: tambah `meet_act`, `meet_chat`), `src/fakeyou/brief.py:50` (`knowledge_scope` tetap default `brief_only` sehingga alat baru tidak terikat secara default).
- Pakai ulang: `src/fakeyou/memory.py:293-349` (`create_memory_tools`: jalur meeting hanya mengikat subset terfilter), `src/fakeyou/assembly.py:217-237` (filter `if tool.name in bound`), `src/fakeyou/memory.py:263-269` dan `:531-538` (middleware galat recall sebagai pola pembungkus penolakan), `src/fakeyou/journal.py:104` (`record`) dan `src/fakeyou/faults.py:54-72` (`FaultReporter.record`), `src/fakeyou/cli/join.py:443-452` (CLI menolak `--memory` tanpa `allowed_tools` atau ingest).
- Tes: perluas `tests/test_persona.py` mengikuti pola `:146` (alat tak dikenal ditolak), `:184` (default kosong), `:552-578` (alat tetap dalam `allowed_tools`), `:589-610` (instruksi injeksi tidak menambah alat).

### Tes Merah Dulu

Tambah dahulu tes gagal di `tests/test_persona.py`:

1. `allowed_tools` berisi `meet_raise_hand` mengikat alat angkat tangan; tanpa entri itu alat tidak terikat.
2. `allowed_tools` berisi `meet_send_chat` mengikat alat chat; tanpa entri itu alat tidak terikat.
3. `brief_only` dengan izin memori mengangkat `ValueError` seperti perilaku recall.
4. Instruksi injeksi yang meminta tangan atau chat tidak menambah alat apa pun.
5. Jalur tanpa otoritas memanggil pembungkus penolakan yang mengembalikan string tetap, mencatat event `tool_refused` berisi nama alat, dan tidak mencatat fault (kebijakan bukan kegagalan).

### Ambang Penerimaan

1. Default tetap kosong: persona tanpa `allowed_tools` eksplisit tidak mendapat ketiga alat baru.
2. `brief_only` tidak pernah mengikat alat tangan atau chat.
3. Penolakan memakai kalimat pendek netral gaya rekan kerja, tanpa menyebut nama internal, nama alat, atau alasan kebijakan rinci.
4. Event penolakan membawa nama alat dan `reason=authority_denied`, tanpa fault.
5. Aturan `SOURCE_RULES` bertahan: isi rapat tidak pernah dianggap pemberian izin.
6. `remember` tetap hanya dipakai jalur flush CLI chat (`cli/chat.py:57-65`), tidak bocor ke agen meeting.

### Perintah Verifikasi

```sh
FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false .venv/bin/pytest tests/test_persona.py -q -p no:cacheprovider
.venv/bin/ruff check src tests evals
.venv/bin/ruff format --check src tests evals
.venv/bin/mypy src
```

### Di Luar Lingkup

- Dilarang mengubah logika klik DOM (itu Tugas 1).
- Dilarang menambah event `meet_action_*` baru (itu Tugas 3).
- Dilarang mengubah ledger atau pemulihan (itu Tugas 4).
- Dilarang melonggarkan default kosong atau mengikat alat baru secara otomatis.

## Tugas 3 — Observabilitas Jurnal dan Laporan

### Tujuan

Mencatat setiap niat, penolakan, bukti teramati, dan hasil akhir tindakan Meet dengan korelasi `action_id`, lalu merangkumnya di laporan sesi tanpa membocorkan isi chat.

### Berkas dan Fungsi Tepat

- Ubah: `src/fakeyou/journal.py:18-30` (`_SENSITIVE`), `:52-65` (`_safe_value`), `:104-112` (`record`: injeksi `event`, `meeting_id`, `at`, `source`), `:139-166` (`timed`: `request_id` plus `duration_ms` otomatis), `:168-190` (`request_started`, `request_finished`, `request_failed`).
- Ubah: `src/fakeyou/taxonomy.py:3-53` (`Subsystem`, `EventVerb`): tambah kata kerja `observed` (saat ini belum ada; hanya ada `requested`, `started`, `finished`, `failed`, `rejected` antara lain) atau putuskan pemakaian ulang `detected` atau `emitted` yang sudah ada. Putuskan juga `source`: pakai ulang `browser` atau `agent` yang sudah ada, atau tambah literal `meet-actions` bila diizinkan taksonomi.
- Ubah: `src/fakeyou/report.py:47-69` (`describe_session`, `read_journal`), `:195-219` (hitungan per alat), `:314-324` (`fault_recorded`, `session_finished`, `policy_decided`), `:373-454` (`session_outcome`), `:501-510` (`_add_duration`: hanya durasi finite non-negatif).
- Tiru pola: `src/fakeyou/conversation.py:167-185` (llm `request_started/finished/failed` dengan `operation=chat`), `:192-213` (`timed tool_started/finished/failed` dengan `source=agent`), `:411-433` (`answer_text`, `sentence_emitted`: hanya `chars`, tanpa isi), `:846-851` (`policy_decided`), `src/fakeyou/voice.py:253-278` (`synthesis_started/finished/failed` plus `synthesis_pcm_emitted` sebagai bukti independen), `src/fakeyou/ingestion.py:246-285` (`memory_episode_ingested/failed`), `src/fakeyou/runtime.py:337-345` (`join_finished` dengan `source=runtime`), `:376-381` (`participant_observer_started`), `src/fakeyou/faults.py:64-72` (`fault_recorded`), `src/fakeyou/provider_faults.py:33-48` (`trim_detail`: sensor rahasia dan URL, potong 300 karakter), `src/fakeyou/worker_backend.py:159-185` (`summarize_session`), `src/fakeyou/cli/diagnostics.py:32-41` (`show_session_report`).
- Tes: perluas `tests/test_meet_actions.py` (korelasi dan idempotensi), `tests/test_report.py` mengikuti pola `:1240-1298`, serta pin `tests/test_taxonomy.py:56-99`, `tests/test_journal.py:62-98`.

### Tes Merah Dulu

Tulis dahulu tes gagal:

1. `meet_action_requested(action, action_id, actor, attempt)` lalu `meet_action_started`, `meet_action_finished` atau `meet_action_failed` berbagi `action_id` yang sama.
2. `meet_action_observed(action, action_id, proof)` wajib ada sebelum `meet_action_finished` yang mengklaim teramati; finish tanpa bukti masuk `evidence.excluded` sebagai `unobserved_finish`.
3. `meet_action_rejected(action, action_id, reason)` terminal untuk `authority_denied`, `not_admitted`, dan `duplicate_suppressed`; tidak ada retry sesudahnya.
4. Chat hanya mencatat `chars`, tidak pernah `text`, `body`, `transcript`, `prompt`, atau `completion`.
5. Laporan membangun `meet_actions` berisi hitungan per aksi per hasil, alasan penolakan, durasi detik, dan baris per `action_id` (`action_id`, `action`, `outcome`, `reason`, `observed`, `attempts`, `duration_ms`).
6. Dedup laporan memakai aturan menang-terminal-pertama per `action_id`; `missing_action_id` dan `duplicate` masuk `evidence.excluded`.
7. Nama event lolos pin taksonomi: huruf kecil, kata kerja di akhir, satu `source` `Subsystem`, durasi hanya `*_ms`.

### Ambang Penerimaan

1. Setiap aksi punya tepat satu hasil terminal per `action_id`.
2. Bukti observasi tercatat sebelum klaim sukses; tanpa bukti tidak boleh ada klaim sukses.
3. Tiga alasan penolakan didukung: `authority_denied`, `not_admitted`, `duplicate_suppressed`.
4. Tidak ada isi chat di jurnal maupun laporan pada semua rotasi jurnal.
5. Durasi non-finite atau negatif dikecualikan mengikuti `_add_duration`.
6. Aturan `tests/test_taxonomy.py:56-99` tetap hijau tanpa pengecualian khusus.

### Perintah Verifikasi

```sh
FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false .venv/bin/pytest tests/test_meet_actions.py tests/test_report.py tests/test_taxonomy.py tests/test_journal.py -q -p no:cacheprovider
.venv/bin/ruff check src tests evals
.venv/bin/ruff format --check src tests evals
.venv/bin/mypy src
```

### Di Luar Lingkup

- Dilarang memutuskan format ledger pemulihan (itu Tugas 4).
- Dilarang mengubah aturan ikat persona (itu Tugas 2).
- Dilarang menambah pemilih DOM baru (itu Tugas 0 dan 1).
- Dilarang menyimpan isi chat dalam bentuk apa pun, termasuk hash yang bisa dibalik atau cuplikan.

## Tugas 4 — Ledger Idempotensi dan Pemulihan Restart

### Tujuan

Menjamin satu niat tindakan hanya dieksekusi nol atau satu kali secara efektif, baik saat retry dalam proses maupun saat restart dengan `--resume`.

### Berkas dan Fungsi Tepat

- Baru: sidecar `actions.json` berdampingan dengan `record.jsonl`. Tulis atomik meniru `src/fakeyou/ingestion_status.py:78` (`write_atomically`: tmp lalu fsync lalu `os.replace` lalu fsync direktori). Kunci penulis-tunggal meniru `src/fakeyou/ingestion_status.py:164` (`record_lock`), `:207` (`file_lease`: flock, ambil-alih basi, heartbeat, lepas hanya token sendiri di `:244-249`), `:55` (`lock_path` sebagai sibling), `:256` (`_owner_recoverable`: ambil-alih setelah basi atau pid mati). Serialisasi antar aksi dengan `asyncio.Lock` mengikuti `src/fakeyou/watchdog.py:101`.
- Catatan niat per aksi: `action_id`, `kind`, `params_hash`, putusan otoritas, `state`, `observed_before`, `observed_after`, `attempt`, `at`. Kunci: `action_id` dari pemanggil (id tool-call) bila ada, bila tidak pakai `uuid5(meeting_id, seq)`. Status: `requested` lalu `acted` lalu `observed_done`, atau `refused`, atau `failed`. Status `refused` dan `failed` terminal tidak pernah dicoba ulang.
- Pakai ulang: `src/fakeyou/meet.py:373` (`_toggle_control`: amati-sebelum-bertindak, kembali bila status diharap terlihat di `:381`), `:408` (`leave_meeting`: jalan pintas sudah-keluar), `:173` (`_click_when_ready`: batas 3 upaya), `src/fakeyou/watchdog.py:240` (sidik `sha256` dialog), `:98` (`_handled`), `:152` (gate stabilitas dua-berurutan), `:178` (verifikasi ulang teks dan hitungan tombol sebelum klik), `:214` (debounce lobi butuh 2 identik), `src/fakeyou/journal.py:104` (`record`), `:139` (`timed`), `:168` (`request_started/finished/failed`), `src/fakeyou/provider_faults.py:51` (`ProviderFault.retryable`), `:78`, `:149`, `:21` (429, 5xx, timeout, koneksi boleh retry), `src/fakeyou/faults.py:54` (`FaultReporter`: dedup `(source, operation, kind, status)`, batas 128 di `:19`), `src/fakeyou/taxonomy.py:19` (`EventVerb`), `src/fakeyou/ingestion.py:352` (lewati rerun saat `corrected_at` cocok), `:367` (peta lanjut `correction_windows`), `:387` (lewati window selesai), `:430` (tulis status atomik per window), `:461` (`corrected_at` hanya saat semua lengkap), `:504` (`_orphaned_corrections`: adopsi episode tertulis lewat nama deterministik `:115` `correction_episode_name`), `src/fakeyou/record.py:102` (`MeetingRecord._seen`: dedup dalam-proses saja), `:146` (JSONL append-only), `src/fakeyou/conversation.py:674` (`_seen` dalam-memori; `docs/plans/conversation.md:34` menegaskan tanpa garansi dedup lintas-restart), `src/fakeyou/lifecycle.py:147` (`meeting_thread_id`: checkpoint penulis-tunggal), `:254` (resume ditolak tanpa flag), `:229` (`external_effects` ditambah sekali), `src/fakeyou/cli/join.py:487` (join memegang `record_lock`), `:488` (belum-selesai wajib `--resume`), `:494` (selesai plus resume ditolak), `:529,917` (`ResumeEntry` penghitung upaya).
- Tes: perluas `tests/test_meet_actions.py` plus tes ledger baru mengikuti pola `docs/plans/phase-2-followups.md:32` (duplikat retry sama dengan nol, penulis tunggal).

### Tes Merah Dulu

Tulis dahulu tes gagal:

1. Niat dicatat dalam `actions.json` sebelum klik pertama (retry tak menggandakan efek).
2. Amati-sebelum-bertindak melewati klik saat status sudah sesuai; `observed_before` tercatat.
3. Hanya `ProviderFault.retryable` yang dicoba ulang; non-retryable, `refused`, dan terminal `failed` bertahan terminal.
4. Crash tengah jalan lalu `--resume` memuat ledger dan merekonsiliasi tiap niat non-terminal lewat observasi: yang sudah efektif diadopsi sebagai selesai (gaya `_orphaned_corrections`), tidak pernah klik ulang yang `done` atau `refused`.
5. Dua penulis berebut: hanya satu yang memegang `record_lock` dengan `purpose=meeting`; yang lain mengangkat `RecordLockError`.
6. Duplikat retry sama dengan nol dan duplikat restart sama dengan nol pada skenario ganda-kirim yang sama.

### Ambang Penerimaan

1. Jurnal `RotatingFileHandler backupCount 2` (`journal.py:92`) tidak dipakai sebagai ledger baca-balik; semua keputusan pemulihan membaca sidecar `actions.json`.
2. Setiap klik didahului observasi dan diakhiri baca-balik; status `observed_done` hanya ditandai sesudah baca-balik sukses.
3. Aturan menang-terminal-pertama per `action_id` ditegakkan di ledger dan di laporan.
4. Pemulihan `--resume` tidak pernah mengklik ulang niat `done` atau `refused`.
5. Konten chat tidak masuk ledger; hanya `params_hash` dan `chars`.
6. Event jurnal memakai `source` yang sudah ada (`agent` atau `browser`) kecuali Tugas 3 memutuskan penambahan literal taksonomi.

### Perintah Verifikasi

```sh
FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false .venv/bin/pytest tests/test_meet_actions.py -q -p no:cacheprovider
.venv/bin/ruff check src tests evals
.venv/bin/ruff format --check src tests evals
.venv/bin/mypy src
```

### Di Luar Lingkup

- Dilarang memakai jurnal sebagai sumber kebenaran pemulihan.
- Dilarang mencoba ulang penolakan otoritas atau kegagalan non-retryable.
- Dilarang mengubah skema `record.jsonl` atau `ResumeEntry`.
- Dilarang menambah pemilih DOM baru selain yang dikunci Tugas 0.

## Tugas 5 — Integrasi Worker dan Gate Penerimaan M4

### Tujuan

Menyambungkan alat persona ke aksi peramban lewat worker meeting, menegakkan flag mati-default dengan gate lulus eksplisit, dan membuktikan tiga kriteria milestone ujung-ke-ujung.

### Berkas dan Fungsi Tepat

- Ubah: `src/fakeyou/runtime.py:639,715-736` (worker membuka tepat satu `Page`; selipkan panggilan aksi lewat `src/fakeyou/runtime.py:155-164` `_until_stopped`), `src/fakeyou/runtime.py:118-153` (`run_join_with_retry`: backoff bertingkat), `:611-618` (leave di bawah timeout 8 detik), `src/fakeyou/cli/join.py:407-409` (profil per twin plus meeting).
- Baca: `src/fakeyou/conversation.py:846-851` (`policy_decided`), `src/fakeyou/lifecycle.py:50,55` (final baru batalkan child lama, naikkan generation, hentikan playback usang; cek generation sebelum TTS), `docs/plans/sentence-streaming.md:89,96` (cek generation dan close sebelum tiap speak; nol PCM usang pasca-stop), `docs/plans/meeting-hardening.md:22,76-77` (gate latensi dan stop), `docs/plans/participant-attribution.md:45` (atribusi async tanpa beban latensi), `docs/plans/multiparty-participation.md:41,58,106` (tanpa buffering tambah latensi, tanpa timer spontan, observer read-only), `docs/plans/meeting-delegation-design.md:66,68` (tanpa proaktivitas terobos lantai sibuk; leave cepat tanpa model sesudah stop), `docs/reports/2026-10-05-voice-labels.md:161-164` (pola flag nyala hanya bila gate lulus eksplisit), `docs/reports/2026-10-04-knowledge-latency-lessons.md:45` (jauhkan efek samping sinkron dari jalur panas).
- Tes: `tests/test_meet_actions.py` (skenario ujung-ke-ujung dengan `FakePage`) plus satu jalur live bertanda browser. Tes gate latensi memakai anggaran yang sudah ada.

### Tes Merah Dulu

Tulis dahulu tes gagal:

1. Flag M4 default mati: tanpa flag nyala, alat tangan dan chat tidak dieksekusi walau terikat.
2. Cek generation dan status tertutup sebelum eksekusi: aksi usang dibatalkan dan tidak menyentuh DOM.
3. Aksi tulis berjalan async di luar jalur jawab; tes jalur panas tidak memanggil efek sinkron.
4. Stop membatalkan sisa aksi usang; tidak ada klik, PCM, atau playback usang pasca-stop.
5. Tanpa timer spontan: tidak ada pemicu aksi otomatis saat lantai sibuk.
6. Skenario ujung-ke-ujung: sukses teramati, tolak tanpa otoritas, dan kirim-ganda (retry lalu restart) menghasilkan tepat satu efek.

### Ambang Penerimaan

1. Flag default mati; flag hanya nyala bila gate lulus eksplisit ala voice-labels: stabilitas minimal 0,95, tidak ada label suara twin, dan latensi agent tidak bertambah.
2. Tiga kriteria milestone terbukti dalam satu rangkaian: sukses teramati, tolak saat otoritas kurang, nol duplikat saat retry dan restart.
3. Anggaran tak dilonggarkan: `p95 agent <= 4 detik`, live `p50 <= 3 detik` dan `p95 <= 6 detik`, `stop->senyap <= 1,5 detik`, `stop->leave <= 5 detik`.
4. Atribusi tetap async dan tidak menambah latensi agent.
5. Efek samping sinkron tidak ada di jalur panas.
6. Verdict tiap gate eksplisit lulus atau gagal di jurnal dan laporan.

### Perintah Verifikasi

```sh
FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false .venv/bin/pytest tests/test_meet_actions.py tests/test_persona.py tests/test_report.py -q -p no:cacheprovider
.venv/bin/ruff check src tests evals
.venv/bin/ruff format --check src tests evals
.venv/bin/mypy src
```

### Di Luar Lingkup

- Dilarang menyalakan flag secara default.
- Dilarang menambah aksi baru selain raise hand, lower hand, dan kirim chat.
- Dilarang mengubah anggaran latensi atau SLA stop.
- Dilarang menambah timer, polling spontan, atau perilaku proaktif.
- Dilarang memakai API privat Meet atau Media.

## Urutan Tugas Disarankan

1. Tugas 0 — Probe gate DOM. Gerbang untuk Tugas 1, 4, dan 5.
2. Tugas 2 — Otoritas persona. Bisa paralel dengan Tugas 0 karena tidak butuh pemilih DOM; pola tesnya mandiri di `tests/test_persona.py`.
3. Tugas 3 — Observabilitas jurnal dan laporan. Bisa paralel dengan Tugas 0 setelah keputusan nama `source` dan kata kerja taksonomi disepakati dengan Tugas 4.
4. Tugas 1 — Modul aksi peramban. Mulai hanya sesudah Tugas 0 lulus dan pemilih dikunci.
5. Tugas 4 — Ledger idempotensi dan pemulihan. Mulai sesudah Tugas 0 (butuh pemilih observasi) dan Tugas 3 (butuh nama event); memakai hasil Tugas 1 untuk baca-balik.
6. Tugas 5 — Integrasi worker dan gate penerimaan. Terakhir; butuh semua tugas sebelumnya hijau.

Alasan urutan: pemilih DOM adalah risiko terbesar sehingga diprobe dulu; otoritas dan observabilitas tidak bergantung pada DOM sehingga bisa paralel; ledger butuh keduanya (pemilih plus nama event); integrasi membuktikan semuanya ujung-ke-ujung.

## Pertanyaan Terbuka dan Gate

Semua hal tak terjawab dari enam laporan dibawa ke sini. Tidak ada yang dihapus.

### Gate Tugas 0 (wajib terjawab lewat probe langsung)

1. G0-1: Apakah atribut pasti tombol tangan adalah `aria-label="Raise hand"` dan `"Lower hand"`, dan apakah `aria-pressed` hadir? Cuplikan pencarian hanya memastikan label terlihat.
2. G0-2: Bagaimana observasi status tangan milik sendiri yang andal — tukar label lawan `aria-pressed` lawan ikon tile lawan toast? Pemilih ikon tangan di tile sendiri belum diketahui.
3. G0-3: Apa pemicu kirim chat yang benar — Enter lawan tombol kirim — dan bagaimana struktur DOM gema pesan sendiri (pemilih item daftar, atribusi pengirim)?
4. G0-4: Apa pemilih deteksi panel chat terbuka lawan tertutup?
5. G0-5: Apa pintasan raise-hand non-macOS? Yang diketahui baru Control-Command-H di macOS.
6. G0-6: Bagaimana gema kirim chat dibaca-balik secara andal (verifikasi echo)? Belum terkonfirmasi di repo; Tugas 1 dan 4 menunggu jawaban ini.
7. G0-7: Kunci seed selector tombol raise-hand dan panel chat Meet. Tanpa ini Tugas 1 dilarang mulai.

### Pertanyaan Arsitektur (jawab sebelum atau selama tugas terkait)

1. Q-1 (Tugas 3, 4): Apa nama `source` taksonomi untuk event aksi — pakai ulang `browser` atau `agent`, atau tambah literal `meet-actions` di `src/fakeyou/taxonomy.py:3`? `Subsystem` saat ini tidak punya `meet-actions`.
2. Q-2 (Tugas 3): Bagaimana menambah kata kerja `observed` ke `EventVerb` (`taxonomy.py:19`) tanpa melanggar pin `tests/test_taxonomy.py:56-99`, atau harus pakai ulang `detected` atau `emitted` yang sudah ada? `rejected` sudah ada dan dipakai untuk penolakan.
3. Q-3 (Tugas 2, 3, 4): Apa nama final kolom otoritas persona untuk tangan dan chat — perluasan `allowed_tools` di `src/fakeyou/persona.py:207-210` atau kolom baru? Desain observabilitas mengasumsikan string alasan `reason=authority_denied`.
4. Q-4 (Tugas 3, 4): Di mana lapisan persistensi `action_id` — jurnal saja lawan sidecar `actions.json`? Jurnal memakai `RotatingFileHandler backupCount 2` (`journal.py:92`) sehingga tidak andal untuk baca-balik; desain ledger mengusulkan sidecar gaya status.
5. Q-5 (Tugas 4): Apa nama dan skema final berkas niat — `actions.json` atau setara — karena belum ada ledger niat untuk aksi UI di repo?
6. Q-6 (Tugas 5): Berapa anggaran latensi khusus aksi tulis twin (klik sampai teramati)? Repo hanya menganggarkan jalur jawab (`p95 agent <= 4`, live `p50 <= 3` dan `p95 <= 6`) plus SLA stop. Nilai ini harus ditetapkan sebelum gate M4.
7. Q-7 (Tugas 5): Apa rumusan gate lulus eksplisit M4 untuk aksi tulis twin-initiated? Belum ada acceptance in-repo untuk kategori ini; adopsi pola flag-mati plus gate lulus eksplisit dari `docs/reports/2026-10-05-voice-labels.md:161-164` (stabilitas minimal 0,95, overlap-unknown minimal 0,95, nol label suara twin, latensi agent tak bertambah).
