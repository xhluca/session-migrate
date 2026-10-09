# OpenCode 2.0 public session transfers

OpenCode 1.17.20 uses `import/export --pure` and nested message/part bundles.
OpenCode 2.0 uses `session import/export --standalone` and flat typed messages.
Native import detects the installed client and selects its transfer schema.
Convert-only output remains legacy unless `--target-cli-version 2.0.22` or another accepted 2.0.x release is set.
Unsupported schema series fail closed. The `opencode v` version prefix and
custom suffixes are accepted; unvalidated exact releases receive schema-aware
warnings. Local native gates cover stock 2.0.22; the CI variant pins stock
2.0.23. Accepting the 2.0 schema family does not establish native compatibility
with every future patch or custom build. Unknown series such as 2.1 and 3.x
fail closed during native import/export.

The v2 adapter preserves ordered user/assistant text, linked tool inputs and
results, inline user/tool images, and readable completed compaction summaries.
User text blocks in one native message become their exact newline join.
Private-only or whitespace-only checkpoints do not retire readable history.
Encrypted provider checkpoints, private traces, source reasoning variants and
unsupported control/metadata fields are explicitly counted as omissions.

A streamed tool input can be an incomplete JSON string. The source projection
keeps it unchanged inside `{ "input": ORIGINAL_STRING }`; it does not replace
it with `{}` or attempt to parse/complete it. On conversion, an unfinished call
is archived as an `ImportedIncompleteTool` error with the same ID and input,
because native transfer discards unsettled assistant records. This is historical
input preservation, not an executable resumed partial tool invocation. The
source→portable→target regression includes an unterminated string input.

OpenCode owns its database import. No private SQLite writes, credentials or
machine-specific launchers are part of the adapter. Dry run performs public
native preflight without importing or writing a migration manifest; native
preflight may initialize the client's ordinary store. Imports enforce global
identity collisions and explicit success confirmation, then verify the imported
ID using public export. A conflict can have native exit status zero and still
fails import confirmation.

## Reproducible native and route checks

```sh
./scripts/install-native-test-clis.sh /tmp/session-migrate-native opencode-v2
./scripts/run-native-test-client.sh \
  opencode-v2 /tmp/session-migrate-native/session-migrate-native.env
PYTHONPATH=src python3 -m pytest -q tests/test_opencode_v2.py tests/test_route_matrix.py
```

The `opencode-v2` CI matrix entry installs **@opencode/cli@2.0.23** (the legacy
package is opencode-ai), supplies `SESSION_MIGRATE_TEST_OPENCODE_V2`, and rejects
skipped assigned tests. Native tests use synthetic transcripts, isolated HOME
and XDG directories, an explicit credential-free config, and a deterministic
localhost provider. They verify import/export, dry run, collisions, cold process
reopening, compaction/tool replay and a new persisted reply under the same ID.
They do not use real accounts or supplier inference credits. The default suite
can skip native tests when neither tested executable is supplied; the selected
CI job cannot silently pass those skips.

Route tests cover legacy and v2 OpenCode source and target variants against the
other supported formats. The 19 variants remain eighteen harnesses. Existing
legacy wire behavior and native tests remain separate.

The metadata catalog reads `session_v2` and legacy `session` within one
read-only transaction. A duplicate ID uses the v2 metadata; legacy-only rows
remain visible. A missing v2 title is represented as an untitled session.
Message bodies are never queried or indexed. Kilo 7.5.0 continues to emit and
accept only the legacy nested schema, even with a metadata version override.

For a preinstalled stock 2.0.22 binary, no dependency installation is needed:

```sh
SESSION_MIGRATE_TEST_OPENCODE_V2=/absolute/path/to/opencode \
  PYTHONPATH=src python3 -m pytest -q \
  tests/test_opencode_v2.py tests/test_opencode_v2_native.py
```

Native Windows acceptance and all historical client versions are not
established by these Linux gates.
