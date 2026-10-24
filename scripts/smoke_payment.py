"""Smoke test for the PayPal sandbox money path (DESIGN.md lifecycle steps 5-7).

Run from the repo root:  python -m scripts.smoke_payment

Creates an order, checks PayPal-Request-Id idempotency, waits for you to
approve as the sandbox buyer, then captures. Prints only order IDs,
statuses and the approval URL, never credentials or tokens.
"""

import sys
import uuid

from guard.paypal_client import PayPalClient, approval_link


def main():
    client = PayPalClient()
    results = {}

    # 1. Create the order.
    request_id = str(uuid.uuid4())
    order = client.create_order("1.00", "USD", request_id, "agent-guard smoke test")
    order_id = order["id"]
    print(f"Created order {order_id} (status {order['status']})")

    # 2. Idempotency: same request ID must return the same order.
    retry = client.create_order("1.00", "USD", request_id, "agent-guard smoke test")
    same_id_ok = retry["id"] == order_id
    print(f"Retry with same request ID -> order {retry['id']} "
          f"({'same' if same_id_ok else 'DIFFERENT'})")

    # Control: a new request ID must create a different order.
    control = client.create_order("1.00", "USD", str(uuid.uuid4()), "agent-guard smoke control")
    new_id_ok = control["id"] != order_id
    print(f"New request ID           -> order {control['id']} "
          f"({'different' if new_id_ok else 'SAME'})")
    results["idempotency"] = same_id_ok and new_id_ok

    # 3. Human approval on PayPal's page.
    print(f"\nApprove here as the sandbox buyer:\n  {approval_link(order)}\n")
    input("After approving, press Enter to capture... ")
    print(f"Order status before capture: {client.get_order(order_id)['status']}")

    # 4. Capture.
    capture = client.capture_order(order_id, str(uuid.uuid4()))
    payment = capture["purchase_units"][0]["payments"]["captures"][0]
    print(f"Capture {payment['id']}: order {capture['status']}, payment {payment['status']}")
    results["capture"] = capture["status"] == "COMPLETED"

    print()
    for check, ok in results.items():
        print(f"{check}: {'PASS' if ok else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
