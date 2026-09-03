"""
Tests for the gate runner. Each test builds a real temporary git repository so the gates are
exercised the way they run in practice, against git's own view of the change set.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agentic"))
import gate  # noqa: E402

GOOD_AGENTS = textwrap.dedent("""\
    # AGENTS.md
    ## Stack
    - python
    ## Conventions
    - tests in tests/
    ## Hard rules
    - no secrets
    ## Workflow
    1. spec 2. tests 3. gates 4. handoff
    """)

GOOD_SPEC = textwrap.dedent("""\
    # SPEC-0007: thing
    Risk tier: production
    ## Intent
    Do the thing.
    ## Architecture
    one module
    ## Acceptance criteria
    1. it works
    ## Out of scope
    nothing
    ## Risk tier
    production because
    ## Verification
    tests/test_thing.py
    """)

GOOD_HANDOFF = textwrap.dedent("""\
    # HANDOFF-0007
    Spec: SPEC-0007
    Agent: fake-agent
    Model: fake-model-1
    Verified: gates pass
    Not verified: nothing
    Reviewer: A Human
    """)

BASE_TOML = textwrap.dedent("""\
    [project]
    name = "t"
    base_branch = "main"
    [tiers]
    default = "production"
    [tiers.branch_patterns]
    prototype = ["proto/*"]
    internal = ["internal/*"]
    production = ["main", "feature/*"]
    [tiers.required]
    prototype = ["G4"]
    internal = ["G1", "G2", "G4"]
    production = ["G0", "G1", "G2", "G3", "G4", "G5"]
    [stages]
    commit = ["G1", "G4"]
    [paths]
    source = ["src/**"]
    tests = ["tests/**"]
    ai_surface = ["prompts/**"]
    specs = "specs"
    handoffs = "handoffs"
    [spec]
    required_sections = ["Intent", "Architecture", "Acceptance criteria", "Out of scope", "Risk tier", "Verification"]
    [context]
    rule_file = "AGENTS.md"
    required_sections = ["Stack", "Conventions", "Hard rules", "Workflow"]
    max_lines = 50
    [tests]
    command = "python -c \\"import sys; sys.exit(0)\\""
    require_test_touch = true
    run_on_commit = false
    [evals]
    command = "python evalrun.py"
    result_file = "result.json"
    min_pass_rate = 0.9
    min_cases = 3
    required_dimensions = ["task_success", "hallucination"]
    allow_stub_target = false
    [review]
    check_hallucinated_imports = true
    [handoff]
    required_fields = ["Spec", "Agent", "Model", "Verified", "Not verified", "Reviewer"]
    """)


def git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        raise RuntimeError(f"git {args}: {p.stderr}")
    return p.stdout


class RepoFixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "t@example.com")
        git(self.root, "config", "user.name", "t")
        git(self.root, "config", "commit.gpgsign", "false")
        self.write("agentic.toml", BASE_TOML)
        self.write("AGENTS.md", GOOD_AGENTS)
        self.write("README.md", "hello\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")

    def write(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def branch(self, name: str):
        git(self.root, "checkout", "-q", "-b", name)

    def stage(self, *rels: str):
        git(self.root, "add", *rels)

    def commit(self, msg: str):
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", msg)

    def run(self, stage="local", tier=None):
        return gate.run(self.root, stage, "main", tier)

    def result(self, rep, g):
        return next(r for r in rep["results"] if r["gate"] == g)

    def close(self):
        self.tmp.cleanup()


def full_production_setup(fx: RepoFixture):
    """Everything a production change needs to pass G0, G1, G2, G4, G5 (G3 is N/A without prompts/)."""
    fx.branch("feature/SPEC-0007-thing")
    fx.write("specs/SPEC-0007-thing.md", GOOD_SPEC)
    fx.write("handoffs/HANDOFF-0007.md", GOOD_HANDOFF)
    fx.write("src/thing.py", "import json\nimport os\n\ndef f():\n    return json.dumps(os.sep)\n")
    fx.write("tests/test_thing.py", "def test_f():\n    pass\n")


class TierDetection(unittest.TestCase):
    def test_patterns_and_default(self):
        cfg = {"tiers": {"default": "production",
                         "branch_patterns": {"prototype": ["proto/*"], "internal": ["internal/*"]}}}
        self.assertEqual(gate.detect_tier(cfg, "proto/x"), "prototype")
        self.assertEqual(gate.detect_tier(cfg, "internal/tooling"), "internal")
        self.assertEqual(gate.detect_tier(cfg, "feature/anything"), "production")

    def test_match_any(self):
        self.assertTrue(gate.match_any("src/a/b.py", ["src/**"]))
        self.assertTrue(gate.match_any("tests/test_x.py", ["**/test_*.py"]))
        self.assertTrue(gate.match_any("AGENTS.md", ["AGENTS.md"]))
        self.assertFalse(gate.match_any("srcx/a.py", ["src/**"]))


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.fx = RepoFixture()

    def tearDown(self):
        self.fx.close()

    def test_all_pass_on_production_branch(self):
        full_production_setup(self.fx)
        rep = self.fx.run()
        self.assertEqual(rep["tier"], "production")
        statuses = {r["gate"]: r["status"] for r in rep["results"]}
        self.assertEqual(statuses["G3"], "not_applicable")
        self.assertTrue(rep["ok"], json.dumps(rep, indent=1))

    def test_prototype_tier_runs_only_g4(self):
        self.fx.branch("proto/idea")
        self.fx.write("src/x.py", "import os\n")
        rep = self.fx.run()
        self.assertEqual(rep["tier"], "prototype")
        self.assertEqual([r["gate"] for r in rep["results"]], ["G4"])
        self.assertTrue(rep["ok"])

    def test_commit_stage_restricts_to_cheap_gates(self):
        full_production_setup(self.fx)
        self.fx.stage("src/thing.py")
        rep = self.fx.run(stage="commit")
        self.assertEqual([r["gate"] for r in rep["results"]], ["G1", "G4"])

    # ---- G0
    def test_g0_fails_without_spec(self):
        self.fx.branch("feature/no-spec")
        self.fx.write("src/x.py", "import os\n")
        rep = self.fx.run()
        self.assertEqual(self.fx.result(rep, "G0")["status"], "fail")
        self.assertIn("no spec referenced", self.fx.result(rep, "G0")["reason"])

    def test_g0_fails_on_placeholders_and_missing_sections(self):
        self.fx.branch("feature/SPEC-0009-x")
        self.fx.write("specs/SPEC-0009-x.md", "# SPEC-0009\nRisk tier: production\n## Intent\n<fill me in>\n")
        rep = self.fx.run()
        g0 = self.fx.result(rep, "G0")
        self.assertEqual(g0["status"], "fail")
        self.assertTrue(any("missing sections" in e for e in g0["evidence"]))
        self.assertTrue(any("placeholders" in e for e in g0["evidence"]))

    def test_g0_fails_when_spec_tier_below_branch_tier(self):
        full_production_setup(self.fx)
        self.fx.write("specs/SPEC-0007-thing.md", GOOD_SPEC.replace("Risk tier: production", "Risk tier: prototype", 1))
        rep = self.fx.run()
        g0 = self.fx.result(rep, "G0")
        self.assertEqual(g0["status"], "fail")
        self.assertTrue(any("declares tier 'prototype'" in e for e in g0["evidence"]))

    def test_g0_finds_spec_id_in_commit_message(self):
        self.fx.branch("feature/no-id-here")
        self.fx.write("specs/SPEC-0007-thing.md", GOOD_SPEC)
        self.fx.commit("work for SPEC-0007")
        self.fx.write("handoffs/HANDOFF-0007.md", GOOD_HANDOFF)
        rep = self.fx.run()
        self.assertEqual(self.fx.result(rep, "G0")["status"], "pass")

    def test_g0_accepts_spec_reference_from_changed_handoff(self):
        self.fx.branch("feature/no-id-here")
        self.fx.write("specs/SPEC-0007-thing.md", GOOD_SPEC)
        self.fx.commit("spec landed earlier, message says nothing useful")
        self.fx.write("handoffs/HANDOFF-0007.md", GOOD_HANDOFF)        # Spec: SPEC-0007 inside
        rep = self.fx.run()
        self.assertEqual(self.fx.result(rep, "G0")["status"], "pass", self.fx.result(rep, "G0")["evidence"])

    # ---- G1
    def test_g1_fails_on_missing_section_and_length(self):
        full_production_setup(self.fx)
        self.fx.write("AGENTS.md", GOOD_AGENTS.replace("## Workflow", "## Steps") + "x\n" * 60)
        rep = self.fx.run()
        g1 = self.fx.result(rep, "G1")
        self.assertEqual(g1["status"], "fail")
        self.assertTrue(any("missing sections ['workflow']" in e for e in g1["evidence"]))
        self.assertTrue(any("limit 50" in e for e in g1["evidence"]))

    def test_g1_fails_when_rule_file_untracked(self):
        full_production_setup(self.fx)
        git(self.fx.root, "rm", "-q", "--cached", "AGENTS.md")
        rep = self.fx.run()
        g1 = self.fx.result(rep, "G1")
        self.assertEqual(g1["status"], "fail")
        self.assertTrue(any("not tracked" in e for e in g1["evidence"]))

    # ---- G2
    def test_g2_fails_when_source_changes_without_tests(self):
        full_production_setup(self.fx)
        os.remove(self.fx.root / "tests/test_thing.py")
        rep = self.fx.run()
        g2 = self.fx.result(rep, "G2")
        self.assertEqual(g2["status"], "fail")
        self.assertIn("without any test change", g2["reason"])

    def test_g2_fails_when_test_command_fails(self):
        full_production_setup(self.fx)
        toml = (self.fx.root / "agentic.toml").read_text(encoding="utf-8")
        self.fx.write("agentic.toml", toml.replace("sys.exit(0)", "sys.exit(3)"))
        rep = self.fx.run()
        g2 = self.fx.result(rep, "G2")
        self.assertEqual(g2["status"], "fail")
        self.assertIn("exited 3", g2["reason"])

    # ---- G3
    def _eval_runner(self, overall=0.95, target="real-agent", dims=("task_success", "hallucination"), cases=5, rubric="r"):
        d = {k: {"pass_rate": overall, "rubric": rubric} for k in dims}
        body = json.dumps({"target": target, "cases": cases, "overall_pass_rate": overall, "dimensions": d})
        self.fx.write("evalrun.py", f"import pathlib; pathlib.Path('result.json').write_text({body!r})\n")

    def test_g3_applies_when_ai_surface_changes_and_passes(self):
        full_production_setup(self.fx)
        self.fx.write("prompts/system.md", "be helpful\n")
        self._eval_runner()
        rep = self.fx.run()
        self.assertEqual(self.fx.result(rep, "G3")["status"], "pass")

    def test_g3_fails_on_stub_target_low_rate_missing_rubric(self):
        full_production_setup(self.fx)
        self.fx.write("prompts/system.md", "be helpful\n")
        self._eval_runner(overall=0.5, target="builtin-stub", rubric="", cases=2)
        rep = self.fx.run()
        g3 = self.fx.result(rep, "G3")
        self.assertEqual(g3["status"], "fail")
        ev = " ".join(g3["evidence"])
        for needle in ("overall_pass_rate 0.500", "built-in stub", "no rubric", "2 eval cases"):
            self.assertIn(needle, ev)

    def test_g3_fails_when_dimension_missing(self):
        full_production_setup(self.fx)
        self.fx.write("prompts/system.md", "x\n")
        self._eval_runner(dims=("task_success",))
        rep = self.fx.run()
        self.assertTrue(any("'hallucination' not scored" in e for e in self.fx.result(rep, "G3")["evidence"]))

    # ---- G4
    def test_g4_blocks_staged_secret_and_honours_allow_marker(self):
        self.fx.branch("proto/leak")
        token = "ghp_" + "A" * 36                     # built by concatenation so this file never trips the scan
        self.fx.write("src/cfg.py", f'TOKEN = "{token}"\n')
        self.fx.stage("src/cfg.py")
        rep = self.fx.run(stage="commit")
        g4 = self.fx.result(rep, "G4")
        self.assertEqual(g4["status"], "fail")
        self.assertTrue(any("src/cfg.py:1" in e for e in g4["evidence"]))
        self.fx.write("src/cfg.py", f'TOKEN = "{token}"  # agentic:allow test fixture\n')
        self.fx.stage("src/cfg.py")
        self.assertEqual(self.fx.result(self.fx.run(stage="commit"), "G4")["status"], "pass")

    def test_g4_generic_password_assignment_and_env_file(self):
        self.fx.branch("proto/leak2")
        self.fx.write("src/db.py", "password = '" + "hunter2hunter2hunter2" + "'\n")
        self.fx.write(".env", "X=1\n")
        rep = self.fx.run()
        ev = " ".join(self.fx.result(rep, "G4")["evidence"])
        self.assertIn("src/db.py:1", ev)
        self.assertIn(".env: secret-bearing", ev)

    def test_g4_hallucinated_python_import_then_declared(self):
        self.fx.branch("proto/imports")
        self.fx.write("src/mod.py", "import os\nimport totallynotapackage\nfrom . import sibling\nimport yaml\n")
        rep = self.fx.run()
        ev = " ".join(self.fx.result(rep, "G4")["evidence"])
        self.assertIn("import 'totallynotapackage'", ev)
        self.assertIn("import 'yaml'", ev)                 # not declared yet
        self.assertNotIn("import 'os'", ev)
        self.fx.write("requirements.txt", "PyYAML>=6\ntotallynotapackage==1.0  # pinned\n")
        rep = self.fx.run()
        self.assertEqual(self.fx.result(rep, "G4")["status"], "pass", self.fx.result(rep, "G4")["evidence"])

    def test_g4_local_module_import_is_fine(self):
        self.fx.branch("proto/local")
        self.fx.write("src/pkg/__init__.py", "")
        self.fx.write("src/pkg/a.py", "import pkg\nfrom pkg import b\nimport src\n")
        self.fx.write("src/pkg/b.py", "X = 1\n")
        rep = self.fx.run()
        self.assertEqual(self.fx.result(rep, "G4")["status"], "pass", self.fx.result(rep, "G4")["evidence"])

    def test_g4_js_imports_against_package_json(self):
        self.fx.branch("proto/js")
        self.fx.write("src/app.ts", "import fs from 'node:fs';\nimport React from 'react';\nimport x from './x';\nimport { z } from '@scope/pkg/sub';\nconst y = require('leftpad-hallucinated');\n")
        rep = self.fx.run()
        ev = " ".join(self.fx.result(rep, "G4")["evidence"])
        self.assertIn("'react'", ev)
        self.assertIn("'@scope/pkg'", ev)
        self.assertIn("'leftpad-hallucinated'", ev)
        self.assertNotIn("'node:fs'", ev)
        self.fx.write("package.json", json.dumps({"dependencies": {"react": "^18", "@scope/pkg": "1", "leftpad-hallucinated": "0"}}))
        self.assertEqual(self.fx.result(self.fx.run(), "G4")["status"], "pass")

    # ---- G5
    def test_g5_fails_without_handoff_or_with_placeholder(self):
        full_production_setup(self.fx)
        os.remove(self.fx.root / "handoffs/HANDOFF-0007.md")
        rep = self.fx.run()
        self.assertEqual(self.fx.result(rep, "G5")["status"], "fail")
        self.fx.write("handoffs/HANDOFF-0007.md", GOOD_HANDOFF.replace("Reviewer: A Human", "Reviewer: <human>"))
        rep = self.fx.run()
        g5 = self.fx.result(rep, "G5")
        self.assertEqual(g5["status"], "fail")
        self.assertTrue(any("'Reviewer' missing or placeholder" in e for e in g5["evidence"]))

    # ---- CLI
    def test_cli_exit_codes_and_report_file(self):
        full_production_setup(self.fx)
        p = subprocess.run([sys.executable, str(ROOT / ".agentic/gate.py"), "--root", str(self.fx.root), "--base", "main"],
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ALL GATES PASS", p.stdout)
        self.assertTrue((self.fx.root / ".agentic/last-report.json").exists())
        os.remove(self.fx.root / "handoffs/HANDOFF-0007.md")
        p = subprocess.run([sys.executable, str(ROOT / ".agentic/gate.py"), "--root", str(self.fx.root), "--base", "main", "--json"],
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(p.returncode, 1)
        self.assertFalse(json.loads(p.stdout)["ok"])


class Loop(unittest.TestCase):
    def test_loop_routes_failure_back_then_stops_on_reviewer(self):
        fx = RepoFixture()
        try:
            full_production_setup(fx)
            # the fake agent "fixes" the repo on its second invocation by writing the handoff (Reviewer blank)
            handoff = GOOD_HANDOFF.replace("Reviewer: A Human", "Reviewer:")
            os.remove(fx.root / "handoffs/HANDOFF-0007.md")
            fx.write("fake_agent.py", textwrap.dedent(f"""\
                import pathlib, sys
                prompt = sys.stdin.read()
                marker = pathlib.Path('.calls'); n = int(marker.read_text()) if marker.exists() else 0
                marker.write_text(str(n + 1))
                if 'GATE REPORT' in prompt:
                    pathlib.Path('handoffs').mkdir(exist_ok=True)
                    pathlib.Path('handoffs/HANDOFF-0007.md').write_text({handoff!r})
                print('agent done', n + 1)
                """))
            p = subprocess.run([sys.executable, str(ROOT / ".agentic/loop.py"), "SPEC-0007",
                                "--agent", f"{sys.executable} fake_agent.py", "--max", "3", "--base", "main"],
                               cwd=fx.root, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("ready_for_human_review", p.stdout)
            self.assertEqual((fx.root / ".calls").read_text(), "2")
            runs = list((fx.root / ".agentic/runs").glob("*/trace.json"))
            self.assertEqual(len(runs), 1)
            trace = json.loads(runs[0].read_text(encoding="utf-8"))
            self.assertEqual([it["fails"] for it in trace["iterations"]], [["G5"], ["G5"]])
        finally:
            fx.close()


if __name__ == "__main__":
    unittest.main()
