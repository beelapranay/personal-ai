# Personal AI

This repo is for building a local personal assistant around my own data. The
goal is to connect messages, email, calendar, and files through explicit tools,
with the model running locally and write actions kept behind confirmation.

## V1: iMessage summaries

V1 tests the smallest useful loop. A local Gemma model gets one tool, reads the
last seven days of iMessages, and writes a summary.

The tool opens `~/Library/Messages/chat.db` in read-only mode and returns each
message's sender, text, and timestamp. Ollama runs the model locally with a
16,384-token context window.

## V1 setup

Grant Full Disk Access to the terminal or app that runs the script:
**System Settings → Privacy & Security → Full Disk Access**. Quit and reopen the
app after changing the setting.

Start Ollama:

```bash
ollama serve
```

In another terminal, download the model:

```bash
ollama pull gemma4:e2b-it-qat
```

If you already have that model, skip the download. You can use another
tool-capable model by passing its name with `--model`.

Create a virtual environment and install the Python client:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

## Run V1

Check that the script can read the Messages database. This prints a count, not
the messages themselves:

```bash
python3 imessage_summary.py --check-db
```

Check that Gemma chooses the right tool without reading the database:

```bash
python3 imessage_summary.py --check-model
```

Generate the weekly summary and show token usage:

```bash
python3 imessage_summary.py --stats
```

The stats show prompt tokens, generated tokens, and how much of the configured
context window each model call used. They do not print the raw context.

Change the date range, model, or context window when needed:

```bash
python3 imessage_summary.py --days 14
python3 imessage_summary.py --model MODEL_NAME
python3 imessage_summary.py --context-window 32768
```

## Tests

The tests use a temporary Messages database and a fake Ollama client. They do
not read personal messages or require Ollama.

```bash
python3 -m unittest discover -s tests -v
```

## V1 limits

The extractor reads `message.text` and, when that is empty, decodes the plain
string from Apple's archived `attributedBody` field. Formatting, mentions, and
attachments are dropped; attachment-only messages are skipped.

There is no redaction layer yet. The model may repeat verification codes,
transaction details, phone numbers, or other sensitive text in its summary.
