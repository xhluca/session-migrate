# Grok, Kilo Code, and OpenHands formats

This note records the exact native contracts used by the three adapters added
in `session-migrate` 0.9.0. These are versioned implementation observations,
not promises that future releases will keep the same storage layout.

## Pinned releases

| Harness | Validated release | Exact Linux x64 artifact |
| --- | --- | --- |
| Grok | `xai-org/grok-build 1.0.5` (`5115b46bc9`) | 166,854,368 bytes; SHA-256 `9ba87444e1819e8f6104adbbf4676a870c204380aa5c3e1c38a926c4ea677238` |
| Kilo Code | `Kilo-Org/kilocode 7.5.0` | 145,118,408 bytes; SHA-256 `ede061eb9178d0158ac66baa81619e2bf66859041d20d0a014798d38ddc7c1ce` |
| OpenHands | `OpenHands-CLI 1.16.0`, SDK `1.21.0` | 88,139,576 bytes; SHA-256 `cb04ee2da91c698733d5201c55cbc08d81dccc9d64b666275abf68a4e0c590e3` |

The checked-in default tests use sanitized fixtures and a local HTTP fixture;
they need no provider account or API key. The opt-in native tests verify these
exact artifacts before execution and use a loopback OpenAI-compatible server.

## Grok

Grok stores a session below a percent-encoded working-directory bucket:

```text
$GROK_HOME/sessions/<encoded-cwd>/<uuid>/
├── summary.json
└── updates.jsonl
```

`GROK_HOME` defaults to `~/.grok`. The update log is the authoritative ACP
timeline. The reader accepts both the public `session/update` method and the
`_x.ai/session/update` method emitted by Grok 1.0.5. It validates one UUID,
integer timestamps, known message/image/tool shapes, bounded record counts, and
stable paired-file identity.

User/assistant text, user images, linked tool calls/results, and portable
summaries are mapped. A summary is flattened into an explicitly marked native
user chunk because this Grok build has no equivalent portable compaction item.
Private thought chunks, provider payloads, namespaces, unsupported result
blocks, lifecycle updates, and runtime-only state are counted in the manifest.

The native gate installed a generated session, resumed it by UUID with the
exact `grok` binary, sent the imported prefix plus a follow-up to the local
fixture model, received a native assistant reply, and proved that Grok appended
to the original `updates.jsonl` prefix.

## Kilo Code

Kilo 7.5.0 shares OpenCode's official import/export bundle shape and stores its
runtime state in its own SQLite database under the normal XDG data root. The
migrator never writes that database. It uses only:

```text
kilo import <bundle.json> --pure
kilo export <session-id> --pure
kilo run ... --session <session-id> --pure
```

The bundle reader/writer validates the Kilo schema, IDs, timestamp order,
message/part ownership, tool linkage, supported images, compaction parts, and
bounded JSON depth/counts. Kilo-only runtime, patch, snapshot, permissions,
reasoning signatures, and unknown parts remain reason-specific losses.

Kilo and OpenCode intentionally share the same import/export schema, and a
cross-imported bundle retains its stored metadata. A standalone bundle therefore
has no reliable producer marker: file-based inspection fails closed and asks for
`--format kilo` or `--format opencode`. Native-ID transfer remains unambiguous
because `--from` selects the official exporter.

Two native behaviors matter:

- the importer replaces the bundle CWD with its process CWD, so the migrator
  invokes it from the requested target workspace and verifies the exported CWD;
- `kilo session list --all --format json` crashes in 7.5.0 for a valid imported
  session when `time.updated` is absent. Collision checks therefore use the
  content-free official `kilo export <id>` probe, with exported bodies discarded.

The exact native gate imports the generated bundle, continues it through a
loopback model, verifies the imported messages/tool result reached that model,
and reparses the official export after the appended reply.

## OpenHands

OpenHands keeps one conversation directory per UUID:

```text
$OPENHANDS_CONVERSATIONS_DIR/<uuid>/
├── base_state.json              # optional complete SDK runtime snapshot
└── events/
    └── event-<ordinal>-<uuid>.json
```

The default is `~/.openhands/conversations`. Each generated event is a separate
JSON document using the SDK's event union. The reader validates the filename
ordinal/UUID, event ID/session ID, timestamps, known content blocks, tool IDs,
and unique event/action linkage. A bounded `base_state.json`, when present,
contributes model and workspace metadata and participates in coherent snapshot
checks; the event stream remains authoritative.

User/assistant text, user images, linked actions/observations, result images,
and condensation summaries are portable. Generated `ActionEvent` and
`Condensation` records include the required `llm_response_id`; omitting it
produces a file that looks plausible but the pinned SDK refuses to load. Agent
state, metrics, delegates, private reasoning, MCP/runtime configuration, and
unsupported event variants are counted rather than replayed.

The generated bundle retains requested CWD, model, title, and CLI version as
validated migration metadata, but installation intentionally writes only event
documents. A partial base state makes SDK 1.21.0 enter a strict restore path and
is not safe to fabricate. On first resume, the SDK rebuilds the complete base
state from the launch workspace and active model configuration; the native gate
checks that result. The native picker title is derived from the first user text,
matching this pinned release.

The native gate loads the generated event directory with OpenHands 1.16.0,
sends the imported messages and tool result to the local fixture model, appends
a user and assistant turn, and proves every imported event file stayed
byte-for-byte unchanged.

## CLI examples

```bash
smigrate catalog refresh
smigrate catalog search "parser timeout" --format grok

smigrate transfer --title "parser timeout" --from grok --to openhands --dry-run
smigrate transfer --title "parser timeout" --from grok --to openhands

smigrate transfer SESSION_ID --from openhands --to kilo \
  --target-cli /path/to/kilo-7.5.0
```

Kilo is a virtual source/target: native lookup and installation require the
pinned official binary and do not accept `--home`. Grok and OpenHands accept
`--source-home`/`--home`; their defaults and environment variables are listed
in the [CLI reference](cli-reference.md).

## Test contract

The default suite covers sanitized fixture parsing, malformed-input rejection,
serialization/reparse equivalence, all 225 ordered routes, installation
collision handling, catalog indexing/search, inspect output, and exact manifest
loss counters.

The opt-in exact-binary gate is:

```bash
SESSION_MIGRATE_GROK_BIN=/path/to/grok-1.0.5 \
SESSION_MIGRATE_KILO_BIN=/path/to/kilo-7.5.0 \
SESSION_MIGRATE_OPENHANDS_BIN=/path/to/openhands-1.16.0 \
  uv run pytest -q tests/test_grok_kilo_openhands_native.py
```

It is credential-free and binds its fixture server only to loopback. It does
not read or copy any native authentication store. After each model-visible
continuation, it launches the actual interactive terminal surface in a bounded
PTY: Grok's fullscreen TUI and Kilo's mini TUI must render imported history;
OpenHands' TUI must open the imported conversation and its native `view`
command must render the complete imported/continued trajectory.
