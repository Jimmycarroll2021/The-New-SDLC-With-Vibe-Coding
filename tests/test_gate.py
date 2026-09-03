"""
Tests for the gate runner. Each test builds a real temporary git repository so the gates are
exercised the way they run in practice, against git's own view of the change set.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

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
        self.write(".agentic/gate.py", "# stand-in for the runner, so G6 has something to protect\n")
        self.write(".github/workflows/gates.yml", textwrap.dedent("""\
            jobs:
              gates:
                steps:
                  - uses: actions/setup-python@v5
                    with:
                      python-version: "3.11"
                  - run: |
                      BASE_SHA="$PR_BASE_SHA"
                      python .agentic/gate.py --root . --stage ci --base "$BASE_SHA"
            """))
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

    def test_prototype_tier_runs_only_g4_and_the_unconditional_g6(self):
        self.fx.branch("proto/idea")
        self.fx.write("notes/x.py", "import os\n")     # not paths.source: a spike may not carry source
        rep = self.fx.run()
        self.assertEqual(rep["tier"], "prototype")
        self.assertEqual([r["gate"] for r in rep["results"]], ["G4", "G6"])
        self.assertTrue(rep["ok"], json.dumps(rep, indent=1))

    def test_ci_env_vars_ignored_for_a_repo_that_is_not_the_ci_workspace(self):
        # regression: the first GitHub Actions run leaked GITHUB_REF_NAME=main into the fixtures
        self.fx.branch("proto/idea")
        self.fx.write("notes/x.py", "import os\n")     # not paths.source: the tier floor is a separate test
        with mock.patch.dict(os.environ, {"GITHUB_WORKSPACE": "/home/runner/work/other", "GITHUB_REF_NAME": "main",
                                          "GITHUB_BASE_REF": "main"}):
            rep = self.fx.run()
            self.assertEqual(rep["tier"], "prototype")
            self.assertEqual(rep["branch"], "proto/idea")
        # honoured when the root IS the workspace. Pin every variable the code reads: on a real PR runner
        # GITHUB_HEAD_REF is set and would win, which is what made this test fail on GitHub the first time.
        with mock.patch.dict(os.environ, {"GITHUB_WORKSPACE": str(self.fx.root), "GITHUB_REF_NAME": "release/1",
                                          "GITHUB_HEAD_REF": "", "SYSTEM_PULLREQUEST_SOURCEBRANCH": "",
                                          "BUILD_SOURCEBRANCHNAME": ""}):
            self.assertEqual(self.fx.run()["branch"], "release/1")

    def test_commit_stage_restricts_to_cheap_gates(self):
        full_production_setup(self.fx)
        self.fx.stage("src/thing.py")
        rep = self.fx.run(stage="commit")
        self.assertEqual([r["gate"] for r in rep["results"]], ["G1", "G4", "G6"])

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
        # gap 4: declaring it is no longer enough. The name must also resolve in this environment.
        self.fx.write("requirements.txt", "PyYAML>=6\ntotallynotapackage==1.0  # pinned\n")
        g4 = self.fx.result(self.fx.run(), "G4")
        self.assertEqual(g4["status"], "fail")
        self.assertTrue(any("totallynotapackage" in e and "not installed" in e for e in g4["evidence"]),
                        g4["evidence"])

    def test_g4_rejects_an_import_that_only_an_empty_directory_makes_look_local(self):
        """Gap 4, the second half: a directory named after the package, with no module in it, used
        to make a hallucinated import read as a local one."""
        self.fx.branch("proto/emptydir")
        (self.fx.root / "quantum_billing_toolkit").mkdir()
        self.fx.write("quantum_billing_toolkit/README.md", "vendored later\n")
        self.fx.write("notes/fetch.py", "import quantum_billing_toolkit\n")
        g4 = self.fx.result(self.fx.run(), "G4")
        self.assertEqual(g4["status"], "fail")
        self.assertTrue(any("quantum_billing_toolkit" in e for e in g4["evidence"]), g4["evidence"])

    def test_g4_import_existence_is_not_applicable_without_the_projects_interpreter(self):
        """Never a silent pass: when the project ships a virtualenv the gate is not running in, the
        existence half is reported as not applicable, with the reason."""
        self.fx.write(".venv/pyvenv.cfg", "home = /usr/bin\n")          # on main: it predates the change
        self.fx.write(".venv/Lib/site-packages/.keep", "")
        self.fx.commit("the developer's virtualenv, tracked here only so the fixture can see it")
        self.fx.branch("proto/venv")
        self.fx.write("requirements.txt", "totallynotapackage==1.0\n")
        self.fx.write("notes/fetch.py", "import totallynotapackage\n")
        g4 = self.fx.result(self.fx.run(), "G4")
        self.assertEqual(g4["status"], "pass", g4["evidence"])
        self.assertTrue(any("import existence: not_applicable" in e for e in g4["evidence"]), g4["evidence"])

    def test_g4_a_virtualenv_the_change_brings_with_it_does_not_disable_the_check(self):
        """The not_applicable escape must not be self-issued: `mkdir .venv && touch
        .venv/pyvenv.cfg` in the change under judgement is a skip flag written in the diff."""
        self.fx.branch("proto/fakevenv")
        self.fx.write(".venv/pyvenv.cfg", "home = /usr/bin\n")
        self.fx.write(".venv/Lib/site-packages/.keep", "")
        self.fx.write("requirements.txt", "totallynotapackage==1.0\n")
        self.fx.write("notes/fetch.py", "import totallynotapackage\n")
        g4 = self.fx.result(self.fx.run(), "G4")
        self.assertEqual(g4["status"], "fail", g4["evidence"])
        self.assertTrue(any("is part of this change" in e for e in g4["evidence"]), g4["evidence"])
        self.assertTrue(any("not installed" in e for e in g4["evidence"]), g4["evidence"])

    def test_g4_a_pyvenv_marker_with_no_library_directory_does_not_disable_the_check(self):
        self.fx.write(".venv/pyvenv.cfg", "home = /usr/bin\n")
        self.fx.commit("a bare marker, no environment behind it")
        self.fx.branch("proto/barevenv")
        self.fx.write("requirements.txt", "totallynotapackage==1.0\n")
        self.fx.write("notes/fetch.py", "import totallynotapackage\n")
        g4 = self.fx.result(self.fx.run(), "G4")
        self.assertEqual(g4["status"], "fail", g4["evidence"])
        self.assertTrue(any("no library directory" in e for e in g4["evidence"]), g4["evidence"])

    def test_g4_accepts_a_namespace_package_whose_module_is_two_levels_down(self):
        """False positive guard: site-packages/google/ holds no module of its own, and a one-level
        check would fail every google-cloud-* import."""
        self.fx.branch("proto/namespace")
        (self.fx.root / "nspkg/cloud/storage").mkdir(parents=True)
        self.fx.write("nspkg/cloud/storage/__init__.py", "")
        self.fx.write("notes/use.py", "import nspkg.cloud.storage\n")
        g4 = self.fx.result(self.fx.run(), "G4")
        self.assertEqual(g4["status"], "pass", g4["evidence"])

    def test_g4_rejects_a_js_package_whose_node_modules_entry_is_empty(self):
        self.fx.branch("proto/js-empty")
        self.fx.write("package.json", json.dumps({"dependencies": {"leftpad-hallucinated": "0"}}))
        (self.fx.root / "node_modules/leftpad-hallucinated").mkdir(parents=True)
        self.fx.write("node_modules/other/package.json", "{}")     # node_modules itself is populated
        self.fx.write("notes/app.ts", "import x from 'leftpad-hallucinated';\n")
        g4 = self.fx.result(self.fx.run(), "G4")
        self.assertEqual(g4["status"], "fail")
        self.assertTrue(any("nothing Node could load" in e for e in g4["evidence"]), g4["evidence"])

    def test_g4_rejects_a_js_package_whose_manifest_has_no_entry_point(self):
        self.fx.branch("proto/js-noentry")
        self.fx.write("package.json", json.dumps({"dependencies": {"hollow-pkg": "0"}}))
        self.fx.write("node_modules/hollow-pkg/package.json", "{}")
        self.fx.write("notes/app.ts", "import x from 'hollow-pkg';\n")
        g4 = self.fx.result(self.fx.run(), "G4")
        self.assertEqual(g4["status"], "fail", g4["evidence"])

    def test_g4_resolves_the_whole_dotted_import_not_only_its_first_part(self):
        """An installed parent package does not make every submodule of it real."""
        self.assertIsNone(gate.python_module_exists("json", "json.decoder"))
        why = gate.python_module_exists("json", "json.this_submodule_does_not_exist")
        self.assertIsNotNone(why)
        self.assertIn("does not exist in it", why)

    def test_g4_a_directory_holding_one_py_file_is_not_a_package(self):
        """A directory only counts as a local module when it is really a package. Otherwise
        dropping any .py file into a directory named after the package is the empty-directory
        bypass with one extra step."""
        self.fx.branch("proto/fakelocal")
        self.fx.write("docs/quantum_billing_toolkit/helper.py", "X = 1\n")
        self.fx.write("notes/fetch.py", "import quantum_billing_toolkit\n")
        g4 = self.fx.result(self.fx.run(), "G4")
        self.assertEqual(g4["status"], "fail", g4["evidence"])

    def test_g4_does_not_prune_package_components_named_build_or_env(self):
        """`build`, `dist` and `env` are ordinary submodule names inside a package."""
        self.fx.branch("proto/vendorbuild")
        self.fx.write("vendor/build/__init__.py", "")
        self.fx.write("notes/use.py", "import vendor\n")
        g4 = self.fx.result(self.fx.run(), "G4")
        self.assertEqual(g4["status"], "pass", g4["evidence"])

    def test_g4_reads_an_editable_requirement_as_a_declaration(self):
        names, manifests = gate.declared_python_deps(self.fx.root, {})
        self.assertEqual(names, set())
        self.fx.write("requirements.txt",
                      "-e ../sharedlib\ngit+https://example.invalid/x.git#egg=vcs_pkg\n")
        names, manifests = gate.declared_python_deps(self.fx.root, {})
        self.assertIn("sharedlib", names)
        self.assertIn("vcs_pkg", names)

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
        g4 = self.fx.result(self.fx.run(), "G4")
        self.assertEqual(g4["status"], "pass")
        self.assertTrue(any("import existence: not_applicable" in e for e in g4["evidence"]), g4["evidence"])

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


class Integrity(unittest.TestCase):
    """G6: the change may not set the rules it is judged by. SPEC-0002."""

    def setUp(self):
        self.fx = RepoFixture()

    def tearDown(self):
        self.fx.close()

    def test_g6_cannot_be_removed_by_editing_the_policy(self):
        full_production_setup(self.fx)
        toml = (self.fx.root / "agentic.toml").read_text(encoding="utf-8")
        self.fx.write("agentic.toml", toml.replace('production = ["G0", "G1", "G2", "G3", "G4", "G5"]',
                                                   'production = ["G4"]'))
        rep = self.fx.run()
        self.assertIn("G6", [r["gate"] for r in rep["results"]])
        g6 = self.fx.result(rep, "G6")
        self.assertEqual(g6["status"], "fail")
        self.assertTrue(any("agentic.toml" in e for e in g6["evidence"]), g6["evidence"])
        self.assertFalse(rep["ok"])

    def test_g6_fails_on_a_runner_edit_then_allows_declared_maintenance(self):
        full_production_setup(self.fx)
        self.fx.write(".agentic/gate.py", "# stand-in\n# tampered\n")
        g6 = self.fx.result(self.fx.run(), "G6")
        self.assertEqual(g6["status"], "fail")
        self.assertTrue(any(".agentic/gate.py" in e for e in g6["evidence"]), g6["evidence"])
        self.fx.write("specs/SPEC-0007-thing.md", GOOD_SPEC.replace(
            "Risk tier: production", "Risk tier: production\nFramework maintenance: yes", 1))
        g6 = self.fx.result(self.fx.run(), "G6")
        self.assertEqual(g6["status"], "pass", g6["evidence"])
        self.assertTrue(any("framework maintenance" in e for e in g6["evidence"]), g6["evidence"])

    def test_g6_will_not_take_a_maintenance_declaration_from_a_low_tier_branch(self):
        self.fx.branch("internal/SPEC-0007-thing")
        self.fx.write("specs/SPEC-0007-thing.md", GOOD_SPEC.replace(
            "Risk tier: production", "Risk tier: production\nFramework maintenance: yes", 1))
        toml = (self.fx.root / "agentic.toml").read_text(encoding="utf-8")
        self.fx.write("agentic.toml", toml + "\n# weakened\n")
        g6 = self.fx.result(self.fx.run(), "G6")
        self.assertEqual(g6["status"], "fail")
        self.assertTrue(any("internal" in e for e in g6["evidence"]), g6["evidence"])

    def test_g6_protects_a_ci_definition_that_already_ran_the_gate(self):
        full_production_setup(self.fx)
        self.fx.write(".github/workflows/gates.yml", "run: exit 0   # gates deleted\n")
        g6 = self.fx.result(self.fx.run(), "G6")
        self.assertEqual(g6["status"], "fail")
        self.assertTrue(any("gates.yml" in e for e in g6["evidence"]), g6["evidence"])

    def test_g6_leaves_an_unrelated_new_workflow_alone(self):
        full_production_setup(self.fx)
        self.fx.write(".github/workflows/deploy.yml", "run: ./deploy.sh\n")
        g6 = self.fx.result(self.fx.run(), "G6")
        self.assertEqual(g6["status"], "pass", g6["evidence"])

    def test_g6_sees_the_runner_deleted_as_well_as_edited(self):
        full_production_setup(self.fx)
        self.fx.commit("the change")
        git(self.fx.root, "rm", "-q", ".agentic/gate.py")
        self.fx.commit("delete the runner")
        g6 = self.fx.result(self.fx.run(), "G6")
        self.assertEqual(g6["status"], "fail")
        self.assertTrue(any(".agentic/gate.py" in e for e in g6["evidence"]), g6["evidence"])

    def test_g6_maintenance_declaration_is_a_boolean_not_a_sentence(self):
        full_production_setup(self.fx)
        self.fx.write(".agentic/gate.py", "# stand-in\n# tampered\n")
        self.fx.write("specs/SPEC-0007-thing.md", GOOD_SPEC.replace(
            "Risk tier: production",
            "Risk tier: production\nFramework maintenance: yes, and G0 does not apply either", 1))
        g6 = self.fx.result(self.fx.run(), "G6")
        self.assertEqual(g6["status"], "fail", g6["evidence"])

    def test_g6_sees_the_runner_renamed_out_of_the_way(self):
        full_production_setup(self.fx)
        self.fx.commit("the change")
        git(self.fx.root, "mv", ".agentic/gate.py", "tools_gate.py")
        self.fx.commit("move the runner out of .agentic")
        g6 = self.fx.result(self.fx.run(), "G6")
        self.assertEqual(g6["status"], "fail")
        self.assertTrue(any(".agentic/gate.py" in e for e in g6["evidence"]), g6["evidence"])

    def test_g6_will_not_reuse_a_maintenance_spec_that_landed_earlier(self):
        self.fx.write("specs/SPEC-0007-thing.md", GOOD_SPEC.replace(
            "Risk tier: production", "Risk tier: production\nFramework maintenance: yes", 1))
        self.fx.commit("the maintenance declaration landed in an earlier change")
        self.fx.branch("feature/SPEC-0007-more")
        self.fx.write(".agentic/gate.py", "# stand-in\n# tampered again\n")
        g6 = self.fx.result(self.fx.run(), "G6")
        self.assertEqual(g6["status"], "fail", g6["evidence"])

    def test_g6_protects_the_ci_definition_at_commit_stage_too(self):
        self.fx.branch("feature/SPEC-0007-ci")
        self.fx.write(".github/workflows/gates.yml", "run: exit 0\n")
        self.fx.stage(".github/workflows/gates.yml")
        g6 = self.fx.result(self.fx.run(stage="commit"), "G6")
        self.assertEqual(g6["status"], "fail", g6["evidence"])

    def test_a_report_that_omits_a_required_gate_is_not_a_pass(self):
        rep = gate.enforce_verdict({"ok": True, "required_gates": ["G4", "G6"],
                                    "results": [{"gate": "G4", "status": "pass"}]})
        self.assertFalse(rep["ok"])
        self.assertIn("integrity_error", rep)
        empty = gate.enforce_verdict({"ok": True, "required_gates": ["G6"], "results": []})
        self.assertFalse(empty["ok"])           # all([]) is True, and that is not a pass

    def test_g6_ignores_runtime_artefacts_under_agentic(self):
        full_production_setup(self.fx)
        self.fx.write(".agentic/last-report.json", "{}\n")
        self.fx.write(".agentic/runs/r1/trace.json", "{}\n")
        self.fx.write(".agentic/evals/result.json", "{}\n")
        g6 = self.fx.result(self.fx.run(), "G6")
        self.assertEqual(g6["status"], "pass", g6["evidence"])

    def test_a_low_tier_branch_carrying_production_source_is_judged_at_production_tier(self):
        """The tier floor. Renaming a branch to internal/* used to drop G0, G3 and G5 silently.
        The change is now evaluated at production tier rather than refused, so the tier system
        still exists for work that really is a spike."""
        self.fx.branch("internal/sneaky")
        self.fx.write("src/billing.py", "def charge(c):\n    return c * 2\n")
        self.fx.write("tests/test_billing.py", "def test_c():\n    pass\n")
        rep = self.fx.run()
        self.assertEqual(rep["tier"], "production")
        self.assertEqual([r["gate"] for r in rep["results"]], ["G0", "G1", "G2", "G3", "G4", "G5", "G6"])
        g6 = self.fx.result(rep, "G6")
        self.assertEqual(g6["status"], "pass", g6["evidence"])
        self.assertTrue(any("src/billing.py" in e and "production tier" in e for e in g6["evidence"]),
                        g6["evidence"])
        self.assertEqual(self.fx.result(rep, "G0")["status"], "fail")   # the rename bought nothing
        self.assertEqual(self.fx.result(rep, "G5")["status"], "fail")
        self.assertFalse(rep["ok"])

    def test_the_tier_floor_cannot_be_emptied_by_the_candidates_own_paths_source(self):
        """Round five, Gemini: on a low-tier branch, empty paths.source and declare framework
        maintenance, so that CI hands the run back to the candidate's policy and the floor finds
        no source to raise the tier for. The declaration cannot help, because the tier check on a
        protected path is decided before it: framework maintenance is production work."""
        self.fx.branch("internal/sneaky")
        self.fx.write("specs/SPEC-0007-thing.md", GOOD_SPEC.replace(
            "Risk tier: production", "Risk tier: production\nFramework maintenance: yes", 1))
        toml = (self.fx.root / "agentic.toml").read_text(encoding="utf-8")
        self.fx.write("agentic.toml", toml.replace('source = ["src/**"]', "source = []"))
        self.fx.write("src/billing.py", "def charge(c):\n    return c * 2\n")
        self.fx.commit("empty paths.source and declare maintenance, on an internal branch")
        for stage in ("local", "ci"):
            rep = gate.run(self.fx.root, stage, "main", None)
            g6 = self.fx.result(rep, "G6")
            self.assertEqual(g6["status"], "fail", (stage, rep["policy"], g6["evidence"]))
            self.assertTrue(any("internal" in e for e in g6["evidence"]), (stage, g6["evidence"]))
            self.assertFalse(rep["ok"], stage)

    def test_the_tier_floor_does_not_authorise_editing_the_runner(self):
        """Round six, Codex P0: the floor raises the tier so the right gates run, but framework
        maintenance is authorised by the BRANCH's tier. Otherwise adding a source file, or passing
        --tier production, turns a spike branch into a place the runner can be edited from."""
        self.fx.branch("internal/SPEC-0007-thing")
        self.fx.write("specs/SPEC-0007-thing.md", GOOD_SPEC.replace(
            "Risk tier: production", "Risk tier: production\nFramework maintenance: yes", 1))
        self.fx.write("handoffs/HANDOFF-0007.md", GOOD_HANDOFF)
        self.fx.write("src/thing.py", "import json\n")
        self.fx.write("tests/test_thing.py", "def test_f():\n    pass\n")
        self.fx.write(".agentic/gate.py", "# stand-in\n# tampered\n")
        rep = self.fx.run()
        self.assertEqual(rep["tier"], "production")                 # the floor still applies
        g6 = self.fx.result(rep, "G6")
        self.assertEqual(g6["status"], "fail", g6["evidence"])
        self.assertTrue(any("'internal' branch" in e for e in g6["evidence"]), g6["evidence"])
        raised = self.fx.run(tier="production")                     # nor does raising it by hand
        self.assertEqual(self.fx.result(raised, "G6")["status"], "fail")

    def test_ci_will_not_take_the_candidate_policy_the_base_policy_would_reject(self):
        """Round six, Codex P0: declaring maintenance is not on its own enough to be handed the
        candidate's own policy. A change that rewrites [tiers.branch_patterns] so its internal
        branch reads as production would otherwise be judged by the very rewrite."""
        self.fx.branch("internal/SPEC-0007-thing")
        self.fx.write("specs/SPEC-0007-thing.md", GOOD_SPEC.replace(
            "Risk tier: production", "Risk tier: production\nFramework maintenance: yes", 1))
        self.fx.write("handoffs/HANDOFF-0007.md", GOOD_HANDOFF)
        toml = (self.fx.root / "agentic.toml").read_text(encoding="utf-8")
        self.fx.write("agentic.toml", toml.replace('production = ["main", "feature/*"]',
                                                   'production = ["main", "feature/*", "internal/*"]'))
        self.fx.commit("reclassify my own branch, then declare maintenance")
        rep = gate.run(self.fx.root, "ci", "main", None)
        self.assertIn("base ref", rep["policy"])
        g6 = self.fx.result(rep, "G6")
        self.assertEqual(g6["status"], "fail", (rep["policy"], g6["evidence"]))
        self.assertFalse(rep["ok"])

    def test_ci_will_not_take_a_declaration_that_is_not_in_the_proposed_tree(self):
        """Round six, Codex P0: at CI stage the proposed tree is the candidate. A declaration that
        exists only in the working tree is not part of what the pull request proposes."""
        self.fx.branch("feature/SPEC-0007-thing")
        self.fx.write("specs/SPEC-0007-thing.md", GOOD_SPEC)
        self.fx.write("handoffs/HANDOFF-0007.md", GOOD_HANDOFF)
        self.fx.write(".agentic/gate.py", "# stand-in\n# tampered\n")
        self.fx.commit("edit the runner, declare nothing")
        self.fx.write("specs/SPEC-0007-thing.md", GOOD_SPEC.replace(
            "Risk tier: production", "Risk tier: production\nFramework maintenance: yes", 1))
        g6 = self.fx.result(gate.run(self.fx.root, "ci", "main", None), "G6")
        self.assertEqual(g6["status"], "fail", g6["evidence"])
        self.fx.write("specs/SPEC-0007-untracked.md", GOOD_SPEC.replace(
            "Risk tier: production", "Risk tier: production\nFramework maintenance: yes", 1))
        g6 = self.fx.result(gate.run(self.fx.root, "ci", "main", None), "G6")
        self.assertEqual(g6["status"], "fail", g6["evidence"])

    def test_ci_does_not_take_its_base_branch_from_the_candidates_own_policy(self):
        """Round six, Codex P0: project.base_branch lives in the file the change may be editing.
        Pointing it at the branch's own tip emptied the diff, which is a skip flag."""
        self.fx.branch("feature/SPEC-0007-thing")
        toml = (self.fx.root / "agentic.toml").read_text(encoding="utf-8")
        self.fx.write("agentic.toml", toml.replace('base_branch = "main"',
                                                   'base_branch = "feature/SPEC-0007-thing"'))
        self.fx.commit("judge me against myself")
        ci_vars = {"GITHUB_BASE_REF": "", "GITHUB_WORKSPACE": "", "GITHUB_HEAD_REF": "",
                   "GITHUB_REF_NAME": "", "SYSTEM_PULLREQUEST_TARGETBRANCH": "",
                   "SYSTEM_PULLREQUEST_SOURCEBRANCH": "", "BUILD_SOURCESDIRECTORY": "",
                   "BUILD_SOURCEBRANCHNAME": ""}
        with mock.patch.dict(os.environ, ci_vars):
            rep = gate.run(self.fx.root, "ci", None, None)
        self.assertEqual(rep["base"], "main")
        g6 = self.fx.result(rep, "G6")
        self.assertEqual(g6["status"], "fail", g6["evidence"])
        self.assertTrue(any("agentic.toml" in e for e in g6["evidence"]), g6["evidence"])

    def test_a_virtualenv_the_test_run_creates_does_not_disable_the_existence_check(self):
        """Round six, Codex P0: the environment decision is part of the snapshot. G2 runs the
        branch's own test command, so a test that creates a virtualenv marker would otherwise turn
        every G4 existence failure into not_applicable."""
        full_production_setup(self.fx)
        self.fx.write(".gitignore", ".venv/\n")
        self.fx.write("requirements.txt", "totallynotapackage==1.0\n")
        self.fx.write("src/thing.py", "import totallynotapackage\n")
        self.fx.write("mkvenv.py", textwrap.dedent("""\
            import pathlib
            p = pathlib.Path('.venv'); (p / 'Lib' / 'site-packages').mkdir(parents=True, exist_ok=True)
            (p / 'pyvenv.cfg').write_text('home = /usr/bin\\n', encoding='utf-8')
            """))
        toml = (self.fx.root / "agentic.toml").read_text(encoding="utf-8")
        self.fx.write("agentic.toml", "\n".join(
            'command = "python mkvenv.py"' if l.startswith('command = "python -c') else l
            for l in toml.splitlines()) + "\n")
        rep = self.fx.run()
        self.assertTrue((self.fx.root / ".venv/pyvenv.cfg").is_file(),
                        "the test command did not run, so this proves nothing")
        g4 = self.fx.result(rep, "G4")
        self.assertEqual(g4["status"], "fail", g4["evidence"])
        self.assertTrue(any("not installed" in e for e in g4["evidence"]), g4["evidence"])

    def test_the_report_write_does_not_follow_a_symlinked_temporary_file(self):
        full_production_setup(self.fx)
        victim = self.fx.root / "agentic.toml"
        link = self.fx.root / ".agentic" / f".last-report.{os.getpid()}.tmp"
        try:
            link.parent.mkdir(exist_ok=True)
            os.symlink(victim, link)
        except (OSError, NotImplementedError, AttributeError) as e:
            self.skipTest(f"this host does not permit creating symlinks: {e}")
        self.assertTrue(link.is_symlink(), "the symlink was not created, so this proves nothing")
        before = victim.read_text(encoding="utf-8")
        # in-process, not a subprocess: the temporary name carries os.getpid(), so a subprocess
        # writes under a different pid and touches neither the link nor the path it stands on
        with mock.patch.object(sys, "stdout", io.StringIO()):
            gate.main(["--root", str(self.fx.root), "--base", "main"])
        self.assertEqual(victim.read_text(encoding="utf-8"), before,
                         "the report was written through the temporary symlink, into the policy")

    def test_the_tier_floor_leaves_a_branch_that_carries_no_source_alone(self):
        self.fx.branch("proto/idea")
        self.fx.write("notes/sketch.md", "an idea\n")
        rep = self.fx.run()
        self.assertEqual(rep["tier"], "prototype")
        self.assertEqual([r["gate"] for r in rep["results"]], ["G4", "G6"])
        self.assertTrue(rep["ok"], json.dumps(rep, indent=1))

    # ---- decision (a): in CI a policy relaxation is judged by the policy it replaces

    def test_ci_judges_an_undeclared_policy_relaxation_by_the_base_policy(self):
        full_production_setup(self.fx)
        self.fx.commit("the change")
        toml = (self.fx.root / "agentic.toml").read_text(encoding="utf-8")
        self.fx.write("agentic.toml", toml.replace("max_lines = 50", "max_lines = 5000"))
        self.fx.write("AGENTS.md", GOOD_AGENTS + "x\n" * 60)   # legal under 5000, illegal under 50
        self.fx.commit("relax the policy, then rely on the relaxation")
        local = gate.run(self.fx.root, "local", "main", None)
        self.assertEqual(local["policy"], "candidate")
        self.assertEqual(self.fx.result(local, "G1")["status"], "pass")     # its own policy, locally
        rep = gate.run(self.fx.root, "ci", "main", None)
        self.assertIn("base ref", rep["policy"])
        self.assertEqual(self.fx.result(rep, "G1")["status"], "fail", rep["policy"])
        self.assertTrue(any("limit 50" in e for e in self.fx.result(rep, "G1")["evidence"]))
        self.assertEqual(self.fx.result(rep, "G6")["status"], "fail")       # and it is undeclared

    def test_ci_uses_the_candidate_policy_when_framework_maintenance_is_declared(self):
        full_production_setup(self.fx)
        self.fx.write("specs/SPEC-0007-thing.md", GOOD_SPEC.replace(
            "Risk tier: production", "Risk tier: production\nFramework maintenance: yes", 1))
        toml = (self.fx.root / "agentic.toml").read_text(encoding="utf-8")
        self.fx.write("agentic.toml", toml.replace("max_lines = 50", "max_lines = 5000"))
        self.fx.write("AGENTS.md", GOOD_AGENTS + "x\n" * 60)
        self.fx.commit("declared framework maintenance")
        rep = gate.run(self.fx.root, "ci", "main", None)
        self.assertIn("framework maintenance declared", rep["policy"])
        self.assertEqual(self.fx.result(rep, "G1")["status"], "pass", rep["policy"])
        self.assertEqual(self.fx.result(rep, "G6")["status"], "pass",
                         self.fx.result(rep, "G6")["evidence"])

    def test_ci_falls_back_to_the_candidate_policy_when_the_base_ref_has_none(self):
        """First install: the base ref predates agentic.toml, so there is no old policy to judge by
        and the reason is recorded rather than assumed."""
        full_production_setup(self.fx)
        self.fx.commit("the change")
        rep = gate.run(self.fx.root, "ci", "no-such-ref", None)
        self.assertIn("candidate", rep["policy"])
        self.assertIn("no base ref", rep["policy"])

    def test_g6_reads_the_committed_diff_so_a_ci_restore_cannot_hide_the_edit(self):
        full_production_setup(self.fx)
        self.fx.commit("the change")
        toml = (self.fx.root / "agentic.toml").read_text(encoding="utf-8")
        self.fx.write("agentic.toml", toml.replace("max_lines = 50", "max_lines = 5000"))
        self.fx.commit("weaken the policy")
        git(self.fx.root, "checkout", "main", "--", "agentic.toml")     # what CI does before running
        self.assertNotIn("max_lines = 5000", (self.fx.root / "agentic.toml").read_text(encoding="utf-8"))
        g6 = self.fx.result(self.fx.run(), "G6")
        self.assertEqual(g6["status"], "fail")
        self.assertTrue(any("agentic.toml" in e for e in g6["evidence"]), g6["evidence"])

    def test_tier_override_may_raise(self):
        self.fx.branch("proto/idea")
        self.fx.write("notes/x.md", "x\n")
        rep = self.fx.run(tier="production")
        self.assertEqual(rep["tier"], "production")
        self.assertEqual(self.fx.result(rep, "G6")["status"], "pass")

    def test_tier_override_may_not_lower(self):
        full_production_setup(self.fx)
        rep = self.fx.run(tier="prototype")
        self.assertEqual(rep["tier"], "production")
        self.assertEqual([r["gate"] for r in rep["results"]], ["G0", "G1", "G2", "G3", "G4", "G5", "G6"])
        g6 = self.fx.result(rep, "G6")
        self.assertEqual(g6["status"], "fail")
        self.assertTrue(any("--tier prototype" in e for e in g6["evidence"]), g6["evidence"])
        self.assertFalse(rep["ok"])

    def test_g6_fails_without_a_resolvable_base(self):
        full_production_setup(self.fx)
        rep = gate.run(self.fx.root, "local", "no-such-ref", None)
        g6 = self.fx.result(rep, "G6")
        self.assertEqual(g6["status"], "fail")
        self.assertIn("no base ref", " ".join(g6["evidence"]))

    def test_verdict_is_a_function_of_the_recorded_results(self):
        rep = {"ok": True, "results": [{"gate": "G2", "status": "fail"}, {"gate": "G4", "status": "pass"}]}
        out = gate.enforce_verdict(rep)
        self.assertFalse(out["ok"])
        self.assertIn("integrity_error", out)
        clean = gate.enforce_verdict({"ok": True, "results": [{"gate": "G4", "status": "not_applicable"},
                                                              {"gate": "G6", "status": "pass"}]})
        self.assertTrue(clean["ok"])
        self.assertNotIn("integrity_error", clean)

    def test_a_report_with_no_g6_result_is_not_a_pass(self):
        """Dropping the failing result and the required_gates list together used to leave a report
        that re-derived as a pass, because an empty required list skipped the coverage check."""
        rep = gate.enforce_verdict({"ok": True, "results": [{"gate": "G0", "status": "pass"}]})
        self.assertFalse(rep["ok"])
        self.assertIn("integrity_error", rep)

    def test_a_tampered_runner_cannot_report_a_pass(self):
        """S14 from the stress test: ok = True spliced into the runner, in the change it judges."""
        full_production_setup(self.fx)
        os.remove(self.fx.root / "handoffs/HANDOFF-0007.md")          # guarantees a real G5 failure
        src = (ROOT / ".agentic/gate.py").read_text(encoding="utf-8")
        tampered = src.replace("    ok = verdict(results)", "    ok = True")
        self.assertNotEqual(src, tampered, "the verdict line moved: update this test")
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "gate.py"
            fake.write_text(tampered, encoding="utf-8")
            p = subprocess.run([sys.executable, str(fake), "--root", str(self.fx.root), "--base", "main", "--json"],
                               capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        rep = json.loads(p.stdout)
        self.assertFalse(rep["ok"])
        self.assertIn("integrity_error", rep)


    # ---- authorisation must come from an immutable snapshot, resolved before branch code runs

    def test_g6_protects_a_ci_file_beyond_the_lines_that_name_the_runner(self):
        """P0: swapping the base revision the workflow trusts, while every line containing
        gate.py stays byte-identical, used to read as an ordinary CI edit."""
        full_production_setup(self.fx)
        wf = (self.fx.root / ".github/workflows/gates.yml").read_text(encoding="utf-8")
        tampered = wf.replace('BASE_SHA="$PR_BASE_SHA"', 'BASE_SHA="$(git rev-parse HEAD)"')
        self.assertNotEqual(wf, tampered)
        self.assertEqual([l for l in wf.splitlines() if "gate.py" in l],
                         [l for l in tampered.splitlines() if "gate.py" in l],
                         "the exploit is that no line naming gate.py changes")
        self.fx.write(".github/workflows/gates.yml", tampered)
        g6 = self.fx.result(self.fx.run(), "G6")
        self.assertEqual(g6["status"], "fail")
        self.assertTrue(any("gates.yml" in e for e in g6["evidence"]), g6["evidence"])

    def test_g6_protects_every_line_of_a_ci_file_that_runs_the_gate(self):
        """The consequence of the above, stated as its own case: there is no longer an unrelated
        edit to a gate-bearing CI definition. A version bump in that file is declared like any
        other change to the judge."""
        full_production_setup(self.fx)
        wf = (self.fx.root / ".github/workflows/gates.yml").read_text(encoding="utf-8")
        self.fx.write(".github/workflows/gates.yml", wf.replace('"3.11"', '"3.12"'))
        g6 = self.fx.result(self.fx.run(), "G6")
        self.assertEqual(g6["status"], "fail", g6["evidence"])

    def test_g6_reads_the_declaration_from_the_index_at_commit_stage(self):
        """P0: the hook judges what is about to be committed. Declaring maintenance only in the
        unstaged copy used to authorise a commit whose index carried no declaration."""
        self.fx.branch("feature/SPEC-0007-thing")
        self.fx.write("specs/SPEC-0007-thing.md", GOOD_SPEC)
        self.fx.write(".agentic/gate.py", "# stand-in\n# tampered\n")
        self.fx.stage("specs/SPEC-0007-thing.md", ".agentic/gate.py")
        declared = GOOD_SPEC.replace("Risk tier: production",
                                     "Risk tier: production\nFramework maintenance: yes", 1)
        self.fx.write("specs/SPEC-0007-thing.md", declared)       # working tree only
        g6 = self.fx.result(self.fx.run(stage="commit"), "G6")
        self.assertEqual(g6["status"], "fail", g6["evidence"])
        self.fx.stage("specs/SPEC-0007-thing.md")                 # now it is really being committed
        g6 = self.fx.result(self.fx.run(stage="commit"), "G6")
        self.assertEqual(g6["status"], "pass", g6["evidence"])

    def test_g6_will_not_take_a_declaration_the_test_run_wrote(self):
        """P0: G2 executes the branch's own test command, so anything G6 read from the filesystem
        afterwards would be state the change had the chance to write."""
        full_production_setup(self.fx)
        self.fx.write("mutate.py", textwrap.dedent("""\
            import pathlib
            p = pathlib.Path('specs/SPEC-0007-thing.md')
            p.write_text(p.read_text(encoding='utf-8') + 'Framework maintenance: yes\\n', encoding='utf-8')
            """))
        toml = (self.fx.root / "agentic.toml").read_text(encoding="utf-8")
        self.fx.write("agentic.toml", "\n".join(
            'command = "python mutate.py"' if l.startswith('command = "python -c') else l
            for l in toml.splitlines()) + "\n")
        self.fx.write(".agentic/gate.py", "# stand-in\n# tampered\n")
        rep = self.fx.run()
        self.assertIn("Framework maintenance: yes",
                      (self.fx.root / "specs/SPEC-0007-thing.md").read_text(encoding="utf-8"),
                      "the test command did not run, so this proves nothing")
        g6 = self.fx.result(rep, "G6")
        self.assertEqual(g6["status"], "fail", g6["evidence"])

    def test_g6_will_not_renew_an_old_declaration_with_a_cosmetic_edit(self):
        """P0: the declaration itself has to be in the diff. Touching the file around one that
        landed a year ago is not a new declaration."""
        self.fx.write("specs/SPEC-0007-thing.md", GOOD_SPEC.replace(
            "Risk tier: production", "Risk tier: production\nFramework maintenance: yes", 1))
        self.fx.commit("the maintenance declaration landed in an earlier change")
        self.fx.branch("feature/SPEC-0007-more")
        old = (self.fx.root / "specs/SPEC-0007-thing.md").read_text(encoding="utf-8")
        self.fx.write("specs/SPEC-0007-thing.md", old + "\nA typo fixed much later.\n")
        self.fx.write(".agentic/gate.py", "# stand-in\n# tampered again\n")
        g6 = self.fx.result(self.fx.run(), "G6")
        self.assertEqual(g6["status"], "fail", g6["evidence"])
        self.assertTrue(any("neither adds" in e for e in g6["evidence"]), g6["evidence"])

    def test_g6_reads_a_declaration_in_a_spec_whose_name_is_not_ascii(self):
        """git C-quotes unusual path names in line-oriented output, so the declaration in a spec
        with an accented filename was invisible to every check that renews it."""
        self.fx.branch("feature/SPEC-0007-thing")
        self.fx.write("specs/SPEC-0007-\u00f1.md", GOOD_SPEC.replace(
            "Risk tier: production", "Risk tier: production\nFramework maintenance: yes", 1))
        self.fx.write("handoffs/HANDOFF-0007.md", GOOD_HANDOFF)
        self.fx.write(".agentic/gate.py", "# stand-in\n# tampered\n")
        rep = self.fx.run()
        self.assertEqual(self.fx.result(rep, "G0")["status"], "pass")
        g6 = self.fx.result(rep, "G6")
        self.assertEqual(g6["status"], "pass", g6["evidence"])

    def test_g6_sees_a_symlinked_runtime_artefact_whose_name_is_not_ascii(self):
        self.fx.branch("feature/SPEC-0007-thing")
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.fx.root,
                              input="../../agentic.toml", capture_output=True, text=True,
                              encoding="utf-8")
        sha = blob.stdout.strip()
        self.assertTrue(sha, blob.stderr)
        git(self.fx.root, "update-index", "--add", "--cacheinfo",
            f"120000,{sha},.agentic/runs/trace-\u00f1.json")
        g6 = self.fx.result(self.fx.run(stage="commit"), "G6")
        self.assertEqual(g6["status"], "fail", g6["evidence"])
        self.assertTrue(any("trace-" in e for e in g6["evidence"]), g6["evidence"])

    def test_g6_will_not_take_a_declaration_from_an_unreferenced_permit_file(self):
        """P0: a one-line specs/permit.md that G0 never looked at authorises nothing."""
        full_production_setup(self.fx)
        self.fx.write("specs/permit.md", "Framework maintenance: yes\n")
        self.fx.write(".agentic/gate.py", "# stand-in\n# tampered\n")
        rep = self.fx.run()
        self.assertEqual(self.fx.result(rep, "G0")["status"], "pass")
        g6 = self.fx.result(rep, "G6")
        self.assertEqual(g6["status"], "fail", g6["evidence"])
        self.assertTrue(any("permit.md" in e and "does not reference it" in e for e in g6["evidence"]),
                        g6["evidence"])

    def test_g6_will_not_take_a_declaration_from_a_spec_that_fails_g0(self):
        full_production_setup(self.fx)
        self.fx.write("specs/SPEC-0007-thing.md",
                      "# SPEC-0007\nRisk tier: production\nFramework maintenance: yes\n## Intent\nx\n")
        self.fx.write(".agentic/gate.py", "# stand-in\n# tampered\n")
        rep = self.fx.run()
        self.assertEqual(self.fx.result(rep, "G0")["status"], "fail")
        g6 = self.fx.result(rep, "G6")
        self.assertEqual(g6["status"], "fail", g6["evidence"])
        self.assertTrue(any("does not satisfy G0" in e for e in g6["evidence"]), g6["evidence"])

    def test_g6_refuses_a_base_that_is_the_candidates_own_tip(self):
        """P1: --base HEAD empties the diff and used to select the vacuous whole-tree mode, which
        is a skip flag by another name."""
        full_production_setup(self.fx)
        self.fx.write(".agentic/gate.py", "# stand-in\n# tampered\n")
        self.fx.commit("edit the runner, then judge it against my own tip")
        g6 = self.fx.result(gate.run(self.fx.root, "local", "HEAD", None), "G6")
        self.assertEqual(g6["status"], "fail", g6["evidence"])
        self.assertTrue(any("own tip" in e for e in g6["evidence"]), g6["evidence"])

    def test_g6_still_sees_a_deletion_in_a_whole_tree_audit(self):
        """P1: whole-tree mode used to empty the change set outright, discarding deletions that
        vanished_files() had already caught."""
        os.remove(self.fx.root / ".agentic/gate.py")      # on main, otherwise clean
        rep = self.fx.run()
        self.assertIn("whole-tree audit", rep["changed_mode"])
        g6 = self.fx.result(rep, "G6")
        self.assertEqual(g6["status"], "fail", g6["evidence"])
        self.assertTrue(any(".agentic/gate.py" in e for e in g6["evidence"]), g6["evidence"])

    def test_g6_passes_a_clean_whole_tree_audit_on_the_base_branch(self):
        g6 = self.fx.result(self.fx.run(), "G6")
        self.assertEqual(g6["status"], "pass", g6["evidence"])
        self.assertTrue(any("nothing to diff" in e for e in g6["evidence"]), g6["evidence"])

    def test_g6_treats_a_symlinked_runtime_artefact_as_protected(self):
        """P1: .agentic/last-report.json -> ../agentic.toml is not an artefact. It turns the
        runner's own report write into an overwrite of the policy."""
        self.fx.branch("feature/SPEC-0007-thing")
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.fx.root,
                              input="../agentic.toml", capture_output=True, text=True,
                              encoding="utf-8")
        sha = blob.stdout.strip()
        self.assertTrue(sha, blob.stderr)
        git(self.fx.root, "update-index", "--add", "--cacheinfo",
            f"120000,{sha},.agentic/last-report.json")
        g6 = self.fx.result(self.fx.run(stage="commit"), "G6")
        self.assertEqual(g6["status"], "fail", g6["evidence"])
        self.assertTrue(any("last-report.json" in e for e in g6["evidence"]), g6["evidence"])

    def test_the_report_write_does_not_follow_a_symlink(self):
        full_production_setup(self.fx)
        victim = self.fx.root / "agentic.toml"
        link = self.fx.root / ".agentic/last-report.json"
        try:
            os.symlink(victim, link)
        except (OSError, NotImplementedError, AttributeError) as e:
            self.skipTest(f"this host does not permit creating symlinks: {e}")
        before = victim.read_text(encoding="utf-8")
        subprocess.run([sys.executable, str(ROOT / ".agentic/gate.py"), "--root", str(self.fx.root),
                        "--base", "main"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(victim.read_text(encoding="utf-8"), before,
                         "the report was written through the symlink, into the policy")


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


class Round7(unittest.TestCase):
    """Findings from the seventh external review round, on the round-six fix commits.

    Every test here failed against the runner immediately before its fix landed.
    """

    def setUp(self):
        self.fx = RepoFixture()

    def tearDown(self):
        self.fx.close()

    # --- a file module cannot carry submodules -------------------------------------
    def test_a_dotted_import_through_a_file_module_cannot_resolve(self):
        """`json.decoder` is a .py file, so `json.decoder.anything` does not exist. The walk used
        to set found=True without descending, leaving locs empty, and the next turn read that as
        'not observable on disk' and passed."""
        why = gate.python_module_exists("json", "json.decoder.no_such_child")
        self.assertIsNotNone(why, "a submodule of a file module was reported as real")
        self.assertIn("not a package", why)

    def test_a_top_level_file_module_cannot_carry_submodules_either(self):
        why = gate.python_module_exists("base64", "base64.no_such_child")
        self.assertIsNotNone(why, "a submodule of a top-level file module was reported as real")
        self.assertIn("not a package", why)

    def test_a_module_that_publishes_its_own_submodule_still_resolves(self):
        """os is a module, not a package, but os.py registers os.path in sys.modules. Observing
        the filesystem is not enough to call that import hallucinated."""
        self.assertIsNone(gate.python_module_exists("os", "os.path"))

    def test_a_real_dotted_package_import_still_resolves(self):
        self.assertIsNone(gate.python_module_exists("json", "json.decoder"))
        self.assertIsNone(gate.python_module_exists("collections", "collections.abc"))

    # --- G4 reads the candidate, not what the branch's own test command left behind --
    def test_g2_cannot_rewrite_the_source_g4_then_reads(self):
        """G2 runs the branch's own test command. If G4 reads the working tree afterwards, a test
        that rewrites the offending file turns a committed bad import into a G4 pass."""
        self.fx.branch("feature/SPEC-0007-thing")
        self.fx.write("specs/SPEC-0007-thing.md", GOOD_SPEC)
        self.fx.write("handoffs/HANDOFF-0007.md", GOOD_HANDOFF)
        self.fx.write("src/thing.py", "import totally_hallucinated_pkg\n")
        self.fx.write("tests/test_thing.py", "def test_f():\n    pass\n")
        toml = (self.fx.root / "agentic.toml").read_text(encoding="utf-8").replace(
            'command = "python -c \\"import sys; sys.exit(0)\\""',
            'command = "python -c \\"import pathlib; '
            'pathlib.Path(\'src/thing.py\').write_text(\'import json\\\\n\')\\""')
        self.fx.write("agentic.toml", toml)
        self.fx.commit("a candidate whose test command launders its own source")
        rep = self.fx.run()
        self.assertEqual(git(self.fx.root, "show", "HEAD:src/thing.py").strip(),
                         "import totally_hallucinated_pkg", "the committed source should be untouched")
        g4 = self.fx.result(rep, "G4")
        self.assertEqual(g4["status"], "fail", g4["evidence"])
        self.assertTrue(any("totally_hallucinated_pkg" in e for e in g4["evidence"]), g4["evidence"])

    # --- a declaration is renewed by the line this change adds, not by an old one ----
    def test_an_added_line_does_not_renew_an_older_declaration_above_it(self):
        """field_value reads the first occurrence in the file, while the diff check only asked
        whether some line naming the field was added. A fenced 'no' therefore renewed an old yes."""
        old = GOOD_SPEC.replace("Risk tier: production",
                                "Risk tier: production\nFramework maintenance: yes")
        self.fx.write("specs/SPEC-0007-thing.md", old)
        self.fx.commit("a maintenance spec that landed long ago")
        self.fx.branch("feature/SPEC-0007-thing")
        self.fx.write("specs/SPEC-0007-thing.md", old + "\n```\nFramework maintenance: no\n```\n")
        self.fx.write(".agentic/gate.py", "# the runner, edited by this change\nX = 1\n")
        self.fx.commit("edit the runner on the back of an old permission")
        ctx = gate.build_ctx(gate.Repo(self.fx.root), gate.load_config(self.fx.root),
                             "local", "main", None)
        declared, _ = gate.resolve_declaration(ctx)
        self.assertIsNone(declared, "an old declaration was renewed by a line that does not declare")

    def test_a_declaration_this_change_actually_adds_is_still_honoured(self):
        self.fx.branch("feature/SPEC-0007-thing")
        self.fx.write("specs/SPEC-0007-thing.md",
                      GOOD_SPEC.replace("Risk tier: production",
                                        "Risk tier: production\nFramework maintenance: yes"))
        self.fx.write(".agentic/gate.py", "# the runner, edited by this change\nX = 1\n")
        self.fx.commit("declared framework maintenance")
        ctx = gate.build_ctx(gate.Repo(self.fx.root), gate.load_config(self.fx.root),
                             "local", "main", None)
        declared, notes = gate.resolve_declaration(ctx)
        self.assertEqual(declared, "specs/SPEC-0007-thing.md", notes)

    # --- in CI the candidate policy is the committed one ----------------------------
    def test_an_uncommitted_policy_does_not_choose_the_gate_list_in_ci(self):
        """Once maintenance is declared the candidate's own policy applies. It has to be the
        policy the candidate is proposing, not whatever the checkout happens to hold."""
        self.fx.branch("feature/SPEC-0007-thing")
        self.fx.write("specs/SPEC-0007-thing.md",
                      GOOD_SPEC.replace("Risk tier: production",
                                        "Risk tier: production\nFramework maintenance: yes"))
        self.fx.write("handoffs/HANDOFF-0007.md", GOOD_HANDOFF)
        self.fx.write(".agentic/gate.py", "# a declared maintenance edit\nX = 1\n")
        self.fx.write("tests/test_thing.py", "def test_f():\n    pass\n")
        self.fx.commit("declared framework maintenance")
        toml = (self.fx.root / "agentic.toml").read_text(encoding="utf-8").replace(
            'production = ["G0", "G1", "G2", "G3", "G4", "G5"]', "production = []")
        self.fx.write("agentic.toml", toml)          # deliberately NOT committed
        rep = gate.run(self.fx.root, "ci", "main", None)
        self.assertNotEqual(rep["required_gates"], ["G6"],
                            "an uncommitted agentic.toml emptied the gate list")
        self.assertIn("G0", rep["required_gates"])

    # --- valid framework maintenance may re-tier its own branch pattern -------------
    def test_declared_maintenance_may_move_its_own_branch_pattern(self):
        """The tier that decides whether the runner may be edited is the tier in force, which is
        the base ref's. Recomputing it from the candidate rejected the very change that moves it."""
        self.fx.branch("feature/SPEC-0007-thing")
        self.fx.write("specs/SPEC-0007-thing.md",
                      GOOD_SPEC.replace("Risk tier: production",
                                        "Risk tier: production\nFramework maintenance: yes"))
        self.fx.write("handoffs/HANDOFF-0007.md", GOOD_HANDOFF)
        self.fx.write("tests/test_thing.py", "def test_f():\n    pass\n")
        toml = (self.fx.root / "agentic.toml").read_text(encoding="utf-8")
        toml = toml.replace('internal = ["internal/*"]', 'internal = ["internal/*", "feature/*"]')
        toml = toml.replace('production = ["main", "feature/*"]', 'production = ["main"]')
        self.fx.write("agentic.toml", toml)
        self.fx.commit("policy change: feature branches become internal")
        rep = gate.run(self.fx.root, "ci", "main", None)
        g6 = self.fx.result(rep, "G6")
        self.assertEqual(g6["status"], "pass", g6["evidence"])

    def test_a_prototype_branch_still_may_not_edit_the_runner(self):
        self.fx.branch("proto/sneaky")
        self.fx.write(".agentic/gate.py", "# edited from a spike\nX = 1\n")
        self.fx.commit("edit the runner from a spike")
        g6 = self.fx.result(gate.run(self.fx.root, "ci", "main", None), "G6")
        self.assertEqual(g6["status"], "fail", g6["evidence"])

    # --- a commented-out requirement declares nothing --------------------------------
    def test_a_commented_out_requirement_is_not_a_declaration(self):
        """`#egg=` was searched before the comment was stripped, so a commented-out line declared
        any already-installed package."""
        self.fx.branch("proto/egg")
        self.fx.write("requirements.txt", "# we took this out  #egg=packaging\n")
        self.fx.write("notes/a.py", "import packaging\n")
        g4 = self.fx.result(self.fx.run(), "G4")
        self.assertEqual(g4["status"], "fail", g4["evidence"])
        self.assertTrue(any("not declared" in e for e in g4["evidence"]), g4["evidence"])

    def test_a_real_vcs_requirement_still_declares_itself(self):
        """Whether the package is installed on this host is not the point and would make the test
        environment-dependent. The point is that the #egg= fragment was read as a declaration, so
        the only thing G4 has left to say about it is that it is not installed."""
        self.fx.branch("proto/vcs")
        self.fx.write("requirements.txt",
                      "git+https://example.invalid/x.git#egg=some_vcs_only_package\n")
        self.fx.write("notes/a.py", "import some_vcs_only_package\n")
        g4 = self.fx.result(self.fx.run(), "G4")
        self.assertFalse(any("not declared" in e for e in g4["evidence"]), g4["evidence"])
        self.assertTrue(any("not installed" in e for e in g4["evidence"]), g4["evidence"])

    # --- an exports map naming nothing that exists is not a loadable package ---------
    def test_g4_rejects_a_js_package_whose_exports_target_is_absent(self):
        self.fx.branch("proto/jsexports")
        self.fx.write("package.json", json.dumps({"dependencies": {"ghost-pkg": "0"}}))
        self.fx.write("node_modules/ghost-pkg/package.json",
                      json.dumps({"exports": "./does-not-exist.js"}))
        self.fx.write("notes/app.ts", "import x from 'ghost-pkg';\n")
        g4 = self.fx.result(self.fx.run(), "G4")
        self.assertEqual(g4["status"], "fail", g4["evidence"])

    def test_g4_accepts_a_js_package_whose_exports_target_is_present(self):
        self.fx.branch("proto/jsexports2")
        self.fx.write("package.json", json.dumps({"dependencies": {"real-pkg": "0"}}))
        self.fx.write("node_modules/real-pkg/package.json",
                      json.dumps({"exports": {".": {"import": "./lib/i.mjs", "require": "./lib/i.cjs"}}}))
        self.fx.write("node_modules/real-pkg/lib/i.mjs", "export default {};\n")
        self.fx.write("node_modules/real-pkg/lib/i.cjs", "module.exports = {};\n")
        self.fx.write("notes/app.ts", "import x from 'real-pkg';\n")
        g4 = self.fx.result(self.fx.run(), "G4")
        self.assertEqual(g4["status"], "pass", g4["evidence"])

    def test_g4_does_not_fail_a_js_package_whose_exports_map_is_a_pattern(self):
        """`"./*": "./src/*.js"` cannot be resolved without Node's own matcher, so the walker
        declines to judge it rather than calling a real package hollow."""
        self.fx.branch("proto/jsglob")
        self.fx.write("package.json", json.dumps({"dependencies": {"glob-pkg": "0"}}))
        self.fx.write("node_modules/glob-pkg/package.json",
                      json.dumps({"exports": {"./*": "./src/*.js"}}))
        self.fx.write("node_modules/glob-pkg/src/thing.js", "module.exports = {};\n")
        self.fx.write("notes/app.ts", "import x from 'glob-pkg';\n")
        g4 = self.fx.result(self.fx.run(), "G4")
        self.assertEqual(g4["status"], "pass", g4["evidence"])

    # --- the symlink guard is exercised at the path the runner actually writes -------
    def test_the_temporary_report_file_is_not_left_behind_when_the_rename_fails(self):
        full_production_setup(self.fx)
        real_replace = os.replace

        def boom(src, dst):
            if str(dst).endswith("last-report.json"):
                raise OSError("rename refused")
            return real_replace(src, dst)

        with mock.patch.object(os, "replace", boom), mock.patch.object(sys, "stdout", io.StringIO()):
            gate.main(["--root", str(self.fx.root), "--base", "main"])
        leftovers = list((self.fx.root / ".agentic").glob(".last-report.*.tmp"))
        self.assertEqual(leftovers, [], f"temporary report files left on disk: {leftovers}")


class Round7b(unittest.TestCase):
    """The second half of the seventh round: findings the re-run surfaced.

    Every test here failed against the runner immediately before its fix landed.
    """

    def setUp(self):
        self.fx = RepoFixture()

    def tearDown(self):
        self.fx.close()

    def _laundering_toml(self, statement: str) -> str:
        toml = (self.fx.root / "agentic.toml").read_text(encoding="utf-8")
        return toml.replace(
            'command = "python -c \\"import sys; sys.exit(0)\\""',
            f'command = "python -c \\"import pathlib; {statement}\\""')

    # --- the secret scan is taken before the branch's own command runs ---------------
    def test_g2_cannot_launder_a_committed_secret_before_the_scan(self):
        self.fx.branch("feature/SPEC-0007-thing")
        self.fx.write("specs/SPEC-0007-thing.md", GOOD_SPEC)
        self.fx.write("handoffs/HANDOFF-0007.md", GOOD_HANDOFF)
        token = "sk-" + "A" * 24        # concatenated so this file never trips the scan itself
        self.fx.write("src/thing.py", f'TOKEN = "{token}"\n')
        self.fx.write("tests/test_thing.py", "def test_f():\n    pass\n")
        self.fx.write("agentic.toml", self._laundering_toml(
            "pathlib.Path('src/thing.py').write_text('SAFE = True\\\\n')"))
        self.fx.commit("a candidate whose test command launders its own secret")
        g4 = self.fx.result(self.fx.run(), "G4")
        self.assertEqual(git(self.fx.root, "show", "HEAD:src/thing.py").strip(),
                         f'TOKEN = "{token}"')
        self.assertEqual(g4["status"], "fail", g4["evidence"])

    # --- G5 judges the handoff the change proposes ------------------------------------
    def test_g2_cannot_complete_a_handoff_g5_then_reads(self):
        self.fx.branch("feature/SPEC-0007-thing")
        self.fx.write("specs/SPEC-0007-thing.md", GOOD_SPEC)
        self.fx.write("handoffs/HANDOFF-0007.md", GOOD_HANDOFF.replace("Verified: gates pass",
                                                                       "Verified: TBD"))
        self.fx.write("src/thing.py", "import json\n")
        self.fx.write("tests/test_thing.py", "def test_f():\n    pass\n")
        self.fx.write("agentic.toml", self._laundering_toml(
            "p = pathlib.Path('handoffs/HANDOFF-0007.md'); "
            "p.write_text(p.read_text().replace('Verified: TBD', 'Verified: gates pass'))"))
        self.fx.commit("a candidate whose test command fills in its own handoff")
        g5 = self.fx.result(self.fx.run(), "G5")
        self.assertIn("Verified: TBD", git(self.fx.root, "show", "HEAD:handoffs/HANDOFF-0007.md"))
        self.assertEqual(g5["status"], "fail", g5["evidence"])

    def test_a_complete_handoff_still_passes_g5(self):
        full_production_setup(self.fx)
        g5 = self.fx.result(self.fx.run(), "G5")
        self.assertEqual(g5["status"], "pass", g5["evidence"])

    # --- in CI an untracked file is not part of what is proposed ----------------------
    def test_an_untracked_spec_and_handoff_do_not_satisfy_g0_and_g5_in_ci(self):
        """In CI the candidate is the proposed tree. A file that was never committed is not in
        it, and no reviewer will ever see it."""
        self.fx.branch("feature/SPEC-0007-thing")
        self.fx.write("src/thing.py", "import json\n")
        self.fx.write("tests/test_thing.py", "def test_f():\n    pass\n")
        self.fx.commit("source with no spec and no handoff")
        self.fx.write("specs/SPEC-0007-thing.md", GOOD_SPEC)        # deliberately NOT committed
        self.fx.write("handoffs/HANDOFF-0007.md", GOOD_HANDOFF)     # deliberately NOT committed
        rep = gate.run(self.fx.root, "ci", "main", None)
        self.assertEqual(self.fx.result(rep, "G0")["status"], "fail",
                         self.fx.result(rep, "G0")["evidence"])
        self.assertEqual(self.fx.result(rep, "G5")["status"], "fail",
                         self.fx.result(rep, "G5")["evidence"])

    def test_a_committed_spec_and_handoff_still_pass_in_ci(self):
        full_production_setup(self.fx)
        self.fx.commit("a complete production change")
        rep = gate.run(self.fx.root, "ci", "main", None)
        self.assertTrue(rep["ok"], json.dumps(rep["results"], indent=1))

    # --- an eval pass rate has to be a real number in range ---------------------------
    def _eval_setup(self, rate):
        full_production_setup(self.fx)
        self.fx.write("prompts/p.txt", "you are a helpful assistant\n")
        self.fx.write("evalrun.py", textwrap.dedent(f"""\
            import json, pathlib
            pathlib.Path('.agentic/evals').mkdir(parents=True, exist_ok=True)
            pathlib.Path('.agentic/evals/result.json').write_text(json.dumps({{
                "cases": 5, "overall_pass_rate": {rate}, "target": "real-thing",
                "dimensions": {{"task_success": {{"rubric": "r", "pass_rate": 1.0}},
                                "hallucination": {{"rubric": "r", "pass_rate": 1.0}}}}}}))
            """))
        toml = (self.fx.root / "agentic.toml").read_text(encoding="utf-8")
        self.fx.write("agentic.toml", toml.replace('result_file = "result.json"',
                                                   'result_file = ".agentic/evals/result.json"'))
        return self.fx.result(self.fx.run(), "G3")

    def test_g3_rejects_a_boolean_pass_rate(self):
        """bool is a subclass of int, so `true` walked past the numeric check and then past
        `overall < min_rate`, and was printed as 1.000."""
        g3 = self._eval_setup("True")
        self.assertEqual(g3["status"], "fail", g3["evidence"])

    def test_g3_rejects_a_non_finite_pass_rate(self):
        """NaN compares false against everything, including `< min_rate`."""
        g3 = self._eval_setup("float('nan')")
        self.assertEqual(g3["status"], "fail", g3["evidence"])

    def test_g3_rejects_a_pass_rate_above_one(self):
        g3 = self._eval_setup("1.5")
        self.assertEqual(g3["status"], "fail", g3["evidence"])

    def test_g3_accepts_a_real_pass_rate(self):
        g3 = self._eval_setup("0.95")
        self.assertEqual(g3["status"], "pass", g3["evidence"])


if __name__ == "__main__":
    unittest.main()
