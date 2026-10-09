# OpenCollab format

[OpenCollab](https://github.com/RISE-X-Lab/OpenCollab) is a multi-agent coding
framework. Each agent persists one resumable session snapshot per run as a
JSON document under the run directory (`agent_<aid>_<role>.json` for teams,
`<seq>_<role-slug>.json` for workflows). The native reader,
`SessionStore.load_snapshot`, accepts the versioned envelope or a bare message
list, and `opencollab --session <file>` restores a snapshot by explicit path,
which makes the format a practical migration target without touching
OpenCollab itself.

session-migrate supports OpenCollab as a **write-only target** pinned to
version 0.9.3.

## Native shape

```json
{
  "snapshot_version": 1,
  "model": "gpt-5",
  "messages": [
    {"role": "user", "content": "Fix the flaky test"},
    {
      "role": "assistant",
      "content": null,
      "tool_calls": [
        {
          "id": "call_1",
          "type": "function",
          "function": {"name": "shell", "arguments": "{\"command\":[\"pytest\"]}"}
        }
      ]
    },
    {"role": "tool", "tool_call_id": "call_1", "content": "1 passed"}
  ]
}
```

Message rules enforced by the native `SessionStore` validation:

- roles are limited to `system`, `user`, `assistant`, and `tool`;
- `assistant` turns need `content` or `tool_calls`, and every entry in
  `tool_calls` must be typed `function` with a string `arguments`;
- `tool` turns need a non-empty `tool_call_id`;
- `content` is text or a content-part list of `text` / `input_text` /
  `output_text` / `image_url` parts.

`Session.restore()` rebuilds clean runtime counters for message-only
snapshots and auto-appends synthetic tool results for assistant turns whose
`tool_calls` were left open, so a truncated import still resumes.

## Conversion mapping

| Portable event | OpenCollab snapshot | Notes |
| --- | --- | --- |
| user/assistant message | `{"role": ..., "content": text}` | Verbatim text. |
| system/compaction message | `{"role": ..., "content": text}` | Compaction summaries become `system` turns prefixed with `[CONTEXT SUMMARY]:`. |
| tool call | assistant turn with `tool_calls` | Object inputs are JSON-encoded; string inputs (custom/freeform tools) are kept verbatim. |
| tool result | `{"role": "tool", ...}` | Single text block collapses to a string; otherwise text/image parts. |
| user image context | `image_url` content part | Data URLs only, validated before writing. |
| thinking | dropped | Counted as `thinking:private` (plus `thinking:provider_payload` when signed/encrypted). |
| orphan/duplicate tool results | dropped | Counted as `tool_result:orphan_id` / `tool_result:duplicate_id`. |

## Resume

```bash
smigrate transfer SESSION_UUID --from codex --to opencollab
opencollab --session ~/.opencollab/sessions/YYYY/MM/DD/<new-uuid>.json
```

The snapshot is staged under `~/.opencollab` (override with
`OPENCOLLAB_HOME`). OpenCollab has no global session registry: imported
snapshots are resumed by explicit path, and `used_tokens` / `step_count`
restart from zero, which matches a continued conversation.
