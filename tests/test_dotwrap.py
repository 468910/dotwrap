import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]
DOTWRAP_SRC = REPO_ROOT / "dotwrap.py"
ALIASES_SRC = REPO_ROOT / "aliases.toml"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


FAKE_GH = r"""#!/usr/bin/env python3
import os
import shlex
import subprocess
import sys
from pathlib import Path

log_path = os.environ.get("DOTWRAP_GH_LOG")
if not log_path:
    print("DOTWRAP_GH_LOG not set", file=sys.stderr)
    sys.exit(2)

Path(log_path).parent.mkdir(parents=True, exist_ok=True)
with open(log_path, "a", encoding="utf-8") as f:
    f.write("\t".join(sys.argv[1:]) + "\n")

alias_file = os.environ.get("DOTWRAP_GH_ALIAS_FILE")


def _load_aliases() -> dict[str, str]:
    if not alias_file:
        return {}
    p = Path(alias_file)
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        name, cmd = line.split("\t", 1)
        out[name] = cmd
    return out


def _save_aliases(aliases: dict[str, str]) -> None:
    if not alias_file:
        return
    p = Path(alias_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}\t{v}" for k, v in sorted(aliases.items())]
    p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

# Expected commands:
#   gh alias set <name> <command>
#   gh alias delete <name>
#   gh alias list

args = sys.argv[1:]

if not args:
    print("unexpected args", file=sys.stderr)
    sys.exit(2)

if args[:2] == ["alias", "list"]:
    # Mixed output to test dotwrap filtering.
    sys.stdout.write("dw_demo: pr list\n")
    sys.stdout.write("other: something\n")
    sys.stdout.write("  dw_indented: pr view\n")
    sys.exit(0)

if args[:2] == ["alias", "delete"]:
    # Default: success (0). Tests can simulate a non-zero delete.
    rc = int(os.environ.get("DOTWRAP_GH_DELETE_RC", "0"))
    if rc != 0:
        # Typical-ish message; dotwrap treats common "missing" phrases as ignorable.
        sys.stderr.write("no such alias\n")
    sys.exit(rc)

if args[:2] == ["alias", "set"]:
    # Supports: gh alias set [--clobber] <name> <command>
    i = 2
    if len(args) > 2 and args[2] == "--clobber":
        i += 1
    if len(args) < i + 2:
        print("invalid alias set args", file=sys.stderr)
        sys.exit(2)

    name = args[i]
    cmd = args[i + 1]
    aliases = _load_aliases()
    aliases[name] = cmd
    _save_aliases(aliases)
    sys.exit(0)


# Minimal gh pr subcommands used by dw_prf.
if args[:2] == ["pr", "list"]:
    # Output a tab-delimited list like the real template would.
    sys.stdout.write("17\talice\tfeat -> main\t2026-02-15T00:00:00Z\tA title\n")
    sys.stdout.write("42\tbob\tbugfix -> main\t2026-02-14T00:00:00Z\tAnother\n")
    sys.exit(0)

if args[:2] == ["pr", "view"]:
    # Support preview and --web open.
    sys.exit(0)

if args[:2] == ["pr", "checkout"]:
    sys.exit(0)


# Alias invocation: gh <aliasName> [...]
alias_name = args[0]
aliases = _load_aliases()
expansion = aliases.get(alias_name)
if not expansion:
    print("unexpected args: " + " ".join(args), file=sys.stderr)
    sys.exit(2)

if expansion.startswith("!"):
    # Shell alias: run via sh. (Good enough for our tests.)
    proc = subprocess.run(expansion[1:], shell=True)
    sys.exit(proc.returncode)

argv = shlex.split(expansion)
proc = subprocess.run(argv + args[1:], text=True)
sys.exit(proc.returncode)
"""


FAKE_FZF = r"""#!/usr/bin/env python3
import os
import sys

# Read stdin to behave like a real pipe consumer.
_ = sys.stdin.read()

out = os.environ.get("DOTWRAP_FZF_OUTPUT", "")
if out:
    sys.stdout.write(out)

sys.exit(0)
"""


class DotwrapCLITestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

        # Working directory that contains a copy of dotwrap.py and an aliases.toml.
        self.workdir = self.tmpdir / "work"
        self.workdir.mkdir(parents=True, exist_ok=True)

        shutil.copy2(DOTWRAP_SRC, self.workdir / "dotwrap.py")

        self.bin_dir = self.tmpdir / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)

        self.log_file = self.tmpdir / "gh_calls.log"
        self.alias_file = self.tmpdir / "gh_aliases.tsv"

        _write_executable(self.bin_dir / "gh", FAKE_GH)
        _write_executable(self.bin_dir / "fzf", FAKE_FZF)

        # Minimal environment with our fake gh first on PATH.
        self.env = dict(os.environ)
        self.env["PATH"] = str(self.bin_dir) + os.pathsep + self.env.get("PATH", "")
        self.env["DOTWRAP_GH_LOG"] = str(self.log_file)
        self.env["DOTWRAP_GH_ALIAS_FILE"] = str(self.alias_file)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_aliases(self, content: str) -> None:
        (self.workdir / "aliases.toml").write_text(content, encoding="utf-8")

    def _run(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                os.fspath(Path(sys.executable)),
                os.fspath(self.workdir / "dotwrap.py"),
                *args,
            ],
            cwd=self.workdir,
            env=env if env is not None else self.env,
            text=True,
            capture_output=True,
        )

    def _logged_calls(self) -> list[list[str]]:
        if not self.log_file.exists():
            return []
        lines = self.log_file.read_text(encoding="utf-8").splitlines()
        return [ln.split("\t") for ln in lines if ln.strip()]


class TestInstall(DotwrapCLITestCase):
    def test_install_sets_each_alias_and_collapses_whitespace(self) -> None:
        self._write_aliases(
            '''[providers.gh.aliases]

dw_b = "pr view --web"

dw_a = """
cmd subcmd   --flag
  --two   value
"""

dw_prf = "!sh -c echo hi"
'''
        )

        proc = self._run("install", "gh")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        calls = self._logged_calls()
        # Exact, deterministic sequence (sorted keys: dw_a, dw_b, dw_prf)
        self.assertEqual(
            calls,
            [
                ["alias", "set", "--clobber", "dw_a", "cmd subcmd --flag --two value"],
                ["alias", "set", "--clobber", "dw_b", "pr view --web"],
                ["alias", "set", "--clobber", "dw_prf", "!sh -c echo hi"],
            ],
        )

    def test_install_sets_dw_prf_with_clobber(self) -> None:
        # Keep this focused: just asserts dw_prf is set with --clobber.
        self._write_aliases(
            """[providers.gh.aliases]

    dw_prf = "!sh -c echo hi"
"""
        )

        proc = self._run("install", "gh")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        calls = self._logged_calls()
        self.assertEqual(calls, [["alias", "set", "--clobber", "dw_prf", "!sh -c echo hi"]])


class TestUninstall(DotwrapCLITestCase):
    def test_uninstall_deletes_each_alias_and_ignores_missing(self) -> None:
        self._write_aliases(
            """[providers.gh.aliases]

dw_one = "pr list"
dw_two = "pr view --web"
"""
        )

        # Simulate delete returning non-zero (e.g. alias not present).
        env = dict(self.env)
        env["DOTWRAP_GH_DELETE_RC"] = "1"

        proc = self._run("uninstall", "gh", env=env)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        calls = self._logged_calls()
        self.assertEqual(
            calls,
            [
                ["alias", "delete", "dw_one"],
                ["alias", "delete", "dw_two"],
            ],
        )


class TestDoctor(DotwrapCLITestCase):
    def test_doctor_calls_alias_list(self) -> None:
        proc = self._run("doctor", "gh")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        calls = self._logged_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ["alias", "list"])

        # Output contract: all user-facing lines start with "dotwrap:".
        for line in proc.stdout.splitlines():
            if line.strip():
                self.assertTrue(line.startswith("dotwrap:"), msg=line)

        # Filters: includes dw_ aliases, excludes others.
        self.assertIn("dw_demo", proc.stdout)
        self.assertIn("dw_indented", proc.stdout)
        self.assertNotIn("other:", proc.stdout)


class TestMissingGh(unittest.TestCase):
    def test_missing_gh_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            workdir = tmpdir / "work"
            workdir.mkdir(parents=True, exist_ok=True)

            shutil.copy2(DOTWRAP_SRC, workdir / "dotwrap.py")
            (workdir / "aliases.toml").write_text(
                "[providers.gh.aliases]\ndw_one='pr list'\n",
                encoding="utf-8",
            )

            # PATH set to an empty dir with no gh.
            env = dict(os.environ)
            env["PATH"] = str(tmpdir / "empty")

            proc = subprocess.run(
                [os.fspath(Path(sys.executable)), os.fspath(workdir / "dotwrap.py"), "install", "gh"],
                cwd=workdir,
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertTrue((proc.stderr or proc.stdout).startswith("dotwrap:"))
            self.assertIn("gh", (proc.stderr or proc.stdout).lower())


class TestMissingAliasesToml(unittest.TestCase):
    def test_missing_aliases_toml_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            workdir = tmpdir / "work"
            workdir.mkdir(parents=True, exist_ok=True)

            shutil.copy2(DOTWRAP_SRC, workdir / "dotwrap.py")

            env = dict(os.environ)
            env["PATH"] = env.get("PATH", "")

            proc = subprocess.run(
                [os.fspath(Path(sys.executable)), os.fspath(workdir / "dotwrap.py"), "install", "gh"],
                cwd=workdir,
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertTrue((proc.stderr or proc.stdout).startswith("dotwrap:"))
            self.assertIn("aliases.toml", (proc.stderr or proc.stdout).lower())


class TestInvalidAliasKey(DotwrapCLITestCase):
    def test_invalid_alias_key_exits_1(self) -> None:
        self._write_aliases(
            """[providers.gh.aliases]

dw_ok = "pr list"
bad = "pr view"
"""
        )

        proc = self._run("install", "gh")
        self.assertEqual(proc.returncode, 1)
        self.assertTrue((proc.stderr or proc.stdout).startswith("dotwrap:"))
        self.assertIn("invalid alias", (proc.stderr or proc.stdout).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestPrfIntegration(DotwrapCLITestCase):
    def _write_dw_prf_only_from_repo(self) -> None:
        with ALIASES_SRC.open("rb") as f:
            data = tomllib.load(f)
        prf = data["providers"]["gh"]["aliases"]["dw_prf"]
        if "'''" in prf:
            raise AssertionError("dw_prf contains triple single quotes; test writer needs adjustment")
        self._write_aliases("[providers.gh.aliases]\n\ndw_prf = '''" + prf + "'''\n")

    def test_dw_prf_select_runs_list_then_web(self) -> None:
        # Install dw_prf into fake gh.
        self._write_dw_prf_only_from_repo()
        proc = self._run("install", "gh")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        # Clear log so we only assert the alias run path.
        self.log_file.write_text("", encoding="utf-8")

        env = dict(self.env)
        # fzf may or may not emit an explicit key line for enter. Both should work.
        env["DOTWRAP_FZF_OUTPUT"] = (
            "17\talice\tfeat -> main\t2026-02-15T00:00:00Z\tA title\n"
        )

        run = subprocess.run(
            ["gh", "dw_prf"],
            cwd=self.workdir,
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(run.returncode, 0, msg=run.stderr)

        calls = self._logged_calls()
        # Must include: gh pr list ... then gh pr view 17 --web
        self.assertIn(["dw_prf"], calls)
        pr_list = next((c for c in calls if c[:2] == ["pr", "list"]), None)
        self.assertIsNotNone(pr_list, msg=calls)

        # Regression guard: template must be a single argv token.
        idx = pr_list.index("--template")
        tmpl = pr_list[idx + 1]
        self.assertIn("->", tmpl)
        self.assertIn("\\t", tmpl)
        self.assertTrue(any(c[:4] == ["pr", "view", "17", "--web"] for c in calls), msg=calls)

    def test_dw_prf_cancel_exits_0_no_web_or_checkout(self) -> None:
        self._write_dw_prf_only_from_repo()
        proc = self._run("install", "gh")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        self.log_file.write_text("", encoding="utf-8")

        env = dict(self.env)
        env["DOTWRAP_FZF_OUTPUT"] = ""  # cancel path: no selection

        run = subprocess.run(
            ["gh", "dw_prf"],
            cwd=self.workdir,
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(run.returncode, 0, msg=run.stderr)
        self.assertEqual(run.stdout.strip(), "")

        calls = self._logged_calls()
        self.assertIn(["dw_prf"], calls)
        self.assertTrue(any(c[:2] == ["pr", "list"] for c in calls), msg=calls)
        self.assertFalse(any(c[:2] == ["pr", "checkout"] for c in calls), msg=calls)
        self.assertFalse(any(c[:2] == ["pr", "view"] and "--web" in c for c in calls), msg=calls)
