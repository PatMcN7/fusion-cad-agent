"""The registry's own invariants.

Five tools were promoted to first class after an agent wrote them mid-session
and they shipped with no coverage at all. Their bodies need live Fusion, but
their *contract* does not, and the contract is where the cheap mistakes live:
a tool with no handler, an argument the model is never told about, a mutating
tool that forgets to report the design state.

adsk is stubbed rather than mocked. Nothing here calls into Fusion -- the
point is to import the registry and inspect it.
"""

import sys
import types
import unittest


def _install_adsk_stub() -> None:
    """A module that answers any attribute with another such module.

    tools.py imports adsk at module scope but only touches it inside function
    bodies, so an inert stand-in is enough to make the registry importable.
    """
    if "adsk" in sys.modules:
        return

    class Stub(types.ModuleType):
        def __getattr__(self, name):
            child = Stub(f"{self.__name__}.{name}")
            setattr(self, name, child)
            return child

    root = Stub("adsk")
    for child in ("core", "fusion"):
        module = Stub(f"adsk.{child}")
        setattr(root, child, module)
        sys.modules[f"adsk.{child}"] = module
    sys.modules["adsk"] = root


_install_adsk_stub()

import tools  # noqa: E402


class RegistryContract(unittest.TestCase):
    def test_every_tool_has_a_handler(self):
        missing = sorted(set(tools.TOOLS) - set(tools.HANDLERS))
        self.assertEqual(missing, [], f"declared but unimplemented: {missing}")

    def test_every_handler_is_declared(self):
        # An undeclared handler is unreachable: validate_request checks TOOLS.
        extra = sorted(set(tools.HANDLERS) - set(tools.TOOLS))
        self.assertEqual(extra, [], f"implemented but undeclared: {extra}")

    def test_every_tool_describes_itself(self):
        for name, spec in tools.TOOLS.items():
            with self.subTest(tool=name):
                self.assertTrue(spec.get("description", "").strip(),
                                f"{name} has no description")
                self.assertIn("mutating", spec, f"{name} declares no mutating flag")
                self.assertIsInstance(spec.get("arguments"), dict)

    def test_every_argument_is_documented(self):
        # The schema handed to the model is generated from these strings; an
        # empty one ships a parameter with no description at all.
        for name, spec in tools.TOOLS.items():
            for argument, text in (spec.get("arguments") or {}).items():
                with self.subTest(tool=name, argument=argument):
                    if isinstance(text, dict):
                        self.assertTrue(text.get("description") or text.get("type"))
                    else:
                        self.assertTrue(str(text).strip())

    def test_mutating_tools_report_design_state(self):
        """The state echo must reach every mutating tool, including new ones.

        It is applied by iterating the registry, so a tool added later is
        wrapped automatically -- this asserts that stays true rather than
        trusting it.
        """
        for name, spec in tools.TOOLS.items():
            if not spec.get("mutating"):
                continue
            with self.subTest(tool=name):
                handler = tools.HANDLERS[name]
                self.assertEqual(
                    getattr(handler, "__wrapped_for_state_echo__", None)
                    or handler.__qualname__.startswith("_echo_state"),
                    True,
                    f"{name} is mutating but does not echo design state")

    def test_read_only_tools_are_not_wrapped(self):
        # Wrapping a read-only tool would make every query pay for a scan of
        # the design.
        for name, spec in tools.TOOLS.items():
            if spec.get("mutating"):
                continue
            with self.subTest(tool=name):
                self.assertFalse(
                    tools.HANDLERS[name].__qualname__.startswith("_echo_state"),
                    f"{name} is read-only but echoes design state")


class PromotedTools(unittest.TestCase):
    """The five an agent wrote mid-session, now first class."""

    PROMOTED = ("save_document", "delete_named_features", "set_6061_material",
                "router_fillet_pocket_corners", "import_step_component")

    def test_all_registered(self):
        for name in self.PROMOTED:
            self.assertIn(name, tools.TOOLS, f"{name} is not registered")
            self.assertIn(name, tools.HANDLERS)

    def test_all_declared_mutating(self):
        # Each of these writes to the document; a false flag here skips the
        # checkpoint and the capture.
        for name in self.PROMOTED:
            self.assertTrue(tools.TOOLS[name]["mutating"], f"{name} claims read-only")

    def test_destructive_tools_require_explicit_targets(self):
        """delete_named_features must never accept a wildcard.

        It was written to remove named features; a version that defaulted to
        "everything" would be one bad argument away from clearing a design.
        """
        arguments = tools.TOOLS["delete_named_features"]["arguments"]
        self.assertIn("feature_names", arguments)
        self.assertIn("Non-empty", str(arguments["feature_names"]))


if __name__ == "__main__":
    unittest.main()


class Documents(unittest.TestCase):
    """Switching between open documents.

    Fusion will happily keep sixteen documents all named "Untitled", so the
    name is not an address. That is the whole reason `index` exists.
    """

    def test_both_tools_registered_with_the_right_mutability(self):
        self.assertFalse(tools.TOOLS["list_documents"]["mutating"])
        # Switching changes which document every later tool acts on, so it
        # gets the state echo like any other mutation.
        self.assertTrue(tools.TOOLS["switch_document"]["mutating"])
        self.assertTrue(
            tools.HANDLERS["switch_document"].__qualname__.startswith("_echo_state"))

    def test_switch_offers_an_index_because_names_repeat(self):
        arguments = tools.TOOLS["switch_document"]["arguments"]
        self.assertIn("index", arguments)
        self.assertIn("share a name", arguments["index"])

    def test_list_documents_takes_no_arguments(self):
        self.assertEqual(tools.TOOLS["list_documents"]["arguments"], {})
