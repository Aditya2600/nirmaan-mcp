# Nirmaan Bot

Automation that creates and updates Plane (Nirmaan) work items so you stop forgetting to log work.

This is **Phase 1 of 4**: a Python client for the Plane REST API. It's the foundation — the CLI (Phase 2), MCP server (Phase 3), and scheduled digest (Phase 4) all wrap this client.

## Setup (5 minutes)

### 1. Generate a Plane API token

In Nirmaan:
- Go to **Workspace Settings → API Tokens → Create token**
- Save the token somewhere safe — you can't see it again.

### 2. Install Python deps

```bash
cd nirmaan-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

This installs the CLI commands `nirmaan` and `nbot`.

### 3. Configure .env

```bash
cp .env.example .env
```

Open `.env` and fill in:
- `PLANE_API_KEY` — the token you just generated
- `PLANE_BASE_URL` — `https://nirmaan.credresolve.com` (already set)
- `PLANE_WORKSPACE_SLUG` — `cr-product` (already set, from your URL)

Leave `PLANE_PROJECT_ID` blank for now. The smoke test will help you find it.

### 4. Run the smoke test

```bash
python scripts/smoke_test.py
```

This will:
1. Connect using your API key
2. List all projects in your workspace
3. Show their IDs

Find the row for **Builder** (or whichever project you're working in), copy its ID.

### 5. Add the project ID to .env

```
PLANE_PROJECT_ID=<paste the ID here>
```

### 6. Re-run the smoke test and choose a module

```bash
python scripts/smoke_test.py
```

Now it will also list:
- All states (Todo, In Progress, Done, etc.)
- All labels (Tech, Bug, etc.)
- All modules (AgentX, etc.)
- The current cycle

If you want all bot-created work items to live inside **AgentX** in **Builder**,
copy the AgentX module ID into `.env`:

```
PLANE_MODULE_ID=<paste the AgentX module ID here>
```

It will offer to create a test work item. Say yes — confirm in the Nirmaan UI that it appears, then delete it.

If everything works, **Phase 1 is done.**

## CLI

After setup, use either command name:

```bash
nirmaan done "Logged Builder work item"
nbot list
```

Avoid using `nm` as the command name on macOS: it is already a system binary, so
running `nm` can produce `/usr/bin/nm: error: a.out: No such file or directory`.

## What's next

| Phase | What | Time |
|---|---|---|
| 2 | `plane done "..."` CLI for instant captures | 1 evening |
| 3 | Plane MCP server so Claude Code can drive it | 1 weekend |
| 4 | `launchd` scheduled 6pm digest | 10 minutes |

## Structure

```
nirmaan-bot/
├── .env                 # secrets (you create, gitignored)
├── .env.example         # template
├── nirmaan/
│   ├── __init__.py
│   └── plane_client.py  # the API client — used by everything
└── scripts/
    └── smoke_test.py    # verify everything works
```

## Troubleshooting

- **401 Unauthorized** → API key is wrong or expired. Regenerate.
- **404 Not Found** → workspace slug or project ID is wrong, or your Plane version uses different endpoint paths. See https://docs.plane.so for current API reference.
- **403 Forbidden** → your token doesn't have access to this workspace.
- **Connection refused / timeout** → check that you can reach `https://nirmaan.credresolve.com` from your network (VPN required?).

## API endpoint reference

This client targets Plane's REST API at:
```
{base_url}/api/v1/workspaces/{slug}/projects/{project-id}/issues/
```

Auth is `X-API-Key: <token>` header. If endpoints 404, check the live docs — Plane has evolved over versions and your self-hosted instance may differ.
