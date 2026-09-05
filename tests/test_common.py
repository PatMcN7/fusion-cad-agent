import os
import stat
import tempfile
import unittest
from pathlib import Path

from common import ProtocolError, load_or_create_token, validate_request

REGISTRY = {
    "health": {"mutating": False},
    "create_test_box": {"mutating": True},
}


class ProtocolTests(unittest.TestCase):
    def test_defaults_arguments(self):
        self.assertEqual(validate_request({"tool": "health"}, REGISTRY)["arguments"], {})

    def test_rejects_unknown_tool(self):
        with self.assertRaises(ProtocolError):
            validate_request({"tool": "delete_everything"}, REGISTRY)

    def test_rejects_non_object_arguments(self):
        with self.assertRaises(ProtocolError):
            validate_request({"tool": "health", "arguments": []}, REGISTRY)

    def test_mutating_tool_requires_confirmation(self):
        with self.assertRaises(ProtocolError):
            validate_request({"tool": "create_test_box"}, REGISTRY)

    def test_mutating_tool_accepts_explicit_confirmation(self):
        self.assertTrue(
            validate_request({"tool": "create_test_box", "confirm": True}, REGISTRY)["confirm"]
        )

    def test_rejects_non_boolean_confirmation(self):
        with self.assertRaises(ProtocolError):
            validate_request({"tool": "health", "confirm": "yes"}, REGISTRY)

    def test_token_is_stable_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first = load_or_create_token(base)
            self.assertEqual(first, load_or_create_token(base))
            self.assertGreaterEqual(len(first), 32)
            if os.name != "nt":
                mode = stat.S_IMODE((base / "token").stat().st_mode)
                self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
