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
to record Claude → Pi and Claude → Codex in native 1440p terminal panes. Claude
review is presented at 2× while target continuation remains at 1×. The recorder uses private,
disposable auth copies and publishes only synthetic TUI frames.
