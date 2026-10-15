# Retensi bukti visual

Retensi bukti visual dikelola dua tingkat. Frame mentah dihapus lebih dulu, dibatasi oleh `FAKEYOU_VISUAL__RETENTION_RAW_DAYS` (bawaan 7). Teks caption disimpan lebih lama, dibatasi oleh `FAKEYOU_VISUAL__RETENTION_TEXT_DAYS` (bawaan 30). Variabel lengkapnya ada di [referensi environment](../reference/environment.md).

Pembersihan manual:

```sh
fakeyou visual purge-expired --days 7
```

`--days` hanya mengganti batas frame mentah. Batas teks tetap mengikuti `FAKEYOU_VISUAL__RETENTION_TEXT_DAYS`. Bila `--days` lebih besar dari batas teks, frame bertahan lebih lama daripada indeks caption-nya. Jalankan dengan nilai yang tidak melebihi batas teks, kecuali itu memang disengaja. Tanpa `--root`, perintah memakai `FAKEYOU_PATHS__RECORD_DIR`.
