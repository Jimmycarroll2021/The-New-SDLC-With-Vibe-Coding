# HANDOFF-SPEC-0002

Spec: SPEC-0002
Agent: claude code, Claude Agent SDK, acting on JC's instruction
Model: claude-opus-5
Iterations: 2, the second driven by external review

## Verified

Verified: `python -m unittest discover -s tests -q` passes 41 tests, 26 pre-existing and 15 new in
the `Integrity` class. `python .agentic/gate.py` on this branch reports G0 to G4 and G6 pass, G3 not
applicable, and G5 failing only on the blank `Reviewer` field below, which is the framework's
documented terminal state. The adversarial harness at `C:\Users\j_car\gates-stress\harness.py` was
re-run against this branch's framework: S11, S12 and S14, the three confirmed bypasses, are now
BLOCKED, and all 15 CATCH scenarios still fire with the same gate and the same evidence. S3 and S4,
the tier and `--tier` bypasses, are BLOCKED by G6. The baseline stayed green on all seven gates, so
no false positive was introduced.

Two changes were made to the harness itself, outside this repository. Both make the rig stricter
rather than weaker, and both are bugs of the same class as the two the original stress test found.

1. S14 patched the literal line `ok = all(r.ok for r in results)` into `ok = True`. That line is now
   `ok = verdict(results)`, so the scenario had silently become a no-op. S14 now tries both
   spellings and raises if neither is present, so it can never again pass by failing to mutate.
2. `reset()` ran `git clean -qfd`, which does not remove ignored files, so `__pycache__` leaked
   between scenarios. A run inherited stale bytecode from C5's deliberately broken `add()` and C6 to
   C9 reported a spurious extra G2 failure. It is now `git clean -qfdx`, and each of the 15 CATCH
   scenarios fails on exactly the one gate it targets.

This change edits `.agentic/gate.py`, `.agentic/loop.py` and the two CI workflows. That is
**framework maintenance, declared deliberately**: `specs/SPEC-0002-gate-self-bypass.md` carries
`Framework maintenance: yes`, which is what allows G6 to pass on its own change. It is the one
route by which the runner may be edited, and it exists so that the edit is stated in a file a human
reviews rather than made silently. `agentic.toml` was **not** edited: no `[tiers]`, `[stages]` or
any other policy key changed, because G6 is hard-coded rather than configured.

## External review

Gemini 3.1 Pro (Antigravity) and Kimi For Coding both reviewed the first iteration independently and
both found the same two P0 holes, which are now fixed:

1. **The CI workflow file was not protected.** On a pull request event GitHub runs the workflow from
   the PR head, so a change that edited or deleted it removed the restore step and the gate run with
   it. G6 now fails a change that touches a CI definition which already invoked the runner on the
   base ref, deletions included, and the README install step now says plainly that branch protection
   with a required status check plus CODEOWNERS is what makes any of this binding.
2. **`git checkout` overwrites, it does not delete.** A pull request could add
   `.agentic/time.py`; the restore would put the base `gate.py` back and leave the added file in
   place, and Python prepends the script's directory to `sys.path`, so the runner would import the
   attacker's module at start-up and exit zero. Both workflows now `rm -rf .agentic` before
   restoring, and fail the job rather than continue if the restore does not succeed.

Also acted on: deletions of protected files were invisible to `--diff-filter=ACMR`; the maintenance
field accepted any value starting with "yes", so it now has to be exactly `yes` or `true`; and
`is_protected` now case-folds, because on a case-insensitive filesystem `Agentic.toml` is the same
file. Four more tests, 41 in total.

Findings deliberately **not** acted on, with the reason:

- **"`tests.command` and `evals.command` still run the branch's code" (both reviewers, P0).** True,
  and by design: they are your test suite and your eval runner. Sandboxing them is a different
  project. Recorded in `docs/GATES.md` rather than pretended away.
- **"The prototype and internal tiers can no longer touch `paths.source`" (both reviewers, P1).**
  Gemini called it "deleting the tier system", Kimi "a blunt instrument". They have a point, and
  **this one is JC's call, not mine.** It is the rule as specified. The alternative that keeps
  spikes working is to apply the tier floor only when the run is a merge proposal, which means the
  local run and the CI run stop agreeing, and the branch-rename bypass stays open locally.
- **"`Framework maintenance: yes` is self-issued" (both reviewers).** Correct, and already stated in
  the spec. It stops drift, not a determined author. Its value is that the claim sits in the diff.
- **"A pull request that relaxes policy is judged by the policy it replaces" (Gemini, P0).**
  Deliberate. That is the whole point of restoring from the base ref. Documented as a trade-off.
- **"fetch-depth may be 1, so the base ref is missing" (Kimi, P0).** Not a defect: the workflow has
  carried `fetch-depth: 0` since before this change. Kimi could not see it in a diff of changed
  lines only.
- **"fnmatch does not support `**`, so nested runtime artefacts are falsely flagged" (Kimi, P1).**
  Not a defect: `match_any` has an explicit `/**` branch, and the test using
  `.agentic/runs/r1/trace.json` proves the nested case.
- **"`enforce_verdict` relies on every caller remembering it" (Kimi, P2).** Not a defect: `run()`
  calls it before returning, so every caller of `run()` gets it.
- Push events, fork pull requests and direct pushes to a protected branch: outside the model.
  Branch protection is what covers those, and the docs now say so.

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
