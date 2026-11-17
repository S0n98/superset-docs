"""Create the demo dataset, charts and dashboards used by test_access.py.
Idempotent (objects are found by name and updated).

  dataset   demo_branch_data   virtual, Trino VALUES: brcd, area, data_type, amount
  charts    "Demo - rows" (table), "Demo - total amount" (big number)
  dashboards (published, both charts):
            "Demo - 1050 - 41"  granted to role "1050 - 41 - ..."
            "Demo - 1060 - 41"  granted to role "1060 - 41 - ..."

Env: see ../ss_client.py. DEMO_DATABASE (default "Trino").
"""
import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from ss_client import Superset  # noqa: E402

log = logging.getLogger("setup-demo")

DATASET = "demo_branch_data"
ROWS = [
    ("1050", "HN", "KHCN", 100),
    ("1050", "HN", "KHDN", 200),
    ("1070", "HN", "KHCN", 50),
    ("1060", "HCM", "KHCN", 300),
    ("1060", "HCM", "KHDN", 400),
]
SQL = "SELECT * FROM (VALUES\n  " + ",\n  ".join(
    f"('{b}', '{a}', '{t}', {n})" for b, a, t, n in ROWS
) + "\n) AS t(brcd, area, data_type, amount)"
DASHBOARDS = {"Demo - 1050 - 41": "1050 - 41 - ", "Demo - 1060 - 41": "1060 - 41 - "}
TABLE_CHART, TOTAL_CHART = "Demo - rows", "Demo - total amount"


def find(ss, resource, col, value):
    rows = ss.list(resource, [(col, "eq", value)])
    return rows[0] if rows else None


def position(title, charts):
    pos = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": ["ROW-1"], "parents": ["ROOT_ID"]},
        "HEADER_ID": {"type": "HEADER", "id": "HEADER_ID", "meta": {"text": title}},
        "ROW-1": {"type": "ROW", "id": "ROW-1", "children": [], "parents": ["ROOT_ID", "GRID_ID"],
                  "meta": {"background": "BACKGROUND_TRANSPARENT"}},
    }
    for i, (chart_id, name, width) in enumerate(charts, 1):
        key = f"CHART-{i}"
        pos["ROW-1"]["children"].append(key)
        pos[key] = {"type": "CHART", "id": key, "children": [], "parents": ["ROOT_ID", "GRID_ID", "ROW-1"],
                    "meta": {"chartId": chart_id, "sliceName": name, "width": width, "height": 50}}
    return json.dumps(pos)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ss = Superset()
    db = find(ss, "database", "database_name", os.environ.get("DEMO_DATABASE", "Trino"))
    if not db:
        sys.exit("demo database not found")

    ds = find(ss, "dataset", "table_name", DATASET)
    if ds:
        ss.call("PUT", f"/api/v1/dataset/{ds['id']}", {"sql": SQL})
        ss.call("PUT", f"/api/v1/dataset/{ds['id']}/refresh")
    else:
        ds = {"id": ss.call("POST", "/api/v1/dataset/", {"database": db["id"], "table_name": DATASET, "sql": SQL})["id"]}
    ds_id = ds["id"]
    log.info("dataset %s id=%s", DATASET, ds_id)

    roles = ss.list("security/roles")
    dash_ids = {}
    for title, prefix in DASHBOARDS.items():
        match = [r for r in roles if r["name"].startswith(prefix)]
        if len(match) != 1:
            sys.exit(f"expected one role starting with {prefix!r}, found {[r['name'] for r in match]} "
                     f"-- run the dept-role-sync job first")
        body = {"dashboard_title": title, "published": True, "roles": [match[0]["id"]]}
        d = find(ss, "dashboard", "dashboard_title", title)
        if d:
            ss.call("PUT", f"/api/v1/dashboard/{d['id']}", body)
            dash_ids[title] = d["id"]
        else:
            dash_ids[title] = ss.call("POST", "/api/v1/dashboard/", body)["id"]
        log.info("dashboard %r id=%s -> role %r", title, dash_ids[title], match[0]["name"])

    charts = {
        TABLE_CHART: {"viz_type": "table", "query_mode": "raw",
                      "all_columns": ["brcd", "area", "data_type", "amount"],
                      "order_by_cols": [], "row_limit": 1000},
        TOTAL_CHART: {"viz_type": "big_number_total",
                      "metric": {"expressionType": "SIMPLE", "column": {"column_name": "amount"},
                                 "aggregate": "SUM", "label": "SUM(amount)"}},
    }
    chart_ids = {}
    for name, params in charts.items():
        params = {**params, "datasource": f"{ds_id}__table"}
        body = {"slice_name": name, "viz_type": params["viz_type"], "datasource_id": ds_id,
                "datasource_type": "table", "params": json.dumps(params),
                "dashboards": list(dash_ids.values())}
        c = find(ss, "chart", "slice_name", name)
        if c:
            ss.call("PUT", f"/api/v1/chart/{c['id']}", body)
            chart_ids[name] = c["id"]
        else:
            chart_ids[name] = ss.call("POST", "/api/v1/chart/", body)["id"]
        log.info("chart %r id=%s", name, chart_ids[name])

    for title, dash_id in dash_ids.items():
        ss.call("PUT", f"/api/v1/dashboard/{dash_id}", {"position_json": position(title, [
            (chart_ids[TOTAL_CHART], TOTAL_CHART, 4), (chart_ids[TABLE_CHART], TABLE_CHART, 8)])})

    print(json.dumps({"dataset": ds_id, "charts": chart_ids, "dashboards": dash_ids}))


if __name__ == "__main__":
    main()
