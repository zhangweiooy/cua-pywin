from __future__ import annotations

from computer_server.learning.coalescer import KeystrokeBuncher, ScrollBuncher


def test_keystrokes_are_split_when_source_window_changes():
    flushed = []
    buncher = KeystrokeBuncher(lambda *args: flushed.append(args))

    buncher.append("a", "App A", "a.exe", "A", "1001", None)
    buncher.append("b", "Teaching", "teaching.exe", "Teaching", "9001", None)
    buncher.flush()

    assert [(item[0], item[4]) for item in flushed] == [
        ("a", "1001"),
        ("b", "9001"),
    ]


def test_keystrokes_can_flush_before_new_source_context_is_collected():
    flushed = []
    buncher = KeystrokeBuncher(lambda *args: flushed.append(args))

    buncher.append("a", "App A", "a.exe", "A", "1001", None)

    assert buncher.flush_if_source_changed("9001") is True
    assert buncher.is_empty() is True
    assert [(item[0], item[4]) for item in flushed] == [("a", "1001")]


def test_scrolls_are_split_when_source_window_changes():
    flushed = []
    buncher = ScrollBuncher(lambda *args: flushed.append(args))

    buncher.append(0, 1, 10, 10, "App A", "a.exe", "1001")
    buncher.append(0, 2, 20, 20, "Teaching", "teaching.exe", "9001")
    buncher.flush()

    assert [(item[1], item[2], item[6]) for item in flushed] == [
        (1, 10, "1001"),
        (2, 20, "9001"),
    ]
