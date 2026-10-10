#!/usr/bin/env python3
"""Submit message text to the OpenRouter Decisions API."""

import argparse
import asyncio
import json

import httpx

from decisions_api import spam_probability


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", required=True, help="Message text to evaluate")
    args = parser.parse_args()
    try:
        probability = asyncio.run(spam_probability(args.text))
    except (httpx.HTTPError, ValueError) as error:
        parser.exit(1, f"Decision failed: {error}\n")
    print(json.dumps({"spam_probability": probability}))


if __name__ == "__main__":
    main()