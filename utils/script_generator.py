"""
Locust script generator with dependency chaining:
  extract: pull values from a response (JSON field or header)
  inject:  push stored values into the next request (header/body/path/query)
"""

import json
import re
from models import TestConfig, ApiEndpoint, HttpMethod


def _safe_name(s: str) -> str:
    name = s.lower().strip()
    name = re.sub(r"[^a-z0-9]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "endpoint"


def _jpath_to_py(path: str) -> str:
    """Convert simple JSONPath $.a.b.c → resp.json()["a"]["b"]["c"]"""
    path = path.strip()
    if path.startswith("$."):
        parts = path[2:].split(".")
        chain = "".join(f'["{p}"]' for p in parts)
        return f'resp.json(){chain}'
    return f'resp.json()["{path}"]'


def _render_task(ep: ApiEndpoint) -> str:
    method   = ep.method.value.lower()
    fn_name  = _safe_name(ep.name)
    extract  = ep.extract or []
    inject   = ep.inject  or []

    lines = []
    lines.append(f"    @task({ep.weight})")
    lines.append(f"    def {fn_name}(self):")

    # --- headers ---
    static_hdrs    = dict(ep.headers or {})
    header_injects = [x for x in inject if x.into == "header"]
    lines.append(f"        _headers = {json.dumps(static_hdrs)}")
    for inj in header_injects:
        key = inj.key or inj.var
        lines.append(f'        if getattr(self, "{inj.var}", None) is not None:')
        lines.append(f'            _headers["{key}"] = self.{inj.var}')

    # --- path ---
    path_injects = [x for x in inject if x.into == "path"]
    if path_injects:
        lines.append(f'        _path = "{ep.path}"')
        for inj in path_injects:
            placeholder = inj.key or "{" + inj.var + "}"
            lines.append(f'        if getattr(self, "{inj.var}", None) is not None:')
            lines.append(f'            _path = _path.replace("{placeholder}", str(self.{inj.var}))')
        path_expr = "_path"
    else:
        path_expr = f'"{ep.path}"'

    # --- body ---
    body_injects = [x for x in inject if x.into == "body"]
    static_body  = ep.body or {}
    if static_body or body_injects:
        lines.append(f"        _body = {json.dumps(static_body)}")
        for inj in body_injects:
            key = inj.key or inj.var
            lines.append(f'        if getattr(self, "{inj.var}", None) is not None:')
            lines.append(f'            _body["{key}"] = self.{inj.var}')
        body_expr = "_body"
    else:
        body_expr = None

    # --- query params ---
    query_injects = [x for x in inject if x.into == "query"]
    if query_injects:
        lines.append("        _params = {}")
        for inj in query_injects:
            key = inj.key or inj.var
            lines.append(f'        if getattr(self, "{inj.var}", None) is not None:')
            lines.append(f'            _params["{key}"] = self.{inj.var}')
        params_expr = "_params"
    else:
        params_expr = None

    # --- request call ---
    call_kwargs = [f'name="{ep.name}"', "headers=_headers", f"timeout={ep.timeout}"]
    if ep.method in (HttpMethod.POST, HttpMethod.PUT, HttpMethod.PATCH):
        call_kwargs.append(f"json={body_expr}" if body_expr else "json=None")
    if params_expr:
        call_kwargs.append(f"params={params_expr}")

    kw_str = ", ".join(call_kwargs)
    lines.append(f"        with self.client.{method}({path_expr}, {kw_str}) as resp:")
    lines.append(f"            if resp.status_code >= 400:")
    lines.append(f"                try:")
    lines.append(f"                    _body_preview = resp.text[:2000]")
    lines.append(f"                except Exception:")
    lines.append(f"                    _body_preview = ''")
    lines.append(f"                _fail_msg = f\"Got status {{resp.status_code}}: {{_body_preview}}\" if _body_preview else f\"Got status {{resp.status_code}}\"")
    lines.append(f"                resp.failure(_fail_msg)")

    # --- extract ---
    if extract:
        lines.append("            else:")
        lines.append("                try:")
        for ext in extract:
            src = ext.from_
            if src == "json":
                py_expr = _jpath_to_py(ext.path)
                lines.append(f"                    self.{ext.var} = {py_expr}")
            elif src == "header":
                lines.append(f'                    self.{ext.var} = resp.headers.get("{ext.path}")')
        lines.append("                except Exception:")
        # Reset all extracted vars on failure, not just the last one
        for ext in extract:
            lines.append(f"                    self.{ext.var} = None")

    lines.append("")
    return "\n".join(lines)


def generate_locust_script(config: TestConfig) -> str:
    # Collect all extracted variable names for on_start init
    all_vars: set[str] = set()
    for ep in config.endpoints:
        for ext in (ep.extract or []):
            all_vars.add(ext.var)

    on_start = ""
    if all_vars:
        inits = "\n".join(f"        self.{v} = None" for v in sorted(all_vars))
        on_start = f"    def on_start(self):\n{inits}\n\n"

    tasks = "\n\n".join(_render_task(ep) for ep in config.endpoints)

    return (
        "# Auto-generated Locust script\n"
        "# Generated by LocustForge API Testing Tool\n\n"
        "from locust import HttpUser, task, between\n"
        "import json\n\n\n"
        "class LoadTestUser(HttpUser):\n"
        f'    host = "{config.base_url}"\n'
        f"    wait_time = between({config.think_time_min}, {config.think_time_max})\n\n"
        f"{on_start}"
        f"{tasks}\n"
    )
