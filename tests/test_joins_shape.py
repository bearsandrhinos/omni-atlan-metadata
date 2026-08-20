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


# --- alias handling: an unresolvable joins name must NOT silently drop a table ---

def _views(*names):
    return {n: {"name": n, "table_name": f"TBL_{n}", "schema": "S", "catalog": "C"} for n in names}


def test_unresolvable_join_name_falls_back_rather_than_dropping_a_table():
    """`joins` uses a relationship's ALIAS where one exists, which will not match a
    .view we hold. Deriving locally would emit lineage missing that table, so the
    whole topic must fall back to the topic-detail API."""
    c = ClientClass()
    parsed = yaml.safe_load("joins:\n  orders: {}\n  cust_alias: {}\n")
    got = c._topic_detail_from_views(parsed, "orders", _views("orders", "customers"))
    assert got is None, got


def test_all_names_resolvable_derives_locally():
    c = ClientClass()
    parsed = yaml.safe_load("joins:\n  orders:\n    customers: {}\n")
    got = c._topic_detail_from_views(parsed, "orders", _views("orders", "customers"))
    assert got is not None
    # base view first, then joins, each exactly once — matching the topic API shape
    assert [v["viewName"] for v in got["viewSources"]] == ["orders", "customers"]
    assert got["sourceTableName"] == "TBL_orders"


def test_resolved_view_without_table_name_is_skipped_not_a_fallback():
    """A derived view with no physical table is legitimate — skip it, don't fall back."""
    c = ClientClass()
    views = _views("orders")
    views["derived"] = {"name": "derived"}          # resolved, but no table_name
    parsed = yaml.safe_load("joins:\n  orders:\n    derived: {}\n")
    got = c._topic_detail_from_views(parsed, "orders", views)
    assert got is not None, "should derive, not fall back"
    assert [v["viewName"] for v in got["viewSources"]] == ["orders"]
    assert "derived" not in [v["viewName"] for v in got["viewSources"]]


def test_unresolvable_base_view_also_falls_back():
    c = ClientClass()
    parsed = yaml.safe_load("joins:\n  orders: {}\n")
    got = c._topic_detail_from_views(parsed, "aliased_base", _views("orders"))
    assert got is None


# --- alias RESOLUTION from the .relationships file (Omni, 2026-08-19) ---

RELATIONSHIPS = """
- join_from_view: users
  join_from_view_as: buyers
  join_from_view_as_label: Buyers
  join_to_view: user_facts
  join_type: always_left
  on_sql: ${users.id} = ${user_facts.id}
  relationship_type: one_to_one
"""


def test_alias_map_parsed_from_relationships_file():
    got = ClientClass._view_aliases_from_payload({"model.relationships": RELATIONSHIPS})
    assert got == {"buyers": "users"}, got


def test_aliased_join_resolves_locally_instead_of_falling_back():
    """The whole point: an aliased join no longer costs a topic-detail request."""
    c = ClientClass()
    parsed = yaml.safe_load("joins:\n  orders:\n    buyers: {}\n")
    got = c._topic_detail_from_views(
        parsed, "orders", _views("orders", "users"), {"buyers": "users"}
    )
    assert got is not None, "should resolve via alias, not fall back"
    # `users`' table is present, reached through the alias `buyers`
    assert "TBL_users" in [v["tableName"] for v in got["viewSources"]]


def test_alias_pointing_at_a_view_we_lack_still_falls_back():
    c = ClientClass()
    parsed = yaml.safe_load("joins:\n  orders:\n    buyers: {}\n")
    got = c._topic_detail_from_views(
        parsed, "orders", _views("orders"), {"buyers": "users"}
    )
    assert got is None


def test_aliased_base_view_resolves():
    c = ClientClass()
    parsed = yaml.safe_load("joins:\n  buyers: {}\n")
    got = c._topic_detail_from_views(
        parsed, "buyers", _views("users"), {"buyers": "users"}
    )
    assert got is not None
    assert got["sourceTableName"] == "TBL_users"


def test_no_relationships_file_yields_empty_alias_map():
    assert ClientClass._view_aliases_from_payload({"a.view": "name: x"}) == {}


def test_malformed_relationships_file_is_ignored_not_fatal():
    assert ClientClass._view_aliases_from_payload({"m.relationships": ":::not yaml:::"}) == {}
    assert ClientClass._view_aliases_from_payload({"m.relationships": "- join_to_view: only"}) == {}
