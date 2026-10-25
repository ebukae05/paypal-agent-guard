"""The agent's two tools: their schemas, and the code that runs them.

The rules that keep the agent safe live here, in code, not in the prompt:
- user_request sent to the guard is always the CLI text, never model text
- the model picks only a product_id; name, size, price and merchant come
  from the catalog
- one request_id per run, locked to the first product the model asks for
"""

import json
import uuid

from agent.guard_client import GuardError
from catalog.search import load_products, search_catalog

# strict: true makes the API guarantee tool inputs match these schemas.
# Optional parameters are listed in properties but left out of "required".
TOOLS = [
    {
        "name": "search_catalog",
        "description": (
            "Search the store catalog by keywords. Returns matching products, "
            "best match first. Optionally filter by maximum price (inclusive, USD) "
            "and by size."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords, e.g. 'running shoes'"},
                "max_price": {"type": "number", "description": "Maximum price in USD, e.g. 100"},
                "size": {"type": "string", "description": "Size, e.g. '10' or 'M'"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "request_purchase",
        "description": (
            "Ask the purchase guard to buy one product by its product_id. The guard "
            "checks the purchase and returns a decision (confirm, warn or block) and, "
            "unless blocked, a PayPal approval link for the user. Call this once."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string", "description": "A product_id from search results"},
            },
            "required": ["product_id"],
            "additionalProperties": False,
        },
    },
]

# Fields of a catalog product the guard's Item model accepts.
ITEM_FIELDS = ("product_id", "name", "size", "price", "merchant")


class ToolError(Exception):
    """A tool failed in a way the model should see and can react to."""


class ToolBox:
    """Runs tools for one agent run and holds that run's purchase state."""

    def __init__(self, user_request, guard_post):
        self.user_request = user_request  # the CLI text, fixed for the whole run
        self.guard_post = guard_post
        # One request_id per run. Retrying the same product resends it, and
        # the guard deduplicates, so a retry can never create a second order.
        self.request_id = str(uuid.uuid4())
        self.locked_product_id = None
        self.purchase = None  # the guard's decision, once it gives one

    def run(self, name, tool_input):
        """Run one tool. Returns a JSON string; raises ToolError on failure."""
        if name == "search_catalog":
            return self.search_catalog(**tool_input)
        if name == "request_purchase":
            return self.request_purchase(**tool_input)
        raise ToolError(f"Unknown tool {name!r}")

    def search_catalog(self, query, max_price=None, size=None):
        try:
            products = search_catalog(query, max_price=max_price, size=size)
        except ValueError as e:
            raise ToolError(str(e))
        return json.dumps(products)

    def request_purchase(self, product_id):
        if self.purchase is not None:
            raise ToolError("A purchase was already requested in this run. Only one is allowed.")

        product = next((p for p in load_products() if p["product_id"] == product_id), None)
        if product is None:
            raise ToolError(f"Unknown product_id {product_id!r}. Use a product_id from search results.")

        if self.locked_product_id is None:
            self.locked_product_id = product_id
        elif product_id != self.locked_product_id:
            raise ToolError(
                f"This run is locked to {self.locked_product_id}. "
                "You can retry that product, but not choose a different one."
            )

        payload = {
            "request_id": self.request_id,
            "user_request": self.user_request,
            "item": {field: product[field] for field in ITEM_FIELDS},
        }
        try:
            result = self.guard_post(payload)
        except GuardError as e:
            # The failure goes back to the model; the lock and request_id stay.
            raise ToolError(str(e))

        self.purchase = {
            "request_id": result["request_id"],
            "product_id": product_id,
            "decision": result["decision"],
            "reason": result["reason"],
            "approval_url": result.get("approval_url"),
        }
        return json.dumps(self.purchase)
