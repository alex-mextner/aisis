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

    def test_near_miss_fence_swallowed_by_unbalanced_fence_fails(self):
        text = self.mutate("## Answers and rendering\n\n~~~python\n",
                           "## Answers and rendering\n\n```text\n~~~pyhton\n")
        self.assertFails(text, f":{line_of(text, '~~~pyhton')}: python fence inside the ``` fence")

    def test_python_fence_swallowed_by_unbalanced_tilde_fence_fails(self):
        text = self.mutate("## Answers and rendering\n\n~~~python\n",
                           "## Answers and rendering\n\n~~~~text\n~~~python\n")
        self.assertFails(text, "python fence inside the ~~~~ fence")

    def test_py_and_python3_languages_are_accepted(self):
        text = self.mutate("~~~python\nclass AnswerAction", "~~~py\nclass AnswerAction")
        text = self.mutate("~~~python\nRisk = ", "~~~python3\nRisk = ", text)
        self.assertEqual(self.check_text(text), self.check_text(REAL))

    def test_doc_without_python_blocks_fails(self):
        self.assertFails("# Contracts\n\n~~~text\nnothing\n~~~\n", "no python contract blocks found")

    def test_python_fence_in_blockquote_or_list_fails(self):
        for opener in ("> ~~~python", ">~~~python", ">> ~~~python", "- ~~~Python", "* ```python",
                       "1. ~~~python", "2) ~~~py"):
            with self.subTest(opener=opener):
                text = self.mutate("~~~python\nclass AnswerAction", f"{opener}\nclass AnswerAction")
                self.assertFails(text, "python fence inside a blockquote or list")

    def test_indented_fence_line_inside_block_does_not_close_it(self):
        text = self.mutate("ExecutionId = str\n", 'ExecutionId = str\nFENCE_EXAMPLE = """\n    ~~~\n"""\n')
        self.assertEqual(self.check_text(text), self.check_text(REAL))

    def test_required_union_that_is_not_discriminated_fails(self):
        text = self.mutate('    Field(discriminator="kind"),\n]\n\nclass ProductTurn',
                           '    Field(description="any"),\n]\n\nclass ProductTurn')
        self.assertFails(text, "not discriminated unions", "SurfaceContext")

    def test_contracts_module_is_not_left_in_sys_modules(self):
        cc.check()
        self.assertNotIn("aisis_contracts", sys.modules)

    def test_discriminator_collision_fails(self):
        text = self.mutate('kind: Literal["web"] = "web"', 'kind: Literal["api"] = "api"')
        # SurfaceContext is built while its block runs (ProductTurn uses it there).
        self.assertFails(text, f"block starting at line {line_of(text, 'class AliceContext')}")

    def test_required_model_that_is_not_a_model_fails(self):
        text = self.mutate("class ProductAnswer(BaseModel):", "class ProductAnswer(Protocol):")
        self.assertFails(text, "required contracts are not pydantic models: ProductAnswer")

    def test_unresolvable_annotation_fails_on_model(self):
        text = self.mutate("    reasons: list[str]", '    reasons: list["Nope"]')
        self.assertFails(text, "model PeerCandidate")


if __name__ == "__main__":
    unittest.main()
