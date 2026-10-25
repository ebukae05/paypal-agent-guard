"""Read-only search over the sandbox catalog: the agent's search_catalog tool.

Plain keyword matching, no AI. Search is deliberately loose ("running shoes"
also returns trail shoes) so the agent can pick the wrong item; catching that
is the guard's job, not the catalog's (DESIGN.md, Risks and failures).
"""

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

CATALOG_PATH = Path(__file__).parent / "products.json"


def load_products():
    # Read fresh on every call, so a caller editing a result can never change
    # what the next search returns.
    with CATALOG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def words(text):
    """Lowercase words and numbers: "Running Shoe (3-pack)" -> ["running", "shoe", "3", "pack"]."""
    return re.findall(r"[a-z0-9]+", text.lower())


def score(query_words, product):
    """How many query words appear in the product's name, category or description."""
    product_words = set(words(f"{product['name']} {product['category']} {product['description']}"))
    matched = 0
    for word in query_words:
        # Crude plural handling: "shoes" also matches "shoe".
        if word in product_words or (word.endswith("s") and word[:-1] in product_words):
            matched += 1
    return matched


def parse_max_price(value):
    """Turn what a model might send ("$100", "1,000", 99.99) into a Decimal.

    The arguments come from an LLM, so they are untrusted. Anything that is
    still not a price raises ValueError with a message the model can act on,
    instead of a stack trace it cannot.
    """
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().replace("$", "").replace(",", "")
    try:
        price = Decimal(text)
    except InvalidOperation:
        price = None
    # Decimal accepts "NaN" and "Infinity", which would break the comparison.
    if price is None or not price.is_finite() or price < 0:
        raise ValueError(f"max_price must be a number like 100 or 99.99, got {value!r}")
    return price


def normalize_size(value):
    """Make "10", 10, "10.0", "size 10" and " 10 " all equal, and "m" equal "M"."""
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().lower().removeprefix("size").strip()
    try:
        number = Decimal(text)
    except InvalidOperation:
        return text  # Letter sizes like "m" or "xl".
    if number == number.to_integral_value():
        return str(int(number))  # "10.0" -> "10"
    return str(number.normalize())  # "10.50" -> "10.5"


def search_catalog(query, max_price=None, size=None):
    """Return products matching any query word, best match first.

    max_price is inclusive and compared as Decimal, never float.
    size is compared after normalizing both sides; products without a size
    are excluded when one is given. Blank or None filters mean "no filter".
    Raises ValueError (with a fix-it message) when max_price is not a price.
    """
    query_words = words(query or "")
    limit = parse_max_price(max_price)
    wanted_size = normalize_size(size)

    results = []
    for product in load_products():
        if limit is not None and Decimal(product["price"]) > limit:
            continue
        if wanted_size is not None and normalize_size(product["size"]) != wanted_size:
            continue
        matched = score(query_words, product)
        if matched > 0:
            results.append((matched, product))

    # sorted() is stable, so ties keep catalog order.
    results = sorted(results, key=lambda pair: pair[0], reverse=True)
    return [product for _, product in results]
