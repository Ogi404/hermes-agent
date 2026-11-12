"""ENS-27: isolate the engineering route on a shared Slack bot."""

import pytest

from gateway.profile_routing import match_profile_route, parse_profile_routes


@pytest.fixture
def engineering_routes():
    return parse_profile_routes([
        {
            "name": "engineering-hub",
            "platform": "slack",
            "profile": "engineering",
            "chat_id": "CTESTENG01",
            "user_id": "UTESTOWNER1",
            "bot_profile": "default",
        },
    ])


@pytest.mark.parametrize("in_thread", [False, True], ids=["channel", "thread"])
def test_engineering_sender_matches_shared_bot_channel_and_thread(
    engineering_routes, in_thread,
):
    match = match_profile_route(
        engineering_routes,
        "slack",
        chat_id="1700000000.000001" if in_thread else "CTESTENG01",
        thread_id="1700000000.000001" if in_thread else None,
        parent_chat_id="CTESTENG01" if in_thread else None,
        user_id="UTESTOWNER1",
        adapter_profile="default",
    )

    assert match is not None
    assert match.name == "engineering-hub"
    assert match.profile == "engineering"


@pytest.mark.parametrize("in_thread", [False, True], ids=["channel", "thread"])
@pytest.mark.parametrize(
    ("channel_id", "user_id", "adapter_profile"),
    [
        ("CTESTENG01", "UTESTOTHER1", "default"),
        ("CTESTOTHER1", "UTESTOWNER1", "default"),
        ("CTESTENG01", "UTESTOWNER1", "other-bot"),
    ],
    ids=["different-sender", "unrelated-channel", "different-receiving-bot"],
)
def test_out_of_scope_slack_messages_do_not_match_engineering(
    engineering_routes, in_thread, channel_id, user_id, adapter_profile,
):
    match = match_profile_route(
        engineering_routes,
        "slack",
        chat_id="1700000000.000001" if in_thread else channel_id,
        thread_id="1700000000.000001" if in_thread else None,
        parent_chat_id=channel_id if in_thread else None,
        user_id=user_id,
        adapter_profile=adapter_profile,
    )

    assert match is None
