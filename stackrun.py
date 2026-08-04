#!/usr/bin/env python3
"""
stackrun.py — self-contained HCP Terraform Stacks runner.

Drives `terraform`/`tofu` directly to run a whole Stacks deployment as one unit,
with no external orchestrator. It provides:
  * a dependency DAG built from component references
  * a run queue that executes independent components concurrently, ancestors
    before dependents (see topo_levels)
  * dependency-output propagation between components
  * provider/backend generation per component

Pipeline per deployment:
  1. parse stack -> expand for_each components into concrete units
  2. build dependency DAG -> run queue (topo_levels)
  3. generate a standalone Terraform root module per unit
  4. run terraform init/apply per unit (levels may run concurrently), read
     `output -json`, inject those outputs into dependents as var.dep_<component>
  5. destroy runs the queue in reverse

Usage:
  python3 stackrun.py <stack_dir> <deploy_file> [--out DIR] [--tf terraform|tofu]
                      [--action plan|apply|destroy|validate] [--dry-run]
                      [--target UNIT] [--parallelism N]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import hclexpr as H
import stackparse as G


# ==========================================================================
# model
# ==========================================================================
class Unit:
    def __init__(self, comp, name, body, each_key=None, each_value=None):
        self.comp = comp
        self.name = name
        self.body = body
        self.each_key = each_key
        self.each_value = each_value
        self.role_arn = None          # concrete assume_role arn or None
        self.dep_units = set()        # unit names this unit depends on
        self.dep_comps = set()        # component names referenced (for var.dep_*)

    def __repr__(self):
        return f"Unit({self.name})"


# ==========================================================================
# deployment inputs -> Terraform-evaluated locals
# ==========================================================================
# Deployment inputs are emitted verbatim into a generated `deploy.tf` locals
# block, so Terraform (not Python) evaluates any function they use (merge,
# format, cidrsubnet, ...). Components reference them as local._deploy.<name>.
_DEPLOY_SKIP = ("role_arn", "identity_token")  # provider-only / ephemeral

_VAR_TO_DEPLOY = re.compile(r"\bvar\.(?!dep_)(?!identity_token\b)([A-Za-z_][A-Za-z0-9_]*)")


def deploy_ref(expr: str) -> str:
    """Point deployment-input references at the generated locals:
    var.NAME -> local._deploy.NAME (leaving var.dep_* and var.identity_token)."""
    return _VAR_TO_DEPLOY.sub(r"local._deploy.\1", expr)


def render_deploy_tf(dep_name, dep_inputs_raw, common):
    """Render deploy.tf: ported common locals + the _deploy input object."""
    lines = ["locals {"]
    for block in common.get("locals", []):
        for k, v in block.items():
            if k.startswith("__"):
                continue
            lines.append(f"  {k} = {H.to_hcl(v, rewrite=False)}")
    lines.append("  _deploy = {")
    for k, v in dep_inputs_raw.items():
        if k.startswith("__") or k in _DEPLOY_SKIP:
            continue
        expr = H.to_hcl(v, rewrite=False)
        expr = expr.replace("identity_token.aws.jwt", "var.identity_token")
        expr = expr.replace("upstream_input.", "local._upstream.")
        lines.append(f"    {k} = {expr}")
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def input_names(dep_inputs_raw):
    return sorted(k for k, _ in dep_inputs_raw.items()
                  if not k.startswith("__") and k not in _DEPLOY_SKIP)


# ==========================================================================
# provider role resolution (concrete arn)
# ==========================================================================
def resolve_role_arn(unit, spoke_accounts, dep_inputs):
    """Concrete IAM role ARN each component's provider assumes via web identity.
    provider.aws.this  -> the deployment role (var.role_arn from the deploy inputs)
    provider.aws.spoke -> the spoke account's role
    """
    aws_ref = (unit.body.get("providers", {}) or {}).get("aws")
    if not aws_ref:
        return dep_inputs.get("role_arn")
    inner = H._unwrap_interpolation(aws_ref) if isinstance(aws_ref, str) else aws_ref
    inner = (inner or "").strip()
    if inner == "provider.aws.this":
        return dep_inputs.get("role_arn")
    m = re.match(r"provider\.aws\.spoke\[(.+)\]$", inner)
    if m:
        idx = m.group(1).strip()
        if idx == "each.key":
            key = unit.each_key
        elif idx == "each.value.account":
            key = unit.each_value.get("account") if isinstance(unit.each_value, dict) else None
        else:
            key = G.resolve_static_account(idx, dep_inputs)
        return spoke_accounts.get(key)
    return dep_inputs.get("role_arn")


# ==========================================================================
# build units + DAG
# ==========================================================================
def build_units(stack_dir, deploy_file):
    comp_blocks, providers, deploy, common, stack_locals = G.load_stack(stack_dir, deploy_file)
    comps = G.components_list(comp_blocks)
    dep_name, dep_body = G.get_deployment(deploy)
    dep_inputs_raw = dep_body.get("inputs", {})
    dep_inputs = {k: G.pyval(v) for k, v in dep_inputs_raw.items() if not k.startswith("__")}
    spoke_accounts = dep_inputs.get("spoke_accounts", {})

    foreach_comps = {}
    for name, body in comps:
        c = G.foreach_collection(body)
        if c:
            foreach_comps[name] = c

    # expand
    units = []
    by_comp = {}
    for name, body in comps:
        for inst in G.expand_component(name, body, dep_inputs):
            u = Unit(inst.comp, inst.unit, inst.body, inst.each_key, inst.each_value)
            u.role_arn = resolve_role_arn(u, spoke_accounts, dep_inputs)
            units.append(u)
            by_comp.setdefault(name, []).append(u)

    # dependency edges (unit-level) + referenced component set
    for u in units:
        comp_deps = H.component_deps(u.body)
        for d in comp_deps:
            if d == u.comp or d not in by_comp:
                continue
            u.dep_comps.add(d)
            for du in by_comp[d]:
                u.dep_units.add(du.name)

    meta = dict(dep_name=dep_name, foreach_comps=foreach_comps, by_comp=by_comp,
                stack_locals=stack_locals, dep_inputs=dep_inputs,
                dep_inputs_raw=dep_inputs_raw, common=common,
                input_names=input_names(dep_inputs_raw), spoke_accounts=spoke_accounts)
    return units, meta


def topo_order(units):
    """Kahn topological sort (deterministic). Raises on cycle."""
    names = {u.name for u in units}
    indeg = {u.name: len([d for d in u.dep_units if d in names]) for u in units}
    adj = {u.name: [] for u in units}
    for u in units:
        for d in u.dep_units:
            if d in names:
                adj[d].append(u.name)
    ready = sorted([n for n, d in indeg.items() if d == 0])
    order = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for m in sorted(adj[n]):
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
        ready.sort()
    if len(order) != len(units):
        remaining = [n for n in names if n not in order]
        raise SystemExit(f"dependency cycle among: {remaining}")
    return order


def topo_levels(units):
    """Group units into dependency levels (a "run queue").

    Level 0 = units with no dependencies; each later level contains units whose
    dependencies all live in earlier levels. Units within a level are mutually
    independent and may run concurrently. Raises on cycle.
    """
    names = {u.name for u in units}
    remaining = {u.name: {d for d in u.dep_units if d in names} for u in units}
    done, levels = set(), []
    while remaining:
        ready = sorted([n for n, deps in remaining.items() if deps <= done])
        if not ready:
            raise SystemExit(f"dependency cycle among: {sorted(remaining)}")
        levels.append(ready)
        done |= set(ready)
        for n in ready:
            del remaining[n]
    return levels


# ==========================================================================
# generate a standalone Terraform module per unit
# ==========================================================================
def _wrap_deferred(expr):
    """For stack-wide plan before deps exist: fall back to null when an injected
    dependency output isn't available yet (approximates Stacks deferred changes)."""
    if "var.dep_" in expr:
        return f"try({expr}, null)"
    return expr


def gen_module(unit, unit_dir, stack_dir, meta, opts=None):
    opts = opts or {}
    mock = opts.get("mock", False)
    state = opts.get("state")  # None -> local backend; dict -> s3 backend
    os.makedirs(unit_dir, exist_ok=True)
    source = G.rel_source(unit_dir, stack_dir, unit.body["source"])

    # provider(s): emit the aws provider only for components that use it (all
    # real-stack components declare `providers = { aws = ... }`). Uses OIDC
    # web-identity, same as the source stack. Secondary/built-in providers
    # auto-propagate to child modules, so no explicit providers map is needed.
    uses_aws = "aws" in (unit.body.get("providers") or {}) or bool(unit.role_arn)
    prov = []
    if uses_aws:
        prov = ['provider "aws" {', "  region = local._deploy.aws_region"]
        if unit.role_arn:
            prov += [
                "  assume_role_with_web_identity {",
                f'    role_arn           = "{unit.role_arn}"',
                "    web_identity_token = var.identity_token",
                f'    session_name       = "stack2tf-{unit.name}"',
                "  }",
            ]
        prov.append("}")

    # inputs -> module args (rewrite refs, each.* substitution, point deployment
    # inputs at local._deploy, deferred wrap for plan)
    arg_lines = []
    for k, v in unit.body.get("inputs", {}).items():
        if k.startswith("__"):
            continue
        expr = H.to_hcl(v)
        expr = G.substitute_each(expr, unit.each_key, unit.each_value)
        expr = deploy_ref(expr)
        if mock:
            expr = _wrap_deferred(expr)
        arg_lines.append(f"  {k} = {expr}")

    main = []
    main += prov
    main.append("")
    main.append(f'module "this" {{\n  source = "{source}"')
    main += arg_lines
    main.append("}")
    main.append("")
    main.append('output "outputs" {\n  value     = module.this\n  sensitive = true\n}')
    write(os.path.join(unit_dir, "main.tf"), "\n".join(main) + "\n")

    # deploy.tf : deployment inputs as Terraform-evaluated locals (any function)
    write(os.path.join(unit_dir, "deploy.tf"),
          render_deploy_tf(meta["dep_name"], meta["dep_inputs_raw"], meta["common"]))

    # upstream.tf : outputs published by other stacks (consumed via upstream_input.*)
    upstream = opts.get("upstream") or {}
    if upstream:
        write(os.path.join(unit_dir, "upstream.tf"),
              "locals {\n  _upstream = " + G.py_to_hcl(upstream) + "\n}\n")

    # variables.tf : OIDC token + dep_<comp> vars (deployment inputs are locals now)
    var_lines = ['variable "identity_token" {\n  type      = string\n'
                 '  default   = ""\n  sensitive = true\n}']
    for d in sorted(unit.dep_comps):
        var_lines.append(f'variable "dep_{d}" {{\n  type    = any\n  default = null\n}}')
    write(os.path.join(unit_dir, "variables.tf"), "\n".join(var_lines) + "\n")

    # locals.tf : ported stack locals (deployment refs -> local._deploy.*)
    if meta["stack_locals"]:
        ll = ["locals {"]
        for block in meta["stack_locals"]:
            for k, v in block.items():
                if k.startswith("__"):
                    continue
                ll.append(f"  {k} = {deploy_ref(H.to_hcl(v))}")
        ll.append("}")
        write(os.path.join(unit_dir, "locals.tf"), "\n".join(ll) + "\n")

    # backend.tf : S3 (per-component key) when configured, else local
    write(os.path.join(unit_dir, "backend.tf"), _backend_block(unit, meta, state))


def _backend_block(unit, meta, state):
    if not state:
        return 'terraform {\n  backend "local" {}\n}\n'
    key = f'{state.get("key_prefix", "stack2tf")}/{meta["dep_name"]}/{unit.name}/terraform.tfstate'
    lines = ['terraform {', '  backend "s3" {',
             f'    bucket = "{state["bucket"]}"',
             f'    key    = "{key}"',
             f'    region = "{state["region"]}"',
             "    encrypt = true"]
    if state.get("dynamodb_table"):
        lines.append(f'    dynamodb_table = "{state["dynamodb_table"]}"')
    lines += ["  }", "}", ""]
    return "\n".join(lines)




def write(path, content):
    with open(path, "w") as f:
        f.write(content)


# ==========================================================================
# tfvars assembly (dependency outputs only; deployment inputs are locals now)
# ==========================================================================
def unit_tfvars(unit, meta, collected):
    """Build terraform.tfvars.json: dep_<component> = upstream outputs (or {})."""
    vals = {}
    for d in unit.dep_comps:
        if d in meta["foreach_comps"]:
            # map of instance-key -> that instance's outputs
            vals[f"dep_{d}"] = {du.each_key: collected.get(du.name, {})
                                for du in meta["by_comp"][d]}
        else:
            du = meta["by_comp"][d][0]
            vals[f"dep_{d}"] = collected.get(du.name, {})
    return vals


# ==========================================================================
# runner
# ==========================================================================
def tf(cmd, cwd, tf_bin, env=None, capture=False):
    full = [tf_bin] + cmd
    print(f"    $ {' '.join(full)}  (cwd={os.path.relpath(cwd)})")
    return subprocess.run(full, cwd=cwd, env=env,
                          capture_output=capture, text=True, check=True)


def _run_one(n, by_name, out_dir, meta, collected, tf_bin, action, env):
    """Generate tfvars from collected outputs, run terraform, return (name, outputs, summary).
    For plan: capture `plan -out` + `show -json` to derive planned outputs and a
    per-component change summary."""
    u = by_name[n]
    d = os.path.join(out_dir, n)
    write(os.path.join(d, "terraform.tfvars.json"),
          json.dumps(unit_tfvars(u, meta, collected), indent=2))
    print(f"\n>>> {n}")
    if action == "validate":
        tf(["init", "-input=false", "-backend=false"], d, tf_bin, env=env)
        tf(["validate"], d, tf_bin, env=env)
        return n, {}, None
    tf(["init", "-input=false"], d, tf_bin, env=env)

    if action == "plan":
        tf(["plan", "-input=false", "-out=plan.bin"], d, tf_bin, env=env)
        planjson = tf(["show", "-json", "plan.bin"], d, tf_bin, env=env, capture=True)
        data = json.loads(planjson.stdout or "{}")
        outputs = _planned_outputs(data)
        summary = _change_summary(data)
        return n, outputs, summary

    if action == "apply":
        tf(["apply", "-input=false", "-auto-approve"], d, tf_bin, env=env)
    elif action == "destroy":
        tf(["destroy", "-input=false", "-auto-approve"], d, tf_bin, env=env)
    out = {}
    if action == "apply":
        try:
            r = tf(["output", "-json"], d, tf_bin, env=env, capture=True)
            out = json.loads(r.stdout or "{}").get("outputs", {}).get("value", {})
        except Exception as e:  # noqa
            print(f"    ({n}: could not read outputs: {e})")
    return n, out, None


# ---- plan-artifact parsing (Level-2 engine) ------------------------------
# Sentinel for an upstream output that is "known after apply" during a
# whole-stack plan. This is the single forward-compatibility hook: if the CLI
# ever accepts genuinely-unknown plan inputs (OpenTofu #812), swap this
# placeholder for a real unknown value and the plan becomes truly deferred.
UNKNOWN = "(known after apply)"


def _merge_known(after, unknown):
    """Combine `after` (known parts) with `after_unknown` (bool/obj/list) into a
    value where unknown leaves become the UNKNOWN sentinel — a typed placeholder."""
    if unknown is True:
        return UNKNOWN
    if unknown is False or unknown is None:
        return after
    if isinstance(unknown, dict):
        base = dict(after) if isinstance(after, dict) else {}
        for k, uv in unknown.items():
            base[k] = _merge_known(base.get(k), uv)
        return base
    if isinstance(unknown, list):
        out = []
        for i, uv in enumerate(unknown):
            av = after[i] if isinstance(after, list) and i < len(after) else None
            out.append(_merge_known(av, uv))
        return out
    return after


def _planned_outputs(plan_data):
    """Extract this component's planned outputs (our single 'outputs' object),
    resolving known-after-apply parts to placeholders."""
    oc = plan_data.get("output_changes", {}).get("outputs")
    if oc is not None:
        return _merge_known(oc.get("after"), oc.get("after_unknown"))
    # fallback: already-known outputs from state
    pv = plan_data.get("planned_values", {}).get("outputs", {}).get("outputs", {})
    return pv.get("value", {})


def _change_summary(plan_data):
    """Count add/change/destroy from resource_changes (replace counts as both)."""
    add = change = destroy = 0
    for rc in plan_data.get("resource_changes", []):
        actions = rc.get("change", {}).get("actions", [])
        if "create" in actions:
            add += 1
        if "update" in actions:
            change += 1
        if "delete" in actions:
            destroy += 1
    return {"add": add, "change": change, "destroy": destroy}


def _load_identity_token(identity_token_file):
    """Resolve the OIDC token: explicit file > AWS_WEB_IDENTITY_TOKEN(_FILE) env."""
    path = identity_token_file or os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE")
    if path and os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    return os.environ.get("AWS_WEB_IDENTITY_TOKEN", "")


def load_json_file(path):
    with open(path) as f:
        return json.load(f)


def load_upstreams(pairs):
    """Parse ['name=path', ...] into {name: <published_outputs.json contents>}."""
    out = {}
    for p in pairs or []:
        name, _, path = p.partition("=")
        if name and path and os.path.exists(path):
            out[name] = load_json_file(path)
    return out


def run(stack_dir, deploy_file, out_root, tf_bin, action, dry_run, target,
        parallelism=1, identity_token_file=None, state=None, mocks=None,
        upstream=None):
    units, meta = build_units(stack_dir, deploy_file)
    levels = topo_levels(units)
    if action == "destroy":
        levels = [list(reversed(lv)) for lv in reversed(levels)]
    by_name = {u.name: u for u in units}
    out_dir = os.path.join(out_root, meta["dep_name"])

    # stack-wide plan runs before deps exist -> defer missing dependency outputs
    opts = {"mock": action == "plan", "state": state, "upstream": upstream or {}}

    print(f"Deployment: {meta['dep_name']}")
    print(f"Units: {len(units)}   Action: {action}   TF: {tf_bin}"
          f"   parallelism={parallelism}"
          f"   state={'s3' if state else 'local'}"
          + ("   [DRY-RUN]" if dry_run else ""))
    print("Run queue (level = may run concurrently):")
    for i, lv in enumerate(levels):
        for n in lv:
            u = by_name[n]
            deps = ",".join(sorted(u.dep_units)) or "-"
            print(f"  L{i} {n}\n        role={u.role_arn}  deps=[{deps}]")

    # generate all modules
    for u in units:
        gen_module(u, os.path.join(out_dir, u.name), stack_dir, meta, opts)
    print(f"\nGenerated {len(units)} standalone TF modules under {out_dir}")

    collected = {}  # unit name -> outputs object
    # first-time plan: seed dependency outputs from user-supplied mocks so a plan
    # can be produced before anything is applied (real outputs override these).
    if action == "plan" and mocks:
        collected.update(mocks)
    if dry_run:
        for u in units:
            write(os.path.join(out_dir, u.name, "terraform.tfvars.json"),
                  json.dumps(unit_tfvars(u, meta, collected), indent=2))
        print("Dry-run: wrote tfvars.json (deps empty). Skipping terraform exec.")
        return out_dir, levels

    if not shutil.which(tf_bin):
        raise SystemExit(f"'{tf_bin}' not found on PATH; install it or use --dry-run")

    # OIDC token passed to terraform as TF_VAR_identity_token (kept off disk)
    env = dict(os.environ)
    token = _load_identity_token(identity_token_file)
    if token:
        env["TF_VAR_identity_token"] = token
    elif action in ("plan", "apply", "destroy") and any(u.role_arn for u in units):
        print("    (warning: no OIDC token found; set AWS_WEB_IDENTITY_TOKEN or "
              "--identity-token-file for assume_role_with_web_identity)")

    # single-target shortcut
    summaries = {}
    if target:
        _, out, summ = _run_one(target, by_name, out_dir, meta, collected, tf_bin, action, env)
        collected[target] = out
        if summ:
            summaries[target] = summ
        _finish(action, out_dir, collected, summaries, [target], by_name)
        return out_dir, levels

    # execute level by level; units within a level are independent and may run
    # concurrently. Outputs merge after a level completes.
    for lv in levels:
        if parallelism > 1 and len(lv) > 1:
            with ThreadPoolExecutor(max_workers=parallelism) as ex:
                futures = {ex.submit(_run_one, n, by_name, out_dir, meta,
                                     collected, tf_bin, action, env): n for n in lv}
                for fut in as_completed(futures):
                    n, out, summ = fut.result()
                    collected[n] = out
                    if summ:
                        summaries[n] = summ
        else:
            for n in lv:
                _, out, summ = _run_one(n, by_name, out_dir, meta, collected, tf_bin, action, env)
                collected[n] = out
                if summ:
                    summaries[n] = summ
    order = [n for lv in levels for n in lv]
    _finish(action, out_dir, collected, summaries, order, by_name)
    return out_dir, levels


def _finish(action, out_dir, collected, summaries, order, by_name):
    """Publish component outputs and, for plan, print+write the whole-stack report."""
    if action in ("apply", "plan"):
        write(os.path.join(out_dir, "published_outputs.json"),
              json.dumps(collected, indent=2))
    if action != "plan" or not summaries:
        return
    tot = {"add": 0, "change": 0, "destroy": 0}
    print("\n" + "=" * 60)
    print("WHOLE-STACK PLAN")
    print("=" * 60)
    for n in order:
        s = summaries.get(n)
        if not s:
            continue
        for k in tot:
            tot[k] += s[k]
        print(f"  {n:42} +{s['add']} ~{s['change']} -{s['destroy']}")
    print("-" * 60)
    print(f"  {'TOTAL':42} +{tot['add']} ~{tot['change']} -{tot['destroy']}")
    report = {"deployment": os.path.basename(out_dir), "totals": tot,
              "components": {n: summaries[n] for n in order if n in summaries}}
    write(os.path.join(out_dir, "stack-plan.json"), json.dumps(report, indent=2))
    print(f"\nWrote {os.path.join(out_dir, 'stack-plan.json')}")


# ==========================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stack_dir")
    ap.add_argument("deploy_file")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "run"))
    ap.add_argument("--tf", default="terraform", help="terraform or tofu")
    ap.add_argument("--action", default="plan", choices=["plan", "apply", "destroy", "validate"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--target", help="run a single unit only")
    ap.add_argument("--parallelism", type=int, default=1,
                    help="max units to run concurrently within a level (run queue)")
    ap.add_argument("--identity-token-file", help="file containing the OIDC JWT")
    ap.add_argument("--state-bucket", help="S3 bucket for remote state")
    ap.add_argument("--state-region", help="region of the S3 state bucket")
    ap.add_argument("--state-dynamodb-table", help="DynamoDB table for state locking")
    ap.add_argument("--state-key-prefix", default="stack2tf", help="S3 key prefix")
    ap.add_argument("--mocks", help="JSON file of mock outputs {component: {out: val}}")
    ap.add_argument("--upstream", action="append", default=[],
                    help="name=path to another deployment's published_outputs.json")
    args = ap.parse_args()
    state = None
    if args.state_bucket:
        state = {"bucket": args.state_bucket, "region": args.state_region,
                 "dynamodb_table": args.state_dynamodb_table,
                 "key_prefix": args.state_key_prefix}
    mocks = load_json_file(args.mocks) if args.mocks else None
    run(args.stack_dir, args.deploy_file, args.out, args.tf,
        args.action, args.dry_run, args.target, args.parallelism,
        args.identity_token_file, state, mocks, load_upstreams(args.upstream))
