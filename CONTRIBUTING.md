# Contributing

Thanks for your interest in ROVIMEN Station Tools. This is tooling for GMN
meteor camera stations; contributions that make it more useful to other
operators are very welcome.

## Ground rules

- **Never commit secrets or real network config.** `dashboard_config.yaml`,
  credentials, private keys, and per-station configs are gitignored on purpose.
  Use the `.example` files with placeholder values.
- **Keep it config-driven.** No hard-coded station IPs, camera codes, hostnames,
  or paths in code — read them from config or the environment.
- **Tests must pass** before a PR is merged (see below).

## Development

```bash
uv sync                 # Python environment
npm ci                  # JS toolchain
```

## Tests

```bash
npm test                                                       # vitest (JS units)
uv run pytest tests/ -v                                        # pytest (Python)
npx playwright test --config tests/e2e/playwright.config.js    # Playwright E2E
```

CI runs all three on every pull request.

## Conventions

- Python 3.13+, type hints on all functions (`list[str]`, `str | None`).
- Pydantic models for configuration and data validation.
- `logging` module (`logger = logging.getLogger(__name__)`), not `print`.
- No hard-coded station IPs, camera codes, hostnames, or paths — everything
  comes from config files.

## Commit / PR style

Semantic commit prefixes: `feat:`, `fix:`, `docs:`, `refactor:`, `chore:`,
`ci:`, `test:`, `perf:`, `style:`, `build:`. PR titles use the same format.

## License

By contributing you agree that your contributions are licensed under the
project's [GPL-3.0](LICENSE) license.
