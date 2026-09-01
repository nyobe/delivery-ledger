# Verification reports

Agent-derived summaries of the two verification passes run over this repo on
2026-09-01 (session 921145c0): mutation tests, review lenses, adversarial
verification, and — in pass 2 — a closure audit of pass 1's findings. They
are receipts for what was found and fixed, not primary material: each
finding cites the file:line or query it rests on, and the repo at its
current commit is the source of truth for what holds now.

- `2026-09-01-pass-1.txt` — 33 agents over the first slice: 8 mutations
  (7 caught, 1 escaped), 21 confirmed findings, 11 low. Drove the revision
  (gate terms as data, generated fixture, audit view).
- `2026-09-01-pass-2.txt` — 12 agents over the revision: 8 confirmed
  findings, closure audit 20 fixed / 6 recorded / 2 partial / 3 open. Drove
  the third commit (audit over approval-bearing terms, view-computed cells,
  release-card pivot).
