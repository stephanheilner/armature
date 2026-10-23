# Armature platform: web UI, config store, and run history

Status: accepted 2026-10-23. Owner: Stephan Heilner, with Bryan Sparks.

## Goal

Anyone who installs Armature can run `armature server`, sign in, and manage
everything from a browser. That covers workflows, roles, prompts, model
providers, secrets, teams, projects, users, and run history. The same code runs
a company instance with Okta SSO, a small instance for a client, and a personal
instance at home. Nothing instance-specific lives in git.

Today the only deployment that does this, armature-ops, keeps that data in
files edited through Claude Code. Its Node dashboard is tied to that one
deployment. This design moves the UI into Armature, ports it to Python, and
puts the data in a database.

## Constraints

- One runtime. The server is FastAPI, already Armature's `service` extra. The
  frontend stays build-free ES modules, as the existing dashboard is.
- Any SQL database. SQLAlchemy 2 Core with Alembic migrations, tested on
  SQLite, MySQL 8, and Postgres 16+. No backend-specific SQL. JSON columns use
  `sqlalchemy.JSON`, and aggregation such as percentiles happens in Python.
- Workflow definitions never carry shell commands a browser user typed.
  `ScriptNode` runs `cmd:` with `shell=True`, so an editable `cmd:` would be
  remote code execution for anyone with an editor role. Commands come only from
  installed packs (see "Workflow packs").
- The armature-ops instance keeps running through the migration. Each phase
  ends in a state the live instance can run on, with a named rollback.

## Workflow format

### Requirements

The format has to stay readable in a diff, be editable from a form and from a
raw editor, be validated before it's saved, be easy for an LLM to write
correctly, and keep long prose readable.

### Survey of the current workflows

The nine specs in armature-ops total about 1,200 lines of YAML. About half of
that is `adapters:` blocks that each wrap one script: name, `cmd:`, `parse:
json`, and a timeout. The other half is stages: id, adapter or inline role,
`depends_on`, fan-out, `skip_if`, and `fail_as_value`. The long text lives
elsewhere: 27 Markdown prompt files, and `config/roles.json`, which ties each
prompt to its platforms, tools, MCP servers, and per-platform models.

So the workflows have two parts with different needs. Structure, meaning which
steps run in which order with what limits, is short and must validate.
Prose, meaning role prompts, the team lead brief, and missions, is long and
must read well.

### Decision: YAML for structure, Markdown for prose

- **Workflows are YAML**, as Armature specs already are. The pydantic model
  (`HarnessSpec`) gives the JSON Schema that drives the form editor, the
  validator, and the LLM assistant's structured output. No second format or
  converter is needed, and existing specs load unchanged.
- **Roles and prompts are Markdown with YAML front matter**, the format Claude
  Code uses for agents and skills. One document replaces a `roles.json` entry
  plus its prompt file.
- **Steps reference adapters from the catalog by name.** In the UI, adding a
  step means picking a step type, filling in its declared parameters, and
  wiring its inputs. Raw `cmd:` strings never appear in a stored workflow.

Markdown alone was considered and rejected for structure. It can't express a
dependency graph, fan-out, or limits without inventing a grammar that has no
schema to validate against. JSON was rejected for prose because long prompts in
JSON strings are unreadable.

A stored workflow looks like this. Every adapter comes from the installed
`github-review` pack, and `{{ }}` values come from run inputs, never from
webhook text:

```yaml
name: pr-role-review
description: Review one pull request with specialist roles, then a team lead.
mission_ref: missions/mobile-team      # a Markdown document
inputs:
  - name: job_file
steps:
  - id: resolve
    uses: github-review/resolve-pr
  - id: incremental
    uses: github-review/incremental-check
    after: [resolve]
    on_error: continue
  - id: assignments
    uses: github-review/assign-roles
    after: [resolve, incremental]
    with:
      roles: { select: by_platform }
  - id: reviews
    uses: github-review/role-review
    after: [assignments]
    for_each: assignments.jobs
    parallel: 9
  - id: verify
    uses: github-review/build
    after: [assignments]
    on_error: continue
  - id: team_lead
    uses: github-review/team-lead
    after: [reviews, verify]
    with:
      prompt: prompts/team-lead-pr
      model: lead
  - id: post
    uses: github-review/post-review
    after: [team_lead]
```

`uses`, `after`, `for_each`, `parallel`, and `on_error` are friendlier names
for `adapter`, `depends_on`, `partition_source`, `fan_out`, and
`fail_as_value`. The loader compiles this form to a `HarnessSpec`, and plain
Armature specs keep loading too, so the friendly form is optional sugar.

A role looks like this:

```markdown
---
name: security
description: Secrets, insecure storage, data leaks, and authorization gaps.
applies_to: [ios, android, backend, dotnet]
model: review            # a model alias the team maps to a provider
tools: [Read, Grep, Glob, "Bash(git -C:*)"]
mcp_servers: []
---

You are the security reviewer. ...
```

Adding a role is a new document. A role runs in every workflow that selects
roles by platform, with no workflow edit. This matches how armature-ops already
treats `roles.json`.

### Model aliases

Workflows and roles name an alias, such as `review`, `lead`, or `fast`, never
a model id. Each team maps aliases to a provider and model. FamilySearch maps
`lead` to Opus 5.5 on Bedrock, and a personal instance can map it to the same
model through an API key. This lets the same pack run anywhere.

### Workflow packs

A pack is a versioned directory of code, installed from git or PyPI:

```
packs/github-review/
  pack.yaml          # name, version, adapters with parameter schemas
  adapters/          # the scripts
  workflows/         # default workflows, copied into the DB on install
  roles/             # default role documents
  prompts/
```

`pack.yaml` declares each adapter's `cmd`, parameters (a JSON Schema),
outputs, timeout, and which secrets it needs. The UI builds the step form from
that schema. Only admins install or upgrade packs, because a pack is code.
Project leads compose workflows from installed adapters and edit prompts and
roles.

armature-ops becomes three packs: `github-review` (PR role review, incremental
review, discuss, fix loop, and daily review), `jira-work`, and `mobile-port`.
Its scripts move with little change, because packs still run through
`ScriptNode`.

## Data model

Every table except `users` and `settings` belongs to a team. Ids are UUIDs
stored as strings, which keeps them portable across backends.

| Area | Tables |
| --- | --- |
| Tenancy | `teams`, `projects` (a repository, its platform, enabled workflows, and branch filters), `team_projects` |
| People | `users`, `identities` (password+TOTP or OIDC subject), `memberships` (user, team or project, role), `sessions`, `audit` |
| Definitions | `workflows`, `workflow_versions`, `documents` (roles, prompts, missions), `document_versions`, `packs`, `adapters` |
| Config | `providers` (per team: kind, region, model aliases), `secrets` (per team or instance), `settings`, `triggers` |
| History | `runs`, `run_steps`, `run_events` (structured, retained), `findings`, `costs`, `actions` (trigger, cancel, re-run, by whom) |

Each edit of a workflow or document writes a new version row with author and
time, which gives an audit trail and one-click rollback. Export and import as
a directory keeps a git copy possible for anyone who wants one.

## Users, roles, and sign-in

| Role | Scope | Can |
| --- | --- | --- |
| Admin | Instance | Everything, including packs, providers, secrets, and users |
| Team lead | One or more teams | Manage the team's projects, members, model aliases, and workflows |
| Project lead | One or more projects | Edit the project's workflows, roles, and prompts. Re-run and cancel runs |
| Contributor | Projects | Trigger, re-run, and cancel runs. Comment |
| Viewer | Projects | Read runs and history |

Sign-in supports two methods, and an instance enables either or both:

- **OIDC** with Okta or any compliant provider through Authlib. A group claim
  maps to roles, such as `armature-admins` to admin.
- **Password and TOTP**, ported from the dashboard: scrypt, RFC 6238 with
  replay protection, step-up for sensitive changes, and rate limits.

Sessions are server-side rows keyed by an HMAC of the token. Mutations need a
CSRF token and a same-origin check.

## Secrets

Secrets are encrypted in the database with an envelope key: Fernet
(AES-128-CBC with HMAC) under a master key that never enters the database. The
key comes from `ARMATURE_MASTER_KEY`, a key file, or AWS KMS. Encryption at
rest is worth it even on a trusted host, because database backups and
replicas leave the machine. The UI never shows a secret again after it's
saved, only that one is set and when.

A run receives only the secrets its pack declares, in the stage environment.
Sessions keep starting under `env -i`, as armature-ops does today.

## Model providers per team

| Kind | Used for | Credential |
| --- | --- | --- |
| `bedrock` | litellm stages and Claude Code (`CLAUDE_CODE_USE_BEDROCK=1`) | Bearer token or IAM role |
| `anthropic` | litellm stages and Claude Code | API key |
| `claude-subscription` | Claude Code sessions only | OAuth token from `claude setup-token` |
| `openai-compatible`, `ollama` | litellm stages | API key or none |

A subscription token works only for Claude Code sessions. A team that has only
a subscription can't run the inline LLM stages (the litellm path), so the
validator flags a workflow whose stages need a provider kind the team lacks.
Anthropic's terms for subscriptions also govern automated use, so check that
an unattended pipeline is allowed under the plan before you rely on one.

## Run history and live activity

### Today

The dashboard polls every 5 seconds. It reads `traces.db`, `session.jsonl`,
and five log files, and parses Python reprs out of trace outputs. Claude Code
sessions run with `--output-format json`, so nothing is visible until a role
finishes. That is why live activity is slow and shows little.

### Design

1. **An event pipe for every run.** The runner gives each run an
   `ARMATURE_EVENTS` endpoint, a Unix socket or loopback URL with a per-run
   token. The engine's `on_event` hook, the runner, and scripts all write JSON
   events to it: step started, finished, failed, cost, finding, and log line.
2. **Claude Code streams.** Session wrappers switch to `--output-format
   stream-json --verbose` and tee each message as an event: tool call, tool
   result summary, assistant text, and token usage. The final result is
   still the last message, so the wrappers' outputs don't change.
3. **The server fans out and batches.** Events go to an in-process broker
   that pushes them over Server-Sent Events (SSE) to viewers of that run or
   team. A writer flushes them to `run_events` in batches every second. With
   several server processes, a Redis or Postgres `LISTEN` broker replaces the
   in-process one behind the same interface.
4. **Completion writes the summary.** When a run ends, the server writes
   `runs`, `run_steps`, `findings`, and `costs` in one transaction. Console
   output stays in log files and is linked, not stored.

Latency drops from 5 to 10 seconds to under one second, and the database
receives one batched insert per second per active run. Reads use indexes on
(team, started_at) and (run_id, seq). This scales with the number of
concurrent projects, because no work depends on polling the filesystem.

## Authoring with an LLM

The workflow editor has an assistant panel that uses the team's `lead` alias:

- **Draft.** Describe a change in words. The assistant returns a patch to the
  YAML or the role document, using structured output against the JSON Schema,
  and the editor shows it as a diff to accept.
- **Validate.** Every save runs `validate_spec`, the pack adapter parameter
  schemas, the provider check, and a lint of `{{ }}` references against step
  outputs. Errors point at the line.
- **Test.** Run the workflow in dry-run mode against a recorded trigger
  payload. Adapters run with `ARMATURE_DRY_RUN=1`. Most writing adapters in
  armature-ops already have a `*_DRY_RUN` flag that this maps to, and packs
  must honor it for every adapter that declares `writes: true`. A
  stub mode replays recorded step outputs, so the graph can be tested without
  spending money.

## Process layout

`armature server` runs the API, the UI, the event broker, and the scheduler
in one process. Runs execute in worker processes (`armature worker`) that
take jobs from a `jobs` table with row locks, so a long run survives a server
restart, as armature-ops's detached runners do today. SQLite gets one worker
with a file lock, and MySQL and Postgres get `SELECT ... FOR UPDATE SKIP
LOCKED`.

The webhook gateway stays a thin, separate process that verifies signatures
and inserts jobs. An instance doesn't need to expose the API port, and
`POST /run` with an arbitrary spec path goes away.

## Migrating armature-ops while it runs

| Phase | Change | Live instance | Rollback |
| --- | --- | --- | --- |
| 0 | Commit current state | Unchanged | armature-ops `3fc5218`, armature `ops-live` |
| 1 | Server, database, auth, RBAC, secrets, and UI shell in Armature | Unchanged. New server runs on its own port and database | Stop the new server |
| 2 | Event pipe, live view, and history. Importer for `traces.db` and the dashboard Postgres | Runs post events to the new server too. Old dashboard keeps working | Unset `ARMATURE_EVENTS` |
| 3 | Workflow, role, prompt, and provider editors. Assistant. Dry-run testing | Read-only mirror of the ops config | Not needed |
| 4 | Import ops config into the database. The server renders `teams.json`, `roles.json`, `prompts/`, and `specs/` from it | Scripts read rendered files, unchanged | Restore files from git |
| 5 | Repackage ops scripts as packs. Runners take jobs from the database | Cut over one workflow at a time, starting with `build-announce` | Point the runner back at the file queue |
| 6 | Retire the Node dashboard and the file-based config | Apache proxies `/dashboard` to the new server | Re-enable the old vhost |

Phase 4's render step is the key to zero downtime. It's the same pattern
`teams.json` already uses, where the dashboard owns the data and writes the
file the scripts read.

## Out of scope for this change

- Multi-tenant hosting of several companies in one database. Each company
  runs its own instance.
- Moving `state/` coordination files (locks, queues, and marks) into the
  database, except where Phase 5 replaces a queue.
- Script authoring in the browser.
