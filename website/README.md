# Original vinext landing-page source

This directory preserves the vinext implementation used to design and validate
the original landing page. The canonical anonymous website now lives at
[`session-migrate.github.io`](https://session-migrate.github.io/) and its
deployable static source is maintained in
[`session-migrate/session-migrate.github.io`](https://github.com/session-migrate/session-migrate.github.io).

```bash
npm install
npm run dev
npm test
```

The root project still owns the demo source and native-media recorder. Run
`MIGRATE_NATIVE_CAPTURE_AUTH=1 scripts/render-demo.sh` from the repository root
to record Claude → Pi and Claude → Codex in native terminal panes. The
published story shows the source, migration, resume, shared-history, and target
continuation stages. The recorder uses private, disposable auth copies and
publishes only the controlled demo trajectory.

The site vendors Asciinema Player 3.17.0 so casts load without a third-party
runtime request. See the repository's `THIRD_PARTY_NOTICES.md`.
