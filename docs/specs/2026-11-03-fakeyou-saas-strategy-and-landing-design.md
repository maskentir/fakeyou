# fakeyou — Strategi SaaS & Desain Landing Page

Status: disetujui owner 2026-11-03.
Tanggal riset: 2026-11-03. Semua angka pasar di dokumen ini bersumber dari riset yang
diverifikasi per tanggal tersebut; yang tidak terverifikasi ditandai eksplisit.

Dokumen ini adalah keputusan, bukan eksplorasi. Semua bagian yang pernah受欢迎 sudah
disetujui owner: model bisnis (hybrid open-core), wedge (memori institutional), pasar
(global dengan motion lokal), kapasitas founder (solo full-time dengan kanal), dan
arah desain landing page (Astro + tema Cobalt).

---

## 1. Ringkasan keputusan

| Pertanyaan | Keputusan |
| --- | --- |
| Kategori produk | AI meeting participant — bukan meeting notetaker |
| Model bisnis | Hybrid open-core: OSS gratis · cloud berbayar · enterprise self-hosted |
| Wedge | Memori institutional: satu keputusan, dijawab lagi dengan bukti |
| Moat | Provenance (sitasi tingkat-klaim) + eval harness + self-host, bukan suara |
| Buyer | CTO / VP Engineering di perusahaan software 50–500 engineer |
| Pass | Bahasa Inggris; motion founder-led dari jaringan lokal |
| Founder | Solo, full-time, sudah punya kanal |
| Harga | Community $0 · Team $29/user/bln (min 5 seat) · Enterprise dari $30K/thn |
| Target M12 | $26K MRR (≈ $315K ARR run-rate), base case |
| Pre-seed | Bulan 9–12, setelah ≥25 paying org + ≥$10K MRR + 3 case study |
| Repositori cloud | `fakeyou-cloud` (terpisah dari engine OSS `fakeyou`) |
| Landing page | Repo `fakeyou-web`, Astro + plain CSS, tema Cobalt |

---

## 2. Positioning

### 2.1 Statement

> Untuk CTO dan engineering lead di perusahaan software 50–500 orang yang mengambil
> keputusan lewat meeting. Berbeda dari meeting recorder yang menghasilkan transkrip
> setelah kejadian, fakeyou adalah **insinyur AI yang join meeting sebagai peserta** —
> ia mendengar keputusan, mengingat alasannya, dan **menjawab dengan sumber** di meeting
> berikutnya.

### 2.2 Lapisan positioning

Positioning tidak choose-one. Tiga lapis dipakai di tempat yang berbeda.

| Lapis | Isi | Dipakai di | Kenapa |
| --- | --- | --- | --- |
| **Hero** | *The AI engineer that joins your meetings* | Headline halaman | Orang harus paham **apa itu** dalam 3 detik, sebelum peduli harganya |
| **Proof** | *Memori dengan bukti* — episode id, timestamp, kutipan verbatim | Kartu sesi, spec sheet | 93% dev bilang "citing your sources is the crux" (SO 2026) |
| **ROI** | *Engineer kedua* | Pricing, sales deck, sales call | Angka yang menutup deal CFO |
| **Trust** | *Peserta yang terlihat, bukan perekam senyap* | Section keamanan & FAQ | Consent/litigasi = risiko industri terbesar AND tailwind fakeyou |

### 2.3 Bahasa yang dipakai

Riset menunjukkan buyer enterprise boughtuczyn dengan bahasa **"digital labor"**,
**"digital colleague"**, **"AI teammate"** (Microsoft WTI 2025; Atlassian SOT 2026).
Istilah **"digital twin"** tetap dipakai sebagai nama produk dan Referring category
karena itulah yang membedakan fakeyou dari notetaker.

Rantai positioning final:

```
Kategori : AI meeting participant
Masalah  : keputusan tim hilang karena tidak ada yang hadir & ingat
Produk   : AI engineer yang hadir sebagai peserta bernama
Bedanya  : setiap jawaban membawa sumber yang bisa dibuka
Koubt    : self-hosted, disclosure AI, bisa dimatikan kapan saja
Pembeli  : CTO / VP Eng
Anggaran : AI & developer tooling
```

### 2.4 Kenapa inivalidated

Riset kompetitor menemukan dua celah yang persis isi fakeyou, dan keduanya disebut
tidak terisi oleh pemain mana pun:

1. **Live in-room Q&A dengan provenance.** "Nobody credible does: agent speaks into the
   room, and every spoken claim is backed by a citable, timestamped, verifiable source
   the other participants can see."
2. **Self-hosted + spoken agent + cited cross-meeting memory.** "Every self-hosted
   player is a recorder/transcriber… every agentic memory layer is cloud-only."

Playbook yang mengisi dua celah itu sudah ada di repo: turn judge (participation
decision), Graphiti memory graph, latency p50 0,87 s, dan eval harness dengan baseline
beku.

---

## 3. Validasi pasar

### 3.1 Pain terukur

| Angka | Sumber | Arti |
| --- | --- | --- |
| $161B/tahun "AI fragmentation tax" di Fortune 500 | Atlassian State of Teams 2026 (12.035 KW + 172 exec Fortune 1000) | Coordination debt, bukan meeting-minutes debt |
| 87% KW kekurangan kapasitas untuk coordinate | Atlassian SOT 2026 | Coordinate = tempat budget baru |
| 84% punya tujuan yang tidak jelas/bertabrakan; 71% duplikasi antar tim | Atlassian SOT 2026 ||source of the wedge: keputusan lama terulang |
| 63% dev terblokir informasi tidak lengkap; **79%** menemukan konteks penting **hanya setelah kejadian**; **75%** menghabiskan 5+ jam/minggu mencari jawaban | Stack Overflow Developer Survey 2026 (n=12.374) | Ini masalah yang fakeyou serang |
| **93%** dev bilang "citing your sources is the crux"; 48% hanya percaya AI **bila bisa diverifikasi sendiri** | Stack Overflow 2026 | Provenance = pemicu pembelian, bukan fitur sampingan |
| **44%** menyebut keputusan sebelumnya sebagai konteks paling penting untuk AI agent | Stack Overflow 2026 | Prioritas context = keputusan lama. Persis wedge fakeyou |
| Hanya **17%** pengguna agent bilang agent memperbaiki kolaborasi tim (rating terendah) | Stack Overflow 2025 | Level tim belum tergarap. fakeyou team-level by construction |
| 275 interupsi/hari, satu tiap ~2 menit | Microsoft WTI 2025 (31.000 worker) | Beban koordinasi terukur |
| Hanya **29%** KW sudah mengembed AI ke alur kerja; hanya **41%** memicu workflow | Atlassian SOT 2026; Gladia 2026 (2.000+ end user) | **Celah aktivasi = celah fakeyou**: tidak butuh "cari di notepad lalu copy ke ticket" |
| 74% nilai data residency & in-region compute minimal "../../ | Deloitte State of AI in the Enterprise 2026 | Self-host = syarat, bukan bonus |
| 22% perusahaan tidak jelas siapa bertanggung jawab atas hasil negatif AI | Deloitte 2026 | Audit log = syarat beli |

### 3.2 Wall yang harus dijawab

| Fakta | Sumber | Jawaban fakeyou |
| --- | --- | --- |
| **>40%** proyek agentic AI dibatalkan akhir 2027 ("biaya meroket, nilai bisnis tidak jelas, kontrol risiko inadequate") | Gartner (Reuters, Jun 2025) | Quality scorecard + unit economics yang transparan |
| **95%** proyek genAI tanpa return terukur | MIT NANDA (dilaporkan Fortune, Aug 2025; PDF primer tak dapat diakses) | Scorecard per-org: berapa persen sesi yang twin menjawab benar |
| AI memperlambat dev senior **19%** (RCT) | METR arXiv 2507.09089 (n=16, 246 task) | Diuji lewat eval harness yang sudah ada, bukan diklaim |
| "AI tidak memperbaiki tim; AI memperbesar apa yang sudah ada" | DORA 2025 | Fokus ke workflow tim, bukan individual productivity |
| 20% KW benar-benar percaya AI bekerja seperti dijanjikan | Atlassian SOT 2026 | Disclosure + sumber + kongsi yang bisa dimatikan |

### 3.3 Kepatuhan

- **EU AI Act Article 50 berlaku sejak 2 Agustus 2026** — sistem AI yang berinteraksi
  dengan orang wajib memberitahu bahwa itu AI. `ai_disclosure` fakeyou +
  twin sebagai **peserta bernama yang terlihat** + kill switch = kepatuhan, bukan
  etika.
- Abaqi: class action federal California terhadap Otter.ai (Agustus 2025), thread
  "AI Notetaker Hell", peringatan ABA, laporan university melarang bot cloud
  (sumber tunggal, belum terverifikasi). Kesimpulan: **consent adalah risiko industri
  terbesar dan fakeyou sudah punya jawaban struktural.**

---

## 4. Lanskap kompetitif

### 4.1 Peta

| Kategori | Pemain | Kelemahan yang bisa dimasuki fakeyou |
| --- | --- | --- |
| Bot-free capture + search | Granola ($125M Series C @ $1,5B, Mar 2026), tl;dv, Fathom (diakuisisi Superhuman, Sep 2026) | Granola hanya capture dari perangkat sendiri; tidak agent yang bicara |
| Visible bot + revenue workflow | Otter ($320M non-dilutif General Catalyst, Okt 2026), Fireflies ($1B valuation), Avoma, Read AI | Otter agent menjawab **tanpa provenance** dan tanpa jalur self-hosted |
| Platform-absorbed | Microsoft 365 Copilot, Zoom AI Companion, Google Meet Gemini | **Microsoft membatalkan** "Interactive Agents for Meetings" (Agustus 2026) — risiko platform turun |
| Artifact-only agent | Acta.ai, Lindy, Halliday Flow (kacamata), MeetStream | MeetStream membuat **layer suara jadi commodity** ($0,35/jam infra bot) |
| Self-hosted recorder | Meetily, Notely, Sika (Lychee), Acta on-prem | Semua **recorder**, tidak ada live speaking agent |

### 4.2 Kesimpulan strategis

1. **Jangan bertarung di "AI yang bicara".** Otter sudah di sana dengan $320M.
   Bertarung di **"yang bisa diaudit"**: provenance, disclosure, self-host.
2. **Layer suara tidak bisa jadi moat.** MeetStream mengomoditasinya. Moat fakeyou
   = provenance engine + eval harness + self-host + org memory graph.
3. **Granola adalah ancaman lain bentuk.** Granola bot-free → tidak ada masalah
   consent participant, dan inline citations-nya kuat. Tapi Granola tidak pernah hadir
   sebagai peserta dan tidak punya self-hosted. Jangan ditiru: Granola mengambil
   "no bot in the room" dan fakeyou mengambil "visible, named, accountable participant".
   Keduanya valid, tidak saling menimpa.
4. **Acta.ai ($2,50/user/bln unlimited) menghapus price anchoring.** fakeyou tidak boleh
   bersaing di harga. Bersaing di auditability + presence + provenance.

---

## 5. Model bisnis

### 5.1 Tier

| Tier | Harga | Isi | Peran |
| --- | --- | --- | --- |
| **Community** | **$0** | Self-hosted, Apache-2.0, 1 org / 1 twin, Google Meet, dukungan komunitas | **Funnel akuisisi** |
| **Team** | **$29/user/bulan, min 5 seat** | Cloud managed · 1 twin bersama per org · org memory graph · Slack bot · brief pra-meeting · 600 menit-meeting termasuk, overage $0,12/menit · retensi 30 hari | **Revenue utama** |
| **Enterprise** | **dari $30.000/tahun** | VPC / on-prem · BYO model key · data residency · SCIM · audit log · DLP · kebijakan retensi · SLA · multi-twin per tim | **ACV tinggi** |

### 5.2 Justifikasi harga

Band harga seat AI-engineering 2026: **$10–$40/user/bulan** (Linear Basic $10 /
Business $16, GitHub Copilot Pro $10 / Pro+ $39, Cursor Teams $30+). Power tier
$100–$200. Meeting AI: Fireflies Business $19, Otter Business $30, Granola Enterprise
$35, Avoma Enterprise $39. **$29/user ada di tengah atas band** — cukup untuk
membedakan dari notetaker ($10–$19), cukup rendah untuk tidak kalah dari Copilot.

Enterprise "low-five-figures per engineering org" cocok dengan Glean (100-seat min
≈ $60K/yr, median kontrak $98.890/yr) dan Granola Enterprise ($35/user → 250 seat
≈ $105K/yr). **$30K/thn** masuk sebagai platform fee yang lolos hambatan seat $30+.

### 5.3 Unit economics

| Item | Nilai | Asumsi |
| --- | --- | --- |
| Biaya capture + transkripsi | ~$0,45/jam | MeetStream $0,35/jam bot infra + $0,10/jam diarization |
| Biaya LLM + TTS + embedding | ~$0,15–0,35/jam | model routing + turn judge kecil |
| **Blended per jam-meeting** | **~$0,60–0,80** | |
| Jam-meeting per org per bulan | ~10 jam (600 menit) | 1 twin, tim 20–50 engineer |
| **Biaya infra per org/bulan** | **~$6–8** | |
| Gross margin di $29/user × 20 seat ($580) | **~98%** | Infra $7 |
| CAC inbound (content + OSS) | ~$400 | |
| CAC outbound (founder-led) | ~$1.500 | |
| Target payback | < 6 bulan | |
| Logo churn target | ≤ 2%/bulan | |

Biaya mengikuti **menit-meeting**, bukan jumlah seat. Ini yang membuat per-seat pricing
sangat menguntungkan: banyak pengguna berbagi satu twin.

**Guardrails:**
- Tidak ada voice cloning owner. Suara khas per-org. Voice cloning = impersonation +
  risiko legal, dan tidak perlu untuk positioning.
- `ai_disclosure` default **`upfront`** di semua tier cloud.
- Consent gate level-org: meeting tidak boleh direkam tanpa persetujuan org.

---

## 6. Go-to-market

### 6.1 Empat fase

| Fase | Bulan | Tujuan | Aksi kunci | Syarat lolos |
| --- | --- | --- | --- | --- |
| **P0 — Validasi** | M0–M2 | Bukti orang mau bayar | Rekrut 5 design partner dari kanal founder. **Charge $250/bln sejak hari 1.** Kriteria: tim ≥20 engineer, ≥3 meeting penting/minggu, punya keputusan yang pernah terulang | 3 paying · 1 case study |
| **P1 — Beta + OSS** | M2–M4 | Trafik masuk | Luncurkan Tier Team (founder pricing, harga dikunci 12 bulan). Repo tetap Apache-2.0 + docs site + link ke cloud. Konten deep-dive: turn judge, barge-in, latency budget, schema memory graph, eval gates. Launch HN, r/ExperiencedDevs, r/devops, X, LinkedIn, komunitas dev ID | 20 paying · 2 case study |
| **P2 — Founder-led sales** | M4–M8 | Kontrol pipeline | **Trigger-based outreach.** Penawaran **"Memory Audit"**: 30 menit review knowledge tim calon customer — "berapa keputusan lama yang masih Anda debate ulang?" Audit → pilot 30 hari | 45 paying · 3 case study |
| **P3 — Content + community** | M6–M12 | Compounding | Benchmark publik latency & kualitas · quality scorecard per org · direktori (Linear, GitHub Marketplace, Product Hunt) · partner fractional-CTO · newsletter sponsorship | 75 paying · inbound ≥40% pipeline |

### 6.2 Motion kedua: the compliance conversation

Penjualan ini punya **dua** percakapan dan keduanya harus disiapkan:

1. **Engineering/VP** — "berapa engineer-time yang bisa saya pull back?"
2. **Legal/CISO/CFO** — "apakah ini boleh saya rekam? siapa yang bertanggung jawab kalau
   dia salah?" Jawabannya: twin terlihat sebagai peserta bernama, disclose AI, self-host
   opsional, audit log, kill switch, kill-switch per-org. Ini percakapan yang **tidak
   bisa dilayani** Fireflies/Granola/Otter.

### 6.3 Kanal, ranked

1. **Kanal founder** (M1 — ROI tertinggi)
2. **OSS + docs site** (M2 — lambat, compounding, CAC ≈ $0)
3. **Founder-led outbound** (M4+, time-boxed)
4. **Komunitas** (spiky, high-signal)
5. **Partnership & integrasi** (M8+)
6. **SEO/inbound** (M9+, slow burn)

### 6.4 Metrik

Yang dihitung: **% sesi di mana twin menjawab pertanyaan lintas-meeting dengan benar,
dengan sumber.** Turunan: *time-to-first-correct-answer* (< 7 hari), *weekly active
twins*, *retention minggu 4*, *% meeting dengan ≥1 jawaban yang dinilai benar*.

**Kill criterion:** < 20% design partner masih pakai di minggu 4 → wedge salah, pivot
ke posisi B (memori) atau C (coverage) sebelum spending lain.

---

## 7. Target pendapatan

Asumsi: ARPA $350–420 (12–20 seat), gross margin ≥80%, logo churn ≤2%/bln, CAC
payback < 6 bulan.

| | M2 | M4 | M6 | M9 | **M12** | M18 | M24 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Org berbayar | 3 | 9 | 20 | 45 | **75** | 175 | 450 |
| MRR | $0,8K | $2,7K | $6,8K | $17,1K | **$26,3K** | $91K | $292K |
| ARR run-rate | — | $32K | $82K | $205K | **$316K** | $1,09M | $3,5M |

**Skenario M12:** konservatif 40 org → $12K MRR · **base 75 org → $26,3K MRR** ·
upside 110 org + 2 enterprise → $55K MRR.

**Gerbang (berbasis milestone, bukan tanggal):**
- M2: 3 paying org · 1 case study · ≥20% design partner aktif di minggu 4
- M6: ≥$6K MRR · 3 case study · ≥1 pull referral terbukti
- M9: ≥$15K MRR · inbound ≥25% pipeline · gross margin ≥70%
- M12: ≥$26K MRR · churn ≤2%/bln · NPS ≥40

**Titik raise pre-seed:** bulan 9–12, saat ≥25 paying org + ≥$10K MRR + 3 case study
+ 500 GitHub stars. Target $500K–$1,5M. JANGAN raise sebelum itu — 0 revenue =
negosiasi dari posisi lemah.

---

## 8. Strategy founder

**Narrative:** "Saya membangun AI digital twin self-hosted karena saya lelah kehilangan
konteks engineering. Sekarang ia jadi engineer kedua di setiap tim." Technical,
evidence-driven, jujur soal limit. README fakeyou sudah jujur soal limit — itu aset,
pertahankan.

**Peran founder:** (1) produk — roadmap digerakkan design partner; (2) sales #1;
(3) support #1 → target <10% waktu support di M12, hiring trigger di M9.

**Urutan hiring (trigger-based):**
1. Platform/infrastructure engineer — saat M6 & $6K MRR & auto-join mulai merepotkan
2. Full-stack control plane — saat M9
3. Customer engineer — saat 30 customer
4. GTM — saat $50K MRR

**Risk register:**

| Risiko | Mitigasi |
| --- | --- |
| Otter / vendor besar-feature "AI agent yang bicara" | Beats on auditability, not features. Provenance + self-host + eval harness. Native = validasi pasar |
| Granola bot-free mengambil cerudi "no bot in the room" | both positions valid; fakeyou menang di presence + provenance. Jangan tiru Granola |
| Price war (Acta $2,50) | jangan bersaing di harga |
| Modal STT/TTS/LLM | pricing per menit + overage + BYO key + cost cap per org |
| **Trust — bot bicara tanpa persetujuan** | disclosure `upfront` default · consent gate level-org · kill switch · larangan keras voice cloning |
| Platform (Google/Zoom) build native | self-host + multi-platform + depth of memory |
| Solo founder bottleneck | docs-driven · OSS community · hiring trigger |
| Over-scope jadi 10 tool | satu wedge 12 bulan; setiap fitur harus bisa dirumuskan "ini mengurangi switching apa?" |

**Non-goals 12 bulan:** tidak hire · tidak tambah platform sebelum 10 paying · tidak
marketplace · tidak vertical penuh · tidak self-host control plane sebelum ada customer
yang minta.

---

## 9. Roadmap teknis

Premis: **hampir tidak ada yang perlu dirombak di engine.** Yang belum ada adalah
control plane, dan itu ~80% pekerjaan menuju "perusahaan".

**Keputusan arsitektur:** `fakeyou-cloud` = repo baru terpisah. Engine tetap
Apache-2.0 di repo `fakeyou`; cloud proprietary. Alasannya: repo OSS tetap bersih untuk
komunitas, cloud butuh kontrol keamanan/compliance berbeda, engine bisa di-version
independen.

| Fase | Isi | Kenapa |
| --- | --- | --- |
| **T0 — Fondasi komersial** (M1–2,5) | Auth + multi-tenancy (org/user/RBAC) · Stripe + metering menit · **Google Calendar OAuth → auto-join** · provisioning twin · onboarding 5 menit · cost-observability per org · ToS/Privacy/DPA | Calendar auto-join = blocker #1 aktivasi. Tanpa itu "SaaS" ini cuma demo |
| **T1 — Multi-tenant quality** (M2–4) | **Memory graph level-org** (sekarang per owner/twin) · **layer sitasi tingkat-klaim: kutipan verbatim + timestamp + episode id + skor verifikasi** · **turn judge & per-stream capture nyalakan default** (README bilang masih OFF di reference deploy) · **audit log** (syarat beli Deloitte) · Slack bot · brief auto pra-meeting · tenant isolation | Ini fase yang mengubah produk dari "menarik" jadi "berguna", dan mengunci pemicu pembelian (93% dev: sitasi adalah inti) |
| **T2 — Reach** (M3,5–6,5) | Adapter **Zoom** & **MS Teams** · skill/org-tools (Jira, Linear, GitHub — ini yang bikin twin jadi insinyur, bukan notetaker) · suara & bilingual en/ID | Multi-platform = pasar lebih luas |
| **T3 — Enterprise** (M6–10) | Self-hosted control plane (Helm, lisensi, air-gap) · **SOC 2 Type II mulai kumpulkan bukti di M3** (butuh ~6 bulan) · residensi data EU/US · SCIM · DLP · admin console + scorecard per-org | Compliance = blocker enterprise, harus mulai dini |
| **T4 — Moat (paralel)** | **Quality scorecard sebagai surface produk** (pubilkasi per-org meeting quality score dari eval harness) · benchmark publik latency & kualitas · vertical packs (incident review, RFC walkthrough) | Eval harness = aset yang tidak dimiliki kompetitor mana pun |

**Tiga hal yang harus jujur dan di Alegreksi sebelum design partner pertama:**
1. Turn judge & per-stream capture masih OFF by default di reference deployment → demo
   bisa gagal. Harus nyala sebelum design partner pertama.
2. SOC 2 butuh ~6 bulan lead time → mulai M3 kalau targetnya enterprise di M9.
3. Adapter Zoom/Teams mahal (protokol, bukan API) → jangan sebelum 10 paying.

---

## 10. Desain landing page

### 10.1 Pre-flight

Tidak ada kode existing di target. Repo baru `fakeyou-web`.

- **Stack**: Astro 7 + TypeScript strict + **plain CSS** (tanpa Tailwind). Satu halaman
  dengan design system terkunci → plain CSS + `tokens.css` lebih sederhana dan memberi
  kontrol persis atas palet Cobalt. Tailwind menambah dependensi tanpa manfaat di
  satu halaman.
- **Domain**: `fakeyou.com` (asumsi — ganti bila berbeda).

### 10.2 Pilik Hallmark

- **Genre**: modern-minimal (dev-tool / infra / B2B).
- **Macrostruktur**: **05 · Workbench** — "product in use is the sale". Workbench
  adalah salah satu macrostructure yang secara eksplisit disukai tema Cobalt.
- **Tema**: **Cobalt** — paper `oklch(98.5% 0.004 250)`, ink `oklch(24% 0.02 258)`,
  accent `oklch(58% 0.20 256)`, graphite `oklch(22% 0.016 260)`. Display Space Grotesk,
  body Inter, mono JetBrains Mono.
- **Axes** (diversifikasi): paper band = cool light · display style = grotesk-sans ·
  accent hue = cool blue.
- **Nav**: **N13 · Inline ⌘K pill** — signature move #5 tema Cobalt. Ini run
  pertama di proyek ini, jadi tidak ada nav sebelumnya untuk dirotasi. Dipilih karena
  produk ini search/docs-heavy dan Cobalt meminta nav bordered + ⌘K yang benar-benar
  bekerja.
- **Footer**: **Ft1 · Mast-headed** — satu band, bukan 4 kolom (Ft3 = AI fingerprint).
- **Enrichment**: **Tier-A CSS art** — kartu sesi graphite. Tanpa fake chrome
  (tanpa title bar, tanpa traffic-light dots) karena itu gate 47.
- **Eyebrows**: 1 saja, di hero (Cobalt signature #7 + cap 1–2 per halaman).

### 10.3 Struktur halaman

| # | Section | Arketipe | Isi |
| --- | --- | --- | --- |
| 1 | Nav | N13 | `fakeyou` · Docs · Pricing · GitHub · ⌘K pill · **Join the beta** |
| 2 | Hero | H2 split diptych 7/5 | Eyebrow `AI MEETING PARTICIPANT · APACHE-2.0` · H1 "The AI engineer that joins your meetings" · lede · 2 CTA · **kartu sesi graphite** |
| 3 | Stat strip | T4 | 0,87 s · 9.471 · 8/8 · 0 — semua angka asli dari repo |
| 4 | Dark band | F4 step sequence | "One session, end to end" — 5 tahap: Attends · Listens · Decides · Remembers · Answers, dengan mekanisme teknis asli per tahap |
| 5 | Spec sheet | F3 | Tabel "What we measured" dari README + README eval report |
| 6 | Pricing | F6 product card grid | Community $0 · Team $29/user · Enterprise $30K/thn |
| 7 | Trust | split tak simetris | Disclosure AI (EU AI Act Art. 50) · self-host · audit log · kill switch |
| 8 | FAQ | 6 Q, grid 2 kolom | Objection handler apa adanya |
| 9 | CTA | C2 inline form | Email + "Request beta access" |
| 10 | Footer | Ft1 | wordmark · tagline · link strip · lisensi |

### 10.4 Copy hero

- **Eyebrow**: `AI MEETING PARTICIPANT · APACHE-2.0`
- **H1**: `The AI engineer that joins your meetings`
- **Lede**: `fakeyou enters Google Meet as a named participant. It hears the decision,
  remembers the reason, and answers it in the next meeting — with the source attached.`
- **CTA**: `Join the beta` (solid) · `Read the README` (outline)
- **Micro-note** (mono): `self-host in 6 minutes · Google Meet today`

### 10.5 Kartu sesi (artefak hero)

Format mengikuti `record.jsonl` + label sumber memory sungguhan. Nama peserta fiktif
namun masuk akal (bukan "Jane Doe"). Chip status `SOURCE VERIFIED` di kanan header.
Footer: `first audio 0.87s p50 · recall verified · disclosure AI`.

### 10.6 Angka yang dipakai (semua asli)

| Angka | Sumber |
| --- | --- |
| p50 0,87 s · p95 2,46 s first audio | README "Verified live" |
| Join ke admitted 28–49 s | README "Verified live" |
| 9.471 tes passing, mypy strict | README "Quality gates" |
| 8/8 keluarga eval = 1,0, 0 severe failure, 9 run | `docs/reports/2026-10-25-conversation-replay-disclosure.json` |
| Recall 0,944 (famili `recall`) | idem |
| Apache-2.0 | LICENSE |

**Yang TIDAK ada** (belum ada customer, jadi tidak boleh difabrikasi): logo wall,
"trusted by N teams", testimonial, angka konversi, klaim uptime yang tidak diukur.

### 10.7 Interaksi

- **⌘K palette** (N13): benar-benar bekerja — ⌘K/Ctrl+K buka, Esc tutup, backdrop
  tutup, ↑/↓ pindah, Enter pilih, focus ke input saat buka, body scroll terkunci,
  `role="dialog" aria-modal="true"`. Dua group: "Go to" (section halaman), "Resources"
  (GitHub, Docs, Changelog, Security). Di mobile pill → ikon, modal jadi sheet full-height.
- **Form beta**: validasi email native, state sukses senyap (tanpa toast celebratif).
- **Hover**: hanya satu signal per elemen — underline tumbuh pada link, border jadi
  cobalt pada kartu, translateY(-1px) pada tombol.
- **Motion**: satu orkestrasi saat load (fade + rise 8px, stagger ≤500 ms total),
  sisanya statis. Semua `transform`/`opacity` saja. `prefers-reduced-motion` →
  collapse ke crossfade ≤150 ms.

### 10.8 Token (semua OKLCH, semua named)

```
--color-paper        oklch(98.5% 0.004 250)
--color-paper-2      oklch(96.2% 0.006 250)
--color-paper-3      oklch(93.5% 0.008 251)
--color-rule         oklch(91%   0.008 252)
--color-rule-2       oklch(85%   0.010 254)
--color-ink          oklch(24%   0.020 258)
--color-ink-2        oklch(34%   0.018 257)
--color-muted        oklch(52%   0.014 256)
--color-accent       oklch(55%   0.200 256)
--color-accent-hover oklch(51%   0.200 256)
--color-accent-ink   oklch(99%   0.000 250)
--color-accent-text  oklch(48%   0.160 256)
--color-accent-soft  oklch(95%   0.022 250)
--color-accent-line  oklch(72%   0.090 254)
--color-control      oklch(60%   0.013 254)
--color-graphite     oklch(22%   0.016 260)
--color-graphite-ink   oklch(93% 0.006 250)
--color-graphite-muted oklch(68% 0.014 258)
--color-graphite-rule  oklch(32% 0.018 258)
--color-graphite-accent oklch(80% 0.120 250)
--color-focus        oklch(55%   0.200 256)
```

Nilai accent diturunkan dari hasil pengukuran kontras, bukan dari dugaan:
`--color-accent` (L55%) 4,80:1 terhadap teks tombol; `--color-accent-text` (L48%)
6,37:1 di atas kertas untuk badge kecil; `--color-control` (L60%) 3,78:1 untuk border
komponen interaktif. `--color-rule` hanya dipakai untuk garis dekoratif pemisah section,
yang tidak tunduk pada ambang 3:1 karena bukan batas UI.

Skala spasi 4pt (3xs→4xl), skala teks 1,25 dari 16 px, `--text-display:
clamp(2.75rem, 5vw + 1rem, 5.25rem)`, tiga easing, tiga durasi, enam z-level bernama.

### 10.9 Responsive

Breakpoint: 40 rem / 60 rem / 90 rem. `html, body { overflow-x: clip }`. Tidak ada
`100vw`. Tidak ada affordance yang wrap dua baris (`white-space: nowrap` + parent
reflow). Track grid yang memuat gambar memakai `minmax(0, 1fr)`. Section head collapse
ke satu kolom. Target uji: 320 / 375 / 414 / 768 px.

---

## 11. Yang tidak boleh terjadi

- Tidak ada klaim yang tidak diukur.
- Tidak ada testimonials, logo wall, atau angka customers sampai ada.
- Tidak ada voice cloning owner.
- Tidak ada `ai_disclosure` default `when_asked` di tier cloud.
- Tidak ada adapter platform baru sebelum 10 paying customer.
- Tidak ada hiring sebelum revenue.
- Tidak ada fundraising sebelum $10K MRR + 3 case study.
