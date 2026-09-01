# Verification reports

Agent-derived summaries of the verification passes run over this repo:
mutation tests, review lenses, adversarial verification, and — in pass 2 —
a closure audit of pass 1's findings. Passes 1 and 2 (session 921145c0)
covered the first slice; pass 3 (session 2d366cdd) the multistack scenario
and the view generalisation under it. They are receipts for what was found
and fixed, not primary material: each finding cites the file:line or query
it rests on, and the repo at its current commit is the source of truth for
what holds now.

- `2026-09-01-pass-1.txt` — 33 agents over the first slice: 8 mutations
  (7 caught, 1 escaped), 21 confirmed findings, 11 low. Drove the revision
  (gate terms as data, generated fixture, audit view).
- `2026-09-01-pass-2.txt` — 12 agents over the revision: 8 confirmed
  findings, closure audit 20 fixed / 6 recorded / 2 partial / 3 open. Drove
  the third commit (audit over approval-bearing terms, view-computed cells,
  release-card pivot).
- `2026-09-01-pass-3.txt` — 11 agents over the multistack scenario: 12
  mutations (6 caught, 2 partial, 4 not cleanly caught — two of those by
  design: latest-wins verification supersedes a premature one; a cycle
  stops the impact walk without a flag), 11 confirmed findings, 8
  downgraded, 2 refuted. Drove the revision commit (NULL-freight
  enactments and `carried since`, concurrent legs in `v_inflight`, the
  per-view `--pure-check`, the binding description back on the uptake
  screen, three new mutation checks, smells #30–#32).
- `2026-09-01-pass-4.txt` — 47 agents over the rollback and canary views
  (session 4a3b8005): 35 findings, 30 confirmed, 2 downgraded, 3 refuted.
  One class of escape (ordering by clock where the ledger's order is
  arrival — the consent and audit floors, direction, reversal, membership)
  and three unexercised shapes (a rollback of a rollback, a multi-stack
  stage between legs, a freight one stack ran). Drove the revision commit
  (seq floors, a reversal that clears, per-stack `v_live`, `v_freight`
  dedup, two lint rules, five new checks, smells #52–#57). Two findings
  the pass dropped unverified were recovered afterwards (session
  9ba241fa): the plan floor was a third clock-comparison site, fixed in a
  follow-up commit that also swept the remaining fact-vs-fact clock
  comparisons, three more checks (smells #58); the other is #59.
