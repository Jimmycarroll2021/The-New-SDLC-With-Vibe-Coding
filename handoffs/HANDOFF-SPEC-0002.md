# HANDOFF-SPEC-0002

Spec: SPEC-0002
Agent: claude code, Claude Agent SDK, acting on JC's instruction
Model: claude-opus-5
Iterations: 1

## Verified

Verified: `python -m unittest discover -s tests -q` passes 37 tests, 26 pre-existing and 11 new in
the `Integrity` class. `python .agentic/gate.py` on this branch reports G0 to G4 and G6 pass, G3 not
applicable, and G5 failing only on the blank `Reviewer` field below, which is the framework's
documented terminal state. The adversarial harness at `C:\Users\j_car\gates-stress\harness.py` was
re-run against this branch's framework: S11, S12 and S14, the three confirmed bypasses, are now
BLOCKED, and all 15 CATCH scenarios still fire with the same gate and the same evidence. S3 and S4,
the tier and `--tier` bypasses, are BLOCKED by G6. The baseline stayed green on all seven gates, so
no false positive was introduced.

One change was made to the harness itself, outside this repository, and it makes the rig stricter
rather than weaker. S14 patched the literal line `ok = all(r.ok for r in results)` into `ok = True`.
That line is now `ok = verdict(results)`, so the scenario had silently become a no-op. S14 now tries
both spellings and raises if neither is present, so it can never again pass by failing to mutate.

This change edits `.agentic/gate.py`, `.agentic/loop.py` and the two CI workflows. That is
**framework maintenance, declared deliberately**: `specs/SPEC-0002-gate-self-bypass.md` carries
`Framework maintenance: yes`, which is what allows G6 to pass on its own change. It is the one
route by which the runner may be edited, and it exists so that the edit is stated in a file a human
reviews rather than made silently. `agentic.toml` was **not** edited: no `[tiers]`, `[stages]` or
any other policy key changed, because G6 is hard-coded rather than configured.

## Not verified

Not verified: the two CI workflow changes have never executed on a runner. The GitHub Actions and
Azure Pipelines steps that restore `.agentic/` and `agentic.toml` from the base ref were read by eye
and the equivalent `git checkout origin/main -- .agentic agentic.toml` was exercised by hand in a
test fixture, but this branch is not pushed, so no pull request has run them. A human must confirm
the first real run restores, prints what it restored, and still fails G6 on the committed edit.

Not verified: that the local tripwire holds against an author who removes G6 from the runner in the
same change. It does not, and it cannot. Nothing that runs from inside a branch can defend itself
against edits to itself, which is why the CI restore exists. Treat the local G6 result as a
tripwire and the CI restore as the boundary, and put CODEOWNERS on `.agentic/` and `agentic.toml`
in any repository where this matters.

Not verified: gap 4 from the stress test, that G4 checks an import is declared rather than that it
exists, is untouched and still open. Adding a hallucinated package to `requirements.txt` still
satisfies G4. It needs a decision from JC about whether a gate may touch the network or the
environment, so it was left out of scope deliberately.

Not verified by any test: that the new prototype and internal tier restriction is the right
trade-off. G6 now refuses to let a prototype or internal branch carry `paths.source`. That is a
real behaviour change: a spike can no longer edit shipping code on a `scratch/*` branch, and one
existing test fixture was moved off `src/` to keep asserting what it was written to assert. The
alternative was to leave the cheapest bypass, renaming a branch, wide open.

## Review

Reviewer:
