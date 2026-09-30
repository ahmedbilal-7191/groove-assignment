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

* **Postgres 16** via `bitnami/postgresql` (ns `db`). `schema.sql` runs once at first boot (`primary.initdb.scripts`), creating the `products` table, seeding demo rows, and granting `SELECT/INSERT/UPDATE/DELETE` on all current *and future* tables to `PUBLIC` — the mechanism that lets every ephemeral Vault-issued role read/write without per-role grants.
* **Vault** (ns `vt`) via `hashicorp/vault` in **dev mode** (`server.dev.enabled=true`) with the **Agent Injector** enabled. Dev mode means: unsealed automatically, in-memory storage, and a fixed root token, literally `"root"`. This is explicitly a demo-only mode — see §6.
* **Vault Agent Injector**: a mutating admission webhook. It reads the `vault.hashicorp.com/*` annotations on the `webapp` Deployment's pod template and, with no YAML we wrote ourselves for it, adds an **init container** (populates the credential file before `webapp` starts) and a **sidecar container** (keeps it fresh for the pod's lifetime).
* **webapp** (FastAPI, ns `app`): never talks to Vault and holds no Vault token. It only reads `/vault/secrets/db-creds`.
* **kube-prometheus-stack** (ns `monitoring`): Prometheus Operator, Prometheus, and Grafana. A `ServiceMonitor` tells Prometheus to scrape Vault's `/v1/sys/metrics`; a `PrometheusRule` turns the same metrics into alerts.

## 3. How each requirement is met

### 3.1 Credentials expire on their own
Role `db-app-role`: `default_ttl=2m`, `max_ttl=2m`. **Important and deliberate detail:** because these two values are equal, a credential is issued already at its ceiling — there is no room to extend it, so Vault cannot meaningfully renew it. Every cycle produces a **genuinely new username and password**, not a renewal of the old one. This is a valid, arguably *more literal*, reading of the assignment's wording ("a credential expires **and a new one is issued**") than a renew-first design. The trade-off: more Postgres role churn (a new `CREATE ROLE`/`DROP ROLE` pair roughly every 2 minutes instead of every renewal-cycle boundary), and — see §4 — the "Credential Renewals" panel will show little to no activity, which is expected, not broken.

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
Fixed in this package (the assignment's own pasted `helm` commands had the Postgres passwords as plaintext literals, which is exactly what "no secrets committed" prohibits): `scripts/deploy.sh` now generates `POSTGRES_ROOT_PASSWORD` and `APPUSER_PASSWORD` with `openssl rand` into `.secrets/postgres-passwords` (gitignored, `chmod 600`), and passes them to both the Helm install and `vault/setup-vault.sh`. **Use this version's `deploy.sh`, not the raw commands from the assignment prompt, if you want to actually satisfy this requirement.** `.gitignore` also blocks `*.env`/`__pycache__`; `scripts/check-secrets.sh` scans the full working tree and git history for anything secret-looking.

Vault's dev-mode root token (`"root"`) is a separate, known limitation — see §6, it is not something a `.gitignore` can fix.

### 3.4 Credential activity is visible (Vault's own metrics)
No application-side instrumentation anywhere — `main.py` exposes no `/metrics` endpoint and imports no metrics library. Every number in Grafana comes from Vault's `/v1/sys/metrics`, scraped by the Prometheus Operator via the `ServiceMonitor`.

Two metric families are in play, and it's worth being precise about the difference, since I got this wrong in an earlier iteration of this project before checking:

| Metric | Scope | What it measures |
|---|---|---|
| `vault_secret_lease_creation{mount_point,secret_engine}` | secrets-engine level | A **new** lease created — i.e. a brand-new username/password |
| `vault_route_read_database__count` | **mount-scoped** (`database/` → `database_`) | Every `read` request routed to the database secrets engine — overlaps almost 1:1 with the line above in this demo, since issuing a credential *is* a read on that mount |
| `vault_route_renew_database__count` | **mount-scoped** | Every `renew` request routed to the database mount |
| `vault_expire_lease_expiration` | **global**, not mount-scoped | A lease expiring, across the whole Vault instance |
| `vault_expire_num_leases` | global gauge | Currently active leases, across the whole Vault instance |

The mount-scoped `vault_route_*` metrics are a genuine improvement over the `vault_expire_*` family for isolating *this specific* dynamic secret's activity from anything else Vault might be doing — worth calling out as a deliberate choice in the video, not a random pick.

### 3.5 Unusual activity stands out — not a single number
Two layers:
1. **The 5 Grafana panels** (built with `ServiceMonitor` + Vault's own metrics — see §4 for the full walkthrough) are time series, not single numbers, so a human watching during the burst demo sees a clear spike against a flat baseline.
2. **`k8s/monitoring/prometheusrule.yaml`** (added in this package, optional but recommended) turns the same distinction into an automatic, explicit alert: `DatabaseCredentialRequestSpike` fires when new-credential issuance exceeds ~6x the expected baseline, `NoCredentialActivity` fires if nothing has happened for 10+ minutes (the Injector silently stopped), and `VaultLeaseCountElevated` catches an abnormal buildup of active leases. This directly answers the assignment's "not just a single number with no way to tell what's expected" wording with something automated, not just visual judgment during a live demo.

## 4. Grafana panels — what each one shows and how to narrate it

| # | Title | PromQL | What "normal" looks like here | What to say on camera |
|---|---|---|---|---|
| 1 | Dynamic Credentials Issued | `sum by (creation_ttl)(rate(vault_secret_lease_creation{mount_point="database/",secret_engine="database"}[5m]))` | A low, roughly flat line — about one issuance every 2 minutes per pod (~0.008/s) | "This is the primary signal: every time Vault mints a brand-new username/password, it shows here. With one pod and a 2-minute TTL, normal looks like a near-flat line close to zero." |
| 2 | Credential Renewals | `rate(vault_route_renew_database__count[5m])` | **Near zero** — expected, not a bug (see §3.1: `default_ttl == max_ttl` leaves no room to renew) | "You'll notice this stays close to zero. That's expected given our TTL config: since default_ttl equals max_ttl, there's no time left to renew, so Vault issues a fresh credential instead. I'm showing this panel anyway because it proves that — an empty panel here is itself informative, not a missing feature." |
| 3 | Lease Expirations | `rate(vault_expire_lease_expiration[5m])` | Tracks Panel 1 with a ~2-minute lag (each issued lease eventually expires) | "This mirrors issuance, offset by the TTL — every credential that gets created here eventually shows up as an expiration two minutes later. That's the 'stops working on its own' half of the auditor's question, visible as data, not a claim." |
| 4 | Active Leases | `vault_expire_num_leases` | A small, stable number — roughly 1-2 | "This should hover very low and flat. A sustained climb would mean credentials are being requested faster than they're expiring — worth watching during the burst demo." |
| 5 | Database Requests | `rate(vault_route_read_database__count[5m])` | Nearly identical to Panel 1 | "This is an independent metric family from Panel 1, but it should move in lockstep with it here, because in this demo issuing a credential *is* the read request. I'm keeping both up because they come from two different parts of Vault's internals agreeing with each other — that's useful corroboration, not redundancy." |

**During the burst demo (`demo-3-anomaly-burst.sh`):** narrate Panels 1 and 5 spiking together sharply above their flat baseline, Panel 4 climbing as the rogue pod's credentials pile up faster than they expire, and — if you applied the `PrometheusRule` — show the `DatabaseCredentialRequestSpike` alert transitioning to firing in Prometheus's Alerts tab at the same moment. That combination (visual spike + an actual alert firing) is the strongest way to demonstrate "distinguishes normal from abnormal" on camera.

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
* **Vault dev mode**: single node, in-memory storage (all data lost on pod restart), plain HTTP, and a fixed, publicly-documented root token (`"root"`). This is explicitly a throwaway-demo mode; never use it for anything with real secrets.
* **Vault holds the Postgres superuser credential directly** (`username=postgres`). A dedicated `CREATEROLE`-only admin account (as in the design we discussed earlier in this project) would be narrower blast-radius if Vault's own config were ever exposed. Rotating the `postgres` superuser's password via `vault write -f database/rotate-root/postgres` would be **more dangerous** here than in that earlier design, since it's the actual database superuser — deliberately not done in `setup-vault.sh`.
* **`GRANT ... TO PUBLIC` with `ALTER DEFAULT PRIVILEGES ... TO PUBLIC`** in `schema.sql` is broad by design (any role, dynamic or not, gets full CRUD on every table). Convenient for a demo where roles are created on the fly; in production you'd grant to the specific dynamic role name pattern instead.
* **`default_ttl == max_ttl`** means no true in-place renewal ever happens — more Postgres role churn than a renew-first design, and the "Credential Renewals" panel will be visually unexciting. Documented above so it doesn't look like a bug on camera.
* **`vault_expire_lease_expiration`** (Panel 3) is not mount-scoped — in a Vault instance with other secrets engines in active use, it would include their expirations too. Fine here since this Vault only manages this one dynamic secret.
* **The reconnect-inside-the-lock** behavior in `main.py` (§3.2) trades a small amount of stall time during a swap for simpler code — worth knowing, not worth fixing for this assignment.
* **kube-prometheus-stack is heavy** for a minikube demo (node-exporter, kube-state-metrics, Alertmanager, etc. all come along by default). `scripts/deploy.sh` starts minikube with more headroom (`--cpus=4 --memory=6144`) than a minimal Prometheus+Grafana pair would need; trim further with `--set nodeExporter.enabled=false --set kubeStateMetrics.enabled=false` if resources are tight.

## 7. What I'd do with more time / a production budget
* HA Vault (Raft, 3-5 nodes) instead of dev mode, real unseal/init flow with a KMS, TLS everywhere, root token never used after initial setup, audit devices to a SIEM.
* A dedicated, narrowly-scoped Postgres admin role for Vault instead of the superuser directly, with `rotate-root` applied to *that* account, not `postgres`.
* Replace `GRANT ... TO PUBLIC` with grants scoped to the dynamic role name pattern.
* Widen `max_ttl` beyond `default_ttl` if you specifically want to demonstrate in-place renewal behavior alongside new-issuance behavior.
* Alertmanager routing (the `PrometheusRule` alerts currently just sit in Prometheus's UI; wiring them to Slack/PagerDuty via Alertmanager, already included in kube-prometheus-stack, is a small additional step) and correlating a spike with Vault's audit log for *who* requested the credentials, not just *how many*.
