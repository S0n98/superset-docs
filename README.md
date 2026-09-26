# Superset: LDAP Authentication + Row-Level Security

How to stand up Apache Superset 6.0.0 with LDAP authentication, and how to
drive Superset's Row Level Security (RLS) from information the LDAP server
sends at login time.

**Production note — group membership is not the only, or even the usual,
source of truth.** The first version of this setup mapped roles from AD/LDAP
*group membership* (`memberOf`). Real production directories frequently
don't authorize this way at all: authorization instead comes from a plain
**attribute already present on the user's own directory entry** —
`department`, `employeeType`, `businessCategory`, a custom AD
`extensionAttribute`, whatever your directory team actually populates —
with no group object, and no group lookup, involved. This repo documents
**both**, but leads with the attribute-driven version, since that's the one
that matches how most real AD/LDAP deployments actually grant permissions.
The underlying mechanism (`AUTH_ROLES_MAPPING`) is identical either way —
only which attribute Superset reads changes.

This repo documents a working, verified setup: a lightweight LDAP server
(`lldap`) as the identity source, a thin custom Superset image with the LDAP
driver installed, Helm values wiring it together, and the exact steps to
create roles and RLS filters so different users see different rows of the
same dataset based on that attribute. **Part H** covers the same problem
one layer up the stack — authorizing from a custom **OIDC claim** instead,
for deployments where Superset sits behind Keycloak/Okta/Azure AD/etc.
rather than talking to LDAP directly. The mechanism (`AUTH_ROLES_MAPPING`)
turns out to be exactly the same one; only where the "role key" comes from
changes.

## Architecture

```mermaid
flowchart LR
    subgraph LDAP["LDAP / AD"]
        U["user entry: uid=bob
        department: HN"]
    end

    subgraph K8s["Kubernetes: default namespace"]
        SS[Superset web/worker pods]
        PG[(Superset metadata Postgres)]
    end

    Trino[(Trino: tpch.tiny.nation / region)]

    U -- "bind + attribute lookup" --> LDAP
    SS -- "AUTH_LDAP_SERVER" --> LDAP
    SS -- "roles, RLS filters" --> PG
    SS -- "SQL query, RLS clause appended" --> Trino
```

The mechanism in one sentence: **an LDAP attribute on the user entry →
`AUTH_ROLES_MAPPING` → Superset role → Row Level Security filter scoped to
that role.** Nothing about RLS is LDAP-specific, and nothing about
`AUTH_ROLES_MAPPING` is group-specific either, despite the setting that
feeds it being named `AUTH_LDAP_GROUP_FIELD` — Flask-AppBuilder just reads
whichever attribute that name points at, treats each value as a "role key",
and looks it up in the mapping. Point it at a group-membership attribute
and you get group-based authorization; point it at any other attribute and
you get attribute-based authorization, with no other code change. LDAP's
only job, either way, is to deterministically hand a user one or more
Superset roles at login — RLS then filters by role exactly like it would
for any other auth backend.

## Result once set up (attribute-driven — the production path)

| LDAP user | `department` attribute | Synced Superset roles | Rows visible on the demo dataset |
|---|---|---|---|
| `admin` (LDAP bind account) | `HO` | `Admin` | all regions (unrestricted) |
| `alice` | `HO` | `Admin` | all regions (unrestricted) |
| `bob` | `HN` | `KHCN`, `Region_HN` | only `region_name = 'ASIA'` |
| `carol` | `HCM` | `KHCN`, `Region_HCM` | only `region_name = 'EUROPE'` |

No LDAP group is read at all — confirmed from the running pod's debug log,
which shows Superset requesting `department`, not `memberOf`, on every bind:

```
DEBUG:flask_appbuilder.security.manager:LDAP search for '(uid=bob)' with fields ['givenName', 'sn', 'mail', 'department'] in scope 'dc=example,dc=com'
DEBUG:flask_appbuilder.security.manager:LDAP search returned: [('uid=bob,ou=people,dc=example,dc=com', {'department': [b'HN'], ...})]
DEBUG:flask_appbuilder.security.manager:Calculated new roles for user='uid=bob,ou=people,dc=example,dc=com' as: [Gamma, Region_HN, KHCN]
```

Verified with the actual Chart Data API response — `bob` sees only Asian
nations, `carol` only European ones, `alice`/`admin` see everything:

```json
{"nation": "INDIA", "region_name": "ASIA"}
{"nation": "INDONESIA", "region_name": "ASIA"}
{"nation": "JAPAN", "region_name": "ASIA"}
{"nation": "CHINA", "region_name": "ASIA"}
{"nation": "VIETNAM", "region_name": "ASIA"}
```

---

## Part A — Stand up the test LDAP server

[lldap](https://github.com/lldap/lldap) is a minimal LDAP server good enough
for auth testing: it speaks LDAPv3, needs no schema design, and supports
adding arbitrary custom attributes to user entries — which is what lets it
stand in for "our AD sends back a `department`/`employeeType`/custom
extension attribute" without needing a real AD to test against.

```bash
docker run -d --name lldap \
  --restart unless-stopped \
  -p 3890:3890 \
  -p 17170:17170 \
  -e LLDAP_JWT_SECRET="<random secret>" \
  -e LLDAP_LDAP_BASE_DN="dc=example,dc=com" \
  -e LLDAP_LDAP_USER_PASS="adminpass" \
  -v /root/lldap/data:/data \
  lldap/lldap:stable
```

- Port `3890` is the LDAP protocol port — this is what Superset connects to.
- Port `17170` is lldap's own admin web UI / REST+GraphQL API — used below to
  create users and groups, not by Superset.
- `LLDAP_LDAP_USER_PASS` sets the password for the built-in admin account,
  `uid=admin,ou=people,dc=example,dc=com`.

### Create users

lldap has no LDAP-native way to create entries (no `ldapadd` support) —
everything is done through its GraphQL API. Get a JWT first:

```bash
TOKEN=$(curl -s -X POST http://localhost:17170/auth/simple/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"adminpass"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['token'])")

gql() { curl -s -X POST http://localhost:17170/api/graphql \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "$1"; }
```

```bash
gql '{"query":"mutation{createUser(user:{id:\"alice\",email:\"alice@example.com\",firstName:\"Alice\",lastName:\"Admin\"}){id}}"}'
gql '{"query":"mutation{createUser(user:{id:\"bob\",email:\"bob@example.com\",firstName:\"Bob\",lastName:\"KhcnHN\"}){id}}"}'
gql '{"query":"mutation{createUser(user:{id:\"carol\",email:\"carol@example.com\",firstName:\"Carol\",lastName:\"KhcnHCM\"}){id}}"}'
```

Set passwords — the GraphQL API has no password mutation; lldap ships a
dedicated CLI for it (present inside the container at `/app/lldap_set_password`):

```bash
for u in alice bob carol; do
  docker exec lldap /app/lldap_set_password \
    --base-url "http://localhost:17170" \
    --admin-username admin --admin-password adminpass \
    --username "$u" --password "Passw0rd!"
done
```

### Add the authorization attribute and set it per user

This is the step that matters for the production scenario: register a new
attribute on the **user** schema (once, cluster-wide), then set its value on
each user entry directly — no group object involved anywhere.

```bash
# Register the attribute once. type STRING, single-valued, visible+editable
# so it shows up in lldap's own admin UI too.
gql '{"query":"mutation{addUserAttribute(name:\"department\",attributeType:STRING,isList:false,isVisible:true,isEditable:true){ok}}"}'

# Set it per user (insertAttributes on updateUser). This is the only place
# each user's authorization value lives -- their own entry.
gql '{"query":"mutation{updateUser(user:{id:\"admin\",insertAttributes:[{name:\"department\",value:[\"HO\"]}]}){ok}}"}'
gql '{"query":"mutation{updateUser(user:{id:\"alice\",insertAttributes:[{name:\"department\",value:[\"HO\"]}]}){ok}}"}'
gql '{"query":"mutation{updateUser(user:{id:\"bob\",insertAttributes:[{name:\"department\",value:[\"HN\"]}]}){ok}}"}'
gql '{"query":"mutation{updateUser(user:{id:\"carol\",insertAttributes:[{name:\"department\",value:[\"HCM\"]}]}){ok}}"}'
```

Note the LDAP bind account (`admin`) gets a value too — see the gotcha in
Part C about why skipping this for "infrastructure" accounts breaks things
on their very next login.

If your actual AD already has the attribute you need (it usually will —
`department`, `employeeType`, `co`, `physicalDeliveryOfficeName`, and custom
`extensionAttribute1`–`15` are common real-world choices), skip this whole
subsection: there's nothing to register, you just need to know the
attribute's LDAP name for Part C.

### Verify the attribute is actually returned

This is the one thing to sanity-check before touching Superset at all,
because Superset's role mapping depends entirely on it:

```bash
ldapsearch -x -H ldap://127.0.0.1:3890 \
  -D "uid=admin,ou=people,dc=example,dc=com" -w adminpass \
  -b "dc=example,dc=com" "(uid=bob)" department
```

Expected:

```
dn: uid=bob,ou=people,dc=example,dc=com
department: HN
```

If this doesn't show the attribute, nothing downstream will work — go no
further until it does. Against a real AD, run the equivalent `ldapsearch`
(or ADUC / `Get-ADUser -Properties <attr>` in PowerShell) as the account
Superset will bind with, since AD can restrict which attributes a given
bind account is allowed to read.

### Alternative: mapping from group membership instead

If your directory genuinely does authorize via group membership, the setup
is the same shape, just reading a different (multi-valued) attribute:
create groups via `createGroup`, add users to them via `addUserToGroup`,
and point Superset at `memberOf` instead of a plain attribute — covered in
Part C. lldap computes `memberOf` on user entries automatically from group
membership, no extra step needed; verify it the same way:
`ldapsearch ... "(uid=bob)" memberOf`.

---

## Part B — Build a Superset image with LDAP support

The published `apache/superset:6.0.0` image does **not** include
`python-ldap` (or, for that matter, `psycopg2`/`trino` — those happen to
already be present in this deployment's metadata-DB and Trino-dataset use
case, but check your own image if you need them). `python-ldap` is a C
extension with no portable prebuilt wheel, so it needs a compiler and the
OpenLDAP/SASL headers at build time.

**Do not try to install it at pod startup** (e.g. via the Helm chart's
`bootstrapScript`, which runs on every single pod start/restart). It was
tried here first and reliably lost the race against Kubernetes' startup
probe — `apt-get install build-essential` plus compiling `python-ldap` took
5–15 minutes depending on registry/mirror load, which blew past even a
generously raised `failureThreshold`, and every restart repeated the whole
compile from scratch since the container filesystem is ephemeral. Bake it
into the image once instead — see `Dockerfile.superset-ldap` in this repo:

```dockerfile
FROM apache/superset:6.0.0
USER root
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends libldap2-dev libsasl2-dev build-essential python3-dev \
    && /app/.venv/bin/python -m ensurepip --upgrade \
    && /app/.venv/bin/python -m pip install --no-cache-dir python-ldap trino psycopg2-binary \
    && apt-get purge -y --auto-remove build-essential python3-dev \
    && rm -rf /var/lib/apt/lists/*
USER superset
```

Two non-obvious details baked into that Dockerfile:

- **Use the app's venv pip, not the system `pip`.** The base image's Python
  environment is a virtualenv at `/app/.venv`, and it's on `PATH` ahead of
  the system Python — but a bare `pip install` on this image resolves to
  `/usr/local/bin/pip` (pulled in by the `python3-dev` apt package), which
  installs into the *system* site-packages, invisible to the app. Always
  install with `/app/.venv/bin/python -m pip install ...`.
- **`ensurepip` first.** This image's venv was built with `uv`, which
  doesn't bundle `pip` by default — `/app/.venv/bin/python -m pip` fails
  with `No module named pip` until you run `ensurepip`.

Build and get it into the cluster:

```bash
docker build -f Dockerfile.superset-ldap -t superset-ldap:6.0.0 .

# Verify before deploying anything:
docker run --rm superset-ldap:6.0.0 python3 -c "import ldap, psycopg2, trino; print('OK')"
docker run --rm -e SUPERSET_SECRET_KEY=x superset-ldap:6.0.0 superset version
```

If your kubelet pulls from a registry your cluster can reach, push it there
and point the Helm chart's `image.repository`/`image.tag` at it — the
normal path. If you're on a single-node cluster where the image only needs
to exist on that one node's containerd (as here, on RKE2/k3s), import it
directly into containerd's `k8s.io` namespace instead of pushing anywhere:

```bash
docker save docker.io/library/superset-ldap:6.0.0 -o superset-ldap-6.0.0.tar

ctr -a /run/k3s/containerd/containerd.sock -n k8s.io images import \
  --platform linux/amd64 superset-ldap-6.0.0.tar

# Required: containerd's CRI plugin only "sees" images carrying this label —
# `ctr images import` alone does not set it, so `crictl images` (and hence
# kubelet's ImageStatus check, and hence `imagePullPolicy: IfNotPresent`)
# won't find the image without this.
ctr -a /run/k3s/containerd/containerd.sock -n k8s.io images label \
  docker.io/library/superset-ldap:6.0.0 io.cri-containerd.image=managed
```

Watch out for kubelet's image garbage collector: if the node is under disk
pressure and no running pod references the image yet, kubelet can reclaim it
between the import and the first pod actually starting. If a freshly
imported image disappears from `crictl images` before its pod comes up,
re-import and immediately restart the pod (`kubectl delete pod ...`) so it
gets pinned as "in use" before the next GC pass.

---

## Part C — Configure Superset for LDAP auth

Everything below lives in `configOverrides` in the Helm values (see
`superset-values.yaml` in this repo for the full file) — the chart pastes
each entry verbatim into `superset_config.py`.

```python
from flask_appbuilder.security.manager import AUTH_LDAP

AUTH_TYPE = AUTH_LDAP
AUTH_USER_REGISTRATION = True
AUTH_USER_REGISTRATION_ROLE = "Gamma"   # fallback role if no group mapping matches

AUTH_LDAP_SERVER = "ldap://<ldap-host>:3890"
AUTH_LDAP_SEARCH = "dc=example,dc=com"
AUTH_LDAP_UID_FIELD = "uid"

AUTH_LDAP_BIND_USER = "uid=admin,ou=people,dc=example,dc=com"
AUTH_LDAP_BIND_PASSWORD = "adminpass"

AUTH_LDAP_FIRSTNAME_FIELD = "givenName"
AUTH_LDAP_LASTNAME_FIELD = "sn"
AUTH_LDAP_EMAIL_FIELD = "mail"

# The attribute read on every login and mapped to Superset roles below.
# Despite the setting's name, this does NOT have to be a group-membership
# field -- it can be any attribute your directory returns on the user
# entry. Here it's "department", a plain attribute with no group object
# behind it at all. (To authorize from real AD group membership instead,
# set this to "memberOf" and key AUTH_ROLES_MAPPING by group DNs instead
# of attribute values -- see the "Alternative" callout in Part A.)
AUTH_LDAP_GROUP_FIELD = "department"
AUTH_ROLES_SYNC_AT_LOGIN = True

AUTH_ROLES_MAPPING = {
    # Keys here are values of the `department` attribute, not group DNs.
    "HO": ["Admin"],
    "HN": ["KHCN", "Region_HN"],
    "HCM": ["KHCN", "Region_HCM"],
}
```

### Gotcha: `AUTH_ROLES_SYNC_AT_LOGIN` recalculates roles from scratch on
### every login — including for accounts you didn't mean to touch

`AUTH_ROLES_SYNC_AT_LOGIN = True` means the user's Superset role set is
**replaced**, not merged, on every login, with whatever
`AUTH_ROLES_MAPPING` computes from their current attribute value (falling
back to `AUTH_USER_REGISTRATION_ROLE` if nothing matches, or if the
attribute is simply unset on that entry). This bit us directly: the LDAP
bind account (`uid=admin`) had no `department` value set at first. The
first time that account logged into Superset through LDAP (which happens
any time you authenticate as `admin` via the LDAP provider — including just
to test the setup), its pre-existing `Admin` role was silently stripped
down to only the default `Gamma` fallback.

**Fix**: make sure every account you care about — including
infrastructure/bind accounts, and especially any real-AD service account
Superset itself binds as — actually has the attribute set to a value
`AUTH_ROLES_MAPPING` covers. (In this setup: `department: HO` on the `admin`
entry, mapped to `Admin` above.) If you hit this after the fact, the role
has to be restored directly:

```sql
insert into ab_user_role (id, user_id, role_id)
select coalesce(max(id),0)+1, <user_id>, <admin_role_id>
from ab_user_role;
```

(Watch the sequence when doing this by hand — see Troubleshooting below.)

### Also required: `python-ldap`/`trino`/`psycopg2` need libc/OS packages

Already covered by the image in Part B — noted here only so the config and
the image requirement stay linked in your head: `AUTH_TYPE = AUTH_LDAP`
does nothing useful without `python-ldap` importable in the running
container, and there's no in-app error that says so clearly — it surfaces
as `ModuleNotFoundError: No module named 'ldap'` or `'psycopg2'` deep in a
gunicorn worker traceback at startup.

---

## Part D — Deploy

```bash
helm upgrade --install superset superset/superset \
  -f superset-values.yaml -n default
```

Key values to set alongside the LDAP `configOverrides` above (see the full
file in this repo):

```yaml
image:
  repository: docker.io/library/superset-ldap   # the image from Part B
  tag: "6.0.0"
  pullPolicy: IfNotPresent

extraSecretEnv:
  SUPERSET_SECRET_KEY: "<random secret>"         # Superset refuses to start on the default
```

If you're changing an **existing** release's chart version, be aware that a
newer chart can change the Deployment's `spec.selector.matchLabels` (this
happened going from an old pinned `superset-0.15.5` install to whatever the
repo currently serves) — that field is immutable on a Kubernetes
Deployment, so `helm upgrade` fails outright with a `field is immutable`
error. The fix is `helm uninstall` + fresh `helm install`, not an upgrade.
This is safe for the Postgres metadata store specifically because
StatefulSet-managed PVCs (`data-superset-postgresql-0`, created from the
chart's `volumeClaimTemplate`) are **not** deleted by `helm uninstall` —
only by deleting the PVC object directly, which uninstall doesn't do. A
fresh install with the same release name and same `postgresql.auth`
credentials reattaches to the existing PVC and keeps every user, role, and
dataset that was already there.

---

## Part E — Create the roles `AUTH_ROLES_MAPPING` refers to

**Flask-AppBuilder does not auto-create roles named in `AUTH_ROLES_MAPPING`.**
If a mapped role name (`KHCN`, `Region_HN`, `Region_HCM` here) doesn't
already exist, FAB logs a warning and silently drops it from the user's
role set for that login:

```
WARNING:flask_appbuilder.security.manager:Can't find role specified in AUTH_ROLES_MAPPING: KHCN
```

Create them once, as an admin, before anyone in those LDAP groups logs in
for real (or just re-log them in afterward — `AUTH_ROLES_SYNC_AT_LOGIN`
recalculates every time):

```bash
# Log in as the LDAP bind account, which now maps to Superset's Admin role
# (see the cn=lldap_admin mapping above)
TOKEN=$(curl -s -X POST http://localhost:8088/api/v1/security/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"adminpass","provider":"ldap","refresh":true}' \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['access_token'])")

# Superset's API CSRF check needs a session cookie paired with the CSRF
# token, not just the header -- use a cookie jar across both calls.
COOKIES=cookies.txt
CSRF=$(curl -s -c $COOKIES -b $COOKIES http://localhost:8088/api/v1/security/csrf_token/ \
  -H "Authorization: Bearer $TOKEN" | python3 -c "import json,sys;print(json.load(sys.stdin)['result'])")

for role in KHCN Region_HN Region_HCM; do
  curl -s -c $COOKIES -b $COOKIES -X POST http://localhost:8088/api/v1/security/roles/ \
    -H "Authorization: Bearer $TOKEN" -H "X-CSRFToken: $CSRF" \
    -H "Content-Type: application/json" -H "Referer: http://localhost:8088/" \
    -d "{\"name\":\"$role\"}"
done
```

A brand-new role starts with **zero permissions** — it can't see any
dataset yet, RLS or not. Grant it access to the datasource(s) it needs
(`Admin` UI: *Settings → List Roles → edit role → add
"datasource access on [DB].[table]"* and "database access on [DB]"; or via
SQL against `ab_permission_view` / `ab_permission_view_role` if scripting
this, matching the pattern in Part F below). Skipping this step produces a
clear, specific error rather than silently-wrong data —
`DATASOURCE_SECURITY_ACCESS_ERROR`, "This endpoint requires the datasource
..., database or `all_datasource_access` permission" — which is the signal
to go grant it.

---

## Part F — Set up the actual Row Level Security

RLS in Superset is a straightforward mechanism once the LDAP → role
plumbing above is in place: a filter is a SQL clause, scoped to one or more
datasets, that applies **only to users holding one of the roles it's
scoped to**. Everyone else queries the dataset unfiltered (or filtered by
whichever *other* RLS rule matches their own roles).

### Demo dataset

A virtual (SQL-defined) dataset over Trino's built-in `tpch` sample data —
chosen because it's read-only, always available, and needs no data loading:

```sql
SELECT n.name AS nation, r.name AS region_name
FROM tpch.tiny.nation n
JOIN tpch.tiny.region r ON n.regionkey = r.regionkey
```

Create it via the API (or Superset's UI: *Data → Datasets → + Dataset →
SQL*):

```bash
curl -s -c $COOKIES -b $COOKIES -X POST http://localhost:8088/api/v1/dataset/ \
  -H "Authorization: Bearer $TOKEN" -H "X-CSRFToken: $CSRF" \
  -H "Content-Type: application/json" -H "Referer: http://localhost:8088/" \
  -d '{
    "database": 1,
    "table_name": "rls_demo_nation_region",
    "sql": "SELECT n.name AS nation, r.name AS region_name FROM tpch.tiny.nation n JOIN tpch.tiny.region r ON n.regionkey = r.regionkey"
  }'
```

### RLS filters

Two filters, each scoped to one of the roles the LDAP groups map to:

```bash
curl -s -c $COOKIES -b $COOKIES -X POST http://localhost:8088/api/v1/rowlevelsecurity/ \
  -H "Authorization: Bearer $TOKEN" -H "X-CSRFToken: $CSRF" \
  -H "Content-Type: application/json" -H "Referer: http://localhost:8088/" \
  -d '{
    "name": "RLS - HN region only",
    "filter_type": "Regular",
    "tables": [1],
    "roles": [<Region_HN role id>],
    "group_key": "region",
    "clause": "region_name = '\''ASIA'\''"
  }'

curl -s -c $COOKIES -b $COOKIES -X POST http://localhost:8088/api/v1/rowlevelsecurity/ \
  -H "Authorization: Bearer $TOKEN" -H "X-CSRFToken: $CSRF" \
  -H "Content-Type: application/json" -H "Referer: http://localhost:8088/" \
  -d '{
    "name": "RLS - HCM region only",
    "filter_type": "Regular",
    "tables": [1],
    "roles": [<Region_HCM role id>],
    "group_key": "region",
    "clause": "region_name = '\''EUROPE'\''"
  }'
```

`filter_type: "Regular"` ANDs the clause onto every query against the
dataset for a matching user. (`"Base"` behaves similarly but with
different combination semantics when multiple filters apply to the same
user — see Superset's own RLS docs if you need to stack rules.) The
`clause` field is a raw SQL boolean expression injected into the `WHERE` —
it can reference `current_username()`, `current_user_id()`, and other Jinja
helpers Superset exposes if you want the filter itself to vary per-user
rather than per-role (not needed here, since the role already encodes
which region a user belongs to).

That's the whole RLS setup. The LDAP group is doing all the interesting
work upstream of this — by the time a request reaches the RLS layer, it's
indistinguishable from RLS configured against a DB-auth or OAuth user with
the same role.

---

## Part G — Test it end-to-end

Log each user in through LDAP and query the dataset via the Chart Data API
(this exercises the same code path a real chart/dashboard would):

```bash
login() {
  curl -s -X POST http://localhost:8088/api/v1/security/login \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"$1\",\"password\":\"$2\",\"provider\":\"ldap\",\"refresh\":true}" \
    | python3 -c "import json,sys;print(json.load(sys.stdin)['access_token'])"
}

query_as() {
  local user=$1 pass=$2 cookies=cookies_$1.txt tok csrf
  tok=$(login "$user" "$pass")
  csrf=$(curl -s -c "$cookies" -b "$cookies" http://localhost:8088/api/v1/security/csrf_token/ \
    -H "Authorization: Bearer $tok" | python3 -c "import json,sys;print(json.load(sys.stdin)['result'])")
  curl -s -c "$cookies" -b "$cookies" -X POST http://localhost:8088/api/v1/chart/data \
    -H "Authorization: Bearer $tok" -H "X-CSRFToken: $csrf" \
    -H "Content-Type: application/json" -H "Referer: http://localhost:8088/" \
    -d '{"datasource":{"id":1,"type":"table"},"queries":[{"columns":["region_name"],"row_limit":1000,"metrics":[],"orderby":[]}],"result_format":"json","result_type":"full"}'
}

query_as alice "Passw0rd!"   # -> ASIA, AMERICA, AFRICA, EUROPE, MIDDLE EAST (Admin: unrestricted)
query_as bob   "Passw0rd!"   # -> ASIA only
query_as carol "Passw0rd!"   # -> EUROPE only
```

Also worth confirming directly against the metadata DB, since it shows the
LDAP → role sync independent of RLS:

```sql
select u.username, r.name
from ab_user u
join ab_user_role ur on u.id = ur.user_id
join ab_role r on r.id = ur.role_id
order by u.username, r.name;
```

Expected after all three have logged in once:

```
 admin | Admin
 admin | Gamma
 alice | Admin
 alice | Gamma
 bob   | Gamma
 bob   | KHCN
 bob   | Region_HN
 carol | Gamma
 carol | KHCN
 carol | Region_HCM
```

---

## Part H — Variant: authorization from OIDC claims, not LDAP at all

Everything above assumes Superset talks to LDAP directly. Plenty of real
deployments don't: Superset sits behind an OIDC identity provider
(Keycloak, Okta, Azure AD / Entra ID, Auth0, ...) that itself may be backed
by the same AD/LDAP directory — the IdP's admin configures a "protocol
mapper" / "claim mapper" that copies an AD attribute (or a group name, or
anything else the IdP can compute) into a custom claim on the ID token or
userinfo response. Superset never talks to LDAP in this setup; it only
ever sees the OIDC claims.

**The good news: the mechanism is identical, just relocated.** FAB's OAuth
login path supports the exact same `role_keys` → `AUTH_ROLES_MAPPING` →
role → RLS pipeline as the LDAP path — it just gets `role_keys` from a
different place: a custom Security Manager method
(`get_oauth_user_info`) that you write, instead of a built-in
`AUTH_LDAP_GROUP_FIELD` setting. Confirmed by reading
`flask_appbuilder/security/manager.py` directly (not guessed): line 1365
of the installed FAB version does
`user_role_keys = userinfo.get("role_keys", [])` in the OAuth login path,
feeding the identical `get_roles_from_keys()` the LDAP path uses at line
1067. Nothing about `AUTH_ROLES_MAPPING`, role creation (Part E), or RLS
(Part F) changes — only Part A (identity source) and Part C (auth config)
do.

### Stand up a lightweight test OIDC provider

[mock-oauth2-server](https://github.com/navikt/mock-oauth2-server) is the
OIDC analogue of `lldap` here: a single-JAR test IdP purpose-built for
exactly this — issuing tokens with whatever custom claims you want, with
no real login UI required for automation.

```bash
docker run -d --name mock-oidc --restart unless-stopped \
  -p 8090:8080 \
  ghcr.io/navikt/mock-oauth2-server:latest
```

Its discovery document is **request-relative** — it reflects whatever
host:port you actually used to reach it, not a fixed hostname. That
matters because Superset's pod reaches this container through the node's
own IP (the same pattern as `AUTH_LDAP_SERVER` above), not `localhost`:

```bash
curl -s http://<node-ip>:8090/default/.well-known/openid-configuration | python3 -m json.tool
# issuer / authorization_endpoint / etc. all come back as <node-ip>:8090,
# exactly the host actually used for the request
```

### Configure Superset for OIDC

Needs `authlib` (Superset's OAuth stack), which — like `python-ldap` in
Part B — is **not** in the base `apache/superset:6.0.0` image. Unlike
`python-ldap`, it's a pure-Python wheel with no compiler needed, so
installing it via `bootstrapScript` at pod startup is fine (it won't race
the startup probe the way a `python-ldap` compile did):

```yaml
bootstrapScript: |
  #!/bin/bash
  if [ ! -f ~/bootstrap ]; then
    /app/.venv/bin/python -m pip install --no-cache-dir Authlib
    echo "Running Superset bootstrap" > ~/bootstrap
  fi
```

```python
from flask_appbuilder.security.manager import AUTH_OAUTH
from superset.security import SupersetSecurityManager

AUTH_TYPE = AUTH_OAUTH
AUTH_USER_REGISTRATION = True
AUTH_USER_REGISTRATION_ROLE = "Gamma"
AUTH_ROLES_SYNC_AT_LOGIN = True

# Identical to the LDAP-variant mapping in Part C.
AUTH_ROLES_MAPPING = {
    "HO": ["Admin"],
    "HN": ["KHCN", "Region_HN"],
    "HCM": ["KHCN", "Region_HCM"],
}

OAUTH_PROVIDERS = [
    {
        "name": "oidc",
        "icon": "fa-key",
        "token_key": "access_token",
        "remote_app": {
            "client_id": "superset",
            "client_secret": "supersetsecret",
            "api_base_url": "http://<node-ip>:8090/default/",
            "server_metadata_url": "http://<node-ip>:8090/default/.well-known/openid-configuration",
            "client_kwargs": {"scope": "openid email profile"},
        },
    }
]

class CustomSsoSecurityManager(SupersetSecurityManager):
    # The whole mechanism, in one method: pull whatever custom claim your
    # IdP actually sends -- "department" here, exactly as arbitrary as the
    # LDAP attribute case -- and hand it back as "role_keys".
    def get_oauth_user_info(self, provider, resp):
        if provider == "oidc":
            me = self.appbuilder.sm.oauth_remotes[provider].get("userinfo")
            me.raise_for_status()
            data = me.json()
            department = data.get("department")
            name = data.get("name") or ""
            return {
                "username": data.get("sub"),
                "email": data.get("email"),
                "first_name": name.split(" ")[0] if name else "",
                "last_name": " ".join(name.split(" ")[1:]) if name else "",
                "role_keys": [department] if department else [],
            }
        return {}

CUSTOM_SECURITY_MANAGER = CustomSsoSecurityManager
```

`api_base_url` is what `.get("userinfo")` resolves against — it must match
the provider's actual `userinfo_endpoint`. `server_metadata_url` lets
authlib auto-discover `authorize_url`/`access_token_url`/`jwks_uri` instead
of hardcoding each one.

**Note:** `AUTH_TYPE` is a single value — a Superset instance is LDAP *or*
OAuth, not both at once through this mechanism. Swapping to this config
replaces the LDAP login entirely; it's not additive.

### Test it — no real browser needed

mock-oauth2-server's "login page" is a plain HTML form (`username` +
`claims` fields) that POSTs back to the same URL — which means the whole
authorization-code flow is scriptable with a cookie jar, the same way the
LDAP tests in Part G were:

```bash
COOKIES=cookies.txt

# 1. Ask Superset to start an OIDC login -- it redirects to the IdP with a
#    state/nonce it will later verify, and sets a session cookie to track them.
LOGIN_REDIRECT=$(curl -s -c $COOKIES -o /dev/null -D - http://localhost:8088/login/oidc \
  | grep -i '^Location:' | sed 's/Location: //I' | tr -d '\r')

# 2. Instead of a real login page, POST straight to that authorize URL
#    with the username and the custom claims this "user" should carry.
CALLBACK=$(curl -s -X POST "$LOGIN_REDIRECT" \
  --data-urlencode "username=bob" \
  --data-urlencode 'claims={"department":"HN","email":"bob@example.com","name":"Bob KhcnHN"}' \
  -D - -o /dev/null | grep -i '^location:' | sed 's/[Ll]ocation: //' | tr -d '\r')

# 3. Follow the IdP's redirect back into Superset's own callback, same
#    cookie jar so it can validate the state/nonce it stashed in step 1.
curl -s -c $COOKIES -b $COOKIES "$CALLBACK" -o /dev/null -D -   # -> 302 to "/" means success

# 4. From here it's a normal authenticated session -- same cookie jar,
#    same CSRF-token dance as Part G.
curl -s -c $COOKIES -b $COOKIES http://localhost:8088/api/v1/me/
```

Verified end to end exactly this way: step 4's `/api/v1/me/` confirms
Superset logged in `bob` (`sub: bob` from the claims in step 2, matched to
username), and the metadata DB shows the same role sync as the LDAP path:

```
 bob | Gamma
 bob | KHCN
 bob | Region_HN
```

...and the Chart Data API against the same RLS-protected dataset from Part
F, using this session's cookie jar instead of a JWT, returns the same
single row set as the LDAP version — `region_name = 'ASIA'` only. Nothing
downstream of "which roles does this user have" needed to change at all.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'ldap'` at Superset startup** — the
image doesn't have `python-ldap` installed into the venv Superset actually
runs under. See Part B; check with
`docker run --rm <image> python3 -c "import ldap"` before deploying.

**`ModuleNotFoundError: No module named 'authlib'` at Superset startup**
(OIDC variant, Part H) — same class of problem, different package: the
base image doesn't bundle `authlib` either, and `AUTH_TYPE = AUTH_OAUTH`
imports it unconditionally during app init, before any of your own config
code runs, so there's no way to defer it. Unlike `python-ldap` this one has
no C extension, so — uniquely among the packages in this repo — it's fine
to install it via `bootstrapScript` at pod startup rather than baking it
into the image; see Part H.

**Pod stuck restarting during a heavy `bootstrapScript`, startup probe
failing with `connection refused`** — the probe is timing out before a
runtime `apt-get install` + compile finishes, and every restart repeats it
from zero. Don't install compiled dependencies at pod startup; bake them
into the image (Part B).

**`Can't find role specified in AUTH_ROLES_MAPPING: <name>`** in the logs —
the role doesn't exist yet in Superset's RBAC. Create it (Part E); FAB does
not auto-create mapped roles.

**A previously-Admin account gets silently downgraded to `Gamma` after
logging in via LDAP once** — `AUTH_ROLES_SYNC_AT_LOGIN` replaced its role
set because that account's `AUTH_LDAP_GROUP_FIELD` value (a group, an
attribute, whichever you configured) wasn't in `AUTH_ROLES_MAPPING` — often
because the attribute was simply blank/unset on that particular entry. Set
the attribute (or group membership) on every real account that needs a
non-default role, not just the "interesting" ones — see the gotcha in
Part C.

**`DATASOURCE_SECURITY_ACCESS_ERROR` when a user with the "right" RLS role
still can't query the dataset** — RLS restricts *rows*, it doesn't grant
*access*. The role also needs `datasource_access` (and `database_access`)
permission-views attached, same as any other Superset role. Grant this
through the role editor UI, or by inserting into
`ab_permission_view_role` (see below) if scripting it.

**`duplicate key value violates unique constraint "..._pkey"` on a table
you just inserted into by hand** — if you ever insert into a FAB-managed
table (`ab_user_role`, `ab_permission_view_role`, etc.) with a manually
computed `id` (e.g. `max(id)+1`) instead of the table's sequence, the
sequence itself doesn't advance, and the next row the *application* inserts
via `nextval()` collides with the id you already used. Always insert with
`nextval('<table>_id_seq')` explicitly, never a hand-computed id:

```sql
insert into ab_permission_view_role (id, permission_view_id, role_id)
values (nextval('ab_permission_view_role_id_seq'), <perm_view_id>, <role_id>);
```

If you've already caused a collision, resync the sequence to the table's
actual max before the app tries to insert again:

```sql
select setval('ab_permission_view_role_id_seq', (select max(id) from ab_permission_view_role));
```

**`{"message":{"provider":["Alternative authentication provider is not
allowed"]}}`** on `/api/v1/security/login` — you passed `"provider":"db"`
while `AUTH_TYPE = AUTH_LDAP` is set globally; the app only accepts
`"provider":"ldap"` (or omit `provider`) once LDAP is the configured auth
type. Any pre-existing local Superset user can still be reached this way if
their **username** matches an LDAP entry — logging in as `admin`/LDAP
authenticates against the LDAP directory, then reuses/updates Superset's
existing local `admin` row by username.

**A POST to `/api/v1/security/roles/` or `/api/v1/dataset/` returns `"The
CSRF session token is missing"` even with a valid `X-CSRFToken` header** —
fetch the CSRF token and make the follow-up request through the *same
cookie jar* (`curl -c cookies.txt -b cookies.txt ...` for both calls). The
CSRF token is paired with a session cookie set by the
`/api/v1/security/csrf_token/` call; sending only the JWT and the header
without that cookie fails validation on some endpoints.

**Kubelet reports `DiskPressure` and evicts pods (including, confusingly,
unrelated ones like `postgresql`/`redis`) while building a large custom
image on the same node** — image builds and `docker save`/`ctr import`
tarballs can transiently consume tens of GB. If the node crosses kubelet's
`imagefs`/`nodefs` high-watermark (commonly ~85%), it starts garbage
collecting *and* evicting, and can reclaim images you just imported before
any pod uses them (see Part B). Prune build cache (`docker builder prune
-af`) between builds, and clean up stale `Evicted`/`ContainerStatusUnknown`
pods across the cluster (`kubectl delete pods --field-selector
status.phase=Failed`) to let kubelet reclaim their leftover container
state — this is often enough to bring usage back under the threshold
without touching other projects' images.
