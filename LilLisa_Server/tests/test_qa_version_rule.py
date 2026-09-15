"""
Unit tests for QA prompt version-rule numbering (pr42-mp.1.2).

Run from LilLisa_Server:
    PYTHONPATH=. python3 tests/test_qa_version_rule.py
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils import (  # noqa: E402
    NO_ANSWER_MARKER,
    QA_VERSION_RULE_NUMBER,
    append_product_version_rule,
)


class QaVersionRuleTests(unittest.TestCase):
    def setUp(self):
        self.prompt = (PROJECT_ROOT / "data" / "prompts" / "qa_system_prompt.txt").read_text(
            encoding="utf-8"
        )

    def test_static_prompt_exempts_no_answer_from_rule_7(self):
        self.assertIn(NO_ANSWER_MARKER, self.prompt)
        self.assertIn("exception to rule 7", self.prompt)
        self.assertIn("exception is the exact leading marker", self.prompt)
        self.assertIn("\n10. Tables", self.prompt)

    def test_matched_versions_appends_rule_11_not_a_second_10(self):
        assembled = append_product_version_rule(self.prompt, ["8.1", "8.2"])
        self.assertIn(f"\n{QA_VERSION_RULE_NUMBER}. Mention the product version(s)", assembled)
        self.assertEqual(assembled.count("\n10."), 1)
        self.assertIn("8.1", assembled)
        self.assertIn("8.2", assembled)

    def test_no_version_appends_rule_11_and_keeps_marker_first(self):
        assembled = append_product_version_rule(self.prompt, None)
        self.assertIn(f"\n{QA_VERSION_RULE_NUMBER}. Mention that because a specific product version", assembled)
        self.assertIn("must still come first", assembled)
        self.assertEqual(assembled.count("\n10."), 1)


if __name__ == "__main__":
    unittest.main()
