#!/usr/bin/env python3
"""
hclexpr.py — Reconstruct HCL expressions from python-hcl2's parsed form and
rewrite HCP Terraform Stacks references into plain Terraform references.

python-hcl2 quirks we reverse here:
  * string literals keep their surrounding double quotes:   '"./ram-share"'
  * bare interpolations are wrapped:                        '${var.env}'
  * template strings keep quotes + interpolation:           '"tgw-${var.env}"'
  * numbers / bools are native python types
  * object/quoted keys may arrive quoted:                   '"aws"'

Reference rewriting (Stacks -> Terraform):
  * component.NAME.OUTPUT   -> var.dep_NAME.OUTPUT   (dependency outputs are
  * component.NAME          -> var.dep_NAME           injected as input variables)
  * var.NAME                -> var.NAME               (unchanged)
  * local.NAME              -> local.NAME             (stack locals ported into unit)
  * each.key / each.value   -> replaced with concrete values by the expander
"""
import re

_COMPONENT_OUT = re.compile(r"\bcomponent\.([A-Za-z_][A-Za-z0-9_]*)\.")
_COMPONENT_BARE = re.compile(r"\bcomponent\.([A-Za-z_][A-Za-z0-9_]*)\b")


def unquote_key(key: str) -> str:
    """'"aws"' -> 'aws'."""
    if len(key) >= 2 and key[0] == '"' and key[-1] == '"':
        return key[1:-1]
    return key


def is_string_literal(s: str) -> bool:
    return len(s) >= 2 and s[0] == '"' and s[-1] == '"'


def _unwrap_interpolation(s: str):
    """If the whole string is a single ${...}, return the inner expr, else None."""
    if not (s.startswith("${") and s.endswith("}")):
        return None
    # Ensure the closing brace matches the opening ${ (single interpolation
    # spanning the whole string), not "${a}-${b}".
    depth = 0
    for i, ch in enumerate(s):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and i != len(s) - 1:
                return None  # closed early -> multiple segments
    return s[2:-1]


def rewrite_refs(expr: str) -> str:
    """Rewrite Stacks component references to injected input variables.

    component.X.Y -> var.dep_X.Y ; bare component.X -> var.dep_X.
    var.X and local.X are kept as-is.
    """
    expr = _COMPONENT_OUT.sub(r"var.dep_\1.", expr)
    expr = _COMPONENT_BARE.sub(r"var.dep_\1", expr)
    return expr


def to_hcl(value, rewrite: bool = True) -> str:
    """Convert a python-hcl2 parsed value into an HCL expression string."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "null"
    if isinstance(value, list):
        items = ", ".join(to_hcl(v, rewrite) for v in value)
        return f"[{items}]"
    if isinstance(value, dict):
        parts = []
        for k, v in value.items():
            if k in ("__is_block__", "__comments__", "__inline_comments__"):
                continue
            key = unquote_key(k)
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                key = f'"{key}"'
            parts.append(f"{key} = {to_hcl(v, rewrite)}")
        return "{ " + ", ".join(parts) + " }"
    if isinstance(value, str):
        inner = _unwrap_interpolation(value)
        if inner is not None:
            return rewrite_refs(inner) if rewrite else inner
        if is_string_literal(value):
            # template string with possible ${...} inside quotes
            if rewrite:
                body = value[1:-1]
                body = re.sub(
                    r"\$\{([^}]*)\}",
                    lambda m: "${" + rewrite_refs(m.group(1)) + "}",
                    body,
                )
                return f'"{body}"'
            return value
        # bare identifier / fallback
        return rewrite_refs(value) if rewrite else value
    raise TypeError(f"unhandled type {type(value)}: {value!r}")


def component_deps(body: dict):
    """Return set of component names this component depends on (depends_on + refs)."""
    deps = set()
    for d in body.get("depends_on", []) or []:
        inner = _unwrap_interpolation(d) if isinstance(d, str) else None
        if inner:
            m = _COMPONENT_BARE.match(inner.strip())
            if m:
                deps.add(m.group(1))
    # also scan inputs for component.X references
    blob = _stringify(body.get("inputs", {}))
    for m in _COMPONENT_OUT.finditer(blob):
        deps.add(m.group(1))
    for m in _COMPONENT_BARE.finditer(blob):
        deps.add(m.group(1))
    return deps


def _stringify(v) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return " ".join(_stringify(x) for x in v)
    if isinstance(v, dict):
        return " ".join(_stringify(x) for x in v.values())
    return str(v)


# --------------------------------------------------------------------------
if __name__ == "__main__":
    tests = [
        ('"./ram-share"', '"./ram-share"'),
        ("${var.env}", "var.env"),
        ('"tgw-${var.env}"', '"tgw-${var.env}"'),
        ("${component.ram.resource_share_arn}", "var.dep_ram.resource_share_arn"),
        ("${component.transit_gateway.transit_gateway_id}", "var.dep_transit_gateway.transit_gateway_id"),
        ("${local.ingress_public_subnet_cidrs}", "local.ingress_public_subnet_cidrs"),
        (64512, "64512"),
        (True, "true"),
        ('"enable"', '"enable"'),
        ("${split(\":\", each.key)[0]}", 'split(":", each.key)[0]'),
        ("${[for k, v in component.vpc_spoke : v.vpc_id]}", "[for k, v in var.dep_vpc_spoke : v.vpc_id]"),
        ("${component.vpc_spoke[each.key].vpc_id}", "var.dep_vpc_spoke[each.key].vpc_id"),
        ("${values(component.r53_zones.private_zone_ids)}", "values(var.dep_r53_zones.private_zone_ids)"),
    ]
    ok = True
    for src, want in tests:
        got = to_hcl(src)
        status = "OK " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"[{status}] {src!r}\n       -> {got!r}\n       want {want!r}")

    print("\nALL PASS" if ok else "\nSOME FAILED")
