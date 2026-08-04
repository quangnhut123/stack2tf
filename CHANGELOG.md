# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
-

### Changed
-

### Fixed
-

## [0.1.0] - 2026-08-04

### Added
- CLI (`list`, `show`, `validate`, `plan`, `apply`, `destroy`) that runs HCP
  Terraform Stacks with plain Terraform / OpenTofu.
- Dependency DAG with a concurrent run queue (`--parallelism`).
- Per-component standalone Terraform modules with per-component state (local or S3).
- Deployment inputs evaluated by Terraform via generated `deploy.tf` locals.
- OIDC `assume_role_with_web_identity` auth (hub + spoke roles).
- Whole-stack planning engine: `plan -out` + `show -json`, auto-derived
  cross-component values, unified plan report (`stack-plan.json`).
- Cross-stack outputs: `published_outputs.json` + `--upstream`.
- `--mocks` seeding for first-time plans.
- Packaging (`pip install stack2tf`), a reusable GitHub Action, a CircleCI orb,
  and a no-cloud example fixture (`examples/local-stack`).

[Unreleased]: https://github.com/quangnhut123/stack2tf/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/quangnhut123/stack2tf/releases/tag/v0.1.0
