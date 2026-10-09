# Contributing to session-migrate

## Contributor eligibility

To reduce spam from AI agents, all pull request authors must meet this requirement:

> Pull request authors must have a GitHub account at least six calendar months old and at least 100 GitHub contributions dated on or before the date one calendar month before the check.

This is the same contributor filter used by
[bm25s](https://github.com/xhluca/bm25s/blob/03eac5a8fb3aa4958ce37415a4cf2220d35157f7/CONTRIBUTING.md).
The check uses UTC dates and contribution history visible to repository automation.
It counts activity across the account's history, excluding the most recent month.
For example, an October 9 check counts contributions on September 9 or earlier
and requires an account created on April 9 or earlier. Private contributions
that GitHub does not expose cannot be counted.

New, reopened, or updated PRs receive an automated reply with the account age and
qualifying contribution count. A PR is closed if either requirement is missing.
API failures do not close PRs. Passing this check does not approve the code or
authorize executing contributor code.

## Proposing another harness

Maintaining adapters requires checking every source/target route and keeping up
with changes to native session formats. We currently consider new core harnesses
only if they have **at least 10,000 GitHub stars OR 1 million downloads per month**.
Either threshold is sufficient. Link the official repository or package's dated
download statistics in a [Discussion](https://github.com/xhluca/session-migrate/discussions)
before implementing a new adapter. Meeting the threshold does not guarantee
acceptance; native format evidence and the existing conversion tests still apply.

This project currently supports terminal harnesses. Desktop and cloud integrations
need a separate scope discussion. Existing supported adapters are not removed by
this policy. Once an extension interface is available, we can reconsider smaller
community integrations without expanding the core conversion matrix.

## Preparing a pull request

Keep each change focused. Explain a reproducible problem, the resulting behavior,
and how you tested it. Include regression tests for bugs and native acceptance
evidence for format changes. Contributors are responsible for understanding and
validating their changes, including changes prepared with AI assistance.

```sh
uv sync --dev --locked
uv run ruff check .
uv run ruff format --check src tests scripts .github/scripts .github/tests
uv run pytest -q
python3 -m unittest discover .github/tests
```

Follow the [development guide](docs/development.md) for adapter, native-client,
privacy, and release checks. Contributor eligibility is an initial filter;
correctness, data preservation, security, and maintainability still require review.
