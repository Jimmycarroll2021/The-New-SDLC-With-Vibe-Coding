# HANDOFF-0001

Spec: SPEC-0001
Agent: Claude Code (interactive session, human directing)
Model: claude-opus-5
Iterations: 1 interactive build, gates run after each file group; amended 2026-09-03 (Architecture section added to G0, G2 wording corrected to "alongside"; G0 accepts the Spec: field of a changed handoff as a reference; pre-commit hook mode bit fixed to 100755 for Linux clones; CI branch env vars honoured only when the repo root is the CI workspace, found by the first Actions run)

## Verified

Verified: `python -m unittest discover -s tests -q` passes (24 tests, re-run after the G0 Architecture change); `python .agentic/gate.py` on main reports ALL GATES PASS with G0 to G5 all evaluated; a deliberate secret and a hallucinated import in a scratch repo both produce G4 FAIL; `loop.py` completes with a fake agent command.

## Not verified

Not verified: `loop.py` against a real agent CLI (claude, codex, gemini) end to end; the Azure Pipelines workflow has not been executed on a hosted runner (GitHub Actions has: the first run failed on the env leak fixed here); Windows pre-commit hook execution relies on Git for Windows' sh, which is present here but not tested on a clean machine.

## Review

Reviewer: Jimmy Carroll
