# Tolok Ukur Percakapan

Dokumen ini menjelaskan cara mengulang pembandingan konfigurasi jawaban memakai fixture sama. Acuan utama berada pada [rencana pengembangan](../plans/next-development.md). Fixture replay berada pada [berkas fixture](../../evals/fixtures/acc-0930-replay.json).

## Tujuan

Setiap kandidat dinilai memakai sampel sama, ambang sama, serta artefak sebanding. Baseline beserta setiap kandidat wajib memiliki artefak sendiri agar perbandingan dapat diaudit.

## Prasyarat

Kredensial peran berasal melalui konfigurasi proyek. Nilai secret tidak dicetak ke layar atau laporan. Ketersediaan key beserta kredit diperiksa saat eksekusi. Jangan menetapkan penyedia tertentu sebagai prasyarat.

Penilaian persona memakai `EVAL_JUDGE_API_KEY` sendiri. Replay deterministik tetap berjalan tanpa judge. Status lewati atau judge gagal bukan bukti kelulusan kualitas.

## Fixture

Fixture memuat dua sesi beserta delapan giliran. Tiga run menghasilkan 24 giliran. Recall fixture wajib sama antar kandidat. Jangan mengganti fakta agar kandidat terlihat lebih baik.

## Baseline

Jalankan command berikut untuk baseline:

```sh
uv run --env-file .env pytest evals/test_replay_eval.py -m live -v -s \
  --basetemp=.runtime/benchmarks/reply-baseline
```

Artefak baseline tersimpan pada direktori sementara pada bawah `--basetemp` tersebut.

## Kandidat

Ulangi pengujian memakai satu konfigurasi kandidat tersedia. Perubahan hanya pada field `FAKEYOU_REPLY__*` atau `FAKEYOU_REPLY_FALLBACK__*` sesuai varian. Fixture tetap identik. Jumlah run tetap tiga atau 24 giliran. Urutan baseline beserta kandidat dibuat bergantian agar bias urutan terlihat.

```sh
uv run --env-file .env pytest evals/test_replay_eval.py -m live -v -s \
  --basetemp=.runtime/benchmarks/reply-candidate
```

Gunakan `--basetemp` berbeda agar artefak baseline beserta kandidat tidak saling tertimpa.

Kandidat terlewati akibat key atau kredit tidak tersedia dicatat sebagai belum diuji.

## Artefak

Setiap run live menyimpan dua berkas pada `tmp_path`:

- `summary.json` memuat `source_commit`, `fixture_sha256`, `runs`, `reply`, `reply_fallback`, `language`, batas waktu, ringkasan skor, jumlah sampel tiap pemeriksaan, ambang, hasil gate, serta status `passed`.
- `turns.json` memuat daftar giliran berisi `meeting_id`, `run_index`, `turn_index`, `latency_seconds`, `recall_called`, serta `checks`.

Hash fixture memakai SHA-256 atas byte berkas. Commit menjadi input eksplisit. Berkas disimpan sebelum pemeriksaan ambang sehingga hasil gagal tetap dapat dibandingkan.

Penyamaran secret memakai `settings.model_dump(mode="json")` lalu seleksi bagian peran beserta budget. Nilai `api_key` tersimpan sebagai `**********`. Userinfo, query, beserta fragment URL dihapus. Field kredensial pada `extra_body` disamarkan rekursif. Berkas mentah `.env` tidak disimpan.

## Ambang

Gate tetap sebagai berikut:

- Pass rate pemeriksaan wajib minimal 0,9.
- Pertanyaan penutup maksimal 20%.
- p95 respons agent maksimal 4 detik.

Artefak menjelaskan apa dibandingkan, sampel dipakai, serta gate lulus atau gagal. Sampel kurang atau bukti hilang berarti bukti belum cukup.
