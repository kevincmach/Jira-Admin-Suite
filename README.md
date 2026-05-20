# Jira Admin Suite

Jira Data Center admin suite for managing project roles, user assignments, and related admin tasks.

This tool lets you define **policies** like:

> For all in-scope projects, role `02 - Mission Assurance Team` must contain exactly this set of people (and no one else).

It includes:

- A Python **core + CLI** for policy evaluation and enforcement.
- A FastAPI **REST API** (`http://localhost:8000`) for programmatic access.
- An optional Flask-based **web UI** (this branch) to browse policies, see diffs, and apply changes.

> **Important:** This tool can modify Jira project role assignments. Always start with **dry runs** and use a **test Jira instance** first.

---

## 1. Prerequisites

### Jira

- Jira **Data Center** instance with REST APIs enabled.
- A **service account** with admin permissions to:
  - Read projects.
  - Read users.
  - Read and modify project roles.

### WSL / Environment

Commands below assume:

- You are using **WSL** (Ubuntu) and the repo is at:
  
  ```bash
  /home/kevin/tools/jira-admin-suite
  ```

### Software

- Python **3.10+**
- `pip`
- Node.js + `npm` (for the UI)

On Ubuntu/WSL you can install basics like this:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
# For Node (example for Node 20)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
```

---

## 2. Project Layout

Key files and directories:

- `pyproject.toml` – Python package config + CLI entrypoint.
- `config/`
  - `config.example.yaml` – example configuration.
  - `config.yaml` – **your** config (you create this from the example).
- `core/`
  - `config.py` – config models + loader.
  - `jira_client.py` – Jira REST client.
  - `user_discovery.py` – user inventory + heuristic mapping.
  - `policy_engine.py` – project filtering, diff logic, and apply.
- `cli/`
  - `main.py` – `jira-admin-suite` CLI implementation.
- `api/`
  - `main.py` – FastAPI app exposing the policy engine.
  (Web UI)
  - Flask-based web UI (admin dashboard, implemented separately in this branch).

---

## 3. Initial Setup

### 3.1 Clone and enter the repo

If you haven’t already cloned it:

```bash
cd /home/kevin/tools
# git clone <your-repo-url> jira-admin-suite
cd jira-admin-suite
```

### 3.2 Python virtual environment

Create and activate a virtualenv (recommended):

```bash
cd /home/kevin/tools/jira-admin-suite
python3 -m venv .venv
source .venv/bin/activate
```

Install the Python package and API dependencies:

```bash
pip install -e . "fastapi[standard]" uvicorn
```

### 3.3 Create and edit `config/config.yaml`

Copy the example config:

```bash
cp config/config.example.yaml config/config.yaml
```

Open `config/config.yaml` in your editor (e.g. `nano config/config.yaml`) and update:

- `jira.base_url`: your Jira DC URL, e.g. `https://jira.yourcompany.com`
- `jira.auth.mode`: `"basic"` or `"token"`
- Env var names used for credentials.

Example basic-auth config:

```yaml
jira:
  base_url: "https://jira.yourcompany.com"
  auth:
    mode: "basic"
    username_env_var: "JIRA_USERNAME"
    password_env_var: "JIRA_PASSWORD"
    token_env_var: "JIRA_TOKEN"  # unused in basic mode

project_filters:
  include_keys: []
  exclude_keys: []
  include_types: []
  exclude_types: []
  match_key_regex: null
  match_name_regex: null

policies:
  - id: mission_assurance_team
    name: 02 - Mission Assurance Team Policy
    role_name: "02 - Mission Assurance Team"
    project_filters:
      match_key_regex: "^[A-Z]+$"
      exclude_keys:
        - TEST
        - SANDBOX
    user_source:
      mode: static_list
      identifier_type: username
      users:
        - alice.smith
        - bob.jones
        - carol.lee
    matching_rules:
      use_firstname_lastname: true
      email_domain_whitelist:
        - example.com
    enforcement:
      mode: enforce_exact
      allow_groups: true
      tolerate_missing_users: true
      max_changes_per_project: 10
```

Set the environment variables for Jira credentials (inside your WSL shell):

```bash
export JIRA_USERNAME='your-service-account'
export JIRA_PASSWORD='your-password'
# or, if using token mode:
# export JIRA_TOKEN='your-token'
```

You can put these in `~/.bashrc` if you want them to persist.

---

## 4. CLI Usage

Once installed (`pip install -e .`) and with your virtualenv active, you can use the CLI.

**Global note:** all commands accept a global `--config` option on the root command (defaults to `config/config.yaml` if omitted). The `--config` flag must come **before** the subcommand, e.g. `jira-admin-suite --config config/config.yaml config validate`.

### 4.1 Validate configuration and Jira connectivity

```bash
jira-admin-suite --config config/config.yaml config validate
```

This will:

- Parse `config.yaml` and validate required fields.
- Attempt to connect to Jira and list projects.
- Print something like:

```text
Config OK. Jira base_url=https://jira.yourcompany.com, projects=123
```

If there is a configuration or connectivity issue, it will print a clear error.

### 4.2 List policies

```bash
jira-admin-suite --config config/config.yaml policies list
```

Example output:

```text
mission_assurance_team	02 - Mission Assurance Team	enforce_exact
```

### 4.3 List projects

List all projects Jira exposes:

```bash
jira-admin-suite --config config/config.yaml projects list

List projects in scope for a specific policy:

```bash
jira-admin-suite --config config/config.yaml projects list --policy mission_assurance_team

### 4.4 Policy dry-run (no changes)

Dry-run a policy across all in-scope projects:

```bash
jira-admin-suite --config config/config.yaml policy dry-run mission_assurance_team

Dry-run for a single project key:

```bash
jira-admin-suite --config config/config.yaml policy dry-run mission_assurance_team \
  --project PROJKEY
```

JSON output (for logging/automation):

```bash
jira-admin-suite --config config/config.yaml policy dry-run mission_assurance_team \
  --json > plan.json
```

Dry-run output includes:

- Total additions/removals.
- Unresolved and ambiguous users.
- Per-project diffs (users to add/remove, warnings).

### 4.5 Apply a policy (make changes)

> **Strongly recommended:** always run a dry-run first.

Apply a policy across all in-scope projects:

```bash
jira-admin-suite --config config/config.yaml policy apply mission_assurance_team

You’ll be prompted to confirm:

```text
Apply policy mission_assurance_team with 5 additions and 2 removals? [y/N]
```

Apply without interactive confirmation (for automation):

```bash
jira-admin-suite policy apply mission_assurance_team \
  --config config/config.yaml \
  --yes
```

Respecting change guardrails:

- Each policy has `enforcement.max_changes_per_project`.
- If a project’s planned changes exceed this, the CLI will **skip** applying for that project unless you force it.

Force applying (overriding per-project limit):

```bash
jira-admin-suite policy apply mission_assurance_team \
  --config config/config.yaml \
  --yes \
  --force
```

### 4.6 User tools

Export full user inventory to JSON:

```bash
jira-admin-suite users inventory \
  --config config/config.yaml \
  --output users.json
```

Heuristic lookup for a `firstname.lastname` style identifier:

```bash
jira-admin-suite users find --pattern alice.smith --config config/config.yaml
```

This uses heuristic mapping (candidate usernames/emails) to suggest likely Jira users.

---

## 5. REST API (FastAPI)

The REST API wraps the same policy engine so other tools (or a web UI) can call it.

### 5.1 Start the API server

From the repo root with your virtualenv activated and config/env set:

```bash
cd /home/kevin/tools/jira-admin-suite
source .venv/bin/activate
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

Now you can open (in your browser on Windows or curl from WSL):

- Health: `http://localhost:8000/health`
- API docs (Swagger UI): `http://localhost:8000/docs`

### 5.2 Endpoints

- `GET /health` – simple status.
- `GET /projects` – all Jira projects.
- `GET /policies` – list policies.
- `GET /policies/{id}` – single policy.
- `GET /policies/{id}/plan?project=KEY` – dry-run for a policy.
- `POST /policies/{id}/apply` – apply a policy.

Example `curl` to dry-run a policy:

```bash
curl "http://localhost:8000/policies/mission_assurance_team/plan"
```

Example `curl` to apply a policy:

```bash
curl -X POST "http://localhost:8000/policies/mission_assurance_team/apply" \
  -H "Content-Type: application/json" \
  -d '{"project": null, "force": false}'
```

### 5.3 API key (optional)

For local development, the API key is **disabled by default**.

- If environment variable `JIRA_ROLE_SYNC_API_KEY` is **not set**, all requests are allowed.
- If you set it, e.g.:

  ```bash
  export JIRA_ROLE_SYNC_API_KEY='my-secret-key'
  ```

  Then every API client must send:

  ```http
  X-API-Key: my-secret-key
  ```

Any web UI client can be updated to send this header if you decide to enable it.

---

## 7. Safety & Best Practices

- **Always dry-run first**:
  - Use `policy dry-run` in the CLI or the UI plan view before applying.
- **Use a non-prod Jira instance** while you dial in policies.
- **Guardrails**:
  - `enforcement.max_changes_per_project` limits changes per project.
  - The CLI and API respect this unless `force=True` / `--force` is explicitly provided.
- **Audit**:
  - Use `--json` output or API responses to log planned changes and actual runs.

---

## 8. Troubleshooting

### CLI says `jira-admin-suite: command not found`

- Make sure your virtualenv is activated:

  ```bash
  source .venv/bin/activate
  ```

- Ensure the package is installed in this environment:

  ```bash
  pip install -e .
  ```

### API/CLI errors about Jira authentication

- Check the env vars referenced in `config/config.yaml` under `jira.auth`.
- Confirm they are exported in your shell:

  ```bash
  echo "$JIRA_USERNAME"
  echo "$JIRA_PASSWORD"
  ```

### UI cannot load policies (errors in browser console)

- Verify the backend is running:

  ```bash
  curl http://localhost:8000/health
  ```

- Confirm CORS is not blocked (FastAPI’s default should allow your simple usage from localhost).
- Ensure any web UI client is configured with the correct backend base URL.

---

## 9. Internal CA / SSL configuration

In many corporate environments Jira is served with certificates issued by an internal CA (root + issuing CAs). On a fresh WSL/Python install, these CAs are usually **not** trusted by default, which leads to errors like:

```text
Error: Failed to connect to Jira: HTTPSConnectionPool(...): Max retries exceeded ...
Caused by SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate'))
```

To trust your internal Jira certificates from WSL and the `jira-admin-suite` CLI, you can create a CA bundle and point `requests` at it.

### 9.1 Export root and issuing CA certificates

From a browser on Windows while visiting your Jira URL (e.g. `https://jira.yourcompany.com`):

- View the certificate chain and export at least:
  - The **root CA** (for example, `Boeing Basic Assurance Software Root CA G2`).
  - The **issuing/intermediate CA** (for example, `Boeing Basic Assurance Software Issuing CA G2`).
- Save them as `.cer` or `.crt` files in a known location (for example: `C:\Users\<you>\Downloads\certs`).

> You do **not** need to export the Jira leaf certificate; clients validate it using the CA chain.

### 9.2 Build a CA bundle in WSL

In WSL, assuming your exported certs live under `/mnt/c/Users/<you>/Downloads/certs` and are already PEM certificates (most `.cer` / `.crt` exports from Windows will be):

```bash
cd /mnt/c/Users/<you>/Downloads/certs

# Copy/rename to simpler filenames
cp "Boeing Basic Assurance Software Root CA G2.crt" software-root.pem
cp "Boeing Basic Assurance Software Issuing CA G2.crt" issuing.pem

# Build a single CA bundle
cat software-root.pem issuing.pem > jira-ca-bundle.pem

# Copy bundle into your WSL home
mkdir -p ~/certs
cp jira-ca-bundle.pem ~/certs/jira-ca-bundle.pem
```

If your exported files are not PEM, you can convert them with `openssl x509` or `openssl pkcs7` first; see internal docs or ask your security team for the appropriate export format.

### 9.3 Tell Python/requests to use the bundle

Before running `jira-admin-suite` in WSL, set the `REQUESTS_CA_BUNDLE` environment variable to point at your CA bundle:

```bash
export REQUESTS_CA_BUNDLE=$HOME/certs/jira-ca-bundle.pem

cd /home/kevin/tools/jira-admin-suite
source .venv/bin/activate
jira-admin-suite --config config/config.yaml config validate
```

If the bundle is correct, SSL verification errors should disappear and you should see output like:

```text
Config OK. Jira base_url=https://jira.yourcompany.com, projects=128
```

### 9.4 Optional: install into the WSL trust store

For a more permanent setup, you can install the root/issuing CAs into WSL's system trust store so you don't need `REQUESTS_CA_BUNDLE`:

```bash
sudo cp ~/certs/software-root.pem /usr/local/share/ca-certificates/software-root.crt
sudo cp ~/certs/issuing.pem /usr/local/share/ca-certificates/issuing.crt
sudo update-ca-certificates
```

After this, you can usually unset `REQUESTS_CA_BUNDLE`:

```bash
unset REQUESTS_CA_BUNDLE
source .venv/bin/activate
jira-admin-suite --config config/config.yaml config validate
```

---

## 10. Next Steps / Extensions

- Add more policies for other roles and patterns.
- Extend `user_source` to pull from files or Jira groups instead of static lists.
- Wire the API key into the UI and enable `JIRA_ROLE_SYNC_API_KEY` for extra safety.
- Add ScriptRunner integrations that call this API on a schedule or on-demand.
