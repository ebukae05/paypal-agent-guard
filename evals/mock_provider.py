"""Stand-in for the intent check so promptfoo runs without API keys.

promptfoo calls call_api() once per test case. It always answers "match",
so it says nothing about quality; it only proves the eval pipeline runs.
Replace it with the real intent check provider once that exists.
"""

import json


def call_api(prompt, options, context):
    verdict = {"verdict": "match", "reason": "mock provider: no LLM called", "confidence": 0.0}
    return {"output": json.dumps(verdict)}
