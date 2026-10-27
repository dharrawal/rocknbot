"""Unit tests for the admin-only /run_nightly_pipeline Slack command.

Run from lil-lisa:
    PYTHONPATH=src .venv/bin/python -m unittest tests.test_run_nightly_pipeline_command
"""

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


class RunNightlyPipelineCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ack = AsyncMock()
        self.say = AsyncMock()
        self.body = {"user_id": "Uadmin"}
        self.dm = MagicMock()
        self.dm.data = {"channel": {"id": "Dadmin"}}
        self.response = MagicMock()
        self.response.text = "Nightly techsupport pipeline initiated."

    async def test_authorized_admin_starts_the_background_pipeline(self):
        with (
            patch.object(slack.app.client, "conversations_open", new=AsyncMock(return_value=self.dm)),
            patch.object(slack, "check_members", new=AsyncMock(return_value=True)),
            patch.object(
                slack,
                "_trigger_techsupport_pipeline",
                return_value="Nightly techsupport pipeline initiated.",
            ) as trigger_mock,
        ):
            await slack.run_nightly_pipeline(self.ack, self.body, self.say)

        self.ack.assert_awaited_once_with()
        trigger_mock.assert_called_once_with(force=True)
        self.say.assert_awaited_once_with(channel="Dadmin", text="Nightly techsupport pipeline initiated.")

    async def test_unauthorized_user_does_not_start_the_pipeline(self):
        with (
            patch.object(slack.app.client, "conversations_open", new=AsyncMock(return_value=self.dm)),
            patch.object(slack, "check_members", new=AsyncMock(return_value=False)),
            patch.object(slack, "_trigger_techsupport_pipeline") as trigger_mock,
        ):
            await slack.run_nightly_pipeline(self.ack, self.body, self.say)

        self.ack.assert_awaited_once_with()
        trigger_mock.assert_not_called()
        self.say.assert_awaited_once()
        self.assertIn("Unauthorized", self.say.await_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
