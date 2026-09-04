# HANDOFF-SPEC-0003

Spec: SPEC-0003
Agent: claude code, Claude Agent SDK, acting on JC's instruction
Model: claude-opus-5
Iterations: 2, the second driven by a re-run of the same external review

## Verified

Verified: the suite is 107 tests, up from 84. On Windows, `python -m pytest tests/test_gate.py`
gives 105 passing and 2 skipping; the two skips are the symlink tests and they skip for a stated
reason, namely that this host does not hold the privilege to create a symlink (`WinError 1314`).
The same commit was then run on Linux, in WSL Ubuntu 24.04 against a fresh clone of the branch:
`python3 -m unittest discover -s tests` reports 107 tests and `OK`, with no skips at all. Both
symlink tests therefore executed and passed on a host that permits symlinks.

Fourteen defects were fixed. Every one of them was reproduced against the runner before a line was
written, and every regression test added for them was run against the pre-fix runner and failed:
nine failed in the first batch and five in the second. The evidence is in the branch history.

The seventh review round was run on Codex, Gemini and Kimi, all on JC's own subscription licences;
no metered API key was set or used. Between them the three reviewers raised eighteen findings, and
a second Codex pass over the same material raised twenty. Each was checked against the live source
rather than taken on the reviewer's confidence. Nine were confirmed and fixed in the first batch,
five more in the second. Four were confirmed and deliberately deferred, nine more from the second
pass are recorded as open, and three were rejected as misreadings of the source.

`python .agentic/gate.py` passes G0, G1, G2, G4, G5 and G6, with G3 not applicable because no
change here touches the configured AI surface. G6 records the framework maintenance declared in
`specs/SPEC-0003-round7-review-findings.md`, which is the spec this change references and which
this change adds.

The adversarial stress harness at `C:\Users\j_car\gates-stress\harness.py` is unchanged from its
baseline: all fifteen CATCH scenarios are blocked, and S3, S4, S8, S11, S12, S13 and S14 remain
blocked. The seven scenarios that were allowed before this change — S1, S2, S5, S6, S7, S9 and S10
— are still allowed, which is their documented pre-existing state and not a regression introduced
here.

## Not verified

Not verified: nothing about the symlink guard is now unproven — that gap was closed by running the
suite on Linux — but note that on Windows it stays unexercised, so a Windows-only regression in
that guard would not be caught locally. The temporary-path test was rewritten because the previous
version could not fail: it created the link under the test process's pid and then ran the gate in a
subprocess under a different one, touching neither name. The rewrite runs the gate in-process and
asserts the link exists before proceeding.

The suite is sensitive to a `python` being on PATH, not merely a `python3`. Nine pre-existing tests
fail without one, because `agentic.toml` in the test fixture invokes `python`. CI supplies it
through `actions/setup-python`. This is pre-existing and is not changed here, but it is worth
knowing before reading a red local run as a real failure.

Neither CI workflow was executed for this change at the time of writing; only the local runner was.
The CI-stage behaviour is covered by tests that call `run(root, "ci", ...)` directly, which is not
the same as a real GitHub Actions run with a trusted runner unpacked from the base ref.

Four findings were confirmed and left open on purpose, because a wrong repair fails real
repositories: a nested `.py` file anywhere in the tree still contributes its stem as a top-level
local module, so `docs/x.py` makes `import x` read as local; a subpath import of a real package is
still truncated to the package name and never checked; one declared distribution still authorises
every installed sibling sharing its top-level namespace; and `check_js_imports` still reads only
the root `package.json`, which is the JavaScript half of the known monorepo defect. The last of
those belongs in the same change as the Python one.

Nine further findings from the second Codex pass are recorded in the spec's "Out of scope" section
and have not been verified beyond reading the source. They are the next round's work, not this
one's, and none of them should be treated as confirmed until reproduced.

The Gemini leg of the re-run timed out and produced nothing. Its findings from the first pass were
recovered from disk and were verified in full; the re-run would have been corroboration, not new
evidence.

Reviewer:
