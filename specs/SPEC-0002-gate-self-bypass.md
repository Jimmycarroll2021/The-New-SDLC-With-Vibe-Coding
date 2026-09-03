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

**Authorisation is resolved from an immutable snapshot, before any branch code runs.** This is the
correction a second external review forced, and it is one principle rather than a list of patches.
G6 used to answer "is this change authorised?" by reading mutable state the change itself controls:
the live filesystem, after G2 and G3 had already executed the branch's own test and eval commands. A
discovered test that appended `Framework maintenance: yes` to a spec during G2 authorised the very
runner edit G6 exists to catch, and at commit stage the declaration only had to exist in the
unstaged copy while the index carried none. So `resolve_integrity()` computes the change set, the
protected paths and the authorising declaration once, in `run()`, before the first gate is called,
and `candidate_text()` reads content from the index at commit stage and from the proposed tree in
CI. Locally the working tree is the candidate, and the ordering is what protects it.

Three conditions now make a declaration valid, each of them necessary. The authorising file has to
be the spec this change references, so G0 validated the same document the human is asked to read; it
has to satisfy G0 on its own content, so a one-line unreferenced permit file authorises nothing; and
the declaration line itself has to appear in this change's diff, so that a cosmetic edit to a spec
carrying a year-old `yes` is not a renewal.

**The whole of a gate-bearing CI definition is protected, not the lines that name the runner.**
Fingerprinting only stripped lines containing `gate.py` left the code that establishes trust outside
the boundary: the revision the workflow picks as its base, the checkout depth, the directory the
trusted runner is unpacked into, an enclosing `if:`, an injected `PYTHONPATH`, a
`continue-on-error: true`, a bare `exit 0` before the call. Swapping
`BASE_SHA="${{ github.event.pull_request.base.sha }}"` for `BASE_SHA="$(git rev-parse HEAD)"` made CI
archive `.agentic` from the pull request's own head, call it trusted, and run the candidate's runner,
with every `gate.py` line byte-identical. A definition that never ran the gate is still not protected
at all; one that did is protected in full.

**In CI the policy comes from the base ref, not from the candidate.** `resolve_policy()` reads
`agentic.toml` from the merge base at `--stage ci`, so a pull request that relaxes a rule is judged
by the rule it replaces. The exception is the same authorising channel as the runner edit: a change
that declares `Framework maintenance: yes` in the G0-valid spec it references, with the declaration
in this diff, is judged by its own `agentic.toml`, so a policy change can still be exercised by the
change proposing it. That question is answered while the base policy is in force, so a candidate
cannot steer the answer with its own `[paths]` or `[spec]` keys, and the report carries a `policy:`
line naming which was used. This is in the runner rather than in the workflow deliberately: on a
`pull_request` event the CI definition comes from the branch, so a restore written in YAML is a rule
the candidate carries, while the runner CI executes is unpacked from the base ref and is not. It is
also why the restore is not done by overwriting the file in the working tree: that would remove
`agentic.toml` from the working-tree change set, and G4 would stop scanning its added lines.

**The tier is a floor, not a veto.** A prototype- or internal-tier branch whose change set touches
`paths.source` is evaluated at production tier rather than refused. The first version of this change
refused it outright; two external reviews called that "deleting the tier system" and "a blunt
instrument", and they were right. Refusing means a repository that legitimately spikes inside its
own source tree has no way to work, while raising the tier closes the branch-rename bypass exactly:
the rename still buys nothing, because G0, G3 and G5 all run. `resolve_integrity()` applies the
floor before the protected paths and the declaration are resolved, so the authorising spec is held
to the raised tier as well.

**G4 checks that a declared import exists, offline.** Gap 4 from the stress test: an import that was
merely *declared* passed, so adding a hallucinated package to `requirements.txt` satisfied the gate,
and so did an empty directory of the same name. Existence is now resolved with
`importlib.util.find_spec` for Python and against the contents of `node_modules` for JS/TS. A name
that resolves to nothing fails; so does one that resolves only to a directory holding no importable
module, which covers both Python's implicit namespace packages and the empty-directory trick.
`local_python_modules()` correspondingly no longer treats an arbitrary top-level directory as an
importable module. The gate does not touch the network or a package index, so "exists" means
"resolves in the environment the gate runs in"; where that environment is demonstrably not the
project's - a `.venv/` the gate is not running inside, or a missing `node_modules` - the existence
half is reported as `not_applicable` with the reason, never as a silent pass.

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
4. A change set that touches `paths.source` on a prototype- or internal-tier branch is evaluated at
   production tier, and G6 records the branch, the tier it named and the offending files as
   evidence. It is not refused: the gates that the lower tier would have skipped simply run.
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
12. A CI definition that invoked the runner on the base ref is protected in its entirety. Any
    difference from the base copy counts, including one that changes no line naming `gate.py`. A
    definition that did not invoke the runner on the base ref is not protected at all.
13. A report whose results do not cover the gates it lists as required is not a pass, because
    `all([])` is `True`.
14. The change set, the protected paths and the authorising declaration are resolved before the
    first gate runs, so nothing G2 or G3 executes can change them. A test that appends
    `Framework maintenance: yes` to a spec while the suite runs does not authorise anything.
15. At commit stage the declaration is read from the index. A declaration present only in the
    unstaged working copy does not authorise the commit; staging it does.
16. The authorising file must be a spec this change references and must satisfy G0 on its own
    content. An unreferenced `specs/permit.md` containing only the field authorises nothing.
17. The `Framework maintenance` line must be introduced or altered in this change's diff. A
    cosmetic edit to a spec that already carried the declaration does not renew it.
18. A whole-tree audit does not discard deletions: a protected file removed from a clean tree on
    the base branch still fails G6. A base ref that resolves to the candidate's own tip, on any
    branch other than the project's base branch, fails G6 rather than emptying the diff.
19. The run-time exemption under `.agentic/` applies to regular files and directories only. A
    symlink or gitlink at an exempt path is protected, and the runner writes its report by
    create-and-replace so that it can never be written through such a link.
20. At `--stage ci` the policy is read from the base ref, so a change that relaxes a rule is judged
    by the rule it replaces. The report names the policy source. A change that declares framework
    maintenance in the spec it references is judged by its own `agentic.toml` instead, and that
    decision is taken while the base policy is in force. With no `agentic.toml` on the base ref the
    candidate's is used and the reason is recorded, not assumed.
21. Locally and in the hook the candidate's own policy is used, so a policy change can be worked on.
22. A declared import must resolve in the environment the gate runs in. A name in
    `requirements*.txt` or `package.json` that resolves to nothing fails G4, and so does one that
    resolves only to a directory holding no importable module. A top-level directory with no module
    in it is not a local module.
23. Where the environment cannot be observed - the project ships a `.venv/` the gate is not running
    inside, or `node_modules` is absent - the existence half of G4 is reported as
    `import existence: not_applicable` with the reason. The declaration check still applies. Nothing
    passes silently, and no check opens a socket.
24. Paths that git C-quotes in line-oriented output, such as a spec whose filename carries an
    accent, are read correctly: every git invocation runs with `core.quotePath=false`. A maintenance
    declaration in `specs/SPEC-NNNN-ñ.md` authorises, and a symlink at
    `.agentic/runs/trace-ñ.json` is still seen as a non-regular path.
25. A report that records no G6 result is not a pass, whatever else it contains. Dropping the
    failing result and the `required_gates` list together used to leave a report that re-derived as
    a pass, because an empty required list skipped the coverage check.

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
- **`project.base_branch` tampering.** Pointing the base at the branch's own tip is now refused
  outright by criterion 18, on any branch that is not the project's base branch, rather than
  degrading to a vacuous whole-tree audit. Editing the value is separately an edit to
  `agentic.toml`, so G6 fails it for that reason as well.
- **Judging a policy relaxation by the base policy outside CI.** Locally and in the hook the
  candidate's own `agentic.toml` is read, because that is the file the author is editing. The local
  protection is only that G6 fails an undeclared change to it.
- **Whether a declared package exists on a package index.** The gate resolves imports against the
  environment it runs in and never opens a socket, so a package that exists on PyPI but is not
  installed reads the same as one that does not exist. The cost is that a CI job which does not
  install the project's dependencies gets a `not_applicable` or a fail rather than a judgement;
  install them, or that half of G4 tells you nothing.
- **Gaps 5 and 6 from the stress test**, hollow spec sections and obfuscated secrets. Separate
  specs. Scenarios S1, S2, S5, S6, S7, S9 and S10 in the harness are still ALLOWED, by design.
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
- **Path quoting beyond non-ASCII.** Every git call now runs with `core.quotePath=false`, which
  covers the realistic case of an accented or CJK filename. Git still C-quotes a path containing a
  literal double quote, a backslash or a control character, and the line-oriented readers
  (`changed_files`, `added_lines`, `declaration_diff_paths`, `non_regular_paths`) would mis-parse
  those. Un-escaping C-quoted paths belongs in its own change.
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

- Criteria 1 to 7, 12 to 18 and 20 to 25: `tests/test_gate.py`, classes `Integrity` and `EndToEnd`,
  each building a real temporary git repository and asserting the failure mode and the matching
  pass. 67 tests, one skipped (see criterion 19). Every test added for criteria 20 to 25 was run
  against the previous runner first and observed to fail for the stated reason.
- Criterion 19: the protection half is tested (`test_g6_treats_a_symlinked_runtime_artefact_as_
  protected`, which builds the symlink through the git index so it runs on Windows). The write half
  is tested by `test_the_report_write_does_not_follow_a_symlink`, which **skips** on a host that
  does not permit creating symlinks, which includes the Windows box this was developed on. It has
  not been executed. A Linux CI run is what confirms it.
- Criterion 8: read by eye. Not executed in GitHub Actions or Azure Pipelines - neither workflow
  file has ever run. A human must confirm the first real pull request run unpacks the trusted runner
  and reports as intended. Criterion 20, by contrast, is executed: the policy restore lives in the
  runner, so `tests/test_gate.py` exercises it directly at `--stage ci` without a CI service.
- Criterion 9: `python .agentic/gate.py` on this branch, and the adversarial harness at
  `C:\Users\j_car\gates-stress\harness.py` re-run against the changed framework. Baseline green,
  all 15 CATCH detections fire, and S3, S4, S8, S11, S12, S13 and S14 are all BLOCKED - S8 and S13
  are the two halves of gap 4 and were ALLOWED before this change.
- Human check: confirm the tripwire and boundary split in Architecture is a trade-off worth making,
  and decide whether `.agentic/` and `agentic.toml` should carry CODEOWNERS in repositories that
  install this framework.
