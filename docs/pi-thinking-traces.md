# Pi thinking-trace handling

This note explains how the pinned Pi coding agent persists, displays, restores,
and replays reasoning state. It is structural research: no real reasoning text,
signature, token, prompt, response, path, model identifier, or session ID is
included.

## Result

Pi 0.80.6 handles thinking through two deliberately separate mechanisms:

1. `thinking_level_change` is session configuration for future turns. It stores
   values such as `low` or `high`; it is not a reasoning trace.
2. An assistant message may contain a `thinking` content block. The block has a
   displayable `thinking` string and may have an opaque `thinkingSignature`.
   The signature is the provider-specific replay state that lets the same
   provider/API/model continue a reasoning trajectory.

The second mechanism is why Pi can preserve a trace even when it has no visible
thinking text. Pi stores the complete assistant message in its v3 JSONL, keeps
the opaque signature on same-model resume, and reconstructs the corresponding
provider-native reasoning item before the next request.

## Inspected implementation

The installed package was `@earendil-works/pi-coding-agent` 0.80.6 with matching
`@earendil-works/pi-ai` and `@earendil-works/pi-agent-core` 0.80.6 packages. Its
package metadata points to `earendil-works/pi`. Selected installed-file SHA-256
values pin the code inspected on 2026-08-19:

| Package-relative file | SHA-256 |
| --- | --- |
| `package.json` | `5f805b25823010b36e801735f07e57d831f0a474f95dca24ab90bca6e27e0aa0` |
| `dist/core/session-manager.js` | `879e80cc6e2371e4b06887e6fb041c323ba4e86f7687bfdac6474c9f61486112` |
| `dist/modes/interactive/components/assistant-message.js` | `ba48f75948a64a1acd8c03900bcd7655e3f08d462d1425ec6526efff88abc9ff` |
| `node_modules/@earendil-works/pi-ai/dist/api/transform-messages.js` | `e51975857b2fefa7e9cc108850ddab5a2fd1753a399f3cde00d76cd700ce6d10` |
| `node_modules/@earendil-works/pi-ai/dist/api/openai-responses-shared.js` | `d71dea744d31deed3f1abc3171b0cef13b072d4c5cc4c5f04a85930d8ecf7f97` |
| `node_modules/@earendil-works/pi-ai/dist/api/anthropic-messages.js` | `60dde4beb52b73d88fb494f5c182a506975cc2f5da4384793cf27c8d1dc667cb` |
| `node_modules/@earendil-works/pi-ai/dist/api/google-shared.js` | `ac3b7e6d3f041b5bc73401ea6a228fe74f101889b23fdf011aa86ec3606ed621` |

The relevant public in-memory type is effectively:

```typescript
interface ThinkingContent {
  type: "thinking";
  thinking: string;
  thinkingSignature?: string;
  redacted?: boolean;
}
```

The signature is intentionally a string rather than one cross-provider schema.
Different adapters put different opaque payloads in it.

## Persistence and context reconstruction

Pi's `SessionManager.appendMessage()` writes the whole assistant message into a
normal `message` entry. Thinking blocks therefore remain ordered alongside text
and tool-call blocks in the same append-only `id`/`parentId` tree.

On resume, `buildSessionContext()`:

- selects the active leaf ancestry;
- applies the latest compaction boundary;
- returns stored message objects without deleting their thinking blocks; and
- separately restores the most recent model and thinking-level settings.

The separation matters. Changing the thinking level affects the next request;
it neither recreates nor replaces an earlier assistant reasoning block.

## Provider-specific capture and replay

| Provider family | Capture | Same-model replay |
| --- | --- | --- |
| OpenAI Responses/Codex Responses | Pi stores the visible reasoning summary/content in `thinking` and serializes the complete returned `reasoning` output item into `thinkingSignature`. | Pi parses the signature JSON and inserts the original `reasoning` item before the assistant message in the next Responses input. Encrypted reasoning can therefore survive with an empty visible string. |
| Anthropic Messages | Pi streams `thinking_delta` text and concatenates `signature_delta` into `thinkingSignature`. A `redacted_thinking` block stores its encrypted payload as the signature and marks `redacted: true`. | The same model receives a native `thinking` block with its signature, or a `redacted_thinking` block containing only the opaque payload. |
| Google Generative AI/Vertex | Pi recognizes thought parts and retains `thoughtSignature`. | For the same provider and model it emits a thought part with the retained signature. |

Before any provider request, `transformMessages()` compares the historical
assistant message's provider, API, and model with the selected target model:

- same provider/API/model: preserve thinking and its signature;
- different model: remove the signature and convert nonempty non-redacted
  thinking to an ordinary text block;
- different model plus redacted thinking: drop the block; and
- empty thinking without a usable same-model signature: drop the block.

That behavior is compatibility logic, not a guarantee that reasoning state is
portable between models.

## TUI and compaction behavior

The TUI renders visible thinking as italic, separately colored Markdown. The
user can collapse it, in which case the component shows a static thinking
label. Collapsing changes display only; it does not remove the persisted block.

Pi's token estimator counts visible thinking text. Its default compaction
serializer also labels visible trace text as assistant thinking in the
summarization input. It does not decode an opaque signature. Consequently,
importing visible reasoning into Pi could expose it to a future compaction
request even if the TUI normally keeps it collapsed.

## Controlled probes

A synthetic adapter-level probe produced these content-free results:

- same-model content types remained `thinking, text` and retained the signature;
- after a model change they became `text, text` with no signature;
- same-model redacted thinking remained present, while cross-model redacted
  thinking was removed; and
- OpenAI Responses reconstruction emitted `developer message, reasoning,
  message` in order and retained the synthetic reasoning item ID.

A bounded aggregate scan of the four accessible Pi v3 files found 27 records,
seven assistant messages, four thinking-level changes, and two thinking blocks.
Both blocks had JSON signatures whose payload type was `reasoning`; one had
visible summary text and one had an empty visible string. No content or
identifier was reported.

Private copies with only their stale CWD metadata relocated into an isolated
workspace then passed Pi's actual offline RPC loader 4/4. RPC returned both
thinking blocks and both signatures, including the empty-visible-text case,
and all four normalized input prefixes remained intact. One unmodified copy had
initially failed for the unrelated and expected reason that its historical CWD
no longer existed; the source files were never changed.

## Bridge policy

`session-bridge` recognizes Pi thinking blocks and counts them as `thinking`,
but no target writer copies the text or signature. Claude and Codex reasoning
readers likewise create content-free thinking markers rather than retaining
their private payloads. This remains intentional because:

- visible reasoning is private model work rather than portable user dialogue;
- signatures are opaque and valid only for a specific provider/API/model;
- Pi itself turns nonempty thinking into ordinary model-visible text on a
  model switch; and
- visible traces may enter Pi's compaction summarization request.

A future opt-in same-provider continuation mode would need a much narrower
contract: preserve only the opaque provider item, require exact
provider/API/model identity, validate and size-bound its shape, prohibit
cross-model fallback, keep it out of logs/manifests, and prove replay against
the exact native CLI. It must not be presented as general Claude/Codex/Pi
reasoning conversion.
