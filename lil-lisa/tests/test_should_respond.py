"""Who a product-channel post is for: first Slack addressee, then who else has posted.

Run from lil-lisa:
    PYTHONPATH=src python3 -m unittest tests.test_should_respond
"""

import os
import sys
import unittest
from pathlib import Path


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


BOT = "UBOT0001"
ALICE = "UALICE01"
BOB = "UBOB0001"


def addresses(text):
    return slack.addressee_decision(text, BOT)


def unaddressed(participants, author=ALICE):
    return slack.unaddressed_thread_is_for_bot(participants, author, {BOT})


class AddresseeDecisionTests(unittest.TestCase):
    def test_bot_first_answers_even_when_someone_else_is_mentioned_later(self):
        self.assertIs(addresses(f"<@{BOT}> what about <@{ALICE}>"), True)

    def test_bot_with_display_name_not_at_the_start_is_still_first(self):
        self.assertIs(addresses(f"hey <@{BOT}|rocknbot> can you check"), True)

    def test_leading_whitespace_does_not_hide_the_bot(self):
        self.assertIs(addresses(f"  <@{BOT}>"), True)

    def test_someone_else_first_stays_silent_even_if_the_bot_is_mentioned_later(self):
        self.assertIs(addresses(f"<@{ALICE}> please ask <@{BOT}>"), False)

    def test_words_before_a_human_mention_still_let_that_mention_win(self):
        self.assertIs(addresses(f"can <@{ALICE}> and <@{BOT}> weigh in"), False)
        self.assertIs(addresses(f"can you look <@{ALICE}>"), False)

    def test_usergroup_and_broadcast_mentions_are_someone_else(self):
        self.assertIs(addresses("<!subteam^S123|@experts> please look"), False)
        self.assertIs(addresses("<!channel>"), False)
        self.assertIs(addresses("<!here>"), False)
        self.assertIs(addresses("<!everyone|everyone>"), False)

    def test_bot_before_a_usergroup_answers(self):
        self.assertIs(addresses(f"<@{BOT}> ask <!subteam^S123|@experts>"), True)

    def test_a_channel_link_is_not_an_addressee(self):
        self.assertIsNone(addresses("see <#C123|docs> for the steps"))
        self.assertIs(addresses(f"see <#C123|docs> and <@{BOT}>"), True)

    def test_no_mention_defers_to_who_has_posted(self):
        self.assertIsNone(addresses("what version supports nested groups?"))


class UnaddressedThreadTests(unittest.TestCase):
    def test_new_thread_with_only_the_author_answers(self):
        self.assertIs(unaddressed({ALICE}), True)
        self.assertIs(unaddressed(set()), True)

    def test_author_and_bot_only_answers(self):
        self.assertIs(unaddressed({ALICE, BOT}), True)

    def test_another_human_stays_silent(self):
        self.assertIs(unaddressed({ALICE, BOT, BOB}), False)
        self.assertIs(unaddressed({ALICE, BOB}), False)


if __name__ == "__main__":
    unittest.main()
