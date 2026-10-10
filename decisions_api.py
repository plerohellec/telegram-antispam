"""Evaluate Telegram message text with the OpenRouter Decisions API."""

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv


SPAM_QUESTION = {
    "type": "noul",
    "instructions": (
        "Is this Telegram private message unsolicited spam, a scam, or a fake attempt to start a conversation? "
        "Generic greetings from strangers, suspicious invitations, and artificial attempts to establish familiarity "
        "are spam indicators. Genuine personal or professional inquiries are legitimate."
    ),
}


async def spam_probability(text):
    """Return the spam probability, raising on API errors or unusable answers."""
    load_dotenv(Path(__file__).with_name(".env"))
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key or not api_key.strip():
        raise ValueError("Set OPENROUTER_API_KEY in .env or the environment")
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://openrouter.ai/api/alpha/decisions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "openai/gpt-6-luna-decisions",
                "state": text,
                "questions": {"spam": SPAM_QUESTION},
            },
        )
        response.raise_for_status()
        decision = response.json()
    answers = decision.get("answers") if isinstance(decision, dict) else None
    answer = answers.get("spam") if isinstance(answers, dict) else None
    if not isinstance(answer, dict):
        raise ValueError("Decisions API returned no spam answer")
    probability = answer.get("noul")
    if (
        answer.get("type") == "noul"
        and type(probability) in (int, float)
        and 0 <= probability <= 1
    ):
        return float(probability)
    raise ValueError("Decisions API refused or returned an invalid spam probability")