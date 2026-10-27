"""Unit tests for single ADMIN_CHANNEL_ID product resolution and slash commands."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

os.environ.update(
    {
        "LIL_LISA_SLACK_USERID": "Ubot",
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_APP_TOKEN": "xapp-test",
        "CHANNEL_ID_IDDM": "Ciddm",
        "CHANNEL_ID_IDA": "Cida",
        "ADMIN_CHANNEL_ID": "Cadmin",
        "AUTHENTICATION_KEY": "test-authentication-key",
        "MAX_LENGTH": "4000",
        "LIL_LISA_SERVER_URL": "http://server.test",
        "EXPERT_GROUP_ID_IDA": "Sida",
        "EXPERT_GROUP_ID_IDDM": "Siddm",
    }
)

import slack  # noqa: E402


class AdminChannelProductTagTests(unittest.IsolatedAsyncioTestCase):
    def test_product_channel_ignores_conflicting_inline_tag(self) -> None:
        product, cleaned = slack.determine_product_and_clean_text(
            "Ciddm", "[IDA] how does IDDM connect to IDA?"
        )
        self.assertEqual(product, "IDDM")
        self.assertEqual(cleaned, "[IDA] how does IDDM connect to IDA?")

    def test_admin_channel_parses_and_strips_bracket_and_colon_tags(self) -> None:
        product, cleaned = slack.determine_product_and_clean_text(
            "Cadmin", "<@Ubot> [ida] How do I configure connectors?"
        )
        self.assertEqual(product, "IDA")
        self.assertEqual(cleaned, "How do I configure connectors?")

        product2, cleaned2 = slack.determine_product_and_clean_text(
            "Cadmin", "IDDM: How do I configure caching?"
        )
        self.assertEqual(product2, "IDDM")
        self.assertEqual(cleaned2, "How do I configure caching?")

    def test_admin_channel_without_tag_returns_none(self) -> None:
        product, cleaned = slack.determine_product_and_clean_text(
            "Cadmin", "How do I configure connectors?"
        )
        self.assertIsNone(product)
        self.assertEqual(cleaned, "How do I configure connectors?")

    async def test_admin_channel_thread_reply_inherits_parent_product(self) -> None:
        parent_payload = {
            "messages": [
                {"ts": "100.0", "text": "<@Ubot> [IDA] How do I configure connectors?"},
                {"ts": "101.0", "text": "Follow-up question without a tag"},
            ]
        }
        with patch.object(
            slack,
            "conversations_replies_with_retry",
            new=AsyncMock(return_value=parent_payload),
        ):
            product, cleaned = await slack.resolve_thread_product(
                "Cadmin", "100.0", "Follow-up question without a tag"
            )
        self.assertEqual(product, "IDA")
        self.assertEqual(cleaned, "Follow-up question without a tag")

    async def test_update_golden_qa_pairs_works_from_admin_channel(self) -> None:
        ack = AsyncMock()
        say = AsyncMock()
        dm = MagicMock()
        dm.data = {"channel": {"id": "Dadmin"}}
        resp = MagicMock()
        resp.text = "Golden QA pair update initiated."

        with (
            patch.object(slack.app.client, "conversations_open", new=AsyncMock(return_value=dm)),
            patch.object(slack, "check_members", new=AsyncMock(return_value=True)),
            patch.object(slack, "_requests_call", new=AsyncMock(return_value=resp)) as req_mock,
        ):
            await slack.update_golden_qa_pairs(
                ack, {"user_id": "Uadmin", "channel_id": "Cadmin"}, say
            )

        req_mock.assert_awaited_once()
        self.assertEqual(req_mock.await_args.kwargs["params"]["product"], "ALL")
        say.assert_awaited_once_with(channel="Dadmin", text="Golden QA pair update initiated.")


if __name__ == "__main__":
    unittest.main()
