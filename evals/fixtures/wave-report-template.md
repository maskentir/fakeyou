# Laporan gelombang {wave} rewrite Twin

Commit trunk integrasi: `{integration_commit}`. Pembanding resmi:
baseline `docs/reports/2026-10-16-rewrite-baseline.md` atas code commit
`d780dca` (8.611 lulus, Ruff 1 error, mypy 24 error); definisi metrik
tidak diubah.

## Gerbang per gerbang

| Gerbang | Perintah | Hasil gelombang | Baseline |
| --- | --- | --- | --- |
{gate_rows}

## Eval replay

{replay_section}

## Acceptance live (terpisah)

- Fungsi: {live_fungsi}
- Kualitas: {live_kualitas}
- Performa: {live_performa}

## Gap tersisa

{gap_rows}
