from twitch.bot import should_process_own_message


def check(**overrides: object) -> bool:
    kwargs: dict[str, object] = {
        "bot_id": "1",
        "owner_id": "1",
        "message_id": "abc",
        "text": "!np",
        "prefix": "!",
        "sent_ids": [],
    }
    kwargs.update(overrides)
    return should_process_own_message(**kwargs)  # type: ignore[arg-type]


def test_broadcaster_running_own_bot_can_use_commands() -> None:
    assert check() is True


def test_dedicated_bot_account_keeps_default_skip() -> None:
    assert check(bot_id="1", owner_id="2") is False


def test_missing_owner_id_keeps_default_skip() -> None:
    assert check(owner_id=None) is False


def test_own_reply_is_not_reprocessed() -> None:
    assert check(message_id="abc", sent_ids=["zzz", "abc"]) is False


def test_non_command_chatter_message_is_ignored() -> None:
    assert check(text="just chatting") is False
