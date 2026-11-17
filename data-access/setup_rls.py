"""Create/update the roles and row-level-security filters for attribute-based
data access. Idempotent: run it after adding an area/type value or a dataset.

For each dimension (e.g. area -> column "area") and each value (HN, HCM):
  role   <role_prefix><value>                    e.g. AREA_HN
  filter "RLS area = HN"   Regular, role AREA_HN, clause  area = 'HN'
plus, per dimension, one deny-by-default filter:
  filter "RLS area: deny by default"  Base, clause 1 = 0,
         excluded roles = unrestricted_roles (Admin, DSP_APPLICATION_ADMIN)

Filters of one dimension share a group key, so Superset ORs them:
  user with AREA_HN          -> (1 = 0 OR area = 'HN')  -> HN rows
  user without any AREA_*    -> (1 = 0)                 -> no rows
  unrestricted role          -> Base filter skipped     -> all rows
Different dimensions have different group keys and are ANDed.

Every filter is attached to the datasets that have the dimension's column
("datasets": "auto"), or to an explicit list of dataset names.

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
    return {c["column_name"] for c in ds.get("columns", [])}


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
        tables = sorted(i for i, cols in columns.items() if dim["column"] in cols)
        if not tables:
            log.warning("no dataset has column %r -- %s filters attach to nothing", dim["column"], dim["name"])
        else:
            log.info("%s: datasets with column %r: %s", dim["name"], dim["column"], [names[i] for i in tables])
        for value in dim["values"]:
            role_id = ensure_role(ss, roles_by_name, f"{dim['role_prefix']}{value}", args.dry_run)
            ensure_filter(ss, existing, {
                "name": f"RLS {dim['name']} = {value}",
                "filter_type": "Regular",
                "tables": tables,
                "roles": [role_id] if role_id else [],
                "group_key": dim["group_key"],
                "clause": f"{dim['column']} = {sql_literal(value)}",
                "description": f"Rows where {dim['column']} is {value}, for users with {dim['name']}={value}.",
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
