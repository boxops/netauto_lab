"""Verify that OPENAI_API_KEY is set and accepted by the OpenAI API."""
from __future__ import annotations

import os
import sys

from openai import AuthenticationError, OpenAI


def main() -> None:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("❌  OPENAI_API_KEY is not set")
        sys.exit(1)

    client = OpenAI(api_key=api_key)
    try:
        models = client.models.list()
        names = [m.id for m in models.data[:3]]
        print(f"✅  OPENAI_API_KEY is valid (sample models: {', '.join(names)})")
    except AuthenticationError as exc:
        print(f"❌  OPENAI_API_KEY is invalid or expired: {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"⚠️  Could not reach OpenAI API: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
