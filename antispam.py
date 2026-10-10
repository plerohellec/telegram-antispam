#!/usr/bin/env python3
"""Watch incoming Telegram messages and record suspicious ones locally."""

import argparse
import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, events, functions, types


LOG = logging.getLogger("antispam")
LINK = re.compile(r"(?:https?://|www\.|t\.me/)[^\s]+", re.IGNORECASE)
WHITELIST_MIN_WORDS = 16
DEFAULTS = {
    "session": "antispam",
    "flags_file": "flags.jsonl",
    "profiles_file": "sender_profiles.json",
    "include_groups": False,
    "contact_refresh_seconds": 300,
    "max_links": 3,
    "suspicious_phrases": ["guaranteed profit", "claim your prize", "seed phrase"],
    "whitelist_phrases": [],
}


def load_config(path):
    with path.open(encoding="utf-8") as source:
        supplied = json.load(source)
    if not isinstance(supplied, dict):
        raise ValueError("Configuration must be a JSON object")
    unknown = supplied.keys() - (DEFAULTS.keys() | {"api_id", "api_hash"})
    if unknown:
        raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
    config = DEFAULTS | supplied
    config["api_id"] = int(os.environ.get("TELEGRAM_API_ID", config.get("api_id", 0)))
    config["api_hash"] = os.environ.get("TELEGRAM_API_HASH", config.get("api_hash", ""))
    if config["api_id"] <= 0 or not re.fullmatch(r"[a-fA-F0-9]{32}", str(config["api_hash"])):
        raise ValueError("Set a valid api_id and api_hash in config or TELEGRAM_API_ID/TELEGRAM_API_HASH")
    if not isinstance(config["include_groups"], bool):
        raise ValueError("include_groups must be true or false")
    for key in ("max_links", "contact_refresh_seconds"):
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    for key in ("suspicious_phrases", "whitelist_phrases"):
        phrases = config[key]
        if not isinstance(phrases, list) or any(not isinstance(p, str) or not p.strip() for p in phrases):
            raise ValueError(f"{key} must be a list of nonempty strings")
    for key in ("session", "flags_file", "profiles_file"):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f"{key} must be a nonempty path")
    if Path(config["profiles_file"]).resolve() == Path(config["flags_file"]).resolve():
        raise ValueError("profiles_file and flags_file must be different paths")
    return config


def reasons_for(message, sender, contacts, config):
    text = (message.raw_text or "").casefold()
    if len((message.raw_text or "").split()) >= WHITELIST_MIN_WORDS:
        LOG.info("Message excluded by word-count whitelist: %d or more words", WHITELIST_MIN_WORDS)
        return []
    for phrase in config["whitelist_phrases"]:
        if phrase.casefold() in text:
            LOG.info("Message excluded by whitelist phrase: %s", phrase)
            return []

    reasons = []
    if sender.id not in contacts and not sender.contact:
        reasons.append("sender is not in contacts")
    for label in ("scam", "fake"):
        if getattr(sender, label, False):
            reasons.append(f"Telegram marks sender as {label}")
    for phrase in config["suspicious_phrases"]:
        if phrase.casefold() in text:
            reasons.append(f"suspicious phrase: {phrase}")
    # Count Telegram URL entities, including links hidden behind display text.
    entity_links = sum(
        isinstance(entity, (types.MessageEntityUrl, types.MessageEntityTextUrl))
        for entity in message.entities or []
    )
    link_count = max(entity_links, len(LINK.findall(text)))
    if link_count >= config["max_links"]:
        reasons.append(f"{link_count} links (threshold: {config['max_links']})")
    return reasons


def load_profiles(path):
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as source:
        profiles = json.load(source)
    if not isinstance(profiles, dict) or any(
        not isinstance(profile, dict)
        or not {"first_name", "last_name", "username", "profile_photo_id", "first_seen_at"} <= profile.keys()
        for profile in profiles.values()
    ):
        raise ValueError(f"Invalid sender profile data in {path}")
    return profiles


def observe_profile(sender, previous, now):
    photo_id = getattr(sender.photo, "photo_id", None)
    profile = dict(previous or {})
    name = {"first_name": sender.first_name, "last_name": sender.last_name, "username": sender.username}
    if previous:
        if any(previous.get(key) != value for key, value in name.items()):
            profile["name_change_observed_at"] = now
        if previous.get("profile_photo_id") != photo_id:
            profile["photo_change_observed_at"] = now
            profile["photo_uploaded_at"] = None
    else:
        profile.update(
            first_seen_at=now,
            name_change_observed_at=None,
            photo_change_observed_at=None,
            photo_uploaded_at=None,
        )
    profile.update(name)
    profile["profile_photo_id"] = photo_id
    return profile


def save_profiles(path, profiles):
    # Replace atomically so a shutdown during writing does not truncate the state.
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(profiles, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


async def watch(config, dry_run):
    profiles_path = Path(config["profiles_file"])
    profiles = load_profiles(profiles_path)
    photo_lookups = {}  # One lookup per sender/photo ID per run, including failures.
    client = TelegramClient(
        config["session"], config["api_id"], config["api_hash"], sequential_updates=True
    )
    contacts = set()
    refreshed_at = 0.0

    async def refresh_contacts():
        nonlocal contacts, refreshed_at
        result = await client(functions.contacts.GetContactsRequest(hash=0))
        contacts = {contact.user_id for contact in result.contacts}
        refreshed_at = time.monotonic()

    async def handle(event):
        nonlocal refreshed_at
        if not event.is_private and not (config["include_groups"] and event.is_group):
            return
        try:
            sender = await event.get_sender()
            # Skip channel posts, anonymous group senders, and our own account.
            if not isinstance(sender, types.User) or sender.is_self:
                return
            now = datetime.now(timezone.utc).isoformat()
            previous = profiles.get(str(sender.id))
            profile = observe_profile(sender, previous, now)
            profiles[str(sender.id)] = profile
            if time.monotonic() - refreshed_at >= config["contact_refresh_seconds"]:
                try:
                    await refresh_contacts()
                except Exception:
                    LOG.exception("Contact refresh failed; retaining the previous contact list")
                    refreshed_at = time.monotonic()  # Retry after the refresh interval.
            reasons = reasons_for(event.message, sender, contacts, config) if previous is None else []
            photo_id = profile["profile_photo_id"]
            photo_status = "no_visible_photo"
            if reasons and photo_id is not None:
                lookup_key = (sender.id, photo_id)
                if lookup_key not in photo_lookups:
                    try:
                        photos = await client.get_profile_photos(sender, limit=1)
                        # A newest visible photo may differ from the current avatar.
                        photo = next((p for p in photos if p.id == photo_id), None)
                        photo_lookups[lookup_key] = (
                            photo.date.isoformat() if photo else None,
                            "available" if photo else "unavailable",
                        )
                    except Exception:
                        LOG.exception("Profile photo lookup failed for sender %s", sender.id)
                        photo_lookups[lookup_key] = (None, "lookup_failed")
                uploaded_at, photo_status = photo_lookups[lookup_key]
                if photo_status != "lookup_failed":
                    profile["photo_uploaded_at"] = uploaded_at
            if not dry_run and profile != previous:
                try:
                    save_profiles(profiles_path, profiles)
                except OSError:
                    LOG.exception("Could not save sender profiles to %s", profiles_path)
            if not reasons:
                return
            record = {
                "flagged_at": now,
                "message_date": event.date.isoformat(),
                "chat_id": event.chat_id,
                "message_id": event.id,
                "sender_id": sender.id,
                "username": sender.username,
                "sender_profile": dict(profile, photo_lookup_status=photo_status),
                "reasons": reasons,
                "text": event.raw_text or "",
            }
            line = json.dumps(record, ensure_ascii=True)
            LOG.info("%s %s", "DRY RUN" if dry_run else "FLAG", line)
            if not dry_run:
                with Path(config["flags_file"]).open("a", encoding="utf-8") as output:
                    output.write(line + "\n")
        except Exception:
            LOG.exception("Failed to process message %s in chat %s", event.id, event.chat_id)

    try:
        await client.start()
        me = await client.get_me()
        if me.bot:
            raise ValueError("Sign in with a user account so its contacts can be checked")
        await refresh_contacts()
        client.add_event_handler(handle, events.NewMessage(incoming=True))
        LOG.info(
            "Listening for new %s messages; %d contacts; %s. Stop with Ctrl+C.",
            "private and group" if config["include_groups"] else "private",
            len(contacts),
            "dry run (console only)" if dry_run else f"saving flags to {config['flags_file']}",
        )
        await client.run_until_disconnected()
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--dry-run", action="store_true", help="Print flags without saving flags or sender profiles")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = load_config(args.config)
    except (OSError, ValueError, TypeError) as error:
        parser.error(str(error))
    try:
        asyncio.run(watch(config, args.dry_run))
    except KeyboardInterrupt:
        LOG.info("Stopped")
    except Exception as error:
        LOG.error("Stopped: %s", error)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
