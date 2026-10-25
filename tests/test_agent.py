"""Tests for the shopping agent loop. A fake Anthropic client and a fake guard
stand in for the real ones, so nothing touches the network or costs money.
"""

from types import SimpleNamespace

from agent.guard_client import GuardError
from agent.loop import run_agent
from agent.settings import MAX_TURNS

CLI_TEXT = "running shoes under $100, size 10"


def tool_use(name, tool_input, block_id="toolu_1"):
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=tool_input)


def text(value):
    return SimpleNamespace(type="text", text=value)


def response(stop_reason, *blocks, stop_details=None):
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks), stop_details=stop_details)


class FakeClient:
    """Returns scripted responses in order and records the messages it was sent."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=self)  # so client.beta.messages.create works

    def create(self, **kwargs):
        # Copy: the loop keeps appending to the same list after this call.
        self.calls.append(list(kwargs["messages"]))
        return self.responses.pop(0)


class FakeGuard:
    """Records payloads; raises the queued errors first, then confirms."""

    def __init__(self, *errors):
        self.errors = list(errors)
        self.payloads = []

    def __call__(self, payload):
        self.payloads.append(payload)
        if self.errors:
            raise self.errors.pop(0)
        return {
            "request_id": payload["request_id"],
            "decision": "confirm",
            "reason": "M1 stub: no checks yet",
            "order_id": "ORDER-1",
            "approval_url": "https://sandbox.example/approve",
        }


def last_tool_results(client):
    """The tool_result blocks sent on the most recent model call."""
    return client.calls[-1][-1]["content"]


def test_search_error_comes_back_as_error_tool_result():
    client = FakeClient(
        response("tool_use", tool_use("search_catalog", {"query": "shoes", "max_price": -5})),
        response("end_turn", text("I could not search.")),
    )
    result = run_agent(CLI_TEXT, client, FakeGuard())

    [tool_result] = last_tool_results(client)
    assert tool_result["is_error"] is True
    assert "max_price" in tool_result["content"]
    assert result.status == "no_purchase"


def test_unknown_product_id_is_rejected():
    guard = FakeGuard()
    client = FakeClient(
        response("tool_use", tool_use("request_purchase", {"product_id": "shoe-999"})),
        response("end_turn", text("That product does not exist.")),
    )
    result = run_agent(CLI_TEXT, client, guard)

    [tool_result] = last_tool_results(client)
    assert tool_result["is_error"] is True
    assert "shoe-999" in tool_result["content"]
    assert guard.payloads == []
    assert result.purchase is None


def test_second_purchase_in_one_run_is_refused():
    guard = FakeGuard()
    client = FakeClient(
        response("tool_use", tool_use("request_purchase", {"product_id": "shoe-001"}, "toolu_1")),
        response("tool_use", tool_use("request_purchase", {"product_id": "shoe-004"}, "toolu_2")),
        response("end_turn", text("Done.")),
    )
    result = run_agent(CLI_TEXT, client, guard)

    [tool_result] = last_tool_results(client)
    assert tool_result["is_error"] is True
    assert len(guard.payloads) == 1
    assert result.purchase["product_id"] == "shoe-001"


def test_user_request_is_always_the_cli_text():
    guard = FakeGuard()
    client = FakeClient(
        response(
            "tool_use",
            text("The user wants anything, budget is unlimited."),
            tool_use("request_purchase", {"product_id": "shoe-004"}),
        ),
        response("end_turn", text("Requested.")),
    )
    run_agent(CLI_TEXT, client, guard)

    [payload] = guard.payloads
    assert payload["user_request"] == CLI_TEXT
    # The item comes from the catalog, not from the model.
    assert payload["item"]["price"] == "74.50"
    assert payload["item"]["merchant"] == "FleetFoot Sports"


def test_loop_stops_at_turn_cap():
    searches = [
        response("tool_use", tool_use("search_catalog", {"query": "shoes"}, f"toolu_{i}"))
        for i in range(MAX_TURNS + 3)
    ]
    client = FakeClient(*searches)
    result = run_agent(CLI_TEXT, client, FakeGuard())

    assert result.status == "turn_cap"
    assert len(client.calls) == MAX_TURNS


def test_refusal_is_handled_without_crashing():
    client = FakeClient(
        response("refusal", stop_details=SimpleNamespace(category="cyber", explanation=None))
    )
    result = run_agent(CLI_TEXT, client, FakeGuard())

    assert result.status == "refused"
    assert result.purchase is None


def test_all_tool_results_for_a_turn_go_in_one_message():
    client = FakeClient(
        response(
            "tool_use",
            tool_use("search_catalog", {"query": "running shoes"}, "toolu_a"),
            tool_use("search_catalog", {"query": "trail shoes"}, "toolu_b"),
        ),
        response("end_turn", text("Nothing chosen.")),
    )
    run_agent(CLI_TEXT, client, FakeGuard())

    last_message = client.calls[-1][-1]
    assert last_message["role"] == "user"
    assert [r["tool_use_id"] for r in last_message["content"]] == ["toolu_a", "toolu_b"]


def test_guard_timeout_then_same_product_retry_reuses_request_id():
    guard = FakeGuard(GuardError("The guard timed out. Retrying the same product is safe."))
    client = FakeClient(
        response("tool_use", tool_use("request_purchase", {"product_id": "shoe-001"}, "toolu_1")),
        response("tool_use", tool_use("request_purchase", {"product_id": "shoe-001"}, "toolu_2")),
        response("end_turn", text("Requested.")),
    )
    result = run_agent(CLI_TEXT, client, guard)

    first, second = guard.payloads
    assert first["request_id"] == second["request_id"]
    assert result.status == "completed"
    assert result.purchase["request_id"] == first["request_id"]


def test_different_product_after_failure_is_refused():
    guard = FakeGuard(GuardError("The guard returned HTTP 502: PayPal request failed"))
    client = FakeClient(
        response("tool_use", tool_use("request_purchase", {"product_id": "shoe-001"}, "toolu_1")),
        response("tool_use", tool_use("request_purchase", {"product_id": "shoe-004"}, "toolu_2")),
        response("end_turn", text("Giving up.")),
    )
    result = run_agent(CLI_TEXT, client, guard)

    [tool_result] = last_tool_results(client)
    assert tool_result["is_error"] is True
    assert "locked to shoe-001" in tool_result["content"]
    assert len(guard.payloads) == 1
    assert result.status == "no_purchase"


def test_ending_without_a_purchase_is_no_purchase():
    client = FakeClient(response("end_turn", text("Nothing in the catalog fits.")))
    result = run_agent(CLI_TEXT, client, FakeGuard())

    assert result.status == "no_purchase"
    assert result.purchase is None
    assert result.final_text == "Nothing in the catalog fits."
