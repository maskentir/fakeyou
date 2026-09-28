# Rencana runtime sesi dan guest Google Meet

Tahap 1 dari [status fondasi](project-status.md): satu worker yang hidup sepanjang meeting dan masuk sebagai guest. Dokumen ini merinci desain sebelum implementasi; kriteria selesai mengikuti tahap 1 di sana.

## Tujuan

Satu guest masuk ke meeting uji dengan nama twin setelah diterima host, tetap hidup sampai dihentikan, lalu menutup semua resource. Penolakan, lobby timeout, dan gangguan browser menghasilkan status berbeda yang dapat diperiksa.

Bukan tujuan tahap ini: audio dua arah (tahap 2), percakapan LangGraph penuh (tahap 3), persona/memori otomatis (tahap 4), observasi multipihak dan tindakan lanjutan (tahap 5).

## Identitas

Setiap sesi membawa empat ID:

| ID | Arti | Contoh sumber |
| --- | --- | --- |
| `owner_id` | Pemilik twin | CLI arg / penjadwalan |
| `twin_id` | Identitas twin milik owner | Profil twin |
| `meeting_id` | Satu kehadiran di satu link | Dibuat saat start worker |
| `thread_id` | Percakapan LangGraph sesi itu | `f"{twin_id}:{meeting_id}"` |

`thread_id` diturunkan deterministik dari twin + meeting agar reconnect memakai konteks yang sama tanpa lookup tambahan. Namespace memori Graphiti mengikuti `owner_id`, bukan `meeting_id`.

## Bentuk worker

Satu worker = satu meeting = satu proses logis yang memiliki:

- Browser CloakBrowser + sesi Playwright MCP (dari `open_browser_tools`)
- Pipeline Pipecat (slot kosong tahap ini; diisi tahap 2)
- Thread LangGraph + checkpointer
- Ruang memori (`group_id` owner)

Worker adalah composition root: membuka resource dalam `AsyncExitStack`, menjalankan lifecycle, lalu menutup semuanya. Mengikuti pola `chat` di [cli.py](../src/fakeyou/cli.py), tetapi hidup selama sesi, bukan satu turn.

Modul yang diusulkan:

| Modul | Isi |
| --- | --- |
| `runtime.py` | Worker: buka resource, jalankan lifecycle graph, stop/cleanup |
| `meet.py` | Guest adapter: langkah join/leave di atas halaman humanized langsung |
| `lifecycle.py` | State lifecycle + transisi + alasan terminal |

Nama final diputuskan saat implementasi; batas modulnya yang penting: adapter Meet deterministik (infrastruktur), lifecycle sebagai state eksplisit, worker sebagai pemilik resource.

## Lifecycle

State: `preparing → lobby → admitted → leaving → ended`, dengan `disconnected` sebagai state samping dari `lobby`/`admitted` saat koneksi terputus dan percobaan ulang berjalan.

Alasan terminal (wajib berbeda dan tercatat): `admitted_then_stopped`, `denied_by_host`, `blocked_by_platform`, `removed_by_host`, `lobby_timeout`, `browser_failure`, `owner_stop`, `meeting_ended`, `duration_limit`.

Klasifikasi verdict mengikuti Vexa: copy penolakan eksplisit ("denied your request", "weren't allowed to join", ...) → `denied_by_host`, terminal tanpa re-knock; layar error ambigu ("can't join this...") → `blocked_by_platform`, terminal; captcha live terlihat → tahan selama grace 120 detik untuk solve (manusia pada sesi headed) lalu `blocked_by_platform` bila tak terselesaikan. Cek rejection selalu sebelum cek admission, dan penanda waiting-room menekan sinyal admitted.

Aturan:

- Gunakan state dan persistence resmi LangGraph untuk alur aplikasi, bukan state machine tulisan tangan di atas variabel.
- Setiap transisi menyimpan bukti: timestamp, state asal/tujuan, detail mentah platform bila ada, dan atribusi (`host_action`, `system_fault`, `user_action`). Kosakata ini mengikuti pelajaran Vexa di [reference lessons](reference-lessons.md).
- Aksi eksternal yang sudah terjadi (join diminta, leave dikirim) dicatat sebelum transisi agar retry tidak menggandakan efek.

## Guest adapter

Langkah join, berurutan, masing-masing dengan observasi hasil:

1. Buka link meeting; tunggu `domcontentloaded` dengan budget 60 detik. Terima timeout bila URL sudah di host meeting (pola Amazon LMA: SPA yang sudah termuat bukan join gagal).
2. Isi nama twin pada kolom nama.
3. Atur perangkat (mic/speaker/kamera sesuai kebijakan sesi; default mute saat masuk bila didukung UI).
4. Klik ask-to-join; catat waktu permintaan.
5. Tunggu admission sampai deadline lobby: polling state halaman (lobby vs in-call vs denied).
6. Setelah admitted, pantau removal/kick dan meeting berakhir.
7. Leave eksplisit saat dihentikan; verifikasi halaman keluar/tutup.

Setiap langkah gagal dengan alasan spesifik, bukan boolean generik. Input memakai browser tools resmi; bila Meet menolak input sintetis, catat sebagai temuan verifikasi (hipotesis Vexa) dan putuskan penanganan — bukan asumsi.

## Stop, batas, dan cleanup

- Stop pemilik: sinyal stop menghentikan lifecycle dari state mana pun → `leaving` → cleanup.
- Deadline lobby: dapat dikonfigurasi, default diusulkan 5 menit.
- Batas durasi: dapat dikonfigurasi, default diusulkan 2 jam.
- Cleanup: tutup sesi MCP, konteks browser, koneksi memori, pipeline audio; hapus profile sementara. Urutan terbalik dari pembukaan via `AsyncExitStack`.

## Keputusan runtime media (masukan tahap 2)

Ditulis 2026-09-28 dari pola terbukti LMA virtual participant, Attendee, dan Vexa. Disetujui pemilik 2026-09-28.

- OS target worker audio: Linux dalam container untuk prod dan dev-audio (image yang sama). macOS native tetap untuk join/monitor/leave dan observasi teks, tanpa audio. Alasan: virtual audio device yang realistis hanya di Linux; LMA eksplisit melarang dev di laptop dan mewajibkan mirror prod. Container membawa PulseAudio dan Chromium sendiri sehingga tak butuh audio host. Catatan: dev ARM64 vs prod x86_64 butuh image multi-arch.
- Mic virtual (TTS keluar): topologi PulseAudio ala LMA. Null sink `agent_output` menerima PCM dari TTS via pipe; `module-remap-source` membuat `agent_mic` dari `agent_output.monitor`; `agent_mic` jadi default source sehingga Chromium memakai mic virtual. Capture 16 kHz mono s16le (native STT, tanpa resampling); sisi agent 24 kHz mengikuti native OpenAI TTS, resample oleh PulseAudio (lihat audio-runtime-plan).
- Capture (STT masuk): null sink `meeting_audio` jadi default sink Chromium; capture `meeting_audio.monitor` via pipe ke STT. Sink ketiga campuran opsional menyusul bila transkrip butuh mix.
- Alternatif ditolak untuk tahap 2: injeksi PCM in-page ala Attendee (chunk PCM via evaluate ke WebAudio lalu mic track) — OS-independen tapi membanjiri CDP dan rapuh; disimpan sebagai fallback macOS-native. Capture MediaRecorder in-page ala Vexa — cukup untuk rekam, tak memberi PCM real-time untuk agen suara.
- Implikasi tahap 2: Dockerfile worker (Linux + browser + PulseAudio + entrypoint topologi audio idempoten), watchdog format device, bridge Pipecat ke pipe audio.
- Risiko: binary CloakBrowser Linux harus tersedia; IP datacenter prod butuh proxy residensial.

## Konfigurasi yang diusulkan

Tambahan `Settings` (prefix `FAKEYOU_` tetap):

| Key | Arti |
| --- | --- |
| `MEET_LINK` / arg CLI | Link meeting (arg perintah, bukan env) |
| `TWIN_NAME` | Nama tampil twin di lobby |
| `LOBBY_TIMEOUT_SECONDS` | Deadline admission |
| `SESSION_DURATION_SECONDS` | Batas durasi sesi |
| `OWNER_ID`, `TWIN_ID` | Identitas sesi |

CLI yang diusulkan: `fakeyou join <link> --twin-name ... --owner ...`, berjalan foreground sampai sesi berakhir atau dihentikan. Kontrol start/stop/status jarak jauh dan penjadwalan menyusul di tahap 6.

## Rencana pengujian

| Uji | Jenis | Bukti |
| --- | --- | --- |
| Transisi lifecycle + alasan terminal | pytest lokal, adapter palsu | Setiap state dan alasan tercapai |
| Stop pemilik dari tiap state | pytest lokal | Cleanup selalu berjalan, tanpa resource menggantung |
| Deadline lobby dan batas durasi | pytest lokal, waktu palsu | Status terminal benar |
| Checkpoint lifecycle dibuka ulang | pytest lokal, SQLite | State pulih tanpa mengulang aksi eksternal |
| Join meeting uji asli | Manual/live, host menerima | Guest tampil di peserta, leave bersih |
| Penolakan host | Manual/live | Status `denied_by_host`, bukan timeout generik |
| Replay/reconnect | pytest + live | Tidak ada join/leave ganda |

Evaluasi DeepEval tahap ini tetap deterministik: trajectory tool adapter (navigasi → isi nama → ask-to-join → observasi), mengikuti pola `evals/test_meeting_agent.py`, bukan kualitas penalaran.

## Status implementasi

- [x] `lifecycle.py`: state, transisi, alasan terminal, persistence LangGraph.
- [x] `meet.py`: langkah join/leave di atas browser tools + observasi hasil.
- [x] `runtime.py`: worker, `AsyncExitStack`, stop/batas/cleanup.
- [x] CLI `join` + settings baru.
- [x] Keputusan runtime media tertulis (disetujui 2026-09-28).
- [x] Uji live di meeting terkontrol (admit, deny, timeout, stop, leave).

Sinyal default `MeetSignals` sebagian sudah terverifikasi dari snapshot live 2026-09-27: placeholder kolom nama, teks tombol ask-to-join, dialog "Continue without microphone and camera", dan penanda denied ("You can't join this video call"). `join_now`, `leave_call`, `removed`, dan `ended` masih tebakan. Adapter menerima `signals` sebagai parameter agar perbaikan tidak mengubah logika langkah.

## Temuan uji live (link qyh-rbwo-epc, 2026-09-27)

- Probe: lobby guest termuat di headless. Kolom nama memakai placeholder (ref di baris sebelum label), tombol ask-to-join disabled tanpa ref sampai nama diisi, dan dialog izin media muncul. Ketiganya ditangani adapter + tes.
- `live-qyh-1`: navigasi sukses salah terdeteksi gagal karena teks hasil mengandung "0 errors". Diperbaiki: deteksi sukses via penanda `Page URL`, unwrap envelope hasil MCP, cek error via prefix. Detail kegagalan dipotong 200 karakter plus host saat itu.
- `live-qyh-2`: alur penuh sampai klik ask-to-join, lalu Meet memblokir otomatis dalam ~2 detik ("Your meeting is safe"). Host tidak melihat tamu sama sekali. Sinyal denied lama tidak cocok sehingga salah lapor `lobby_timeout`.
- `live-qyh-3`: jalur denied terverifikasi, lapor `denied_by_host` dalam ~1 menit.
- Alasan `denied_by_host` mencakup blokir otomatis platform; keduanya tidak dapat dibedakan dari halaman Meet. Atribusi `host_action` pada event denied berarti "entri ditolak", bukan vonis bahwa host menekan tombol.
- Terbuka: permintaan guest tidak sampai ke host pada meeting ini. Dugaan: deteksi bot headless atau kebijakan meeting yang menolak tamu anonim (ada dialog "Instead of waiting to be let in, sign in..."). Langkah berikut: periksa kebijakan guest meeting tersebut, coba mode headed, dan pertimbangkan penanganan dialog sign-in bila hanya imbauan.
- `live-qyh-4` (headless + hardening: flag clean, init script deviceMemory/chrome.runtime, `?hl=en`): tetap diblokir ~2 detik setelah klik. Init script terverifikasi bekerja via smoke test browser.
- `live-qyh-5` (headed, jendela terlihat): tetap diblokir dengan layar yang sama.
- Kesimpulan sementara: flag, init script, dan headless/headed bukan pembeda. Tersisa dua hipotesis: (a) input sintetis sesuai klaim Vexa — butuh klik non-sintetis (halaman Playwright langsung atau XTEST OS-level); (b) meeting/link menolak tamu anonim apa pun. Eksperimen penentu: manusia membuka link di Chrome normal dan klik Ask to join — hasilnya memisahkan kedua hipotesis.
- `live-qyh-6` (pengulangan headless): hasil sama, `denied_by_host` cepat. Blokir konsisten, bukan gangguan sesaat.
- `live-qyh-7` (klasifikasi Vexa): layar blokir yang sama kini dilaporkan `blocked_by_platform` dengan benar dan terminal. Tidak ada captcha live terdeteksi pada polling.
- `live-qyh-8` (seed fingerprint stabil per twin + patch cookie Google): tetap diblokir. Sisa paket LMA: profil persisten + warmup (butuh keputusan model isolasi) dan binary terbaru via key gratis.
- `live-qyh-9` (profil persisten per twin + warmup + seed stabil + patch cookie): tetap diblokir. Profil terverifikasi matang (Cookies + History terisi, dipakai ulang antar sesi). Satu-satunya item LMA yang belum dicoba: binary terbaru via key gratis, dan mode headed + humanize langsung (jalur MCP mem-bypass humanize).
- `live-qus-1` (link kedua, profil matang dipakai ulang): blokir identik ~2 detik setelah klik. Dua link berbeda, hasil sama — kemungkinan masalah di sisi automation, bukan kebijakan satu meeting (kecuali kedua link memakai kebijakan org yang sama).
- Terobosan jalur input: forensik API membuktikan klik MCP valid tapi `CreateMeetingDevice` dijawab 403. Probe direct-drive CloakBrowser `humanize=True` masuk meeting 3 dari 4 percobaan (0 dari 11 via MCP). Worker dibangun ulang di atas halaman humanized langsung: observasi via `page.content()`, aksi via locator `text=`/`get_by_placeholder`/CSS (humanize tidak mendukung `get_by_role`).
- `live-qus-2` (jalur direct-drive via CLI): **admitted setelah host menyetujui permintaan masuk**, monitor sampai batas durasi 90 detik, leave terverifikasi, `duration_limit`. Alur persetujuan host terverifikasi ujung-ke-ujung. Penolakan, lobby timeout, dan stop pemilik kemudian terverifikasi live (`live-deny-1`, `live-timeout-1`, `live-stop-4`).
- Investigasi API: klik valid dan menembakkan `MeetingDeviceService/CreateMeetingDevice`; server menjawab 403 `PERMISSION_DENIED` ("The caller does not have permission"). `ResolveMeetingSpace` 200 (link valid), tidak ada error saat load. IP egress residential (Indosat). Artinya penolakan terjadi pada evaluasi server atas request join — konsisten dengan kebijakan wajib-login maupun skor risiko sesi; tidak ada varians dalam 10+ percobaan lintas fingerprint/profil.
- `live-deny-1`: host menekan Deny, worker lapor `denied_by_host`. `live-timeout-1`: knock dibiarkan 60 detik, lapor `lobby_timeout`. `live-stop-4` (identitas segar `stop_twin`): admit host lalu SIGINT saat monitor, leave terverifikasi, `admitted_then_stopped`. Ketiga jalur gagal tahap 1 terverifikasi live.
- Klik humanized tidak pulih dari overlay sesaat dalam satu panggilan: `live-stop-1/2` gagal 30 detik penuh ("covered by DIV") padahal halaman bersih 2-3 detik setelah load; klik ulang dengan resolusi segar selalu sukses <1 detik. Perbaikan: helper `_click_when_ready` (3 upaya, locator segar tiap upaya, timeout pendek) untuk semua klik lobby dan leave, plus 4 tes regresi.
- Rate-limit identitas: setelah ~5 join + ~15 load dalam satu jam, identitas `local_twin` langsung ditolak tiap knock (`blocked_by_platform`), sementara identitas segar (seed + profil baru) masuk waiting room normal di meeting yang sama. Meeting dan host terbukti sehat. Implikasi uji: jeda antar join atau rotasi identitas uji; pemakaian normal (satu join per meeting) tidak terpengaruh.

## Urutan implementasi

1. `lifecycle.py`: state, transisi, alasan terminal, persistence LangGraph.
2. `meet.py`: langkah join/leave di atas browser tools + observasi hasil.
3. `runtime.py`: worker, `AsyncExitStack`, stop/batas/cleanup.
4. CLI `join` + settings baru.
5. Keputusan runtime media tertulis.
6. Uji lokal, lalu uji live di meeting terkontrol.

## Verifikasi selesai

- Satu guest masuk meeting uji setelah diterima host, tetap hidup sampai dihentikan, cleanup bersih.
- Penolakan, lobby timeout, dan gangguan browser masing-masing menghasilkan status berbeda yang dapat diperiksa.
- Keputusan runtime media tercatat dan menjadi masukan tahap 2.

## Refactor sebelum tahap 2

[Laporan refactor 28 September](refactor-2026-09-28.md) mencatat pembagian konfigurasi sesi, persistence lifecycle, worker, serta perbaikan cleanup/cancellation. Jalur humanized langsung dipertahankan. State sesi aktif setelah process crash tetap ditolak untuk mencegah replay efek eksternal; pemulihan resource otomatis belum tersedia.
