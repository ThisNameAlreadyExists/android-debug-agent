# android-debug-agent (ada)

[中文](README.md) | English

An LLM-driven Android on-device debugging / automated testing / log-diagnosis agent.
Works as a CLI and as an MCP server (WorkBuddy / Claude / Cursor).

## Core Architecture

- **Single-channel chokepoint**: every device operation goes through `channel.AdbChannel` — risk tiers (green/yellow/red), confirmation gate for destructive ops, audit log, unified timeout & retry all live in this layer
- **Structured data throughout**: modules exchange structured objects only (DeviceProfile / AppState / ExecutionTrace / LogBundle / Diagnosis); raw terminal text never leaks across boundaries
- **Capability probing & graceful degradation**: unsupported environments return explicit errors (`UnsupportedError`) instead of crashing
- **Raw-shell escape hatch is off by default**: enable with `ADA_ALLOW_SHELL=1`

## Features

### Phase 1 — MVP (V0.1.0)

| Module | Capabilities |
|---|---|
| Device gateway | Enumeration (USB/network), device profile (model/SDK/ABI/ROM/root/screen), session cache, health check |
| App controller | Install (ABI/minSdk pre-check + auto permission grant), launch, force-stop, clear data, pm grant/revoke |
| Scenario runner | Declarative YAML cases, deterministic replay; semantic locators (id/text/desc); 14 actions; per-step screenshot + timeline marks |
| Log capture | logcat on a background thread + ring buffer; live FATAL/native/ANR detection; step-log timestamp alignment |
| Rule-based diagnosis | Crash-stack parsing (frames → source file/line), pattern library (NPE/OOM/ANR/permission/IllegalState/native), stable cross-version crash fingerprints |
| Reporting | Markdown: environment info + execution table + diagnosis (evidence & fingerprint) |

### Phase 2 (V0.2.0)

| Module | Capabilities |
|---|---|
| CrashDB fingerprint store | SQLite persistence; instant hit on known crashes; regression alert when a fixed fingerprint resurfaces; cross-device/version aggregation (`ada crashes history/show/fixed`) |
| LLM diagnosis tier | Falls through to local Ollama when rules miss; regex scrubbing (phone/ID/token/IP/email) before anything leaves the process; auto-fallback to rule results on LLM failure |
| Explore mode | `ada explore "goal"`: UI tree → LLM decision → action loop; loop detection via UI fingerprints + step cap + dangerous-widget blacklist (uninstall/payment/SMS) + stop-on-crash |
| HTML replay report | `replay_<case>.html`: single-file, zero external links, embedded screenshots; every step listed chronologically with action, activity and its log window |

### Phase 3 (V0.3.0)

| Module | Capabilities |
|---|---|
| Fix-verification loop | `ada verify case.yaml --fingerprint <fp> [--apk fixed.apk]`: install → rerun → original fingerprint gone + all steps pass = fix confirmed → auto mark_fixed + case sunk |
| Case auto-sinking | Verified repro cases are persisted into `regression_cases/` (deduped by fingerprint); `ada regress` replays the whole library, any resurfaced fingerprint = regression failure |
| Repro minimization | `ada minimize case.yaml --fingerprint <fp>`: ddmin-style step deletion to a minimal reproducing YAML (structural steps launch/clear_data/grant are protected); flaky-crash confirmation; `--fingerprint any` mode |
| Privacy tiers | `ADA_PRIVACY=L0/L1/L2`: L0 local-only / L1 scrubbed cloud (default) / L2 full; every outbound payload audited to `privacy_audit.jsonl` |

### Phase 4 (V0.4.0)

| Module | Capabilities |
|---|---|
| Multi-device differential | `ada mrun case.yaml -s S1,S2,...`: same case runs in parallel across devices (isolated channels/threads); auto diff on pass/fail, crash fingerprints (device-specific detection) and execution-path divergence |
| Chaos testing | `chaos` step in cases: low memory (send-trim-memory), background reclaim, rotation storm, mid-run permission revoke, network off/on |
| Rule self-learning | `ada learn --exception X --category Y --cause ... --fix ...` sinks a rule; diagnosis funnel becomes built-in rules → learned rules → LLM, LLM calls decrease with use |
| Performance baseline regression | After a passing `ada run`, cold-start time / peak memory / frame P95 are sampled and compared against the median of the last 10 runs; > +20% degradation raises a regression warning |

**Multi-device note**: every command takes `-s <serial>` to route to a specific device; `ada mrun` adds parallel multi-device diffing.

## Usage

```bash
pip install -e .            # only dependency: PyYAML
ada devices                 # list devices
ada profile <serial>        # device profile
ada install app.apk -s <serial>
ada run examples/login_flow.yaml -s <serial> -p com.example.app   # run + diagnose + report + replay
ada run case.yaml -s <serial> --llm                               # enable LLM deep diagnosis
ada explore "explore the login flow for bugs" -s <serial> -p com.example.app
ada diagnose crash.log -p com.example.app                         # offline log diagnosis
ada crashes history                                               # crash fingerprint history
```

MCP server (WorkBuddy / Claude / Cursor):

```bash
pip install -e '.[mcp]'
ada-mcp    # stdio transport
```

Tools: `ada_devices` `ada_device_profile` `ada_health` `ada_install` `ada_launch` `ada_run_scenario` `ada_logcat` `ada_diagnose_log`.
In MCP mode there is no interactive prompt, so RED-tier operations are always refused (safe default); CLI mode gates them behind a human confirmation.

## Testing

```bash
PYTHONPATH=src python3 -m unittest discover -s tests   # 77 tests
```

## License

[MIT](LICENSE)
