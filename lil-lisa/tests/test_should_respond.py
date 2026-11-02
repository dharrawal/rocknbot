"""Who a product-channel post is for: first Slack addressee, then who else has posted.

A product-channel thread whose root @-addresses somebody else stays silent
until a message addresses the bot. Any thread goes silent once someone other
than the original poster and the bot has posted.

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


def unaddressed(participants, original_poster=ALICE):
    return slack.unaddressed_thread_is_for_bot(participants, original_poster, {BOT})


def reply_decision(participants, root_text, product_channel, original_poster=ALICE):
    return slack.unaddressed_reply_decision(
        participants,
        original_poster,
        {BOT},
        product_channel=product_channel,
        root_text=root_text,
        bot_user_id=BOT,
    )


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

    def test_a_reply_from_someone_else_is_not_the_original_poster(self):
        self.assertIs(unaddressed({BOB}, original_poster=ALICE), False)
        self.assertIs(unaddressed({ALICE, BOT, BOB}, original_poster=ALICE), False)


class AddressedThreadTests(unittest.TestCase):
    def test_user_and_usergroup_mentions_name_someone_else(self):
        self.assertIs(slack.addressed_to_someone_else(f"<@{ALICE}> how do I reset this?", BOT), True)
        self.assertIs(slack.addressed_to_someone_else("<!subteam^S123|@experts> please look", BOT), True)

    def test_the_bot_a_broadcast_and_no_mention_do_not(self):
        self.assertIs(slack.addressed_to_someone_else(f"<@{BOT}> how do I reset this?", BOT), False)
        self.assertIs(slack.addressed_to_someone_else(f"hey <@{BOT}|rocknbot> can you check", BOT), False)
        self.assertIs(slack.addressed_to_someone_else("<!channel> standup notes", BOT), False)
        self.assertIs(slack.addressed_to_someone_else("what version supports nested groups?", BOT), False)

    def test_bot_first_keeps_the_thread(self):
        self.assertIs(slack.addressed_to_someone_else(f"<@{BOT}> ask <@{ALICE}>", BOT), False)

    def test_product_thread_started_for_someone_else_stays_silent(self):
        answer, reason = reply_decision({ALICE, BOT}, f"<@{BOB}> how do I reset this?", True)
        self.assertIs(answer, False)
        self.assertEqual(reason, "product thread started addressed to someone else")

    def test_same_root_outside_a_product_channel_still_answers(self):
        answer, reason = reply_decision({ALICE}, f"<@{BOB}> how do I reset this?", False)
        self.assertIs(answer, True)
        self.assertEqual(reason, "no addressee; no other human has posted")

    def test_product_thread_with_no_addressee_still_answers(self):
        answer, _ = reply_decision({ALICE, BOT}, "what version supports nested groups?", True)
        self.assertIs(answer, True)

    def test_a_third_poster_stops_the_bot_in_any_channel(self):
        for product_channel in (True, False):
            answer, reason = reply_decision({ALICE, BOT, BOB}, "what version?", product_channel)
            self.assertIs(answer, False)
            self.assertEqual(reason, "no addressee; another human has posted")

    def test_product_channels_are_the_question_channels_only(self):
        self.assertIs(slack.is_product_channel("Cida"), True)
        self.assertIs(slack.is_product_channel("Ciddm"), True)
        self.assertIs(slack.is_product_channel("Cadmin"), False)


if __name__ == "__main__":
    unittest.main()
