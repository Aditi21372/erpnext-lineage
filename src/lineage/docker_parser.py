"""frappe_docker adapter: parse a frappe_docker checkout into lineage nodes/edges.

Understands compose files, Dockerfiles/Containerfiles, docker-bake targets,
example.env, MAINTAINERS.md and the scenario matrix in tests/. Emits services,
images, build stages, volumes, networks, ports, env vars, secrets, jobs and the
build/runtime edges between them. All derivations that are not literally
written in a file are marked confidence="inferred".

This module is repo-specific by design; the graph model it fills in lives in
core.py (Builder) and the schema is documented in SCHEMA.md.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from .core import Builder

# ------------------------------------------------------------------ tags

class _Loader(yaml.SafeLoader):
    pass


def _reset_constructor(loader, node):  # docker compose `!reset` tag
    return None


_Loader.add_constructor("!reset", _reset_constructor)
_Loader.add_constructor("!override", _reset_constructor)

RE_SUB = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::[-?]([^}]*))?\}")
SECRET_RE = re.compile(r"(PASSWORD|SECRET|TOKEN|API[_-]?KEY|CREDENTIAL)", re.I)
WEAK_VALUES = {"admin", "123", "1234", "changeit", "password", "pass", "root",
               "test", "admin123", "changeme"}
SENSITIVE_VOLUMES = {
    "sites": ("likely", ["personal", "files", "backups", "config"]),
    "db-data": ("likely", ["personal", "financial"]),
    "redis-queue-data": ("possible", ["transient", "personal"]),
    "logs": ("possible", ["activity", "metadata"]),
}
GROUP_FOR_SERVICE = {
    "frontend": "ingress", "proxy": "ingress", "traefik": "ingress",
    "nginx-proxy": "ingress", "custom-domain": "ingress",
    "db": "data", "database": "data", "redis-cache": "data", "redis-queue": "data",
    "configurator": "config", "create-site": "ops", "migrator": "ops",
    "cron": "ops", "scheduler": "compute",
}


def resolve(s: str) -> str:
    """Resolve ${VAR:-default} / ${VAR:?msg} / ${VAR} to best-known literal."""
    s2 = str(s).replace("$$", "\x00")
    def repl(m):
        return m.group(2) if m.group(2) is not None else m.group(1)
    out = RE_SUB.sub(repl, s2)
    return out.replace("\x00", "$")


def substitute_args(s: str, args: dict[str, str]) -> str:
    """Replace ${ARG} references with collected Dockerfile ARG defaults."""
    for k, v in args.items():
        s = s.replace("${" + k + "}", v)
    return s


def split_port_mapping(s: str) -> list[str]:
    """Split 'host:container' on colons outside ${...} groups."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in s:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        if ch == ":" and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def env_origin(value) -> tuple[str, str | None]:
    if value is None:
        return "empty", None
    v = str(value)
    m = re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}", v)
    if m:
        return "default", m.group(1)
    m = re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*:\?[^}]*\}", v)
    if m:
        return "required", None
    m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", v)
    if m:
        return "varref", None
    return "literal", v


def mask(v: str) -> str:
    v = str(v)
    if len(v) <= 2:
        return "*" * len(v)
    return v[0] + "*" * (len(v) - 2) + v[-1]


# ------------------------------------------------------------------ compose

def load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.load(fh, Loader=_Loader) or {}


def as_dict_of_str(d) -> dict:
    """environment/labels may be list of 'K=V' or dict."""
    if isinstance(d, dict):
        return {str(k): (None if v is None else str(v)) for k, v in d.items()}
    out = {}
    for item in d or []:
        s = str(item)
        k, _, v = s.partition("=")
        out[k] = v or None
    return out


def service_ports(service: dict) -> list[str]:
    out = []
    for p in service.get("ports") or []:
        if isinstance(p, dict):
            out.append(f"{p.get('published')}:{p.get('target')}")
        else:
            out.append(str(p))
    return out


def parse_compose(b: Builder, rel: str, known_services: set[str] | None = None) -> dict:
    doc = load_yaml(b.repo / rel)
    if not doc:
        return {}
    file_services: list[str] = []
    service_names = set((doc.get("services") or {}).keys())
    container_aliases = {}
    for sname, svc in (doc.get("services") or {}).items():
        if isinstance(svc, dict) and svc.get("container_name"):
            container_aliases[str(svc["container_name"])] = sname
    alias_lookup = {**{n: n for n in service_names}, **container_aliases}

    # networks section (external flags resolved per-file)
    net_defs = doc.get("networks") or {}

    for sname, svc in (doc.get("services") or {}).items():
        if not isinstance(svc, dict):
            continue
        file_services.append(sname)
        group = GROUP_FOR_SERVICE.get(sname, "compute")
        if any(x in sname for x in ("proxy", "traefik", "caddy")):
            group = "ingress"
        if any(x in sname for x in ("redis",)) or sname in ("db", "database"):
            group = "data"
        snode_id = f"service:{sname}"
        b.node(snode_id, "service", name=sname, group=group,
               image=str(svc.get("image", "")) or None,
               restart=str(svc.get("restart", "")) or None)
        b.attach(snode_id, b.ev(rel, f"  {sname}:"), rel)

        # image
        img = svc.get("image")
        if img:
            img_r = resolve(str(img))
            repo, _, tag = img_r.rpartition(":")
            if "/" in tag:
                repo, tag = img_r, ""
            inode = b.node(f"image:{repo}", "image", name=repo, group="build",
                           repo=repo, tags=[tag] if tag else [], boundary=True)
            if tag and tag not in inode["props"].get("tags", []):
                inode["props"].setdefault("tags", []).append(tag)
            b.attach(inode, b.ev(rel, str(img)), rel, boundary=True,
                     confidence="parsed")
            e = b.edge("uses_image", snode_id, inode["id"], b.ev(rel, str(img)),
                       label=img_r)
            b.attach_edge(e, rel)

        # env
        env = as_dict_of_str(svc.get("environment"))
        for key, val in env.items():
            origin, dflt = env_origin(val)
            pii = key.endswith("_EMAIL") or key in ("EMAIL", "USERNAME", "PHONE")
            is_secret = bool(SECRET_RE.search(key))
            env_ev = b.ev(rel, f"{key}:", redact=is_secret)
            enode = b.node(f"env:{key}", "env_var", name=key, group="config",
                           origin=origin,
                           categories=["contact"] if pii else [])
            b.attach(enode, env_ev, rel)
            e = b.edge("configures", enode["id"], snode_id, env_ev,
                       label=key,
                       value_ref=None if is_secret else (
                           val if val is None or "${" in str(val) else None))
            b.attach_edge(e, rel)
            if is_secret:
                kind = {"literal": "env_literal", "default": "env_default"}.get(origin, "env_var")
                candidate = dflt if origin == "default" else (str(val) if origin == "literal" else None)
                weak = bool(candidate and candidate.lower() in WEAK_VALUES)
                sid = f"secret:{key}"
                b.node(sid, "secret", name=key, group="config", kind=kind,
                       weak=weak, masked=mask(candidate) if candidate else None,
                       boundary=True)
                b.attach(sid, env_ev, rel, boundary=True)
                e = b.edge("uses_secret", snode_id, sid, env_ev, label=key)
                b.attach_edge(e, rel)

        # command / entrypoint (string or list)
        def flat(x):
            if isinstance(x, list):
                return " ".join(str(i) for i in x)
            return str(x) if x is not None else ""

        cmd = flat(svc.get("command"))
        entry = flat(svc.get("entrypoint"))
        full = f"{entry} {cmd}".strip()
        if full:
            b.nodes[snode_id]["props"]["command"] = full[:500]

        # secrets (docker secrets)
        for s in svc.get("secrets") or []:
            sec_name = s if isinstance(s, str) else str(s.get("source"))
            secrets_top = doc.get("secrets") or {}
            spec = secrets_top.get(sec_name) or {}
            if isinstance(spec, str):
                spec = {"file": spec}
            file_val = spec.get("file")
            sid = f"secret:{sec_name}"
            b.node(sid, "secret", name=sec_name, group="config",
                   kind="docker_secret", weak=False, file=file_val, boundary=True)
            b.attach(sid, b.ev(rel, sec_name, redact=True), rel, boundary=True)
            e = b.edge("uses_secret", snode_id, sid, b.ev(rel, sec_name, redact=True),
                       label=sec_name)
            b.attach_edge(e, rel)

        # command password flags -> weak secret (evidence: the flag itself)
        for m in re.finditer(r"--([A-Za-z0-9-]*password[A-Za-z0-9-]*)=([^\s;'\"\\]+)", full):
            flag, value = m.group(1), m.group(2)
            flag_ev = b.ev(rel, f"--{flag}=", redact=True)
            sid = f"secret:{sname}:--{flag}"
            b.node(sid, "secret", name=f"--{flag}", group="config",
                   kind="cli_flag", weak=value.lower() in WEAK_VALUES,
                   masked=mask(value), boundary=True)
            b.attach(sid, flag_ev, rel, boundary=True)
            e = b.edge("uses_secret", snode_id, sid, flag_ev, label=flag)
            b.attach_edge(e, rel)

        # depends_on (dict or list)
        dep = svc.get("depends_on") or {}
        dep_items = dep.items() if isinstance(dep, dict) else [(d, {}) for d in dep]
        for target, spec in dep_items:
            # targets may live in sibling compose files of the same scenario
            # (e.g. migrator depends_on configurator from overrides/), so
            # resolve against every service name seen across compose files.
            if target not in (known_services or service_names):
                continue
            cond = (spec or {}).get("condition", "service_started") if isinstance(spec, dict) else "service_started"
            e = b.edge("depends_on", snode_id, f"service:{target}",
                       b.ev(rel, f"  {sname}:"), label=cond, condition=cond)
            b.attach_edge(e, rel)

        # volumes / mounts
        for v in svc.get("volumes") or []:
            if isinstance(v, str):
                parts = v.split(":")
                if len(parts) < 2:
                    continue
                src, dst = parts[0], parts[1]
                mode = parts[2] if len(parts) > 2 else "rw"
            else:
                src = str(v.get("source", ""))
                dst = str(v.get("target", ""))
                mode = "rw"
                if v.get("read_only"):
                    mode = "ro"
                if len(v) == 0 or not src:
                    continue
            bind = src.startswith("/") or src.startswith(".")
            vid = f"volume:{src}"
            if bind:
                b.node(vid, "volume", name=src, group="storage", kind="bind",
                       device=src, boundary=True, sensitivity="none")
            else:
                sens, cats = SENSITIVE_VOLUMES.get(src, ("none", []))
                b.node(vid, "volume", name=src, group="storage", kind="named",
                       sensitivity=sens, categories=cats)
            b.attach(vid, b.ev(rel, src), rel,
                     sensitivity=SENSITIVE_VOLUMES.get(src, ("none", []))[0] if not bind else "none",
                     boundary=bind)
            e = b.edge("mounts", snode_id, vid, b.ev(rel, src),
                       label=f"{mode} {dst}", path=dst, mode=mode, bind=bind,
                       host_path=src if bind else None, boundary=bind)
            b.attach_edge(e, rel)
            # data-flow derivation
            if not bind:
                flow_conf = "inferred"
                if src == "sites" and mode == "rw":
                    b.edge("reads_from", snode_id, vid, b.ev(rel, src), confidence=flow_conf, label="reads sites")
                    b.edge("writes_to", snode_id, vid, b.ev(rel, src), confidence=flow_conf, label="writes sites")
                elif src in ("db-data", "redis-queue-data"):
                    b.edge("reads_from", snode_id, vid, b.ev(rel, src), confidence=flow_conf, label=f"reads {src}")
                    b.edge("writes_to", snode_id, vid, b.ev(rel, src), confidence=flow_conf, label=f"writes {src}")
                elif src == "logs" and mode == "rw":
                    b.edge("writes_to", snode_id, vid, b.ev(rel, src), confidence=flow_conf, label="writes logs")
                elif mode == "ro":
                    b.edge("reads_from", snode_id, vid, b.ev(rel, src), confidence=flow_conf, label=f"reads {src}")

        # networks
        nets = svc.get("networks")
        if isinstance(nets, dict):
            net_names = list(nets.keys())
        elif isinstance(nets, list):
            net_names = [str(x) for x in nets]
        else:
            net_names = []
        for net in net_names:
            spec = net_defs.get(net) or {}
            external = bool(spec.get("external")) if isinstance(spec, dict) else False
            b.node(f"network:{net}", "network", name=net, group="network",
                   external=external, driver=(spec.get("driver") if isinstance(spec, dict) else None))
            b.attach(f"network:{net}", b.ev(rel, net), rel)
            e = b.edge("joins_network", snode_id, f"network:{net}", b.ev(rel, net),
                       label=net, external=external)
            b.attach_edge(e, rel)
    # networks declared but unused (e.g. standalone traefik)
    for net, spec in net_defs.items():
        external = bool((spec or {}).get("external")) if isinstance(spec, dict) else False
        b.node(f"network:{net}", "network", name=net, group="network", external=external)
        b.attach(f"network:{net}", b.ev(rel, net), rel)

    # ports (published)
    for sname, svc in (doc.get("services") or {}).items():
        if not isinstance(svc, dict):
            continue
        for raw in service_ports(svc):
            parts = split_port_mapping(raw)
            if len(parts) == 2:
                host_raw, cont = parts
            elif len(parts) == 3:
                host_raw, cont = parts[1], parts[2]
            else:
                continue
            host = resolve(host_raw)
            cont = resolve(cont)
            if not cont.isdigit():
                continue
            pid = f"port:{sname}:{cont}"
            b.node(pid, "port", name=f"{cont}", group="ingress",
                   container_port=int(cont), host_port=host if host.isdigit() else None,
                   protocol="tcp", published=True, boundary=True)
            b.attach(pid, b.ev(rel, raw), rel, boundary=True)
            e = b.edge("publishes", f"service:{sname}", pid, b.ev(rel, raw),
                       label=raw, host_port=host if host.isdigit() else None,
                       boundary=True)
            b.attach_edge(e, rel)

    # top-level volume defs: bind-mount overrides cross the host boundary
    for vname, vspec in (doc.get("volumes") or {}).items():
        if not isinstance(vspec, dict):
            continue
        dopts = vspec.get("driver_opts") or {}
        if str(dopts.get("type", "")) == "none" and str(dopts.get("o", "")) == "bind":
            vid = f"volume:{vname}"
            b.node(vid, "volume", name=vname, group="storage", kind="bind",
                   device=str(dopts.get("device", "")), boundary=True)
            b.attach(vid, b.ev(rel, "device:"), rel, boundary=True)

    # ofelia scheduled jobs (labels dict or list)
    for sname, svc in (doc.get("services") or {}).items():
        labels = as_dict_of_str(svc.get("labels")) if isinstance(svc, dict) else {}
        jobs: dict[str, dict] = {}
        for key, val in labels.items():
            m = re.match(r"ofelia\.job-exec\.([^.]+)\.(schedule|command|user)", key)
            if m:
                jobs.setdefault(m.group(1), {})[m.group(2)] = val or ""
        for job_name, spec in jobs.items():
            jid = f"job:{job_name}"
            schedule = resolve(spec.get("schedule", ""))
            command = resolve(spec.get("command", ""))
            b.node(jid, "job", name=job_name, group="ops",
                   schedule=schedule, command=command, user=spec.get("user", ""),
                   boundary=True)
            b.attach(jid, b.ev(rel, job_name), rel, boundary=True)
            e = b.edge("executes", jid, f"service:{sname}", b.ev(rel, job_name),
                       label=command)
            b.attach_edge(e, rel)
            if "backup" in command:
                e = b.edge("backs_up", jid, "volume:sites", b.ev(rel, command),
                           label=command, boundary=True)
                b.attach_edge(e, rel)
                if "volume:sites" not in b.nodes:
                    b.node("volume:sites", "volume", name="sites", group="storage",
                           kind="named", sensitivity="likely",
                           categories=["personal", "backups"], boundary=False)

    return {"services": file_services}


def derive_connections(b: Builder, rel: str, doc: dict):
    """connects_to / proxies_to / exposes derived from env values, commands, scripts."""
    services = doc.get("services") or {}
    names = {n: n for n in services if isinstance(services[n], dict)}
    for sname, svc in services.items():
        if not isinstance(svc, dict):
            continue
        env = as_dict_of_str(svc.get("environment"))
        def flat(x):
            return " ".join(str(i) for i in x) if isinstance(x, list) else (str(x) if x else "")
        full = f"{flat(svc.get('entrypoint'))} {flat(svc.get('command'))}"
        # env host references
        for key, val in env.items():
            if not val:
                continue
            for hm in re.finditer(r"(?:^|[/@:=\"'])([a-z][a-z0-9_-]*)(?::(\d+))?", val):
                host, port = hm.group(1), hm.group(2)
                if host in names and host != sname:
                    e = b.edge("connects_to", f"service:{sname}", f"service:{host}",
                               b.ev(rel, f"{key}:"), confidence="inferred",
                               label=f"{key}={val}", via=key, port=port)
                    b.attach_edge(e, rel)
        # command host references: wait-for-it host:port, redis://host:port
        for cm in re.finditer(r"wait-for-it(?:\s+-t\s+\d+)?\s+([a-z][a-z0-9_-]*):(\d+)", full):
            host, port = cm.group(1), cm.group(2)
            if host in names and host != sname:
                e = b.edge("connects_to", f"service:{sname}", f"service:{host}",
                           b.ev(rel, f"{host}:{port}"), confidence="inferred",
                           label=f"wait-for-it {host}:{port}", via="wait-for-it",
                           port=port)
                b.attach_edge(e, rel)
        for cm in re.finditer(r"redis://([a-z][a-z0-9_-]*):(\d+)", full):
            host, port = cm.group(1), cm.group(2)
            if host in names and host != sname:
                e = b.edge("connects_to", f"service:{sname}", f"service:{host}",
                           b.ev(rel, f"redis://{host}"), confidence="inferred",
                           label=f"redis://{host}:{port}", via="redis", port=port)
                b.attach_edge(e, rel)

        # nginx frontend proxies
        if env.get("BACKEND"):
            host = resolve(env["BACKEND"]).split(":")[0]
            if host in names:
                e = b.edge("proxies_to", f"service:{sname}", f"service:{host}",
                           b.ev(rel, "BACKEND:"), confidence="inferred",
                           label=f"http -> {host}", kind="http")
                b.attach_edge(e, rel)
        if env.get("SOCKETIO"):
            host = resolve(env["SOCKETIO"]).split(":")[0]
            if host in names:
                e = b.edge("proxies_to", f"service:{sname}", f"service:{host}",
                           b.ev(rel, "SOCKETIO:"), confidence="inferred",
                           label=f"socketio -> {host}", kind="socketio")
                b.attach_edge(e, rel)
        # caddy custom-domain -> frontend
        if "custom-domain" in sname and "frontend:" in full:
            e = b.edge("proxies_to", f"service:{sname}", "service:frontend",
                       b.ev(rel, "frontend:"), confidence="inferred",
                       label="caddy reverse-proxy", kind="caddy")
            b.attach_edge(e, rel)

    # load-balancer labels on frontend -> proxy-ish services in same file
    frontend = services.get("frontend")
    if isinstance(frontend, dict):
        labels = as_dict_of_str(frontend.get("labels"))
        if any("loadbalancer" in k or k == "VIRTUAL_HOST" for k in labels):
            for sname, svc in services.items():
                if not isinstance(svc, dict) or sname == "frontend":
                    continue
                img = resolve(str(svc.get("image", "")))
                if any(x in img for x in ("traefik", "nginx-proxy", "caddy")):
                    e = b.edge("proxies_to", f"service:{sname}", "service:frontend",
                               b.ev(rel, "loadbalancer"), confidence="inferred",
                               label="load-balances frontend", kind="lb")
                    b.attach_edge(e, rel)


def add_internal_exposes(b: Builder):
    """Known internal ports from scripts/images (start.sh, nginx, socketio)."""
    known = {
        "backend": (8000, "resources/core/start.sh", "--bind=0.0.0.0:8000"),
        "websocket": (9000, "pwd.yml", "SOCKETIO_PORT"),
        "frontend": (8080, "resources/core/nginx/nginx-template.conf", "listen ${NGINX_LISTEN_PORT}"),
        "db": (3306, "overrides/compose.mariadb.yaml", "3306"),
        "redis-cache": (6379, "overrides/compose.redis.yaml", "redis-cache:6379"),
        "redis-queue": (6379, "overrides/compose.redis.yaml", "redis-queue:6379"),
        "custom-domain": (2016, "overrides/compose.custom-domain.yaml", "2016"),
    }
    for svc, (port, evfile, needle) in known.items():
        sid = f"service:{svc}"
        if sid not in b.nodes:
            continue
        pid = f"port:{svc}:{port}"
        if pid not in b.nodes:
            b.node(pid, "port", name=str(port), group="ingress",
                   container_port=port, host_port=None, protocol="tcp",
                   published=False, boundary=False)
            b.attach(pid, b.ev(evfile, needle), evfile, confidence="inferred")
            e = b.edge("exposes", sid, pid, b.ev(evfile, needle),
                       confidence="inferred", label=f":{port}")
            b.attach_edge(e, evfile)
    # postgres scenario exposes 5432 in addition to the mariadb default
    if "service:db" in b.nodes and any(
            "compose.postgres.yaml" in f for f in b.nodes["service:db"]["files"]):
        pid = "port:db:5432"
        if pid not in b.nodes:
            evfile, needle = "overrides/compose.postgres.yaml", "5432"
            b.node(pid, "port", name="5432", group="ingress",
                   container_port=5432, host_port=None, protocol="tcp",
                   published=False, boundary=False)
            b.attach(pid, b.ev(evfile, needle), evfile, confidence="inferred")
            e = b.edge("exposes", "service:db", pid, b.ev(evfile, needle),
                       confidence="inferred", label=":5432")
            b.attach_edge(e, evfile)


# ------------------------------------------------------------------ containerfiles

def parse_containerfile(b: Builder, rel: str):
    text = b.text(rel)
    args: dict[str, str] = {"FRAPPE_IMAGE_PREFIX": "frappe", "FRAPPE_BRANCH": "version-16"}
    stages: list[str] = []
    stage_ids: dict[str, str] = {}
    for raw in text.splitlines():
        line_ = raw.strip()
        if line_.startswith("#"):
            continue
        m = re.match(r"ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:=(.*))?$", line_)
        if m:
            args[m.group(1)] = m.group(2) if m.group(2) is not None else args.get(m.group(1), "")
            continue
        m = re.match(r"FROM\s+(\S+)(?:\s+AS\s+(\S+))?$", line_, re.I)
        if m:
            base_raw, stage = m.group(1), m.group(2) or f"stage{len(stages)}"
            base = resolve(substitute_args(base_raw, args))
            sid = f"build_stage:{rel}:{stage}"
            b.node(sid, "build_stage", name=stage, group="build",
                   dockerfile=rel, stage=stage, base=base)
            b.attach(sid, b.ev(rel, line_), rel)
            stages.append(stage)
            stage_ids[stage] = sid
            if base in stage_ids:
                e = b.edge("builds_from", sid, stage_ids[base], b.ev(rel, line_),
                           label=f"FROM {base}", confidence="parsed")
                b.attach_edge(e, rel)
            else:
                repo, _, tag = base.rpartition(":")
                if "/" in tag:
                    repo, tag = base, ""
                # internal frappe/{base,build,erpnext,bench} images -> bake stages
                aid = f"artifact:{base}" if not base.startswith("frappe/") else None
                if base.startswith("frappe/"):
                    repo_i, _, tag_i = base.rpartition(":")
                    if "/" in tag_i:
                        repo_i, tag_i = base, ""
                    iid = f"image:{repo_i}"
                    b.node(iid, "image", name=repo_i, group="build",
                           repo=repo_i, tags=[tag_i] if tag_i else [], boundary=True)
                    b.attach(iid, b.ev(rel, line_), rel, boundary=True,
                             confidence="inferred")
                    e = b.edge("builds_from", sid, iid, b.ev(rel, line_),
                               label=f"FROM {base}", confidence="inferred")
                else:
                    b.node(aid, "artifact", name=base, group="external",
                           kind="base_image", boundary=True)
                    b.attach(aid, b.ev(rel, line_), rel, boundary=True)
                    e = b.edge("builds_from", sid, aid, b.ev(rel, line_),
                               label=f"FROM {base}", boundary=True)
                b.attach_edge(e, rel)
        # git clones / bench init / bench get-app (scan this line; args collected so far)
        for m in re.finditer(r"git clone\s+(?:--depth \d+\s+)?(\$\{[A-Za-z_]+\}|https?://\S+)", raw):
            ref = m.group(1)
            url = resolve(substitute_args(ref, args))
            url = url.replace("https://", "").replace("http://", "").removesuffix(".git")
            aid = f"artifact:{url}"
            b.node(aid, "artifact", name=url, group="external", kind="git_repo",
                   boundary=True)
            b.attach(aid, b.ev(rel, ref), rel, boundary=True)
            cur_stage = stages[-1] if stages else "?"
            e = b.edge("clones", stage_ids.get(cur_stage, f"build_stage:{rel}:?"), aid,
                       b.ev(rel, ref), label=url, boundary=True)
            b.attach_edge(e, rel)
        for pat, needle in ((r"--frappe-path=(\S+)", "frappe-path"),
                            (r"get-app[^\n]*?(\$\{ERPNEXT_REPO\}|https?://\S+)", "get-app")):
            for m in re.finditer(pat, raw):
                url = resolve(substitute_args(m.group(1), args))
                url = url.replace("https://", "").removesuffix(".git")
                aid = f"artifact:{url}"
                b.node(aid, "artifact", name=url, group="external", kind="git_repo",
                       boundary=True)
                b.attach(aid, b.ev(rel, needle), rel, boundary=True)
                cur_stage = stages[-1] if stages else "?"
                e = b.edge("clones", stage_ids.get(cur_stage, f"build_stage:{rel}:?"), aid,
                           b.ev(rel, needle), label=url, boundary=True)
                b.attach_edge(e, rel)
    # literal github URLs fetched during build (pyenv, nvm, wkhtmltopdf release)
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("#") or "/blob/" in raw_line or "/tree/" in raw_line:
            continue
        for m in re.finditer(r"https?://(?:raw\.)?github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", raw_line):
            slug = f"github.com/{m.group(1).removesuffix('.git')}"
            kind = "release" if "/releases/" in raw_line else "git_repo"
            aid = f"artifact:{slug}"
            b.node(aid, "artifact", name=slug, group="external", kind=kind,
                   boundary=True)
            b.attach(aid, b.ev(rel, m.group(1)), rel, boundary=True)
            cur_stage = stages[-1] if stages else "?"
            e = b.edge("clones", stage_ids.get(cur_stage, f"build_stage:{rel}:?"), aid,
                       b.ev(rel, m.group(1)), label=slug, boundary=True)
            b.attach_edge(e, rel)


def parse_bake(b: Builder, rel: str):
    text = b.text(rel)
    for m in re.finditer(r'target "([A-Za-z0-9_-]+)"\s*\{([^}]*)\}', text, re.S):
        name, body = m.group(1), m.group(2)
        df = re.search(r'dockerfile\s*=\s*"([^"]+)"', body)
        stage = re.search(r'target\s*=\s*"([^"]+)"', body)
        if not df or not stage:
            continue
        stage_id = f"build_stage:{df.group(1)}:{stage.group(1)}"
        if stage_id not in b.nodes:
            continue
        img_repo = f"frappe/{name}"
        img_id = f"image:{img_repo}"
        b.node(img_id, "image", name=img_repo, group="build", repo=img_repo,
               tags=[], boundary=True)
        b.attach(img_id, b.ev(rel, f'target "{name}"'), rel, boundary=True,
                 confidence="inferred")
        e = b.edge("built_from", img_id, stage_id, b.ev(rel, f'target "{name}"'),
                   confidence="inferred", label=f"bake target {name}")
        b.attach_edge(e, rel)


def parse_example_env(b: Builder, rel: str):
    for num, line in enumerate(b.text(rel).splitlines(), start=1):
        m = re.match(r"([A-Z_][A-Z0-9_]*)=", line)
        if not m:
            continue
        key = m.group(1)
        pii = key.endswith("_EMAIL") or key in ("EMAIL", "USERNAME", "PHONE")
        enode = b.node(f"env:{key}", "env_var", name=key, group="config",
                       origin="example", categories=["contact"] if pii else [])
        b.attach(enode, b.ev(rel, key, redact=bool(SECRET_RE.search(key))), rel)


def parse_owners(b: Builder, rel: str):
    if not (b.repo / rel).exists():
        return
    b.node("owner:frappe-maintainers", "owner", name="frappe maintainers", group="ops",
           role="maintainer")
    b.attach("owner:frappe-maintainers", b.ev(rel, "Maintainers"), rel)
    for target in ("README.md", "compose.yaml", "pwd.yml"):
        if (b.repo / target).exists():
            b.node(f"file:{target}", "file", name=target, group="config", runtime=False)
            b.attach(f"file:{target}", b.ev(rel, "Maintainers"), rel)
            e = b.edge("owns", "owner:frappe-maintainers", f"file:{target}",
                       b.ev(rel, "Maintainers"), label="owns")
            b.attach_edge(e, rel)


def link_runtime_files(b: Builder):
    """Configurator writes common_site_config.json into sites; nginx renders frappe.conf."""
    cfg_file = b.node("file:common_site_config.json", "file", name="common_site_config.json",
                      group="config", runtime=True)
    b.attach(cfg_file, b.ev("pwd.yml", "common_site_config.json"), "pwd.yml",
             confidence="inferred")
    if "service:configurator" in b.nodes:
        e = b.edge("writes_config", "service:configurator", "file:common_site_config.json",
                   b.ev("pwd.yml", "common_site_config.json"), confidence="inferred",
                   label="bench set-config", transform_kind="config_mapping")
        b.attach_edge(e, "pwd.yml")
    nginx_file = b.node("file:frappe.conf", "file", name="frappe.conf (rendered nginx)",
                        group="config", runtime=True)
    b.attach(nginx_file, b.ev("resources/core/nginx/nginx-entrypoint.sh", "frappe.conf"),
             "resources/core/nginx/nginx-entrypoint.sh", confidence="inferred")
    if "service:frontend" in b.nodes:
        e = b.edge("writes_config", "service:frontend", "file:frappe.conf",
                   b.ev("resources/core/nginx/nginx-entrypoint.sh", "frappe.conf"),
                   confidence="inferred", label="envsubst template render",
                   transform_kind="template_render")
        b.attach_edge(e, "resources/core/nginx/nginx-entrypoint.sh")


# ------------------------------------------------------------------ scenarios

def load_scenarios(repo: Path) -> dict[str, list[str]]:
    cfg = json.loads((repo / "tests" / "compose-configs.json").read_text(encoding="utf-8"))
    scenarios = {c["name"]: [f.replace(".\\", "").replace("\\", "/") for f in c["files"]]
                 for c in cfg["configurations"]}
    scenarios["pwd"] = ["pwd.yml"]
    return scenarios


def scenario_services(repo: Path, name: str) -> list[str]:
    scenarios = load_scenarios(repo)
    if name not in scenarios:
        return []
    merged: dict[str, dict] = {}
    for rel in scenarios[name]:
        doc = load_yaml(repo / rel)
        for sname, svc in (doc.get("services") or {}).items():
            if sname not in merged:
                merged[sname] = {}
            if isinstance(svc, dict):
                merged[sname].update(svc)
    return sorted(merged)
