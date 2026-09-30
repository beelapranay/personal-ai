from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

import imessage_summary as app


def apple_ns(value: datetime) -> int:
    return app._apple_nanoseconds(value)


def attributed_body(text: str) -> bytes:
    """Build a minimal typedstream blob shaped like Messages' attributedBody."""
    encoded = text.encode("utf-8")
    if len(encoded) < 0x80:
        length = bytes([len(encoded)])
    else:
        length = b"\x81" + len(encoded).to_bytes(2, "little")
    return (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84"
        b"\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92"
        b"\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
        + length
        + encoded
        + b"\x86\x84\x02iI\x01"
    )


class FakeClient:
    def __init__(self) -> None:
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return {
                "prompt_eval_count": 120,
                "eval_count": 8,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "get_recent_messages", "arguments": {}}}
                    ],
                }
            }
        return {
            "prompt_eval_count": 640,
            "eval_count": 40,
            "message": {"role": "assistant", "content": "A useful summary."},
        }


class IMessageSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.db_path = Path(temporary_directory.name) / "chat.db"
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                text TEXT,
                attributedBody BLOB,
                date INTEGER,
                is_from_me INTEGER,
                handle_id INTEGER,
                associated_message_type INTEGER DEFAULT 0
            );
            """
        )
        connection.execute("INSERT INTO handle (ROWID, id) VALUES (1, ?)", ("+15551234567",))
        rows = [
            ("too early", None, datetime(2026, 9, 27, 23, 59, tzinfo=timezone.utc), 0, 1),
            ("hello", None, datetime(2026, 9, 28, 9, 30, tzinfo=timezone.utc), 0, 1),
            ("reply", None, datetime(2026, 9, 28, 9, 31, tzinfo=timezone.utc), 1, None),
            (None, None, datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc), 0, 1),
            (
                None,
                attributed_body("from blob"),
                datetime(2026, 9, 28, 10, 5, tzinfo=timezone.utc),
                0,
                1,
            ),
            (
                None,
                attributed_body("\ufffc"),
                datetime(2026, 9, 28, 10, 10, tzinfo=timezone.utc),
                0,
                1,
            ),
            ("too late", None, datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc), 0, 1),
        ]
        connection.executemany(
            "INSERT INTO message (text, attributedBody, date, is_from_me, handle_id) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (text, body, apple_ns(when), mine, handle)
                for text, body, when, mine, handle in rows
            ],
        )
        connection.executemany(
            "INSERT INTO message "
            "(text, date, is_from_me, handle_id, associated_message_type) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (text, apple_ns(datetime(2026, 9, 28, 9, minute, tzinfo=timezone.utc)), 1, None, kind)
                for text, minute, kind in [
                    ("Loved “hello”", 32, 2000),
                    ("Removed a heart from “hello”", 33, 3000),
                ]
            ],
        )
        connection.commit()
        connection.close()

    def test_reads_only_nonempty_messages_from_target_day(self) -> None:
        messages = app.read_messages_for_day(
            self.db_path, date(2026, 9, 28), timezone.utc
        )
        self.assertEqual(
            [item["text"] for item in messages], ["hello", "reply", "from blob"]
        )
        self.assertEqual(messages[0]["sender"], "+15551234567")
        self.assertEqual(messages[1]["sender"], "Me")
        self.assertEqual(messages[0]["timestamp"], "2026-09-28T09:30:00+00:00")

    def test_model_exchange_includes_tool_result(self) -> None:
        client = FakeClient()
        usage_stats = []
        summary = app.summarize_with_model(
            client,
            model="test-model",
            db_path=self.db_path,
            now=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
            usage_stats=usage_stats,
        )
        self.assertEqual(summary, "A useful summary.")
        self.assertEqual(len(client.calls), 2)
        tool_message = client.calls[1]["messages"][-1]
        self.assertEqual(tool_message["role"], "tool")
        self.assertEqual(tool_message["tool_name"], "get_recent_messages")
        self.assertEqual(len(json.loads(tool_message["content"])), 4)
        self.assertEqual(client.calls[0]["options"]["num_ctx"], 16_384)
        self.assertEqual(client.calls[1]["options"]["num_ctx"], 16_384)
        self.assertEqual(usage_stats[0]["prompt_tokens"], 120)
        self.assertEqual(usage_stats[1]["prompt_tokens"], 640)

    def test_missing_tool_call_is_a_failure(self) -> None:
        class NoToolClient:
            def chat(self, **kwargs):
                return {"message": {"role": "assistant", "content": "I guessed."}}

        with self.assertRaisesRegex(app.SummaryError, "without calling"):
            app.summarize_with_model(
                NoToolClient(),
                model="test-model",
                db_path=self.db_path,
            )

    def test_model_only_check_does_not_execute_tool(self) -> None:
        client = FakeClient()
        tool_name = app.check_model_tool_call(client, model="test-model")
        self.assertEqual(tool_name, "get_recent_messages")
        self.assertEqual(len(client.calls), 1)

    def test_recent_messages_include_seven_calendar_days(self) -> None:
        messages = app.get_recent_messages(
            self.db_path,
            days=7,
            now=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            [item["text"] for item in messages],
            ["too early", "hello", "reply", "from blob"],
        )

    def test_decodes_attributed_body(self) -> None:
        self.assertEqual(app.decode_attributed_body(attributed_body("hi 👋")), "hi 👋")
        long_text = "x" * 300
        self.assertEqual(app.decode_attributed_body(attributed_body(long_text)), long_text)
        self.assertIsNone(app.decode_attributed_body(None))
        self.assertIsNone(app.decode_attributed_body(b"not a typedstream"))
        self.assertIsNone(app.decode_attributed_body(attributed_body("hello")[:-10]))

    def test_formats_usage_without_message_content(self) -> None:
        output = app.format_usage_stats(
            [
                {
                    "label": "final summary",
                    "prompt_tokens": 1_000,
                    "generated_tokens": 100,
                }
            ],
            context_window=16_384,
        )
        self.assertIn("1,000 prompt + 100 generated", output)
        self.assertIn("1,100 / 16,384 tokens (6.7%)", output)


if __name__ == "__main__":
    unittest.main()
