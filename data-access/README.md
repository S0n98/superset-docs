# data-access

Scripts that set up and test the attribute-based access model described in
[../ACCESS-RULES.md](../ACCESS-RULES.md). All are Python 3 stdlib only and
talk to Superset's REST API.

```
data-access/
├── ss_client.py          shared Superset API client (login, CSRF, paging)
├── setup_rls.py          AREA_*/RLS_TYPE_* roles + RLS filters, branch filter (production)
├── rls_config.json       its config: dimensions, values, unrestricted roles
├── roles/dsp_roles.json  the DSP_* functional roles (from the system export)
└── demo/
    ├── setup_demo.py     demo datasets, charts, dashboards
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

For each `values` dimension without a `clause` (`type`) and each of its
values, it ensures:

- the role `<role_prefix><value>` (e.g. `RLS_TYPE_KHCN`);
- a Regular filter for that role: `<column> IN (<codes>)` when `values`
  maps each value to a code list (as for `type`), else `<column> =
  '<value>'`;
- for each of `all_values` (`ALL`), the role and a Regular filter
  `<column> IN (<union of every code list>)`;
- one Base `1 = 0` deny-by-default filter per dimension, excluding
  `unrestricted_roles`.

All filters of a dimension share its group key. With `"datasets": "auto"`,
filters attach to every dataset that has the dimension's column.

The three dimensions in `rls_config.json`:

| Dimension | Column | Roles | Filter | Datasets |
|---|---|---|---|---|
| `area` | `area` | `AREA_HN`, `AREA_HCM` | Jinja: `{{ user_role_filter('area', 'AREA_') }}` | with `area` |
| `type` | `CUSTTPCD` | `RLS_TYPE_KHCN`, `RLS_TYPE_KHDN`, `RLS_TYPE_ALL` | one Regular per value + Base `1 = 0` | with `CUSTTPCD` (matched case-insensitively) |
| `brcd` | `brcd` | none | Jinja: `{{ user_brcd_filter('brcd') }}` | with `brcd` and **without** `area` (`"unless_columns": ["area"]`) |

A dimension with a `clause` gets, instead of the per-value filters, just
one Base filter `RLS <name>: per user` with that clause, excluding
`unrestricted_roles`; its `values` (if any) still get their roles.
Superset renders the Jinja per user with macros defined in the Superset
values (`JINJA_CONTEXT_ADDONS`):

- `user_role_filter('area', 'AREA_')` → `area IN ('HN')` from the user's
  `AREA_*` roles;
- `user_brcd_filter('brcd')` → `brcd IN ('1050')` from the user's
  department role, so ~1000 branches need no roles or setup;
- either → `1 = 0` if the user has no such role.

They need `ENABLE_TEMPLATE_PROCESSING` and the security manager's
`get_rls_cache_key` override; see ACCESS-RULES R13b. The script never
deletes, so after switching a dimension to a `clause`, delete its old
per-value filters by hand.

`unless_columns` makes the branch filter a fallback: a dataset with `area`
is scoped to the user's area, one without `area` to the user's branch.

It's idempotent: on a re-run, unchanged filters log `ok`. **Re-run it
whenever a dataset is added**, or the new dataset is unfiltered. New
branches need nothing.

It never deletes: a filter for a value that was dropped from the config
stays until removed by hand.

Adding a value, e.g. area `DN`:

1. Add `"DN"` to the `area` values in `rls_config.json` and run the
   script.
2. Add `"area:DN": ["AREA_DN"]` to `AUTH_ROLES_MAPPING` in the Superset
   Helm values and roll out.

Until both are done, users with `area: DN` see no rows (fail closed).

Changing a customer-type code list (e.g. a new KHCN code): edit the list
under `type` → `values` in `rls_config.json` and run the script. It
updates `RLS type = KHCN` and the `RLS type = ALL` union in place; no
Superset rollout is needed. If `CUSTTPCD` is a text column in the real
tables, write the codes as strings (`"100"`) so they're quoted.

## 3. Demo and tests

```bash
python3 demo/setup_demo.py       # needs the dept-role-sync job to have run
python3 setup_rls.py             # attach filters to the new demo dataset
python3 demo/test_access.py      # all users; or: test_access.py dave bob
```

`setup_demo.py` (idempotent) creates:

- **datasets**, virtual over Trino `VALUES`, so there's no table to load:
  `demo_branch_data` with these rows, and `demo_branch_detail` with the
  same rows minus the `area` column (filtered by the user's branch):

  | brcd | area | custtpcd | (type) | amount |
  |---|---|---|---|---|
  | 1050 | HN | 100 | KHCN | 100 |
  | 1050 | HN | 200 | KHDN | 200 |
  | 1070 | HN | 300 | KHCN | 50 |
  | 1060 | HCM | 570 | KHCN | 300 |
  | 1060 | HCM | 510 | KHDN | 400 |
  | 1050 | HN | 999 | neither list | 10 |

  The code-999 row checks that `RLS_TYPE_ALL` is the union of the two
  lists, not "everything": only Admin and `DSP_APPLICATION_ADMIN` see it.

- **charts** `Demo - rows` (table) and `Demo - total amount` (big number)
  on `demo_branch_data`, and `Demo - branch rows` (table) on
  `demo_branch_detail`;
- **dashboards**, both published, each with all three charts:
  - `Demo - 1050 - 41`, granted to `1050 - 41 - …`
  - `Demo - 1060 - 41`, granted to `1060 - 41 - …`

`test_access.py` logs in through LDAP as each test user (password
`Passw0rd!`, or `TEST_PASSWORD`) and checks:

- the roles they get;
- which demo dashboards they can list;
- the rows returned from each dataset through each dashboard (or a denial);
- the rows returned with no dashboard context;
- whether they can create content (a dashboard);
- whether they can run SQL Lab;
- the branch filter: dave's rendered SQL contains `brcd IN ('1050')`, and
  the area dataset's SQL has no branch filter;
- the cache: dave (1050) loads the area-less chart with `force`, then
  `jack` (1060, the same RLS filters otherwise) loads it from the cache.
  They must get different cache keys and jack only his own row, and
  jack's repeat must be a cache hit (proving caching is on). With the
  `get_rls_cache_key` override disabled, the first two checks fail: jack
  gets dave's row.

The test users' directory entries are listed in the README's "Result"
table.

### Result (2026-11-18)

```
PASS  alice  roles
PASS  alice  dashboards visible ['Demo - 1050 - 41', 'Demo - 1060 - 41']
PASS  alice  data   without dashboard: 6 rows
PASS  alice  data   via 'Demo - 1050 - 41': 6 rows
PASS  alice  detail without dashboard: 6 rows
PASS  alice  detail via 'Demo - 1050 - 41': 6 rows
PASS  alice  create content: True
PASS  alice  SQL Lab: True
PASS  henry  roles
PASS  henry  dashboards visible []
PASS  henry  data   without dashboard: 6 rows
PASS  henry  data   via 'Demo - 1050 - 41': 6 rows
PASS  henry  detail without dashboard: 6 rows
PASS  henry  detail via 'Demo - 1050 - 41': 6 rows
PASS  henry  create content: True
PASS  henry  SQL Lab: True
PASS  dave   roles
PASS  dave   dashboards visible ['Demo - 1050 - 41']
PASS  dave   data   via 'Demo - 1050 - 41': 2 rows
PASS  dave   data   via 'Demo - 1060 - 41': denied
PASS  dave   data   without dashboard: denied
PASS  dave   detail via 'Demo - 1050 - 41': 1 rows
PASS  dave   detail without dashboard: denied
PASS  dave   create content: True
PASS  dave   SQL Lab: False
PASS  carol  roles
PASS  carol  dashboards visible ['Demo - 1050 - 41']
PASS  carol  data   via 'Demo - 1050 - 41': 1 rows
PASS  carol  data   without dashboard: denied
PASS  carol  detail via 'Demo - 1050 - 41': 1 rows
PASS  carol  create content: False
PASS  carol  SQL Lab: False
PASS  bob    roles
PASS  bob    dashboards visible ['Demo - 1060 - 41']
PASS  bob    data   via 'Demo - 1060 - 41': 1 rows
PASS  bob    data   via 'Demo - 1050 - 41': denied
PASS  bob    detail via 'Demo - 1060 - 41': 1 rows
PASS  bob    create content: True
PASS  bob    SQL Lab: False
PASS  erin   roles
PASS  erin   dashboards visible ['Demo - 1050 - 41']
PASS  erin   data   via 'Demo - 1050 - 41': 0 rows
PASS  erin   detail via 'Demo - 1050 - 41': 0 rows
PASS  erin   create content: False
PASS  erin   SQL Lab: False
PASS  gina   roles
PASS  gina   dashboards visible ['Demo - 1050 - 41']
PASS  gina   data   via 'Demo - 1050 - 41': 0 rows
PASS  gina   detail via 'Demo - 1050 - 41': 0 rows
PASS  gina   create content: True
PASS  gina   SQL Lab: False
PASS  ivan   roles
PASS  ivan   dashboards visible ['Demo - 1050 - 41']
PASS  ivan   data   via 'Demo - 1050 - 41': 0 rows
PASS  ivan   detail via 'Demo - 1050 - 41': 1 rows
PASS  ivan   create content: False
PASS  ivan   SQL Lab: False
PASS  jack   roles
PASS  jack   dashboards visible ['Demo - 1060 - 41']
PASS  jack   data   via 'Demo - 1060 - 41': 1 rows
PASS  jack   data   via 'Demo - 1050 - 41': denied
PASS  jack   detail via 'Demo - 1060 - 41': 1 rows
PASS  jack   detail via 'Demo - 1050 - 41': denied
PASS  jack   create content: False
PASS  jack   SQL Lab: False
PASS  kim    roles
PASS  kim    dashboards visible ['Demo - 1050 - 41']
PASS  kim    data   via 'Demo - 1050 - 41': 3 rows
PASS  kim    detail via 'Demo - 1050 - 41': 2 rows
PASS  kim    create content: False
PASS  kim    SQL Lab: False
PASS  dave   area dataset SQL has area IN ('HN'), no brcd
PASS  dave   area-less dataset SQL has brcd IN ('1050'), no area
PASS  ivan   no AREA_* role: no area IN (...) in SQL
PASS  kim    type ALL renders the union of KHCN and KHDN codes
PASS  cache  data: dave and jack get different cache keys
PASS  cache  data: jack is not served dave's cached rows
PASS  cache  data: caching is on (jack's repeat is a cache hit)
PASS  cache  detail: dave and jack get different cache keys
PASS  cache  detail: jack is not served dave's cached rows
PASS  cache  detail: caching is on (jack's repeat is a cache hit)

81/81 checks passed
```

Also verified: a department rename through the real CronJob kept the role
id (134), and `dave` kept access to his dashboard after logging in again.

The data checks call `/api/v1/chart/data` with the chart's `slice_id` and
`dashboardId` in `form_data`, the same as a chart rendered inside the
dashboard, so the dashboard-based access path (`DASHBOARD_RBAC`) is
exercised.
