"""Agent settings in one place, so evals can switch model or effort later."""

import os

from dotenv import load_dotenv

# Puts .env values (ANTHROPIC_API_KEY, GUARD_URL) into the environment. The
# anthropic SDK reads the key itself; this code never touches it.
load_dotenv()

MODEL = "claude-opus-5-5"
# Opus 5.5 defaults to "medium"; written out so it is a visible, tunable choice.
EFFORT = "medium"
MAX_TOKENS = 16000
# One turn = one model call. Search, maybe a retry, purchase, final reply fits.
MAX_TURNS = 6

GUARD_URL = os.getenv("GUARD_URL", "http://127.0.0.1:8000")
GUARD_TIMEOUT = 30.0  # seconds; the guard waits on PayPal before it answers
