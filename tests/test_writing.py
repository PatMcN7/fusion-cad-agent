"""Writing to the repository, and what it refuses.

Reading let an agent diagnose why a tool could not do the job. Writing is
what lets it fix that. The refusals here are the ones that keep a mistake
from taking away the agent's own ability to recover.
"""

import pathlib
import tempfile
import unittest

from fusion_cad import workspace
from fusion_cad.errors import ArgumentError


class WriteFence(unittest.TestCase):
    def test_writing_outside_the_repo_is_refused(self):
        for path in ("/tmp/evil.py", "~/Downloads/x.py", "/etc/hosts"):
            with self.subTest(path=path), self.assertRaises(ArgumentError):
                workspace.resolve_writable(path)

    def test_execution_runtime_is_not_writable_through_the_agent(self):
        with self.assertRaises(ArgumentError):
            workspace.resolve_writable("fusion_cad/execution.py")

    def test_the_addin_shell_is_never_writable(self):
        """A syntax error there kills the bridge.

        The agent would destroy the only channel it has for fixing itself,
        and only a Fusion restart would bring it back.
        """
        with self.assertRaises(ArgumentError) as caught:
            workspace.resolve_writable("FusionCadAgent.py")
        self.assertIn("bridge", str(caught.exception))

    def test_errors_module_is_never_writable(self):
        # It is deliberately never hot-reloaded; rewriting it rebinds the
        # exception classes and isinstance silently stops matching.
        with self.assertRaises(ArgumentError) as caught:
            workspace.resolve_writable("errors.py")
        self.assertIn("hot-reloaded", str(caught.exception))

    def test_relative_paths_resolve_against_the_repo(self):
        self.assertEqual(workspace.resolve_writable("fusion_cad/patterns.py"), workspace.REPO / "fusion_cad" / "patterns.py")

    def test_credentials_are_refused_even_in_the_repo(self):
        with self.assertRaises(ArgumentError):
            workspace.resolve_writable("studio/.env")


class SyntaxGate(unittest.TestCase):
    """The refusal that matters most."""

    def setUp(self):
        self.target = workspace.REPO / "tests" / "_scratch_probe.py"
        self.addCleanup(lambda: self.target.unlink(missing_ok=True))

    def test_python_that_does_not_parse_is_refused(self):
        # A module that will not compile breaks every following call --
        # including the ones needed to fix it. Better an error than a brick.
        with self.assertRaises(ArgumentError) as caught:
            workspace.write_text(str(self.target), "def broken(:\n  pass\n")
        self.assertIn("line", str(caught.exception))
        self.assertFalse(self.target.exists())

    def test_valid_python_is_written(self):
        result = workspace.write_text(str(self.target), "VALUE = 41 + 1\n")
        self.assertTrue(self.target.exists())
        self.assertTrue(result["created"])
        self.assertIsNone(result["backup"])

    def test_overwriting_keeps_the_previous_contents(self):
        workspace.write_text(str(self.target), "VALUE = 1\n")
        result = workspace.write_text(str(self.target), "VALUE = 2\n")
        self.assertFalse(result["created"])
        self.assertIsNotNone(result["backup"])
        self.assertEqual(pathlib.Path(result["backup"]).read_text(), "VALUE = 1\n")

    def test_non_python_skips_the_syntax_gate(self):
        note = workspace.REPO / "tests" / "_scratch_probe.txt"
        self.addCleanup(lambda: note.unlink(missing_ok=True))
        workspace.write_text(str(note), "not python at all: {[(")
        self.assertTrue(note.exists())


if __name__ == "__main__":
    unittest.main()
