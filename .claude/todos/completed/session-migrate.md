# Session migration implementation

- [x] Inventory the candidate Docker image source.
- [x] Locate current official Codex resume/import documentation.
- [x] Record exact Claude and Codex versions in the image.
- [x] Document both persisted session schemas with sanitized examples.
- [x] Specify the neutral event model and loss policy.
- [x] Implement Claude reader/writer and discovery.
- [x] Implement Codex reader/writer and discovery.
- [x] Implement inspect, convert, and import CLI commands.
- [x] Add synthetic fixtures and round-trip/regression tests.
- [x] Validate native discovery/resume in `basic-claude-uv`.
- [x] Publish the dedicated repository.

Completed on 2026-08-17. Native resume was validated in both directions against
the pinned image using credential-free, network-disabled synthetic fixtures.
The repository is `xhluca/session-migrate`.
