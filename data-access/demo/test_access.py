"""End-to-end access tests against the demo objects (run setup_demo.py and
../setup_rls.py first). Logs in as each test user through LDAP and checks:
roles, dashboard visibility, rows returned per dashboard (RLS) from both the
area dataset (demo_branch_data) and the area-less one (demo_branch_detail,
filtered by brcd), the per-user Jinja area/branch filters, access without
dashboard context, functional-role permissions, and that the chart-data
cache never serves one user's rows to another.

Env: SUPERSET_URL, SUPERSET_HOST_HEADER (optional), TEST_PASSWORD (default
Passw0rd!), ADMIN_PASSWORD for the admin account (default adminpass).
Exit code 0 only if every check passes.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from ss_client import Superset, SupersetError  # noqa: E402

PW = os.environ.get("TEST_PASSWORD", "Passw0rd!")
DEPT_A, DEPT_B = "Demo - 1050 - 41", "Demo - 1060 - 41"
# demo rows as (brcd, area, custtpcd); KHCN codes 100/300/570, KHDN 200/510,
# 999 in neither list (only Admin and DSP_APPLICATION_ADMIN see it)
ALL = {("1050", "HN", 100), ("1050", "HN", 200), ("1070", "HN", 300),
       ("1060", "HCM", 570), ("1060", "HCM", 510), ("1050", "HN", 999)}
# demo_branch_detail: same rows without area -> (brcd, custtpcd)
BRANCH_ALL = {(b, t) for b, _, t in ALL}
DENIED = "denied"

# user: (password, expected roles (prefix match for "<brcd> - <deptcd> - "),
#        dashboards visible, {dashboard or None: expected rows | DENIED},
#        can create content (dashboards), can run SQL Lab,
#        {dashboard or None: expected demo_branch_detail rows | DENIED})
CASES = {
    "alice": (None, {"Admin", "DSP_DATA_VIEWER"},
              {DEPT_A, DEPT_B}, {None: ALL, DEPT_A: ALL}, True, True,
              {None: BRANCH_ALL, DEPT_A: BRANCH_ALL}),
    "henry": (None, {"DSP_APPLICATION_ADMIN", "DSP_DATA_VIEWER"},
              # all_datasource_access: reads every row in any context, but
              # can't open role-granted dashboards (only Admin bypasses DASHBOARD_RBAC)
              set(), {None: ALL, DEPT_A: ALL}, True, True,
              {None: BRANCH_ALL, DEPT_A: BRANCH_ALL}),
    # area dataset: whole area HN (incl. branch 1070); area-less dataset: own branch only
    "dave": (None, {"1050 - 41 - ", "AREA_HN", "RLS_TYPE_KHCN", "DSP_DATA_ANALYST", "DSP_DATA_VIEWER"},
             {DEPT_A}, {DEPT_A: {("1050", "HN", 100), ("1070", "HN", 300)},
                        DEPT_B: DENIED, None: DENIED}, True, False,
             {DEPT_A: {("1050", 100)}, None: DENIED}),
    "carol": (None, {"1050 - 41 - ", "AREA_HN", "RLS_TYPE_KHDN", "DSP_DATA_VIEWER"},
              {DEPT_A}, {DEPT_A: {("1050", "HN", 200)}, None: DENIED}, False, False,
              {DEPT_A: {("1050", 200)}}),
    "bob": (None, {"1060 - 41 - ", "AREA_HCM", "RLS_TYPE_KHDN", "DSP_DATA_STEWARD", "DSP_DATA_VIEWER"},
            {DEPT_B}, {DEPT_B: {("1060", "HCM", 510)}, DEPT_A: DENIED}, True, False,
            {DEPT_B: {("1060", 510)}}),
    # no area / no type: deny-by-default on every dataset
    "erin": (None, {"1050 - 41 - ", "DSP_DATA_VIEWER"},
             {DEPT_A}, {DEPT_A: set()}, False, False, {DEPT_A: set()}),
    "gina": (None, {"1050 - 41 - ", "AREA_HN", "DSP_DATA_ANALYST", "DSP_DATA_VIEWER"},
             {DEPT_A}, {DEPT_A: set()}, True, False, {DEPT_A: set()}),
    # brcd + type, no area: nothing from the area dataset, own branch from the area-less one
    "ivan": (None, {"1050 - 41 - ", "RLS_TYPE_KHCN", "DSP_DATA_VIEWER"},
             {DEPT_A}, {DEPT_A: set()}, False, False, {DEPT_A: {("1050", 100)}}),
    # same filter set as dave except area/branch: the cache test pairs them
    "jack": (None, {"1060 - 41 - ", "AREA_HCM", "RLS_TYPE_KHCN", "DSP_DATA_VIEWER"},
             {DEPT_B}, {DEPT_B: {("1060", "HCM", 570)}, DEPT_A: DENIED}, False, False,
             {DEPT_B: {("1060", 570)}, DEPT_A: DENIED}),
    # type "all" (lowercase in LDAP): KHCN + KHDN codes, but not 999
    "kim": (None, {"1050 - 41 - ", "AREA_HN", "RLS_TYPE_ALL", "DSP_DATA_VIEWER"},
            {DEPT_A}, {DEPT_A: {("1050", "HN", 100), ("1050", "HN", 200), ("1070", "HN", 300)}},
            False, False, {DEPT_A: {("1050", 100), ("1050", 200)}}),
}


results = []


def check(user, what, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {user:6} {what}" + (f"  -- {detail}" if detail and not ok else ""))


def roles_match(actual, expected):
    actual = set(actual)
    for exp in expected:
        if exp.endswith(" - "):
            hits = [a for a in actual if a.startswith(exp)]
            if len(hits) != 1:
                return False
            actual.discard(hits[0])
        elif exp in actual:
            actual.discard(exp)
        else:
            return False
    return not actual  # nothing unexpected


TABLES = {  # dataset: (chart rendering it, columns returned as the row key)
    "demo_branch_data": ("Demo - rows", ["brcd", "area", "custtpcd"]),
    "demo_branch_detail": ("Demo - branch rows", ["brcd", "custtpcd"]),
}


def chart_query(ss, ids, dashboard, dataset, force=True, result_type="full"):
    """POST /api/v1/chart/data as a chart of `dashboard`; the first result,
    or DENIED."""
    chart, key = TABLES[dataset]
    ds_id = ids["datasets"][dataset]
    form_data = {"viz_type": "table", "datasource": f"{ds_id}__table"}
    if dashboard:
        form_data.update(slice_id=ids["charts"][chart], dashboardId=ids["dashboards"][dashboard])
    body = {
        "datasource": {"id": ds_id, "type": "table"}, "force": force,
        "queries": [{"columns": key + ["amount"], "metrics": [],
                     "orderby": [], "row_limit": 1000}],
        "form_data": form_data, "result_format": "json", "result_type": result_type,
    }
    try:
        return ss.call("POST", "/api/v1/chart/data", body)["result"][0]
    except SupersetError as e:
        if e.status in (401, 403, 404):
            return DENIED
        raise


def rows_of(result, dataset):
    return DENIED if result == DENIED else {tuple(r[c] for c in TABLES[dataset][1]) for r in result["data"]}


def chart_rows(ss, ids, dashboard, dataset="demo_branch_data"):
    return rows_of(chart_query(ss, ids, dashboard, dataset), dataset)


def jinja_filter_tests(ids, sessions):
    """The Jinja area/branch filters render per user, and cached results stay
    per user: dave (1050, HN) and jack (1060, HCM) hold the same RLS filters
    (both Jinja filters plus RLS_TYPE_KHCN), so only the rendered values tell
    their queries apart."""
    detail, data = "demo_branch_detail", "demo_branch_data"
    sql = chart_query(sessions["dave"], ids, DEPT_A, data, result_type="query")["query"]
    check("dave", "area dataset SQL has area IN ('HN'), no brcd",
          "area IN ('HN')" in sql and "brcd IN" not in sql, sql)
    sql = chart_query(sessions["dave"], ids, DEPT_A, detail, result_type="query")["query"]
    check("dave", "area-less dataset SQL has brcd IN ('1050'), no area",
          "brcd IN ('1050')" in sql and "area IN" not in sql, sql)
    # ivan has RLS_TYPE_KHCN but no area: the area filter must render 1 = 0 (0 rows, checked above)
    sql = chart_query(sessions["ivan"], ids, DEPT_A, data, result_type="query")["query"]
    check("ivan", "no AREA_* role: no area IN (...) in SQL", "area IN" not in sql and "1 = 0" in sql, sql)
    sql = chart_query(sessions["kim"], ids, DEPT_A, data, result_type="query")["query"]
    check("kim", "type ALL renders the union of KHCN and KHDN codes",
          "CUSTTPCD IN (100, 200, 300, 510, 520, 530, 540, 550, 560, 570, 600, 700, 800)" in sql, sql)

    for dataset, jack_rows in ((data, {("1060", "HCM", 570)}), (detail, {("1060", 570)})):
        first = chart_query(sessions["dave"], ids, DEPT_A, dataset, force=True)    # fills the cache
        second = chart_query(sessions["jack"], ids, DEPT_B, dataset, force=False)  # same query, other area/branch
        third = chart_query(sessions["jack"], ids, DEPT_B, dataset, force=False)
        name = dataset[12:]
        check("cache", f"{name}: dave and jack get different cache keys",
              first["cache_key"] != second["cache_key"], f"{first['cache_key']} == {second['cache_key']}")
        check("cache", f"{name}: jack is not served dave's cached rows", rows_of(second, dataset) == jack_rows,
              f"got {sorted(rows_of(second, dataset))}")
        check("cache", f"{name}: caching is on (jack's repeat is a cache hit)", third.get("is_cached") is True,
              f"is_cached={third.get('is_cached')}")


def can_create_content(ss):
    """Creating a (empty) dashboard needs can_write Dashboard -- the same
    write permission set as charts, but no dataset access, so the probe can
    be cleaned up by its creator."""
    try:
        did = ss.call("POST", "/api/v1/dashboard/", {"dashboard_title": "test_access probe"})["id"]
    except SupersetError as e:
        if e.status in (401, 403):
            return False
        raise
    ss.call("DELETE", f"/api/v1/dashboard/{did}")
    return True


def can_run_sql(ss, database_id):
    try:
        res = ss.call("POST", "/api/v1/sqllab/execute/", {
            "database_id": database_id, "sql": "SELECT 1 AS x", "runAsync": False, "client_id": os.urandom(5).hex()})
        return res.get("status") == "success"
    except SupersetError as e:
        if e.status in (401, 403):
            return False
        raise


def main():
    admin = Superset(username="admin", password=os.environ.get("ADMIN_PASSWORD", "adminpass"))
    ids = {"datasets": {t: admin.list("dataset", [("table_name", "eq", t)])[0]["id"] for t in TABLES},
           "charts": {c["slice_name"]: c["id"] for c in admin.list("chart")},
           "dashboards": {d["dashboard_title"]: d["id"] for d in admin.list("dashboard")}}
    database_id = admin.list("database")[0]["id"]

    only = set(sys.argv[1:])
    sessions = {}
    for user, (pw, roles, dashboards, data, chart_ok, sql_ok, branch_data) in CASES.items():
        if only and user not in only:
            continue
        ss = sessions[user] = Superset(username=user, password=pw or PW)
        # roles as stored after this login (/me/roles needs a browser session, so ask as admin)
        actual_roles = [r["name"] for r in admin.list("security/users", [("username", "eq", user)])[0]["roles"]]
        check(user, "roles", roles_match(actual_roles, roles), f"got {sorted(actual_roles)}")

        visible = {d["dashboard_title"] for d in ss.list("dashboard")} & {DEPT_A, DEPT_B}
        check(user, f"dashboards visible {sorted(dashboards)}", visible == dashboards, f"got {sorted(visible)}")

        for dataset, expectations in (("demo_branch_data", data), ("demo_branch_detail", branch_data)):
            for dash, expected in expectations.items():
                got = chart_rows(ss, ids, dash, dataset)
                where = f"via {dash!r}" if dash else "without dashboard"
                label = expected if expected == DENIED else f"{len(expected)} rows"
                check(user, f"{dataset[12:]:6} {where}: {label}", got == expected,
                      f"got {got if got == DENIED else sorted(got)}")

        check(user, f"create content: {chart_ok}", can_create_content(ss) == chart_ok)
        check(user, f"SQL Lab: {sql_ok}", can_run_sql(ss, database_id) == sql_ok)

    if {"dave", "jack", "ivan", "kim"} <= set(sessions):
        jinja_filter_tests(ids, sessions)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
