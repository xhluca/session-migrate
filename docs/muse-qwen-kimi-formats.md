# Muse, Qwen Code, and Kimi Code formats

This note records the exact native contracts implemented for three additional
coding-agent harnesses. The adapters are version-pinned: their on-disk formats
are implementation details, not cross-version vendor APIs.

## Pinned releases

| Harness | Validated release | Source/runtime evidence |
| --- | --- | --- |
| Muse Code | `0.2.1 (0.2.1-R1215.1)` | installed binary build SHA `b3170a534f`; native event stream and actual CLI resume |
| Qwen Code | `0.22.1` | official `@qwen-code/qwen-code` release at commit `2755dbe1399f94e53e24377d2e21fa86ce923529` |
| Kimi Code | `0.38.0` | official `@moonshot-ai/kimi-code` release at commit `0999454` |
| Muse OpenRouter adapter | `muse-code-openrouter 0.3.2` | loopback-only adapter used by the opt-in live Muse oracle |

The migrator never reads or copies any harness credential store. Real-provider
validation uses one explicitly supplied, mode-`0600` key file inside disposable
homes. Normal tests are credential-free and do not make network requests.

## Native stores

### Muse Code

Muse keeps one durable JSONL event stream per session:

```text
$XDG_DATA_HOME/muse/sessions/YYYY/MM/DD/<uuid>/session.jsonl
```

The default XDG data home is `~/.local/share`. A generated history begins with
`runtime.session.metadata` and `session.opened.observed`. Each portable user
turn is written as the native lifecycle Muse uses to rebuild model context:

1. `runtime.user_intent.accepted`, with both `model_messages` and nonempty
   `refill_blocks`;
2. a `runtime.session` run whose event is `started`;
3. `runtime.user_intent.materialized`, linked to both earlier records;
4. committed assistant/tool/result run events; and
5. a completed terminal run event.

The reader also accepts Muse's durable retained marker for an omitted ephemeral
status record. It becomes one reason-specific opaque event, so the next target
cannot silently claim a lossless migration.

Text messages and linked tool calls/results are portable. Private reasoning,
images, compaction/runtime state, task streams, reminders, policies, and other
Muse-only events are omitted with exact counters. Tool-result text is retained;
non-text blocks are counted.

### Qwen Code

Qwen stores an append-only UUID/parent graph below a project bucket:

```text
$QWEN_HOME/projects/<encoded-cwd>/chats/<uuid>.jsonl
```

`QWEN_HOME` defaults to `~/.qwen`. The reader validates one session identity,
unique record IDs, parent linkage, timestamps, and the active leaf. Inactive
branches become counted opaque events. The writer emits a fresh linear graph
with native user/model parts, function calls/responses, inline user images, and
a native custom-title record.

Tool-result text and supported image blocks are preserved in the native
response plus a namespaced portable sidecar field. Qwen ignores that sidecar;
it lets a later migration reconstruct the exact supported block list without
changing runtime-visible output. Private thought parts are parsed as thinking
and deliberately not replayed into another target.

### Kimi Code

Kimi sessions are multi-file directories:

```text
$KIMI_CODE_HOME/sessions/wd_<slug>_<hash>/session_<uuid>/
├── state.json
└── agents/main/wire.jsonl
```

`KIMI_CODE_HOME` defaults to `~/.kimi-code`. The migration artifact is a
validated transport bundle; installation publishes a private directory with
the native state document and protocol-`1.5` main-agent journal. Parsing takes
a stable snapshot of both files and rejects identity, protocol, or mutation
drift.

Portable text, linked function tools/results, supported images, and compaction
summaries map to native context records. Message timestamps are made
monotonic—without changing semantic order—because Kimi's native journal is an
ordered append stream. Private reasoning, loop/runtime/provider state, and
unsupported nested blocks remain reason-specific losses.

## CLI examples

```bash
# Find native titles/IDs under all configured roots.
smigrate catalog refresh
smigrate catalog search "timeline merge" --format qwen

# Preview and apply a native migration.
smigrate transfer --title "timeline merge" --from qwen --to kimi --dry-run
smigrate transfer --title "timeline merge" --from qwen --to kimi

# Direct UUID lookup can be scoped to one workspace where relevant.
smigrate transfer UUID --from kimi --source-cwd "$PWD" --to muse \
  --model-provider meta --model meta/muse-glimmer-30b
```

Target homes can be overridden with `--home`. Source lookup uses `--source-home`.
Qwen and Kimi additionally accept `--source-cwd`; Muse's date-partitioned UUID
lookup does not need a CWD.

## Test contract

The checked-in default suite covers strict malformed-input rejection,
serialization/reparse equivalence, title discovery and search, collisions,
loss counters, and every ordered source/target pair. It does not need a model
key.

The live release oracle is opt-in:

```bash
SESSION_MIGRATE_OPENROUTER_KEY_FILE=/private/openrouter.key \
SESSION_MIGRATE_QWEN_BIN=/path/to/qwen-0.22.1 \
SESSION_MIGRATE_KIMI_BIN=/path/to/kimi-0.38.0 \
SESSION_MIGRATE_MUSE_BIN=/path/to/muse-0.2.1 \
SESSION_MIGRATE_MUSE_OPENROUTER_BIN=/path/to/muse-openrouter-0.3.2 \
  uv run pytest -q tests/test_muse_qwen_kimi_native.py
```

The key file must be a regular mode-`0600` file. Each test imports the same
sanitized Claude fixture into a new private target home, invokes the exact
native harness through OpenRouter, and requires all of the following:

- the native command succeeds;
- the target file grows while retaining the imported byte prefix;
- the target adapter reparses the result; and
- the model identifies `README.md`, which appears only in the imported tool
  history.

The validated models were `qwen/qwen3-coder-next`,
`moonshotai/kimi-k2.7-code`, and `meta/muse-glimmer-30b`. These model choices
belong to the test trajectory, not to the migration format. Credentials,
provider selection, and model configuration never migrate with a session.
