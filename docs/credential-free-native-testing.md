# Credential-free native client testing

The native CI runs the real, pinned public clients without copying a developer's
login or calling a paid model. Each job installs one exact client, gives it an
empty home directory, and connects it to a deterministic server on `127.0.0.1`.

## Why a protocol stub, not vLLM

These tests validate session transport rather than model quality. Running a
language model would add downloads, GPUs, nondeterminism, and token cost without
improving the compatibility assertion. `tests/offline_provider.py` implements
only the protocol surfaces the clients need:

- OpenAI Chat Completions;
- OpenAI Responses;
- Anthropic Messages; and
- Gemini `generateContent` and `streamGenerateContent`.

The server records the complete JSON request and returns a fixed, unique reply.
The test then asserts that source-history markers reached the provider and that
the reply was appended to the same native session. This is stronger than simply
echoing the last prompt: it verifies messages, tools, summaries, and ordering
without treating a client's large system prompt as assistant output.

## Isolation contract

Every native gate:

1. verifies the exact package version, source revision, or binary digest;
2. creates fresh `HOME`, config, cache, data, runtime, temporary, and workspace
   directories;
3. removes inherited provider keys and login state;
4. supplies only a visibly fake key when the client requires a non-empty value;
5. points the supported provider URL at localhost;
6. captures and inspects the model request;
7. reparses the target-native store after continuation; and
8. fails if pytest skips any assigned test.

No production credential is read, translated, or written by this workflow.

## Exact-client coverage

| Client | Credential-free native evidence |
| --- | --- |
| Antigravity 1.1.16 | Gemini override; imported history reaches the server and the reply persists |
| Claude Code 2.1.209 | Anthropic override; cold reload and fresh-writer continuation persist |
| Codex 0.144.4 | Responses override; cold reload and fresh-writer continuation persist |
| Copilot 1.0.70 | OpenAI-compatible override; native multimodal/tool history replays and persists |
| Cursor `2026.03.20-44cb435` | Shipped backend loads blobs and the real TUI renders imported text; no offline model append |
| Devin 3000.6.7 | Native list/resume selection and ACP `session/load`; stops at vendor authentication |
| Grok 1.0.5 | OpenAI-compatible override; native creation, reload, TUI replay, and append |
| Hermes 0.20.6 | OpenAI-compatible override; replay, append, and compaction |
| Kilo 7.5.0 | Official import/export plus OpenAI-compatible replay and append |
| Kimi 0.38.0 | OpenAI-compatible override; imported history replays and the reply persists |
| MastraCode 0.37.1 | OpenAI-compatible override; replay and append |
| Muse 0.2.1 | Exact Muse with the pinned OpenRouter adapter pointed locally; replay and append |
| OMP 18.0.5 | Local provider; replay, append, and rename |
| OpenCode 1.17.20 | Official import/export plus local provider replay and append |
| OpenHands 1.16.0 | OpenAI-compatible override; native creation, reload, TUI replay, and append |
| Pi 0.80.6 | Offline RPC with local provider; tools, images, compaction, replay, and append |
| Qwen 0.22.1 | OpenAI-compatible override; imported history replays and the reply persists |
| Vibe 2.24.3 | OpenAI-compatible override; native TUI creation, reload, replay, and append |

Cursor and Devin remain honest partial gates. The test suite does not patch
their binaries or forge vendor authentication to turn those boundaries into a
false continuation result.

## Run it

Install and test one client in a disposable directory:

```bash
./scripts/install-native-test-clis.sh /tmp/session-migrate-native qwen
./scripts/run-native-test-client.sh \
  qwen /tmp/session-migrate-native/session-migrate-native.env
```

Omit the client name from the installer to fetch every pinned client. GitHub
Actions uses one client per matrix job so a failure identifies the exact
harness and installations do not compete for disk space.

The normal Python job separately runs the deterministic 18 by 18 route oracle.
The native matrix is the independent acceptance layer: generated files must be
accepted by code owned and shipped by each harness.
