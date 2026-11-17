# data-access

Scripts that set up and test the attribute-based access model described in
[../ACCESS-RULES.md](../ACCESS-RULES.md). All are Python 3 stdlib only and
talk to Superset's REST API.

```
data-access/
├── ss_client.py          shared Superset API client (login, CSRF, paging)
├── setup_rls.py          AREA_*/TYPE_* roles + RLS filters (production)
├── rls_config.json       its config: dimensions, values, unrestricted roles
├── roles/dsp_roles.json  the DSP_* functional roles (from the system export)
└── demo/
    ├── setup_demo.py     demo dataset, charts, dashboards
    └── test_access.py    end-to-end checks, logging in as each test user
```

Common environment for all scripts:

| Env | |
|---|---|
| `SUPERSET_URL` | e.g. `http://superset.default.svc.cluster.local:8088` |
| `SUPERSET_USERNAME` / `SUPERSET_PASSWORD` | An Admin account (`test_access.py` uses `admin` / `ADMIN_PASSWORD`) |
| `SUPERSET_AUTH_PROVIDER` | `ldap` (default) or `db` |
| `SUPERSET_HOST_HEADER` | Optional. Send this `Host` header, to reach Superset through an ingress by IP |
| `SUPERSET_INSECURE_SKIP_VERIFY` | Optional, `true` to skip TLS verification |

## 1. Functional roles

Import once (and again after changing them), from inside a Superset pod:

```bash
kubectl cp roles/dsp_roles.json default/<superset-pod>:/tmp/dsp_roles.json -c superset
kubectl exec -n default <superset-pod> -c superset -- superset fab import-roles --path /tmp/dsp_roles.json
```

`dsp_roles.json` holds the `DSP_*` roles from the production export
(`superset fab export-roles`), without `DSP_DATA_APPROVE`. Import before
users log in: `DSP_DATA_VIEWER` is the default role
(`AUTH_USER_REGISTRATION_ROLE`).

## 2. RLS: `setup_rls.py`

```bash
python3 setup_rls.py --dry-run   # show what would change
python3 setup_rls.py
```

For each dimension in `rls_config.json` and each of its values, it ensures:

- the role `<role_prefix><value>` (e.g. `AREA_HN`);
- a Regular filter `<column> = '<value>'` for that role;
- one Base `1 = 0` deny-by-default filter per dimension, excluding
  `unrestricted_roles`.

All filters of a dimension share its group key. With `"datasets": "auto"`,
filters attach to every dataset that has the dimension's column.

It's idempotent: on a re-run, unchanged filters log `ok`. **Re-run it
whenever a dataset is added**, or the new dataset is unfiltered.

Adding a value, e.g. area `DN`:

1. Add `"DN"` to the `area` values in `rls_config.json` and run the
   script.
2. Add `"area:DN": ["AREA_DN"]` to `AUTH_ROLES_MAPPING` in the Superset
   Helm values and roll out.

Until both are done, users with `area: DN` see no rows (fail closed).

## 3. Demo and tests

```bash
python3 demo/setup_demo.py       # needs the dept-role-sync job to have run
python3 setup_rls.py             # attach filters to the new demo dataset
python3 demo/test_access.py      # all users; or: test_access.py dave bob
```

`setup_demo.py` (idempotent) creates:

- **dataset** `demo_branch_data`: virtual, over Trino `VALUES`, so there's
  no table to load:

  | brcd | area | data_type | amount |
  |---|---|---|---|
  | 1050 | HN | KHCN | 100 |
  | 1050 | HN | KHDN | 200 |
  | 1070 | HN | KHCN | 50 |
  | 1060 | HCM | KHCN | 300 |
  | 1060 | HCM | KHDN | 400 |

- **charts** `Demo - rows` (table) and `Demo - total amount` (big number);
- **dashboards**, both published, each with both charts:
  - `Demo - 1050 - 41`, granted to `1050 - 41 - …`
  - `Demo - 1060 - 41`, granted to `1060 - 41 - …`

`test_access.py` logs in through LDAP as each test user (password
`Passw0rd!`, or `TEST_PASSWORD`) and checks:

- the roles they get;
- which demo dashboards they can list;
- the rows returned through each dashboard (or a denial);
- the rows returned with no dashboard context;
- whether they can create content (a dashboard);
- whether they can run SQL Lab.

The test users' directory entries are listed in the README's "Result"
table.

### Result (2026-11-17)

```
PASS  alice  roles
PASS  alice  dashboards visible ['Demo - 1050 - 41', 'Demo - 1060 - 41']
PASS  alice  data without dashboard: 5 rows
PASS  alice  data via 'Demo - 1050 - 41': 5 rows
PASS  alice  create content: True
PASS  alice  SQL Lab: True
PASS  henry  roles
PASS  henry  dashboards visible []
PASS  henry  data without dashboard: 5 rows
PASS  henry  data via 'Demo - 1050 - 41': 5 rows
PASS  henry  create content: True
PASS  henry  SQL Lab: True
PASS  dave   roles
PASS  dave   dashboards visible ['Demo - 1050 - 41']
PASS  dave   data via 'Demo - 1050 - 41': 2 rows
PASS  dave   data via 'Demo - 1060 - 41': denied
PASS  dave   data without dashboard: denied
PASS  dave   create content: True
PASS  dave   SQL Lab: False
PASS  carol  roles
PASS  carol  dashboards visible ['Demo - 1050 - 41']
PASS  carol  data via 'Demo - 1050 - 41': 1 rows
PASS  carol  data without dashboard: denied
PASS  carol  create content: False
PASS  carol  SQL Lab: False
PASS  bob    roles
PASS  bob    dashboards visible ['Demo - 1060 - 41']
PASS  bob    data via 'Demo - 1060 - 41': 1 rows
PASS  bob    data via 'Demo - 1050 - 41': denied
PASS  bob    create content: True
PASS  bob    SQL Lab: False
PASS  erin   roles
PASS  erin   dashboards visible ['Demo - 1050 - 41']
PASS  erin   data via 'Demo - 1050 - 41': 0 rows
PASS  erin   create content: False
PASS  erin   SQL Lab: False
PASS  gina   roles
PASS  gina   dashboards visible ['Demo - 1050 - 41']
PASS  gina   data via 'Demo - 1050 - 41': 0 rows
PASS  gina   create content: True
PASS  gina   SQL Lab: False

41/41 checks passed
```

Also verified: a department rename through the real CronJob kept the role
id (134), and `dave` kept access to his dashboard after logging in again.

The data checks call `/api/v1/chart/data` with the chart's `slice_id` and
`dashboardId` in `form_data`, the same as a chart rendered inside the
dashboard, so the dashboard-based access path (`DASHBOARD_RBAC`) is
exercised.
