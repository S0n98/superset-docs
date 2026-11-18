"""Create/update the roles and row-level-security filters for attribute-based
data access. Idempotent: run it after adding an area/type value or a dataset.

For each "values" dimension (e.g. type -> column "CUSTTPCD") and each value
(KHCN, KHDN, ALL):
  role   <role_prefix><value>                    e.g. RLS_TYPE_KHCN
  filter "RLS type = KHCN"  Regular, role RLS_TYPE_KHCN,
         clause CUSTTPCD IN (100, 300, 570)
plus, per dimension, one deny-by-default filter:
  filter "RLS type: deny by default"  Base, clause 1 = 0,
         excluded roles = unrestricted_roles (Admin, DSP_APPLICATION_ADMIN)

Filters of one dimension share a group key, so Superset ORs them:
  user with RLS_TYPE_KHCN    -> (1 = 0 OR CUSTTPCD IN (100, 300, 570)) -> KHCN rows
  user without RLS_TYPE_*    -> (1 = 0)                                -> no rows
  unrestricted role          -> Base filter skipped                    -> all rows
Different dimensions have different group keys and are ANDed.

Every filter is attached to the datasets that have the dimension's column
("datasets": "auto"), or to an explicit list of dataset names. A dimension
with "unless_columns" skips datasets that have any of those columns: brcd
filters only datasets without an area column, since area already scopes
the others (a branch belongs to one area).

A dimension with a "clause" (area, brcd) gets just one Base filter with
that clause instead of the per-value filters above: a Jinja template that
Superset renders per user from their roles (see JINJA_CONTEXT_ADDONS in the
Superset values; needs ENABLE_TEMPLATE_PROCESSING):
  {{ user_role_filter('area', 'AREA_') }} -> area IN ('HN')   from AREA_HN
  {{ user_brcd_filter('brcd') }}          -> brcd IN ('1050') from "1050 - 41 - ..."
or 1 = 0 when the user has no such role. Unrestricted roles are excluded.
Its "values", if any, still get their <role_prefix><value> roles.

"values" can map each value to a code list (type: KHCN -> CUSTTPCD IN
(100, 300, 570)). "all_values" (type: ["ALL"]) get the union of every list:
RLS_TYPE_ALL sees KHCN and KHDN codes, but not codes in neither list.

Usage: python setup_rls.py [--config rls_config.json] [--dry-run]
Env:   see ss_client.py (SUPERSET_URL, SUPERSET_USERNAME, ...)
"""
import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ss_client import Superset  # noqa: E402

log = logging.getLogger("setup-rls")


def sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def dataset_columns(ss, dataset_id):
    ds = ss.call("GET", f"/api/v1/dataset/{dataset_id}")["result"]
    return {c["column_name"].lower() for c in ds.get("columns", [])}


def value_clauses(dim):
    """{value: clause} for a "values" dimension.
    values as a list:  ["HN"]               -> {"HN": "area = 'HN'"}
    values as a dict:  {"KHCN": [100, 300]} -> {"KHCN": "CUSTTPCD IN (100, 300)"}
    all_values:        ["ALL"]              -> {"ALL": "CUSTTPCD IN (<every code above>)"}
    Codes are written as given: numbers unquoted, strings quoted."""
    col, values = dim["column"], dim["values"]
    def in_list(codes):
        return f"{col} IN ({', '.join(str(c) if isinstance(c, (int, float)) else sql_literal(c) for c in codes)})"
    if isinstance(values, dict):
        clauses = {v: in_list(codes) for v, codes in values.items()}
        union = sorted({c for codes in values.values() for c in codes}, key=lambda c: (str(type(c)), c))
    else:
        clauses = {v: f"{col} = {sql_literal(v)}" for v in values}
        union = list(values)
    for v in dim.get("all_values", []):
        clauses[v] = in_list(union)
    return clauses


def ensure_role(ss, roles_by_name, name, dry_run):
    if name in roles_by_name:
        return roles_by_name[name]["id"]
    log.info("create role %s", name)
    if dry_run:
        return None
    ss.call("POST", "/api/v1/security/roles/", {"name": name})
    roles_by_name.update({r["name"]: r for r in ss.list("security/roles")})
    return roles_by_name[name]["id"]


def ensure_filter(ss, existing, desired, dry_run):
    cur = existing.get(desired["name"])
    if cur is None:
        log.info("create filter %-32s tables=%s roles=%s", desired["name"], desired["tables"], desired["roles"])
        if not dry_run:
            ss.call("POST", "/api/v1/rowlevelsecurity/", desired)
        return
    same = (
        cur["filter_type"] == desired["filter_type"]
        and cur["clause"] == desired["clause"]
        and (cur.get("group_key") or "") == desired["group_key"]
        and sorted(t["id"] for t in cur["tables"]) == sorted(desired["tables"])
        and sorted(r["id"] for r in cur["roles"]) == sorted(desired["roles"])
    )
    if same:
        log.info("ok     filter %s", desired["name"])
        return
    log.info("update filter %-32s tables=%s roles=%s", desired["name"], desired["tables"], desired["roles"])
    if not dry_run:
        ss.call("PUT", f"/api/v1/rowlevelsecurity/{cur['id']}", desired)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=os.path.join(here, "rls_config.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = json.load(open(args.config, encoding="utf-8"))

    ss = Superset()
    roles_by_name = {r["name"]: r for r in ss.list("security/roles")}
    missing = [r for r in cfg["unrestricted_roles"] if r not in roles_by_name]
    if missing:
        sys.exit(f"unrestricted roles not found in Superset: {missing}")
    unrestricted_ids = [roles_by_name[r]["id"] for r in cfg["unrestricted_roles"]]

    datasets = ss.list("dataset")
    if cfg["datasets"] != "auto":
        wanted = set(cfg["datasets"])
        datasets = [d for d in datasets if d["table_name"] in wanted]
        unknown = wanted - {d["table_name"] for d in datasets}
        if unknown:
            sys.exit(f"datasets not found: {sorted(unknown)}")
    columns = {d["id"]: dataset_columns(ss, d["id"]) for d in datasets}
    names = {d["id"]: d["table_name"] for d in datasets}

    existing = {f["name"]: f for f in ss.list("rowlevelsecurity")}
    for dim in cfg["dimensions"]:
        skip = {c.lower() for c in dim.get("unless_columns", [])}
        tables = sorted(i for i, cols in columns.items() if dim["column"].lower() in cols and not cols & skip)
        if not tables:
            log.warning("no dataset has column %r -- %s filters attach to nothing", dim["column"], dim["name"])
        else:
            log.info("%s: datasets with column %r%s: %s", dim["name"], dim["column"],
                     f" and none of {sorted(skip)}" if skip else "", [names[i] for i in tables])
        if "clause" in dim:
            for value in dim.get("values", []):
                ensure_role(ss, roles_by_name, f"{dim['role_prefix']}{value}", args.dry_run)
            ensure_filter(ss, existing, {
                "name": f"RLS {dim['name']}: per user",
                "filter_type": "Base",
                "tables": tables,
                "roles": unrestricted_ids,
                "group_key": dim["group_key"],
                "clause": dim["clause"],
                "description": f"Rendered per user (Jinja); everyone except {cfg['unrestricted_roles']}.",
            }, args.dry_run)
            continue
        clauses = value_clauses(dim)
        for value, clause in clauses.items():
            role_id = ensure_role(ss, roles_by_name, f"{dim['role_prefix']}{value}", args.dry_run)
            ensure_filter(ss, existing, {
                "name": f"RLS {dim['name']} = {value}",
                "filter_type": "Regular",
                "tables": tables,
                "roles": [role_id] if role_id else [],
                "group_key": dim["group_key"],
                "clause": clause,
                "description": f"For users with {dim['name']}={value}.",
            }, args.dry_run)
        ensure_filter(ss, existing, {
            "name": f"RLS {dim['name']}: deny by default",
            "filter_type": "Base",
            "tables": tables,
            "roles": unrestricted_ids,
            "group_key": dim["group_key"],
            "clause": "1 = 0",
            "description": f"No rows unless the user has an {dim['role_prefix']}* role "
                           f"(ORed with them via group key) or is one of {cfg['unrestricted_roles']}.",
        }, args.dry_run)


if __name__ == "__main__":
    main()
