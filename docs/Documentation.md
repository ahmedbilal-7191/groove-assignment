# Voltra Retail — Technical Documentation (Task 1)

## 1. The problem, restated
The auditor asked two things: *how fast would a stolen DB credential stop working* and *how would you know it was stolen*. This solution answers both with dynamic, short-lived Postgres credentials issued by Vault and delivered to the app via the Vault Agent Injector, monitored entirely through Vault's own Prometheus telemetry.

## 2. Architecture

```
 namespace: db                    namespace: vt                      namespace: app
┌───────────────────┐   admin   ┌────────────────────┐  k8s auth  ┌───────────────────────┐
│ Bitnami postgresql │◀─────────│ Vault (dev mode)    │◀───────────│ webapp pod             │
│ (Helm)             │  conn    │ + Agent Injector    │            │  ┌──────────────────┐  │
│  role: postgres    │          │ webhook             │──injects──▶│  │ vault-agent-init │  │
│  (superuser, used   │          │                     │            │  │ vault-agent-sidecar│  │
│  directly by Vault) │          │ database secrets    │            │  │  writes            │  │
│  db: appdb          │          │ engine: db-app-role │            │  │  /vault/secrets/   │  │
│  table: products    │          │ ttl=max_ttl=2m       │            │  │  db-creds          │  │
└─────────▲───────────┘          └─────────┬───────────┘            │  └─────────┬──────────┘  │
          │                                │                        │  ┌─────────▼──────────┐  │
          │ ephemeral v-* role connects    │ /v1/sys/metrics        │  │ webapp (FastAPI)    │  │
          └────────────────────────────────┼────────────────────────┼─▶│  polls the file,     │  │
                                            │                        │  │  reconnects, serves  │  │
                                            ▼                        │  │  /inventory etc.     │  │
                              namespace: monitoring                  │  └─────────────────────┘  │
                              ┌────────────────────────┐             └────────────┬──────────────┘
                              │ kube-prometheus-stack   │                          │
                              │ Prometheus Operator     │◀── NodePort 30080 ───────┘
                              │ + Prometheus + Grafana  │
                              │ ServiceMonitor + Rules  │
                              └────────────────────────┘
```

* **Postgres 16** via `bitnami/postgresql` (ns `pg-database`). `schema.sql` runs once at first boot (`primary.initdb.scripts`), creating the `products` table, seeding demo rows, and granting `SELECT/INSERT/UPDATE/DELETE` on all current *and future* tables to `PUBLIC` — the mechanism that lets every ephemeral Vault-issued role read/write without per-role grants.
* **Vault** (ns `vault`) via `hashicorp/vault` in **dev mode** (`server.dev.enabled=true`) with the **Agent Injector** enabled. Dev mode means: unsealed automatically, in-memory storage, and a fixed root token, literally `"root"`. This is explicitly a demo-only mode — see 6.
* **Vault Agent Injector**: a mutating admission webhook. It reads the `vault.hashicorp.com/*` annotations on the `webapp` Deployment's pod template and, with no YAML we wrote ourselves for it, adds an **init container** (populates the credential file before `webapp` starts) and a **sidecar container** (keeps it fresh for the pod's lifetime).
* **webapp** (FastAPI, ns `app`): never talks to Vault and holds no Vault token. It only reads `/vault/secrets/db-creds`.
* **kube-prometheus-stack** (ns `monitoring`): Prometheus Operator, Prometheus, and Grafana. A `ServiceMonitor` tells Prometheus to scrape Vault's `/v1/sys/metrics`;


## 3. How each requirement is met

### 3.1 Credentials expire on their own

The role `db-app-role` has `default_ttl=2m` and `max_ttl=2m`. I set both to the same value on purpose. Since they are equal, the credential is already at its maximum when it is issued, so there is nothing left to extend and Vault can't really renew it. Every cycle gives a completely new username and password instead of a renewal of the old one.

I think this matches the assignment wording better ("a credential expires **and a new one is issued**") than a design that tries to renew first. The trade-off is more Postgres role churn, because a `CREATE ROLE` / `DROP ROLE` pair happens roughly every 2 minutes. Also, the "Credential Renewals" panel (panel 4) will show little or no activity. That is expected, it is not broken.

There are two separate layers that enforce the expiry:
1. **Vault lease expiry** runs the `revocation_statements`: terminate the role's Postgres sessions, revoke its privileges, then `DROP ROLE`.
2. **`VALID UNTIL '{{expiration}}'`** in `creation_statements`. Postgres itself refuses new logins after the deadline, even if Vault is unreachable.

### 3.2 The app keeps working when the credential changes (no restart, no manual step)

All of this is handled in `main.py`:

1. **Startup**: it waits up to 90s for `/vault/secrets/db-creds` to appear, then calls `reload_if_changed(force=True)` and connects. After that it starts a background thread (`poll_credentials`) that calls `reload_if_changed()` every `CREDS_POLL_SECONDS` (2s).
2. **Detecting a change**: `reload_if_changed` compares the parsed username and password with the ones currently in use. It does not look at the file's mtime, so if the file is rewritten with the same content, nothing happens.
3. **Swapping**: when the credentials really changed, it opens a new connection with the new credentials, closes the old one, and updates `_conn`, `_username` and `_password`. All of this happens inside `_lock`.
4. **Using the connection**: the `db()` context manager takes `_lock` only to read the `_conn` pointer, then releases it and hands the connection to the request handler. So the lock is never held while a query runs.
5. **Recovering from a failure**: if a query raises `psycopg.OperationalError` (for example the credential was revoked just before the file was updated), the `except` in `db()` calls `reload_if_changed(force=True)` and re-raises. That one request fails, but the next request already has a working connection.

One thing I should point out: the reconnect itself (`_connect(...)`, a real network call to Postgres) happens inside `_lock` in `reload_if_changed`. So a request that arrives exactly during a swap can wait for the reconnect, usually a few tens of milliseconds, instead of being unaffected. For this demo I only need to prove the app keeps working across a change, not that there is zero latency, so I kept it simple. It is a known trade-off, not a mistake.

### 3.3 No secrets committed

The `helm` commands in the assignment had the Postgres passwords written as plain text, which is exactly what "no secrets committed" is against. I fixed that in this package. I also did not use automation scripts for the deployment, to keep things simple.

Vault's dev-mode root token (`"root"`) is a separate known limitation. I kept it as is because this is only an assignment task .

### 3.4 Credential activity is visible (Vault's own metrics)

There is no instrumentation in the app. `main.py` has no `/metrics` endpoint and imports no metrics library. Every number in Grafana comes from Vault's `/v1/sys/metrics`, which Prometheus scrapes through the `ServiceMonitor`.

Two kinds of metrics are used, and the difference matters:

| Metric | Scope | What it measures |
|---|---|---|
| `vault_secret_lease_creation{mount_point,secret_engine}` | secrets-engine level | A new lease was created, meaning a brand-new username/password |
| `vault_route_read_database__count` | mount-scoped (`database/` becomes `database_`) | Every `read` request to the database secrets engine. In this demo it almost matches the line above 1:1, because issuing a credential is a read on that mount |
| `vault_route_renew_database__count` | mount-scoped | Every `renew` request to the database mount |
| `vault_expire_lease_expiration` | global, not mount-scoped | A lease expiring anywhere in the Vault instance |
| `vault_expire_num_leases` | global gauge | Number of active leases in the whole Vault instance |

The mount-scoped `vault_route_*` metrics are better than the `vault_expire_*` ones because they show only the activity of this one dynamic secret, separate from anything else Vault is doing.

## 4. End-to-end request walkthrough (example timestamps)

Example run with `default_ttl=max_ttl=2m`, one `webapp` pod and `CREDS_POLL_SECONDS=2`:

```
t=00:00.0   Pod starts. Init container "vault-agent-init" logs in to Vault
            (kubernetes auth, role webapp-role) and asks for
            database/creds/db-app-role.
t=00:00.4   Vault runs CREATE ROLE "v-db-app-role-a1b2c3" ... VALID UNTIL '00:02:00.4';
            GRANT ALL PRIVILEGES ON DATABASE appdb TO "v-db-app-role-a1b2c3";
            and returns {username: v-db-app-role-a1b2c3, password: ..., lease_duration: 120}.
            The init container writes /vault/secrets/db-creds and exits.
t=00:00.5   webapp container starts. The FastAPI startup handler finds the file
            right away and calls reload_if_changed(force=True). It connects as
            v-db-app-role-a1b2c3, checks with the query used in /health, and
            starts the poll_credentials thread.
t=00:00.6   Pod becomes Ready (readinessProbe /health passes). The "vault-agent"
            sidecar keeps running next to webapp for the life of the pod.
t=00:15.0   GET /inventory: db() takes _lock, reads _conn (still the
            v-db-app-role-a1b2c3 connection), releases _lock, runs the query and
            returns {"queried_as": "v-db-app-role-a1b2c3", "items": [...]}.
t=01:58.0   The lease is in its last ~2s. The sidecar has nothing to extend
            (default_ttl == max_ttl), so it reads database/creds/db-app-role again.
            Vault runs CREATE ROLE "v-db-app-role-d4e5f6" ... and the sidecar
            rewrites /vault/secrets/db-creds with the new username/password.
            vault_secret_lease_creation goes up again.
t=01:58.0   On its next tick (every 2s), poll_credentials reads the file and sees the
            username changed from v-db-app-role-a1b2c3 to v-db-app-role-d4e5f6. It
            calls reload_if_changed, connects as the new user, closes the old
            connection and updates _username/_password under _lock.
t=01:58.1   A request in flight at this moment waits briefly behind _lock (the
            reconnect happens inside the lock, see 3.2). That is tens of
            milliseconds, not a failure.
t=02:00.4   The OLD lease (v-db-app-role-a1b2c3) reaches VALID UNTIL and gets
            revoked. Postgres ends any leftover session under that name and the
            role is dropped. The app already moved away from it 2 seconds earlier,
            so the user sees nothing.
t=02:15.0   GET /inventory now returns {"queried_as": "v-db-app-role-d4e5f6", ...}.
            No restart happened anywhere. kubectl -n app get pods shows
            RESTARTS: 0 the whole time.
```

That is the proof: `/inventory` keeps returning successful responses, the `queried_as` value changes once per TTL window, there are no restarts and no manual steps, and the change shows up in Vault's `vault_secret_lease_creation` metric at the same time.

## 5. Limitations (demo vs production)

* **Vault dev mode**: single node, data stored in memory (everything is lost if the pod restarts), plain HTTP, and a fixed, publicly known root token (`"root"`) and unseal key.
* **Vault holds the Postgres superuser credential** (`username=postgres`). A separate role with only `CREATEROLE` could have been used to reduce the damage if it leaks.
* **`GRANT ... TO PUBLIC` and `ALTER DEFAULT PRIVILEGES ... TO PUBLIC`** in `schema.sql` are very broad, so any role, dynamic or not, gets full CRUD on every table. This is easy for a demo where roles are created on the fly. In production I would grant to the dynamic role name pattern only.
* **`default_ttl == max_ttl`** means no real in-place renewal ever happens. There is more role churn than a renew-first design, and the "Credential Renewals" panel looks empty. I documented it above so it doesn't look like a bug.
* **`vault_expire_lease_expiration`** (panel 3) is not mount-scoped. If Vault had other secrets engines in use, their expirations would be counted too. It is fine here because this Vault only manages this one dynamic secret.
* **Reconnect inside the lock** in `main.py` (see 3.2) costs a small stall during a swap in exchange for simpler code.

## 6. What I would do with more time / a production budget

* **App design**: add DB connection pooling and handle in-flight requests properly when the secret rotates. I would keep two pools, and when the credentials change, new requests go to the new pool while the old one finishes its running requests and is then closed before its credential expires.
* **HA Vault** (Raft, 3-5 nodes) instead of dev mode, with a proper init/unseal flow using a KMS, TLS everywhere, the root token not used after the initial setup, and audit devices sending logs to a SIEM.
* **A dedicated Postgres admin role** with limited permissions for Vault, and `rotate-root` applied to that account instead of `postgres`.
* **Replace `GRANT ... TO PUBLIC`** with grants scoped to the dynamic role name pattern.
* **Make `max_ttl` larger than `default_ttl`** if I want to show in-place renewal alongside new-issuance.
* **Alertmanager routing**: right now the `PrometheusRule` alerts only appear in the Prometheus UI. Sending them to Slack through Alertmanager (already part of kube-prometheus-stack) is a small extra step. I would also match a spike with Vault's audit log to see who requested the credentials, not just how many.








## 3. How each requirement is met

### 3.1 Credentials expire on their own
Role `db-app-role`: `default_ttl=2m`, `max_ttl=2m`. **Important and deliberate detail:** because these two values are equal, a credential is issued already at its ceiling — there is no room to extend it, so Vault cannot meaningfully renew it. Every cycle produces a **genuinely new username and password**, not a renewal of the old one.reading of the assignment's wording ("a credential expires **and a new one is issued**") than a renew-first design. The trade-off: more Postgres role churn (a new `CREATE ROLE`/`DROP ROLE` pair roughly every 2 minutes instead of every renewal-cycle boundary), and — see panel4 — the "Credential Renewals" panel will show little to no activity, which is expected, not broken.

Two independent enforcement layers:
1. **Vault lease expiry** runs `revocation_statements`: terminate the role's Postgres sessions, revoke its privileges, `DROP ROLE`.
2. **`VALID UNTIL '{{expiration}}'`** in `creation_statements` — Postgres itself refuses new logins past the deadline even if Vault were unreachable.

### 3.2 The app keeps working across a credential change — no restart, no manual step
This is `main.py`'s job end to end:
1. **Startup**: polls for `/vault/secrets/db-creds` up to 90s, then calls `reload_if_changed(force=True)`, connects, and starts a background thread (`poll_credentials`) that calls `reload_if_changed()` every `CREDS_POLL_SECONDS` (2s).
2. **Detecting a real change**: `reload_if_changed` compares the *parsed username and password values* against what's currently active — not the file's mtime — so it correctly ignores a file write that happens to contain identical content.
3. **Swap**: on an actual change, it opens a **new** connection with the new credentials, closes the old connection, and updates `_conn`/`_username`/`_password` — all **inside** `_lock`.
4. **Using the connection**: the `db()` context manager acquires `_lock` **only long enough to read the `_conn` pointer**, releases it, then yields the connection for the request handler to use — so the lock is never held across an actual query.
5. **Recovery from a mid-flight failure**: if a query raises `psycopg.OperationalError` (e.g. the credential was revoked slightly ahead of the file catching up), the `db()` context manager's `except` clause calls `reload_if_changed(force=True)` and re-raises; the caller sees one failed request, and the *next* request already has a working connection.

**One honest nuance worth being able to explain on camera:** the reconnect itself (`_connect(...)`, a real network round-trip to Postgres) happens **inside** `_lock` in `reload_if_changed`, not outside it. That means a request arriving at the exact moment of a swap can stall for the duration of that reconnect (typically tens of milliseconds) rather than being completely unaffected. This is a reasonable simplification for a demo proving *continuity*, not *zero-latency concurrency* — but it is a deliberate trade-off, not an oversight, and you should be able to name it if asked.

### 3.3 No secrets committed
Fixed in this package (the assignment's own pasted `helm` commands had the Postgres passwords as plaintext literals, which is exactly what "no secrets committed" prohibits),didnt used automation scripts to automate the deployment to keep it simple

while but for assignmetn task keep it Vault's dev-mode root token (`"root"`) is a separate, known limitation 

### 3.4 Credential activity is visible (Vault's own metrics)
No application-side instrumentation anywhere — `main.py` exposes no `/metrics` endpoint and imports no metrics library. Every number in Grafana comes from Vault's `/v1/sys/metrics`, scraped by the Prometheus Operator via the `ServiceMonitor`.

Two metric families are in play, and it's worth being precise about the difference, :

| Metric | Scope | What it measures |
|---|---|---|
| `vault_secret_lease_creation{mount_point,secret_engine}` | secrets-engine level | A **new** lease created — i.e. a brand-new username/password |
| `vault_route_read_database__count` | **mount-scoped** (`database/` → `database_`) | Every `read` request routed to the database secrets engine — overlaps almost 1:1 with the line above in this demo, since issuing a credential *is* a read on that mount |
| `vault_route_renew_database__count` | **mount-scoped** | Every `renew` request routed to the database mount |
| `vault_expire_lease_expiration` | **global**, not mount-scoped | A lease expiring, across the whole Vault instance |
| `vault_expire_num_leases` | global gauge | Currently active leases, across the whole Vault instance |

The mount-scoped `vault_route_*` metrics are a genuine improvement over the `vault_expire_*` family for isolating *this specific* dynamic secret's activity from anything else Vault might be doing

## 5. End-to-end request walkthrough, with example timestamps

Concrete run, `default_ttl=max_ttl=2m`, one `webapp` pod, `CREDS_POLL_SECONDS=2`:

```
t=00:00.0   Pod starts. Init container "vault-agent-init" logs in to Vault (kubernetes
            auth, role webapp-role) and requests database/creds/db-app-role.
t=00:00.4   Vault: CREATE ROLE "v-db-app-role-a1b2c3" ... VALID UNTIL '00:02:00.4';
            GRANT ALL PRIVILEGES ON DATABASE appdb TO "v-db-app-role-a1b2c3";
            Returns {username: v-db-app-role-a1b2c3, password: ..., lease_duration: 120}.
            Init container writes /vault/secrets/db-creds and exits.
t=00:00.5   webapp container starts. FastAPI @app.on_event("startup") finds the file
            immediately, calls reload_if_changed(force=True): connects as
            v-db-app-role-a1b2c3, verifies with the query in @app.get("/health"),
            starts the poll_credentials background thread.
t=00:00.6   Pod becomes Ready (readinessProbe /health passes). Sidecar "vault-agent"
            keeps running alongside webapp for the pod's lifetime.
t=00:15.0   GET /inventory -> db() acquires _lock, reads _conn (already the
            v-db-app-role-a1b2c3 connection), releases _lock, runs the query, returns
            {"queried_as": "v-db-app-role-a1b2c3", "items": [...]}.
t=01:58.0   Vault: the lease is now within its last ~2s. The sidecar's internal renewer
            has nothing left to extend (default_ttl == max_ttl), so it re-reads
            database/creds/db-app-role instead. Vault: CREATE ROLE
            "v-db-app-role-d4e5f6" ...; sidecar rewrites /vault/secrets/db-creds with
            the new username/password. vault_secret_lease_creation increments again.
t=01:58.0   poll_credentials's next tick (every 2s) reads the file, sees username
            changed from v-db-app-role-a1b2c3 to v-db-app-role-d4e5f6, calls
            reload_if_changed: connects as the new user, closes the old connection,
            updates _username/_password under _lock.
t=01:58.1   Any request in flight at this exact instant briefly stalls behind _lock
            (the reconnect happens inside the lock - see §3.2) - on the order of tens
            of milliseconds, not a failure.
t=02:00.4   The OLD lease (v-db-app-role-a1b2c3) hits its VALID UNTIL / gets revoked:
            Postgres terminates any lingering session under that name and the role is
            dropped. By this point the app switched away from it two seconds earlier,
            so nothing user-visible happens.
t=02:15.0   GET /inventory -> now returns {"queried_as": "v-db-app-role-d4e5f6", ...}.
            No restart occurred anywhere in this timeline; kubectl -n app get pods
            would show RESTARTS: 0 throughout.
```

That's the whole proof: a continuous stream of successful `/inventory` calls whose `queried_as` field changes exactly once per TTL window, with zero restarts, zero manual steps, and the change itself visible in Vault's own `vault_secret_lease_creation` metric at the same moment.

## 6. Honest limitations (demo vs production)
* **Vault dev mode**: single node, in-memory storage (all data lost on pod restart), plain HTTP, and a fixed, publicly-documented root token (`"root"`) and unseal.
* **Vault holds the Postgres superuser credential directly** (`username=postgres`). A dedicated `CREATEROLE' different role could have been used here to narrow the blast radius
* **`GRANT ... TO PUBLIC` with `ALTER DEFAULT PRIVILEGES ... TO PUBLIC`** in `schema.sql` is broad by design (any role, dynamic or not, gets full CRUD on every table). Convenient for a demo where roles are created on the fly; in production you'd grant to the specific dynamic role name pattern instead.
* **`default_ttl == max_ttl`** means no true in-place renewal ever happens — more Postgres role churn than a renew-first design, and the "Credential Renewals" panel will be visually unexciting. Documented above so it doesn't look like a bug on camera.
* **`vault_expire_lease_expiration`** (Panel 3) is not mount-scoped — in a Vault instance with other secrets engines in active use, it would include their expirations too. Fine here since this Vault only manages this one dynamic secret.
* **The reconnect-inside-the-lock** behavior in `main.py` (§3.2) trades a small amount of stall time during a swap for simpler code — worth knowing, not worth fixing for this assignment.

## 7. What I'd do with more time / a production budget
the application design i would add db pooling and concurrency ahndlin gracefully handling the inflight request if the secrets rotate during that time maintng 2 pool and shifting thee older to another when the creds expire in robust way
* HA Vault (Raft, 3-5 nodes) instead of dev mode, real unseal/init flow with a KMS, TLS everywhere, root token never used after initial setup, audit devices to a SIEM.
* A dedicated, narrowly-scoped Postgres admin role for Vault instead of the superuser directly, with `rotate-root` applied to *that* account, not `postgres`.
* Replace `GRANT ... TO PUBLIC` with grants scoped to the dynamic role name pattern.
* Widen `max_ttl` beyond `default_ttl` if you specifically want to demonstrate in-place renewal behavior alongside new-issuance behavior.
* Alertmanager routing (the `PrometheusRule` alerts currently just sit in Prometheus's UI; wiring them to Slack via Alertmanager, already included in kube-prometheus-stack, is a small additional step) and correlating a spike with Vault's audit log for *who* requested the credentials, not just *how many*.
