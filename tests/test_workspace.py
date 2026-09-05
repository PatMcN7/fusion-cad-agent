"""The fence.

These tests are the reason the file tools are safe to have. Each one stands
for a way this could leak: a path that escapes by spelling, a symlink that
escapes by pointing, a credentials file sitting inside an allowed directory,
and a URL that reaches back into this machine.
"""

import os
import pathlib
import tempfile
import unittest

import workspace
from errors import ArgumentError


class Fence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name).resolve()
        (self.root / "part.step").write_text("ISO-10303")
        (self.root / "notes.txt").write_text("3 inch cells")
        os.environ["STUDIO_FILE_ROOTS"] = str(self.root)
        self.addCleanup(os.environ.pop, "STUDIO_FILE_ROOTS", None)

    def test_a_file_inside_the_root_resolves(self):
        self.assertTrue(str(workspace.resolve(str(self.root / "part.step"))))

    def test_traversal_out_of_the_root_is_refused(self):
        with self.assertRaises(ArgumentError):
            workspace.resolve(str(self.root / ".." / ".." / "etc" / "passwd"))

    def test_absolute_path_outside_the_root_is_refused(self):
        with self.assertRaises(ArgumentError):
            workspace.resolve("/etc/hosts")

    def test_a_symlink_pointing_out_is_refused(self):
        """Resolution must happen before the containment check.

        Checking the spelling first and resolving later is the classic way a
        fence like this is defeated.
        """
        link = self.root / "escape"
        link.symlink_to("/etc")
        with self.assertRaises(ArgumentError):
            workspace.resolve(str(link / "hosts"))

    def test_home_expansion_still_has_to_land_inside(self):
        with self.assertRaises(ArgumentError):
            workspace.resolve("~/.ssh/id_rsa")


class Secrets(unittest.TestCase):
    """Being inside the fence is not enough."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name).resolve()
        os.environ["STUDIO_FILE_ROOTS"] = str(self.root)
        self.addCleanup(os.environ.pop, "STUDIO_FILE_ROOTS", None)

    def test_dotenv_inside_an_allowed_root_is_refused(self):
        # studio/.env holds live API keys and lives in this repo.
        secret = self.root / ".env"
        secret.write_text("OPENROUTER_API_KEY=sk-or-v1-real")
        with self.assertRaises(ArgumentError) as caught:
            workspace.read_text(str(secret))
        self.assertIn("credentials", str(caught.exception))

    def test_secret_shaped_names_are_refused(self):
        for name in ("id_rsa", "server.pem", "api.key", ".env.local",
                     "auth.json", "my_token.txt", "aws_credentials"):
            with self.subTest(name=name):
                (self.root / name).write_text("x")
                self.assertTrue(workspace.is_secret(self.root / name), name)

    def test_ordinary_files_are_not_mistaken_for_secrets(self):
        for name in ("part.step", "notes.txt", "patterns.py", "drawing.pdf"):
            with self.subTest(name=name):
                self.assertFalse(workspace.is_secret(self.root / name), name)

    def test_secrets_are_not_even_listed(self):
        # Listing them would tell the model they exist and invite a request.
        (self.root / ".env").write_text("KEY=1")
        (self.root / "part.step").write_text("x")
        names = [e["name"] for e in workspace.list_dir(str(self.root))["entries"]]
        self.assertIn("part.step", names)
        self.assertNotIn(".env", names)


class Listing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name).resolve()
        os.environ["STUDIO_FILE_ROOTS"] = str(self.root)
        self.addCleanup(os.environ.pop, "STUDIO_FILE_ROOTS", None)

    def test_importable_files_are_flagged(self):
        for name in ("module.step", "frame.f3d", "gasket.dxf", "readme.txt"):
            (self.root / name).write_text("x")
        by_name = {e["name"]: e for e in workspace.list_dir(str(self.root))["entries"]}
        self.assertTrue(by_name["module.step"]["importable"])
        self.assertTrue(by_name["frame.f3d"]["importable"])
        self.assertTrue(by_name["gasket.dxf"]["importable"])
        self.assertFalse(by_name["readme.txt"]["importable"])

    def test_noise_directories_are_skipped(self):
        (self.root / "__pycache__").mkdir()
        (self.root / "keep").mkdir()
        names = [e["name"] for e in workspace.list_dir(str(self.root))["entries"]]
        self.assertIn("keep/", names)
        self.assertNotIn("__pycache__/", names)

    def test_binary_files_say_what_to_do_instead(self):
        blob = self.root / "drawing.pdf"
        blob.write_bytes(b"%PDF-1.4\x00\x80\xff binary")
        with self.assertRaises(ArgumentError) as caught:
            workspace.read_text(str(blob))
        self.assertIn("pdf_to_images", str(caught.exception))


class Network(unittest.TestCase):
    """Fetching is for vendor drawings, not for reaching back inside."""

    def test_loopback_is_refused(self):
        for url in ("http://127.0.0.1:8765/v1/tools", "http://localhost:8765/"):
            with self.subTest(url=url), self.assertRaises(ArgumentError):
                workspace.fetch(url)

    def test_cloud_metadata_is_refused(self):
        # 169.254.169.254 is where credentials live on a cloud host.
        with self.assertRaises(ArgumentError):
            workspace.fetch("http://169.254.169.254/latest/meta-data/")

    def test_private_ranges_are_refused(self):
        with self.assertRaises(ArgumentError):
            workspace.fetch("http://192.168.1.1/")

    def test_non_http_schemes_are_refused(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://x/"):
            with self.subTest(url=url), self.assertRaises(ArgumentError):
                workspace.fetch(url)


if __name__ == "__main__":
    unittest.main()
