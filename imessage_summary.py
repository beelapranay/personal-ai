#!/usr/bin/env python3
"""Summarize recent iMessages with an Ollama model and one tool call."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sqlite3
import sys
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote


APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
DEFAULT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"
DEFAULT_MODEL = "gemma4:e2b-it-qat"
DEFAULT_DAYS = 7
DEFAULT_CONTEXT_WINDOW = 16_384

GET_RECENT_MESSAGES_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_recent_messages",
        "description": "Fetch all iMessages sent or received during the requested period",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


class SummaryError(RuntimeError):
    """A user-facing error while reading messages or running the model."""


def _timedelta_to_nanoseconds(delta: timedelta) -> int:
    return (
        (delta.days * 86_400 + delta.seconds) * 1_000_000_000
        + delta.microseconds * 1_000
    )


def _apple_nanoseconds(value: datetime) -> int:
    return _timedelta_to_nanoseconds(value.astimezone(timezone.utc) - APPLE_EPOCH)


def _datetime_from_apple_nanoseconds(value: int, local_tz: tzinfo) -> datetime:
    # sqlite stores an integer number of nanoseconds, while datetime supports
    # microseconds. Dropping sub-microsecond precision is harmless for display.
    return (APPLE_EPOCH + timedelta(microseconds=value // 1_000)).astimezone(local_tz)


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Extract plain text from an attributedBody typedstream blob.

    Newer macOS versions often leave message.text NULL and store the content
    as an archived NSAttributedString. The plain string follows the NSString
    class name as a '+' type tag, a length, and UTF-8 bytes.
    """
    if not blob:
        return None
    marker = blob.find(b"NSString")
    if marker == -1:
        return None
    # A few class-info bytes sit between the class name and the '+' tag.
    tag = blob.find(b"+", marker + len(b"NSString"), marker + len(b"NSString") + 16)
    if tag == -1:
        return None

    position = tag + 1
    if position >= len(blob):
        return None
    # typedstream integers: small values inline, 0x81/0x82 prefix 2/4-byte
    # little-endian values.
    prefix = blob[position]
    if prefix == 0x81:
        length = int.from_bytes(blob[position + 1 : position + 3], "little")
        position += 3
    elif prefix == 0x82:
        length = int.from_bytes(blob[position + 1 : position + 5], "little")
        position += 5
    else:
        length = prefix
        position += 1

    raw = blob[position : position + length]
    if len(raw) != length:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _message_text(text: str | None, attributed_body: bytes | None) -> str | None:
    # U+FFFC marks where an attachment sat; it carries no readable content.
    for candidate in (text, decode_attributed_body(attributed_body)):
        if candidate is not None:
            cleaned = candidate.replace("\ufffc", "").strip()
            if cleaned:
                return cleaned
    return None


def _read_only_connection(db_path: Path) -> sqlite3.Connection:
    resolved = db_path.expanduser().resolve()
    if not resolved.is_file():
        raise SummaryError(f"Messages database not found: {resolved}")

    # mode=ro prevents accidental writes to chat.db. Quoting also makes paths
    # containing spaces safe in SQLite's URI form.
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise SummaryError(
            f"Could not open {resolved}. Grant Full Disk Access to the app "
            "running this script, then try again."
        ) from exc
    connection.row_factory = sqlite3.Row
    return connection


def read_messages_for_day(
    db_path: Path | str,
    target_day: date,
    local_tz: tzinfo,
) -> list[dict[str, str]]:
    """Read non-empty messages for one local calendar day, oldest first."""
    return read_messages_for_range(
        db_path=db_path,
        start_day=target_day,
        end_day=target_day + timedelta(days=1),
        local_tz=local_tz,
    )


def read_messages_for_range(
    db_path: Path | str,
    start_day: date,
    end_day: date,
    local_tz: tzinfo,
) -> list[dict[str, str]]:
    """Read messages in a local-date range with an exclusive end date."""
    if end_day <= start_day:
        raise ValueError("end_day must be later than start_day")
    start_local = datetime.combine(start_day, datetime.min.time(), tzinfo=local_tz)
    end_local = datetime.combine(end_day, datetime.min.time(), tzinfo=local_tz)
    start_ns = _apple_nanoseconds(start_local)
    end_ns = _apple_nanoseconds(end_local)

    query = """
        SELECT
            m.text AS text,
            m.attributedBody AS attributed_body,
            m.date AS message_date,
            m.is_from_me AS is_from_me,
            h.id AS handle_id
        FROM message AS m
        LEFT JOIN handle AS h ON h.ROWID = m.handle_id
        WHERE m.date >= ?
          AND m.date < ?
          AND (
              (m.text IS NOT NULL AND TRIM(m.text) != '')
              OR m.attributedBody IS NOT NULL
          )
        ORDER BY m.date ASC, m.ROWID ASC
    """

    connection = _read_only_connection(Path(db_path))
    try:
        rows = connection.execute(query, (start_ns, end_ns)).fetchall()
    except sqlite3.Error as exc:
        raise SummaryError(f"Could not query the Messages database: {exc}") from exc
    finally:
        connection.close()

    messages: list[dict[str, str]] = []
    for row in rows:
        text = _message_text(row["text"], row["attributed_body"])
        if text is None:
            continue
        sender = "Me" if row["is_from_me"] else (row["handle_id"] or "Unknown sender")
        timestamp = _datetime_from_apple_nanoseconds(
            int(row["message_date"]), local_tz
        ).isoformat(timespec="seconds")
        messages.append(
            {
                "sender": str(sender),
                "text": text,
                "timestamp": timestamp,
            }
        )
    return messages


def get_todays_messages(
    db_path: Path | str = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """Return today's iMessages in the computer's local timezone."""
    current = now or datetime.now().astimezone()
    local_tz = current.tzinfo
    if local_tz is None:  # Defensive only; astimezone() always supplies one.
        raise SummaryError("Could not determine the local timezone")
    return read_messages_for_day(db_path, current.date(), local_tz)


def get_recent_messages(
    db_path: Path | str = DEFAULT_DB_PATH,
    days: int = DEFAULT_DAYS,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """Return messages from the last N local calendar days, including today."""
    if days < 1:
        raise ValueError("days must be at least 1")
    current = now or datetime.now().astimezone()
    local_tz = current.tzinfo
    if local_tz is None:
        raise SummaryError("Could not determine the local timezone")
    start_day = current.date() - timedelta(days=days - 1)
    end_day = current.date() + timedelta(days=1)
    return read_messages_for_range(db_path, start_day, end_day, local_tz)


def prompt_for_period(days: int) -> str:
    if days == 1:
        return "Summarize my iMessages from today"
    return f"Summarize my iMessages from the last {days} days, including today"


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return cast(Mapping[str, Any], value).get(name, default)
    return getattr(value, name, default)


def _message_from_response(response: Any) -> Any:
    message = _field(response, "message")
    if message is None:
        raise SummaryError("Ollama returned a response without a message")
    return message


def _tool_call_name(tool_call: Any) -> str | None:
    function = _field(tool_call, "function", {})
    return _field(function, "name")


def _record_usage(
    response: Any,
    label: str,
    usage_stats: list[dict[str, int | str]] | None,
) -> None:
    if usage_stats is None:
        return
    prompt_tokens = int(_field(response, "prompt_eval_count", 0) or 0)
    generated_tokens = int(_field(response, "eval_count", 0) or 0)
    usage_stats.append(
        {
            "label": label,
            "prompt_tokens": prompt_tokens,
            "generated_tokens": generated_tokens,
        }
    )


def format_usage_stats(
    usage_stats: list[dict[str, int | str]], context_window: int
) -> str:
    """Format token counts without exposing any message content."""
    lines = ["Token usage:"]
    for item in usage_stats:
        prompt_tokens = int(item["prompt_tokens"])
        generated_tokens = int(item["generated_tokens"])
        used_tokens = prompt_tokens + generated_tokens
        percentage = used_tokens / context_window * 100
        lines.append(
            f"  {item['label']}: {prompt_tokens:,} prompt + "
            f"{generated_tokens:,} generated = {used_tokens:,} / "
            f"{context_window:,} tokens ({percentage:.1f}%)"
        )
    return "\n".join(lines)


def summarize_with_model(
    client: Any,
    model: str,
    db_path: Path | str = DEFAULT_DB_PATH,
    days: int = DEFAULT_DAYS,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    prompt: str | None = None,
    now: datetime | None = None,
    usage_stats: list[dict[str, int | str]] | None = None,
) -> str:
    """Run the two-turn Ollama tool-calling exchange."""
    effective_prompt = prompt or prompt_for_period(days)
    conversation: list[Any] = [{"role": "user", "content": effective_prompt}]
    first_response = client.chat(
        model=model,
        messages=conversation,
        tools=[GET_RECENT_MESSAGES_TOOL],
        options={"num_ctx": context_window},
    )
    _record_usage(first_response, "tool decision", usage_stats)
    assistant_message = _message_from_response(first_response)
    tool_calls = cast(
        list[Any], _field(assistant_message, "tool_calls", []) or []
    )

    if not tool_calls:
        model_text = _field(assistant_message, "content", "")
        detail = f" Model response: {model_text}" if model_text else ""
        raise SummaryError(
            "The model answered without calling get_recent_messages; this run "
            f"does not meet the tool-calling success criterion.{detail}"
        )

    if len(tool_calls) != 1 or _tool_call_name(tool_calls[0]) != "get_recent_messages":
        names = [_tool_call_name(call) for call in tool_calls]
        raise SummaryError(f"The model requested unexpected tool calls: {names}")

    messages = get_recent_messages(db_path=db_path, days=days, now=now)
    conversation.append(assistant_message)
    conversation.append(
        {
            "role": "tool",
            "tool_name": "get_recent_messages",
            "content": json.dumps(messages, ensure_ascii=False),
        }
    )

    final_response = client.chat(
        model=model,
        messages=conversation,
        tools=[GET_RECENT_MESSAGES_TOOL],
        options={"num_ctx": context_window},
    )
    _record_usage(final_response, "final summary", usage_stats)
    final_message = _message_from_response(final_response)
    final_text = _field(final_message, "content", "")
    if not final_text or not str(final_text).strip():
        raise SummaryError("Ollama returned an empty final summary")
    return str(final_text).strip()


def check_model_tool_call(
    client: Any,
    model: str,
    days: int = DEFAULT_DAYS,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    usage_stats: list[dict[str, int | str]] | None = None,
) -> str:
    """Ask the model for a tool call without reading or executing the tool."""
    response = client.chat(
        model=model,
        messages=[{"role": "user", "content": prompt_for_period(days)}],
        tools=[GET_RECENT_MESSAGES_TOOL],
        options={"num_ctx": context_window},
    )
    _record_usage(response, "tool decision", usage_stats)
    message = _message_from_response(response)
    tool_calls = cast(list[Any], _field(message, "tool_calls", []) or [])
    if not tool_calls:
        model_text = _field(message, "content", "")
        detail = f" Model response: {model_text}" if model_text else ""
        raise SummaryError(f"The model did not request a tool call.{detail}")
    names = [_tool_call_name(call) for call in tool_calls]
    if names != ["get_recent_messages"]:
        raise SummaryError(f"The model requested unexpected tool calls: {names}")
    name = names[0]
    if name is None:
        raise SummaryError("The model requested a tool call without a name")
    return name


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize recent iMessages using an Ollama tool call."
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL),
        help="Ollama model name (default: %(default)s)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="Path to the Messages chat.db file",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help="Number of calendar days to include (default: %(default)s)",
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=DEFAULT_CONTEXT_WINDOW,
        help="Ollama context window in tokens (default: %(default)s)",
    )
    parser.add_argument(
        "--check-db",
        action="store_true",
        help="Only verify database access and report the selected period's count",
    )
    parser.add_argument(
        "--check-model",
        action="store_true",
        help="Only verify that the model requests get_recent_messages",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show token usage without printing raw context data",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.days < 1:
            raise SummaryError("--days must be at least 1")
        if args.context_window < 1:
            raise SummaryError("--context-window must be at least 1")
        if args.check_db:
            messages = get_recent_messages(db_path=args.db, days=args.days)
            print(
                "Database access OK; found "
                f"{len(messages)} text messages in the last {args.days} day(s)."
            )
            return 0

        try:
            ollama = importlib.import_module("ollama")
        except ImportError as exc:
            raise SummaryError(
                "The Ollama Python package is missing. Install dependencies with "
                "python3 -m pip install -r requirements.txt"
            ) from exc

        usage_stats: list[dict[str, int | str]] = []
        if args.check_model:
            tool_name = check_model_tool_call(
                ollama,
                model=args.model,
                days=args.days,
                context_window=args.context_window,
                usage_stats=usage_stats,
            )
            print(
                f"Model tool calling OK at {args.context_window} tokens; "
                f"requested {tool_name}."
            )
            if args.stats:
                print(
                    format_usage_stats(usage_stats, args.context_window),
                    file=sys.stderr,
                )
            return 0

        summary = summarize_with_model(
            ollama,
            model=args.model,
            db_path=args.db,
            days=args.days,
            context_window=args.context_window,
            usage_stats=usage_stats,
        )
        print(summary)
        if args.stats:
            print(format_usage_stats(usage_stats, args.context_window), file=sys.stderr)
        return 0
    except SummaryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Ollama's connection and model errors differ by package version. Keep the
        # CLI error concise while preserving the original message.
        print(f"Error communicating with Ollama: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
