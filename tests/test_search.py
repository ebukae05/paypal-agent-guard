"""Tests for the sandbox catalog and search_catalog."""

import pytest

from catalog.search import load_products, search_catalog
from guard.main import Item


def ids(products):
    return {product["product_id"] for product in products}


def test_running_shoes_under_100_size_10():
    results = search_catalog("running shoes", max_price="100", size="10")
    assert ids(results) == {
        "shoe-001", "shoe-004", "shoe-005", "shoe-008", "shoe-009", "shoe-010",
    }


def test_size_filter_excludes_other_sizes_and_sizeless_items():
    results = search_catalog("running", size="10")
    assert results
    assert all(product["size"] == "10" for product in results)
    assert "gear-001" not in ids(results)


def test_max_price_is_inclusive():
    assert "shoe-010" in ids(search_catalog("tempo", max_price="100.00"))
    assert "shoe-010" not in ids(search_catalog("tempo", max_price="99.99"))


def test_no_matching_words_returns_empty_list():
    assert search_catalog("laptop") == []


def test_search_is_case_insensitive():
    assert search_catalog("RUNNING SHOES") == search_catalog("running shoes")


def test_editing_a_result_does_not_change_the_catalog():
    first = search_catalog("pegasus", size="10")
    first[0]["price"] = "0.01"
    second = search_catalog("pegasus", size="10")
    assert second[0]["price"] == "89.99"


# The inputs below are what an LLM agent is likely to send as tool arguments.

@pytest.mark.parametrize("max_price", ["100", 100, 100.0, "$100", " $100.00 ", "1,000"])
def test_max_price_accepts_common_formats(max_price):
    results = search_catalog("running shoes", max_price=max_price, size="10")
    assert "shoe-001" in ids(results)


@pytest.mark.parametrize("max_price", ["cheap", "about 100", "NaN", "Infinity", "-5"])
def test_invalid_max_price_raises_a_fixable_error(max_price):
    with pytest.raises(ValueError, match="max_price must be a number"):
        search_catalog("running shoes", max_price=max_price)


@pytest.mark.parametrize("size", ["10", 10, "10.0", "size 10", "Size 10", " 10 "])
def test_size_accepts_common_formats(size):
    assert search_catalog("running shoes", size=size) == search_catalog("running shoes", size="10")


def test_letter_sizes_ignore_case():
    assert "apparel-002" in ids(search_catalog("running shorts", size="m"))


@pytest.mark.parametrize("blank", [None, ""])
def test_blank_filters_mean_no_filter(blank):
    assert search_catalog("running shoes", max_price=blank, size=blank) == search_catalog("running shoes")


def test_none_query_returns_empty_list():
    assert search_catalog(None) == []


def test_every_product_is_a_valid_guard_item():
    # Keeps the catalog in sync with the guard's Item model (price, size, ids).
    for product in load_products():
        Item(**product)
