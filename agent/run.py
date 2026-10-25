"""CLI entry point.

    python -m agent.run "running shoes under $100, size 10"

Needs the guard running (uvicorn guard.main:app) and ANTHROPIC_API_KEY in .env.
"""

import sys

from agent.guard_client import post_purchase
from agent.llm import make_client
from agent.loop import run_agent


def main():
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        print('Usage: python -m agent.run "<what you want to buy>"')
        return 2

    result = run_agent(sys.argv[1], make_client(), post_purchase)

    print()
    if result.final_text:
        print(f"Agent: {result.final_text}")
    print(f"Status: {result.status}")
    if result.purchase:
        p = result.purchase
        print(f"Product: {p['product_id']}")
        print(f"Decision: {p['decision']} ({p['reason']})")
        print(f"Request ID: {p['request_id']}")
        print(f"Approval link: {p['approval_url'] or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
