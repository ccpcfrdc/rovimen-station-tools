# Security Policy

## Reporting a vulnerability

Please report security issues **privately**, not through public GitHub issues.

- Use [GitHub's private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
  ("Report a vulnerability" under the repository's **Security** tab), or
- Open a minimal issue asking for a private contact channel (no details).

We aim to acknowledge reports within a few days.

## Scope

This repository is operational tooling for meteor camera stations. The most
sensitive areas are:

- **`dashboard/`** — the web dashboard and its public read-only API. It is
  built to run behind authentication; the anonymous public surface is
  explicitly opt-in per station (`public:` flag) and per page (`public_pages`).
- **Station configuration** — `dashboard_config.yaml` and any real credentials
  are gitignored and must never be committed. If you find committed secrets in
  a fork or PR, treat them as compromised and report them.

## What is intentionally not here

Live network configuration (station IPs, SSH users, credentials, private keys)
is excluded from this public repository by design. Example configs use
placeholder values.
