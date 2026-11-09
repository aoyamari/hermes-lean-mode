# Lean Mode (`/quick`)

**Cheap mode that can't make you lose.**

A Hermes Agent plugin that runs tasks on a cheap model with a **budget cap**, **auto-escalation** on failure, and a **cost receipt** proving what you saved.

## Quick Start

```bash
# Enable the plugin
hermes plugins enable lean-mode

# Configure in ~/.hermes/config.yaml:
lean:
  model: "google/gemini-flash-2.0"   # ← MUST set to a real model on your provider
  provider: "openrouter"
  toolsets: [terminal, read, web_search]
  prices:
    google/gemini-flash-2.0:
      input: 0.10
      output: 0.50
```

Then in a Hermes session:
```
/quick research the latest AI papers on agentic systems
/quick --budget 0.02 research the latest AI papers
/quick stats
/quick --full deploy the app
```

## Features

| Feature | Description |
|---------|-------------|
| **Budget cap** (`--budget N`) | Per-run dollar limit; watchdog polls every 3s and cancels when crossed |
| **Auto-escalation** | If the cheap model fails / returns low confidence, runs once on the parent model |
| **Cost receipt** | Tokens, $ estimate, vs-non-Lean comparison line |
| **`/quick stats`** | Cumulative savings report from the audit log |
| **Skill suppression** | Cheap worker has no skill index — can't use `skill_view`/`skill_manage` |
| **Narrow toolsets** | Only `terminal`, `read`, `web_search` by default (operator-configurable) |
| **`--full`** | Escape hatch: run with parent model + full tools (Lean off) |
| **`--raw`** | Direct launch without parent brief (for self-contained tasks) |
| **`@preset`** | Shortcuts: `@research`, `@code` |

## Commands

```
/quick [@preset] [flags] <task>
/quick stats [--days N]
```

Flags:
- `--model M` — Override cheap model
- `--tools a,b,c` — Override toolsets
- `--timeout S` — Override timeout (default 300s)
- `--budget N` — Dollar budget cap
- `--no-escalate` — Disable auto-escalation
- `--full` — Use parent model + full tools
- `--raw` — Direct launch (no parent brief)

## Configuration

Place under `lean:` in `~/.hermes/config.yaml`:

```yaml
lean:
  model: ""                      # Cheap model ID (operator MUST set)
  provider: "openrouter"         # Optional provider
  toolsets: [terminal, read, web_search]
  timeout_seconds: 300
  gateway_max_timeout_seconds: 120
  poll_interval_seconds: 3
  budget_usd: 0.0                # 0 = off
  escalate: true
  escalate_on: [failed, empty_result, max_iterations, low_confidence, launch_failed]
  min_escalation_seconds: 60
  max_brief_chars: 6000
  max_result_chars: 8000
  prices:
    google/gemini-flash-2.0:
      input: 0.10
      output: 0.50
      cache_read: 0.05
      cache_write: 0.10
  presets:
    research:
      toolsets: [terminal, read, web_search]
      context_hint: "Be thorough with web research."
    code:
      toolsets: [terminal, read, write]
      timeout_seconds: 600
  audit_retention_lines: 1000
```

Model fallback chain: `lean.model` → `delegation.model` → parent model.

## Receipt Format

```
⚡ Lean receipt — model google/gemini-flash-2.0 · 3 calls · 2 turns · 5 tool calls · 12s
tokens in/out/cache: 1240 / 380 / 200 · est. cost $0.0004
vs non-Lean (same work on parent): $0.012 (estimate) — saved ~97%
run id lean-a1b2c3d4e5f6 · audit: plugin-data/lean-mode/runs.jsonl
excludes the main agent's brief-writing turn
```

Escalated receipt:
```
⚡ Lean receipt — ESCALATED (low_confidence) · cheap $0.001 (wasted) + full $0.03 = total $0.031
saved: −$0.001 vs running on parent-model directly
run id lean-a1b2c3d4e5f6-esc · audit: plugin-data/lean-mode/runs.jsonl
```

## No Model? No Problem

If `lean.model` is unset, Lean still works: the child runs on the parent model but with narrow toolsets, no skills, a watchdog, and a receipt. The receipt says "savings unavailable."

## How It Works

1. **`/quick <task>`** injects a message into the session asking the parent to write a brief and call `quick_run`
2. **Parent writes a brief** with conversation context and calls `quick_run(task, context=...)`
3. **Cheap subagent launches** with narrow toolsets (no skills, no delegation)
4. **Watchdog polls** every 3s checking wall-clock remaining and budget spent
5. If the cheap run fails → **auto-escalates** to parent model once (remaining time + budget)
6. **Receipt** shows: tokens, $ estimate, vs-non-Lean, budget
7. **Audit log** (`runs.jsonl`) records every run

## Development

```bash
# Run unit tests
cd ~/.hermes/plugins/lean-mode
python -m pytest tests/ -v

# Validate plugin
hermes plugins validate . --install-deps

# Live test
hermes -c "/quick --raw echo hello world"
```

## Competitor Comparison

| Rival | Their weakness | Lean's advantage |
|-------|---------------|------------------|
| `smart_model_routing` | Length-based guess, no receipt | Task-level with brief, budget + receipt |
| External routers (NadirClaw, Spark) | Separate proxy, brittle rules | Pure Hermes plugin, no extra infra |
| `hermes-teammates` | Costs rise 26-44% | One-shot, receipt proves saving |
| `advanced-delegation` | Routing only, no guardrails | Budget cap, escalation, watchdog |

## Demo (90 seconds)

1. `/quick @research summarize sessions` → receipt shows saving
2. `/quick --budget 0.001 <hard task>` → stopped by budget cap
3. `/quick <task that fails>` → auto-escalates → ESCALATED receipt
4. `/quick stats` → cumulative savings
5. "Cheap mode that can't make you lose."
