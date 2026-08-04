# Contributing to stack2tf

Thanks for your interest in improving stack2tf! Contributions of all kinds are
welcome — bug reports, docs, and code.

## Development setup

```bash
git clone git@github.com:quangnhut123/stack2tf.git
cd stack2tf
pip install -r requirements.txt        # python-hcl2
# terraform or tofu on PATH, e.g. brew install hashicorp/tap/terraform
```

## Running checks locally

stack2tf ships a no-cloud example so you can exercise the full engine without
any credentials:

```bash
python3 hclexpr.py                                   # expression-translator self-tests -> ALL PASS
python3 stack2tf.py plan --chdir examples/local-stack # whole-stack plan on the fixture
```

Both run in CI (`.github/workflows/ci.yml`) on every pull request.

## Guidelines

- Keep changes focused; prefer small, reviewable PRs.
- Match the existing style (standard library only in the engine; `python-hcl2`
  is the sole runtime dependency).
- Cover behavior changes with the `examples/local-stack` fixture where possible.
- Update `README.md` and `CHANGELOG.md` (the **Unreleased** section) for any
  user-visible change or new flag.
- Do not commit secrets, real account IDs, ARNs, or generated artifacts
  (`.stack2tf/`, `dist/`, state files are already git-ignored).

## Pull request checklist

- [ ] `python3 hclexpr.py` passes
- [ ] `python3 stack2tf.py plan --chdir examples/local-stack` works
- [ ] README / CHANGELOG updated if needed
- [ ] No secrets or environment-specific values added

## Reporting security issues

Please do not open public issues for security-sensitive reports. Instead, use
GitHub's private vulnerability reporting for this repository.

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).
