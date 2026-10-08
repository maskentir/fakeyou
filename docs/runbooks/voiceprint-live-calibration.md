# Kalibrasi Live Voiceprint (Item 17, Task 5)

Sesi: enroll pemilik + manusia, kunci θ + margin pada data live,
buktikan TTS twin tak terverifikasi dan overlap → unknown.
Flag `voiceprint_enabled` + model ERes2Net dalam image worker;
default tetap mati di luar sesi.

## Persiapan async (tanpa room, tanpa bebarengan)

Setiap manusia (pemilik + ≥2) merekam 3 klip di ruangan hening:

- Format bebas (voice memo HP/laptop boleh); konversi ke
  WAV 16 kHz mono 16-bit dilakukan operator.
- Tiap klip ≥3 detik, kalimat bervariasi.
- Klip 1 DIAWALI persetujuan lisan: "Saya setuju enrollment
  voiceprint untuk fakeyou" + satu kalimat bebas.
- Taruh di `.runtime/audio/enroll/<nama>-1.wav` (diabaikan Git).

Operator lalu: konversi (`afconvert`), `fakeyou voiceprint enroll`,
`verify` satu klip tahan-luar per orang, enroll negatif TTS
(disintesis via path TTS repo dengan voice twin yang sama),
HAPUS klip enrollment mentah setelah enroll+verify lolos
(tanpa consent retensi terpisah, audio mentah tak disimpan).

## Kasut uji twin-only (tanpa manusia, jalan duluan)

| ID | Uji | Harapan |
| --- | --- | --- |
| T1 | Twin ping-pong ≥20 giliran | Semua `voiceprint_decided` unknown/negative; nol verified |
| T2 | Twin ucap kalimat <3 dtk | Tanpa skor (dibuang pemanggil), unknown |
| T3 | Dua twin overlap 2–3 dtk | unknown |
| T4 | Twin meniru frasa enrollment manusia | TIDAK verified (uji impostor) |
| P1 | Latensi keputusan per turn (selisih `at` transcript_finalized → voiceprint_decided) | p95 < 500 mdetik, tak ada frame audio tertahan |
| S1 | Audit privasi pasca-sesi | Tiap baris punya consent_ts + policy_version; nol WAV di meeting dir; purge-expired jalan |

## Kasut uji manusia (berurutan, hemat waktu)

Manusia di ruang berbeda. Setiap giliran solo menyebut
nama + nomor ("Kalimat tiga dari Budi") — transkrip
menjadi ground truth otomatis.

| ID | Uji | Harapan |
| --- | --- | --- |
| H1 | Consent lisan di room per orang (dicatat waktu) | Pemetaan waktu ↔ `consent_ts` enroll |
| H2 | Solo bergiliran bernomor, ≥10/orang | verified + telemetri s1/margin |
| H3 | Overlap 2 manusia 2–3 dtk, 4–6× | unknown |
| H4 | Mute 30 dtk lalu solo | Mute tanpa turn; solo verified |
| H5 | Satu manusia keluar + join lagi (nama sama) | uuid baru, suara tetap verified ke orang sama |
| H6 | Satu manusia rename tengah sesi | Suara tetap verified (bukti independen-nama) |
| H7 | (opsional) Suara tak-terdaftar 3 solo | unknown |

Target ronde 1: ≥30 solo manusia berskor, ≥20 giliran twin,
4–6 overlap. Gate penuh 0,98 (≥190 solo tanpa-salah,
speaker-disjoint) butuh sesi lanjutan — ronde ini
mengunci θ + margin sementara + membuktikan mekanisme.

## Analisis

```sh
fakeyou report --meeting <id> --journal-dir .runtime/audio/journals
```

Baca event `voiceprint_decided` (outcome, s1/s2, margin,
durasi): presisi verified, cakupan solo→verified,
overlap→unknown, twin false-verify (=0 wajib), distribusi
margin. Hasil + θ/margin terkunci dicatat di adendum
laporan acceptance.
