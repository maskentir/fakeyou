# Rencana implementasi persona dan akses memori ber-scope

> **Untuk agentic worker:** Gunakan `executing-plans` atau `subagent-driven-development` untuk mengimplementasikan dan me-review setiap task. Checkbox melacak penyelesaian.

**Tujuan:** Berikan twin meeting sebuah profil owner yang tervalidasi dan akses baca-saja ke namespace memori owner tersebut.

**Arsitektur:** Pydantic memvalidasi dokumen persona JSON. Composition root yang sudah ada menginjeksi prompt-nya dan memfilter tool Graphiti native sebelum membangun agent LangGraph. Penjadwalan percakapan, transport audio, dan lifecycle browser mempertahankan kontrak yang sudah ada.

**Stack teknologi:** Python 3.12, Pydantic 2, LangChain 1.4.2, LangGraph 1.2.12, Graphiti 0.30.2, Pipecat 1.12.0, pytest, DeepEval.

**Spesifikasi:** [Status proyek, Tahap 4](../reference/project-status.md), dengan [kontrak conversation](conversation.md) sebagai batas integrasi.

## Batasan global

- Baca dokumentasi/source library terpasang sebelum mengedit integrasi; utamakan API native.
- Python yang mudah dibaca tanpa comment/docstring, strict typing, dan tanpa framework orkestrasi tambahan.
- Satu worker browser/audio per meeting; pertahankan admission guest oleh host dan perilaku stop yang normal.
- Simpan kredensial di `.env` yang sudah ada dan di-ignore. Dokumen persona tidak berisi key provider.
- Gunakan `create_meeting_agent(..., persona=...)` native, `group_id` Graphiti native, dan validasi Pydantic native.
- Jangan menyimpulkan owner dari id speaker di transkrip, dan jangan menggabungkan namespace owner lain.
- Persona adalah konfigurasi, bukan penegakan permission. Tegakkan ketersediaan tool saat menyusun agent.
- Task ini menghasilkan profil dan retrieval. Ingestion transkrip otomatis, koreksi, kebijakan penghapusan, replay aksi yang durable, avatar, dan barge-in membutuhkan kriteria acceptance sendiri.

## Entry gate: tutup celah bukti percakapan live

- [ ] Baca laporan refactor conversation terbaru dan jalankan `uv run pytest`.
- [ ] Gunakan Meet uji yang sedang di-host dan disediakan oleh pengguna. Jalankan worker Linux yang sudah ada dengan `--audio --converse`, meeting id baru, dan durasi singkat.
- [ ] Ajukan dua pertanyaan berkaitan: perkenalkan nama proyek sintetis, lalu tanyakan nama itu. Pastikan kedua jawaban terdengar dan jawaban kedua memakai konteks giliran pertama.
- [ ] Stop saat model sedang bekerja dan pastikan tidak ada respons terlambat, lifecycle berakhir (ended), dan tidak ada container meeting yang tersisa.
- [ ] Catat transkrip lokal, hasil graph, pengiriman TTS, dan pendengaran remote sebagai bukti terpisah. Host yang tidak tersedia membuat gate ini tetap terbuka; pekerjaan unit profil dapat dilanjutkan, tetapi acceptance live tidak boleh ditandai lulus.

## Peta file

| File | Tanggung jawab |
| --- | --- |
| `src/fakeyou/persona.py` (baru) | Profil tervalidasi, pemeriksaan identitas, rendering prompt, derivasi namespace |
| `src/fakeyou/cli.py` | Memuat persona opsional dan menyusun agent yang sudah ada dengan tool yang diizinkan |
| `tests/test_persona.py` (baru) | Validasi, isolasi namespace, data prompt |
| `tests/test_cli.py` | Kontrak CLI dan daftar tool hasil komposisi yang sebenarnya |
| `tests/test_providers.py` | Pemeriksaan isolasi opt-in memakai fakta sintetis di grup sekali pakai |
| `README.md`, `docs/project-status.md` | Command, catatan kompatibilitas, verifikasi terukur |

## Task 1: kontrak profil dan identitas owner

**Menghasilkan:** `PersonaProfile`, `load_persona(path: Path, session: SessionConfig) -> PersonaProfile`, dan `build_persona(profile: PersonaProfile) -> str`.

- [ ] Tambahkan tes yang gagal untuk field asing, identitas/nama kosong, profil untuk owner/twin yang berbeda, dan isolasi memori. Gunakan profil yang sama di dua meeting dan assert bahwa profil itu mempertahankan namespace memorinya.

```python
def test_owner_memory_namespace_is_stable_and_isolated():
    first = PersonaProfile(owner_id="owner-a", twin_id="twin", display_name="Twin")
    same = PersonaProfile(owner_id="owner-a", twin_id="twin", display_name="Renamed Twin")
    other = PersonaProfile(owner_id="owner-b", twin_id="twin", display_name="Twin")
    assert first.memory_group == same.memory_group
    assert first.memory_group != other.memory_group
```

- [ ] Jalankan `uv run pytest tests/test_persona.py -q` dan pastikan kontrak yang belum ada menyebabkan kegagalan.
- [ ] Implementasikan schema dengan tipe tervalidasi `SessionId` dan `TwinName` yang sudah ada. Gunakan `ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)` milik Pydantic, bukan parser JSON/schema kustom.

```python
class PersonaProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    owner_id: SessionId
    twin_id: SessionId
    display_name: TwinName
    role: str = "meeting participant"
    language: str = "id"
    speaking_style: str = "brief and clear"
    goals: tuple[str, ...] = ()
    verified_facts: tuple[str, ...] = ()
    allowed_tools: frozenset[Literal["recall_meeting_context"]] = frozenset()

    @property
    def memory_group(self) -> str:
        identity = json.dumps([self.owner_id, self.twin_id])
        return f"twin_{uuid5(NAMESPACE_URL, identity).hex}"
```

- [ ] `load_persona` memakai `PersonaProfile.model_validate_json(path.read_text())`, lalu menolak ketidakcocokan owner/twin dengan `ValueError`. Pemanggil async menjalankan operasi file ini lewat `asyncio.to_thread` sebelum membuka resource model, Graphiti, atau browser.
- [ ] `build_persona` menggabungkan `DEFAULT_PERSONA` dengan guidance suara yang eksplisit dan `profile.model_dump_json()`: jawab singkat dalam bahasa yang dikonfigurasi, bedakan fakta profil dari klaim participant dan inferensi model, serta jangan pernah mengarang kewenangan owner atau identitas speaker. Letakkan instruksi task di luar data profil JSON.
- [ ] Jalankan tes profil, Ruff, dan mypy. Review lalu commit kontrak yang dapat diuji secara independen ini.

## Task 2: susun profil dan retrieval ber-scope

**Memakai:** fungsi profil dari Task 1 serta `open_conversation`, `create_memory_tools`, dan `create_meeting_agent` yang sudah ada.

**Menghasilkan:** `join --audio --converse --persona PATH`, dengan `--memory` yang hanya mengekspos tool recall yang diizinkan profil.

- [ ] Perluas tes CLI agar `--persona` mensyaratkan `--converse`, identitas yang tidak cocok gagal sebelum startup runtime, dan tidak ada tool tulis yang sampai ke meeting agent.

```python
assert captured_persona == build_persona(profile)
assert captured_group_id == profile.memory_group
assert [tool.name for tool in captured_tools] == ["recall_meeting_context"]
```

- [ ] Jalankan `uv run pytest tests/test_cli.py -q`; pastikan kontrak baru gagal sebelum mengubah komposisi.
- [ ] Tambahkan `persona_path: str | None = None` ke `join_meeting`. Validasi file setelah `SessionConfig` dibangun dan sebelum memasuki resource eksternal. Teruskan `profile: PersonaProfile | None = None` ke `open_conversation`.
- [ ] Pada jalur percakapan, wajibkan `--persona` bila `--memory` diatur. Pertahankan command `chat --memory` yang independen tetap kompatibel dengan grup yang dikonfigurasi secara eksplisit. Dokumentasikan perbedaan ini.
- [ ] Injeksikan `build_persona(profile)` ke `create_meeting_agent`. Untuk `--memory`, gunakan namespace turunan profil dan filter tool native sebelum membangun agent:

```python
available = create_memory_tools(graphiti, group_id=profile.memory_group)
tools = [tool for tool in available if tool.name in profile.allowed_tools]
```

- [ ] Verifikasi bahwa profil tanpa `recall_meeting_context` tidak menerima tool memori. Pertahankan perilaku `--audio` yang hanya transkripsi serta semua tes pembatalan/cleanup.
- [ ] Jalankan `uv run pytest tests/test_cli.py tests/test_conversation.py tests/test_runtime_audio.py -q`, Ruff, dan mypy; review lalu commit integrasinya.

## Task 3: verifikasi isolasi dan dokumentasikan batas berikutnya

- [ ] Perluas tes Graphiti opt-in dengan dua namespace owner yang unik dan fakta sintetis yang saling bertentangan. Gunakan konteks client native yang sudah ada dan pola cleanup berdasarkan UUID episode yang persis. Buka ulang client sebelum retrieval; tidak ada owner yang boleh mengambil fakta milik owner lain.
- [ ] Jangan mengirim pengetahuan owner yang nyata selama verifikasi provider. Pakai ulang API SDK terpasang milik test suite dan bersihkan hanya id episode miliknya sendiri.
- [ ] Jalankan suite offline penuh dan DeepEval, lalu tes live terpilih hanya dengan kredensial yang sudah dikonfigurasi. Catat setiap hasil secara terpisah.
- [ ] Perbarui README dengan profil JSON sintetis yang lengkap dan command `--persona`. Jangan masukkan profil pribadi yang sebenarnya ke version control kecuali memang ditujukan sebagai fixture.
- [ ] Catat hasil entry gate percakapan live. Jangan menyatakan Tahap 4 selesai: ingestion dengan id sumber, waktu kejadian, koreksi, retensi, dan idempotensi tulis yang durable adalah slice berikutnya.
- [ ] Review diff akhir dan commit. Laporkan bukti tes yang sebenarnya dan gate yang masih terbuka.

## Dokumentasi yang dirujuk

- [Validasi model Pydantic](https://docs.pydantic.dev/latest/concepts/models/#validating-data).
- [Short-term memory LangChain](https://docs.langchain.com/oss/python/langchain/short-term-memory).
- [Namespacing graph Graphiti](https://help.getzep.com/graphiti/core-concepts/graph-namespacing).
- [Episode Graphiti](https://help.getzep.com/graphiti/core-concepts/adding-episodes).
- Graphiti 0.30.2 terpasang: `Graphiti.search`, `Graphiti.add_episode`, `EpisodicNode`, serta tes cleanup live milik proyek yang sudah ada, sebelum mengubah tes persistence.
