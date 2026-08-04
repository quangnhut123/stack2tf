# Security Policy

## Supported versions

stack2tf is pre-1.0; only the latest released version receives fixes.

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue.

Use GitHub's private vulnerability reporting for this repository:
**Security → Report a vulnerability** (Repository → *Security* tab →
*Report a vulnerability*).

Include a description, reproduction steps, and impact. We aim to acknowledge
reports within a few days.

## Scope notes

- stack2tf shells out to `terraform`/`tofu` and generates Terraform code from
  your stack files. It does not transmit your configuration anywhere.
- Never commit real credentials, account IDs, ARNs, or OIDC tokens. The provided
  `.gitignore` excludes generated state, plans, and common secret file patterns.
