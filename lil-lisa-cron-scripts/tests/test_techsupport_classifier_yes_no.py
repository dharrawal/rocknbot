"""
Unit tests for classifier yes/no normalization.

Run from lil-lisa-cron-scripts:
    PYTHONPATH=. python3 tests/test_techsupport_classifier_yes_no.py
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import techsupport_classifier as classifier  # noqa: E402

_MESSAGES = [{"ts": "1.0", "user": "U1", "text": "hello"}]


class IsYesAnswerTests(unittest.TestCase):
    def test_plain_yes(self):
        self.assertTrue(classifier.is_yes_answer("yes"))

    def test_capitalized_yes(self):
        self.assertTrue(classifier.is_yes_answer("Yes"))

    def test_uppercase_yes(self):
        self.assertTrue(classifier.is_yes_answer("YES"))

    def test_trailing_period(self):
        self.assertTrue(classifier.is_yes_answer("yes."))

    def test_whitespace(self):
        self.assertTrue(classifier.is_yes_answer("  yes  "))
        self.assertTrue(classifier.is_yes_answer("\tYes.\n"))

    def test_trailing_bang(self):
        self.assertTrue(classifier.is_yes_answer("yes!"))
        self.assertTrue(classifier.is_yes_answer("Yes.!"))

    def test_no_is_false(self):
        self.assertFalse(classifier.is_yes_answer("no"))
        self.assertFalse(classifier.is_yes_answer("No"))
        self.assertFalse(classifier.is_yes_answer("NO."))

    def test_unknown_stays_false(self):
        self.assertFalse(classifier.is_yes_answer("unknown"))
        self.assertFalse(classifier.is_yes_answer("maybe"))
        self.assertFalse(classifier.is_yes_answer("yes?"))
        self.assertFalse(classifier.is_yes_answer(""))
        self.assertFalse(classifier.is_yes_answer(None))


class ClassifyThreadYesNoTests(unittest.TestCase):
    def test_useful_accepts_normalized_yes(self):
        useful = MagicMock()
        useful.is_useful = "Yes"
        conclusive = MagicMock()
        conclusive.is_conclusive = "yes."
        with patch.object(classifier, "configure_dspy_lm"), patch.object(
            classifier, "check_useful", return_value=useful
        ), patch.object(classifier, "check_conclusive", return_value=conclusive):
            result = classifier.classify_thread(_MESSAGES)
        self.assertTrue(result["is_useful"])
        self.assertTrue(result["is_conclusive"])

    def test_unknown_useful_does_not_ingest(self):
        useful = MagicMock()
        useful.is_useful = "unknown"
        with patch.object(classifier, "configure_dspy_lm"), patch.object(
            classifier, "check_useful", return_value=useful
        ) as check_useful, patch.object(classifier, "check_conclusive") as check_conclusive:
            result = classifier.classify_thread(_MESSAGES)
        check_useful.assert_called_once()
        check_conclusive.assert_not_called()
        self.assertFalse(result["is_useful"])
        self.assertIsNone(result["is_conclusive"])

    def test_unknown_conclusive_is_false(self):
        useful = MagicMock()
        useful.is_useful = " YES "
        conclusive = MagicMock()
        conclusive.is_conclusive = "unclear"
        with patch.object(classifier, "configure_dspy_lm"), patch.object(
            classifier, "check_useful", return_value=useful
        ), patch.object(classifier, "check_conclusive", return_value=conclusive):
            result = classifier.classify_thread(_MESSAGES)
        self.assertTrue(result["is_useful"])
        self.assertFalse(result["is_conclusive"])


if __name__ == "__main__":
    unittest.main()
