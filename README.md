# Telegram antispam

A small Python 3.10+ script that watches new incoming messages using your Telegram
user account. It runs until you press Ctrl+C.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
```

Get an API ID and API hash from [my.telegram.org](https://my.telegram.org), under
**API development tools**, and put them in `config.json`. Alternatively, set
`TELEGRAM_API_ID` and `TELEGRAM_API_HASH` in your environment; these override the
config values. A bot token is not needed.

Add your OpenRouter API key to `.env` beside `antispam.py`:

```dotenv
OPENROUTER_API_KEY=your-api-key
```

An existing `OPENROUTER_API_KEY` environment variable takes precedence. `.env` is
ignored by Git. Install the updated requirements even when using an existing
virtual environment.

```bash
python antispam.py --dry-run
```

On the first run, Telethon prompts for your phone number, login code, and two-step
verification password if enabled. Later runs reuse `antispam.session`.
Keep the API hash and session file private: the session grants account access.
See [Telethon's sign-in guide](https://docs.telethon.dev/en/stable/basic/signing-in.html).

## Flags and dry run

By default, only private messages are checked. Spam filters apply only to the first
message observed from each sender; all subsequent messages are excluded from
flagging, including across restarts using the saved sender profiles. Senders already
in the profile file are considered previously seen. Existing Telegram history is
not checked to determine whether a sender is new.

A first message is flagged if any rule matches:

- The sender is not in your Telegram contacts.
- Telegram marks the sender as scam or fake.
- The text or media caption contains a configured suspicious phrase, ignoring case.
- It has at least `max_links` links, including links hidden behind display text.

If the text or media caption has 16 or more whitespace-separated words, the
message is excluded from flagging even when another rule matches.

If the text or media caption contains any configured `whitelist_phrases`, the
message is excluded from flagging even when another rule matches. Whitelist
matching also ignores case and logs the matched phrase. An empty whitelist
disables only the phrase-based override. A whitelisted first message still counts
as the sender's first observed message.

Every first observed message from a non-contact is also submitted to OpenRouter's
Decisions API using the `spam` question of type `noul` in `decisions_api.py` and the
`openai/gpt-6-luna-decisions` model. The input is the message text or caption, or an empty string
for messages without text; attachments and profile data are not sent. This
includes whitelisted messages, which are still excluded from heuristic rules
but now produce a record with empty `reasons`. No probability threshold is
applied, and the existing heuristic rules are otherwise unchanged.

Each record has `spam_probability` (a number from 0 to 1 when available) and
`decision_status` (`available`, `failed`, or `not_requested`). API failures are
logged and produce a null probability without stopping local processing.
Contact messages flagged by other rules have a null probability and
`not_requested` status. Failed decisions are not retried for previously seen
senders, including after a restart.

Test the API independently of Telegram:

```bash
python test_decision.py --text "Hello, have we met before?"
```

The command prints JSON containing `spam_probability`, or exits with an error.
It makes a real API request. The watcher also makes real API requests in dry-run
mode, so message text is sent to OpenRouter and its model provider and API charges can apply.

Flags are heuristics for review; legitimate messages can match them. Contacts are
loaded at startup and refreshed every five minutes. If a refresh fails, the last
successful list is retained until a later refresh succeeds.

Dry run prints each flag and its reasons to the console without saving flag
records or sender profiles. It reads existing profile state and tracks subsequent
changes in memory for that run. It still connects to Telegram and creates or updates the login session.
Run without `--dry-run` to also append flags to `flags.jsonl`:

```bash
python antispam.py
```

Application and Telethon log messages go to stderr by default. Set `log_file` in
`config.json` to append them to a UTF-8 logfile instead, for example:

```json
{
  "log_file": "antispam.log"
}
```

The path is relative to the working directory, like the other configured paths.
Set it to `null` to keep logging to stderr.

Each record includes the timestamp, chat/message/sender IDs, username, reasons,
and full message text. Store this file privately. This version flags locally only:
it does not delete messages, block users, report spam, or send messages.

Each flag also includes `sender_profile` with:

- Current first name, last name, username, and visible profile photo ID.
- `first_seen_at`: when this script first observed the sender.
- `name_change_observed_at`: when a change to their name or username was last noticed.
- `photo_change_observed_at`: when a change to their visible photo ID was last noticed,
  including a photo disappearing or becoming visible.
- `photo_uploaded_at`: Telegram's upload date for the current photo, when obtainable.
- `photo_lookup_status`: `available`, `unavailable`, `no_visible_photo`, or `lookup_failed`.

The upload date is not necessarily when the photo was selected as the avatar.
Change timestamps are observation times, not Telegram's exact change times;
changes between observations can be missed. Initial observations have null change
dates. Account creation dates are unavailable and are not inferred.

In normal mode, `sender_profiles.json` preserves observations between runs for all
eligible incoming senders, even when their messages are not flagged. Set
`profiles_file` to change its location. Keep it private alongside the flags file.
Photo dates are fetched only for flagged messages, at most once per sender/photo
ID per run. Lookup failures still produce flags and are retried after a restart.

Set `include_groups` to `true` to also check group messages. Broadcast channel
posts and anonymous/channel senders are skipped. Messages sent by your account
are skipped. Existing history and message edits are not scanned.
When group checking is enabled, the same first-message/non-contact decision
rule also applies to group senders, using the same private-message instructions.

Edit `suspicious_phrases`, `whitelist_phrases`, `max_links`, or
`contact_refresh_seconds` to tune the rules. Empty phrase lists disable their
respective phrase matching. Paths are relative to the working directory; use
`--config /path/to/config.json` for another config file.
