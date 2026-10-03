# Desain brief, delegasi, dan debrief meeting

**Status:** desain yang disetujui pengguna pada 3 Oktober 2026; implementasi belum dijalankan oleh penulis plan. Baseline diperiksa: `master`, `21af795`, working tree bersih. Tidak ada asumsi tentang remote. Eksekusi dipecah menjadi [brief dan delegasi](meeting-brief.md) lalu [debrief berbukti](meeting-debrief.md); [handoff Muse](meeting-delegation-handoff.md) membawa prompt orkestrasi.

## Hasil untuk owner

Owner memberi satu brief untuk satu meeting: agenda, tujuan, fakta yang boleh disampaikan, pertanyaan yang perlu diajukan, dan lingkup pengetahuan. Twin menjawab ketika memperoleh giliran sesuai policy existing. Sesudah meeting, owner menerima usulan, keputusan yang dinyatakan peserta, action item, pertanyaan terbuka, serta permintaan keputusan owner dengan kutipan sumber.

Contoh: “Wakili saya di kickoff Orchid. Cari tahu blocker dan kebutuhan vendor. Jangan menyetujui perubahan deadline. Setelah selesai, tampilkan keputusan yang perlu saya ambil.” Tidak ada janji bahwa twin otomatis menanyakan semua pertanyaan atau menghubungi owner; tujuan yang belum tercapai tetap terbuka di debrief.

## Kondisi awal dan batas bukti

- [Hardening acceptance](../reports/2026-10-03-hardening-acceptance.md) mencatat cancellation/stop dan resume ingestion lulus; pendengaran host masih bersyarat pada laporan itu.
- [Acceptance multipihak](../reports/2026-10-03-multiparty-acceptance.md) mencatat 62 utterance/26 jawaban, routing explicit-address lulus, speaker tetap unknown seluruhnya, dan p95 ucapan→PCM 10,0–10,6 detik. Angka ini hasil laporan, bukan pengukuran ulang.
- [Gate media](../reports/2026-10-03-media-gate.md) gagal alignment. Brief/debrief tidak memperbaiki capture atau mengubah unknown menjadi nama orang.
- `PersonaProfile` sekarang mempunyai goals/verified_facts permanen, tetapi tidak ada brief per meeting. `SOURCE_RULES` bahkan memperbolehkan kewenangan dari verified fact; ini harus diperbaiki agar prosa fact/recall tidak mengubah daftar tool atau memberikan izin keputusan baru.
- `record.jsonl` menyimpan transcript, hasil tool, dan ucapan twin setelah playback selesai. Jawaban yang dibatalkan/terpotong tidak otomatis mempunyai teks ucapan selesai; debrief tidak boleh mengaku seluruh jawaban tersebut terdengar.

## Batasan global

- Versi tetap: Python 3.12, LangChain 1.4.2, LangGraph 1.2.12, Graphiti 0.30.2, Pipecat 1.12.0. Tanpa dependency baru atau perubahan lockfile.
- Python Inggris tanpa komentar/docstring baru; dokumentasi Bahasa Indonesia. Tanpa shim legacy, alias konfigurasi, atau re-export migrasi.
- Tidak mengubah `.env`, backup, stash, persona privat, data `acc2`/`acc3`, atau record acceptance lama. Tes menggunakan namespace, meeting ID, dan direktori run baru.
- Streaming kalimat tetap mati pada pilot. Cancellation, stop, record lock/finalisasi, namespace, cap dua kalimat, dan routing multipihak dipertahankan.
- Gate lama tidak dilonggarkan: replay pass minimal 0,9, pertanyaan penutup maksimal 20%, p95 agent maksimal 4 detik; live median akhir ucapan→PCM pertama maksimal 3 detik dan p95 maksimal 6 detik.
- Tanpa `xfail` baru, penghapusan kasus gagal, penghilangan sampel lambat, atau skip yang dihitung sebagai kelulusan.
- Commit lokal hanya oleh orkestrator setelah review; tanpa push/merge atau pesan ke pihak luar tanpa instruksi pengguna.

## A. Brief dan delegasi v1

### Data dan scope

`MeetingBrief` ialah Pydantic frozen, extra forbid, input tersembunyi pada error. Field: `schema_version` literal `meeting_brief/v1`, `owner_id`, `twin_id`, `meeting_id` memakai `SessionId`; `agenda`, `objectives`, `approved_facts`, `questions`; `permissions`; `knowledge_scope`. Maksimal 20 agenda/tujuan/pertanyaan dan 30 fakta; tiap item mempunyai ID unik dalam koleksinya dan teks 1–500 karakter. Agenda berupa daftar teks, koleksi lainnya memakai `BriefItem(id: SessionId, text: str)`.

`permissions` berupa set dari `explain_facts`, `discuss_options`, `ask_brief_questions`, default kosong. Ini kemampuan percakapan, bukan izin menjalankan efek eksternal. Tidak ada field yang dapat mengaktifkan persetujuan budget, deadline baru, kontrak, atau kirim pesan. Field tidak dikenal/kemampuan lain ditolak.

Contoh lengkap yang harus diterbitkan sebagai JSON sintetis pada implementasi:

```json
{
  "schema_version": "meeting_brief/v1",
  "owner_id": "demo_owner",
  "twin_id": "demo_twin",
  "meeting_id": "brief-demo-1003",
  "agenda": ["Kickoff Orchid dan kebutuhan vendor"],
  "objectives": [{"id": "blockers", "text": "Cari tahu blocker kickoff."}],
  "approved_facts": [{"id": "demo-date", "text": "Demo Orchid sudah dijadwalkan Jumat."}],
  "questions": [{"id": "vendor-needs", "text": "Apa yang masih dibutuhkan vendor?"}],
  "permissions": ["explain_facts", "discuss_options", "ask_brief_questions"],
  "knowledge_scope": "brief_only"
}
```

`knowledge_scope` literal `brief_only` atau `persona_memory`, default `brief_only`. Pada brief_only, model hanya menerima identitas/gaya persona dan isi brief yang diizinkan; verified_facts/goals persona serta recall tidak disertakan. `--memory` bersama brief_only ditolak sebelum koneksi provider/browser. Persona_memory berarti owner sengaja mengizinkan fakta persona dan recall sesuai allowed_tools serta namespace persona; ini bukan filter fakta privat satu per satu. Konten rahasia yang tidak boleh disampaikan jangan dimasukkan dalam lingkup ini.

Agenda/tujuan/fakta/pertanyaan adalah data, bukan instruksi yang dapat memperluas tools. Alias twin dan nama peserta tidak menjadi owner terautentikasi. Operator lokal yang memuat brief adalah sumber konfigurasi; hash hanya menjaga integritas snapshot, bukan bukti autentikasi orang di Meet.

### Runtime dan persistensi

- Tambahkan argumen canonical `join --brief PATH`; membutuhkan persona, converse, dan ID meeting eksplisit. Tidak ada alias env atau command generik konfigurasi baru.
- Validasi scope dan batas input sebelum browser/API. Simpan `meeting-context.json` dalam direktori record dengan brief tervalidasi, SHA-256 JSON canonical brief, dan hash JSON canonical persona dari objek yang benar-benar dimuat. Tidak menyalin seluruh persona atau secret. Urutan set permissions/allowed_tools disortir sebelum hash; whitespace file asal tidak mengubah identitas konteks.
- Simpan snapshot atomik di bawah record lock sebelum agent menerima pesan. Snapshot membeku selama meeting; perubahan file asal tidak mengubah sesi aktif. Snapshot berbeda pada meeting ID yang sama ditolak.
- Record/checkpoint yang sudah mempunyai percakapan tanpa snapshot tidak dapat diberi brief secara retroaktif. Gunakan meeting ID baru; jangan menghapus data atau checkpoint untuk membuat validasi lulus. Resume ID sama hanya dengan snapshot/context yang cocok.
- Daftar tools dibangun dari irisan allowed_tools persona dan lingkup brief. Saat ini hanya recall; tidak ada aksi eksternal yang ditambahkan. Fakta atau ucapan peserta tidak dapat menambah tools.
- Permission percakapan memandu prompt native; validator input tidak membuktikan semantik semua kalimat output. Ucapan melanggar kewenangan tetap dapat gagal evaluasi. Jangan menyebut prosa/label model sebagai guard deterministik untuk ucapan.
- Pertanyaan brief hanya diajukan ketika twin diberi giliran, maksimal satu pertanyaan yang relevan. Tidak ada timer/proaktivitas yang menerobos occupied floor. Tujuan/pertanyaan tidak diberi status selesai hanya karena tercantum pada brief.
- Permintaan keputusan dijawab dengan kebutuhan konfirmasi owner, tanpa “sudah saya hubungi/approve/kirim”. Daftar permintaan keputusan dibangun setelah meeting dari sumber; belum ada kanal notifikasi atau approval selama meeting.
- `join` tetap leave cepat tanpa model debrief/ingestion sesudah stop. Journal hanya metadata context/hash/ID/permission; tidak mencatat brief/fakta/teks mentah.

## B. Debrief v1

### Sumber dan keluaran

Tambahkan command operator `debrief --meeting ID --persona PATH`, dengan scope owner/twin tervalidasi. Jalankan terpisah dari join maupun memory-ingest; tidak perlu Graphiti/embedding. Debrief dapat memakai record historis valid tanpa brief, tetapi tujuan diberi status not_available dan batas sumber ditampilkan.

Ruling eksekusi: tanpa snapshot brief, scope historis harus dibuktikan oleh meeting_id/group_id status ingestion yang cocok dengan namespace persona. Status boleh pending/not_requested; ingestion tidak wajib selesai. Record tanpa kedua sumber scope itu ditolak sebelum provider, karena persona yang dimuat sekarang tidak membuktikan siapa pemilik record lama. Dengan snapshot, scope dan digest snapshot tetap diperiksa; hash persona saat ini tidak mengganti brief historis.

Baca record yang sudah selesai di bawah record lock; jangan menulis ulang `record.jsonl`. Sumber model hanya TranscriptEntry dan TwinSpeechEntry, bukan tool output yang seolah ucapan peserta. ID sumber transcript ialah `transcript:<id>`, ucapan twin ialah `twin_speech:<nomor_baris_1_based>` pada byte record yang di-hash. Nomor baris menghitung seluruh baris record; blank line yang ada tetap dihitung. Twin speech berarti playback selesai menurut aplikasi, bukan verifikasi pendengaran host.

`DebriefItem` mempunyai kind `decision_claim`, `proposal`, `action_item`, `open_question`, atau `owner_decision`; teks, evidence nonkosong, optional assignee_claim/due_text, serta status `stated`, `proposed`, `needs_owner`, atau `uncertain`. `EvidenceRef` memuat entry_id dan quote persis sebagai substring nonkosong sumber. Nama PIC yang disebut peserta ialah klaim; participant ID berasal dari metadata sumber dan tidak diisi model berdasarkan nama.

Draft model boleh salah. Validator deterministik menolak ID palsu, quote tidak cocok, sumber lintas meeting, status tidak sah, dan attributed speaker yang tidak sesuai metadata. Kutipan cocok belum membuktikan entailment ringkasan; review independen tetap diperlukan. Semua artefak awal bertanda `review_status=unreviewed`.

Klaim “owner sudah setuju” dari participant unknown tetap klaim peserta, bukan approval operator. Ucapan twin tidak dapat menjadi bukti persetujuan owner. Pembahasan perubahan budget/deadline/kontrak menghasilkan needs_owner kecuali referensinya hanya menerangkan fakta yang telah dikonfirmasi dalam snapshot brief; jangan mengubah laporan menjadi mekanisme pemberian kewenangan.

Owner request adalah item lokal needs_owner, bukan bukti bahwa owner telah dihubungi. `assignee_claim`/`due_text` mempertahankan bentuk sumber; tanggal relatif tanpa jangkar pasti tidak dinormalisasi menjadi tanggal baru. Keputusan yang saling bertentangan ditampilkan beserta kedua sumber; debrief tidak mengganti koreksi owner note atau memutasi memori.

### Proses dan batas

- Pakai structured output native LangChain yang diprobe dengan model produksi. Tidak menambah model judge di setiap jawaban meeting dan tidak mengubah agent live menjadi format JSON.
- Pecah sumber secara deterministik maksimal 40 entry/16.000 karakter teks per chunk, overlap maksimal empat entry. Entry lebih besar dari batas atau lebih dari 20 chunk ditolak dengan alasan eksplisit sebelum provider; tidak dipotong diam-diam. Ambang adalah batas pilot, bukan dukungan semua meeting panjang.
- Merge hasil tanpa LLM merge tambahan: dedupe hanya item identik dengan refs/status sama; pertahankan perbedaan/konflik. Semua chunk harus selesai dan lolos validasi sebelum artefak completed diterbitkan. Context yang melintasi chunk tetap bisa terlewat; evaluasi wajib mencakupnya.
- Pilot membatasi laporan gabungan maksimal 100 item; overflow menjadi kegagalan eksplisit, bukan pemotongan. Setiap ID objective/question brief harus muncul pada hasil akhir; finding yang tidak diberikan model menjadi uncertain, tidak dianggap terpenuhi.
- Cocokkan objective/question brief ke bukti sumber melalui draft terstruktur: `addressed`, `open`, atau `uncertain`; addressed memerlukan evidence. Menanyakan sesuatu bukan bukti memperoleh jawaban. Tidak adanya item bukan bukti tujuan terpenuhi.
- Simpan `debrief.json` dan tampilan Markdown `debrief.md` dalam direktori record, dengan hash record/context, schema/version, provider/model tanpa credential, jumlah sumber/chunk, coverage, dan status. JSON ialah artefak canonical; Markdown render darinya tanpa call model.
- Publication memakai fsync/replace, JSON canonical ditulis terakhir sebagai tanda lengkap. Renderer dapat dibangun ulang bila crash di antara dua file. Model gagal, cancellation, output invalid, record kosong atau berubah tidak menghasilkan completed atau menimpa laporan sebelumnya. Metadata percobaan gagal dipisahkan dari laporan sukses.
- Pengulangan tanpa `--force` memakai cache hanya jika source/context hash dan schema cocok. `--force` boleh meregenerasi tetapi kegagalan tetap mempertahankan laporan lama. Report menyatakan stale bila hash berbeda; tidak menampilkannya sebagai hasil record terbaru.
- Tidak ada auto-ingest debrief menjadi fakta owner, kirim email/chat, approval endpoint, UI/dashboard, kalender, atau scheduler pada milestone ini. `memory-forget` menjaga artefak sesuai keep-record dan menolak penghapusan ketika lock debrief aktif.

## Acceptance

A: validasi dan scope sebelum resource; nol mutating tool dari instruksi peserta/fakta; korpus brief 12 kasus × 3 dan trust lama 24 × 3 direview. Minimal 90% output berguna dan nol pelanggaran serius; izin hilang tidak boleh menghasilkan approve/follow-up palsu. Dua sesi baru, minimal 20 jawaban total, dua brief berbeda, dan satu percobaan perubahan kewenangan lewat ucapan.

B: korpus delapan record sintetis mencakup konflik, unknown, twin terpotong, instruksi palsu, owner-decision, relatif waktu, chunk boundary, dan missing/corrupt source. Semua reference valid; nol klaim keputusan/identitas/aksi selesai yang tidak didukung; minimal 90% item wajib ground truth tercakup. Review seluruh item, bukan skor substring saja. Debrief dua record live A juga direview owner dengan bukti.

Verdict dipisahkan: brief/scope, ucapan/delegasi, debrief/provenance, recall/trust, stop/ingestion, fungsi live, dan performa. Provider/host belum tersedia berarti gate terkait terbuka. Atribusi unknown dan performa yang masih gagal tidak disembunyikan atau dianggap selesai oleh fitur baru.
