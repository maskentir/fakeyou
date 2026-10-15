# Runbook pelabelan partisipasi dan uji bayangan

Protokol ini menghasilkan label layak pakai untuk harness replay (`evals/test_participation_replay_eval.py`) dan aturan main mode bayangan. Satu anotator saja tidak cukup; contoh `p07` corpus kecil berubah label sekali saat model membantah — label tunggal rapuh.

## Anotasi ganda plus agreement

- Dua anotator memberi label per final transkrip: `speak`, `silent`, atau `clarify`, plus alasan pendek.
- Hitung kappa Cohen antar anotator; ambang lolos kappa minimal 0.80.
- Baris sengketa otomatis berlabel `clarify`; jangan dipaksa lewat voting.
- Label merekam maksud manusia (`seharusnya twin apa`), bukan perilaku policy saat ini; selisih label vs replay adalah ukuran gap, bukan kegagalan harness.

## Replay rekaman

- Sumber: `record.jsonl` sesi plus file label di `evals/fixtures/participation-labels.json`.
- `replay_record` memutar final sesuai urutan lewat `ConversationLoop` rule-only dan mengumpulkan `policy_decided`.
- `score_replay` melaporkan agreement dan daftar miss; tidak ada ambang lolos global sebelum data multipihak tersedia.

## Mode bayangan

- `LoopInteraction(shadow=True)`: loop menjalankan `_decide` penuh (termasuk pregen Jev/Gemini) tetapi tidak pernah antre jawaban; tiap final yang lolos dicatat `transcript_suppressed` alasan `shadow`.
- Tanpa suara keluar; operator tetap wajib memastikan mic tidak terbuka mengikuti prosedur mute existing.
- Journal sesi bayangan adalah bahan label berikutnya: bandingkan `policy_decided` vs anotasi manusia untuk mengukur false-speak dan false-silent pada skala nyata tanpa risiko interupsi.
