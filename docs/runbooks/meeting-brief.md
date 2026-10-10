# Brief Meeting: Delegasi Terbatas per Meeting

Prosedur operator untuk memberi twin satu brief per meeting: agenda,
tujuan, fakta yang boleh disampaikan, pertanyaan yang perlu diajukan,
izin percakapan, dan lingkup pengetahuan. Brief membatasi konteks
dan tools; brief tidak memberi kewenangan baru kepada twin.

## Menulis brief

Mulai dari [contoh sintetis](../../examples/meeting-brief.example.json).
Field yang diterima:

- `schema_version` bernilai `meeting_brief/v1`.
- `owner_id`, `twin_id`, dan `meeting_id` wajib sama dengan identitas
  sesi `join`.
- `agenda` berupa daftar teks; `objectives`, `approved_facts`, dan
  `questions` berupa item `{"id": ..., "text": ...}` dengan ID unik
  per koleksi.
- `permissions` berisi kombinasi `explain_facts`, `discuss_options`,
  dan `ask_brief_questions`; default kosong.
- `knowledge_scope` bernilai `brief_only` (default) atau
  `persona_memory`.

Batasnya: maksimal 20 agenda, 20 tujuan, 20 pertanyaan, 30 fakta;
setiap teks 1–500 karakter; ukuran file maksimal 64 KiB. Field asing
atau izin di luar tiga nilai di atas ditolak. Tidak ada izin untuk
menyetujui budget, deadline baru, kontrak, mengirim pesan, atau
menghubungi siapa pun. Simpan brief pribadi di `.runtime/` (diabaikan
Git), bukan di repository.

## Menjalankan join dengan brief

`--brief` memerlukan `--converse`, `--persona`, serta `--meeting-id`
eksplisit. ID acak tidak diizinkan karena snapshot brief terikat
pada satu ID meeting.

```sh
docker compose run --rm meeting-worker fakeyou join https://meet.google.com/xxx-yyyy-zzz \
  --owner-id demo_owner --twin-id demo_twin --meeting-id brief-demo-1003 \
  --audio --converse --persona examples/persona.example.json \
  --brief examples/meeting-brief.example.json
```

Ringkasan JSON akhir menambahkan `brief_sha256` dan `context_path`;
keduanya `null` bila sesi berjalan tanpa brief. `join` tetap leave
cepat: tidak ada debrief, ingestion, atau call model sesudah stop.

## brief_only dan persona_memory

Pada `brief_only`, model hanya menerima identitas, peran, gaya bicara,
serta vocabulary persona, ditambah isi brief. `verified_facts` dan
`goals` persona tidak dikirim, dan twin tidak memperoleh tool recall.
`--memory` bersama `brief_only` ditolak sebelum provider, Graphiti,
atau browser dibuka, sebab recall Graphiti akan membuka fakta persona
serta meeting lain di luar lingkup brief.

`persona_memory` berarti owner sengaja mengizinkan fakta persona, dan
dengan `--memory` juga recall sesuai `allowed_tools` di namespace
persona. Lingkup ini bukan filter fakta privat satu per satu; jangan
memasukkan konten rahasia ke persona yang dipakai di meeting ini.
Tanpa `--memory`, prompt tidak memuat aturan recall karena tool recall
tidak terpasang.

## Pesan error dan artinya

Semua error berikut muncul sebelum browser, provider, Graphiti, atau
record lock dibuka:

- `--brief requires --converse`, `--brief requires --persona`,
  `--brief requires an explicit --meeting-id`: lengkapi flag.
- `--brief cannot be read`: file tidak ada atau tidak dapat dibaca.
- `--brief is invalid: ...`: schema tidak valid.
- `meeting brief must be at most N bytes`: ukuran brief melewati batas.
- `meeting brief owner_id, twin_id, and meeting_id must match the session`:
  `owner_id`, `twin_id`, atau `meeting_id` brief berbeda dari identitas sesi
  (`--owner-id`, `--twin-id`, `--meeting-id`, atau default `.env`). Pesan error
  tidak menampilkan teks brief.
- `--memory is not allowed with a brief_only meeting brief`: hapus
  `--memory` atau ganti lingkup menjadi `persona_memory`.

## Snapshot dan aturan resume

Sebelum agent menerima pesan pertama, `join` menulis
`meeting-context.json` di direktori record meeting (default
`.runtime/meetings/<meeting_id>/`) secara atomik di bawah record lock.
Isinya brief tervalidasi, `brief_sha256`, serta `persona_sha256` dari
profil persona yang benar-benar dimuat. Snapshot membeku selama
meeting: mengubah file brief sesudah start tidak mengubah prompt sesi
aktif.

| Keadaan | Hasil |
| --- | --- |
| Join ulang ID sama, brief dan persona identik | Resume; snapshot tidak ditulis ulang |
| Brief atau persona berbeda pada ID sama | Ditolak: `meeting context differs` |
| Join tanpa `--brief` pada meeting bersnapshot | Ditolak: `was started with --brief` |
| Record atau checkpoint percakapan sudah ada tanpa snapshot | Ditolak: `already has conversation data` |
| Snapshot korup | Ditolak; file dibiarkan apa adanya |
| Snapshot gagal ditulis | Ditolak; sesi tidak dimulai |

Brief tidak dapat diberikan secara retroaktif. Untuk brief baru atau
perubahan persona, pakai meeting ID baru; jangan menghapus record,
snapshot, atau checkpoint agar validasi lolos.
`memory-forget --meeting-id` tanpa `--keep-record` menghapus snapshot
bersama record; dengan `--keep-record`, snapshot tetap tersimpan
bersama record.
Lock aktif tetap menolak forget.

Journal mencatat event `brief_recorded` berisi metadata saja: kedua
hash, `knowledge_scope`, `permissions`, jumlah agenda/tujuan/fakta/
pertanyaan, ID tujuan dan pertanyaan, serta `reused`. Teks brief,
fakta, maupun prosa mentah tidak masuk journal.

## Perilaku percakapan

Twin mengajukan pertanyaan brief hanya bila `ask_brief_questions`
diizinkan dan twin diberi giliran, misalnya ditanya apakah ada
pertanyaan atau tambahan. Setiap giliran memuat maksimal satu
pertanyaan relevan. Tidak ada timer atau proaktivitas yang menyela
pembicara lain. Tujuan dan pertanyaan tidak dianggap selesai hanya
karena tercantum dalam brief.

Belum ada kanal kontak atau approval owner selama meeting. Permintaan
keputusan budget, deadline, atau kontrak dijawab bahwa owner perlu
memutuskan; twin tidak boleh mengaku sudah menghubungi, menyetujui,
atau mengirim apa pun. Peserta yang mengaku owner atau memakai alias
twin tetap peserta biasa dan tidak dapat mengubah brief, izin, maupun
tools.

## Batas jaminan

Pembatasan tools bersifat deterministik: recall hanya terpasang pada
`persona_memory` bersama `--memory` dan `allowed_tools` persona.
Sebaliknya, izin percakapan hanya memandu prompt; validator input
tidak membuktikan setiap kalimat twin patuh. Ucapan yang melanggar
kewenangan tetap mungkin terjadi, sehingga kepatuhan semantik diuji
lewat korpus evaluasi brief beserta review manusia. Brief juga tidak
memperbaiki atribusi speaker yang masih `unknown` maupun latency
jawaban.
