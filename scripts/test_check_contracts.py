"""Tests for check_contracts.py: run with `python3 -m unittest discover -s scripts`."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_contracts as cc  # noqa: E402

REAL = cc.CONTRACTS.read_text(encoding="utf-8")


def line_of(text: str, needle: str) -> int:
    return text[: text.index(needle)].count("\n") + 1


class CheckContractsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def check_text(self, text: str, newline: str = "\n") -> str:
        path = Path(self.tmp.name) / "contracts.md"
        path.write_bytes(text.replace("\n", newline).encode("utf-8"))
        return cc.check(path)

    def mutate(self, old: str, new: str, text: str = REAL) -> str:
        self.assertIn(old, text)
        return text.replace(old, new, 1)

    def assertFails(self, text: str, *fragments: str):
        with self.assertRaises(cc.CheckError) as ctx:
            self.check_text(text)
        for fragment in fragments:
            self.assertIn(fragment, str(ctx.exception))

    def test_real_contracts_pass(self):
        self.assertTrue(cc.check().startswith("ok: "))

    def test_crlf_case_and_spacing_variants_are_accepted(self):
        text = self.mutate("~~~python\nclass AnswerAction", "~~~ Python  \nclass AnswerAction")
        self.assertEqual(self.check_text(text, newline="\r\n"), self.check_text(REAL))

    def test_backtick_fence_is_accepted(self):
        text = self.mutate("~~~python\nclass AnswerAction", "```python\nclass AnswerAction")
        text = self.mutate("    fraction_denominator: int | None = None\n~~~",
                           "    fraction_denominator: int | None = None\n```", text)
        self.assertEqual(self.check_text(text), self.check_text(REAL))

    def test_near_miss_language_fails(self):
        text = self.mutate("~~~python\nclass AnswerAction", "~~~pyhton\nclass AnswerAction")
        self.assertFails(text, f":{line_of(text, '~~~pyhton')}:", "unrecognised fence language")

    def test_indented_python_fence_fails(self):
        text = self.mutate("~~~python\nclass AnswerAction", "  ~~~python\nclass AnswerAction")
        self.assertFails(text, "indented python fence")

    def test_unterminated_fence_fails(self):
        self.assertFails(REAL + "\n~~~python\nX = 1\n", "unterminated ~~~ fence")

    def test_python_fence_swallowed_by_unbalanced_fence_fails(self):
        text = self.mutate("## Answers and rendering\n", "## Answers and rendering\n\n```text\n")
        text = self.mutate("## Domain tools\n", "```\n\n## Domain tools\n", text)
        self.assertFails(text, "python fence inside the ``` fence")

    def test_duplicate_top_level_name_fails_with_both_lines(self):
        text = self.mutate("class PeerCandidate(BaseModel):",
                           "class ProductTurn(BaseModel):\n    x: int\n\nclass PeerCandidate(BaseModel):")
        first = line_of(text, "class ProductTurn(BaseModel):")
        self.assertFails(text, "ProductTurn is already defined at line " + str(first))

    @unittest.skipUnless(cc.TYPE_ALIAS, "needs Python 3.12+")
    def test_duplicate_type_alias_fails(self):
        text = self.mutate("ExecutionId = str\n", "ExecutionId = str\ntype ExecutionId = int\n")
        self.assertFails(text, "ExecutionId is already defined")

    def test_syntax_error_reports_doc_line(self):
        text = self.mutate("class RichDocument(BaseModel):", "class RichDocument(BaseModel)")
        self.assertFails(text, f":{line_of(text, 'class RichDocument(BaseModel)')}: SyntaxError")

    def test_runtime_error_reports_doc_line(self):
        text = self.mutate("ExecutionId = str\n", "ExecutionId = undefined_name\n")
        self.assertFails(text, f":{line_of(text, 'ExecutionId = undefined_name')}:", "NameError")

    def test_missing_required_contract_fails(self):
        text = REAL.replace("RuntimeTaskBinding", "TaskBinding")
        self.assertFails(text, "required contracts missing: RuntimeTaskBinding")

    def test_discriminator_collision_fails(self):
        text = self.mutate('kind: Literal["web"] = "web"', 'kind: Literal["api"] = "api"')
        self.assertFails(text, "mapped to multiple choices")

    def test_unresolvable_annotation_fails_on_model(self):
        text = self.mutate("    reasons: list[str]", '    reasons: list["Nope"]')
        self.assertFails(text, "model PeerCandidate")


if __name__ == "__main__":
    unittest.main()
