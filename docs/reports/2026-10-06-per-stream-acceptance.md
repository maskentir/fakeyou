# Laporan acceptance capture per stream

Tanggal: 2026-10-06. Branch: `feat/per-stream-capture`.
Cakupan: spec `docs/plans/per-stream-capture.md`, rencana
`docs/plans/per-stream-capture-plan.md` Task 1–12.

## Vonis

Jalur per-stream **diterima bersyarat**: capture beridentitas,
atribusi, ketiadaan-miss, dan rejoin terbukti di room Meet live
dengan bukti angka di bawah. Yang belum: langkah cutover
penghapusan jalur mixed (Task 6 Langkah 5) dan tindak lanjut
triase. Rekomendasi flag di akhir laporan.

## Tabel gate

| Gate (spec) | Hasil ukur | Bukti |
| --- | --- | --- |
| Overlap pisah 2 teks benar ≥95% | LULUS kalimat, parsial angka | PS-2: "berbicara sekarang" + "Saya sedang berbicara depan" detik sama; PS-1F: split 2 stream, "dua" hilang + "empat" halusinasi STT |
| Atribusi solo ke stream benar ≥95% | LULUS 10/10, 0-miss otomatis | PS-1F 10/10; PS-2 9/10 pra-fix (giliran 6 hilang); PS-3 198 pasang 0 miss; PS-7 54 pasang 0 miss |
| Presisi nama ≥0,98 | tidak diuji (vakum) | Fitur speaker-names mati sesuai keputusan (gate SPK gagal pra-cabang); binder huruf a/b/c/d stabil sebagai identitas |
| Akhir-ucapan → finalisasi p50 ≤3 dtk, p95 ≤6 dtk | LULUS 0,0/0,0 dtk | spoken_at → finalized di semua sesi; metrik PCM-pertama belum instrumentasi langsung |
| Gate lama dipertahankan | LULUS | Replay probe-attr-8 + csrc-live-3 hijau, gate semantik hijau, laju c04 hijau, tanpa xfail |
| Suite offline penuh | LULUS 3136 passed | `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false pytest tests/ -q -p no:cacheprovider` → 3136 passed, 40 skipped, 41 deselected, exit 0 (angka deterministik reviewer final) |
| Suite browser | LULUS 8 passed | `pytest tests/test_stream_capture.py -o addopts= -m browser` → 8 passed (termasuk 2 tes racun mixer), exit 0 |
| Statis | LULUS | `ruff check`, `ruff format --check`, `mypy src` bersih |
| Identitas 4 pihak | LULUS | PS-7: 121 attributed, 0 unknown; label a/b/c stabil per slot 118/121 (~97,5%); 3 sisanya re-mint delete-one pasca-kepergian (by design). `speaker_bound` False multi-party: device binding hanya 1:1, label-lah identitasnya |
| Rejoin | LULUS seamless (sudut pandang A) | PS-7 D2: ucapan mendarat di slot/track D-lama dengan label lanjutan speaker-c — tanpa label baru, unknown, churn. B terpin di slot-1/b via teks persis, jadi slot-2/c = D-lama lalu D2 |

Satu-satunya merah lokal (`test_flag_off_keeps_mixed_path`)
adalah artefak env: gagal jika dan hanya jika `.env` live
gitignore menyetel flag, lolos netral dan di CI, tidak
terkait hunk diff mana pun (terbukti reviewer final).
(Klaim "gagal identik di base" pada draf ditarik: tesnya
tidak ada di base; uji stash terdahulu menyisakan file
tes versi branch sehingga tidak valid.)

## Sesi live

- Twin-twin TT-A2/TT-B: 4 giliran dua arah, semua
  `slot-2/speaker-a/stream-2`, loop agent penuh. Verifikasi
  mesin decode Opus pertama.
- PS-1F (2 manusia + twin): 10/10 atribusi, mute 2×30 dtk
  bersih, overlap angka parsial, rejoin terekam (ID baru).
- PS-2 (skrip baru): overlap kalimat LULUS; solo 9/10 —
  giliran 6 hilang di jendela reconnect STT → Task 8.
- PS-3 (3 twin + manusia): oracle silang 198 pasang 0 miss;
  hipotesis gema GUGUR (loop Sip/Okay = percakapan asli).
- PS-4 (4 twin): semua `unknown` → akar mixer-CSRC +
  roster-1 terbukti (CSRC 3707908267 di semua receiver).
- PS-5 (4 twin): pinball hilang (sync-only valid), tapi
  filter-nama makan saudara kembar (post=1) → Task 12.
- PS-6/PS-7 (4 twin): a/b/c distinct 1:1, 0 unknown,
  rejoin seamless, oracle 54/54.

## Akar penyebab yang diperbaiki (SDD penuh)

- Task 8 (`7a9ea08`): audio ke socket STT mati dibuang
  diam-diam selama reconnect antre → buffer kontinuitas
  30 dtk + reconnect segera saat socket mati + tag stream.
- Task 9 (`29b1192`): roster termasuk-self 4 entri
  melebihi kapasitas 3 → self-filter nama, kapasitas
  fleet, tenure dormant, alasan + ukuran roster di journal.
- Task 11 (`ee38711`): contributing-sources mixer racuni
  CSRC per slot → readCsrcs hanya-sync + diagnostik
  roster (tiles/pre/post) di heartbeat.
- Task 12 (`7aa82af`): filter nama salah saat nama
  tabrakan → saring hanya kecocokan tunggal, gate
  demand-side, rilis satu tenure per kepergian.
- Task 10 (`8017255`): reply-all memantulkan ack
  selamanya → ack-dalam-ack-run di-silent + dicatat
  (ack terisolasi, pertanyaan, substantif tetap dibalas).

Setiap task: tes merah dulu, implementer + reviewer
terpisah, suite penuh + browser + statis hijau, commit
lokal tanpa push/merge.

## Risiko dan triase (tidak memblokir vonis)

1. Infra pra-ada: checkpoint SQLite 100MB macet
   (starter ke-4 crash, agent mati DatabaseError tak
   pulih); preflight sekali-gagal-langsung-mati;
   guard ID bakar sesi gagal-start; dialog manual Meet.
2. Loop sapaan ("Halo." 15 giliran) bertahan —
   follow-up subsistem greetings, bukan ack (ruling
   review Task 10).
3. Kualitas STT kata pendek eksternal (provider).
4. Caveat harness: ucapan pertama setelah hening
   panjang tak andal; twin butuh bootstrap manusia.
5. Catatan minor review Task 8/9/11/12 (tercatat di
  status; dokumentasi + diagnostik lanjutan).

## Rekomendasi flag dan langkah berikut

- `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED` default
  TETAP MATI di commit ini.
- Cutover (hapus jalur mixed + nyalakan default) sebagai
  commit terpisah HANYA setelah pengguna menyetujui
  laporan ini — sesuai Task 6 Langkah 5.
- Berikutnya: review final seluruh branch (berjalan),
  lalu keputusan cutover.

## Adendum Fase 0 (rencana jangka panjang)

10/10 butir Fase 0 selesai, tiap butir SDD
(tes merah → implementasi → reviewer terpisah →
commit lokal): guard `[moved]` (`6f6b3bf`), jurnal
tenure + CSRC stream + dormant-defer
(`d046dc3`), observasi inversi
`order_inversion_detected` (`f53dce3`), knob VAD
(`832f82c`), vocab angka per bahasa profil
(`669056a`), metrik `stt_turn_recorded` + engine +
`stt_quality` (`10ee930`), harness WER
(`aa34906`), rename taksonomi (`ab465b5`).

Gate pasca-Fase 0 (output aktual):
suite serial 3182 passed / 40 skipped /
41 deselected; browser 8 passed;
`ruff check` + `format` bersih (209 berkas);
`mypy src` bersih (56 berkas). Flag default
tetap MATI. Kualifikasi jujur: bukti latensi
formal pasca-Fase 0 belum diukur ulang live
(Fase 0 menambah nol kerja blocking di jalur
agent; pengukuran ulang dijadwalkan sesi live
Fase 1). checklist merge terpenuhi kecuali
butir itu dan keputusan push/merge pemilik repo.

## Adendum ukur live Fase 1 (twin-only, 2026-10-07)

Sesi ukur-a-4/ukur-b-3/ukur-c-3, twin-only, 159 giliran total.
Angka via `fakeyou report`:

- Inversi: 0/159 (chatter sekuensial; tanpa tekanan overlap —
  keputusan butir 12 BELUM bisa diambil, butuh stres overlap).
- Churn interim: p50 5,5-7,0, p95 9-10 per giliran.
- Endpointing stop→final: p50 0,35-0,52 dtk, p95 1,05-2,10 dtk.
- Latensi agent stop→first-pcm: p50 4,5-5,2 dtk (dominan LLM+TTS).
- Engine: 600/600 poll `mstp`, 0 `decode` — jalur decode tak
  pernah fresh (temuan L0 Fase 1).
- WER opener: A→B 0,0 persis (digit 5/5); C→A 0,333
  (onset "Ukur C" terpotong VAD, digit 3/3); B→A miss
  artefak (opener 41 dtk sebelum A admit).
- I10: ucapan pertama pasca-hening-60d hadir (terpotong onset).
- Label vs speaker: A 3 label / 2 speaker, C 2 label /
  1 speaker, hanya 1 `tenure_released`, 0 move/relabel —
  mint diam saat churn slot (butuh event mint atau tabel fusi).

Insiden sesi: brief/persona ID mismatch mematikan worker
pra-jurnal (tanpa retry — gap infra); repro foreground
membakar ID ukur-a-3 via guard session-ended. Pelajaran:
verifikasi status+jurnal segera setiap start.

## Adendum overlap probe (2026-10-07)

Listener A/B + pembicara X/Y (4 twin). 97 giliran, 0 inversi
(total 0/256 bersama sesi ukur). Final lintas-stream 1-8 dtk
apart tiba urut — merge arrival-ordered bertahan dalam praktik.

- WER: X→A 0,25 (nama→"Orchid"); Y→A/B kehilangan deret hari
  ("...Ukur Rabu") — kelas kata-pendek I9, bukan overlap
  (admit terpaut 8 dtk: simultanitas SEJATI tak tercapai).
- Temuan bias: vocab "Orchid" mengalahkan nama "Ukur X" di STT.
- Temuan harness: earliest-in-window memungut transcript
  sesat "." — butuh saring kosong/tanda-baca.
- Temuan ordering: `run_admitted` bicara --speak SEBELUM
  capture nyala — twin tuli saat opener sendiri (bug Fase 1).

Keputusan butir 12: record-repair (publish-segera +
repair-on-read) + `order_inversion_detected` sebagai tripwire;
tanpa holdback. Sub-detik belum teruji; alarm produksi
menjadi pemicu revisit.

## Adendum implementasi butir 11+12 (2026-10-07)

Cabang `feat/phase-1-ordering`, plan
[phase-1-ordering.md](../plans/phase-1-ordering.md), tiap task
SDD (tes merah → implementasi → reviewer terpisah → commit
lokal).

| Butir | Kerja | Commit |
| --- | --- | --- |
| 11a | `drain()` JS mengemisikan `seq` monoton per slot | `db0714e` |
| 11b | Parse + validasi seq, jurnal `drain_gap_detected`
(gap/resync), hitung `drain_gaps` di `stt_quality` | `b848aaf` |
| 11c | Jam capture end-to-end: registry → `StreamAudioInput`
→ atribut frame → `TranscriptEvent.capture_ms` →
`TranscriptEntry.capture_ms` | `0a0e721` |
| 12 | `repaired_order` murni + `load_meeting_record(repair=True)`;
transkrip tercap diurut `(capture_ms, spoken_at, id)` di
posisinya, sisanya tetap | `04593fb` |
| fixup | Rename `drain_gap` → `drain_gap_detected` (aturan
taksonomi partisip lampau) + tautan plan | `c6c0c4c` |

Gate aktual (output teramati): suite serial 3231 passed /
41 skipped / 42 deselected, exit 0; browser 25 passed /
2 skipped (termasuk tes seq baru); `ruff check` bersih;
`ruff format --check` bersih (212 berkas); `mypy src`
bersih (56 berkas). Flag default tetap MATI.

Nol-tambah-latensi: `merge_transcripts` dan loop emit live
tak tersentuh; yang ditambah hanya cap atribut dan jurnal
aditif; repair hanya di baca-record offline. Tripwire
`order_inversion_detected` tetap sebagai alarm produksi.
Gap plan tercatat: `taxonomy.py` Subsystem `transport`
(Task 2), nama event taksonomi (fixup).

Sisa Fase 1: butir 13–16 (supervisor tap, pre-roll/keepalive,
tabel fusi identitas, endpointing semantik) — plan
tersendiri.

## Adendum implementasi butir 13+14 (2026-10-07)

Cabang `feat/phase-1-robustness` (basis
`feat/phase-1-ordering`), plan
[phase-1-robustness.md](../plans/phase-1-robustness.md), tiap
task SDD (tes merah → implementasi → reviewer terpisah →
commit lokal).

| Butir | Kerja | Commit |
| --- | --- | --- |
| 13a | Supervisor JS: deteksi macet > 3 dtk, rescue
membuka ulang slot yang sama, `health` per slot di drain | `aff7aaa` |
| 13b | Parse `health`, jurnal `tap_slot_rescued`, health di
heartbeat, hitung `tap_slot_rescues` di `stt_quality` | `2b5bb93` |
| 14a | `PreRollBuffer`: ring 500 ms disuntik sebelum
`VADUserStartedSpeakingFrame`; audio idle tetap diteruskan | `4f23186` |
| 14b | Reconnect proaktif saat sepi + idle (default mati),
uji kesiapan `stt_session_started/failed` fail-fast | `8685228` |

Gate aktual (output teramati): suite serial 3257 passed /
41 skipped / 43 deselected, exit 0; browser 26 passed /
2 skipped (termasuk tes rescue baru); `ruff check` bersih;
`ruff format --check` bersih (214 berkas); `mypy src`
bersih (56 berkas). Flag default tetap MATI.

Nol-tambah-latensi: rescue hanya membuka ulang slot yang
sudah mati; pre-roll menyuntik audio yang sudah diterima
(tanpa menunda); reconnect proaktif hanya saat sepi dan
mati default sampai data live; readiness langsung lolos di
start sehat. Deviasi plan yang disetujui reviewer: replay
pre-roll memakai `InputAudioRawFrame` (wajib agar STT
mentranskrip), event readiness `started/failed` (taksonomi
tanpa `ready`), verb `rescued` di taksonomi.

Sisa Fase 1: butir 15 (tabel fusi identitas) dan 16
(endpointing semantik + pra-generate) — plan tersendiri.

## Adendum implementasi butir 15 (2026-10-07)

Cabang `feat/phase-1-identity` (basis
`feat/phase-1-robustness`), plan
[phase-1-identity.md](../plans/phase-1-identity.md), tiap
task SDD (tes merah → implementasi → reviewer terpisah →
commit lokal).

| Butir | Kerja | Commit |
| --- | --- | --- |
| 15a | `SpeakerRegistry`: baris uuid deterministik,
status/keyakinan/margin/riwayat, aturan 1:1 + refusal | `91e6e43` |
| 15b | `SlotBinder` jadi adaptor tipis; tes lama hijau
tanpa modifikasi | `92912c6` |
| 15c | Tracker + binder stream berbagi satu registry;
`speaker_uuid` di ekstra/journal; `_csrc_labels` dihapus | `fc238df` |

Gate aktual (output teramati): suite serial 3276 passed /
41 skipped / 43 deselected, exit 0; browser 26 passed /
2 skipped; `ruff check` bersih; `ruff format --check`
bersih (217 berkas); `mypy src` bersih (57 berkas). Flag
default tetap MATI.

Bukti kesetaraan: probe diferensial reviewer 15/15 —
29 operasi atas binder lama vs registry: semua outcome dan
event `tenure_*` identik bita-per-bita. Dua substrat kini
satu; label huruf tampil-saja; relabel sunyi mustahil
(baris stabil, slot menumpang). Catatan reviewer:
indeks slot last-wins, kapasitas mixed 3→4 di sesi
berbagi, margin 1.0 kandidat-tunggal (jarak embedding
Fase 2).

Sisa Fase 1: butir 16 (endpointing semantik +
pra-generate) — plan tersendiri.

## Adendum implementasi butir 16 (2026-10-07)

Cabang `feat/phase-1-endpointing` (basis
`feat/phase-1-identity`), plan
[phase-1-endpointing.md](../plans/phase-1-endpointing.md),
tiap task SDD (tes merah → implementasi → reviewer
terpisah → commit lokal).

| Butir | Kerja | Commit |
| --- | --- | --- |
| 16a | `SemanticEndpoint`: tanda terminal + cukup kata +
stabil antar-interim, tepi-naik | `03be409` |
| 16b | Pra-generate saat sepi: buffer/antrean LLM,
adopsi cocok-persis, buang saat beda; default MATI | `cb3fbc1` |

Gate aktual (output teramati): suite serial 3321 passed /
41 skipped / 43 deselected, exit 0; browser 26 passed /
2 skipped; `ruff check` bersih; `ruff format --check`
bersih (220 berkas); `mypy src` bersih (58 berkas). Flag
default tetap MATI (termasuk `pre_generate_enabled`).

Nol-tambah-latensi: Task 2 aditif murni (387 tambah,
0 hapus); flag mati = satu panggilan inert; jalur normal
tak tersentuh (tes lama hijau tak tersentuh). Adopsi
konservatif: cocok-persis kata-ternormalisasi, sisanya
jalur normal. Catatan reviewer: adopsi tanpa
`agent_started` (hitung via `pregeneration_finished`
outcome=adopted), buffer-drain di `handle_transcript`
(kandidat pindah ke worker bila flag dinyalakan).

Fase 1 (butir 11–16) LENGKAP. Peta cabang bertumpuk
menuju merge berurutan: `feat/phase-1-ordering` →
`feat/phase-1-robustness` → `feat/phase-1-identity` →
`feat/phase-1-endpointing`. Sisa: ukur live (adopsi
pra-generate, rescue tap, inversi pasca-repair) lalu
Fase 2 (voiceprint, diarization offline, verified_name).

## Adendum verifikasi live pasca-Fase 1 (2026-10-07)

Sesi twin-only f1v (3 twin, ±195 giliran) + probe f1p
(2 twin ping-pong, beberapa ronde). Temuan dan perbaikan:

| Temuan live | Akar | Perbaikan |
| --- | --- | --- |
| Slot hantu di-rescue ±200×/twin
tiap 3 dtk, tak sembuh | Supervisor reattach tanpa
cek track ended + tanpa batas futility | Cabang
`fix/supervisor-release-dead-slots`: lepas track
ended, cap futility 5, `tap_slot_dropped`, semantik
stalled pra-rescue. Terverifikasi live: rescue
terbatas 4× lalu drop (73 drop, 0 loop) |
| Pregen 0 start dari ±60 giliran | Endpoint menuntut
tanda terminal; provider tak emit di interim | Cabang
`fix/endpointing-stability-trigger`: stabilitas 3× +
trigger VAD-stop + interim terakhir |
| 11 start / 0 adopsi | Interim saat VAD-stop basi
(provider lag) | Restart saat interim lebih baru
(reason `superseded`), min 1 kata jalur VAD |
| 3 twin mati `audio_failure` 3–5 dtk
pasca-admit | Deadline reconnect 120 dtk dihitung
sejak antre; lobby ≥150 dtk mati instan tanpa
pernah coba (bug pre-existing era Task-8, bukan
kode Fase 1) | Cabang `fix/stt-reconnect-lobby-epoch`
(basis endpointing): reset epoch saat admit |

Angka adopsi final (f1p-11, 18 final): 40 pregen —
17 adopted (42%), 23 superseded, 0 mismatched.
Hemat overlap: p50 0,03 dtk, p95 2,9 dtk.
Keputusan jujur: mekanisme aman (adopsi cocok-persis,
nol risiko konten salah) tetapi hemat median kecil
karena adopsi mengunci ke interim paling segar yang
tiba sekitar final; pertahankan default MATI sampai
kerja lanjutan (adopsi fuzzy berambang / pakai-ulang
prefiks, Fase 2). Inversi tetap 0; `drain_gaps` 0;
`speaker_uuid` + `capture_ms` mengalir live.

Gate tiap fix: SDD reviewer terpisah (2 Approve,
2 Approve-with-notes, 1 Reject→perbaiki→Approve),
suite 3328–3347 hijau, ruff + mypy bersih.

## Adendum ukur pregen Fase 2 (twin-only, 2026-10-07)

Sesi f2p-base-a2/x2 (D1 mati, 7 final) vs f2p-d1-a/x
(D1 nyala, 51 final), ping-pong 2 twin, seksi laporan
`pregeneration` baru terisi otomatis dari jurnal live.

| Metrik | Baseline (D1 mati) | D1 nyala |
| --- | --- | --- |
| Start | 16 | 56 |
| Adopted | 6 (38%) | 51 (91%) |
| Superseded | 10 (62%) | 5 (9%) |
| Mismatched | 0 | 0 |
| Restart-skipped | — | 64 (semua `prefix_extend`) |
| Hemat overlap p50 | 0,002 dtk | 0,36–0,38 dtk |
| Hemat overlap p95 | 0,16–0,44 dtk | 0,57–1,04 dtk |

Bacaan: 10/10 superseded baseline adalah `prefix_extend`
kecil — tepat kelas yang ditekan D1. 5 superseded yang
tersisa saat D1 nyala semuanya lompatan besar (norm
≥0,8; mis. 1→10 kata) yang benar di-restart. Gerbang
final exact menahan nol konten-salah di kedua sesi.
Kriteria Task 3 (restart turun, adopsi naik, p50 naik,
nol salah) lolos semua. Rekomendasi: D1 boleh nyala
kapan pun pregen nyala; default kedua flag tetap MATI
menunggu keputusan rollout yang lebih luas.

Catatan infra sesi: persona baru wajib `language: id`
(menyamai speech); volume Postgres lawas (5 Okt)
memakai password default sehingga URL checkpoint sesi
ini menunjuk password itu (non-destruktif, `.env`
direstore setelah sesi); `sentence_streaming` wajib
nyala agar gate pregen lolos.

## Adendum kalibrasi voiceprint ronde 1 (host-only, 2026-10-08)

Sesi vp-t5 (shakedown TTS, 45 keputusan) + vp-e (host,
64 keputusan matched), per-stream ON, θ=0,60/margin 0,15.

| Populasi | n | Skor | Hasil |
| --- | --- | --- | --- |
| TTS vs print TTS (shakedown) | 45 | 0,22–0,98 | 35 verified (loop live terbukti) |
| Host vs print host | 17 | 0,46–0,86 | 11 verified (cakupan 65%) |
| TTS vs print host (impostor) | 40 | 0,17–0,79 | 0 verified (semua via negatif/ambang) |

H1 consent ✓; H2 solo campur (di atas); H3 overlap —
turn host tetap verified BENAR (slot per-stream bersih,
ekspektasi unknown hanya untuk audio campuran); H4 mute
✓ + miss tepat di 0,60; H5+H6 rejoin + nama baru →
verified 0,86 di kedua worker ✓✓; T2 gate durasi tepat
(≥3 dtk terskor, <3 dtk dibuang); P1 latensi p50 219
mdtk p95 427 mdtk ✓; S1 audit ✓ (consent tercatat,
purge jalan, audio mentah enrollment dihapus pasca-sesi,
print ikut retensi 90 hari).

Temuan keras: (1) populasi true vs impostor TTS TUMPANG
di [0,45–0,79] — ambang tunggal tak memisahkan; nol
false-verify HANYA berkat print negatif (impostor
0,83 pun tertolak). (2) Turn identik diskor beda
antar worker s.d. 0,13 — segmentasi VAD per worker
mengayun batas klip. (3) Skor ≥0,60 pada solo rejoin
panjang vs miss pada solo pendek — durasi klip
mendominasi kualitas.

Kunci sementara: θ=0,60 + margin 0,15 + negatif wajib
dipertahankan (default flag tetap MATI). Gate presisi
0,98 butuh sesi multi-manusia + kerja stabilisasi skor
(batas klip deterministik, enroll lebih kaya); butir 19
mewarisi temuan ini. Temuan bug sesi: tap menimpa
segmen VAD (stop 0,2 dtk) → diakumulasi + discard-on-final
(cabang fix merge terpisah, SDD review); pin sherpa
1.12.38 cocok ORT 1.24.x pipecat.

## Adendum smoke persistensi audio Fase A (twin-only, 2026-10-08)

Sesi twin-only ±11 menit (Daz A + Daz X, per-stream +
persist on): tiap twin menulis 3 slot WAV 16 kHz mono
16-bit + `slots.json` skema `slot_audio/v1`, durasi WAV
(675 dtk / 791 dtk) ≈ durasi sesi, `gaps_filled=0`,
tepat 1 event `audio_recording_started` + 1
`audio_recording_finished` per journal. Matematika ukuran
eksak 32.000 B/dtk = 115,2 MB/jam/slot-aktif.
Drift jangkar page_ms-vs-mono: −34,0 ms/menit (Daz A)
dan +165,4 ms/menit (Daz X) — searah beban STT
berbeda, dalam batas koreksi linear offline.
Purge kering (`--days 36500`, root eksplisit):
`removed=[] kept=3`, exit 0.

Catatan sesi (bukan bug): (1) wiring join mensyaratkan
persist AND per-stream — diputuskan konsisten karena
mode campuran di luar cakupan Fase A (ruling tercatat
di ledger). (2) Default root purge (`record_dir`)
perlu `--root` eksplisit di host karena split path
host/container — pola sama seperti `report
--journal-dir`. (3) Start ulang meeting-id yang sudah
`session_finished` ditolak ("session already ended") —
dipakai id baru. Artefak WAV dipertahankan sebagai
deliverable retensi 30 hari.

## Adendum verified_name Fase 2 butir 19 (dev-lock + eval disjoint, 2026-10-08 s.d. 2026-10-09)

Hasil akhir Task 6–7 (`docs/plans/phase-2-verified-name-design.md`,
`phase-2-verified-name-plan.md`): gate eval SHIP (exit 0) —
presisi 1,0 (192/192 solo target terverifikasi, 0 alarm),
Wilson-bawah 0,9804 ≥ 0,98, overlap-unverified 1,0 (15/15),
twin false-verify 0 (188 trial TTS), lembah LLR non-overlap
lolos, θ=1,706, margin_floor=−0,19, artefak
`evals/fixtures/verified-name-dev-lock-v2.json`
(cal-18d382494254, dev_speakers=[harun], model ERes2Net
c5915837…, diprovenance `model_version`).

Jalur ke gate: ekspor ONNX CampPlus terbukti kacau-panjang
(konten identik ±0,3 dtk → cosine 0,295) lewat sesi diagnosis
bertakedump; diganti ERes2Net (uji regresi ketahanan panjang
+ suite 3693 hijau, review terpisah). Dev: 159 turn unik
compliant (dev2 speed-read didokumentasikan non-compliant,
tidak dipakai lock). Eval speaker-disjoint via re-split yang
disetujui (manusia segar tak ada): dev hanya-Harun
(120 target + 10 nontarget TTS), eval hanya-Annisa
(192 solo + 15 overlap + 188 twin-TTS) — id pembicara eval
∩ dev = ∅, diverifikasi exit-2 bila overlap.

Batasan peluncuran (blocker sebelum flag hidup): `display_claim`
tercatat None di semua sesi live sehingga `decide_verified`
selalu None di produksi hari ini — gate suara terbukti,
pengikatan nama butuh perbaikan aliran claim (tindak lanjut).
Perbaikan wiring mendarat 2026-10-08: poll→`note_roster` via
`parse_roster_names`, klaim pending untuk baris yang lahir
belakangan, `VoiceprintLive.artifact`, dan join meneruskan
klaim+artifact+slot ke scoring (sesi solo mengikat; multi-party
tetap abstain sampai atribusi fusion speaker-names ada).
`voiceprint_enabled` tetap default mati sampai verifikasi live
ulang membuktikan `name_verified` terbit di produksi.
Temuan infra sesi (bukan voiceprint): modal privacy-tip Meet
tak tertangani mematikan satu twin; brief `meeting_id` wajib
cocok sesi; jangkar `slots.json` dev1 meleset ~55 dtk.
