# Verifikasi provider live — 2026-09-28

Pemeriksaan opt-in di `tests/test_providers.py` menjalankan factory library native dengan kredensial `.env` yang sudah dikonfigurasi. Pemeriksaan ini memakai teks sintetis dan sampel ucapan pendek yang dihasilkan. Jalankan dari root repository dengan:

```sh
uv run --no-sync pytest -m live tests/test_providers.py -q --tb=short
```

Hasil pada 2026-09-28: **3 passed, 3 warnings in 20.84s**.

| Pemeriksaan | Hasil yang diamati |
| --- | --- |
| Agent LangChain dan OpenAI | `create_meeting_agent` mengembalikan pesan assistant OpenAI yang memuat marker persis sama dengan yang dikembalikan oleh tool lokal yang tidak berbahaya. Riwayat pesan memuat `ToolMessage` yang bersesuaian. |
| Pipecat dan OpenAI TTS | `create_openai_tts` berjalan di dalam `PipelineWorker` dan `WorkerRunner`. Collector menerima setidaknya satu `TTSAudioRawFrame` yang tidak kosong dan satu `TTSStoppedFrame`; worker berhenti. |
| Graphiti dan Neo4j | `create_graphiti_memory` menambahkan satu episode sintetis dalam grup `live_provider_...` yang unik. Setelah client ditutup dan dibuka ulang, pencarian ber-scope menemukan marker-nya pada fakta sebuah edge. Tes menghapus episode berdasarkan UUID yang dikembalikan, lalu menutup client. |

Pemeriksaan memakai timeout yang dibatasi. Kredensial API yang tidak ada atau listener Neo4j yang tidak tersedia membuat tes yang terdampak dilewati (skip). Setelah panggilan provider dimulai, error autentikasi, billing, API, dan database menggagalkan pemeriksaan dengan tipe exception dan status yang ringkas bila tersedia. Tes tidak mencatat kredensial maupun respons provider secara lengkap.

Hasil ini memverifikasi satu request live per jalur dan tidak membuktikan akses Google Meet, kualitas audio meeting, latency berkelanjutan, atau keandalan produksi. Ketiga warning berasal dari dependency terpasang: konfigurasi Pydantic berbasis class milik Graphiti, import `audioop` milik Pipecat, dan class `AudioContextTTSService` milik Pipecat yang sudah deprecated. Tidak satu pun menggagalkan pemeriksaan.

Referensi API yang dibaca sebelum implementasi: [LangChain agents](https://docs.langchain.com/oss/python/langchain/agents), [Pipecat OpenAI TTS](https://docs.pipecat.ai/api-reference/server/services/tts/openai), [Graphiti episodes](https://help.getzep.com/graphiti/core-concepts/adding-episodes), dan [Graphiti namespacing](https://help.getzep.com/v2/graphiti/core-concepts/graph-namespacing). Signature package terpasang serta implementasi TTS Pipecat dan cleanup Graphiti juga diperiksa untuk versi hasil resolusi proyek ini.
