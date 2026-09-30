# Changes

## 2026-09-30

### Fix: drop tapbacks and reaction removals

Tapbacks are stored as separate message rows (`associated_message_type`
2000–2006, removals 3000–3006) whose text only quotes the message they react
to, e.g. `Loved “see you at 7”`. They duplicated content and could be read as
things the sender wrote.

- Query excludes `associated_message_type` 2000–3999; `NULL` is treated as a
  normal message.
- Test fixtures include a tapback and a removal on the target day.

#### Future implementation

- [openclaw/imsg#8](https://github.com/openclaw/imsg/pull/8): Preserve tapbacks
  as structured reactions attached to their target message instead of dropping
  them. A later version can use `associated_message_guid` to resolve the target
  and apply reaction removals to the stored reaction state.

### Fix: read messages stored in `attributedBody`

Newer macOS versions often leave `message.text` empty and store the message in
the archived `attributedBody` blob. The extractor skipped those rows, so on a
real database it saw 2 of 16 text messages from the last 7 days.

- Added `decode_attributed_body`, which pulls the plain string out of the
  typedstream blob (handles 1-, 2-, and 4-byte lengths; returns `None` on
  malformed input).
- `text` is used first, with the decoded blob as fallback. Attachment
  placeholders (`U+FFFC`) are stripped, and attachment-only messages are skipped.
- Tests cover blob-only messages, attachment-only blobs, emoji, long messages,
  and truncated or invalid blobs.
- README limits section updated.

#### References

- [openclaw/openclaw#73172](https://github.com/openclaw/openclaw/issues/73172):
  Documents blank iMessage history when `message.text` is `NULL` and the body
  exists only in `attributedBody`.
- [zeroclaw-labs/zeroclaw#3151](https://github.com/zeroclaw-labs/zeroclaw/pull/3151):
  Adds the same `text`-first, `attributedBody`-fallback approach with a targeted
  length-prefix decoder and parser tests.
- [anthropics/claude-plugins-official#1704](https://github.com/anthropics/claude-plugins-official/issues/1704):
  Describes how an incorrect typedstream length-prefix implementation truncates
  messages longer than 127 UTF-8 bytes.
