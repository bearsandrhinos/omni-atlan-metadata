import yaml
from app.client import ClientClass

# Omni's confirmed shape (Peter, 2026-08-19)
NESTED = yaml.safe_load("""
joins:
  inventory_items:
    products:
      distribution_centers: {}
  users: {}
""")

def test_nested_joins_collects_every_depth():
    got = ClientClass._joined_view_names(NESTED)
    assert got == ["inventory_items", "products", "distribution_centers", "users"], got

def test_no_joins_key_is_empty_not_none():
    assert ClientClass._joined_view_names({"base_view": "orders"}) == []

def test_empty_joins_is_empty():
    assert ClientClass._joined_view_names({"joins": {}}) == []
    assert ClientClass._joined_view_names({"joins": None}) == []

def test_list_of_names_accepted():
    assert ClientClass._joined_view_names({"joins": ["a", "b"]}) == ["a", "b"]

def test_unrecognised_shape_returns_none_so_caller_falls_back():
    assert ClientClass._joined_view_names({"joins": 42}) is None
    assert ClientClass._joined_view_names({"joins": {"a": 7}}) is None

def test_duplicate_view_across_branches_appears_once():
    d = yaml.safe_load("joins:\n  a:\n    shared: {}\n  b:\n    shared: {}\n")
    assert ClientClass._joined_view_names(d) == ["a", "shared", "b"]
