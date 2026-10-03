# Debrief meeting untuk review owner

Debrief membaca catatan meeting yang sudah selesai dan membuat laporan lokal beserta kutipan sumber. Jalankan command ini terpisah dari join dan ingestion. Model memakai peran jawaban utama serta cadangan yang dikonfigurasi; Graphiti, embedding, browser, STT, dan TTS tidak diperlukan.

```sh
uv run fakeyou debrief --meeting MEETING_ID --persona examples/persona.example.json
uv run fakeyou debrief --meeting MEETING_ID --persona examples/persona.example.json --force
uv run fakeyou report --meeting MEETING_ID
```

Tambahkan `--owner-id` dan `--twin-id` bila identitas konfigurasi berbeda dari persona operator. Gunakan direktori record yang sama dengan worker melalui `FAKEYOU_PATHS__RECORD_DIR`; persona dan meeting ID harus cocok dengan scope historis. Jangan memakai persona contoh untuk membaca record milik persona lain.

## Sumber dan scope

Sumber berasal dari `record.jsonl`: transkrip final dan ucapan twin yang selesai diputar. Hasil tool tidak menjadi bukti ucapan. Identitas `unknown` tetap tidak diketahui; nama PIC di dalam ucapan merupakan klaim peserta. Ucapan twin tidak membuktikan persetujuan owner atau bahwa host mendengar seluruh audio. Audio terpotong tidak diberi kelanjutan yang dikarang.

Snapshot `meeting-context.json` memuat brief historis. Hash persona saat ini boleh berbeda untuk debrief bila scope owner, twin, dan meeting sama; brief lama tetap menjadi dasar tujuan. Join ulang memiliki persyaratan snapshot yang lebih ketat. Tanpa snapshot, status ingestion harus membuktikan meeting ID dan namespace persona yang cocok. Status boleh `pending` atau `not_requested`; ingestion tidak wajib selesai. Tanpa keduanya, scope tidak dapat dibuktikan dan command ditolak. Snapshot korup juga ditolak sebelum model dipanggil.

## Membaca laporan

Command mencetak metadata serta lokasi `debrief.json` dan `debrief.md` dalam direktori record. JSON merupakan artefak canonical; Markdown hanya tampilan yang dapat dibangun ulang tanpa model. Status awal selalu `completed` dan `review_status=unreviewed`. Validitas kutipan tidak menjamin bahwa ringkasan model memahami maknanya dengan benar.

Periksa setiap item, jenis sumber, participant ID, waktu sumber, dan kutipannya. Bedakan keputusan yang diklaim peserta, usulan, action item, pertanyaan terbuka, serta kebutuhan keputusan owner. Pembahasan perubahan budget, deadline, atau kontrak tetap memerlukan owner. `assignee_claim` dan `due_text` mempertahankan klaim sumber; waktu relatif seperti besok tidak diubah menjadi tanggal baru tanpa dasar pasti. Konflik tetap terlihat sebagai beberapa item dan tujuan terkait dapat berstatus `uncertain`.

Laporan tidak mengirim pesan, menghubungi owner, menjalankan action item, memberikan approval, atau menulis fakta ke memori. Owner memeriksa seluruh item sebelum memakai laporan sebagai dasar tindak lanjut. Perubahan hasil review belum memiliki command otomatis.

## Cache dan kegagalan

Sumber, snapshot, schema, dan kutipan yang sama memungkinkan penggunaan cache tanpa model. Markdown yang hilang atau berubah dirender ulang dari JSON. `--force` membuat percobaan baru. Provider gagal, output structured tidak valid, timeout, pembatalan, atau perubahan sumber tidak boleh mengganti JSON sukses sebelumnya. Metadata percobaan gagal disimpan pada `debrief-attempt.json`; isi error hanya nama kelasnya.

Laporan sesi menampilkan status debrief secara terpisah: `available`, `stale`, `missing`, atau `invalid`. Hash sumber atau snapshot yang berubah membuat laporan `stale`. Bukti palsu atau artefak korup membuatnya `invalid`. Kedua keadaan tersebut bukan hasil review owner yang lulus.

Penulisan Markdown dilakukan sebelum JSON atomik. Bila filesystem juga menggagalkan pemulihan JSON lama, berkas `debrief.json.*.rollback` dipertahankan dan laporan dianggap `invalid`; cache dan publikasi berikutnya diblokir. Setelah filesystem pulih, operator perlu memeriksa salinan itu terhadap record dan snapshot, memulihkan JSON lama, lalu mengulang command. Jangan menghapus satu-satunya backup ketika recovery belum selesai.

## Batas operasi

Record harus memiliki sumber ucapan yang valid. Transkrip dengan ID sama tetapi metadata atau teks berbeda ditolak. Setiap chunk dibatasi 40 sumber dan 16.000 karakter, overlap maksimal empat sumber, dan maksimal 20 chunk. Satu sumber terlalu panjang atau record yang melewati batas ditolak sebelum provider. Snapshot context dibatasi 1 MiB. Hasil gabungan maksimal 100 item, 40 tujuan/pertanyaan, dan 10 kutipan per item/finding; overflow ditolak tanpa membuang bukti diam-diam.

Anggaran total ekstraksi memakai `FAKEYOU_CONVERSATION__RESPONSE_TIMEOUT_SECONDS` dikali jumlah chunk. Timeout dan retry client mengikuti peran jawaban yang ada. Latency ekstraksi dilaporkan terpisah dari latency jawaban meeting; konfigurasi streaming pilot tetap mati.

Record lock yang sama dipakai oleh join, ingestion, forget, dan debrief. Operasi aktif menolak akses debrief/forget lain. `memory-forget --keep-record` mempertahankan record, snapshot, dan artefak debrief. Forget tanpa flag itu menghapus semuanya bersama direktori record. Stop pada jalur meeting tidak otomatis memanggil debrief.

Spesifikasi dan gate semantik ada di [plan debrief](../plans/meeting-debrief.md) dan [desain delegasi](../plans/meeting-delegation-design.md). Korpus sintetis atau JSON valid tidak menggantikan acceptance dua record Meet baru beserta review owner.
