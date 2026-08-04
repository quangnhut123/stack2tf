#!/usr/bin/env python3
"""
stackparse.py — parsing helpers shared by the stack2tf engine.

Loads HCP Terraform Stacks files via python-hcl2 and provides the primitives the
orchestrator needs: stack loading, component listing, deployment lookup, literal
evaluation, for_each expansion, and each.* substitution.
"""
import json
import os
import re

import hcl2

import hclexpr as H


# --------------------------------------------------------------------------
# loading the stack
# --------------------------------------------------------------------------
def load(path):
    with open(path) as f:
        return hcl2.load(f)


def load_stack(stack_dir, deploy_file):
    """Return (components, providers, deploy, common, stack_locals)."""
    comp = load(os.path.join(stack_dir, "components.tfcomponent.hcl"))["component"]
    providers = load(os.path.join(stack_dir, "providers.tfcomponent.hcl"))
    deploy = load(os.path.join(stack_dir, deploy_file))
    common_path = os.path.join(stack_dir, "common.tfdeploy.hcl")
    common = load(common_path) if os.path.exists(common_path) else {}
    locals_path = os.path.join(stack_dir, "locals.tfcomponent.hcl")
    stack_locals = []
    if os.path.exists(locals_path):
        stack_locals = load(locals_path).get("locals", [])
    return comp, providers, deploy, common, stack_locals


def components_list(comp_blocks):
    """[(name, body), ...] preserving order."""
    out = []
    for block in comp_blocks:
        for name, body in block.items():
            if name.startswith("__"):
                continue
            out.append((H.unquote_key(name), body))
    return out


def get_deployment(deploy):
    """Return (deployment_name, body) of the first deployment block."""
    for block in deploy.get("deployment", []):
        for name, body in block.items():
            if name.startswith("__"):
                continue
            return H.unquote_key(name), body
    raise SystemExit("no deployment block found")


# --------------------------------------------------------------------------
# literal evaluation + naming
# --------------------------------------------------------------------------
def pyval(v):
    """Convert a python-hcl2 *literal* value into a native python value
    (used for spoke maps that drive for_each). Expressions are left as-is."""
    if isinstance(v, dict):
        out = {}
        for k, val in v.items():
            if k in ("__is_block__", "__comments__", "__inline_comments__"):
                continue
            out[H.unquote_key(k)] = pyval(val)
        return out
    if isinstance(v, list):
        return [pyval(x) for x in v]
    if isinstance(v, str):
        if H.is_string_literal(v):
            return v[1:-1]
        return v
    return v


def safe(key: str) -> str:
    """'vpc-p1:nonprod' -> 'vpc_p1_nonprod' (valid unit/component-instance name)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", key)


def hcl_literal(v):
    """python value -> HCL literal string (JSON is valid HCL for scalars/list/obj)."""
    return json.dumps(v)


def py_to_hcl(v):
    """Render a native python value as an HCL expression (object keys use '=')."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if v is None:
        return "null"
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(py_to_hcl(x) for x in v) + "]"
    if isinstance(v, dict):
        parts = []
        for k, val in v.items():
            key = k if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(k)) else json.dumps(k)
            parts.append(f"{key} = {py_to_hcl(val)}")
        return "{ " + ", ".join(parts) + " }"
    return json.dumps(v)


# --------------------------------------------------------------------------
# instance expansion (for_each)
# --------------------------------------------------------------------------
class Instance:
    def __init__(self, comp_name, unit_name, body, each_key=None, each_value=None):
        self.comp = comp_name
        self.unit = unit_name
        self.body = body
        self.each_key = each_key
        self.each_value = each_value


def foreach_collection(body):
    """Return the name of the deployment var driving for_each, or None."""
    fe = body.get("for_each")
    if not fe:
        return None
    inner = H._unwrap_interpolation(fe) if isinstance(fe, str) else None
    if inner and inner.startswith("var."):
        return inner[len("var."):]
    return None


def expand_component(name, body, dep_inputs):
    """Yield Instance objects for a component (1 if no for_each, else N)."""
    coll_name = foreach_collection(body)
    if not coll_name:
        yield Instance(name, name, body)
        return
    for k, v in dep_inputs.get(coll_name, {}).items():
        yield Instance(name, f"{name}__{safe(k)}", body, each_key=k, each_value=v)


def substitute_each(expr, each_key, each_value):
    """Replace each.key / each.value[.attr] with concrete literals for an instance."""
    if each_key is None:
        return expr
    if isinstance(each_value, dict):
        for attr, val in each_value.items():
            expr = re.sub(r"\beach\.value\." + re.escape(attr) + r"\b",
                          hcl_literal(val), expr)
    expr = re.sub(r"\beach\.value\b", hcl_literal(each_value), expr)
    expr = re.sub(r"\beach\.key\b", hcl_literal(each_key), expr)
    return expr


# --------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------
def rel_source(unit_dir, stack_dir, source):
    """Relative path from a generated unit dir to the component's module source."""
    src = source.strip().strip('"')
    if src.startswith("./"):
        src = src[2:]
    target = os.path.normpath(os.path.join(stack_dir, src))
    return os.path.relpath(target, unit_dir)


def resolve_static_account(idx_expr, dep_inputs):
    """Resolve provider.aws.spoke[var.spoke_vpcs[var.NAME].account] to an account key."""
    m = re.match(r"var\.spoke_vpcs\[var\.(\w+)\]\.account", idx_expr)
    if m:
        vpc_key = dep_inputs.get(m.group(1))
        return dep_inputs.get("spoke_vpcs", {}).get(vpc_key, {}).get("account")
    return None
