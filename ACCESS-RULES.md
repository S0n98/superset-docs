# Superset data access rules

Every access rule, in one place. Each rule is marked:

- ✅ **Implemented & tested** on the test cluster (`data-access/demo/test_access.py`, 41/41 checks pass, 2026-11-17)
- 📝 **Decision taken** while building; easy to change, listed so it can be reviewed
- ❓ **Open**: needs a decision

## Summary: what every user has

Each user holds **three kinds of role**, all rebuilt from the directory at
every login:

| Kind | Example | From (LDAP attribute / OIDC claim) | Controls | Status |
|---|---|---|---|---|
| **Functional role** | `DSP_DATA_ANALYST` | group membership (`memberOf`) | What they can **do**: view, build, SQL Lab, manage | ✅ |
| **Department role** | `1050 - 41 - Văn phòng Đoàn Thanh niên` | `brcd` + `department` | Which **dashboards** they can open | ✅ |
| **Data roles** | `AREA_HN`, `TYPE_KHCN` | `area`, `type` | Which **rows** they see: `area = 'HN' AND data_type = 'KHCN'` | ✅ |

`brcd` picks the department role. Rows are filtered by `area` and `type`,
not by `brcd`: a user sees every branch of their area (R13).

---

## 1. Login and roles

**R1 ✅ Everyone authenticates through the directory.** Superset uses LDAP
(README Part C), or OIDC with the same model (Part H). A user is created on
first login.

**R2 ✅ Roles come only from the directory, and are rebuilt at every
login** (`AUTH_ROLES_SYNC_AT_LOGIN = True`):

- A role added to a user by hand in the Superset UI is removed at their
  next login.
- A change in the directory takes effect at the user's next login.
- Service accounts are included: the LDAP bind account, which
  `dept-role-sync` also logs in as, gets `Admin` from the
  `superset_admins` group.

**R3 ✅ Every user also gets `DSP_DATA_VIEWER`** (📝
`AUTH_USER_REGISTRATION_ROLE`; it was `Gamma`). Permissions add up across a
user's roles, so the default role is the floor for everyone. With `Gamma`
as the default, every user could build charts and dashboards and
`DSP_DATA_VIEWER` would restrict nothing. Tested: users with no
functional group (`carol`, `erin`) can view their dashboards but cannot
create content.

**R4 ✅ A role must exist before it can be assigned.** Flask-AppBuilder does
not create mapped roles; a missing role is silently dropped at login. Each
kind of role has its creator:

| Roles | Created by |
|---|---|
| `DSP_*` | One-time import from a roles export (`superset fab import-roles`) |
| `<brcd> - <deptcd> - <deptnm>` | `dept-role-sync` CronJob, every 15 min |
| `AREA_*`, `TYPE_*` | `data-access/setup_rls.py` (together with their RLS filters) |

**R5 ✅ Department and data roles carry no permissions.** Data reaches
users through granted dashboards (R10) and is cut down by RLS (section 4).
Only `Admin` and `DSP_APPLICATION_ADMIN` have dataset access of their own.

## 2. Functional roles (`DSP_*`)

**R6 ✅ Functional roles come from directory groups.**
`AUTH_LDAP_GROUP_FIELD = "memberOf"`, mapped in `AUTH_ROLES_MAPPING`:

| Group | Role |
|---|---|
| `superset_admins` | `Admin` |
| `dsp_application_admin` | `DSP_APPLICATION_ADMIN` |
| `dsp_data_steward` | `DSP_DATA_STEWARD` |
| `dsp_data_analyst` | `DSP_DATA_ANALYST` |
| `dsp_data_viewer` | `DSP_DATA_VIEWER` (also the default, R3) |

For OIDC, the IdP sends group names in a `groups` claim.

**R7 ✅ What each role can do** (from the system's roles export; imported
unchanged into the test cluster):

| Role | Equivalent to | Own data access | SQL Lab | Can create/change |
|---|---|---|---|---|
| `DSP_APPLICATION_ADMIN` | `Alpha` + SQL Lab query rights + `can_write Database`, minus CSV upload | **All datasets & databases** | Yes | Datasets, databases, charts, dashboards, alerts/reports, annotations, CSS templates; import/export |
| `DSP_DATA_STEWARD` | `Gamma` + `can_save Datasource` + `can_write Database` | None | No | Charts, dashboards, dataset metadata, **database connections** |
| `DSP_DATA_ANALYST` | **Identical to `Gamma`** | None | No | Charts, dashboards |
| `DSP_DATA_VIEWER` | Strict subset of `Gamma` | None | No | Nothing: view dashboards/charts, filters, drill, share |

Tested:

- Admin and application admin: SQL Lab yes, create content yes.
- Steward and analyst: create content yes, SQL Lab no.
- Viewer: neither.

**R8 📝 `DSP_APPLICATION_ADMIN` is unrestricted by RLS, like `Admin`.** The
deny-by-default filters (R15) exclude it. Reason: it already has
`all_datasource_access` and SQL Lab, and RLS doesn't apply in SQL Lab (R17),
so filtering its charts would protect nothing. Tested: `henry` reads all
rows. To restrict it, remove it from `unrestricted_roles` in
`data-access/rls_config.json` **and** turn on `RLS_IN_SQLLAB`.

## 3. Department roles → dashboard access

**R9 ✅ Every department in the department API is a role named
`<brcd> - <deptcd> - <deptnm>`.** The same `deptcd` in another branch is a
different role. The `dept-role-sync` CronJob runs every 15 minutes:

| Situation | What the sync does |
|---|---|
| New department | Creates its role |
| `deptnm` changed upstream | **Renames the role in place.** Same role id, so user assignments and dashboard grants are kept (tested: id 134 kept) |
| Department removed upstream | Nothing (no deletes) |
| Same `brcd`/`deptcd` twice in the API, or two roles with the same prefix | Skips it and logs a warning |
| Department without `brcd` or `deptcd` | Skips it and logs an error |
| Name longer than 64 characters | Truncates it and logs a warning |

**R10 ✅ Dashboards are granted per department role** (`DASHBOARD_RBAC`): an
admin adds department roles in the dashboard's *Properties → Roles*.
Confirmed in Superset 6.0's access check and tested:

- **The dashboard must be published.** A draft with roles is visible only
  to Admins and its owners.
- **Once a dashboard has roles, the roles decide.** Dataset access no
  longer opens it.
- **A granted user can read that dashboard's chart data without dataset
  permissions**, only inside that dashboard, and with RLS applied. Tested:
  `dave` gets data through `Demo - 1050 - 41`, but is denied through
  `Demo - 1060 - 41` and with no dashboard context.

**R11 ✅ `brcd` + `department` select the department role.** At login,
`1050` + `41` matches the one role whose name starts with `1050 - 41 - `.

- The codes must match exactly, leading zeros included: `06` ≠ `6`.
- If no role matches, or several do, the user gets no department role
  (several matches are logged).

Tested: `bob` (`1060`/`41`) gets `1060 - 41 - …` and only sees that
dashboard.

**R12 ✅ The `<brcd> - <deptcd> - ` prefix is the role's identity.** Never
edit a department role's name by hand: breaking the prefix makes the next
sync create a duplicate. Changing the name format also means new roles:
the switch from `<deptcd> - ` to `<brcd> - <deptcd> - ` created new roles,
and their dashboards must be granted again.

**Timing:**

- A new department's role exists after the next sync (≤ 15 min). Users get
  it at their next login after that.
- A rename takes effect right after the sync, with no re-login, because the
  role id doesn't change.

## 4. Row-level security: area and type

Data tables carry `area` and `data_type` columns (and `brcd`). **A user sees
only rows of their own area AND their own customer type.**

**R13 ✅ Rows are filtered by `area` and `type`, not by `brcd`** (decided
2026-11-17). `brcd` belongs to exactly one area, so a `brcd` filter would
narrow users to their own branch; the rule is "everything in my area".

**R14 ✅ One role and one filter per value**, mapped statically, because
the values are few and stable:

| Attribute value | Role | Filter (Regular) | Group key | Clause |
|---|---|---|---|---|
| `area: HN` | `AREA_HN` | `RLS area = HN` | `area` | `area = 'HN'` |
| `area: HCM` | `AREA_HCM` | `RLS area = HCM` | `area` | `area = 'HCM'` |
| `type: KHCN` | `TYPE_KHCN` | `RLS type = KHCN` | `data_type` | `data_type = 'KHCN'` |
| `type: KHDN` | `TYPE_KHDN` | `RLS type = KHDN` | `data_type` | `data_type = 'KHDN'` |

Mapping keys are prefixed (`area:HN`, `type:KHCN`) so attribute values
can't collide with each other or with group DNs. A new value needs:

1. a line in `AUTH_ROLES_MAPPING`;
2. a value in `data-access/rls_config.json`;
3. a re-run of `setup_rls.py`.

**R15 ✅ Deny by default.** A Regular filter only restricts users who hold
its role, so without this a user with no `area` would see **every** row.
For each dimension there is one **Base** filter with clause `1 = 0`, on the
same group key. It applies to everyone **except** `Admin` and
`DSP_APPLICATION_ADMIN` (R8).

Superset ORs filters that share a group key and ANDs the groups:

| User | Effective clause | Rows (tested) |
|---|---|---|
| `dave` HN / KHCN | `(1=0 OR area='HN') AND (1=0 OR data_type='KHCN')` | 2 (branches 1050 and 1070) |
| `carol` HN / KHDN | `… area='HN' … data_type='KHDN'` | 1 |
| `bob` HCM / KHDN | `… area='HCM' … data_type='KHDN'` | 1 |
| `erin`, no area or type | `(1=0) AND (1=0)` | 0 |
| `gina` HN, no type | `(1=0 OR area='HN') AND (1=0)` | 0 |
| `alice` Admin, `henry` App Admin | none | all 5 |

An unknown attribute value (e.g. `area: DN` with no mapping) gives no role,
so the user sees no rows: it fails closed.

**R16 ✅ Every dataset with the column is covered automatically.**
`setup_rls.py` attaches each dimension's filters to every dataset that has
its column (`"datasets": "auto"`). **Re-run it after adding a dataset**: a
dataset added later isn't filtered until then.

**R17 ✅ SQL Lab bypasses RLS** (`RLS_IN_SQLLAB` is off by default in 6.0).
Only `Admin` and `DSP_APPLICATION_ADMIN` have SQL Lab, and both are
unrestricted anyway (R8). If a restricted role ever gets SQL Lab, enable
`RLS_IN_SQLLAB` first.

**R18 ✅ The directory owns `area` and `type`.** If a branch moves to
another area, every user of that branch needs a new `area` attribute; it
applies at their next login. Ideally the directory derives `area` from
`brcd` automatically.

> **Alternatives considered:**
>
> - **A branch API.** The sync job reads a branch → area API and builds
>   per-branch roles. Branch moves would then need no directory change.
>   Choose this if the directory can't maintain `area` reliably.
> - **Ranger row filters in Trino.** These cover SQL Lab and other tools
>   too, but need per-user impersonation from Superset to Trino.

## 5. Findings to review

**R19 ❓ `DSP_DATA_STEWARD` and `DSP_APPLICATION_ADMIN` have `can_write
Database`.** That allows creating or editing database connections,
including the connection URI and credentials. Confirm this is intended for
stewards.

**R20 ❓ `DSP_APPLICATION_ADMIN` can't open role-granted dashboards.** Only
`Admin` and the dashboard's owners bypass `DASHBOARD_RBAC`. Tested: `henry`
lists none of the demo dashboards, although he can read all their data.
If application admins should see them, add the role to those dashboards,
or make them owners.

**R21 ❓ Analysts and stewards can create a chart on a dataset they have
no access to**, and then can't open or delete it. Found while testing:
`POST /api/v1/chart/` succeeded for `dave`, but he couldn't read the chart
back. This is harmless (no data is exposed), but it leaves orphans an
admin has to clean up.

**R22 ❓ `DSP_DATA_ANALYST` is identical to `Gamma`.** If it's meant to differ
(e.g. dataset access, SQL Lab), those permissions are still missing.

**R23 ❓ `DSP_APPLICATION_ADMIN` lacks some of the built-in `sql_lab`
role's permissions** (`TabStateView`, `TableSchemaView`, `Query`,
`SavedQuery`). Running queries works (tested through the API). Check the
SQL Lab UI (tabs, query history, saved queries), or add `sql_lab` alongside
it.

## 6. Open questions

- ❓ Can a user belong to more than one branch, area or type? The
  attributes are single-valued today. Multi-valued would work, because
  filters are ORed per group, but needs list attributes and a small code
  change.
- ❓ Which datasets are intentionally global? Any dataset without
  `area`/`data_type` columns is not filtered at all.
- ❓ Who grants dashboards to department roles: Admins only, or dashboard
  owners?
- ❓ R19 to R23 above.
