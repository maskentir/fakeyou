# Operasi Worker Meeting

Prosedur operator untuk menjalankan worker meeting di container Docker
dari luar container: start, status, stop, dua meeting paralel, lanjutan
crash, job ingestion, serta checkpoint Postgres. Perintah
`fakeyou worker` berjalan di host dan memanggil Docker CLI; tidak ada
SDK Docker baru. Backend Docker lokal adalah implementasi pertama dari
antarmuka `WorkerBackend`; backend Kubernetes dapat menggantikannya
nanti tanpa mengubah CLI. Rencana milestone ada di
[worker-operations](../plans/worker-operations.md).

## Prasyarat

Bangun image worker dan siapkan `.env` dari contoh. Data live ada di
`.runtime/audio` (diabaikan Git): journal, record, brief, dan persona
yang terlihat dari dalam container.

```sh
docker compose build meeting-worker
```

Untuk dua worker paralel, nyalakan Postgres dulu lalu setel URL
checkpoint (rincian di bagian Postgres):

```sh
docker compose --profile production up -d --wait postgres
```

## Start

`--meeting-id` wajib pada `worker start`. Perintah ini meluncurkan
container detached `fakeyou-worker-<id>` (`--rm`) dengan label
`fakeyou.managed`, `fakeyou.meeting-id`, dan `fakeyou.role=worker`
lewat file override sementara, karena `compose run` tidak menerima
`--label`.

```sh
fakeyou worker start https://meet.google.com/xxx-yyyy-zzz --meeting-id OPC-1 \
  --audio --converse --persona .runtime/audio/persona.json
```

Contoh dengan brief:

```sh
fakeyou worker start https://meet.google.com/xxx-yyyy-zzz --meeting-id OPC-2 \
  --audio --converse --persona .runtime/audio/persona.json \
  --brief .runtime/audio/brief.json
```

Output JSON berisi `meeting_id`, `container_id`, dan
`container_name`. Start ditolak bila worker meeting itu masih jalan,
bila job ingest meeting itu masih jalan, atau bila file persona/brief
di luar mount yang terlihat container (`.runtime/audio`, `examples`).

Bila journal meeting menunjukkan sesi tak selesai (ada baris journal
tanpa `session_finished`), start otomatis menambah `--resume` ke
perintah `join` di dalam container, sehingga worker melanjutkan sesi
lama, bukan membuka sesi baru.

## Status

```sh
fakeyou worker status
fakeyou worker status OPC-1
```

Tanpa ID, perintah ini mendaftar semua meeting terkelola (urut abjad)
dari container berlabel plus journal di host. Dengan ID, perintah ini
menampilkan satu meeting. JSON status berisi `meeting_id`,
`container_id`, `container_name`, `container_state`, `phase`,
`terminal_reason`, `last_fault`, plus dua status berkas:

- `ingestion`: `not_requested`, `pending`, `interrupted`,
  `completed`, `failed`, atau `invalid` bila file status ada tetapi
  tidak terbaca; `null` bila file belum ada.
- `debrief`: `available`, `stale`, `missing`, atau `invalid`.

Pencarian container selalu memfilter `fakeyou.role=worker`, sehingga
job ingest tidak pernah tampil sebagai worker meeting.

## Stop

```sh
fakeyou worker stop OPC-1 --timeout 30
```

Stop mengirim `docker stop` (grace 30 detik bila timeout ≥ 30,
atau sebesar timeout), menunggu `session_finished` sampai timeout,
lalu `docker kill` bila container masih jalan (`escalated: true`). Sesi yang berhenti normal tercatat
`stopped_by_owner`. Stop pada meeting yang sudah ended melaporkan
alasan akhir tanpa error. Seperti status, stop hanya menyasar
container `role=worker`; job ingest yang sedang jalan tidak tersentuh.

## Dua meeting paralel

Jalankan `worker start` dua kali dengan ID berbeda. Isolasi per
meeting dijamin oleh ID yang sama di empat tempat: direktori record
`.runtime/audio/meetings/<id>/`, journal
`.runtime/audio/journals/<id>.jsonl`, thread checkpoint lifecycle
`<twin_id>:<id>` (plus thread percakapan hash per owner/twin/meeting),
dan profil browser guest per twin per meeting. Setiap twin hanya
menjawab meeting-nya.

Dua worker paralel wajib memakai checkpoint Postgres. Satu file
SQLite hanya untuk satu worker lokal; dua proses tidak boleh menulis
satu file checkpoint yang sama.

## Crash dan lanjutan

Worker yang mati mendadak (SIGKILL, OOM) meninggalkan record,
journal, dan checkpoint tanpa `session_finished`. `worker start`
dengan ID yang sama mendeteksi sesi tak selesai dan melanjutkan.
Langsung dari CLI juga bisa:

```sh
fakeyou join https://meet.google.com/xxx-yyyy-zzz --meeting-id OPC-1 --resume
```

Aturannya:

- Tanpa `--resume`, join ke meeting dengan sesi tak selesai ditolak;
  operator harus menyatakan lanjutan secara eksplisit.
- Dengan `--resume`, join ke meeting yang sudah ended ditolak;
  pakai ID baru.
- Bila meeting memakai brief/persona, lanjutan wajib memakai brief
  dan persona yang sama (digest snapshot dicocokkan); beda atau
  hilang ditolak sebelum resource dibuka.
- Lanjutan menulis penanda `session_resumed` di journal dan entri
  `resume` bernomor attempt di record, memakai thread checkpoint
  yang sama, dan masuk Meet dengan browser baru. Jawaban twin yang
  sudah tercatat sebagai `twin_speech` tidak diputar ulang.
- Bila journal hilang tetapi thread checkpoint masih hidup (misal
  journal terhapus manual), `--resume` tetap menulis penanda agar
  penanda dan checkpoint tetap konsisten.

## Ingestion sebagai job

```sh
fakeyou worker ingest OPC-1 --persona .runtime/audio/persona.json \
  --owner-id wops_owner --twin-id wops_twin
```

Perintah ini foreground sampai job selesai: menjalankan
`memory-ingest` di container terpisah `fakeyou-ingest-<id>`
(`role=ingest`, `--rm`). Syarat jalan: sesi sudah finished, tidak
ada worker atau job ingest lain untuk meeting itu, dan lock record
tidak live atau corrupt. Persona harus terlihat di dalam container.
Bila identitas persona beda dari identitas di `.env`, teruskan
`--owner-id`/`--twin-id`; tanpa itu container memakai identitas
`.env` dan ingestion ditolak karena tidak cocok.

Hasil job dibaca dari file status ingestion di host. JSON berisi
`meeting_id` dan `status`; CLI exit 1 bila status bukan
`completed`. Status `interrupted` berarti job terhenti di tengah
jalan (misal Ctrl+C) dan dapat dilanjutkan dengan menjalankan
perintah yang sama lagi; episode yang sudah masuk tidak digandakan.
Ctrl+C pada host dilaporkan bersih sebagai error interupsi tanpa
traceback; container menulis statusnya sendiri. Bila job gagal
sebelum menulis status (misal Compose gagal), hasilnya `unknown`,
bukan status basi dari run sebelumnya.

## Checkpoint Postgres

Bila `FAKEYOU_CHECKPOINT__URL` diset (lihat `.env.example`), CLI
memakai `AsyncPostgresSaver` resmi beserta `setup()`; bila kosong,
CLI memakai SQLite lokal. Worker Compose meneruskan URL lewat
`FAKEYOU_COMPOSE_WORKER_CHECKPOINT_URL`:

- CLI di host menjangkau Postgres lewat `localhost:5432` (port
  dipetakan ke loopback).
- Container worker menjangkau Postgres lewat nama service
  `postgres:5432` di network Compose.

Contoh nilai (ganti password sesuai `.env`):

```sh
FAKEYOU_CHECKPOINT__URL=postgresql://fakeyou:password@127.0.0.1:5432/fakeyou
FAKEYOU_COMPOSE_WORKER_CHECKPOINT_URL=postgresql://fakeyou:password@postgres:5432/fakeyou
```

Service `postgres` ada di profil `production` dengan healthcheck.
Service worker sengaja tanpa `depends_on` agar default SQLite tetap
jalan tanpa Postgres; bila URL diset, operator menyalakan Postgres
dulu. Tes Postgres membiarkan service tetap jalan; itu disengaja,
bukan kebocoran.

## Lock lease lintas container

Lock record dan profil memakai lease JSON berisi `host`, `pid`,
`purpose`, `token`, dan `heartbeat`, bukan kepemilikan PID mentah
yang tidak berlaku lintas container. Heartbeat diperbarui berkala
oleh pemegang; lease basi (heartbeat lebih tua dari 60 detik) boleh
diambil alih proses mana pun. Pada host yang sama dengan heartbeat
segar, PID masih diperiksa sebagai pengaman tambahan. File lock
record ada di sebelah direktori meeting:
`.runtime/audio/meetings/<id>.lock`.

Perintah bantu `describe_lease` meringkas keadaan lock menjadi
`absent`, `live`, `stale`, atau `corrupt`. `worker ingest`
menolak lock `live` dan `corrupt`; lock `stale` diteruskan ke
container yang akan mengambil alih. File lock lama tanpa field
`host` memakai semantik PID saja; tabrakan PID lintas container
pada file lama adalah risiko yang diterima. Sebelum upgrade image,
hentikan semua worker dulu (single-image rollout; tidak ada penulis
legacy yang hidup). Hapus file lock manual hanya bila pemiliknya
dipastikan sudah mati.

## Profil browser

Setiap worker memakai direktori profil guest per twin per meeting,
sehingga dua meeting paralel tidak berbagi state browser. Profil
signed-in eksplisit (`--profile-dir`) dikunci eksklusif; worker
kedua pada profil yang sama ditolak sampai profil bebas.

## Batas dan risiko

- Backend Kubernetes belum ada. Antarmuka `WorkerBackend` sudah
  siap diganti; yang masih dibutuhkan adalah pemetaan Job/Deployment,
  selector label, stream log, volume persisten untuk `.runtime/audio`,
  secret, dan DNS Postgres di cluster.
- Lock legacy tanpa `host` rentan tabrakan PID; upgrade image
  mensyaratkan stop dulu.
- Hasil job ingest `unknown` berarti tidak ada status baru yang
  ditulis; periksa output container sebelum mengulang.
- Latency live masih di atas target (lihat status proyek); operasi
  worker tidak mengubah jalur audio, agent, atau TTS.
