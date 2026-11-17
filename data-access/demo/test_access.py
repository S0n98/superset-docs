"""End-to-end access tests against the demo objects (run setup_demo.py and
../setup_rls.py first). Logs in as each test user through LDAP and checks:
roles, dashboard visibility, rows returned per dashboard (RLS), access
without dashboard context, and functional-role permissions.

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
ALL = {("1050", "HN", "KHCN"), ("1050", "HN", "KHDN"), ("1070", "HN", "KHCN"),
       ("1060", "HCM", "KHCN"), ("1060", "HCM", "KHDN")}
DENIED = "denied"

# user: (password, expected roles (prefix match for "<brcd> - <deptcd> - "),
#        dashboards visible, {dashboard or None: expected rows | DENIED},
#        can create content (dashboards), can run SQL Lab)
CASES = {
    "alice": (None, {"Admin", "DSP_DATA_VIEWER"},
              {DEPT_A, DEPT_B}, {None: ALL, DEPT_A: ALL}, True, True),
    "henry": (None, {"DSP_APPLICATION_ADMIN", "DSP_DATA_VIEWER"},
              # all_datasource_access: reads every row in any context, but
              # can't open role-granted dashboards (only Admin bypasses DASHBOARD_RBAC)
              set(), {None: ALL, DEPT_A: ALL}, True, True),
    "dave": (None, {"1050 - 41 - ", "AREA_HN", "TYPE_KHCN", "DSP_DATA_ANALYST", "DSP_DATA_VIEWER"},
             {DEPT_A}, {DEPT_A: {("1050", "HN", "KHCN"), ("1070", "HN", "KHCN")},
                        DEPT_B: DENIED, None: DENIED}, True, False),
    "carol": (None, {"1050 - 41 - ", "AREA_HN", "TYPE_KHDN", "DSP_DATA_VIEWER"},
              {DEPT_A}, {DEPT_A: {("1050", "HN", "KHDN")}, None: DENIED}, False, False),
    "bob": (None, {"1060 - 41 - ", "AREA_HCM", "TYPE_KHDN", "DSP_DATA_STEWARD", "DSP_DATA_VIEWER"},
            {DEPT_B}, {DEPT_B: {("1060", "HCM", "KHDN")}, DEPT_A: DENIED}, True, False),
    "erin": (None, {"1050 - 41 - ", "DSP_DATA_VIEWER"},
             {DEPT_A}, {DEPT_A: set()}, False, False),
    "gina": (None, {"1050 - 41 - ", "AREA_HN", "DSP_DATA_ANALYST", "DSP_DATA_VIEWER"},
             {DEPT_A}, {DEPT_A: set()}, True, False),
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


def chart_rows(ss, ids, dashboard):
    form_data = {"viz_type": "table", "datasource": f"{ids['dataset']}__table"}
    if dashboard:
        form_data.update(slice_id=ids["charts"]["Demo - rows"], dashboardId=ids["dashboards"][dashboard])
    body = {
        "datasource": {"id": ids["dataset"], "type": "table"}, "force": True,
        "queries": [{"columns": ["brcd", "area", "data_type", "amount"], "metrics": [],
                     "orderby": [], "row_limit": 1000}],
        "form_data": form_data, "result_format": "json", "result_type": "full",
    }
    try:
        data = ss.call("POST", "/api/v1/chart/data", body)["result"][0]["data"]
    except SupersetError as e:
        if e.status in (401, 403, 404):
            return DENIED
        raise
    return {(r["brcd"], r["area"], r["data_type"]) for r in data}


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
    ids = {"dataset": admin.list("dataset", [("table_name", "eq", "demo_branch_data")])[0]["id"],
           "charts": {c["slice_name"]: c["id"] for c in admin.list("chart")},
           "dashboards": {d["dashboard_title"]: d["id"] for d in admin.list("dashboard")}}
    database_id = admin.list("database")[0]["id"]

    only = set(sys.argv[1:])
    for user, (pw, roles, dashboards, data, chart_ok, sql_ok) in CASES.items():
        if only and user not in only:
            continue
        ss = Superset(username=user, password=pw or PW)
        # roles as stored after this login (/me/roles needs a browser session, so ask as admin)
        actual_roles = [r["name"] for r in admin.list("security/users", [("username", "eq", user)])[0]["roles"]]
        check(user, "roles", roles_match(actual_roles, roles), f"got {sorted(actual_roles)}")

        visible = {d["dashboard_title"] for d in ss.list("dashboard")} & {DEPT_A, DEPT_B}
        check(user, f"dashboards visible {sorted(dashboards)}", visible == dashboards, f"got {sorted(visible)}")

        for dash, expected in data.items():
            got = chart_rows(ss, ids, dash)
            where = f"via {dash!r}" if dash else "without dashboard"
            label = expected if expected == DENIED else f"{len(expected)} rows"
            check(user, f"data {where}: {label}", got == expected,
                  f"got {got if got == DENIED else sorted(got)}")

        check(user, f"create content: {chart_ok}", can_create_content(ss) == chart_ok)
        check(user, f"SQL Lab: {sql_ok}", can_run_sql(ss, database_id) == sql_ok)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
