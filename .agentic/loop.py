#!/usr/bin/env python3
"""
agentic-gates orchestration loop: agent -> gates -> (fail? feed the report back) -> ... -> ready.

This is the box in the diagram labelled "orchestration captures the failure and routes it back,
in minutes, with no human in the path". It is LLM-agnostic: the agent is whatever CLI command
`[loop].agent_command` names. The prompt is piped to stdin, or written to a file whose path
replaces `{prompt_file}` in the command.

  python .agentic/loop.py SPEC-0002
  python .agentic/loop.py SPEC-0002 --agent "codex exec --full-auto -" --max 3

Stops when every gate passes, or when only G5 fails and only on the Reviewer field (that field is
the human's, by design). Every iteration's prompt, agent output and gate report is written under
[loop].runs_dir so the trajectory is reviewable afterwards.
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate  # noqa: E402  (local module, same directory)


def only_reviewer_missing(rep: dict) -> bool:
    fails = [r for r in rep["results"] if r["status"] == "fail"]
    if not fails:
        return True
    if len(fails) != 1 or fails[0]["gate"] != "G5":
        return False
    return all("'Reviewer'" in e for e in fails[0]["evidence"])


def report_text(rep: dict) -> str:
    buf = io.StringIO()
    gate.print_report(rep, buf)
    return buf.getvalue()


def first_prompt(root: Path, spec_id: str, spec_text: str) -> str:
    return f"""You are working in the repository at {root}.
Read AGENTS.md first and follow its Workflow section exactly.

TASK: implement {spec_id}, reproduced below.

RULES
- Tests before code. Every acceptance criterion maps to a named test or eval.
- Do not modify agentic.toml, anything under .agentic/, or the spec's acceptance criteria.
- When you believe you are done, run `python .agentic/gate.py` and fix every FAIL it reports.
- Create or update handoffs/HANDOFF-{spec_id.split('-', 1)[1]}.md from .agentic/templates/HANDOFF.md.
  Fill Spec, Agent, Model (the model you are), Iterations, Verified, Not verified. Leave Reviewer blank.
- Finish by printing the gate report.

SPEC
{spec_text}
"""


def retry_prompt(spec_id: str, iteration: int, report: str) -> str:
    return f"""The deterministic gates FAILED after your last change to {spec_id} (iteration {iteration}).
Fix the causes listed below. Do not weaken, skip or edit any gate, and do not edit agentic.toml.
Then run `python .agentic/gate.py` again and update handoffs/HANDOFF-{spec_id.split('-', 1)[1]}.md.

GATE REPORT
{report}
"""


def run_agent(cmd: str, prompt: str, prompt_file: Path, cwd: Path, timeout: int) -> tuple[int, str]:
    prompt_file.write_text(prompt, encoding="utf-8")
    if "{prompt_file}" in cmd:
        full = cmd.replace("{prompt_file}", str(prompt_file))
        p = subprocess.run(full, shell=True, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    else:
        p = subprocess.run(cmd, shell=True, cwd=cwd, input=prompt, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="agent -> gates -> route failure back, until ready.")
    ap.add_argument("spec_id", help="e.g. SPEC-0002")
    ap.add_argument("--agent", help="override [loop].agent_command")
    ap.add_argument("--max", type=int, help="override [loop].max_iterations")
    ap.add_argument("--base", help="base ref for the gate diff (default: project.base_branch)")
    ap.add_argument("--tier", choices=list(gate.TIER_RANK))
    ap.add_argument("--agent-timeout", type=int, default=3600)
    a = ap.parse_args(argv)

    root = gate.find_root(Path.cwd().resolve())
    cfg = gate.load_config(root)
    loop_cfg = cfg.get("loop", {})
    agent_cmd = a.agent or loop_cfg.get("agent_command")
    if not agent_cmd:
        sys.exit("no agent command: set [loop].agent_command in agentic.toml or pass --agent")
    max_iter = a.max or int(loop_cfg.get("max_iterations", 4))
    spec_dir = cfg.get("paths", {}).get("specs", "specs")
    specs = list((root / spec_dir).glob(f"**/{a.spec_id}*.md"))
    if not specs:
        sys.exit(f"no spec file {spec_dir}/**/{a.spec_id}*.md. Write one from .agentic/templates/SPEC.md first.")
    spec_text = specs[0].read_text(encoding="utf-8")

    run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{a.spec_id}"
    run_dir = root / loop_cfg.get("runs_dir", ".agentic/runs") / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    trace = {"run_id": run_id, "spec": a.spec_id, "agent_command": agent_cmd, "iterations": []}

    prompt = first_prompt(root, a.spec_id, spec_text)
    outcome = "max_iterations_reached"
    for i in range(1, max_iter + 1):
        t0 = time.time()
        print(f"\n[loop] iteration {i}/{max_iter}: invoking agent")
        code, out = run_agent(agent_cmd, prompt, run_dir / f"iter-{i}.prompt.md", root, a.agent_timeout)
        (run_dir / f"iter-{i}.agent.log").write_text(out, encoding="utf-8")
        print(f"[loop] agent exited {code} after {time.time() - t0:.0f}s; running gates")
        rep = gate.enforce_verdict(gate.run(root, "local", a.base, a.tier))
        (run_dir / f"iter-{i}.gates.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
        text = report_text(rep)
        print(text)
        trace["iterations"].append({"n": i, "agent_exit": code, "seconds": round(time.time() - t0),
                                    "gates_ok": rep["ok"], "fails": [r["gate"] for r in rep["results"] if r["status"] == "fail"]})
        if rep["ok"]:
            outcome = "all_gates_pass"
            break
        if only_reviewer_missing(rep):
            outcome = "ready_for_human_review"
            break
        prompt = retry_prompt(a.spec_id, i, text)

    trace["outcome"] = outcome
    (run_dir / "trace.json").write_text(json.dumps(trace, indent=2), encoding="utf-8")
    print(f"[loop] outcome: {outcome}  (trace: {run_dir.relative_to(root)})")
    if outcome == "max_iterations_reached":
        print("[loop] the agent could not satisfy the gates. The failure is in the change or the spec, not the gates.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
