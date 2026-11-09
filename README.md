# Lean Mode — `quick_run` for Hermes Agent

**Cheap mode that can't make you lose: a dollar cap, auto-escalation when the cheap model fails, and an honest cost receipt.**

## The problem

Every call your main agent makes carries the whole conversation. In our live
sessions the parent averaged **~225k prompt tokens per call** (100–300k is
normal for a long chat). Handing a self-contained task to a subagent on that
same model still pays parent prices for every token.

A Lean child starts clean: just the task, a short brief and a small toolset.
In our live tests every child's first call was **6.0–6.1k prompt tokens**, and
short tasks stayed in the 4–10k range per call. Lean runs that child on a
cheap model, caps what it can spend, retries on the parent model if the cheap
one fails, and prints a receipt so you can check the saving yourself.

## Use it

Ask the agent in plain language. The agent calls the `quick_run` tool:

```
use quick_run preset research for: find the 3 most popular Hermes plugins and summarize them
use quick_run preset code with budget_usd 0.01 for: write a regex that matches ISO dates
```

`quick_run` arguments: `task` (required), `context` (brief from the main agent),
`preset`, `model`, `toolsets`, `timeout_seconds`, `budget_usd`, `escalate`.

The `/quick` command does not start runs (see Known limitations). It has two
subcommands:

```
/quick stats [--days N]   # spend, vs-non-Lean, net saved, escalations
/quick config             # effective config, prices, presets, bundle warnings
/quick <task>             # replies with the sentence to send the agent instead
```

## Config

Put this under `lean:` in `~/.hermes/config.yaml`. This is the config the live
tests ran with; the two `context_hint`s are translated from Indonesian. Prices
are $ per 1M tokens, set them to your provider's real rates:

```yaml
lean:
  model: surplus/glm-5.3-flash        # cheap model for the child
  parent_model: combo                 # your main model: used for escalation and the vs-non-Lean line
  toolsets: [web, file]               # default child toolsets (never delegate/memory/skills/clarify/send_message/cronjob)
  timeout_seconds: 300
  gateway_max_timeout_seconds: 120    # Telegram/Discord/WhatsApp/gateway sessions are clamped to this
  escalate: true
  escalate_on: [failed, empty_result, max_iterations, low_confidence, launch_failed]
  min_escalation_seconds: 60          # skip escalation if less time than this is left
  budget_usd: 0                       # default per-run cap, 0 = off
  prices:
    surplus/glm-5.3-flash:
      input: 0.0023
      output: 0.0066
      cache_read: 0.0023
      cache_write: 0.0023
    combo:
      input: 0.0091
      output: 0.018
      cache_read: 0.0091
      cache_write: 0.0091
  presets:
    research:
      toolsets: [web, file]
    code:
      toolsets: [file, terminal, code_execution]
    hard:
      context_hint: >-
        If you cannot verify your answer with sources or exact steps, or the
        problem is unsolved, end with LEAN_CONFIDENCE: low. Never claim to have
        proved something that is unproven.
    force_escalate:                   # test preset: always escalates
      context_hint: "This is an escalation test. Answer briefly, then end with exactly: LEAN_CONFIDENCE: low"
```

Then `hermes plugins enable lean-mode`, add `lean` to your platform toolsets,
and restart the gateway.

## Features

- **Dollar cap (soft).** `budget_usd` per run. The watchdog checks spend
  between API calls and cancels as soon as the cap is crossed, so a run stops
  within one call. The call already in flight can overshoot. Setting a budget
  without a price entry for the cheap model is refused.
- **Auto-escalation.** If the cheap child fails, returns empty, hits max
  iterations or ends with `LEAN_CONFIDENCE: low`, Lean reruns the task once on
  `parent_model` with the parent's toolsets. The escalation only gets the time
  left before the original deadline. Escalation is never triggered by a budget
  or timeout stop.
- **Watchdog.** Wall-clock limit per run (`timeout_seconds`, clamped to
  `gateway_max_timeout_seconds` on messaging gateways). On timeout the child
  is cancelled and you get its partial result.
- **Restricted child.** The child only gets the configured toolsets. No
  delegation, memory, clarify, send_message or cronjob, and no skills.
- **Receipt.** Model, calls, tool calls, duration, tokens in/out/cache, cost
  and a "vs non-Lean" line that reprices the same tokens at `parent_model`
  rates. Escalated runs show both costs and a negative saving.
- **Audit + stats.** One JSON line per run in `plugin-data/lean-mode/runs.jsonl`
  with per-call token buckets. `/quick stats` totals spend, the non-Lean
  comparison and net saved over all priced runs (escalations count as
  losses). Older entries without a stored comparison are repriced from their
  tokens. Unreadable entries are counted as skipped, not dropped silently.

## Live test results

Real runs on Hermes v0.23.0 over Telegram, cheap model `surplus/glm-5.3-flash`,
parent `combo`. All numbers come from `runs.dev.jsonl` (development log) and
`runs.jsonl` (clean log after the final fixes). Costs use the prices above.

| Test | Run id | Result | Calls | Prompt tokens (in + cache) | Cost |
|------|--------|--------|-------|----------------------------|------|
| Research run, narrowed toolsets (no "inherited from parent") | `lean-6793e5064240` | succeeded, 45 s | 3 | 22,774 + 8,960 | $0.0000843 |
| Budget cap $0.00002, escalate off | `lean-98a8f44d042b` | cancelled_by_budget after 2 calls, 17 s, no escalation | 2 | 11,877 + 4,032 | $0.0000389 (in-flight overshoot) |
| Escalation (`force_escalate`) | `lean-a55c75094200` | escalated_low_confidence → combo answered | 1 cheap + 1 combo | 6,068 cheap / 9,802 + 768 combo | cheap $0.0000147 (wasted) + combo $0.0000991 = $0.000114 |
| Watchdog, timeout 60 s, escalate off | `lean-c5cca1da6f96` | cancelled_by_watchdog at 61 s, partial result returned | 7 | 92,461 + 49,152 | $0.000333 |
| Child asked to call `skill_view` / `skills_list` | `lean-ca498161cbfa` | child reported both tools unavailable; its toolset was patch, read_file, search_files, web_extract, web_search, write_file | 1 | 6,024 + 0 | $0.0000172 |
| `/quick stats` vs hand count of `runs.jsonl` | 4 runs | match: 1 ok · 1 escalated · 2 timeout, spent $0.000564 | – | – | – |

Output of `/quick stats` on the clean log:

```
📊 Lean Stats — last 7 days
  4 runs · 1 ok · 1 escalated · 0 budget-stop · 2 timeout
  spent: $0.000564 (estimate) · vs non-Lean $0.00202 · net saved $0.00145 (72%)
  escalated: 1 runs (−$0.0000147 wasted)
  top preset: @research
```

Across all 58 child calls in both logs the first call was always 6.0–6.1k
prompt tokens. The median was 16.8k: long research runs grow as tool results
pile up, but they never got near the parent's ~225k per call.

## Known limitations

- **Bundle toolsets block narrowing.** Hermes checks a child's
  `allowed_toolsets` against the literal names in the parent's toolsets and
  does not look inside bundles such as `hermes-telegram`. With a bundle the
  child launch is refused ("would broaden parent permissions") and Lean falls
  back to inheriting all parent tools; the receipt then says
  `toolsets: inherited from parent (narrowing rejected) · skills: ON`. Fix it
  by listing granular toolsets for the platform:

  ```yaml
  platform_toolsets:
    telegram: [web, terminal, file, vision, image_gen, skills, browser, tts, todo,
               memory, session_search, clarify, code_execution, delegation, cronjob,
               computer_use, connections, kanban, lean]
  ```

  `/quick config` warns when it sees a bundle.
- **`/quick <task>` cannot start a run.** A slash-command handler cannot
  launch a subagent, and `inject_message` did not start an agent turn in our
  tests. The command replies with the exact sentence to send the agent instead.
- **Escalation trusts the child's own confidence.** `low_confidence` relies
  on the cheap model ending with `LEAN_CONFIDENCE: low`. With the `hard`
  preset, the same unprovable task (prove the twin prime conjecture) once
  escalated (`lean-dd59300c0885`) and once came back confident
  (`lean-18e37aa6eebd`, which correctly said no proof exists). A cheap model
  can be wrong and still say it is sure; Lean cannot catch that.
- **Dashboards can differ from the receipt.** Lean counts the token buckets
  Hermes reports per call. A caching proxy in front of the provider can bill
  fewer input tokens than the provider reports; we saw a fixed 2,000-token
  gap per call on one router. The receipt is an estimate from your price
  table, not an invoice.

## Disclosure

- No network calls of its own. All model and tool traffic goes through the
  Hermes subagent the plugin launches (`ctx.subagent_lifecycle`), on the
  models and toolsets you configure.
- Reads only your Hermes config (the `lean:` section, plus
  `custom_providers` to guess `parent_model` when it is unset) and the token
  buckets Hermes passes to its `post_api_request` / `subagent_start` /
  `subagent_stop` hooks. No direct reads of `state.db`.
- Writes only to `plugin-data/lean-mode/` (`runs.jsonl`, rotated at
  `audit_retention_lines`). Entries hold model names, token counts, costs,
  statuses and session ids, never the task text or the result.
- Reads no credentials, `.env`, `auth.json` or other credential stores.
- No telemetry and no usage reporting.
- It uses `toolsets.get_toolset_names` / `validate_toolset` from Hermes core
  to validate toolset names. `hermes plugins validate` flags these as private
  core imports.

## Development

```bash
cd ~/.hermes/plugins/lean-mode
python -m pytest tests/ -q
hermes plugins validate ~/.hermes/plugins/lean-mode --install-deps
```

MIT licensed. See [LICENSE](LICENSE).
