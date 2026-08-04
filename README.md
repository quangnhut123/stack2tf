# stack2tf

> Run **HCP Terraform Stacks** configurations with plain **Terraform** / **OpenTofu** — no HCP Terraform, no extra orchestrator.

![python](https://img.shields.io/badge/python-3.8%2B-blue)
![pypi](https://img.shields.io/pypi/v/stack2tf)
![terraform](https://img.shields.io/badge/terraform%20|%20opentofu-supported-623CE4)
![dependencies](https://img.shields.io/badge/deps-python--hcl2-lightgrey)
![license](https://img.shields.io/badge/license-MIT-green)
![status](https://img.shields.io/badge/status-experimental-orange)

stack2tf reads a Terraform Stacks project (`*.tfcomponent.hcl` + `*.tfdeploy.hcl`)
and runs it on the open-source CLI. It builds the component dependency graph,
generates a standalone Terraform root module per component, executes them in
dependency order, and passes each component's outputs to its dependents — the
same model as Terraform Stacks, without the hosted platform.

```text
        ┌──────────────── stack2tf ────────────────┐
        │  parse stack → build DAG → generate TF →  │
        │  run terraform per component → wire outputs│
        └───────────────────────────────────────────┘
                 │ drives
                 ▼
           terraform / tofu   (does the real provisioning)
```

---

## Table of contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [CLI reference](#cli-reference)
- [Concepts](#concepts--stacks-feature-mapping)
- [Authentication](#authentication)
- [State](#state)
- [Whole-stack planning](#whole-stack-planning)
- [Cross-stack outputs](#cross-stack-outputs)
- [CI/CD](#cicd)
- [Compatibility with Terraform Stacks](#compatibility-with-terraform-stacks)
- [Limitations](#limitations)
- [Project layout](#project-layout)
- [Contributing](#contributing)
- [Releasing](#releasing)
- [License](#license)

---

## Features

- **Dependency DAG + run queue** — components run in dependency order; independent
  ones can run concurrently (`--parallelism`).
- **Output propagation** — `component.x.output` is wired to dependents automatically.
- **`for_each` components & per-account providers** — expanded to one module per instance.
- **Deployment inputs evaluated by Terraform** — any HCL function works (`merge`,
  `cidrsubnet`, …), not a hard-coded subset.
- **OIDC auth** — `assume_role_with_web_identity`, hub + spoke roles, same as Stacks.
- **Remote state per component** — optional S3 backend with a per-component key.
- **Whole-stack planning** — a single unified plan report across all components,
  with cross-component values derived from real plan artifacts.
- **Cross-stack outputs** — publish/consume outputs between stacks.
- **No lock-in** — pure Python + the `terraform`/`tofu` binary you already use.

## How it works

```text
stack2tf.py    CLI: discover deployments, dispatch commands
   └── stackrun.py    engine: build units, DAG run queue, generate TF,
                      run terraform, propagate outputs, aggregate the plan
         ├── stackparse.py   parse stack files, for_each expansion, each.* substitution
         └── hclexpr.py      reconstruct HCL expressions, rewrite Stacks references
```

Each component instance becomes a standalone Terraform root module:

```text
<stack>/.stack2tf/<deployment>/<component>/
  main.tf                 provider (assume_role_with_web_identity) + module "this" + output "outputs"
  deploy.tf               deployment inputs as Terraform-evaluated locals (local._deploy.*)
  variables.tf            identity_token + dep_<component> variables
  locals.tf               ported stack locals (native TF functions)
  backend.tf              S3 (per-component key) when --state-bucket is set, else local
  upstream.tf             other stacks' published outputs (only with --upstream)
  terraform.tfvars.json   injected dependency outputs (written at run time)
```

## Requirements

- Python **3.8+**
- `terraform` **or** `tofu` on `PATH`
  (needed for `validate`/`plan`/`apply`/`destroy`; `list`/`show`/`--dry-run` do not)
- AWS credentials able to assume the deployment / spoke roles (for real plan/apply)

## Installation

### From PyPI (recommended)

stack2tf is published on [PyPI](https://pypi.org/project/stack2tf/):

```bash
pip install stack2tf
```

This puts a `stack2tf` command on your `PATH` (and installs the only runtime
dependency, `python-hcl2`). You still need `terraform` or `tofu` available, e.g.:

```bash
brew install hashicorp/tap/terraform     # or: brew install opentofu
stack2tf --help
```

Pin a version if you prefer: `pip install stack2tf==0.1.0`.

### From source

```bash
git clone https://github.com/quangnhut123/stack2tf.git
cd stack2tf
pip install .                          # or: pip install -r requirements.txt
```

Or install a specific tag straight from git:

```bash
pip install "git+https://github.com/quangnhut123/stack2tf@v0.1.0"
```

## Quick start

Try it with the bundled no-cloud example (uses the built-in `terraform_data`
resource — no AWS, no credentials):

```bash
python3 stack2tf.py plan --chdir examples/local-stack
```

```text
============================================================
WHOLE-STACK PLAN
============================================================
  base       +1 ~0 -0
  consumer   +1 ~0 -0
------------------------------------------------------------
  TOTAL      +2 ~0 -0
```

Against a real stack:

```bash
# 1. inspect (no AWS calls)
python3 stack2tf.py list     --chdir ../my-stack
python3 stack2tf.py plan     --chdir ../my-stack --deployment prod --dry-run

# 2. validate generated Terraform (init + validate, no provisioning)
python3 stack2tf.py validate --chdir ../my-stack --deployment prod

# 3. provide the OIDC token, then provision in dependency order
export AWS_WEB_IDENTITY_TOKEN="$(cat token.jwt)"
python3 stack2tf.py apply    --chdir ../my-stack --deployment prod \
        --state-bucket my-tf-state --state-region ap-southeast-1 --state-dynamodb-table tf-locks

# tear down (reverse order)
python3 stack2tf.py destroy  --chdir ../my-stack --deployment prod
```

> Tip: start with a single component via `--target <name>` on a non-prod
> deployment before applying the whole stack.

## CLI reference

```text
python3 stack2tf.py <command> [options]
```

| Command | Description |
|---|---|
| `list` | Discover deployments + components; print the DAG run order |
| `show` | Print resolved per-component provider roles and deployment inputs |
| `validate` | `init -backend=false` + `validate` for every component (offline) |
| `plan` | Whole-stack plan in DAG order; unified report + `stack-plan.json` |
| `apply` | Provision the whole deployment in dependency order |
| `destroy` | Destroy in reverse dependency order |

| Option | Description |
|---|---|
| `--chdir DIR` | Stack directory (default: cwd) |
| `--deployment NAME` | Deployment name/file (default: all discovered) |
| `--tf terraform\|tofu` | Binary to drive (default: `terraform`) |
| `--target NAME` | Operate on a single component instance |
| `--parallelism N` | Max components to run concurrently within a DAG level (default 1) |
| `--dry-run` | Generate modules + print run order, without invoking Terraform |
| `--identity-token-file PATH` | OIDC JWT file (else `$AWS_WEB_IDENTITY_TOKEN[_FILE]`) |
| `--state-bucket NAME` | Enable S3 remote state (per-component key) |
| `--state-region REGION` | Region of the S3 state bucket |
| `--state-dynamodb-table T` | DynamoDB table for state locking |
| `--state-key-prefix P` | S3 state key prefix (default `stack2tf`) |
| `--mocks FILE` | JSON mock outputs `{component: {output: value}}` for a first-time plan |
| `--upstream name=FILE` | Consume another deployment's `published_outputs.json` (repeatable) |

## Concepts — Stacks feature mapping

| Terraform Stacks | stack2tf |
|---|---|
| `component "x" { source, inputs }` | a per-component Terraform root module calling that `source` |
| `depends_on` / `component.x.out` | DAG edge; `component.x.out` → `var.dep_x.out`, injected from x's outputs |
| `for_each` component | expanded to one module per instance (`component.x["key"]`) |
| aggregate `[for k,v in component.x : …]` | `var.dep_x` = map `{key → outputs}` (indexed + aggregate handled uniformly) |
| `provider "aws" "this"` | `assume_role_with_web_identity` → the deployment role |
| `provider "aws" "spoke" { for_each }` | `assume_role_with_web_identity` → each spoke account role |
| `deployment "<env>" { inputs }` | generated `deploy.tf` `locals`, evaluated by Terraform (`local._deploy.*`) |
| stack `locals` | ported to `locals.tf` (native TF functions) |
| `identity_token.aws.jwt` | `var.identity_token` (from `--identity-token-file` / env) |
| per-component state | S3 backend with per-component key (`--state-bucket`) or local |
| `publish_output` / `upstream_input` | `published_outputs.json` + `--upstream name=path` (`upstream_input.name.*` → `local._upstream.name.*`) |

## Authentication

Providers use Terraform-native `assume_role_with_web_identity`, matching Stacks:

- `provider.aws.this` components assume the **deployment role**.
- `provider.aws.spoke[...]` components assume the resolved **spoke account role**.

The OIDC JWT is passed as `var.identity_token` via `TF_VAR_identity_token` (kept
off disk). Provide it with `--identity-token-file` or `AWS_WEB_IDENTITY_TOKEN` /
`AWS_WEB_IDENTITY_TOKEN_FILE`.

## State

By default each component keeps local state. Pass `--state-bucket` (with
`--state-region`, optional `--state-dynamodb-table`) to generate an S3 backend
per component keyed `<prefix>/<deployment>/<component>/terraform.tfstate`.
`validate` always runs `init -backend=false`, so it works offline regardless.

## Whole-stack planning

`plan` runs stack-wide in DAG order. For each component it captures a real plan
artifact and derives that component's outputs:

```text
terraform plan -out=plan.bin       # per component, own state
terraform show -json plan.bin      # parse planned outputs + change summary
```

Known values are used directly; `known after apply` values become typed
placeholders. These feed dependents (`var.dep_<component>`), so downstream plans
use the upstream's *actual planned outputs* rather than static mocks. Results are
summarised into a single report plus machine-readable artifacts:

- `stack-plan.json` — per-component and total add/change/destroy counts
- `published_outputs.json` — each component's (planned or applied) outputs

For a brand-new stack you can seed values with `--mocks <file>`; anything
unresolved falls back to `try(var.dep_x.y, null)`.

## Cross-stack outputs

Every run writes `published_outputs.json` for the deployment. A downstream
deployment consumes it:

```bash
python3 stack2tf.py apply --chdir ../app-stack \
    --upstream platform=../platform-stack/.stack2tf/prod/published_outputs.json
```

`upstream_input.platform.*` references are rewritten to `local._upstream.platform.*`.

## CI/CD

stack2tf is a plain CLI, so it runs in any pipeline (GitHub Actions, CircleCI,
GitLab CI, …). `validate`, `list`, `show`, and `plan --dry-run` need **no cloud
access** and make great PR gates; `apply` needs remote state and credentials.

Two things to get right in CI:

- **Use remote state for `apply`.** CI runners are ephemeral — pass
  `--state-bucket`/`--state-region`/`--state-dynamodb-table` so state lives in S3.
- **Disable the Terraform wrapper.** Set `terraform_wrapper: false` in
  `setup-terraform` (or use `setup-opentofu`); the default wrapper intercepts
  output and breaks the engine's `terraform show -json` parsing.
- **Credentials.** The generated providers use `assume_role_with_web_identity`,
  and the target roles must trust the token you present. In CI either add your
  CI's OIDC provider to those roles' trust policies and pass the token via
  `--identity-token-file`, or supply base credentials (e.g. via
  `aws-actions/configure-aws-credentials`).

### GitHub Actions — PR check (no cloud access)

```yaml
name: stack2tf
on: [pull_request]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -r stack2tf/requirements.txt
      - uses: hashicorp/setup-terraform@v3
        with: { terraform_wrapper: false }
      - name: tests + whole-stack plan (fixture)
        run: |
          python3 stack2tf/hclexpr.py
          python3 stack2tf/stack2tf.py plan --chdir stack2tf/examples/local-stack
```

### GitHub Actions — deploy on main (AWS OIDC)

```yaml
  deploy:
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    permissions: { id-token: write, contents: read }
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -r stack2tf/requirements.txt
      - uses: hashicorp/setup-terraform@v3
        with: { terraform_wrapper: false }
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::<acct>:role/ci-bootstrap
          aws-region: ap-southeast-1
      - run: |
          python3 stack2tf/stack2tf.py apply --chdir my-stack --deployment prod \
            --state-bucket my-tf-state --state-region ap-southeast-1 \
            --state-dynamodb-table tf-locks --parallelism 4
```

### Reusable GitHub Action

This repo ships a composite action (`action.yml`) so consumers integrate with a
single `uses:` step — it installs stack2tf, sets up Terraform/OpenTofu (with the
wrapper disabled), and runs the command:

```yaml
- uses: actions/checkout@v4          # checkout the repo that holds your stack
- uses: quangnhut123/stack2tf@v0.1.0
  with:
    command: plan                    # list | show | validate | plan | apply | destroy
    chdir: my-stack
    deployment: prod
    tf: terraform                    # or: tofu
    args: "--state-bucket my-tf-state --state-region ap-southeast-1 --parallelism 4"
```

Inputs: `command`, `chdir` (required), `deployment`, `tf`, `args`,
`python-version`, `terraform-version`, `tofu-version`. For real `apply`, add an
`aws-actions/configure-aws-credentials` step (and `permissions: id-token: write`)
before it.

### Reusable CircleCI orb

A publishable orb lives in `circleci/orb.yml`. Publish it once to your namespace:

```bash
circleci orb create <namespace>/stack2tf
circleci orb publish circleci/orb.yml <namespace>/stack2tf@1.0.0
```

Then consume it (full example in `examples/circleci-config.yml`):

```yaml
version: 2.1
orbs:
  stack2tf: <namespace>/stack2tf@1.0.0
workflows:
  infra:
    jobs:
      - stack2tf/stack2tf:
          command: plan
          chdir: examples/local-stack
```

### CircleCI (without the orb)

```yaml
jobs:
  check:
    docker: [{ image: cimg/python:3.11 }]
    steps:
      - checkout
      - run: pip install -r stack2tf/requirements.txt
      - run: |
          curl -fsSL https://releases.hashicorp.com/terraform/1.15.8/terraform_1.15.8_linux_amd64.zip -o tf.zip
          unzip tf.zip && sudo mv terraform /usr/local/bin/
      - run: python3 stack2tf/stack2tf.py plan --chdir stack2tf/examples/local-stack
```

The `stack-plan.json` written by `plan` is a convenient CI artifact — upload it
or surface its add/change/destroy totals on the pull request.

## Compatibility with Terraform Stacks

| Capability | stack2tf | Terraform Stacks (HCP) |
|---|---|---|
| Component DAG + ordering | ✅ | ✅ |
| Output wiring between components | ✅ | ✅ |
| `for_each` / per-account providers | ✅ | ✅ |
| Deployment inputs (any HCL function) | ✅ | ✅ |
| Per-component state | ✅ | ✅ |
| Unified whole-stack plan | ✅ (from real plan artifacts) | ✅ |
| **Cross-component _deferred/unknown_ planning** | ⚠️ placeholder approximation | ✅ (hosted engine) |
| Runs on open-source CLI, self-hosted | ✅ | ❌ (hosted) |

The one capability that cannot be fully reproduced is HCP's **cross-component
deferred planning** (treating a not-yet-created upstream output as *unknown*
during a whole-stack plan). The open-source CLI plans one root module at a time
with concrete inputs and has no way to accept a genuinely-unknown cross-root
value. stack2tf approximates it with derived placeholders; the exact behavior is
gated on a CLI feature (OpenTofu issue #812, unshipped). See
[Limitations](#limitations).

## Limitations

- **Deferred planning.** Downstream values are derived from each upstream's real
  planned outputs (known values + `known after apply` placeholders). A resource
  keyed on a genuinely-unknown upstream output (`count`/`for_each`) is therefore
  not *truly* deferred. This is isolated to a single `UNKNOWN` sentinel in
  `stackrun.py`; if the CLI gains unknown plan inputs (OpenTofu #812), swap it for
  a real unknown and planning becomes truly deferred with no other change.
- **Deployment-input evaluation** covers literals plus any expression Terraform
  itself can evaluate in the generated `deploy.tf` locals.
- **`publish_output`/`upstream_input`** wiring is implemented but dormant unless
  those blocks are enabled in the source stack.

## Project layout

```text
stack2tf.py            CLI (list / show / validate / plan / apply / destroy)
stackrun.py            engine: DAG, module generation, runner, plan aggregation
stackparse.py          stack-file parsing, for_each expansion, each.* substitution
hclexpr.py             HCL expression reconstruction + reference rewriting (self-tests)
pyproject.toml         packaging (pip install -> `stack2tf` command)
requirements.txt       Python dependencies (python-hcl2)
CHANGELOG.md           Keep a Changelog / SemVer history
action.yml             reusable GitHub composite Action
.github/workflows/     ci.yml (fixture check) + release.yml (publish to PyPI)
circleci/orb.yml       publishable CircleCI orb
examples/local-stack/  no-AWS, 2-component fixture for trying `plan`
examples/circleci-config.yml   consumer example for the orb
```

Run the built-in self-tests for the expression translator:

```bash
python3 hclexpr.py     # -> ALL PASS
```

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for
setup and guidelines. In short:

1. Keep changes focused and covered by the `examples/local-stack` fixture where possible.
2. Run `python3 hclexpr.py` and `python3 stack2tf.py plan --chdir examples/local-stack` before submitting.
3. Describe behavior changes and any new flags in the PR.

## Releasing

Releases are published automatically by `.github/workflows/release.yml` when a
GitHub Release is published, using **PyPI Trusted Publishing (OIDC)** — no API
token or secret is stored. Routing:

- **Pre-release** (GitHub Release marked *pre-release*, e.g. tag `v0.2.0rc1`) →
  [TestPyPI](https://test.pypi.org/project/stack2tf/)
- **Normal release** (e.g. tag `v0.2.0`) → [PyPI](https://pypi.org/project/stack2tf/)

One-time setup — add a trusted publisher on **both** PyPI and TestPyPI
(Project → Settings → Publishing → *Add a pending publisher*):

| Field | PyPI | TestPyPI |
|---|---|---|
| Owner | your GitHub org/user | same |
| Repository | your repo name | same |
| Workflow name | `release.yml` | `release.yml` |
| Environment | `pypi` | `testpypi` |

### Release checklist

1. Update `CHANGELOG.md` — move items from **Unreleased** into a new version section.
2. Bump `version` in `pyproject.toml` (PEP 440, e.g. `0.2.0` or `0.2.0rc1`).
3. Commit, tag, and push: `git tag v0.2.0 && git push origin main --tags`.
4. Create a GitHub Release for the tag (tick **pre-release** to route to TestPyPI).
5. The workflow builds sdist + wheel, **verifies the tag matches the
   `pyproject.toml` version**, and publishes to the right index.
6. Verify the install: `pip install stack2tf==0.2.0` (or the TestPyPI index URL for pre-releases).

## License

Released under the [MIT License](LICENSE). Update the copyright holder in
`LICENSE` (currently "stack2tf contributors") to your name or organization.

---

<sub>stack2tf is an independent project and is not affiliated with HashiCorp or
the OpenTofu project. "Terraform" and "HCP Terraform" are trademarks of
HashiCorp; "OpenTofu" is a trademark of the OpenTofu project.</sub>
