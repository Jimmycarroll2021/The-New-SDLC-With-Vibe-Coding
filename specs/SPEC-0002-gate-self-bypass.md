# SPEC-0002: The change may not set the rules it is judged by

Risk tier: production
Framework maintenance: yes
Owner: Jimmy Carroll
Status: draft

## Intent

An adversarial stress test of this framework on 2026-09-03 ran 24 hostile scenarios against a real
target repository. Detection held 15 for 15 against the threat the paper is written for, an agent
that forgets, drifts or is sloppy. It did not hold at all against a change that edits the policy,
edits the runner, or renames its own branch. Three bypasses were confirmed:

1. The policy and the runner sit inside the diff they judge. Rewriting `agentic.toml` so that
   `production = ["G4"]` and shipping unspecced, untested, unhandoffed source passed. Rewriting
   `tests.command` to `echo` and breaking the source passed. Editing `.agentic/gate.py` so that
   `ok = True` passed, and the printed report still listed `G2 fail` and `G5 fail` immediately
   above its own pass verdict. CI did not save any of it, because the workflow runs the branch's
   own `gate.py` and the branch's own `agentic.toml`.
2. Risk tier is asserted by the branch name and never compared with the diff. A branch named
   `docs/anything` carrying `src/billing.py` drops to internal tier, so G0, G3 and G5 are never
   required. Renaming the branch is the whole exploit.
3. `--tier` is a skip flag wearing a hat. `--tier prototype` on a `feature/*` branch cut the run
   to G4 and passed unspecced, untested source, while the README says there is no skip flag and
   this repository's own hard rules forbid override flags.

This change closes all three. It is framework maintenance: it edits the runner deliberately, and
it says so in this spec so that the new integrity gate can tell a declared change to the judge
from a silent one.

## Architecture

One new gate, G6 integrity, is added to `.agentic/gate.py` as `gate_g6_integrity`, a pure function
of `Ctx` returning a `GateResult` like every other gate. Three structural decisions:

**G6 is unconditional.** It is not listed in `agentic.toml` `[tiers.required]` and has no
configuration key of its own. `run()` appends it to the required set at every tier and every stage,
after the tier filter and after the `[stages]` filter. The alternative, making it a configurable
gate like G0 to G5, was rejected: a gate whose job is to detect edits to `agentic.toml` cannot be
switched off by editing `agentic.toml`. This is the same reasoning that already puts the secret
scan at every tier.

**G6 reads the committed diff as well as the working tree.** Protected paths are collected from
the union of the working-tree change set that the other gates use and `git diff base..HEAD`. The
working-tree view catches an agent that has edited the runner but not yet committed. The committed
view is what lets CI restore the policy and the runner from the base ref before running, without
that restore hiding the very edit it is protecting against.

**The verdict is recomputed from the recorded results.** `verdict()` derives the overall boolean
from the statuses in the report, and `enforce_verdict()` re-derives it wherever a report is
produced or consumed: at the end of `run()`, in `main()` before the report is written or printed,
and in `loop.py`. A report that records a failure can no longer carry a pass verdict. This is the
inconsistency the stress test caught in scenario S14. It is a consistency invariant, not a
containment boundary: see Out of scope.

Enforcement, honestly split:

| Where | Mechanism | Sound? |
|---|---|---|
| CI | the workflow unpacks the base ref's `.agentic/` outside the working tree and runs that copy against the tree with `--root`, so the judge is never the branch's copy | yes, up to the workflow file itself |
| local, hook, loop | G6 fails on any edit to the runner or the policy that the spec has not declared | no, it is a tripwire |

Unpacking the trusted runner outside the tree rather than restoring it into the tree is what makes
framework maintenance possible at all: the branch's own tests still import the branch's runner, so a
change to the runner can be tested by the change that makes it, while the verdict still comes from
the reviewed copy. The policy read is deliberately the branch's `agentic.toml`, so a policy change
can be exercised by the change proposing it; the trusted G6 is what stops a change granting itself a
weaker policy undeclared.

The tripwire is defeated by an author who edits G6 out of the runner in the same change. Nothing
that runs from inside a branch can defend itself against edits to itself; that is why the CI
restore exists and why `.agentic/` and `agentic.toml` want CODEOWNERS in any repository that
matters.

## Acceptance criteria

1. G6 appears in every report at every tier and every stage, and cannot be removed by
   `agentic.toml`. Covered by `tests/test_gate.py`.
2. G6 fails when the change set touches `agentic.toml`, any file under `.agentic/`, or a CI
   definition that already invoked the runner on the base ref, excluding the runtime artefacts
   `.agentic/last-report.json`, `.agentic/runs/`, `.agentic/evals/result.json` and compiled Python.
   Deletions count as changes; a CI definition that did not previously run the gate does not.
3. G6 allows that same change when the branch tier is production and a referenced spec carries a
   `Framework maintenance: yes` field, exactly that value and not a sentence containing it, and
   records which spec declared it as evidence.
4. G6 fails when the change set touches `paths.source` and the branch tier is not production, and
   names the branch, the tier and the offending files.
5. `--tier` may raise the tier above the one the branch name implies and may not lower it. A lower
   value is ignored, the branch tier stands, and G6 fails with the attempted value.
6. A report whose results contain a failure always carries `ok = false` and exits non-zero, even if
   the code that computed the verdict was tampered with to say otherwise.
7. G6 fails when no base ref can be resolved, because with no change set the policy, runner and
   tier checks cannot be performed at all.
8. The GitHub Actions and Azure Pipelines workflows unpack the base ref's `.agentic/` into a
   directory outside the working tree and run that copy with `--root .`, on pull request runs,
   printing the base commit and the policy, runner and CI changes the change proposes. Unpacking
   outside the tree is what keeps `sys.path[0]` clean, so a Python file added under `.agentic/` and
   named after a standard library module cannot be imported by the judge, and what leaves the
   branch's own tests able to import the branch's runner.
9. The existing 26 tests still pass, and the framework still passes its own gates on this branch.
10. G6 sees a protected file that is deleted, renamed out of its protected path, or changed to
    another file type. `--diff-filter=ACMR` with rename detection reports none of those.
11. A `Framework maintenance: yes` declaration authorises only the change that adds or modifies the
    spec carrying it. A declaration merged earlier is not a standing permission.
12. A CI definition is protected when the change alters the lines that invoke the runner, and not
    when it alters something else in the same file, such as a Python or action version.
13. A report whose results do not cover the gates it lists as required is not a pass, because
    `all([])` is `True`.

## Out of scope

- **Making the local run sound against a hostile author.** A runner cannot defend itself from
  inside the branch it is judging. G6 makes the attempt loud and visible in the diff; CI's restore
  from the base ref is the boundary that actually holds. Re-executing the base ref's copy of
  `gate.py` from the branch's copy was considered and rejected: the branch's copy chooses whether
  to re-execute, so it buys no soundness, and it makes every local report come from a runner that
  is not the one in the tree.
- **`paths.ai_surface` in the tier consistency check.** Criterion 4 covers `paths.source` only. An
  AI surface change on an internal branch still skips G3. Narrowing that would fail every ordinary
  documentation branch in this repository, where `AGENTS.md` is itself an AI surface.
- **`project.base_branch` tampering.** Pointing the base at the branch's own tip degrades the run
  to a whole-tree audit rather than passing trivially, but it is not detected as tampering. It is
  an edit to `agentic.toml`, so G6 fails it for that reason instead.
- **Gap 4 from the stress test**, that G4 checks an import is declared rather than that it exists,
  and gaps 5 and 6, hollow spec sections and obfuscated secrets. Separate specs, and gap 4 needs a
  human decision about whether the gate is allowed to touch the network or the environment.
- **The CI definition itself.** On a pull request event the workflow file comes from the pull
  request head, so a change that edits or deletes it decides whether any of this runs. G6 fails such
  a change, which makes it visible, but the thing that makes the gate binding is a base branch
  ruleset requiring the gate job by its job id, with review from code owners enabled and CODEOWNERS
  covering `.github/workflows/`, `.agentic/` and `agentic.toml`. That is repository configuration,
  not something a file in the repository can enforce, and it belongs in the install instructions
  rather than in the runner. A deterministic trust root would need the workflow to live outside the
  repository, as an organisation-required workflow.
- **The bootstrap.** The change that first introduces G6 cannot be judged by G6: the base ref's
  runner does not have it. That is true of any control on its first landing, and it is why this one
  is being reviewed by three models and a human rather than by itself.
- **Merge queues.** There is no `merge_group` trigger, so a repository using a merge queue needs one
  added, and needs the trusted-runner step taught about that event.
- **Path quoting outside G6.** The G6 collectors read NUL-delimited paths. `changed_files` and
  `added_lines` still read line-oriented git output, which `core.quotePath` escapes for unusual
  names. That is pre-existing and belongs in its own change.
- **Indirect invocation.** A CI definition that calls the runner through a wrapper script, a make
  target or a reusable workflow contains no literal `gate.py`, so G6 will not recognise it as part
  of the judge.
- **Sandboxing `tests.command` and `evals.command`.** They run the branch's code, by design. A
  branch that ships a test which always passes is the documented S5 case and a code review problem.
- **Push events.** The restore is guarded on pull request events. A direct push to a protected
  branch is what branch protection is for.
- Any skip, force or override flag. There is still no way to turn a gate off.

## Risk tier

Production. This file decides whether other production changes ship, and the three defects it
closes are the ones that let an unverified change through silently, in every repository where the
framework is installed. A regression here is not visible in the report it produces.

## Verification

- Criteria 1 to 7: `tests/test_gate.py`, classes `Integrity` and `EndToEnd`, each building a real
  temporary git repository and asserting the failure mode and the matching pass.
- Criterion 8: read by eye, plus `git checkout origin/main -- .agentic agentic.toml` exercised by
  hand locally. Not executed in GitHub Actions or Azure Pipelines, because this branch is not
  pushed and the private repository has no runner budget spent on it. A human must confirm the
  first real pull request run restores and reports as intended.
- Criterion 9: `python .agentic/gate.py` on this branch, and the adversarial harness at
  `C:\Users\j_car\gates-stress\harness.py` re-run against the changed framework, which asserts the
  three bypass scenarios are now blocked and the 15 detections still fire.
- Human check: confirm the tripwire and boundary split in Architecture is a trade-off worth making,
  and decide whether `.agentic/` and `agentic.toml` should carry CODEOWNERS in repositories that
  install this framework.
