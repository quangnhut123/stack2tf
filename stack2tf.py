#!/usr/bin/env python3
"""
stack2tf — run HCP Terraform Stacks with plain terraform/tofu.

Brings the Terraform Stacks experience to open-source Terraform/OpenTofu with no
HCP Terraform. Point it at a stack directory; it discovers the
deployments (*.tfdeploy.hcl) and components (*.tfcomponent.hcl) and operates on a
whole deployment at once, running every component in dependency (DAG) order with
one state per component — the same model as Terraform Stacks.

Commands (mirroring the Stacks workflow):
  stack2tf list       [--chdir DIR] [--deployment NAME]
  stack2tf validate   [--chdir DIR] [--deployment NAME] [--tf terraform|tofu]
  stack2tf plan       [--chdir DIR] [--deployment NAME] [--tf ...] [--dry-run]
  stack2tf apply      [--chdir DIR] [--deployment NAME] [--tf ...]
  stack2tf destroy    [--chdir DIR] [--deployment NAME] [--tf ...]
  stack2tf show       [--chdir DIR] [--deployment NAME]   # resolved providers/roles

If --deployment is omitted, the command runs for every discovered deployment.
Generated working modules live under <stack>/.stack2tf/<deployment>/<component>/.
"""
import argparse
import glob
import os
import sys

import stackrun as R


WORKDIR = ".stack2tf"


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------
def discover(stack_dir):
    """Return list of (deploy_file_basename, deployment_name)."""
    found = []
    for path in sorted(glob.glob(os.path.join(stack_dir, "*.tfdeploy.hcl"))):
        try:
            data = R.G.load(path)
        except Exception:  # noqa
            continue
        for block in data.get("deployment", []):
            for name in block:
                if not name.startswith("__"):
                    found.append((os.path.basename(path), R.H.unquote_key(name)))
                    break
    return found


def select(stack_dir, deployment):
    """Resolve --deployment to a list of (file, name); all if None."""
    all_d = discover(stack_dir)
    if not all_d:
        raise SystemExit(f"no *.tfdeploy.hcl with a deployment block in {stack_dir}")
    if not deployment:
        return all_d
    hits = [(f, n) for (f, n) in all_d
            if deployment in (n, f, f.replace(".tfdeploy.hcl", ""))]
    if not hits:
        names = ", ".join(n for _, n in all_d)
        raise SystemExit(f"deployment '{deployment}' not found. Available: {names}")
    return hits


def addr(unit):
    """Stacks-style component address: component.NAME or component.NAME[\"key\"]."""
    if unit.each_key is not None:
        return f'component.{unit.comp}["{unit.each_key}"]'
    return f"component.{unit.comp}"


def components_count(stack_dir, deploy_file):
    comp_blocks, *_ = R.G.load_stack(stack_dir, deploy_file)
    return len(R.G.components_list(comp_blocks))


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_list(stack_dir, args):
    deployments = select(stack_dir, args.deployment)
    print(f"Stack: {os.path.abspath(stack_dir)}")
    print(f"Deployments discovered: {len(deployments)}\n")
    for f, name in deployments:
        units, meta = R.build_units(stack_dir, f)
        order = R.topo_order(units)
        by_name = {u.name: u for u in units}
        ncomp = components_count(stack_dir, f)
        print(f"● deployment {name}   ({f})")
        print(f"  components: {ncomp}   instances (after for_each): {len(units)}")
        print(f"  run order:")
        for i, n in enumerate(order, 1):
            u = by_name[n]
            role = u.role_arn or "(deployment role)"
            deps = ", ".join(addr(by_name[d]) for d in sorted(u.dep_units)) or "—"
            print(f"    {i:2}. {addr(u)}")
            print(f"          account/role: {role}")
            print(f"          depends on:   {deps}")
        print()


def cmd_show(stack_dir, args):
    deployments = select(stack_dir, args.deployment)
    for f, name in deployments:
        units, meta = R.build_units(stack_dir, f)
        print(f"● deployment {name}   ({f})")
        print(f"  inputs: {', '.join(meta['input_names'])}")
        print(f"  providers (resolved per component):")
        for u in sorted(units, key=lambda x: x.name):
            print(f"    {addr(u):45} -> {u.role_arn or '(deployment role / ambient creds)'}")
        print()


def cmd_run(stack_dir, args, action):
    deployments = select(stack_dir, args.deployment)
    out_root = os.path.join(stack_dir, WORKDIR)
    state = None
    if getattr(args, "state_bucket", None):
        state = {"bucket": args.state_bucket, "region": args.state_region,
                 "dynamodb_table": args.state_dynamodb_table,
                 "key_prefix": args.state_key_prefix}
    mocks = R.load_json_file(args.mocks) if getattr(args, "mocks", None) else None
    upstream = R.load_upstreams(getattr(args, "upstream", []))
    for f, name in deployments:
        print(f"\n===== stack2tf {action}: deployment {name} =====")
        R.run(stack_dir, f, out_root, args.tf, action,
              getattr(args, "dry_run", False), getattr(args, "target", None),
              getattr(args, "parallelism", 1),
              getattr(args, "identity_token_file", None), state, mocks, upstream)


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(prog="stack2tf",
                                 description="Run Terraform Stacks with plain terraform/tofu.")
    ap.add_argument("command",
                    choices=["list", "show", "validate", "plan", "apply", "destroy"])
    ap.add_argument("--chdir", default=".", help="stack directory (default: cwd)")
    ap.add_argument("--deployment", help="deployment name/file (default: all)")
    ap.add_argument("--tf", default="terraform", help="terraform or tofu")
    ap.add_argument("--dry-run", action="store_true",
                    help="plan only: print order + generate modules, skip terraform")
    ap.add_argument("--target", help="operate on a single component instance")
    ap.add_argument("--parallelism", type=int, default=1,
                    help="max components to run concurrently within a DAG level "
                         "(run-queue model; default 1 = serial)")
    ap.add_argument("--identity-token-file",
                    help="file with the OIDC JWT for assume_role_with_web_identity "
                         "(else $AWS_WEB_IDENTITY_TOKEN[_FILE])")
    ap.add_argument("--state-bucket", help="S3 bucket for remote state (else local)")
    ap.add_argument("--state-region", help="region of the S3 state bucket")
    ap.add_argument("--state-dynamodb-table", help="DynamoDB table for state locking")
    ap.add_argument("--state-key-prefix", default="stack2tf", help="S3 state key prefix")
    ap.add_argument("--mocks",
                    help="JSON file of mock dependency outputs for a first-time plan "
                         "{component: {output: value}}")
    ap.add_argument("--upstream", action="append", default=[],
                    help="name=path to another deployment's published_outputs.json "
                         "(consumed via upstream_input.name.*); repeatable")
    args = ap.parse_args()

    stack_dir = os.path.abspath(args.chdir)
    if not os.path.isdir(stack_dir):
        raise SystemExit(f"not a directory: {stack_dir}")

    if args.command == "list":
        cmd_list(stack_dir, args)
    elif args.command == "show":
        cmd_show(stack_dir, args)
    else:
        cmd_run(stack_dir, args, args.command)


if __name__ == "__main__":
    main()
