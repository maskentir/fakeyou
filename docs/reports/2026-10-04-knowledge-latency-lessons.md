# Pelajaran Vexa dan Amazon LMA: pengetahuan pada jawaban live tanpa perjalanan model kedua

Tanggal: 4 Oktober 2026. Lingkup: pembacaan source Vexa `dba990b4` dan Amazon LMA `a23420ad`, ditambah pemecahan ulang artefak corpus Gemini milik fakeyou. Tidak menjalankan provider, Meet, atau benchmark baru. Melanjutkan [pelajaran latensi 2 Oktober](2026-10-02-latency-lessons.md), yang membahas streaming kalimat dan playback; laporan ini membahas jalur pengetahuan dan grounding. Dipakai oleh [plan recall satu perjalanan model](../plans/recall-latency.md).

**Kesimpulan:** pindahkan retrieval memori dari loop tool model ke kode sebelum panggilan model. Vexa melakukan perpindahan yang sama dua kali karena alasan latency dan keandalan; LMA awalnya memakai prefetch satu panggilan, sedangkan desain agentic penggantinya dianggarkan 5–10 detik dan memerlukan filler. Biarkan kode, bukan model, memilih cabang "ada evidence" atau "tidak ada catatan". Tool recall cukup menjadi hop kedua bila evidence kosong atau lemah.

## Bukti fakeyou

Corpus trust 24×3, prompt identik, Gemini native, recall memakai storage sintetis:

| Model | Giliran 1 call | Giliran recall (2 call) | Per call |
| --- | --- | --- | --- |
| `gemini-3.8-flash` | n=20, p50 1,98 s, p95 2,44 s | n=53, p50 4,23 s, p95 5,94 s | ~2,0 s |
| `gemini-3.5-flash-lite` | n=33, p50 0,78 s, p95 0,88 s | n=42, p50 1,56 s, p95 1,78 s | ~0,8 s |

Durasi tool median ~1 ms; selisih berasal dari perjalanan model kedua. `flash-lite` gagal useful trust (56/72) terutama karena 12 pair melewati recall wajib lalu memberi penolakan generik. Sumber: `.runtime/meeting-delegation/G2-*/trust-live/turns.json`, review `resume-g2-*-review.md`.

## Vexa

Vexa tidak mempunyai agent yang menjawab dengan suara secara mandiri. Jalur `speak` ada di bot, tetapi tidak terhubung lewat API open-source. Pelajarannya berasal dari copilot meeting dan chat agent.

| Bukti source | Perilaku | Pelajaran bagi fakeyou |
| --- | --- | --- |
| `core/agent/worker/meeting.py:245–256` | Copilot memakai satu panggilan completion langsung tanpa tool. Docstring: giliran lama yang diberi tool menjelajah knowledge graph setiap beat dan menjadi "the dominant per-beat latency" | Retrieval di dalam loop tool adalah biaya utama; satu panggilan dengan konteks siap pakai lebih cepat |
| `core/agent/control_plane/api.py:639–716`, `:814–864`; commit `241d68db` | Chat agent menghapus tool MCP `meeting.read_transcript` yang menggantung, lalu menyuntikkan transkrip di server sebelum panggilan model; kegagalan fetch tidak menggagalkan giliran | Prefetch evidence di kode, injeksi ke konteks, degradasi aman bila search gagal |
| `core/agent/control_plane/meeting_steering.py:57–117`, dipilih di `api.py:705–716` | Template berbeda per fase; bila data kosong, kode memilih template "Tell the user plainly that no record … exists — do not reconstruct or invent" | Kode memilih cabang grounded atau tanpa catatan; model tidak perlu memutuskan sendiri apakah boleh menjawab "tidak tahu" |
| `core/agent/worker/meeting.py:169–171`, `:286–288` | Output divalidasi terhadap ID input; output buruk jatuh ke hasil deterministik dan memunculkan event error | Validasi output terhadap evidence yang disediakan, dengan fallback yang terlihat |
| `core/meetings/services/mcp/src/vexa_mcp/app.py:336–360` | Tool bicara mewajibkan field `asked_by_a_human`; komentar: peringatan prosa dapat dilewati, field wajib tidak | Keputusan berisiko sebaiknya menjadi struktur yang diperiksa kode, bukan kehati-hatian umum dalam prompt |
| `core/meetings/eval/namer-corpus/README.md:113–114` | Latency dibandingkan hanya pada sampel yang benar di kedua arm | Bandingkan latency model pada giliran yang grounded; `flash-lite` tampak cepat sebagian karena melewati recall |

Yang tidak ada di Vexa: streaming token LLM ke TTS per kalimat, filler, prompt caching, barge-in otomatis, dan target latency agent. TTS-nya hanya mem-pipe PCM untuk teks utuh (`core/meetings/services/bot/src/tts-playback.ts:82–89`).

## Amazon LMA

Checkout: `/Users/pushmove/Desktop/jemboed/amazon-transcribe-live-meeting-assistant`, HEAD `a23420ad`. Berkas historis dibaca lewat `git show 9c62c564^:<path>` dan `git show 1098d6d8^:lma-main.yaml`. Singkatan: SMA = `lma-meetingassist-setup-stack/src/strands_meeting_assist_function.py`, NOVA = `lma-virtual-participant-stack/backend/src/nova-agent.ts`, KBHOOK = `lma-meetingassist-setup-stack/src/qna_bedrockkb_lambdahook_function.py` (historis).

| Bukti source | Perilaku | Pelajaran bagi fakeyou |
| --- | --- | --- |
| KBHOOK:48–128, `:303–311` (dihapus April 2026) | Desain awal: kode mengambil N giliran transkrip, LLM menulis ulang pertanyaan menjadi query mandiri atau label "small talk", lalu satu panggilan RetrieveAndGenerate dengan transkrip diinjeksi | LMA pernah memakai prefetch satu panggilan, mirip plan kita |
| SMA:1740–1752, `:1978–1982` | Desain sekarang (agent Strands, default Claude Haiku 4.5): pengetahuan hanya lewat tool; prompt harus memerintahkan "DEFAULT to using current_meeting_transcript" | Recall diperlukan hampir setiap giliran; keputusan itu lebih andal di kode daripada di model |
| NOVA:751–765, `:1291–1297` | Jalur suara Nova memanggil sub-agent sebagai tool secara async; prompt memaksa ucapan "Let me search for that information…" lalu menunggu ack atau 2 detik; sub-agent dianggarkan "5-10 seconds" | Pola keputusan model + tool tidak didokumentasikan di bawah 4 s. Filler hanya menutup jeda, bukan menghapusnya |
| SMA:288–302, `:522–551` | Tool `document_search`/`meeting_history` memanggil RetrieveAndGenerate (LLM bersarang) lalu membuang citation | Hindari generate di dalam retrieval; injeksi evidence mentah dengan ID sumber dan batas top-k |
| `lma-main.yaml` historis 2479–2489; KBHOOK:286–298 | Prompt KB lama: jawab hanya dari hasil search, nyatakan tidak menemukan jawaban bila kosong, klaim pengguna bukan kebenaran; regex "Sorry," menandai no-hit di kode | Grounding memerlukan aturan prompt dan sinyal no-hit di kode; prefetch membuat sinyal itu diketahui sebelum model berjalan |
| `lma-virtual-participant-stack/backend/src/index.ts:553–583`; `mcp_server_loader.py:46–60` | Warmup Lambda saat join dan tiap 3 menit; klien MCP di-cache di level modul | Pertahankan klien memori/embedding tetap hangat |
| SMA:1148–1200 | Setiap token streaming memicu mutation AppSync sinkron dengan klien baru | Jauhkan efek samping sinkron dari jalur panas |

Yang tidak ada di LMA: prompt caching, evaluasi kualitas jawaban, pengukuran latency asisten per giliran, guardrail pada output suara Nova/ElevenLabs, dan aturan eksplisit melarang komitmen atas nama orang lain (threat model `threat-modeling/feature-threats/meeting-assistant.md:116–129` mengklaim mitigasi yang tidak ada di prompt).

## Dampak pada plan

1. Prefetch evidence sebelum panggilan model (Task 1 plan) didukung Vexa dan desain awal LMA. Ini juga menutup kegagalan `flash-lite` karena evidence sudah ada di konteks.
2. Cabang "tidak ada evidence" dipilih kode dengan instruksi eksplisit untuk menjawab tidak diketahui tanpa mengarang; ini ditambahkan sebagai kontrak Task 1.
3. Laporan latency Task 3 memisahkan giliran grounded dari giliran yang melewati evidence wajib, mengikuti aturan pembanding Vexa.
4. Penanda keputusan terstruktur (misalnya apakah jawaban menyentuh komitmen owner) menarik untuk mengurangi penolakan generik, tetapi menambah structured output pada setiap jawaban live yang sebelumnya sengaja dihindari. Catat sebagai eksperimen terpisah, bukan bagian plan ini.
5. Varian hybrid (Task 2): tool recall hanya untuk hop kedua ketika evidence prefetch kosong atau lemah, sesuai saran dari pola LMA. Filler singkat tanpa menunggu ack hanya dipertimbangkan untuk hop itu dan harus lulus aturan larangan tawaran tindak lanjut.

## Batas

Pembacaan source tidak membuktikan keunggulan numerik Vexa atau LMA pada provider dan fixture kita. Angka fakeyou berasal dari storage memori sintetis; latency search Graphiti nyata diukur di Task 0 plan.
