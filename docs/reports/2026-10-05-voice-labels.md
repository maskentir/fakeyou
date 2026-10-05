# Laporan Label Suara Anonim — 2026-10-05

Lanjutan dari [laporan probe atribusi](2026-10-05-attribution-probe.md).
Gate penamaan gagal, jadi milestone dipersempit menjadi label anonim
("Speaker A/B/…") dari tap energi WebAudio. Fitur ada di balik flag
`FAKEYOU_CONVERSATION__VOICE_LABELS_ENABLED` yang default mati. Saat
flag mati, perilaku sama persis dengan sebelumnya.

**Verdict replay: LULUS gate stabilitas per join dan overlap.**
Stabilitas lintas rejoin tidak tercapai dan memang tidak bisa dicapai
dari bukti ini; lihat [Batas](#batas).

## Yang dipasang

- `assets/voice_activity.js`: init script minimal.
  - Membungkus `createMediaStreamSource` pada `AudioContext` dan
    memasang `AnalyserNode` pasif per sumber.
  - Mensampel RMS tiap 50 ms (probe dulu 250 ms) dan hanya mengirim
    level ≥0,001.
  - Mic lokal dikecualikan secara struktural: track dari
    `getUserMedia` beserta clone-nya ditandai lokal dan tidak
    disampel.
  - `drain()` juga mengirim roster `[data-participant-id]` (id
    device dan nama tile) serta id instance script.
- `voice_activity.py`:
  - `VoiceActivityTracker` memetakan jam halaman ke jam monotonik
    lewat anchor drain dengan round trip terkecil, lalu menyimpan
    level 120 detik terakhir.
  - Jendela ucapan sebuah transkrip adalah seluruh segmen VAD sejak
    transkrip sebelumnya. Segmen yang masih terbuka ikut dihitung
    sampai saat transkrip tiba. Jendela digeser lag pipa 200 ms.
  - Aturan dominan bermargin 0,5 dengan ambang aktif 0,01: tepat
    satu slot dominan menjadi label, lebih dari satu menjadi
    `unknown(overlap)`.
  - `SlotBinder` memberi label per *tenure* slot, lalu mengikat
    tenure ke device roster lewat eliminasi satu-ke-satu (satu
    device tak terikat dan satu tenure tak terikat).
    - Bila device terikat keluar, hanya slot miliknya yang
      dilepas.
    - Bila device tak terikat keluar, semua tenure tak terikat
      dilepas, karena lebih aman memecah label daripada
      mencampur orang.
    - Tile yang hilang kurang dari 3 detik dianggap kedip.
  - Alasan `unknown`:
    - `no_speech`, `stale`, `no_source`, `overlap`;
    - `no_roster`;
    - `slot_contention` (lebih dari 3 peserta remote, sehingga
      slot Meet dapat bergilir);
    - `slot_conflict` (slot baru tanpa device yang belum terikat);
    - `self_overlap` (tenure baru tidak dibuat selama twin
      sedang bicara).
  - Reload script (id instance berubah) mereset semua tenure.
- Integrasi:
  - `TranscriptEvent.participant_id` menjadi `speaker-a` dan
    seterusnya, dengan `speaker_evidence_id` `voice-N`.
  - Journal `transcript_finalized` membawa `speaker_reason`,
    `speaker_slot`, `speaker_bound`, dan `speaker_levels`.
  - `fakeyou report` menghitung alasan `unknown` dari field itu.
  - Ingestion memberi label "Speaker A with unverified identity".
    Instruksi ekstraksi menambah larangan menggabungkan label
    dengan nama, label lain, atau label meeting lain, tetapi hanya
    bila record memang berisi label suara.
  - Debrief menambah batasan bahwa label bukan identitas
    terverifikasi.
- Nama tidak pernah dikeluarkan. Device terikat hanya dicatat
  sebagai `speaker_bound` di journal.

## Replay offline (probe-attr-8, tracker produksi)

Fixture `tests/fixtures/voice-labels-probe-attr-8.json` (151 KB,
nama disamarkan menjadi Host/Guest 1–3/Twin, device id
dinomori ulang) dibangun oleh `voice_fixture_from_probe` dan
diputar oleh `replay_voice_labels`. Tes
`test_voice_label_replay_gate_on_probe_attr_8` menjaganya di CI.

| Metrik | Nilai | Gate | Verdict |
| --- | --- | --- | --- |
| Stabilitas label per join (giliran solo berlabel) | 0,971 (33/34) | ≥0,95 | LULUS |
| Purity label (satu label = satu join) | 0,971 (33/34) | — | info |
| Cakupan giliran solo | 0,944 (34/36) | — | info |
| Overlap menjadi `unknown` | 1,000 (1/1) | ≥0,95 | LULUS (tipis, n=1) |
| Join mendapat label berbeda | Host A, Guest 1 B, Guest 2 C, Guest 3 D | — | ya |
| Tenure terikat lewat eliminasi | 41/41 transkrip berlabel | — | info |
| Stabilitas per orang lintas rejoin (B) | 0,556 (10/18) | — | tidak tercapai |

Tiga giliran yang tidak tepat:

- Turn 1 menjadi `overlap`, karena jendela sejak transkrip
  sebelumnya ikut memuat ucapan Host. Hasil ini konservatif dan
  benar.
- Turn 5 menjadi `stale`, karena probe men-drain tiap 1 detik
  sehingga tap jendela itu belum tiba saat transkrip final.
  Produksi men-drain tiap 250 ms.
- Turn 11 diberi label Host. Ini giliran ambigu yang sama di
  laporan probe: energi kuat di slot Host sementara truth menyebut
  B.

Variasi parameter pada prototipe (lag 0–300 ms, padding 0–250 ms)
memberi stabilitas 0,969–0,971. Aturan puncak lebih baik daripada
voting per frame (0,962–0,968 dengan cakupan lebih rendah). Lag
200 ms dipilih dari pemindaian kesesuaian dan parameter VAD, bukan
dari tuning atas truth.

Bila tenure terikat dipakai sebagai nama, presisinya 0,971 (33/34),
di bawah gate nama 0,98. Nama tetap mati.

## Batas

- Label hanya sah dalam satu join. Orang yang keluar lalu masuk
  lagi mendapat label baru, karena slot Meet bukan identitas dan
  CSRC terblokir CloakBrowser. Menyatukan label lintas rejoin butuh
  sinyal identitas suara (embedding speaker) atau CSRC dari browser
  lain; keduanya milestone sendiri.
- Lebih dari 3 peserta remote berarti semua `unknown`
  (`slot_contention`), karena Meet hanya punya 3 slot audio dan
  menggilirnya.
- Asumsi yang belum diuji live:
  - Meet tidak memindah slot orang yang masih hadir selama peserta
    ≤3. Bila asumsi ini salah, binder bisa mengikat slot baru ke
    peserta diam.
  - Track mic Meet berasal dari `getUserMedia`. Bila Meet
    membuat track lokal dengan cara lain, tap mic tidak
    dikecualikan di JS; penjaga `self_overlap` hanya mencegah
    tenure baru.
- Pengecualian tile twin memakai kecocokan nama display, karena Meet
  tidak merender penanda diri struktural.
- Graphiti dapat menyatukan entitas "Speaker A" antar-meeting saat
  dedupe. Instruksi ekstraksi melarangnya, tetapi dedupe Graphiti
  memakai prompt sendiri. Ini perlu dicek saat acceptance ingestion.
- `participant_id` berlabel mengaktifkan cabang `same_participant`
  di kebijakan partisipasi (follow-up dari pembicara yang sama)
  bila `participant_policy_enabled` juga menyala.

## Verifikasi

- `ruff check`, `ruff format --check`, `mypy src`: bersih.
- `pytest -m 'not live and not container and not browser'`: 3073
  lolos, 40 skip.
- `pytest -m browser` (probe + voice, CloakBrowser asli): 6 lolos.
  Tes voice memastikan tap remote terbaca dan mic lokal hasil
  clone tidak tersampel.
- `docker compose build meeting-worker` lalu `pytest -m container`:
  5 lolos, termasuk asset `voice_activity.js` di image.

## Acceptance live (belum dijalankan)

1. Backup `.env`, lalu set
   `FAKEYOU_CONVERSATION__VOICE_LABELS_ENABLED=true`.
2. Jalankan sesi dengan host, minimal satu manusia lain, dan twin
   aktif yang ikut bicara (menguji pengecualian mic lokal dan
   `self_overlap`). Sertakan satu rejoin dan satu overlap.
3. Ukur dengan `fakeyou report`: `attribution_coverage` dan
   `unknown_reasons`. Bandingkan label per transkrip dengan truth
   host memakai pola replay di atas.
4. Lulus bila stabilitas per join ≥0,95, overlap menjadi `unknown`
   ≥0,95, tidak ada label yang tersampel dari suara twin, dan
   latency agent tidak bertambah. Baru setelah itu flag boleh
   default menyala.
