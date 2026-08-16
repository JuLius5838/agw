# Claude External Subagents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. **Execution mode: Inline Execution.** Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every current, explicitly verified external AGW model discoverable to a managed parent Claude session as a native read-only custom-agent type, so Claude can autonomously run zero, one, or several distinct external models and synthesize their normal child results.

**Architecture:** Build the feature as one compatibility-gated path with narrow modules: pure agent-definition and CLI-argument construction, conservative Claude identity/policy evaluation, one-record-per-model compatibility storage, secure append-only probe events, a one-shot router registration, a synchronous `SubagentStart` hook, and a bounded native-Claude verifier. Managed launch injects only current eligible definitions through `--agents`; ordinary sessions neither register probes nor persist request evidence.

**Tech Stack:** Python `>=3.12,<3.13`; stdlib `dataclasses`, `hashlib`, `json`, `os`, `pathlib`, `secrets`, `signal`, `subprocess`, and `time`; Pydantic `>=2.7,<3.0`; HTTPX `>=0.27,<0.29`; Starlette `>=1.0.1,<2.0`; Uvicorn `>=0.30,<1.0`; psutil `>=5.9,<8.0`; Typer `>=0.12,<1.0`; pytest `>=8.0,<9.0`; Ruff line length 100; strict mypy; Claude Code compatibility target `2.1.232 (Claude Code)`.

## Global Constraints

- Preserve the dirty Council baseline in `src/agent_gateway/cli.py`, `src/agent_gateway/config.py`, `tests/integration/test_cli.py`, `src/agent_gateway/council.py`, and `tests/unit/test_council.py`; Council integration is outside this slice.
- Never stage, commit, or push anything. No step in this plan performs a version-control write; publication stays the user's decision. Read-only inspection (`git status`, `git diff`) is expected.
- Use TDD in every implementation task: add the named failing tests, observe the specified failure, write only the minimal production change, then run the narrow passing gate.
- Ordinary offline verification must make no Claude/provider model calls. Only the explicit, unexecuted live gate may run `agw models verify ... --claude-subagent` or live smoke tests.
- Keep native saved-login behavior: managed Claude receives loopback `ANTHROPIC_BASE_URL` plus `X-AGW-Key`, but no `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, provider credential-directory variable, or LiteLLM private key.
- Keep provider authentication global and fail-fast for every provider backing an active model; do not silently omit an active model because its provider is unauthenticated.
- Never silently substitute an external model/provider. Routing always uses the exact public `ModelEntry.name`; generated IDs and display labels are metadata only.
- Generated contract v1 is read-only: exactly `Read`, `Grep`, and `Glob`; no Bash, Edit, Write, Skill, Agent/Workflow recursion, web, MCP, permission-mode override, memory, or automatic worktree.
- Compatibility/probe evidence must never contain prompts, responses, delegated tasks, tool inputs/results, auth headers, tokens, credentials, provider paths, or LiteLLM keys.
- Strip `x-claude-code-agent-id` and `x-claude-code-parent-agent-id` from every external upstream request; lifecycle IDs remain local to the controlled probe.
- Generated definitions are advertised only from successful, current evidence matching the configured Claude launch path, canonical target, executable SHA-256, exact version output, exact public model/provider, runtime fingerprint, and definition contract version.
- `claude` after shell setup and explicit `agw claude` share the same managed path; `command claude` remains the native bypass.
- Error mapping is fixed: malformed/colliding `--agents` -> `ConfigError`; missing/inactive model, or a failed native compatibility verdict once every requested model has been attempted -> `ModelUnavailableError`; executable/version failures -> `PrerequisiteError`; router registration, hook/probe lifecycle, and cleanup failures -> `ProxyError`. A single model's compatibility verdict is a returned report, never an exception; only the CLI raises the aggregate. Add no exit-code category.
- POSIX probe state uses `0700` directories and `0600` files. Append uses `O_APPEND | O_NOFOLLOW`, validates owner/mode/type/containment/device/inode before every write, and writes one NDJSON record smaller than `PIPE_BUF`.
- Timeouts/cancellation never signal a bare PID. Create a process group/session, retain `ProcessIdentity`, re-check identity before TERM/KILL escalation, bound each wait, and clean probe files/registrations in every exit path.

---

## Decision Ledger

- **One compatibility record per exact model/provider:** `external-subagents/compatibility/<provider>/<sha256(model UTF-8)>.json`; the record carries the public name so filenames never become aliases.
- **One ephemeral event file per verification run:** `external-subagents/probes/<32-lowercase-hex-nonce>.ndjson`; it is removed after every success or failure. Alongside it, `external-subagents/probes/run-<nonce>/` is the probe child's `cwd` and holds only the one-line read fixture; both are removed in the same cleanup.
- **Verdict versus lifecycle failure:** an ordinary compatibility outcome is always a returned `NativeSubagentVerifyReport`, so the CLI can attempt every requested model. Only registration/hook-lifecycle and cleanup failures raise (`ProxyError`), and only executable/version problems raise `PrerequisiteError`.
- **One router endpoint:** `POST /_agw/control/v1/subagent-probes`; authenticated by `X-AGW-Key`, accepted only from a loopback peer, and never forwarded upstream.
- **One-shot in-memory registrations:** bound to nonce, exact agent ID/model/provider, exact pre-created event-file identity, and a 60-second monotonic expiry. Router restart intentionally loses them and makes verification fail closed.
- **Race resolution:** the session-scope `SubagentStart` command hook validates/appends the start event and synchronously registers the probe before returning. Claude Code drains this hook before the child model loop; no verifier polling window authorizes the route after startup.
- **Compatibility classes:** `current`, `missing`, `invalid`, `failed`, `stale`, and `policy_excluded`. Only `current` is eligible. A new verification invalidates any old success before launching Claude; failed verification stores a sanitized `outcome="failed"` record.
- **Policy semantics:** inspect `~/.claude/settings.json`, `.claude/settings.json`, and `.claude/settings.local.json` conservatively. Any valid non-empty `availableModels` list that excludes the exact name is `policy_excluded`; unreadable/invalid/uninspectable managed policy is not treated as permission and can only be disproved by the live exact-route probe.
- **Runtime ABI:** replace `5-litellm-readiness` with `6-subagent-probe-control`; this invalidates daemon reuse because the router process contract and header filtering change.
- **Council reuse is a contract, not code, in this slice:** Council stays out of the implementation, but the eligibility resolver is the single approved seat-to-agent path (spec FR-10). A later Council follow-up must call `resolve_external_agent_eligibility()` rather than re-deriving generated types or introducing a second execution mechanism.
- **No ordinary per-spawn pinning claim:** current evidence enables definition injection, but ordinary sessions register no probe. A later Claude regression is surfaced by re-verification, not retroactively caught on an unobserved child.

## File and Interface Map

### New focused modules

- `src/agent_gateway/external_subagents.py` — pure generated-ID/definition construction, user `--agents` merge, and eligible-definition resolver.
- `src/agent_gateway/claude_compat.py` — configured/canonical Claude executable identity, bounded version query, conservative settings policy, compatibility schema/storage/status.
- `src/agent_gateway/probe_events.py` — secure event-file creation, identity validation, atomic NDJSON append/read, and cleanup.
- `src/agent_gateway/subagent_probe.py` — event schemas, router registration client, hook handlers/CLI, isolated settings and inline verifier-agent definitions, process orchestration, evidence join, and compatibility-record outcome.

### Existing modules modified surgically

- `src/agent_gateway/paths.py` — external-subagent state paths only; retain `atomic_write_text` for complete records.
- `src/agent_gateway/router.py` — one-shot registry/control route, exact route-event correlation, and lifecycle-header stripping.
- `src/agent_gateway/proxy.py` — ABI bump and probe-directory propagation while preserving global provider-auth checks.
- `src/agent_gateway/supervisor.py` — pass only the dedicated probe directory to the secret-scrubbed router.
- `src/agent_gateway/process.py` — identity-safe process-group termination used by the verifier.
- `src/agent_gateway/harnesses/claude.py` — resolve current definitions after proxy readiness, merge `--agents`, and preserve zero-definition argv bytes.
- `src/agent_gateway/cli.py` — add `--claude-subagent` without disturbing Council or direct verification.
- `src/agent_gateway/doctor.py` — offline compatibility/policy/collision diagnostics and corrected override wording.
- `src/agent_gateway/uninstall.py` — remove generated compatibility/probe state, never credentials.

### Tests

- Create `tests/unit/test_external_subagents.py`, `tests/unit/test_claude_compat.py`, `tests/unit/test_probe_events.py`, and `tests/unit/test_subagent_probe.py`.
- Modify `tests/unit/test_paths.py`, `tests/unit/test_router.py`, `tests/unit/test_process.py`, `tests/unit/test_supervisor.py`, `tests/unit/test_harness_env.py`, `tests/unit/test_doctor.py`, `tests/integration/test_launcher.py`, `tests/integration/test_cli.py`, `tests/contract/test_proxy_lifecycle.py`, and `tests/manual/live_smoke.py`.

### Documentation

- Modify the approved specification, `README.md`, `docs/model-selection.md`, `docs/compatibility-report.md`, `docs/security.md`, `docs/architecture.md`, `docs/operations.md`, `skills/model-routing/SKILL.md`, and `tests/manual/README.md`.

---

### Task 1: Pure Generated Agent Definitions and `--agents` Merge

**Files:**
- Create: `src/agent_gateway/external_subagents.py`
- Create: `tests/unit/test_external_subagents.py`

**Interfaces:**
- Produces:
  - `DEFINITION_CONTRACT_VERSION: Final[int] = 1`
  - `EXTERNAL_AGENT_TOOLS: Final[tuple[str, ...]] = ("Read", "Grep", "Glob")`
  - `EXTERNAL_AGENT_MAX_TURNS: Final[int] = 20`
  - `GeneratedAgent(agent_type: str, model: str, provider: Provider, definition: dict[str, object])`
  - `generated_agent_type(entry: ModelEntry) -> str`
  - `build_generated_agent(entry: ModelEntry) -> GeneratedAgent`
  - `build_generated_agents(entries: Iterable[ModelEntry]) -> tuple[GeneratedAgent, ...]`
  - `merge_agents_args(forwarded_args: Sequence[str], generated: Sequence[GeneratedAgent]) -> list[str]`
- Consumes: immutable `ModelEntry` and `Provider` from existing registry code.

- [ ] **Step 1: Write deterministic identity and definition-shape tests**

```python
from agent_gateway.external_subagents import (
    EXTERNAL_AGENT_TOOLS,
    build_generated_agent,
    generated_agent_type,
)


def test_generated_agent_type_matches_contract_example(sol_entry):
    assert generated_agent_type(sol_entry) == "agw-gpt-5-6-sol-66127d2195a1"


def test_generated_definition_is_exactly_read_only(sol_entry):
    agent = build_generated_agent(sol_entry)
    assert agent.model == "gpt-5.6-sol"
    assert agent.provider.value == "chatgpt"
    assert set(agent.definition) == {"description", "prompt", "model", "tools", "maxTurns"}
    assert agent.definition["model"] == "gpt-5.6-sol"
    assert agent.definition["tools"] == list(EXTERNAL_AGENT_TOOLS) == ["Read", "Grep", "Glob"]
    assert agent.definition["maxTurns"] == 20
    assert "ChatGPT" in str(agent.definition["description"])
    assert "read-only" in str(agent.definition["description"])
    assert "sent to ChatGPT" in str(agent.definition["description"])
    assert "do not use merely" in str(agent.definition["description"])
```

Also add two entries whose names sanitize to the same prefix and assert their 12-hex suffixes differ. Compute the digest over exactly:

```python
json.dumps(
    {"contract": 1, "model": entry.name, "provider": entry.provider.value},
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=True,
).encode("utf-8")
```

Expected prefix algorithm: lowercase, replace each maximal `[^a-z0-9]+` run with `-`, trim `-`, truncate to 40 characters; if the result is empty, use `model`.

- [ ] **Step 2: Run the identity tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_external_subagents.py -k "generated_agent" -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'agent_gateway.external_subagents'`.

- [ ] **Step 3: Implement the minimal immutable definition builder**

```python
DEFINITION_CONTRACT_VERSION: Final = 1
EXTERNAL_AGENT_TOOLS: Final = ("Read", "Grep", "Glob")
EXTERNAL_AGENT_MAX_TURNS: Final = 20

_PROVIDER_LABELS: Final = {
    Provider.chatgpt: "ChatGPT",
    Provider.copilot: "GitHub Copilot",
}

@dataclass(frozen=True)
class GeneratedAgent:
    agent_type: str
    model: str
    provider: Provider
    definition: dict[str, object]
```

Use this exact description template:

```text
Use the external AGW model {model} ({provider_label}) for an independent read-only perspective. Repository content read by this agent is sent to {provider_label}. Multiple AGW model agents may run concurrently when their perspectives add value; do not use merely to spend quota or duplicate work.
```

Use this exact prompt:

```text
Complete only the delegated analysis task. Use only the provided read-only repository tools. Do not attempt to modify files or invoke external services. State limitations clearly and return a concise result to the parent.
```

Return tuples sorted by `(model, provider.value, agent_type)` and raise `ConfigError("generated external-agent IDs collide: '<id>'.")` if two generated entries ever share an ID.

- [ ] **Step 4: Add the complete `--agents` argument matrix**

```python
@pytest.mark.parametrize(
    ("args", "expected_tail"),
    [
        ([], []),
        (["--agents", '{"mine":{"description":"x"}}'], []),
        (["--agents={\"mine\":{\"description\":\"x\"}}"], []),
        (["--", "--agents", "not-json"], ["--", "--agents", "not-json"]),
    ],
)
def test_merge_agents_args_preserves_position_and_terminator(args, expected_tail, sol_agent):
    merged = merge_agents_args(args, [sol_agent])
    option_index = merged.index("--agents")
    parsed = json.loads(merged[option_index + 1])
    assert parsed[sol_agent.agent_type] == sol_agent.definition
    assert merged[option_index + 2 :] == expected_tail
```

Add explicit assertions for:

- no generated definitions -> `merge_agents_args(original, []) == list(original)` byte-for-byte, including malformed pre-terminator JSON;
- no user option and no terminator -> append `--agents` plus compact deterministic JSON at the end;
- no user option but a `--` terminator present -> insert the generated `--agents JSON` immediately **before** the terminator. Appending past `--` would turn AGW's own option into positional prompt text, so the parametrized case `["--", "--agents", "not-json"]` expects the tail `["--", "--agents", "not-json"]` to follow the inserted pair;
- one `--agents JSON` or `--agents=JSON` -> replace at that option's original index with two tokens;
- nested lists/objects/numbers/booleans/null in every user value survive semantic round-trip;
- values after the first `--` stay untouched and are never parsed;
- missing two-token value, malformed JSON, scalar/list/null root, duplicate pre-terminator options, and generated-ID collision raise `ConfigError`;
- every diagnostic names only `--agents` or the colliding generated ID and does not contain the supplied JSON, its prompt, or a token canary.

Required messages:

```text
--agents requires a JSON object value before '--'.
--agents must contain valid JSON; the value was not included in this diagnostic.
--agents JSON must have an object root.
--agents may be specified at most once before '--'.
--agents defines reserved generated agent ID '<id>'.
```

- [ ] **Step 5: Run the merge tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_external_subagents.py -k "merge_agents_args" -v
```

Expected: FAIL because `merge_agents_args` is absent or does not enforce the matrix.

- [ ] **Step 6: Implement one pre-terminator scan and deterministic merge**

Scan indices only until the first exact `--`, returning both the located option and the terminator index. Recognize `--agents` plus its next token and `--agents=<value>`. Do not treat prefixes such as `--agents-file` as matches. Parse only when `generated` is non-empty. Serialize once with `sort_keys=True`, compact separators, and `ensure_ascii=True`; never log or interpolate user JSON into exceptions.

Emission has two cases. With a user option, replace its token span in place. Without one, insert at the terminator index when a `--` exists and only otherwise at `len(args)` — a plain append would place AGW's option after the terminator, where Claude reads it as prompt text rather than an option. Treat a `--agents` whose next token is the terminator as a missing value, not as a value of `"--"`.

- [ ] **Step 7: Run Task 1's passing gate**

Run:

```bash
uv run pytest tests/unit/test_external_subagents.py -v
uv run ruff check src/agent_gateway/external_subagents.py tests/unit/test_external_subagents.py
uv run mypy src/agent_gateway/external_subagents.py
```

Expected: all tests PASS, Ruff reports no errors, and mypy reports `Success: no issues found`.

- [ ] **Step 8: Review the narrow diff**

Run:

```bash
git diff --check -- src/agent_gateway/external_subagents.py tests/unit/test_external_subagents.py
```

Expected: no output. Confirm no existing file, especially a Council file, changed in this task.

---

### Task 2: Claude Executable Identity, Policy, and Compatibility Store

**Files:**
- Create: `src/agent_gateway/claude_compat.py`
- Create: `tests/unit/test_claude_compat.py`
- Modify: `src/agent_gateway/paths.py`
- Modify: `tests/unit/test_paths.py`

**Interfaces:**
- Consumes: `DEFINITION_CONTRACT_VERSION`, `GatewayConfig`, `ModelEntry`, `Paths`, `ProxyState.runtime_fingerprint`, `atomic_write_text`, and `read_claude_settings`.
- Produces:
  - `COMPATIBILITY_SCHEMA: Final[int] = 1`
  - `CLAUDE_VERSION_TIMEOUT_SECONDS: Final[float] = 5.0`
  - `utc_now() -> datetime` returning `datetime.now(tz=UTC)`; the codebase has no such helper yet, so this module owns it and Task 7 imports it rather than defining a second one
  - `ClaudeExecutableIdentity(configured_path: str, resolved_path: str, sha256: str, version: str)`
  - `CompatibilityIdentity(model: str, provider: Provider, claude: ClaudeExecutableIdentity, runtime_fingerprint: str, definition_contract: int)`
  - `CompatibilityRecord(schema: int, identity: CompatibilityIdentity, verified_at: datetime, outcome: Literal["passed", "failed"], detail: str)` — `verified_at` is an aware UTC `datetime` in Python and serializes to the `"2026-08-14T12:00:00Z"` string form through `model_dump(mode="json")`
  - `CompatibilityState(StrEnum)` with `current`, `missing`, `invalid`, `failed`, `stale`, `policy_excluded`
  - `CompatibilityStatus(state: CompatibilityState, model: str, provider: Provider, detail: str, record: CompatibilityRecord | None)`
  - `resolve_claude_identity(config: GatewayConfig, *, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> ClaudeExecutableIdentity`
  - `evaluate_available_models(model: str, settings: Sequence[tuple[Path, dict[str, object]]]) -> tuple[Path, ...]`
  - `compatibility_status(paths: Paths, identity: CompatibilityIdentity, *, settings: Sequence[tuple[Path, dict[str, object]]]) -> CompatibilityStatus`
  - `invalidate_compatibility(paths: Paths, identity: CompatibilityIdentity, *, now: datetime) -> None`
  - `write_compatibility_record(paths: Paths, record: CompatibilityRecord) -> None`
  - `read_compatibility_record(paths: Paths, model: str, provider: Provider) -> CompatibilityRecord | None`
- Extends `Paths` with `external_subagents_dir`, `compatibility_dir`, `probe_dir`, and `compatibility_file(model, provider)`.

- [ ] **Step 1: Write path-layout and safe-filename tests**

```python
def test_external_subagent_paths_stay_under_state_root(paths):
    assert paths.external_subagents_dir == paths.state_dir / "external-subagents"
    assert paths.compatibility_dir == paths.external_subagents_dir / "compatibility"
    assert paths.probe_dir == paths.external_subagents_dir / "probes"
    path = paths.compatibility_file("gpt-5.6-sol", "chatgpt")
    assert path.parent == paths.compatibility_dir / "chatgpt"
    assert path.name == hashlib.sha256(b"gpt-5.6-sol").hexdigest() + ".json"
```

The method signature is `compatibility_file(self, model: str, provider: str) -> Path`; reject a provider segment not matching `[a-z0-9][a-z0-9_-]*` with `ValueError("invalid provider path segment")`.

- [ ] **Step 2: Run the path test and verify RED**

Run:

```bash
uv run pytest tests/unit/test_paths.py::test_external_subagent_paths_stay_under_state_root -v
```

Expected: FAIL with `AttributeError: 'Paths' object has no attribute 'external_subagents_dir'`.

- [ ] **Step 3: Add only the fixed `Paths` properties**

```python
@property
def external_subagents_dir(self) -> Path:
    return self.state_dir / "external-subagents"

@property
def compatibility_dir(self) -> Path:
    return self.external_subagents_dir / "compatibility"

@property
def probe_dir(self) -> Path:
    return self.external_subagents_dir / "probes"
```

`compatibility_file()` hashes the exact UTF-8 public model and keeps only the validated provider as a directory component.

- [ ] **Step 4: Write executable-identity tests**

Create a stable symlink pointing to a fake executable target. Inject a fake `run` returning exactly `stdout="2.1.232 (Claude Code)\n"`. Assert:

```python
assert identity.configured_path == str(stable_path)
assert identity.resolved_path == str(target.resolve())
assert identity.sha256 == executable_sha256(target)
assert identity.version == "2.1.232 (Claude Code)"
```

Add cases for missing configured path, dangling/non-executable target, `TimeoutExpired`, non-zero exit, empty stdout, multiline stdout, and non-UTF-8 decode failure. Required `PrerequisiteError` messages:

```text
the native Claude executable path is not configured.
native Claude executable is invalid: <stable-path>
could not read the native Claude version within 5s.
native Claude version command failed with exit code <n>.
native Claude version output must be one non-empty line.
```

Never include stderr in the diagnostic. Invoke exactly `[str(configured_path), "--version"]` with `capture_output=True`, `text=True`, `encoding="utf-8"`, `errors="strict"`, `timeout=5.0`, and `check=False`.

- [ ] **Step 5: Run identity tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_claude_compat.py -k "claude_identity" -v
```

Expected: collection fails because `agent_gateway.claude_compat` does not exist.

- [ ] **Step 6: Implement identity dataclasses and bounded resolution**

Keep the configured absolute path (including its stable final symlink) and separately resolve/hash the canonical target. Convert `ConfigError` from existing path validation into `PrerequisiteError` with the fixed message and original remediation hint. Do not alter `validate_native_claude_path()`.

- [ ] **Step 7: Write conservative policy tests**

```python
def test_any_nonempty_available_models_exclusion_wins(tmp_path):
    settings = [
        (tmp_path / "user.json", {"availableModels": ["gpt-5.6-sol", "sonnet"]}),
        (tmp_path / "project.json", {"availableModels": ["sonnet"]}),
    ]
    assert evaluate_available_models("gpt-5.6-sol", settings) == (tmp_path / "project.json",)
```

Lock these semantics:

- absent key, empty list, or non-list value does not exclude by itself;
- for a valid non-empty list, only string elements count; exact case-sensitive name matching is required;
- a valid non-empty list with no exact name excludes the model;
- return all excluding paths in input order without reading or writing anything else;
- collision diagnostics later inspect top-level `agents` objects in the same settings payloads, but policy evaluation never guesses uninspectable managed state.

- [ ] **Step 8: Write record-schema and classification tests**

Use this exact canonical JSON shape. The stored key is `schema`, but a Pydantic field of that name shadows `BaseModel.schema` and emits a `UserWarning` at class-definition time — visible on ordinary `agw` commands. Declare the field as `schema_version: int = Field(alias="schema")` with `populate_by_name=True`, so the constructor still accepts `schema=1` and the on-disk JSON is unchanged:

```json
{
  "schema": 1,
  "identity": {
    "model": "gpt-5.6-sol",
    "provider": "chatgpt",
    "claude": {
      "configured_path": "/home/me/.local/bin/claude",
      "resolved_path": "/home/me/.local/share/claude/versions/2.1.232",
      "sha256": "<64 lowercase hex>",
      "version": "2.1.232 (Claude Code)"
    },
    "runtime_fingerprint": "<64 lowercase hex>",
    "definition_contract": 1
  },
  "verified_at": "2026-08-14T12:00:00Z",
  "outcome": "passed",
  "detail": "exact native subagent route verified"
}
```

Assert classifications and exact details:

| State | Condition | `detail` |
|---|---|---|
| `policy_excluded` | any inspectable allowlist excludes exact model | `excluded by Claude availableModels in: <comma-separated paths>` |
| `missing` | no file | `external subagent not verified` |
| `invalid` | unreadable JSON, wrong root/schema, Pydantic validation failure, wrong filename payload model/provider | `external subagent evidence is invalid` |
| `failed` | matching identity, `outcome="failed"` | stored sanitized detail |
| `stale` | valid record but any identity field differs | `external subagent evidence is stale` |
| `current` | exact identity and `outcome="passed"` | `exact native subagent route verified` |

Assert each identity component independently causes `stale`: configured path, resolved path, executable digest, exact version output, model, provider, runtime fingerprint, or definition contract.

- [ ] **Step 9: Run policy/store tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_claude_compat.py -k "available_models or compatibility" -v
```

Expected: FAIL because policy/storage functions are absent.

- [ ] **Step 10: Implement strict Pydantic schema and one-record storage**

Use frozen `BaseModel` classes with `extra="forbid"`; validate SHA-256 and runtime fingerprints as 64 lowercase hex, `verified_at` as an aware UTC `datetime`, `schema_version == 1`, and `definition_contract >= 1`. Serialize with `model_dump(mode="json", by_alias=True)`, sorted/indented JSON, trailing newline, and `atomic_write_text(..., mode=0o600)` after `ensure_dir(parent, 0o700)`.

`invalidate_compatibility()` overwrites the model's record before the probe with `outcome="failed"`, the supplied current identity, UTC time, and `detail="verification in progress; previous success invalidated"`. It must never preserve an old passing record while re-verification is running.

- [ ] **Step 11: Run Task 2's passing gate**

Run:

```bash
uv run pytest tests/unit/test_paths.py tests/unit/test_claude_compat.py -v
uv run ruff check src/agent_gateway/paths.py src/agent_gateway/claude_compat.py tests/unit/test_paths.py tests/unit/test_claude_compat.py
uv run mypy src/agent_gateway/paths.py src/agent_gateway/claude_compat.py
```

Expected: all tests PASS, Ruff clean, mypy clean.

- [ ] **Step 12: Review the narrow diff**

Run:

```bash
git diff --check -- src/agent_gateway/paths.py src/agent_gateway/claude_compat.py tests/unit/test_paths.py tests/unit/test_claude_compat.py
```

Expected: no output; complete records use `atomic_write_text`, while no append-only event helper has been added to `paths.py`.

---

### Task 3: Secure Append-Only Probe Event Files

**Files:**
- Create: `src/agent_gateway/probe_events.py`
- Create: `tests/unit/test_probe_events.py`

**Interfaces:**
- Produces:
  - `PROBE_EVENT_SCHEMA: Final[int] = 1`
  - `ProbeFileIdentity(path: str, device: int, inode: int, uid: int, mode: int)`
  - `create_probe_file(probe_dir: Path, nonce: str) -> ProbeFileIdentity`
  - `validate_probe_file(probe_dir: Path, identity: ProbeFileIdentity) -> None` — pure path/owner/mode/type/containment/device/inode validation that raises before any descriptor is handed out; it never returns or leaks a file descriptor, so no caller owns a close. `append_probe_event()` and `read_probe_events()` each call it, then open their own descriptor and re-validate through `fstat()` before touching bytes.
  - `append_probe_event(probe_dir: Path, identity: ProbeFileIdentity, event: Mapping[str, object]) -> None`
  - `read_probe_events(probe_dir: Path, identity: ProbeFileIdentity) -> tuple[dict[str, object], ...]`
  - `remove_probe_file(probe_dir: Path, identity: ProbeFileIdentity) -> None`
- Consumed by both the router and hook/verifier module; therefore this module imports no router, Claude, provider, or CLI code.

- [ ] **Step 1: Write safe creation and append tests**

```python
def test_create_and_append_probe_event_is_restrictive(probe_dir):
    identity = create_probe_file(probe_dir, "a" * 32)
    assert stat.S_IMODE(probe_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(Path(identity.path).stat().st_mode) == 0o600
    append_probe_event(probe_dir, identity, {"schema": 1, "event": "pre_tool_use"})
    assert read_probe_events(probe_dir, identity) == (
        {"schema": 1, "event": "pre_tool_use"},
    )
```

Require nonce regex `[0-9a-f]{32}` and exact path `<probe_dir>/<nonce>.ndjson`. Creation uses `O_WRONLY | O_CREAT | O_EXCL | O_APPEND` plus `O_NOFOLLOW` where available; reject existing paths rather than truncate.

- [ ] **Step 2: Add the adversarial filesystem matrix**

Tests must prove each append fails with `ProxyError` before writing when:

- the provided path escapes the canonical dedicated directory;
- any directory or file is a symlink;
- file owner UID differs from `os.getuid()`;
- directory mode is not `0700` or file mode is not `0600`;
- target is not a regular file;
- path's device/inode differs from the pre-created `ProbeFileIdentity`;
- `fstat(fd)` differs from the path's just-validated `lstat()` identity;
- serialized UTF-8 record including newline is `>= os.fpathconf(fd, "PC_PIPE_BUF")`;
- the event is not JSON-serializable or is not a mapping.

Required generic message (do not leak event content):

```text
probe event file failed safety validation.
```

- [ ] **Step 3: Run secure-event tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_probe_events.py -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'agent_gateway.probe_events'`.

- [ ] **Step 4: Implement validation-before-every-append**

Validation order:

1. Canonically resolve the dedicated directory itself without following a candidate file symlink; require it to be a real directory owned by the current UID and mode `0700`.
2. Require `Path(identity.path).parent == probe_dir` lexically and its resolved parent/device to equal the validated directory.
3. `lstat()` the file; require regular, current UID, mode `0600`, and matching device/inode/uid/mode from `ProbeFileIdentity`.
4. Open with `O_WRONLY | O_APPEND | O_NOFOLLOW`; `fstat()` and re-`lstat()`; require all three identities to match.
5. Serialize canonical compact `sort_keys=True`, `ensure_ascii=True` NDJSON; compare byte length including `\n` against `PIPE_BUF`; call one `os.write(fd, record)` and require a full write.

`read_probe_events()` repeats the same path/owner/mode/type/device/inode validation, caps total file size at 64 KiB, rejects blank/malformed/non-object lines with `ProxyError("probe event file is invalid.")`, and never repairs content. `remove_probe_file()` validates directory containment and unlinks only if the current device/inode still match; a mismatch leaves the file untouched and raises the generic safety error.

- [ ] **Step 5: Run Task 3's passing gate**

Run:

```bash
uv run pytest tests/unit/test_probe_events.py -v
uv run ruff check src/agent_gateway/probe_events.py tests/unit/test_probe_events.py
uv run mypy src/agent_gateway/probe_events.py
```

Expected: all tests PASS, Ruff clean, mypy clean.

- [ ] **Step 6: Review the narrow diff**

Run:

```bash
git diff --check -- src/agent_gateway/probe_events.py tests/unit/test_probe_events.py
```

Expected: no output. Confirm no event value appears in any exception message.

---

### Task 4: One-Shot Router Probe Registration and Local Route Evidence

**Files:**
- Modify: `src/agent_gateway/router.py`
- Modify: `tests/unit/test_router.py`

**Interfaces:**
- Consumes: `ModelRegistry`, `Provider`, `ProbeFileIdentity`, and `append_probe_event()`.
- Produces inside `router.py`:
  - `SUBAGENT_PROBE_PATH: Final[str] = "/_agw/control/v1/subagent-probes"`
  - `SUBAGENT_PROBE_TTL_SECONDS: Final[float] = 60.0`
  - `ProbeRegistration(agent_id: str, model: str, provider: Provider, nonce: str, file: ProbeFileIdentity, expires_at: float)`
  - `OneShotProbeRegistry(probe_dir: Path, *, clock: Callable[[], float] = time.monotonic)`
  - `register(registration: ProbeRegistration) -> None`
  - `consume(*, agent_id: str, requested_model: str, resolved_model: str, provider: Provider, request_kind: str, timestamp: str) -> bool`
- Extends `RouterSettings` with `probe_registry: OneShotProbeRegistry | None = None` so ordinary unit fixtures and sessions default to no evidence.

- [ ] **Step 1: Write one-shot registry tests with an injected clock**

```python
def test_probe_registration_is_exact_and_one_shot(probe_registry, probe_identity):
    probe_registry.register(registration(probe_identity, expires_at=160.0))
    assert probe_registry.consume(
        agent_id="agent-1",
        requested_model="gpt-5.6-sol",
        resolved_model="gpt-5.6-sol",
        provider=Provider.chatgpt,
        request_kind="messages",
        timestamp="2026-08-14T12:00:00Z",
    )
    assert not probe_registry.consume(...same exact values...)
```

Add cases for wrong agent ID, requested model, resolved model, provider, missing lifecycle header, and expired `expires_at <= clock()`. A mismatch must not consume another registration. Expired registrations are removed on `register()` and `consume()`. Reject duplicate nonce or duplicate agent ID with `ValueError`, and cap live registrations at 32 with `ProxyError("too many active subagent probe registrations.")`.

Both raises stay inside the router process: the control endpoint maps `ValueError` to HTTP 409 and the cap `ProxyError` to HTTP 429, so no new user-facing exception category reaches the CLI. Only the hook's failed registration surfaces to the verifier, and it does so as the single `ProxyError` named in the global error mapping.

The route record is exactly:

```json
{
  "schema": 1,
  "event": "route",
  "nonce": "<32 lowercase hex>",
  "agent_id": "agent-1",
  "requested_model": "gpt-5.6-sol",
  "resolved_model": "gpt-5.6-sol",
  "provider": "chatgpt",
  "request_kind": "messages",
  "timestamp": "2026-08-14T12:00:00Z"
}
```

- [ ] **Step 2: Run registry tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_router.py -k "probe_registration" -v
```

Expected: FAIL because `OneShotProbeRegistry` is not defined.

- [ ] **Step 3: Implement the lock-protected in-memory registry**

Use `threading.Lock`; bind each registration to both nonce and agent ID; never serialize registrations. `consume()` calls `append_probe_event()` while the matching registration remains reserved, then removes it only after a successful append. If append fails, remove the registration and raise `ProxyError`; verification then fails closed.

- [ ] **Step 4: Write control-endpoint tests**

Use Starlette's `TestClient` with `client=("127.0.0.1", 50000)` and assert:

- missing/wrong `X-AGW-Key` -> 401;
- non-loopback peer (`198.51.100.2`) -> 403 even with the key;
- missing registry -> 503;
- malformed/non-object JSON or extra/missing fields -> 400;
- nonce not `[0-9a-f]{32}`, empty/oversized agent ID, invalid file identity -> 400;
- exact model inactive/missing or provider different from its active registry owner -> 409;
- unsafe/out-of-directory event identity -> 409;
- duplicate nonce or duplicate agent ID (the registry's `ValueError`) -> 409;
- more than 32 live registrations (the registry's cap `ProxyError`) -> 429;
- success -> 201 body `{"ok": true, "expires_in": 60}`;
- request/response diagnostics contain neither key nor event path.

Registration body is exact and `extra="forbid"`:

```json
{
  "schema": 1,
  "nonce": "<32 lowercase hex>",
  "agent_id": "agent-1",
  "model": "gpt-5.6-sol",
  "provider": "chatgpt",
  "event_file": {
    "path": "<absolute dedicated path>",
    "device": 1,
    "inode": 2,
    "uid": 501,
    "mode": 384
  }
}
```

The endpoint computes `expires_at = monotonic_now + 60.0`; callers never choose expiry.

- [ ] **Step 5: Write lifecycle-header and no-ordinary-evidence tests**

For an external request, send:

```python
headers={
    "X-AGW-Key": "local-key",
    "x-claude-code-agent-id": "agent-1",
    "x-claude-code-parent-agent-id": "parent-1",
    "X-Claude-Code-Session-Id": "session-1",
}
```

Assert upstream omits both agent lifecycle headers while preserving `X-Claude-Code-Session-Id` under the existing contract. Register exact evidence and assert one route event; repeat without registration and assert no file mutation. Native Anthropic requests generate no route event. Add `count_tokens` and `messages` cases with request kinds exactly `count_tokens` and `messages`.

- [ ] **Step 6: Run endpoint/header tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_router.py -k "probe_control or lifecycle_header or route_evidence" -v
```

Expected: FAIL because the route and filtering/correlation do not exist.

- [ ] **Step 7: Implement endpoint, validation, header stripping, and correlation**

Add the control route before the catch-all message routes. Derive `agent_id` only from the inbound `x-claude-code-agent-id`. Capture `requested_model` before rewriting payload; use `external_entry.name/provider` as resolved values. The router formats `timestamp` itself with `datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")` — do not import `utc_now` from `claude_compat`, because the router must stay free of the config/Pydantic import chain. Call `consume()` before upstream streaming so exact local route evidence exists even if the external provider later fails; the verifier still separately requires successful child completion. Strip these external-only headers case-insensitively:

```python
_EXTERNAL_LOCAL_HEADERS: Final = {
    "authorization",
    "x-api-key",
    "x-claude-code-agent-id",
    "x-claude-code-parent-agent-id",
}
```

Do not strip `X-Claude-Code-Session-Id`; it remains part of Claude Code's existing protocol unless a separate security design changes it.

- [ ] **Step 8: Run Task 4's passing gate**

Run:

```bash
uv run pytest tests/unit/test_router.py -v
uv run ruff check src/agent_gateway/router.py tests/unit/test_router.py
uv run mypy src/agent_gateway/router.py
```

Expected: all router tests PASS, Ruff clean, mypy clean.

- [ ] **Step 9: Review the narrow diff**

Run:

```bash
git diff --check -- src/agent_gateway/router.py tests/unit/test_router.py
```

Expected: no output. Search the route-event schema and verify it has no prompt/body/header/tool field.

---

### Task 5: Router Process Contract and Runtime ABI Invalidation

**Files:**
- Modify: `src/agent_gateway/proxy.py`
- Modify: `src/agent_gateway/supervisor.py`
- Modify: `src/agent_gateway/router.py`
- Modify: `tests/unit/test_supervisor.py`
- Modify: `tests/contract/test_proxy_lifecycle.py`

**Interfaces:**
- Consumes: `Paths.probe_dir`, `OneShotProbeRegistry`, current `ProxyState`, current provider auth, and secret-scrubbed router environment.
- Produces: router CLI `--probe-dir PATH`, supervisor CLI `--probe-dir PATH`, and `RUNTIME_ABI = "6-subagent-probe-control"`.

- [ ] **Step 1: Write supervisor propagation and secret-boundary tests**

Assert `_parse_args()` requires `--probe-dir`. Capture `_spawn()` calls and assert router argv contains exactly one `--probe-dir <paths.probe_dir>`. Assert router env retains `AGW_LOCAL_KEY` and `AGW_LITELLM_KEY` but strips `CHATGPT_TOKEN_DIR`, `CHATGPT_AUTH_FILE`, `GITHUB_COPILOT_TOKEN_DIR`, and `LITELLM_MASTER_KEY`. Assert no key appears in argv.

- [ ] **Step 2: Run supervisor tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_supervisor.py -k "probe_dir or router_env" -v
```

Expected: FAIL because `--probe-dir` is absent.

- [ ] **Step 3: Thread only the dedicated path through proxy and supervisor**

In `_spawn_supervisor()`, call `ensure_dir(paths.probe_dir, 0o700)` and add:

```python
"--probe-dir", str(paths.probe_dir)
```

The supervisor forwards that argument to the router. Router `main()` creates exactly one `OneShotProbeRegistry(Path(args.probe_dir))` and injects it into `RouterSettings`. Do not add the path to `ProxyState`, logs, user-visible status, or evidence records.

- [ ] **Step 4: Write ABI/fingerprint contract tests**

Assert:

```python
assert proxy.RUNTIME_ABI == "6-subagent-probe-control"
```

Retain the existing test that a differing runtime ABI yields a different runtime fingerprint and makes a live old daemon fail with `ProxyError("a proxy is running with a different config/version.")` rather than silently reusing/restarting it.

- [ ] **Step 5: Run ABI test and verify RED**

Run:

```bash
uv run pytest tests/contract/test_proxy_lifecycle.py -k "runtime_abi or fingerprint_mismatch" -v
```

Expected: FAIL because the ABI remains `5-litellm-readiness`.

- [ ] **Step 6: Bump the ABI and preserve provider authentication**

Change only:

```python
RUNTIME_ABI = "6-subagent-probe-control"
```

Do not alter `_require_authenticated_providers()`: it must continue checking every provider in `{model.provider for model in registry.active_models()}` before launch.

- [ ] **Step 7: Run Task 5's passing gate**

Run:

```bash
uv run pytest tests/unit/test_supervisor.py tests/contract/test_proxy_lifecycle.py -v
uv run ruff check src/agent_gateway/proxy.py src/agent_gateway/supervisor.py src/agent_gateway/router.py tests/unit/test_supervisor.py tests/contract/test_proxy_lifecycle.py
uv run mypy src/agent_gateway/proxy.py src/agent_gateway/supervisor.py src/agent_gateway/router.py
```

Expected: tests PASS, including native-only startup, concurrent launch convergence, and provider-auth fail-fast coverage.

- [ ] **Step 8: Review the narrow diff**

Run:

```bash
git diff --check -- src/agent_gateway/proxy.py src/agent_gateway/supervisor.py src/agent_gateway/router.py tests/unit/test_supervisor.py tests/contract/test_proxy_lifecycle.py
```

Expected: no output. Confirm neither local key nor internal key moved into process arguments.

---

### Task 6: Hook Protocol and Synchronous `SubagentStart` Registration

**Files:**
- Create: `src/agent_gateway/subagent_probe.py`
- Create: `tests/unit/test_subagent_probe.py`
- Modify: `docs/superpowers/specs/2026-08-14-claude-external-subagents-design.md`

**Interfaces:**
- Consumes: `ProbeFileIdentity`, secure event APIs, `SUBAGENT_PROBE_PATH`, and `X-AGW-Key` supplied only through environment/stdin-safe process state.
- Produces:
  - Hook entry point `hook_main(argv: Sequence[str] | None = None, *, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int`
  - CLI forms `python -m agent_gateway.subagent_probe hook pre-tool-use` and `python -m agent_gateway.subagent_probe hook subagent-start`
  - `HookContext` — a frozen dataclass built from the environment by `hook_context_from_env(env: Mapping[str, str]) -> HookContext`, carrying the probe directory, `ProbeFileIdentity`, nonce, expected agent type/model/provider, router URL, and local key
  - `run_pre_tool_use_hook(context: HookContext, payload: Mapping[str, object]) -> tuple[int, dict[str, object]]` and `run_subagent_start_hook(context: HookContext, payload: Mapping[str, object]) -> int` — the two testable bodies `hook_main()` dispatches to, so tests never need a subprocess
  - `RouterProbeRegistration` — a frozen dataclass mirroring the endpoint body exactly (`schema`, `nonce`, `agent_id`, `model`, `provider`, `event_file`), with `to_dict()` producing that JSON
  - `register_probe(router_url: str, local_key: str, registration: RouterProbeRegistration, *, transport: httpx.BaseTransport | None = None) -> None`
  - `pre_tool_use_event(payload: Mapping[str, object], expected_type: str, existing_events: Sequence[Mapping[str, object]]) -> dict[str, object]`
  - `subagent_start_event(payload: Mapping[str, object], expected_type: str) -> dict[str, object]`
  - `pre_tool_use_decision(reason: str | None = None) -> dict[str, object]` — `None` yields the allow response; any string yields the deny response carrying that reason
- The same synchronous `subagent-start` helper must append its event, call `register_probe()`, and return success only after HTTP 201.

- [ ] **Step 1: Write exact `PreToolUse` protocol tests**

Input payload examples contain extra Claude fields, but output evidence is only:

```json
{
  "schema": 1,
  "event": "pre_tool_use",
  "tool_use_id": "toolu_1",
  "subagent_type": "agw-gpt-5-6-sol-66127d2195a1",
  "explicit_model_absent": true
}
```

Assert allowed stdout is:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow"
  }
}
```

Assert wrong type, a present `model` key (even null/empty), or a prior `pre_tool_use` event yields exit code 0 plus the modern deny protocol:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "AGW native subagent probe permits exactly one Agent call to the expected generated type without an explicit model override."
  }
}
```

Do not rely on exit code 2 or deprecated top-level `decision: "block"`. Malformed hook input, unsafe event file, or missing environment returns non-zero with only `subagent probe hook failed` on stderr; no payload or secret is echoed.

- [ ] **Step 2: Run pre-tool tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_subagent_probe.py -k "pre_tool_use" -v
```

Expected: collection fails because `agent_gateway.subagent_probe` does not exist.

- [ ] **Step 3: Implement minimal validation, evidence append, and modern decisions**

Read hook JSON only from stdin (limit 64 KiB). Read these environment variables, never argv:

```text
AGW_PROBE_DIR
AGW_PROBE_EVENT_FILE
AGW_PROBE_EVENT_DEVICE
AGW_PROBE_EVENT_INODE
AGW_PROBE_EVENT_UID
AGW_PROBE_EVENT_MODE
AGW_PROBE_NONCE
AGW_PROBE_EXPECTED_AGENT_TYPE
AGW_PROBE_EXPECTED_MODEL
AGW_PROBE_EXPECTED_PROVIDER
AGW_PROBE_ROUTER_URL
AGW_PROBE_LOCAL_KEY
```

The key remains process environment only and is never persisted or printed. For `pre-tool-use`, validate `hook_event_name == "PreToolUse"`, `tool_name == "Agent"`, string `tool_use_id`, dict `tool_input`, exact `subagent_type`, absent `model`, and no existing `pre_tool_use` event before append.

- [ ] **Step 4: Write the race-free `SubagentStart` ordering test**

```python
def test_subagent_start_appends_then_registers_before_return(
    monkeypatch, hook_context, start_payload
):
    calls = []
    monkeypatch.setattr(subagent_probe, "append_probe_event", lambda *a: calls.append("append"))
    monkeypatch.setattr(subagent_probe, "register_probe", lambda *a, **k: calls.append("register"))
    assert subagent_probe.run_subagent_start_hook(hook_context, start_payload) == 0
    assert calls == ["append", "register"]
```

Assert start evidence is exactly:

```json
{
  "schema": 1,
  "event": "subagent_start",
  "agent_id": "agent-1",
  "agent_type": "agw-gpt-5-6-sol-66127d2195a1"
}
```

Assert wrong/missing type/ID, duplicate start evidence, connection failure, timeout, non-201, or response not exactly `{"ok": true, "expires_in": 60}` returns non-zero. `register_probe()` uses `httpx.post(f"{router_url}{SUBAGENT_PROBE_PATH}", headers={"X-AGW-Key": local_key}, json=..., timeout=5.0)`; map every failure to `ProxyError("could not register the native subagent probe with the local router.")` without response text.

- [ ] **Step 5: Run start/registration tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_subagent_probe.py -k "subagent_start or register_probe" -v
```

Expected: FAIL because synchronous append/registration is absent.

- [ ] **Step 6: Implement synchronous start registration**

`SubagentStart` validates `hook_event_name`, expected `agent_type`, and non-empty `agent_id`; appends the sanitized start record; builds the exact registration body with the pre-created file identity; calls the router synchronously; and returns only after successful registration. Never poll for the start event in the parent verifier and never register after the hook returns.

- [ ] **Step 7: Correct the approved spec's stale race description**

Replace the stale sequence in the `Route correlation` section so item 3 says the same `SubagentStart` helper appends and registers before returning, item 4 says Claude's synchronous hook drain closes the first-request race, and item 5 joins evidence after child exit. Preserve the statement that hook failure may not prevent Claude startup; absent exact route evidence still makes verification fail closed.

- [ ] **Step 8: Run Task 6's passing gate**

Run:

```bash
uv run pytest tests/unit/test_subagent_probe.py -k "pre_tool_use or subagent_start or register_probe" -v
uv run ruff check src/agent_gateway/subagent_probe.py tests/unit/test_subagent_probe.py
uv run mypy src/agent_gateway/subagent_probe.py
```

Expected: hook tests PASS, Ruff clean, mypy clean.

- [ ] **Step 9: Review the narrow diff**

Run:

```bash
git diff --check -- src/agent_gateway/subagent_probe.py tests/unit/test_subagent_probe.py docs/superpowers/specs/2026-08-14-claude-external-subagents-design.md
```

Expected: no output. Confirm the spec no longer authorizes registration through polling after `SubagentStart`.

---

### Task 7: Bounded Verifier Definitions, Isolated Settings, and Process Lifecycle

**Files:**
- Modify: `src/agent_gateway/subagent_probe.py`
- Modify: `src/agent_gateway/process.py`
- Modify: `tests/unit/test_subagent_probe.py`
- Modify: `tests/unit/test_process.py`

**Interfaces:**
- Consumes: Task 1 generated worker, Task 2 identity/store, Task 3 event file, Task 6 hooks, `ProxyState.url`, and existing `ProcessIdentity`.
- Produces:
  - `VERIFIER_PARENT_TYPE: Final[str] = "agw-native-subagent-verifier"`
  - `PROBE_PROCESS_TIMEOUT_SECONDS: Final[float] = 90.0`
  - `PROBE_TERM_TIMEOUT_SECONDS: Final[float] = 5.0`
  - `PROBE_KILL_TIMEOUT_SECONDS: Final[float] = 5.0`
  - `PopenFactory` — the injection alias `Callable[..., subprocess.Popen[str]]`, so tests substitute a fake process without patching `subprocess`
  - `build_verifier_agents(worker: GeneratedAgent) -> dict[str, dict[str, object]]`
  - `build_probe_settings(hook_command: str, worker_type: str) -> dict[str, object]` — session hooks only; it takes no environment and no secret, so the settings JSON cannot carry one
  - `build_probe_env(base_env: Mapping[str, str], context: ProbeContext) -> dict[str, str]` — the only place the hook variables and `X-AGW-Key` are assembled, and they reach Claude solely through `Popen(env=...)`
  - `build_probe_argv(executable: Path, worker: GeneratedAgent, settings_json: str, agents_json: str, prompt: str) -> list[str]`
  - `terminate_process_group(identity: ProcessIdentity, *, term_timeout: float = 5.0, kill_timeout: float = 5.0) -> bool`
  - `NativeSubagentVerifyReport(model: str, provider: str, ok: bool, detail: str)` — mirrors the existing `VerifyReport` (`model`, `provider`, `ok`) and replaces its `checks` list with one sanitized `detail`
  - `verify_native_subagent(paths: Paths, config: GatewayConfig, registry: ModelRegistry, state: ProxyState, local_key: str, entry: ModelEntry, *, now: Callable[[], datetime] = utc_now, popen: PopenFactory = subprocess.Popen) -> NativeSubagentVerifyReport`
- `ProbeContext` is this task's own frozen dataclass carrying the run directory, `ProbeFileIdentity`, nonce, fixture filename, expected agent type/model/provider, router URL, and local key. It is never serialized to disk and never embedded in settings or argv.

- [ ] **Step 1: Write exact verifier-agent and isolated-settings tests**

The worker definition is Task 1's exact definition. The verifier parent definition is exactly:

```json
{
  "description": "Run one controlled AGW native-subagent compatibility check.",
  "prompt": "Call the expected generated AGW agent exactly once. Omit the Agent model override. Ask it to read the supplied one-line fixture and return its content. Do not perform any other task.",
  "model": "sonnet",
  "tools": ["Agent"],
  "maxTurns": 4
}
```

The initial prompt is exactly:

```text
Use Agent exactly once with subagent_type {worker_type}, omit model, and ask it to read {fixture_name} and return the exact one-line content. Then return only that child result.
```

`{fixture_name}` is the fixture's bare filename, not its absolute path: the child resolves it against the `cwd` set at launch, and the delegated prompt reaching the external provider therefore never contains AGW's state directory layout.

`build_probe_settings()` returns only session hooks:

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Agent",
      "hooks": [{"type": "command", "command": "<python>", "args": ["-I", "-m", "agent_gateway.subagent_probe", "hook", "pre-tool-use"], "timeout": 30}]
    }],
    "SubagentStart": [{
      "matcher": "<worker_type>",
      "hooks": [{"type": "command", "command": "<python>", "args": ["-I", "-m", "agent_gateway.subagent_probe", "hook", "subagent-start"], "timeout": 30}]
    }]
  }
}
```

`<python>` is `sys.executable` — AGW's own interpreter, already trusted by the packaged plugin hook — and `-I` matches that hook's isolation. Both timeouts are 30 seconds: each hook only validates JSON, appends one line, and makes one 5-second HTTP call, so a longer bound would only delay the child without adding capability. The 90-second process timeout and process-group kill remain the outer bound.

`build_probe_settings()` takes only the hook command and worker type, so no environment value can reach the settings JSON. `build_probe_env()` assembles the hook variables and `X-AGW-Key` separately, and they reach Claude only through `Popen(env=...)`. Assert the settings/agents/argv/prompt contain no key, provider path, token, nonce, event path, or parent transcript.

- [ ] **Step 2: Write exact bounded argv tests**

Lock argv to these flags and no unsupported `--max-turns`/`--safe-mode`:

```text
<configured executable>
--print
--output-format
stream-json
--include-hook-events
--no-session-persistence
--disable-slash-commands
--no-chrome
--verbose
--strict-mcp-config
--mcp-config
{"mcpServers":{}}
--tools
Agent,Read,Grep,Glob
--allowedTools
Agent,Read,Grep,Glob
--disallowedTools
Bash,Edit,Write,Skill,WebFetch,WebSearch,Workflow
--setting-sources
local
--settings
<compact isolated JSON>
--agents
<compact two-definition JSON>
--agent
agw-native-subagent-verifier
--max-budget-usd
1.00
<prompt>
```

The session-level `--tools`/`--allowedTools` value is the *union* of what the two definitions need, not the verifier parent's own contract: a session set of only `Agent` would remove `Read` from the built-in set and make the worker's required read round trip impossible. Per-agent restriction still comes from the definitions themselves — the verifier parent keeps `["Agent"]` and the worker keeps `["Read", "Grep", "Glob"]` — so the parent cannot read and the worker cannot spawn. Assert both definition allowlists and the session union in the same test so a future edit cannot silently widen one.

Listing `Grep` and `Glob` explicitly is deliberate: on native macOS/Linux builds those names resolve to dedicated tools only when named in `--tools`, and denying `Bash` can otherwise remove them. The asserted pass condition depends only on the `Read` round trip, so a build that still withholds `Grep`/`Glob` fails no gate.

`--setting-sources local` plus inline `--settings` retains mandatory managed policy but excludes user and project settings. Because `local` still resolves `.claude/settings.local.json` relative to the working directory, the probe process is launched with `cwd` set to its own dedicated run directory under `Paths.probe_dir`, which AGW creates and which contains no `.claude` tree — so the only session hooks are the two inline ones. `--strict-mcp-config --mcp-config {}` disables configured MCP. If installed Claude rejects a required flag, the explicit live gate fails with `PrerequisiteError`; never weaken isolation automatically to make a flag accepted.

- [ ] **Step 3: Run definition/argv tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_subagent_probe.py -k "verifier_agents or probe_settings or probe_argv" -v
```

Expected: FAIL because builders are absent.

- [ ] **Step 4: Implement deterministic builders**

Serialize settings and agents as compact sorted JSON. Pass `sys.executable` as `build_probe_settings()`'s `hook_command`. `build_probe_env()` starts from a scrubbed copy of `base_env` that removes provider secret vars, `LITELLM_MASTER_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, and `CLAUDE_CODE_SUBAGENT_MODEL`, then adds only the Task 6 hook variables plus the managed `ANTHROPIC_BASE_URL`/`ANTHROPIC_CUSTOM_HEADERS` pair built the same way `ClaudeHarness.build_env()` builds it. Assert the returned mapping has no other added key.

- [ ] **Step 5: Write identity-safe process-group termination tests**

Inject/match a process whose PID/create time belongs to a new session. Assert TERM goes to `os.killpg(os.getpgid(identity.pid), signal.SIGTERM)` only after the leader's create time still matches; wait boundedly; then KILL the same still-matching group. If the identity mismatches before either signal, return `True` without signaling. Access denied or a still-live matching group after KILL returns `False`.

- [ ] **Step 6: Run process tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_process.py -k "process_group" -v
```

Expected: FAIL because `terminate_process_group` is absent.

- [ ] **Step 7: Implement process-group cleanup without bare-PID signaling**

Use `identity_of()`, `is_alive()`, `psutil.wait_procs()` or bounded polling, and re-check the leader identity immediately before every `killpg`. Never call `os.kill(identity.pid, ...)` or signal a group if the leader PID has been reused.

- [ ] **Step 8: Write verifier outcome/evidence-join tests with fake `Popen`**

Each test calls `invalidate_compatibility()` first and then supplies events/process result. Passing evidence requires exactly one each, in order, joined by expected type/ID/nonce:

1. `pre_tool_use` with expected type and `explicit_model_absent=true`;
2. `subagent_start` with expected type and one non-empty agent ID;
3. `route` with same agent ID, nonce, exact requested/resolved model/provider, `request_kind="messages"`;
4. at least one successful verifier-parent result in the stream JSON, with the fixture's canary present in one of them.

The process exit code is **not** a pass condition. Claude Code exits `1` merely for printing `[claude-code:unrecognized_model]` about an external name it does not know, after routing it and completing successfully; the result object is authoritative. The exit code is used only to word the failure when there is no successful result at all. Likewise, scan *every* successful result rather than the first: Claude may background the child, answer immediately, and deliver the real content in a later result carrying `origin: {"kind": "task-notification"}`.

The canary is how the read round trip is proven, rather than scanning the stream for a `tool_use` named `Read`. The worker is the only agent holding `Read`, so its fixture content can reach the parent's final result only by the worker having actually read the file — and unlike the shape of subagent tool events in the parent's stream, that holds no matter how Claude Code chooses to forward child activity.

`verify_native_subagent()` always **returns** its report and never raises for an ordinary compatibility verdict, so the CLI can attempt every requested model before failing. Return `NativeSubagentVerifyReport(ok=False, ...)` while writing `outcome="failed"` and a sanitized detail for:

- process exits non-zero;
- timeout;
- missing/duplicate/malformed/unordered event;
- wrong type, ID, nonce, model, provider, or explicit model present;
- route only uses `count_tokens` but never `messages`;
- no successful `Read` tool round trip;
- warning/output contains case-insensitive `availableModels`, `not allowed`, `model restriction`, `substitut`, or `falling back`;
- plausible final text with no exact route evidence.

Two failure classes are *not* compatibility verdicts and keep the global error mapping instead, because the probe never reached a state where a verdict is meaningful: a hook/probe lifecycle or router-registration failure and a cleanup failure both raise `ProxyError` immediately, after writing the invalidation record. An unusable executable or version still raises `PrerequisiteError` from Task 2. Assert each of the three categories in its own test so the boundary cannot drift.

Never store stdout/stderr, warning text, prompt, child result, or event file path. Fixed successful detail: `exact native subagent route verified`. Fixed timeout detail: `native Claude subagent probe timed out`.

- [ ] **Step 9: Run verifier tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_subagent_probe.py -k "verify_native_subagent" -v
```

Expected: FAIL because orchestration/evidence joining is absent.

- [ ] **Step 10: Implement verifier orchestration and unconditional cleanup**

Order is fixed:

1. Resolve current `CompatibilityIdentity` and write invalidation record.
2. Generate an unpredictable `secrets.token_hex(16)` nonce and call `create_probe_file(paths.probe_dir, nonce)`. The event file stays directly in `Paths.probe_dir` so Task 3's lexical containment rule (`Path(identity.path).parent == probe_dir`) holds unchanged.
3. Create a `0700` per-run working directory `paths.probe_dir / f"run-{nonce}"` holding exactly one `0600` one-line fixture with an unpredictable non-secret canary. This directory is the child's `cwd`, so the child's working directory contains no AGW state other than the fixture it is asked to read. Keep both the path and the content out of stored evidence.
4. Build definitions/settings/argv through the builders, and the environment through `build_probe_env()`.
5. Launch with `cwd=<per-run working directory>`, `stdin=DEVNULL`, `stdout=PIPE`, `stderr=PIPE`, `text=True`, `start_new_session=True`; immediately capture `ProcessIdentity`. That `cwd` is what makes `--setting-sources local` resolve against a directory with no `.claude` tree.
6. `communicate(timeout=90.0)`; on timeout/cancellation/exception call `terminate_process_group()`.
7. Parse capped stream output and secure events; evaluate exact pass conditions.
8. Write one passed or failed compatibility record.
9. In `finally`, call `remove_probe_file()` for the event file, then remove the fixture and the now-empty per-run working directory. Registration expiry handles router cleanup; the event file removal prevents late writes from satisfying anything.

If cleanup itself fails, raise `ProxyError("could not clean native subagent probe state.")` and leave the compatibility outcome failed. Catch `KeyboardInterrupt`/`BaseException` only to clean and re-raise; never convert user cancellation into success.

- [ ] **Step 11: Run Task 7's passing gate**

Run:

```bash
uv run pytest tests/unit/test_process.py tests/unit/test_subagent_probe.py -v
uv run ruff check src/agent_gateway/process.py src/agent_gateway/subagent_probe.py tests/unit/test_process.py tests/unit/test_subagent_probe.py
uv run mypy src/agent_gateway/process.py src/agent_gateway/subagent_probe.py
```

Expected: tests PASS, Ruff clean, mypy clean.

- [ ] **Step 12: Review the narrow diff**

Run:

```bash
git diff --check -- src/agent_gateway/process.py src/agent_gateway/subagent_probe.py tests/unit/test_process.py tests/unit/test_subagent_probe.py
```

Expected: no output. Search argv/settings/evidence assertions to confirm secrets and content are absent.

---

### Task 8: Explicit `models verify --claude-subagent` CLI Mode

**Files:**
- Modify: `src/agent_gateway/cli.py`
- Modify: `tests/integration/test_cli.py`

**Interfaces:**
- Consumes: existing `resolve_models_to_verify()`, `proxy.ensure_running()`, `ensure_proxy_key()`, and `verify_native_subagent()`.
- Produces: Typer option `--claude-subagent/--no-claude-subagent` default `False`; direct verification path remains byte-for-byte behaviorally unchanged without the flag.

- [ ] **Step 1: Add CLI help and default-path regression tests**

Assert `agw models verify --help` contains:

```text
--claude-subagent  Run bounded native Claude and external-provider model calls to verify generated subagent routing.
```

Monkeypatch `verify_native_subagent` to raise if called; invoke `models verify gpt-5.6-sol` without the flag and assert the existing direct `verify_model()` path/output remains unchanged.

- [ ] **Step 2: Add flagged dispatch and omitted-model tests**

Monkeypatch all I/O. Assert:

- `models verify gpt-5.6-sol --claude-subagent` resolves one exact active entry and calls native verification once;
- `models verify --claude-subagent` retains `resolve_models_to_verify(registry, None)` and calls every active model in sorted registry order;
- before calls, output discloses: `Native subagent verification runs bounded Claude and external-provider model calls.`;
- one failed report eventually raises `ModelUnavailableError("one or more models failed native Claude subagent verification.")` after attempting every resolved entry;
- no ordinary test invokes a real process, Claude, proxy network, or provider.

- [ ] **Step 3: Run CLI tests and verify RED**

Run:

```bash
uv run pytest tests/integration/test_cli.py -k "claude_subagent or models_verify" -v
```

Expected: FAIL because `--claude-subagent` is unknown.

- [ ] **Step 4: Add a surgical option branch without touching Council**

Keep imports local in `models_verify()` as the file currently does. Call `proxy.ensure_running()` once, retain its `ProxyState`, and get the local key once. `NativeSubagentVerifyReport` and the existing `VerifyReport` are different types, so branch the whole per-entry body rather than assigning both to one variable — a shared name would not type-check under strict mypy:

```python
any_failed = False
for entry in entries:
    if claude_subagent:
        native = verify_native_subagent(paths, config, registry, state, key, entry)
        marker = "✓" if native.ok else "✗"
        color = typer.colors.GREEN if native.ok else typer.colors.RED
        typer.secho(f"{marker} {native.model} ({native.provider})", fg=color)
        typer.echo(f"    {marker} native subagent: {native.detail}")
        any_failed = any_failed or not native.ok
        continue

    report = verify_model(state, key, entry)  # existing rendering below, unchanged
    marker = "✓" if report.ok else "✗"
    color = typer.colors.GREEN if report.ok else typer.colors.RED
    typer.secho(f"{marker} {report.model} ({report.provider})", fg=color)
    for check in report.checks:
        symbol = "✓" if check.status is CheckStatus.passed else "✗"
        typer.echo(f"    {symbol} {check.name}: {check.detail}")
    any_failed = any_failed or not report.ok

if any_failed:
    raise ModelUnavailableError(
        "one or more models failed native Claude subagent verification."
        if claude_subagent
        else "one or more models failed Claude-compatibility verification."
    )
```

The existing `verify_model()` rendering and its existing message stay byte-for-byte identical; the native branch adds only model/provider, pass/fail, and the sanitized `detail`. It must not print the prompt, process output, event file path, or auth data.

- [ ] **Step 5: Run Task 8's passing gate**

Run:

```bash
uv run pytest tests/integration/test_cli.py -k "models_verify or council" -v
uv run ruff check src/agent_gateway/cli.py tests/integration/test_cli.py
uv run mypy src/agent_gateway/cli.py
```

Expected: verification and pre-existing Council integration tests PASS; Ruff/mypy clean.

- [ ] **Step 6: Review the dirty-file diff carefully**

Run:

```bash
git diff --check -- src/agent_gateway/cli.py tests/integration/test_cli.py
git diff -- src/agent_gateway/cli.py tests/integration/test_cli.py
```

Expected: no whitespace errors. Manually identify the pre-existing Council hunks and confirm the task neither removes nor rewrites them.

---

### Task 9: Launch-Time Eligibility and Definition Injection

**Files:**
- Modify: `src/agent_gateway/external_subagents.py`
- Modify: `src/agent_gateway/harnesses/claude.py`
- Modify: `src/agent_gateway/doctor.py`
- Modify: `tests/unit/test_external_subagents.py`
- Modify: `tests/unit/test_harness_env.py`
- Modify: `tests/integration/test_launcher.py`
- Modify: `tests/unit/test_doctor.py`

**Interfaces:**
- Consumes: current `CompatibilityIdentity/Status`, `read_claude_settings()`, generated definitions, `ProxyState.runtime_fingerprint`, and `merge_agents_args()`.
- Produces:
  - `Eligibility(agent: GeneratedAgent | None, status: CompatibilityStatus)`
  - `resolve_external_agent_eligibility(paths: Paths, registry: ModelRegistry, claude: ClaudeExecutableIdentity, runtime_fingerprint: str, settings: Sequence[tuple[Path, dict[str, object]]]) -> tuple[Eligibility, ...]`
  - `current_generated_agents(paths: Paths, registry: ModelRegistry, claude: ClaudeExecutableIdentity, runtime_fingerprint: str, settings: Sequence[tuple[Path, dict[str, object]]]) -> tuple[GeneratedAgent, ...]` — the same five positional parameters as the resolver, in the same order, returning only the `current` agents
  - `inspect_generated_id_collisions(settings: Sequence[tuple[Path, dict[str, object]]], agents: Sequence[GeneratedAgent]) -> tuple[tuple[Path, str], ...]`
- `ClaudeHarness.launch()` retains `ProxyState`, resolves Claude identity once, computes eligible agents, merges args, then `execve()`s the stable configured executable.

- [ ] **Step 1: Write eligibility matrix tests**

Seed active/inactive entries and Task 2 records. Assert one sorted `Eligibility` per active registry model, with `agent is not None` only for `current`; missing, invalid, failed, stale, and policy-excluded records produce `agent is None` and their exact status. Inactive models produce no eligibility row. Do not inspect provider auth here; proxy readiness remains the global gate.

Assert `inspect_generated_id_collisions()` checks only a top-level `agents` object in each inspectable settings payload, returns every `(path, id)` matching generated reserved IDs, and never mutates settings. Unknown managed definitions remain uninspectable and are handled by the live probe.

- [ ] **Step 2: Run eligibility tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_external_subagents.py -k "eligibility or inspect_generated_id_collisions" -v
```

Expected: FAIL because eligibility functions are absent.

- [ ] **Step 3: Implement eligibility as pure status mapping**

Build a `CompatibilityIdentity` from each active entry plus current Claude/runtime identity and contract version. Evaluate policy first, then stored evidence. Never downgrade `policy_excluded` to another state. Build agents only for `current` statuses and preserve deterministic ordering.

- [ ] **Step 4: Write launcher injection tests with offline seeded evidence**

Extend the fake Claude launcher fixture to seed a current compatibility record matching:

- stable configured fake executable path;
- resolved fake target;
- exact fake executable SHA-256;
- fake version output;
- test `ProxyState.runtime_fingerprint`;
- exact public model/provider;
- contract version 1.

Assert:

- one current model adds one two-token `--agents <json>` definition with exact fields;
- two current models add two deterministic definitions;
- user `--agents` semantics are preserved and merged at original position;
- malformed/colliding user `--agents` fails before `os.execve` and never echoes its JSON;
- missing/stale/policy-excluded evidence injects nothing;
- with zero generated definitions, the complete forwarded sequence stays byte-for-byte unchanged, including an invalid `--agents` after/before `--`;
- effort insertion remains independent: configured `--effort` placement is unchanged and `--agents` processing does not reinterpret prompt content;
- fake Claude's exit code 7 still propagates;
- managed environment still strips Claude API credentials, provider paths, LiteLLM key, cloud routing flags, and `CLAUDE_CODE_SUBAGENT_MODEL`;
- `claude` through the shell wrapper and explicit `agw claude` reach the same `ClaudeHarness.launch()` construction (reuse existing shell tests rather than launching a real shell if possible).

- [ ] **Step 5: Run launcher tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_harness_env.py tests/integration/test_launcher.py -k "agents or eligible or exit_code or effort" -v
```

Expected: FAIL because launch does not retain `ProxyState`, resolve identity, or inject definitions.

- [ ] **Step 6: Integrate after proxy readiness and before environment construction**

Change the launch sequence to:

```python
state = proxy.ensure_running(paths, config, registry)
claude_identity = resolve_claude_identity(config)
settings = read_claude_settings()
generated = current_generated_agents(
    paths, registry, claude_identity, state.runtime_fingerprint, settings
)
launch_args = _launch_args(config, forwarded_args)
launch_args = merge_agents_args(launch_args, generated)
env = self.build_env(os.environ, config, registry, proxy_key, launch_args)
```

The stable configured path remains argv[0]/`execve` target; identity resolution hashes the canonical target. Do not move compatibility calls before `ensure_running()`, because global active-provider authentication must still fail first.

- [ ] **Step 7: Write offline doctor diagnostics tests**

Add one check per active model with names/details:

```text
external subagent gpt-5.6-sol: current
external subagent gpt-5.6-luna: external subagent not verified
external subagent gpt-5.6-terra: external subagent evidence is stale
```

Add inspectable policy/collision details and update conflicting env wording to:

```text
CLAUDE_CODE_SUBAGENT_MODEL overrides definition-level and per-invocation subagent models
```

Seed `paths.generated_litellm_config` in these tests so a runtime fingerprint is computable, and add one test that deletes it and asserts the single warn row `external subagent status unavailable until the proxy has been started once` with no per-model rows.

Monkeypatch executable version/hash inputs and assert `run_doctor(..., online=False)` invokes neither provider HTTP nor Claude/provider model calls. It may run only bounded local `claude --version` when the executable prerequisite check already does so; if doctor currently avoids subprocess execution, inject/read identity through a local helper without starting a model session.

- [ ] **Step 8: Run doctor tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_doctor.py -k "external_subagent or subagent_model or offline" -v
```

Expected: FAIL because compatibility diagnostics do not exist and wording is stale.

- [ ] **Step 9: Add diagnostics without automatic verification**

Doctor computes current identity/status only from local executable metadata/version, inspectable settings, registry, runtime fingerprint material, and stored records. It never calls `verify_native_subagent()`, never registers a route, and never mutates compatibility evidence. Collision reports include only path and generated ID.

Offline doctor cannot assume a runtime fingerprint exists: `proxy.runtime_fingerprint()` needs the rendered LiteLLM config, and `paths.generated_litellm_config` is absent before the first proxy start. When that file is missing or unreadable, emit one `Level.warn` check with detail `external subagent status unavailable until the proxy has been started once` and skip the per-model rows entirely rather than computing a fingerprint from a guessed config, which would report every model as `stale`.

- [ ] **Step 10: Run Task 9's passing gate**

Run:

```bash
uv run pytest tests/unit/test_external_subagents.py tests/unit/test_harness_env.py tests/integration/test_launcher.py tests/unit/test_doctor.py -v
uv run ruff check src/agent_gateway/external_subagents.py src/agent_gateway/harnesses/claude.py src/agent_gateway/doctor.py tests/unit/test_external_subagents.py tests/unit/test_harness_env.py tests/integration/test_launcher.py tests/unit/test_doctor.py
uv run mypy src/agent_gateway/external_subagents.py src/agent_gateway/harnesses/claude.py src/agent_gateway/doctor.py
```

Expected: tests PASS; no real model/network calls; Ruff/mypy clean.

- [ ] **Step 11: Review the narrow diff**

Run:

```bash
git diff --check -- src/agent_gateway/external_subagents.py src/agent_gateway/harnesses/claude.py src/agent_gateway/doctor.py tests/unit/test_external_subagents.py tests/unit/test_harness_env.py tests/integration/test_launcher.py tests/unit/test_doctor.py
```

Expected: no output. Confirm ordinary launch does not create a probe event file or call the router control endpoint.

---

### Task 10: Cleanup, Documentation, and Live-Only Acceptance Gate

**Files:**
- Modify: `src/agent_gateway/uninstall.py`
- Modify: `tests/unit/test_doctor.py`
- Modify: `README.md`
- Modify: `docs/model-selection.md`
- Modify: `docs/compatibility-report.md`
- Modify: `docs/security.md`
- Modify: `docs/architecture.md`
- Modify: `docs/operations.md`
- Modify: `skills/model-routing/SKILL.md`
- Modify: `tests/manual/README.md`
- Modify: `tests/manual/live_smoke.py`

**Interfaces:**
- Consumes: all final user-facing commands/statuses and existing uninstall ownership behavior.
- Produces: uninstall cleanup for `Paths.external_subagents_dir`; live-marked tests that are collected but never run by the default `-m "not live"` suite.

- [ ] **Step 1: Write uninstall cleanup tests in the existing combined doctor/uninstall test file**

Create compatibility/probe files under `paths.external_subagents_dir`, run normal uninstall, and assert the whole generated directory is removed while `paths.credentials_dir` remains. Preserve existing explicit credential-acknowledgment tests.

- [ ] **Step 2: Run uninstall test and verify RED**

Run:

```bash
uv run pytest tests/unit/test_doctor.py -k "uninstall and external_subagents" -v
```

Expected: FAIL because uninstall leaves the new generated state.

- [ ] **Step 3: Add one generated-state removal**

Add `_remove(paths.external_subagents_dir, removed)` alongside other generated state. Do not add it to credential deletion or touch provider credential directories.

- [ ] **Step 4: Replace every stale external-Agent claim**

Make these exact documentation contracts consistent:

- top-level external selection remains `/model NAME` or `claude --model NAME`;
- Agent's explicit `model` field accepts only `sonnet|opus|haiku|fable` and must not receive an external name;
- verified external models appear as generated `agw-...` `subagent_type` choices after managed `claude`/`agw claude` launch;
- Claude may autonomously run multiple generated types concurrently and synthesize results;
- generated workers are analysis-only with `Read`, `Grep`, `Glob`, and may send repository content to the named external provider;
- missing generated types mean inactive/policy-excluded/unverified/failed/stale/collision status, never fallback;
- run `agw models verify NAME --claude-subagent` explicitly to make bounded Claude/provider calls;
- ordinary sessions have no per-spawn route recording or fail-closed pinning claim;
- `command claude` bypasses AGW;
- Council and agent-team external routing claims are deferred.

Specifically remove or rewrite:

```text
per-agent model assignment
pass <name> unchanged as that subagent's model
assign <name> per teammate
```

Update compatibility target references from Claude Code 2.1.218 to the tested verifier target `2.1.232` where the claim concerns external subagents; retain historical picker findings only if clearly labeled historical.

- [ ] **Step 5: Update architecture/security/operations details exactly**

Document:

- compatibility layout and one-record status classes;
- control endpoint, loopback/key auth, 60-second one-shot in-memory registry, and synchronous `SubagentStart` ordering;
- `0700`/`0600`, `O_NOFOLLOW|O_APPEND`, containment/owner/mode/type/device/inode/`PIPE_BUF` checks;
- lifecycle-header stripping before external forwarding;
- exact evidence allowlist and forbidden content;
- process-group timeout/cancellation cleanup;
- ABI invalidation/restart requirement;
- no automatic live calls from setup, doctor, or launch;
- normal provider-auth fail-fast behavior.

- [ ] **Step 6: Add live-marked smoke functions without running them**

Add `pytest.mark.live` coverage/helpers for this explicit sequence:

1. Verify at least two active external models with `--claude-subagent`; assert current compatibility records bind exact configured/canonical executable identities, version, runtime fingerprint, public model/provider, and contract.
2. Start managed parent Claude with two generated types; request parallel independent analyses; capture only sanitized type/agent/model/provider/timestamps and assert overlap demonstrates concurrency.
3. Assert parent stream contains both child completions and one synthesis result without persisting their content.
4. Force one cancellation and one external-provider failure; assert bounded return/control and no model substitution.
5. Use canary strings in delegated prompt/tool result and assert they are absent from compatibility JSON, route NDJSON after cleanup, `proxy.log`, and CLI diagnostics.
6. Enable a restrictive managed policy and assert native inheritance cannot produce a passing verification record.

Tests must require explicit local prerequisites and skip with instructions rather than authenticate, mutate settings, or choose a fallback. Never run these functions in this implementation task.

- [ ] **Step 7: Run documentation/static collection checks only**

Run:

```bash
uv run pytest tests/unit/test_doctor.py -k "uninstall" -v
uv run pytest tests/manual --collect-only -m live -q
rg -n "per-agent model assignment|pass .* unchanged as .*subagent.*model|assign .* per teammate" README.md docs skills tests/manual
```

Expected: uninstall tests PASS; live tests collect without executing; `rg` returns no stale claims (exit 1 is expected for no matches).

- [ ] **Step 8: Run the complete offline verification gate**

Run:

```bash
uv run pytest -m "not live"
uv run ruff check .
uv run mypy src
python3 -m compileall -q src tests
uv run python - <<'PY'
from agent_gateway.external_subagents import generated_agent_type
from agent_gateway.model_registry import ModelEntry, ModelMode
from agent_gateway.providers import Provider

entry = ModelEntry(
    name="gpt-5.6-sol",
    provider=Provider.chatgpt,
    upstream_model="chatgpt/gpt-5.6-sol",
    mode=ModelMode.responses,
    enabled=True,
)
assert generated_agent_type(entry) == "agw-gpt-5-6-sol-66127d2195a1"
print("external-subagent contract: ok")
PY
git diff --check
```

Expected: every non-live test passes; Ruff/mypy/compileall clean; contract prints `external-subagent contract: ok`; `git diff --check` emits no output. These commands must make no Claude or provider model call.

- [ ] **Step 9: Review all feature changes against the approved specification**

Inspect:

```bash
git status --short
git diff --stat
git diff -- src/agent_gateway tests README.md docs skills/model-routing/SKILL.md
```

Confirm each AC-1 through AC-10 has an implementation/test or is explicitly reserved for the live gate. Confirm the original Council changes are still present. Confirm no credential file, generated state, compatibility record, or probe event was added to the repository.

- [ ] **Step 10: Stop at the explicit, unexecuted live-verification gate**

Do **not** run these during ordinary implementation or CI. Present them for the user to authorize after reviewing the offline diff:

```bash
uv run agw proxy restart
uv run agw models verify gpt-5.6-luna --claude-subagent
uv run agw models verify gpt-5.6-sol --claude-subagent
uv run agw models verify gpt-5.6-terra --claude-subagent
uv run pytest tests/manual -m live -v
```

Expected only when explicitly authorized with real subscriptions:

- each selected model stores `current` evidence for exact model/provider and Claude 2.1.232 identity;
- managed Claude advertises those generated types;
- at least two run concurrently and return normal results to the parent synthesis;
- cancellation/provider failure returns control without fallback;
- no canary appears in persisted evidence/logs.

Any missing exact route, policy substitution, warning, registration failure, timeout, or cleanup failure is a failed gate. Do not advertise that model and do not reinterpret a native answer as external success.

---

## Execution Checkpoints

Execute inline in dependency order:

1. Tasks 1-3 establish pure definitions, current-evidence storage, and safe event I/O.
2. Tasks 4-5 add controlled router correlation and invalidate the daemon ABI.
3. Tasks 6-7 close the `SubagentStart` race and implement bounded native verification.
4. Task 8 exposes verification only through explicit CLI consent.
5. Task 9 enables current-evidence launch injection and offline diagnostics.
6. Task 10 completes cleanup/docs/offline verification, then stops before live calls.

At every checkpoint, review the narrow diff and preserve the dirty Council baseline. If an unexpected file changes, stop immediately and ask the user how to proceed.
