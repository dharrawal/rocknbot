"""
Unit tests for the optional Jev follow-up classifier.

Run from LilLisa_Server:
    PYTHONPATH=. python3 tests/test_followup_classifier.py
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import utils  # noqa: E402
from src.followup_classifier import (  # noqa: E402
    FOLLOWUP_YES_THRESHOLD,
    JEV_MODEL,
    _jev_key_filepath,
    build_retrieval_args,
    classify_depends_on_prior,
    jev_api_key,
)

THREAD = [
    ("User", "How do certificates work?", "1"),
    ("Assistant", "Use the admin console.", "1"),
    ("User", "How do I reset the admin password?", "2"),
]
NEW_QUESTION = "How do I reset the admin password?"


class JevApiKeyTests(unittest.TestCase):
    def _without_filepath(self):
        saved = os.environ.pop("JEV_API_KEY_FILEPATH", None)
        return saved

    def test_no_filepath_is_absent(self):
        saved = self._without_filepath()
        try:
            with patch.dict(utils.LILLISA_SERVER_ENV_DICT):
                utils.LILLISA_SERVER_ENV_DICT.pop("JEV_API_KEY_FILEPATH", None)
                self.assertIsNone(jev_api_key())
        finally:
            if saved is not None:
                os.environ["JEV_API_KEY_FILEPATH"] = saved

    def test_reads_stripped_key_from_password_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "jev_api_key"
            path.write_text("  secret-key\n", encoding="utf-8")
            with patch.dict(os.environ, {"JEV_API_KEY_FILEPATH": str(path)}, clear=False):
                self.assertEqual(jev_api_key(), "secret-key")

    def test_env_filepath_overrides_configured_filepath(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "jev_api_key"
            path.write_text("from-env-file\n", encoding="utf-8")
            with patch.dict(os.environ, {"JEV_API_KEY_FILEPATH": str(path)}, clear=False), patch.dict(
                utils.LILLISA_SERVER_ENV_DICT, {"JEV_API_KEY_FILEPATH": "/does/not/exist"}
            ):
                self.assertEqual(jev_api_key(), "from-env-file")

    def test_blank_password_file_is_absent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "jev_api_key"
            path.write_text("  \n", encoding="utf-8")
            with patch.dict(os.environ, {"JEV_API_KEY_FILEPATH": str(path)}, clear=False):
                self.assertIsNone(jev_api_key())

    def test_missing_password_file_is_absent_and_does_not_log_a_key(self):
        missing = Path(tempfile.gettempdir()) / "jev-key-does-not-exist"
        secret = "not-a-real-key"
        with patch.dict(os.environ, {"JEV_API_KEY_FILEPATH": str(missing)}, clear=False):
            with self.assertLogs("RL_Logger", level="WARNING") as logs:
                self.assertIsNone(jev_api_key())
        rendered = "\n".join(logs.output)
        self.assertNotIn(secret, rendered)
        self.assertIn("jev_key_file_missing", rendered)

    def test_relative_filepath_resolves_under_server_root(self):
        saved = self._without_filepath()
        try:
            with patch.dict(utils.LILLISA_SERVER_ENV_DICT, {"JEV_API_KEY_FILEPATH": "./passwords/jev_api_key"}):
                path = _jev_key_filepath()
            expected = Path(__file__).resolve().parents[1] / "passwords" / "jev_api_key"
            self.assertEqual(path, expected)
        finally:
            if saved is not None:
                os.environ["JEV_API_KEY_FILEPATH"] = saved

    def test_quoted_filepath_is_unwrapped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "jev_api_key"
            path.write_text("secret-key\n", encoding="utf-8")
            with patch.dict(os.environ, {"JEV_API_KEY_FILEPATH": f'"{path}"'}, clear=False):
                self.assertEqual(jev_api_key(), "secret-key")


class RetrievalPlanTests(unittest.TestCase):
    def test_without_key_in_thread_question_still_rewrites_full_history(self):
        def classify(*_args):
            raise AssertionError("Jev must not be called when the key file is absent")

        def improve(query, history):
            self.assertEqual(query, NEW_QUESTION)
            self.assertIn("How do certificates work?", history)
            self.assertIn(NEW_QUESTION, history)
            return "rewritten"

        with patch("src.followup_classifier.jev_api_key", return_value=None):
            generated, history = build_retrieval_args(
                NEW_QUESTION,
                THREAD,
                may_rewrite=True,
                improve_query=improve,
                classify=classify,
            )
        self.assertEqual(generated, "rewritten")
        self.assertIn("Use the admin console.", history)

    def test_new_thread_does_not_classify_or_rewrite(self):
        def classify(*_args):
            raise AssertionError("a new thread is not a follow-up candidate")

        def improve(*_args):
            raise AssertionError("a new thread must not be rewritten")

        with patch("src.followup_classifier.jev_api_key", return_value="sekret"):
            generated, history = build_retrieval_args(
                NEW_QUESTION,
                [("User", NEW_QUESTION, "1")],
                may_rewrite=False,
                improve_query=improve,
                classify=classify,
            )
        self.assertEqual(generated, "")
        self.assertEqual(history, f"User: {NEW_QUESTION}")

    def test_confident_new_question_skips_rewrite_and_drops_prior_turns(self):
        seen = {}

        def classify(prior, new_message, api_key):
            seen["prior"] = prior
            seen["new"] = new_message
            seen["key"] = api_key
            return False, 0.08

        def improve(*_args):
            raise AssertionError("a new question must be searched as typed")

        with patch("src.followup_classifier.jev_api_key", return_value="sekret"):
            generated, history = build_retrieval_args(
                NEW_QUESTION,
                THREAD,
                may_rewrite=True,
                improve_query=improve,
                classify=classify,
            )
        self.assertEqual(seen["prior"], "User: How do certificates work?\nAssistant: Use the admin console.")
        self.assertEqual(seen["new"], NEW_QUESTION)
        self.assertEqual(seen["key"], "sekret")
        self.assertEqual(generated, "")
        self.assertEqual(history, f"User: {NEW_QUESTION}")
        self.assertNotIn("sekret", history)

    def test_confident_followup_rewrites_with_full_history(self):
        def classify(prior, _new_message, _api_key):
            self.assertIn("certificates", prior)
            return True, 0.93

        def improve(query, history):
            self.assertEqual(query, "what about version 7.4?")
            self.assertIn("certificates", history)
            self.assertIn("what about version 7.4?", history)
            return "How do certificates work in version 7.4?"

        with patch("src.followup_classifier.jev_api_key", return_value="sekret"):
            generated, history = build_retrieval_args(
                "what about version 7.4?",
                THREAD[:2] + [("User", "what about version 7.4?", "2")],
                may_rewrite=True,
                improve_query=improve,
                classify=classify,
            )
        self.assertEqual(generated, "How do certificates work in version 7.4?")
        self.assertIn("what about version 7.4?", history)
        self.assertIn("Use the admin console.", history)

    def test_no_prior_turns_skips_jev_and_rewrite(self):
        def classify(*_args):
            raise AssertionError("there is no prior turn to compare")

        def improve(*_args):
            raise AssertionError("a lone message must not be rewritten")

        with patch("src.followup_classifier.jev_api_key", return_value="sekret"):
            generated, history = build_retrieval_args(
                NEW_QUESTION,
                [("User", NEW_QUESTION, "1")],
                may_rewrite=True,
                improve_query=improve,
                classify=classify,
            )
        self.assertEqual(generated, "")
        self.assertEqual(history, f"User: {NEW_QUESTION}")

    def test_jev_error_falls_back_to_rewrite_and_redacts_the_key(self):
        secret = "jev-secret-value"

        def classify(_prior, _new_message, api_key):
            raise RuntimeError(f"HTTP 401 for {api_key}")

        with patch("src.followup_classifier.jev_api_key", return_value=secret):
            with self.assertLogs("RL_Logger", level="WARNING") as logs:
                generated, history = build_retrieval_args(
                    NEW_QUESTION,
                    THREAD,
                    may_rewrite=True,
                    improve_query=lambda _query, _history: "rewritten",
                    classify=classify,
                )
        self.assertEqual(generated, "rewritten")
        self.assertIn("Use the admin console.", history)
        rendered = "\n".join(logs.output)
        self.assertNotIn(secret, rendered)
        self.assertIn("[REDACTED]", rendered)


class DspyNoulCallTests(unittest.TestCase):
    def test_classify_uses_jev_noul_without_global_configure(self):
        predictor = MagicMock()
        predictor.fields = {}
        noul = MagicMock()
        noul.value = True
        noul.probability = 0.91
        predictor.return_value.depends_on_prior_turns = noul
        with patch("src.followup_classifier.dspy.Predict", return_value=predictor) as predict, patch(
            "src.followup_classifier.TypeSafe"
        ) as typesafe, patch("src.followup_classifier.dspy.configure") as configure:
            is_followup, probability = classify_depends_on_prior("prior turns", "and the timeout?", "sekret")

        configure.assert_not_called()
        predict.assert_called_once()
        signature = predict.call_args.args[0]
        self.assertIn("depends_on_prior_turns", signature.output_fields)
        self.assertEqual(
            predictor.fields["depends_on_prior_turns"],
            {"threshold": FOLLOWUP_YES_THRESHOLD},
        )
        typesafe.assert_called_once_with(JEV_MODEL, api_key="sekret", cache=False)
        self.assertIs(predictor.call_args.kwargs["lm"], typesafe.return_value)
        self.assertEqual(predictor.call_args.kwargs["prior_conversation"], "prior turns")
        self.assertEqual(predictor.call_args.kwargs["new_message"], "and the timeout?")
        self.assertNotIn("sekret", predictor.call_args.kwargs["prior_conversation"])
        self.assertTrue(is_followup)
        self.assertEqual(probability, 0.91)


if __name__ == "__main__":
    unittest.main()
