import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOTWRAP_SRC = REPO_ROOT / "dotwrap.py"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


FAKE_GH = r"""#!/usr/bin/env python3
import os
import sys
from pathlib import Path

log_path = os.environ.get("DOTWRAP_GH_LOG")
if not log_path:
    print("DOTWRAP_GH_LOG not set", file=sys.stderr)
    sys.exit(2)

Path(log_path).parent.mkdir(parents=True, exist_ok=True)
with open(log_path, "a", encoding="utf-8") as f:
    f.write("\t".join(sys.argv[1:]) + "\n")

# Expected commands:
#   gh alias set <name> <command>
#   gh alias delete <name>
#   gh alias list

args = sys.argv[1:]

if args[:2] != ["alias", "set"] and args[:2] != ["alias", "delete"] and args[:2] != ["alias", "list"]:
    print("unexpected args: " + " ".join(args), file=sys.stderr)
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

# alias set
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

        _write_executable(self.bin_dir / "gh", FAKE_GH)

        # Minimal environment with our fake gh first on PATH.
        self.env = dict(os.environ)
        self.env["PATH"] = str(self.bin_dir) + os.pathsep + self.env.get("PATH", "")
        self.env["DOTWRAP_GH_LOG"] = str(self.log_file)

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
'''
        )

        proc = self._run("install", "gh")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        calls = self._logged_calls()
        # Exact, deterministic sequence (sorted keys: dw_a, dw_b)
        self.assertEqual(
            calls,
            [
                ["alias", "set", "dw_a", "cmd subcmd --flag --two value"],
                ["alias", "set", "dw_b", "pr view --web"],
            ],
        )


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
