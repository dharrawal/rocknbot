"""
Unit tests for role-tagged thread formatting in techsupport_classifier.

Run from LilLisa_Server/cron:
    PYTHONPATH=. python3 tests/test_techsupport_classifier_format.py
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import techsupport_classifier as classifier  # noqa: E402

BOT_USER_ID = "UBOT"
EXPERT_USER_ID = "UEXPERT"


class FormatThreadMessagesRoleTagTests(unittest.TestCase):
    def test_bot_via_bot_id(self):
        messages = [{"ts": "1.0", "bot_id": "B123", "text": "restart the service"}]
        self.assertEqual(
            classifier.format_thread_messages(messages),
            "[1.0] Lil Lisa (bot): restart the service",
        )

    def test_bot_via_bot_user_id(self):
        messages = [{"ts": "1.0", "user": BOT_USER_ID, "text": "restart the service"}]
        self.assertEqual(
            classifier.format_thread_messages(messages, bot_user_id=BOT_USER_ID),
            "[1.0] Lil Lisa (bot): restart the service",
        )

    def test_bot_via_subtype(self):
        messages = [{"ts": "1.0", "subtype": "bot_message", "text": "hi"}]
        self.assertEqual(
            classifier.format_thread_messages(messages),
            "[1.0] Lil Lisa (bot): hi",
        )

    def test_bot_user_id_only_tags_the_bot(self):
        messages = [{"ts": "1.0", "user": "U1", "text": "hi"}]
        self.assertEqual(
            classifier.format_thread_messages(messages, bot_user_id=BOT_USER_ID),
            "[1.0] U1: hi",
        )

    def test_bot_username_overrides_default_name(self):
        messages = [{"ts": "1.0", "bot_id": "B123", "username": "Helper", "text": "hi"}]
        self.assertEqual(
            classifier.format_thread_messages(messages),
            "[1.0] Helper (bot): hi",
        )

    def test_bot_profile_name_overrides_default_name(self):
        messages = [{"ts": "1.0", "bot_id": "B123", "bot_profile": {"name": "Helper"}, "text": "hi"}]
        self.assertEqual(
            classifier.format_thread_messages(messages),
            "[1.0] Helper (bot): hi",
        )

    def test_custom_bot_display_name(self):
        messages = [{"ts": "1.0", "bot_id": "B123", "text": "hi"}]
        self.assertEqual(
            classifier.format_thread_messages(messages, bot_display_name="Rocknbot"),
            "[1.0] Rocknbot (bot): hi",
        )

    def test_expert_tagged(self):
        messages = [{"ts": "1.0", "user": EXPERT_USER_ID, "text": "actually it's a config flag"}]
        self.assertEqual(
            classifier.format_thread_messages(messages, expert_user_ids=[EXPERT_USER_ID]),
            "[1.0] UEXPERT (expert): actually it's a config flag",
        )

    def test_expert_tag_uses_resolved_display_name(self):
        messages = [{"ts": "1.0", "user": EXPERT_USER_ID, "text": "use the flag"}]
        with patch.object(classifier, "_resolve_user_name", return_value="Jane"):
            formatted = classifier.format_thread_messages(
                messages, slack_token="xoxb-test", expert_user_ids=[EXPERT_USER_ID]
            )
        self.assertEqual(formatted, "[1.0] Jane (expert): use the flag")

    def test_regular_user_untouched(self):
        messages = [{"ts": "1.0", "user": "U1", "text": "my job keeps failing"}]
        self.assertEqual(
            classifier.format_thread_messages(
                messages, bot_user_id=BOT_USER_ID, expert_user_ids=[EXPERT_USER_ID]
            ),
            "[1.0] U1: my job keeps failing",
        )

    def test_no_token_keeps_raw_ids_for_humans(self):
        messages = [
            {"ts": "1.0", "user": "U1", "text": "help"},
            {"ts": "2.0", "user": EXPERT_USER_ID, "text": "here"},
        ]
        with patch.object(classifier, "_resolve_user_name") as resolve:
            formatted = classifier.format_thread_messages(messages, expert_user_ids=[EXPERT_USER_ID])
        resolve.assert_not_called()
        self.assertEqual(formatted, "[1.0] U1: help\n[2.0] UEXPERT (expert): here")

    def test_processing_placeholder_skipped(self):
        messages = [
            {"ts": "1.0", "user": "U1", "text": "my job keeps failing"},
            {"ts": "2.0", "bot_id": "B123", "text": "Processing..."},
            {"ts": "3.0", "bot_id": "B123", "text": "try bumping the heap"},
        ]
        self.assertEqual(
            classifier.format_thread_messages(messages),
            "[1.0] U1: my job keeps failing\n[3.0] Lil Lisa (bot): try bumping the heap",
        )

    def test_escalation_relay_post_is_not_tagged_as_ai_answer(self):
        # The escalate button reposts the user's question under the bot's identity
        # in the techsupport channel; it must not read as unverified AI content.
        messages = [
            {"ts": "1.0", "bot_id": "B123", "text": "Question posted in ida. How do I rotate the CA?"},
            {"ts": "2.0", "user": "U2", "text": "Use the rotate-ca command."},
        ]
        self.assertEqual(
            classifier.format_thread_messages(messages),
            "[1.0] Lil Lisa (bot, relaying a user's question): Question posted in ida. How do I rotate the CA?\n"
            "[2.0] U2: Use the rotate-ca command.",
        )

    def test_ignored_subtypes_still_skipped(self):
        messages = [
            {"ts": "1.0", "user": "U1", "subtype": "channel_join", "text": "joined"},
            {"ts": "2.0", "user": "U1", "text": "real message"},
        ]
        self.assertEqual(
            classifier.format_thread_messages(messages),
            "[2.0] U1: real message",
        )

    def test_ordering_preserved_and_roles_mixed(self):
        messages = [
            {"ts": "3.0", "user": EXPERT_USER_ID, "text": "no, use --flag instead"},
            {"ts": "1.0", "user": "U1", "text": "my job keeps failing"},
            {"ts": "2.0", "user": BOT_USER_ID, "text": "try bumping the heap"},
        ]
        formatted = classifier.format_thread_messages(
            messages, bot_user_id=BOT_USER_ID, expert_user_ids=[EXPERT_USER_ID]
        )
        self.assertEqual(
            formatted,
            "[1.0] U1: my job keeps failing\n"
            "[2.0] Lil Lisa (bot): try bumping the heap\n"
            "[3.0] UEXPERT (expert): no, use --flag instead",
        )


class ClassifyThreadForwardsRoleKwargsTests(unittest.TestCase):
    MESSAGES = [
        {"ts": "1.0", "user": "U1", "text": "my job keeps failing"},
        {"ts": "2.0", "user": BOT_USER_ID, "text": "try bumping the heap"},
        {"ts": "3.0", "user": EXPERT_USER_ID, "text": "no, use --flag instead"},
    ]

    def _run(self, **kwargs):
        useful = MagicMock()
        useful.is_useful = "yes"
        conclusive = MagicMock()
        conclusive.is_conclusive = "yes"
        with patch.object(classifier, "configure_dspy_lm"), patch.object(
            classifier, "check_useful", return_value=useful
        ) as check_useful, patch.object(classifier, "check_conclusive", return_value=conclusive):
            result = classifier.classify_thread(self.MESSAGES, **kwargs)
        return result, check_useful

    def test_kwargs_forwarded_to_formatter(self):
        result, check_useful = self._run(bot_user_id=BOT_USER_ID, expert_user_ids=[EXPERT_USER_ID])
        expected = (
            "[1.0] U1: my job keeps failing\n"
            "[2.0] Lil Lisa (bot): try bumping the heap\n"
            "[3.0] UEXPERT (expert): no, use --flag instead"
        )
        self.assertEqual(result["conversation_thread"], expected)
        check_useful.assert_called_once_with(conversation_thread=expected)

    def test_existing_callers_unchanged(self):
        result, _ = self._run()
        self.assertEqual(
            result["conversation_thread"],
            "[1.0] U1: my job keeps failing\n"
            "[2.0] UBOT: try bumping the heap\n"
            "[3.0] UEXPERT: no, use --flag instead",
        )
        self.assertTrue(result["is_useful"])
        self.assertTrue(result["is_conclusive"])

    def test_positional_call_signature_still_works(self):
        useful = MagicMock()
        useful.is_useful = "no"
        with patch.object(classifier, "configure_dspy_lm"), patch.object(
            classifier, "check_useful", return_value=useful
        ), patch.object(classifier, "check_conclusive") as check_conclusive:
            result = classifier.classify_thread(self.MESSAGES, None, True)
        check_conclusive.assert_not_called()
        self.assertFalse(result["is_useful"])


class HasExpertInsightTests(unittest.TestCase):
    """The expert-insight gate: an expert's own questions are not insight."""

    THREAD = (
        "[1.0] U1: my job keeps failing\n"
        "[2.0] Lil Lisa (bot): try bumping the heap\n"
        "[3.0] UEXPERT (expert): no, restart the agent service instead"
    )

    def _run(self, answer):
        prediction = MagicMock()
        prediction.has_expert_insight = answer
        with (
            patch.object(classifier, "configure_dspy_lm") as configure,
            patch.object(classifier, "check_expert_insight", return_value=prediction) as check,
        ):
            result = classifier.has_expert_insight(self.THREAD)
        return result, configure, check

    def test_yes_is_true_and_passes_the_thread_through(self):
        result, configure, check = self._run("yes")
        self.assertTrue(result)
        configure.assert_called_once_with()
        check.assert_called_once_with(conversation_thread=self.THREAD)

    def test_no_is_false(self):
        result, _configure, _check = self._run("no")
        self.assertFalse(result)

    def test_normalises_the_same_way_the_classifiers_do(self):
        for answer in ("Yes", "YES", " yes. ", "yes!"):
            with self.subTest(answer=answer):
                self.assertTrue(self._run(answer)[0])

    def test_garbage_is_false_so_nothing_is_ingested(self):
        for answer in ("", "unknown", "maybe", None, "no."):
            with self.subTest(answer=answer):
                self.assertFalse(self._run(answer)[0])


class PromptRoleTagMentionTests(unittest.TestCase):
    def test_classifier_prompts_mention_role_tags(self):
        for signature, field in (
            (classifier.IsUsefulConversation, "is_useful"),
            (classifier.IsConclusiveConversation, "is_conclusive"),
            (classifier.HasExpertInsight, "has_expert_insight"),
        ):
            desc = signature.model_fields[field].json_schema_extra["desc"]
            self.assertIn("(bot)", desc)
            self.assertIn("(expert)", desc)

    def test_expert_insight_prompt_excludes_questions_and_the_parent(self):
        desc = classifier.HasExpertInsight.model_fields["has_expert_insight"].json_schema_extra["desc"]
        self.assertIn("thread parent", desc)
        for phrase in ("corrects", "confirms", "question"):
            self.assertIn(phrase, desc)


if __name__ == "__main__":
    unittest.main()
