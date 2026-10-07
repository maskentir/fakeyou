# Rencana Implementasi Fase 1 Identitas (Butir 15)

> **Untuk pekerja agentic:** SUB-SKILL WAJIB: pakai subagent-driven-development untuk mengerjakan plan ini task-per-task. Step memakai sintaks checkbox (`- [ ]`) untuk pelacakan.

**Goal:** Satu substrat identitas (baris speaker-uuid) menggantikan slot-tenure-only dan dict `_csrc_labels` paralel, dipakai bersama jalur stream dan mixed, dengan label huruf sebagai label tampil saja.

**Architecture:** Modul baru `identity.py` memegang `SpeakerRegistry`: baris per pembicara (uuid deterministik dari label, status, keyakinan, margin, riwayat) dengan aturan fusi yang dipindah 1:1 dari `SlotBinder` (ikut-CSRC, ikat-sekali 1:1, dorman, semua refusal); `SlotBinder` menjadi adaptor tipis yang mempertahankan API publik dan event `tenure_*`; `_csrc_labels` diganti resolusi CSRC registry; satu registry per sesi di-wiring ke binder stream dan tracker mixed; `speaker_uuid` aditif di ekstra/journal.

**Tech Stack:** Python 3.12, pytest, ruff, mypy.

**Spec:** [long-term-architecture.md](long-term-architecture.md) butir 15 (L4); rantai sebelumnya [phase-1-robustness.md](phase-1-robustness.md) (cabang basis `feat/phase-1-robustness`, belum merge).

## Global Constraints

- Python Inggris tanpa komentar/docstring dan tanpa string Bahasa Indonesia di `.py`; dokumentasi Bahasa Indonesia.
- Journal hanya metadata; tanpa teks transkrip, audio, prompt, atau secret.
- Nama event jurnal baru wajib akhiran partisip lampau terkendali (`EventVerb`); plan ini tidak menambah event jurnal (memakai ulang dict `tenure_*` sebagai ekstra, bukan event jurnal).
- Tanpa dependency baru; tanpa protokol privat Meet dan tanpa Meet Media API.
- Flag `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED` default tetap mati; plan ini tidak mengubah flag.
- Tes merah sebelum implementasi; reviewer terpisah per task; tanpa push/merge.
- Semua gate lama dipertahankan: suite serial hijau, browser hijau, `ruff check`, `ruff format --check`, `mypy`; tanpa `xfail`; tanpa tambah latensi agent (resolusi tetap O(slot), tanpa IO baru di jalur live).
- Tes binder/tracker yang ada harus lulus TANPA MODIFIKASI sebagai bukti kompatibilitas (kecuali helper yang plan sebut eksplisit).
- Verifikasi hanya offline + browser harness; tanpa sesi live di plan ini.
- Progres dicatat di `.runtime/phase-1-identity/status.json` tiap task selesai.

---

## Scope Check

Butir 15 adalah subsistem fusi identitas (L4). Butir 16 (endpointing semantik + pra-generate) dapat plan tersendiri sesudah ini. Di luar scope plan ini: sinyal nama-display (fitur speaker-names OFF, butuh voiceprint Fase 2) dan jarak embedding (margin di bawah dirumus jujur dari kandidat tunggal vs konflik).

## File Structure

| File | Tanggung jawab |
| --- | --- |
| `src/fakeyou/identity.py` (baru) | `SpeakerRow` + `SpeakerRegistry` + `ResolveRefusal` (Task 1) |
| `src/fakeyou/voice_activity.py` | `SlotBinder` jadi adaptor; tracker pakai registry; `Tenure.uuid`; `VoiceAttribution.speaker_uuid` (Task 2–3) |
| `src/fakeyou/cli/join.py` | Satu registry per sesi + `speaker_uuid` di ekstra (Task 3) |
| `tests/test_identity.py` (baru) | Tes registry (Task 1) |
| `tests/test_voice_activity.py` | Tes adaptor + unifikasi (Task 2–3; tes lama tak tersentuh) |
| `tests/test_stream_capture.py` | Tes wiring bila perlu (Task 3; tes lama tak tersentuh) |
| `docs/reports/2026-10-06-per-stream-acceptance.md` | Adendum implementasi (Task 4) |

---

### Task 1: SpeakerRegistry inti

**Files:**

- Create: `src/fakeyou/identity.py`
- Test: `tests/test_identity.py` (baru)

**Interfaces:**

- Consumes: tidak ada (modul daun; hanya stdlib `dataclasses`, `itertools`, `string`, `typing`, `uuid`, `collections.abc`; generator label default didefinisikan lokal di modul ini — JANGAN impor dari `voice_activity` agar tak siklus).
- Produces: `ResolveRefusal = Literal["no_roster", "slot_contention", "slot_conflict", "self_overlap"]`; `@dataclass(frozen=True, slots=True) class SpeakerRow` dengan field `uuid: str`, `label: str`, `device: str | None`, `status: str` (`"confirmed"` bila device terikat, else `"provisional"`), `confidence: float` (1.0 confirmed, 0.7 pernah-ikut-CSRC, 0.5 slot-saja), `margin: float` (1.0 kandidat-tunggal tak ambigu), `slot: int | None`, `csrcs: tuple[int, ...]`, `track: str`, `level: float`, `history: tuple[str, ...]`; `SpeakerRegistry(labels=None, capacity=3)` dengan `observe_stream(slot, csrcs, *, track="", level=0.0)`, `update_roster(devices)`, `resolve(slot, *, allow_new=True) -> SpeakerRow | ResolveRefusal`, `resolve_csrcs(csrcs: frozenset[int]) -> SpeakerRow | None` (None = konflik; input kosong tak pernah: pemanggil menyaring), `stream_csrcs(slot)`, `drain_events()`, `reset()`, properti `roster_size`. uuid = `uuid5(NAMESPACE_URL, "speaker:" + label).hex` (deterministik per label). Event dict `tenure_minted/moved/released/restored/relabel` sama PERSIS bentuknya dengan `SlotBinder` kini (kunci dan nilai identik).

Semantik yang dipindah 1:1 dari `SlotBinder`: no_roster tanpa roster; tenure per slot; demand_full → slot_contention; ikut-CSRC tepat-satu-cocok (len(matches) != 1 → None); slot_conflict bila tenure tak terikat >= device tak terikat; self_overlap bila allow_new False; mint label baru + bind 1:1; release → dorman per device (atau delete-one tak terikat); restore dorman bila tak contended dan slot asal bebas; relabel sunyi jadi event `tenure_relabel`.

Semantik `resolve_csrcs` dipindah 1:1 dari `_attribute_csrc` (tanpa marker — pemanggil membersihkan): indeks CSRC→baris; himpunan baris dikenal dari identitas; konflik bila >1 baris dikenal ATAU terlabel-sebagian → None; else pakai dikenal atau mint baru; setdefault tiap CSRC ke baris putusan.

- [ ] **Step 1: tulis tes merah**

```python
from uuid import NAMESPACE_URL, uuid5

from fakeyou.identity import SpeakerRegistry


def _registry() -> SpeakerRegistry:
    registry = SpeakerRegistry()
    registry.update_roster(["dev-a", "dev-b"])
    return registry


def test_registry_mints_deterministic_uuid_rows() -> None:
    registry = _registry()
    registry.observe_stream(0, [11])
    row = registry.resolve(0)
    assert row.label == "A"
    assert row.uuid == uuid5(NAMESPACE_URL, "speaker:A").hex
    assert row.status == "provisional"
    assert row.confidence == 0.5
    assert row.margin == 1.0
    assert row.slot == 0
    assert row.csrcs == (11,)
    assert row.history[0] == "minted"


def test_registry_follows_csrc_across_slots() -> None:
    registry = _registry()
    registry.observe_stream(0, [11])
    first = registry.resolve(0)
    registry.observe_stream(1, [11])
    moved = registry.resolve(1)
    assert moved.uuid == first.uuid
    assert moved.slot == 1
    assert moved.confidence == 0.7
    assert "moved" in moved.history
    events = registry.drain_events()
    assert [event["event"] for event in events] == ["tenure_minted", "tenure_moved"]
    assert events[1]["from_slot"] == 0
    assert events[1]["to_slot"] == 1


def test_registry_binds_once_and_refuses_conflicts() -> None:
    registry = SpeakerRegistry()
    registry.update_roster(["only"])
    registry.observe_stream(0, [11])
    row = registry.resolve(0)
    assert row.device == "only"
    assert row.status == "confirmed"
    assert row.confidence == 1.0
    assert registry.resolve(0) == row
    registry.observe_stream(1, [22])
    assert registry.resolve(1) == "slot_conflict"


def test_registry_resolve_csrcs_conflicts_on_split_identity() -> None:
    registry = _registry()
    first = registry.resolve_csrcs(frozenset([11]))
    second = registry.resolve_csrcs(frozenset([22]))
    assert first.uuid != second.uuid
    assert registry.resolve_csrcs(frozenset([11, 22])) is None
    assert registry.resolve_csrcs(frozenset([11])) == first
```

- [ ] **Step 2: jalankan tes, pastikan merah**

Run: `pytest tests/test_identity.py -v`
Expected: FAIL (`identity` belum ada)

- [ ] **Step 3: implementasi minimal**

Struktur internal (mutable): `_rows: dict[str, _Record]` (kunci uuid), `_by_slot: dict[int, str]`, `_by_csrc: dict[int, str]`, `_stream_csrcs: dict[int, frozenset[int]]`, `_present`, `_dormant: dict[str, tuple[int, str]]` (device → (slot, uuid)), `_slot_labels`, `_events`, `_labels`, `_capacity`, `_row_meta: dict[str, dict]` (track/level terakhir + followed + history list).

`_Record`: dataclass mutable (label, device, slot, followed, history list). Tiap transisi: mutasi record + append history + emit dict event warisan. `resolve`/`resolve_csrcs` mengembalikan snapshot `SpeakerRow` beku yang dibangun dari record + `_stream_csrcs` slot + meta.

Aturan main disalin baris-per-baris dari `SlotBinder` (tenures→`_by_slot`+`_rows`, `_dormant`, `_bind`, `_release`, `_contended`, `_demand_full`, `_unbound_*`, `_remember`) dan dari `_attribute_csrc` untuk `resolve_csrcs` (deduplikasi: `_csrc_labels.setdefault` → `_by_csrc.setdefault`).

`status/confidence`: device → ("confirmed", 1.0); followed → ("provisional", 0.7); else ("provisional", 0.5). `margin` = 1.0. `track`/`level`: last-seen dari `observe_stream` (audit; tak dipakai putusan — catat di status bila reviewer bertanya).

- [ ] **Step 4: jalankan tes, pastikan hijau**

Run: `pytest tests/test_identity.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: review reviewer terpisah, lalu commit**

```bash
git add src/fakeyou/identity.py tests/test_identity.py
git commit -m "feat(l4): add speaker fusion registry"
```

---

### Task 2: SlotBinder jadi adaptor registry

**Files:**

- Modify: `src/fakeyou/voice_activity.py` (`SlotBinder` delegasi, `Tenure.uuid`, alias `BinderRefusal`)
- Test: tanpa tes baru — seluruh `tests/test_voice_activity.py` + `tests/test_stream_capture.py` yang ada harus hijau TANPA MODIFIKASI.

**Interfaces:**

- Consumes: `SpeakerRegistry`, `SpeakerRow` (Task 1).
- Produces: `SlotBinder(labels=None, capacity=MEET_AUDIO_SLOTS, registry=None)` — bila registry None buat `SpeakerRegistry(labels, capacity)`; `resolve/observe_stream/update_roster/stream_csrcs/drain_binder_events/reset/roster_size` berdelegasi; `resolve` mengembalikan `Tenure(label=row.label, device=row.device, uuid=row.uuid)` atau refusal string; `Tenure` tambah field `uuid: str = ""`; `BinderRefusal = ResolveRefusal` (alias impor; nilai literal identik).

- [ ] **Step 1: tulis tes merah (adaptor, bukan duplikat perilaku)**

```python
def test_binder_adapter_exposes_registry_uuid() -> None:
    from fakeyou.identity import SpeakerRegistry

    registry = SpeakerRegistry()
    registry.update_roster(["dev-a"])
    binder = SlotBinder(registry=registry)
    outcome = binder.resolve(0)
    assert outcome.uuid == registry.resolve(0).uuid
    assert outcome.uuid


def test_binder_adapter_keeps_default_registry_private() -> None:
    first = SlotBinder()
    second = SlotBinder()
    first.update_roster(["dev-a"])
    second.update_roster(["dev-a"])
    assert first.resolve(0).uuid == second.resolve(0).uuid
    assert first.drain_binder_events() != []
```

(Determinisme uuid per label membuat dua registry privat setara — disengaja dan di-pin di sini.)

- [ ] **Step 2: jalankan tes, pastikan merah**

Run: `pytest tests/test_voice_activity.py::test_binder_adapter_exposes_registry_uuid tests/test_voice_activity.py::test_binder_adapter_keeps_default_registry_private -v`
Expected: FAIL (`registry` kwarg / `Tenure.uuid` belum ada)

- [ ] **Step 3: implementasi minimal**

```python
@dataclass(slots=True)
class Tenure:
    label: str
    device: str | None = None
    uuid: str = ""
```

`SlotBinder.__init__` tambah param `registry: SpeakerRegistry | None = None`; simpan `self._registry = registry if registry is not None else SpeakerRegistry(labels, capacity)`. Tiap metode publik delegasi satu baris ke registry; `resolve` membungkus `SpeakerRow` → `Tenure`. Hapus seluruh state privat lama (`_tenures`, `_stream_csrcs`, `_present`, `_dormant`, `_labels`, `_capacity`, `_events`, `_slot_labels`) dan metode privat lama (`_emit`, `_remember`, `_follow_csrc`, `_contended`, `_demand_full`, `_release`, `_unbound_devices`, `_unbound_tenures`, `_bind`). `BinderRefusal = ResolveRefusal` via impor dari `fakeyou.identity`.

- [ ] **Step 4: jalankan tes, pastikan hijau**

Run: `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false pytest tests/test_voice_activity.py tests/test_stream_capture.py -q -p no:cacheprovider`
Expected: PASS penuh termasuk semua tes lama tak tersentuh

- [ ] **Step 5: review reviewer terpisah, lalu commit**

```bash
git add src/fakeyou/voice_activity.py tests/test_voice_activity.py
git commit -m "feat(l4): delegate slot binder to fusion registry"
```

---

### Task 3: unifikasi tracker + wiring sesi + speaker_uuid

**Files:**

- Modify: `src/fakeyou/voice_activity.py` (tracker pakai registry bersama; `_csrc_labels` → `resolve_csrcs`; `VoiceAttribution.speaker_uuid`)
- Modify: `src/fakeyou/cli/join.py` (satu registry per sesi; `speaker_uuid` di ekstra)
- Test: tambah tes unifikasi di `tests/test_voice_activity.py`; tes lama tak tersentuh.

**Interfaces:**

- Consumes: `SpeakerRegistry` bersama (Task 1–2).
- Produces: `VoiceActivityTracker(..., registry=None)` — internal `SlotBinder` memakai registry itu (atau buat sendiri bila None, perilaku warisan); `_attribute_csrc` memanggil `registry.resolve_csrcs(identity)` ganti dict `_csrc_labels` (hapus dict); `VoiceAttribution` tambah `speaker_uuid: str = ""`, `journal_fields()` tambah `"speaker_uuid"`; `join.py` membuat satu `SpeakerRegistry(capacity=STREAM_FLEET_SLOTS)` per sesi dan meneruskannya ke `SlotBinder` stream dan `VoiceActivityTracker`; `attribute_stream_frame` tambah `extra["speaker_uuid"] = outcome.uuid` di jalur sukses (dan `""` implisit tak ada di jalur unknown — ekstra unknown tak berubah).

- [ ] **Step 1: tulis tes merah**

```python
def test_tracker_and_binder_share_registry_rows() -> None:
    from fakeyou.identity import SpeakerRegistry

    registry = SpeakerRegistry()
    registry.update_roster(["dev-a", "dev-b"])
    binder = SlotBinder(registry=registry)
    tracker = VoiceActivityTracker(twin_name="Twin", registry=registry)
    binder.observe_stream(0, [11])
    row = binder.resolve(0)
    same = tracker._registry.resolve_csrcs(frozenset([11]))
    assert same.uuid == row.uuid


def test_csrc_attribution_carries_speaker_uuid() -> None:
    tracker = VoiceActivityTracker(twin_name="Twin")
    tracker.update_roster_for_test(["dev-a"])
    tracker.observe_csrc_frame_for_test(slot=0, csrcs=[11], level=0.5)
    tracker.observe_vad_for_test(active=True)
    attribution = tracker.attribute()
    assert attribution.speaker_uuid
    assert attribution.journal_fields()["speaker_uuid"] == attribution.speaker_uuid
```

(Helper `*_for_test` mengikuti pola injeksi state yang sudah ada di berkas tes — JANGAN tambah metode produksi bertanda test; bila pola injeksi langsung ke privat dipakai tes lama (`tracker._frames.append` dll), tiru pola itu. Eksekutor menyesuaikan dengan helper aktual dan tetap tanpa helper produksi baru.)

- [ ] **Step 2: jalankan tes, pastikan merah**

Run: `pytest tests/test_voice_activity.py::test_tracker_and_binder_share_registry_rows tests/test_voice_activity.py::test_csrc_attribution_carries_speaker_uuid -v`
Expected: FAIL (`registry` kwarg / `speaker_uuid` belum ada)

- [ ] **Step 3: implementasi minimal**

Tracker `__init__` tambah `registry: SpeakerRegistry | None = None`; `self._registry = registry if registry is not None else SpeakerRegistry()`; `self._binder = SlotBinder(self._labels, registry=self._registry)`. HAPUS `self._csrc_labels`; `_attribute_csrc` ganti blok labeled/known/mint dengan:

```python
        row = self._registry.resolve_csrcs(identity_set)
        if row is None:
            return VoiceAttribution(
                "unknown", "csrc_conflict", evidence_id, slot, levels=levels, csrc=identity
            )
        return VoiceAttribution(
            voice_participant_id(row.label),
            "dominant_csrc",
            evidence_id,
            slot,
            levels=levels,
            csrc=identity,
            speaker_uuid=row.uuid,
        )
```

dengan `identity_set = frozenset(identity)`. (`identity` tuple terurut untuk `csrc` VoiceAttribution dipertahankan.)

HATI-HATI namespace label: warisan memakai SATU iterator `self._labels` untuk binder internal dan `_csrc_labels`. Registry memiliki iteratornya sendiri; solusi: tracker warisan (registry None) teruskan iteratornya — `SpeakerRegistry(self._labels)`; adaptor Task 2 memakai `labels` hanya bila registry None. Dengan begitu satu namespace label dipertahankan di semua konfigurasi.

`VoiceAttribution` tambah `speaker_uuid: str = ""` di AKHIR setelah `csrc` agar semua konstruksi posisional lama aman (field baru selalu default).

`journal_fields()` tambah `"speaker_uuid": self.speaker_uuid`.

`join.py`: impor `SpeakerRegistry`; sebelum `stream_binder = SlotBinder(...)` buat `speaker_registry = SpeakerRegistry(capacity=STREAM_FLEET_SLOTS)`; `SlotBinder(capacity=..., registry=speaker_registry)`; `VoiceActivityTracker(twin_name=..., registry=speaker_registry)` (situs konstruksi tracker di join.py — verifikasi nama param existing). `attribute_stream_frame` jalur sukses tambah `extra["speaker_uuid"] = outcome.uuid` (di samping `speaker_reason`).

- [ ] **Step 4: jalankan tes, pastikan hijau**

Run: `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false pytest tests/test_voice_activity.py tests/test_stream_capture.py tests/test_conversation.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: review reviewer terpisah, lalu commit**

```bash
git add src/fakeyou/voice_activity.py src/fakeyou/cli/join.py tests/test_voice_activity.py
git commit -m "feat(l4): unify stream and mixed identity on speaker rows"
```

---

### Task 4: gate penuh + adendum + status

**Files:**

- Modify: `docs/reports/2026-10-06-per-stream-acceptance.md` (adendum implementasi)
- Modify: `.runtime/phase-1-identity/status.json` (final; untracked)
- Test: tanpa tes baru; verifikasi gate penuh.

**Interfaces:**

- Consumes: Task 1–3.
- Produces: adendum laporan + status final + cabang siap review merge (tanpa push/merge).

- [ ] **Step 1: gate serial penuh**

Run: `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false pytest tests/ -q -p no:cacheprovider`
Expected: exit 0, 0 failed (catat angka passed/skipped/deselected aktual)

- [ ] **Step 2: gate statis + browser**

Run: `ruff check .`, `ruff format --check .`, `mypy src`, browser `-m "browser"` dengan override addopts agar tes browser benar jalan (catat output aktual)
Expected: bersih semua

- [ ] **Step 3: tulis adendum + status, commit**

Adendum: tabel butir 15, angka gate aktual, penegasan dua substrat kini satu + label huruf tampil-saja + uuid deterministik + tanpa perubahan perilaku atribusi (tes lama hijau tak tersentuh). Status: task, status tiap task, command + exit code, artefak, gate terbuka sisa (butir 16).

```bash
git add docs/reports/2026-10-06-per-stream-acceptance.md
git commit -m "docs(phase-1): identity implementation addendum and status"
```

---

## Self-Review

1. **Cakupan spec:** butir 15 → Task 1 (tabel fusi: sinyal→baris uuid + status/keyakinan/margin/riwayat + bind-once + konflik→unknown) + Task 2 (tenure menjadi sinyal/adaptor, bukan keputusan) + Task 3 (satukan substrat stream+mixed, label tampil dipertahankan). I6/I7 tertutup struktural: satu substrat, relabel sunyi mustahil (baris stabil, slot menumpang).
2. **Pindai placeholder:** tiap step berisi kode/perintah aktual; helper tes dirujuk ke pola helper yang sudah ada.
3. **Konsistensi tipe:** `SpeakerRow.uuid/label/device` → `Tenure(label, device, uuid)`; `ResolveRefusal` ≡ `BinderRefusal`; `resolve_csrcs(frozenset[int]) -> SpeakerRow | None`; `speaker_uuid: str` di ekstra + journal_fields.
