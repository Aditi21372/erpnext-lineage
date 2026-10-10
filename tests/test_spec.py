"""Spec tests for the lineage graph.

Run all:        lineage test          (or: python tests/test_spec.py)
Run one phase:  lineage test --phase p1
Run by name:    lineage test --only sites

These tests are the specification: they were written before the parser existed.
Golden facts come from manual reading of frappe_docker / erpnext
(counts, file:line refs) and are intentionally brittle: a source pin change
or a parser regression turns them red.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lineage import agent
from lineage import build as build_mod
from lineage import docker_parser
from lineage import dummy as dummy_data
from lineage import paths
from lineage import render
from lineage import tools as tools_pkg
from lineage import viewer as view
from lineage import views as slices

REPO = paths.frappe_docker_repo()

_cache: dict = {}


# ---------------------------------------------------------------- helpers

def graph() -> dict:
    if "graph" not in _cache:
        _cache["graph"] = build_mod.build_graph(REPO)
    return _cache["graph"]


def tools():
    return tools_pkg


def nodes(g: dict, type_: str | None = None):
    return [n for n in g["nodes"] if type_ is None or n["type"] == type_]


def node(g: dict, id_: str) -> dict:
    for n in g["nodes"]:
        if n["id"] == id_:
            return n
    raise AssertionError(f"node not found: {id_}")


def edges(g: dict, type_: str, src: str | None = None, dst: str | None = None):
    out = []
    for e in g["edges"]:
        if e["type"] != type_:
            continue
        if src is not None and e["src"] != src:
            continue
        if dst is not None and e["dst"] != dst:
            continue
        out.append(e)
    return out


# ---------------------------------------------------------------- P1: parse

def test_p1_graph_builds_with_nodes_edges_meta():
    g = graph()
    assert g["nodes"], "graph has no nodes"
    assert g["edges"], "graph has no edges"
    assert "meta" in g


def test_p1_graph_meta_records_git_commit_and_counts():
    g = graph()
    m = g["meta"]
    for key in ("repo", "git_commit", "generated_at", "node_count", "edge_count"):
        assert key in m, f"meta missing {key}"
    assert m["node_count"] == len(g["nodes"])
    assert m["edge_count"] == len(g["edges"])


def test_p1_schema_is_valid():
    errors = tools().validate_schema(graph())
    assert errors == [], f"schema errors: {errors[:5]}"


def test_p1_pwd_has_eleven_services():
    g = graph()
    pwd = [n for n in nodes(g, "service") if "pwd" in n["scenarios"]]
    assert len(pwd) == 11, sorted(n["name"] for n in pwd)


def test_p1_pwd_sites_mounted_rw_by_eight_services():
    g = graph()
    mounts = [e for e in edges(g, "mounts", dst="volume:sites")
              if e["props"].get("mode") == "rw" and "pwd" in e["scenarios"]]
    assert len(mounts) == 8, [e["src"] for e in mounts]


def test_p1_db_writes_to_db_data_volume():
    g = graph()
    writers = {e["src"] for e in edges(g, "writes_to", dst="volume:db-data")}
    assert "service:db" in writers, writers


def test_p1_frontend_publishes_8080_as_boundary_port():
    g = graph()
    pub = edges(g, "publishes", src="service:frontend")
    ports_8080 = [e for e in pub if e["props"].get("host_port") == "8080"
                  and "pwd" in e["scenarios"]]
    assert len(ports_8080) == 1, pub
    assert ports_8080[0]["boundary"] is True
    pnode = node(g, ports_8080[0]["dst"])
    assert pnode["props"]["published"] is True


def test_p1_four_weak_credentials_in_pwd():
    g = graph()
    weak = [n for n in nodes(g, "secret")
            if n["props"].get("weak") and "pwd.yml" in n["files"]]
    assert len(weak) >= 4, [n["id"] for n in weak]


def test_p1_docker_secret_override_resolves_to_file():
    g = graph()
    sec = node(g, "secret:db_password")
    assert sec["props"]["kind"] == "docker_secret"
    files = {ev["file"] for ev in sec["evidence"]}
    assert "overrides/compose.mariadb-secrets.yaml" in files, files


def test_p1_backend_connects_to_db():
    g = graph()
    assert edges(g, "connects_to", src="service:backend", dst="service:db"), \
        "backend DB_HOST=db not detected"


def test_p1_frontend_connects_to_backend_and_websocket():
    g = graph()
    assert edges(g, "connects_to", src="service:frontend", dst="service:backend")
    assert edges(g, "connects_to", src="service:frontend", dst="service:websocket")


def test_p1_create_site_connects_via_wait_for_it():
    g = graph()
    for target in ("service:db", "service:redis-cache", "service:redis-queue"):
        assert edges(g, "connects_to", src="service:create-site", dst=target), target


def test_p1_nginx_proxies_to_backend_http_and_websocket_socketio():
    g = graph()
    http = edges(g, "proxies_to", src="service:frontend", dst="service:backend")
    sock = edges(g, "proxies_to", src="service:frontend", dst="service:websocket")
    assert any(e["props"].get("kind") == "http" for e in http), http
    assert any(e["props"].get("kind") == "socketio" for e in sock), sock


def test_p1_backup_job_scheduled_and_executes_on_scheduler():
    g = graph()
    job = node(g, "job:datecron")
    assert "6h" in job["props"]["schedule"], job["props"]
    assert "bench --site all backup" in job["props"]["command"]
    assert edges(g, "executes", src="job:datecron", dst="service:scheduler")


def test_p1_docker_sock_bind_mounts_flagged_as_boundary():
    g = graph()
    dock = [e for e in g["edges"]
            if e["type"] == "mounts"
            and "/var/run/docker.sock" in json.dumps(e["props"])]
    assert len(dock) >= 5, len(dock)
    assert all(e["boundary"] is True for e in dock)


def test_p1_reads_writes_derived_for_scheduler_and_sites():
    g = graph()
    assert edges(g, "reads_from", src="service:scheduler", dst="volume:sites")
    assert edges(g, "writes_to", src="service:scheduler", dst="volume:sites")


def test_p1_build_stages_count_eleven():
    g = graph()
    stages = nodes(g, "build_stage")
    assert len(stages) == 11, sorted(n["id"] for n in stages)


def test_p1_erpnext_image_built_from_production_stage():
    g = graph()
    imgs = [n for n in nodes(g, "image") if n["props"].get("repo") == "frappe/erpnext"]
    assert imgs, "frappe/erpnext image node missing"
    built = edges(g, "built_from", src=imgs[0]["id"])
    assert any("images/production/Containerfile" in json.dumps(e) for e in built), built


def test_p1_external_artifacts_cloned_into_build():
    g = graph()
    arts = {n["name"] for n in nodes(g, "artifact") if n["props"].get("kind") == "git_repo"}
    for needle in ("frappe/bench", "frappe/frappe", "frappe/erpnext"):
        assert any(needle in a for a in arts), (needle, arts)
    assert len(edges(g, "clones")) >= 3


def test_p1_all_eighteen_scenarios_parse():
    g = graph()
    cfg = json.loads((REPO / "tests" / "compose-configs.json").read_text(encoding="utf-8"))
    names = [c["name"] for c in cfg["configurations"]]
    assert len(names) == 18
    for name in names:
        services = docker_parser.scenario_services(REPO, name)
        assert services, f"scenario {name} resolved to no services"


def test_p1_multi_bench_joins_external_networks():
    g = graph()
    ext = [e for e in edges(g, "joins_network", src="service:frontend",
                           dst="network:traefik-public")]
    assert ext, "frontend not joined to traefik-public in multi-bench"
    assert ext[0]["props"].get("external") is True


# ------------------------------------------- P1 vulnerability regressions

def test_p1_vuln_bind_mount_overrides_mark_boundary():
    g = graph()
    v = node(g, "volume:sites")
    assert v["boundary"] is True, "bind-mount override must flag host boundary"
    assert "SITES_DATA_LOCATION" in json.dumps(v["props"]), v["props"]
    files = {ev["file"] for ev in v["evidence"]}
    assert "overrides/compose.bind-mount-sites.yaml" in files, files


def test_p1_vuln_no_unresolved_arg_artifacts():
    g = graph()
    tokens = ("GIT_REPO", "FRAPPE_PATH", "ERPNEXT_REPO", "DEBIAN_BASE",
              "PYTHON_VERSION", "FRAPPE_IMAGE_PREFIX", "FRAPPE_BRANCH")
    bad = [n["id"] for n in nodes(g, "artifact")
           if any(t in n["id"] for t in tokens)]
    assert bad == [], bad


def test_p1_vuln_all_edges_have_located_evidence():
    g = graph()
    bad = [e["id"] for e in g["edges"]
           if not any(ev["line"] > 0 for ev in e["evidence"])]
    assert bad == [], bad[:10]


def test_p1_vuln_all_nodes_have_located_evidence():
    g = graph()
    bad = [n["id"] for n in g["nodes"]
           if not any(ev["line"] > 0 for ev in n["evidence"])]
    assert bad == [], bad[:10]


def test_p1_vuln_postgres_scenario_exposes_5432():
    g = graph()
    pids = [n["id"] for n in nodes(g, "port") if n["id"].startswith("port:db:")]
    assert "port:db:3306" in pids and "port:db:5432" in pids, pids


def test_p1_vuln_doc_links_not_artifacts_and_releases_labeled():
    g = graph()
    arts = {n["id"] for n in nodes(g, "artifact")}
    assert "artifact:github.com/frappe/frappe_docker" not in arts, arts
    wk = node(g, "artifact:github.com/wkhtmltopdf/packaging")
    assert wk["props"]["kind"] == "release", wk["props"]


def test_p1_vuln_noproxy_scenario_publishes_8080():
    g = graph()
    pub = edges(g, "publishes", src="service:frontend", dst="port:frontend:8080")
    assert any("overrides/compose.noproxy.yaml" in e["files"] for e in pub), \
        [e["files"] for e in pub]
    r = tools().scenario_report(g, "mariadb-no-proxy")
    assert "port:frontend:8080" in r["published_ports"], r["published_ports"]


# ---------------------------------------------------------------- P2: tools

def test_p2_taint_from_sites_reaches_carriers_and_exits():
    t = tools()
    reached = {n["id"] for n in t.taint(graph(), "volume:sites")}
    for expected in ("service:backend", "service:scheduler",
                     "job:datecron", "port:frontend:8080"):
        assert expected in reached, (expected, sorted(reached)[:20])


def test_p2_find_exits_inventory():
    t = tools()
    exits = t.find_exits(graph())
    ids = {e["node"] for e in exits}
    assert len(exits) >= 5, ids
    assert "job:datecron" in ids
    assert any("docker.sock" in i for i in ids), ids
    assert any(i.startswith("port:") for i in ids), ids
    for e in exits:
        assert e["evidence"], f"exit without evidence: {e}"


def test_p2_search_pii_finds_stores_secrets_and_contact_env():
    t = tools()
    hits = t.search_pii(graph())
    ids = {h["id"] for h in hits}
    assert "volume:sites" in ids
    assert "volume:db-data" in ids
    assert any(i.startswith("secret:") for i in ids), ids
    assert "env:LETSENCRYPT_EMAIL" in ids


def test_p2_blast_radius_of_db_includes_known_callers():
    t = tools()
    callers = {n["id"] for n in t.reachable_from(graph(), "service:db", direction="in")}
    for expected in ("service:configurator", "service:backend", "service:create-site"):
        assert expected in callers, (expected, sorted(callers))


def test_p2_trace_reports_edges_with_direction_and_depth():
    t = tools()
    res = t.trace(graph(), "service:backend", direction="both", depth=1)
    assert res["node"]["id"] == "service:backend"
    assert res["edges"], "trace returned no edges"
    for e in res["edges"]:
        assert e["direction"] in ("in", "out")
        assert e["evidence"]


# ------------------------------------------- P2 vulnerability regressions

def test_p2_vuln_schema_validator_catches_ghost_edge():
    import copy
    bad = copy.deepcopy(graph())
    bad["edges"].append({
        "id": "x", "type": "mounts", "src": "service:ghost", "dst": "volume:sites",
        "label": "", "confidence": "parsed", "evidence": [], "files": [],
        "scenarios": [], "boundary": False, "props": {}})
    errs = tools().validate_schema(bad)
    assert any("service:ghost" in e for e in errs), errs


def test_p2_vuln_get_node_unknown_raises_with_context():
    try:
        tools().get_node(graph(), "service:backendk")
        assert False, "expected KeyError"
    except KeyError as exc:
        assert "backendk" in str(exc)


def test_p2_vuln_cli_numeric_args_are_coerced():
    import io
    from contextlib import redirect_stdout
    t = tools()
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = t.main(["list_nodes", "type=doctype", "limit=5"])
    assert rc == 0
    assert len(json.loads(buf.getvalue())) == 5


# ---------------------------------------------------------------- P3: agent

def test_p3_agent_tool_specs_match_tools_registry():
    t = tools()
    for spec in agent.TOOL_SPECS:
        name = spec["function"]["name"]
        assert hasattr(t, name), f"agent exposes missing tool: {name}"


def test_p3_agent_dispatch_executes_tool_with_evidence():
    out = agent.dispatch(graph(), "get_node", {"id": "service:db"})
    assert out["id"] == "service:db"
    assert out["evidence"]


def test_p3_agent_offline_loop_with_scripted_llm():
    scripted = [
        {"tool_calls": [{"id": "c1", "function": {
            "name": "get_node", "arguments": json.dumps({"id": "service:db"})}}]},
        {"content": "The db service exists (see evidence)."},
    ]
    calls = []

    def fake_llm(messages, tool_specs):
        calls.append(messages)
        return scripted.pop(0)

    answer = agent.run_loop(graph(), "where is db?", llm=fake_llm)
    assert "db" in answer["answer"].lower()
    assert answer["tool_calls_used"] == ["get_node"]
    assert len(calls) == 2


def test_p3_fallback_router_answers_demo_queries_with_evidence():
    questions = [
        "Where is personal data stored and who can reach it?",
        "What credentials are exposed and where?",
        "Trace a request hitting port 8080.",
        "What is the blast radius of the db service?",
        "What can leave this system?",
        "What data is tainted by personal information?",
    ]
    for q in questions:
        ans = agent.answer_without_llm(graph(), q)
        assert ans["answer"], q
        assert ans["evidence"], q


# ------------------------------------------- P3 vulnerability regressions

def test_p3_vuln_secret_evidence_snippets_are_redacted():
    import re
    leaks = []
    for n in nodes(graph(), "secret"):
        for ev in n["evidence"]:
            if re.search(r"[:=]\s*(admin|123|changeit|password)\b", ev["snippet"]):
                leaks.append((n["id"], ev["snippet"][:60]))
    assert leaks == [], leaks[:3]


def test_p3_vuln_tool_spec_params_match_signatures():
    import inspect
    t = tools()
    for spec in agent.TOOL_SPECS:
        name = spec["function"]["name"]
        params = set(spec["function"]["parameters"].get("properties", {}))
        sig = set(inspect.signature(t.TOOLS[name]).parameters) - {"g"}
        unknown = params - sig
        assert not unknown, (name, unknown)


def test_p3_vuln_dispatch_unknown_tool_returns_error():
    out = agent.dispatch(graph(), "terraform_destroy", {})
    assert isinstance(out, dict) and "error" in out, out


# ---------------------------------------------------------------- P4: render

def test_p4_render_mermaid_contains_key_nodes():
    text = render.to_mermaid(graph())
    assert text.startswith("flowchart") or text.startswith("graph"), text[:40]
    assert "service:backend" in text.replace(" ", "") or "backend" in text
    assert "sites" in text


def test_p4_render_dot_is_valid_digraph():
    dot = render.to_dot(graph())
    assert dot.startswith("digraph"), dot[:40]
    assert dot.rstrip().endswith("}")


# --------------------------------------- P5: ERPNext data-level lineage

def test_p5_doctype_count_and_modules():
    from collections import Counter
    g = graph()
    real = [n for n in nodes(g, "doctype") if not n["props"].get("external")]
    assert len(real) == 529, len(real)
    mods = Counter(n["props"].get("module") for n in real)
    assert mods["Accounts"] == 191, mods
    assert mods["Stock"] == 75
    assert mods["Selling"] == 18


def test_p5_materialized_field_count():
    g = graph()
    fields = nodes(g, "field")
    assert len(fields) == 2953, len(fields)


def test_p5_customer_doctype_and_pii_fields():
    g = graph()
    assert node(g, "doctype:customer")["props"]["module"] == "Selling"
    for fn, cat in (("email_id", "contact"), ("mobile_no", "contact"),
                    ("customer_name", "identity"), ("tax_id", "identity"),
                    ("first_name", "identity"), ("last_name", "identity")):
        f = node(g, f"field:customer.{fn}")
        assert cat in f["props"]["categories"], (fn, f["props"]["categories"])
    assert node(g, "field:customer.email_id")["props"]["options"] == "Email"
    assert node(g, "field:customer.email_id")["sensitivity"] == "likely"


def test_p5_lead_contact_fields():
    g = graph()
    for fn in ("email_id", "phone", "mobile_no", "whatsapp_no"):
        f = node(g, f"field:lead.{fn}")
        assert "contact" in f["props"]["categories"], fn


def test_p5_external_stubs_are_bounded():
    g = graph()
    stubs = [n for n in nodes(g, "doctype") if n["props"].get("external")]
    # 26 from Link targets + 4 frappe-core targets wired via hooks doc_events
    # (Communication, Contact Us Settings, Email Unsubscribe, Integration Request)
    assert len(stubs) == 30, sorted(n["id"] for n in stubs)
    ids = {n["id"] for n in stubs}
    assert {"doctype:user", "doctype:contact", "doctype:address"} <= ids
    assert "doctype:dynamic-link" in ids  # real frappe-core child doctype
    assert "doctype:communication" in ids  # wired via doc_events


def test_p5_links_counts():
    g = graph()
    assert len([e for e in edges(g, "links_to")
                if e["props"].get("kind") == "link"]) == 2170
    assert len(edges(g, "contains_child")) == 320
    cust = [e for e in edges(g, "links_to", dst="doctype:customer")
            if e["props"].get("kind") == "link"]
    assert len(cust) == 42, len(cust)


def test_p5_links_internal_and_external_targets():
    g = graph()
    assert edges(g, "links_to", src="field:customer.customer_primary_contact",
                 dst="doctype:contact")
    assert edges(g, "links_to", src="field:customer.lead_name", dst="doctype:lead")
    assert edges(g, "links_to", src="field:customer.account_manager", dst="doctype:user")
    assert node(g, "doctype:contact")["props"]["external"] is True
    assert not node(g, "doctype:lead")["props"].get("external")


def test_p5_fetch_from_edges_and_chains():
    g = graph()
    assert len(edges(g, "fetch_from")) == 362
    e = edges(g, "fetch_from", src="field:customer.email_id", dst="doctype:contact")
    assert e and e[0]["props"].get("source_field") == "email_id", e
    assert g["meta"]["erpnext"]["fetch_from_unresolved"] == 6


def test_p5_stored_in_db_data_and_sites_files():
    g = graph()
    assert edges(g, "stored_in", src="doctype:customer", dst="volume:db-data")
    attach_stored = [e for e in g["edges"]
                     if e["type"] == "stored_in" and e["dst"] == "volume:sites"]
    assert len(attach_stored) == 48, len(attach_stored)


def test_p5_roles_and_deduped_permits():
    g = graph()
    assert len(nodes(g, "role")) == 34
    assert len(edges(g, "permits")) == 1115
    e = edges(g, "permits", src="role:sales-user", dst="doctype:customer")
    assert e and e[0]["props"].get("read") is True, e
    assert any(x["props"].get("export") for x in edges(g, "permits")), "no export flags"


def test_p5_pii_signal_aggregates():
    g = graph()
    fields = nodes(g, "field")
    opt = [f for f in fields
           if f["props"].get("options") in ("Email", "Phone", "Mobile", "WhatsApp")]
    assert len(opt) == 62, len(opt)
    attach = [f for f in fields
              if f["props"].get("fieldtype") in ("Attach", "Attach Image")]
    assert len(attach) == 48
    pw = [f for f in fields if f["props"].get("fieldtype") == "Password"]
    assert len(pw) == 2
    gs = [f for f in fields
          if f["props"].get("globally_searchable")
          and set(f["props"].get("categories", [])) & {"contact", "identity", "location"}]
    assert len(gs) == 37, len(gs)


def test_p5_meta_repos_and_artifact_contains():
    g = graph()
    m = g["meta"]
    assert m["repos"] == ["frappe_docker", "erpnext"]
    assert m["git_commits"]["erpnext"].startswith("7474")
    assert edges(g, "contains", src="artifact:github.com/frappe/erpnext",
                 dst="doctype:customer")
    assert "erpnext" in node(g, "doctype:customer")["scenarios"]


def test_p5_reports_and_print_formats():
    g = graph()
    reps = nodes(g, "report")
    assert len(reps) == 186, len(reps)
    sql = [n for n in reps if n["props"].get("query_type") == "sql"]
    assert len(sql) == 16
    script = [n for n in reps if n["props"].get("reads_known") is False]
    assert len(script) == 170
    reads = [e for e in edges(g, "reads_from") if e["src"].startswith("report:")]
    assert any(e["dst"] == "doctype:delivery-note" for e in reads), reads[:5]
    assert len(nodes(g, "print_format")) == 75
    assert edges(g, "displays"), "no displays edges"


def test_p5_tools_module_filter_and_role_pii():
    t = tools()
    dt = t.list_nodes(graph(), type="doctype", module="Selling")
    assert dt and all(n["props"]["module"] == "Selling" for n in dt)
    hits = {h["id"] for h in t.search_pii(graph())}
    assert "field:customer.email_id" in hits, sorted(hits)[:10]


def test_p5_fallback_answers_data_questions_with_evidence():
    for q in ("Which roles can export customer data?",
              "Show fetch_from chains.",
              "Where is customer data physically stored?"):
        a = agent.answer_without_llm(graph(), q)
        assert a["answer"], q
        assert a["evidence"], q


def test_p5_vuln_chart_templates_never_become_doctypes():
    g = graph()
    bad = [n["id"] for n in nodes(g, "doctype")
           for ev in n["evidence"] if "doctype/account/chart_of_accounts" in ev["file"]]
    assert bad == [], bad
    assert g["meta"]["erpnext"]["skipped_non_doctype"] == 15
    assert g["meta"]["erpnext"]["slug_collisions"] == 0


# --------------------------------------- DUMMY data fixtures

def dummy():
    if "dummy" not in _cache:
        _cache["dummy"] = dummy_data.generate(graph())
    return _cache["dummy"]


def test_dummy_generator_is_deterministic():
    a = dummy_data.generate(graph(), seed=7)
    b = dummy_data.generate(graph(), seed=7)
    assert a == b
    c = dummy_data.generate(graph(), seed=8)
    assert a != c


def test_dummy_covers_every_field_node():
    d = dummy()
    field_ids = {n["id"] for n in nodes(graph(), "field")}
    assert set(d["fields"]) == field_ids
    assert all(e["samples"] for e in d["fields"].values())


def test_dummy_contact_values_are_reserved_and_fake():
    d = dummy()
    emails = d["fields"]["field:customer.email_id"]["samples"]
    assert all(x.endswith("@example.com") for x in emails), emails
    phones = d["fields"]["field:lead.mobile_no"]["samples"]
    assert all(x.startswith("+91-90000") for x in phones), phones


def test_dummy_password_samples_are_masked():
    d = dummy()
    pw = [(fid, e) for fid, e in d["fields"].items()
          if e.get("fieldtype") == "Password"]
    assert len(pw) == 2
    for fid, e in pw:
        assert all("redacted" in v for v in e["samples"]), fid


def test_dummy_records_exist_for_all_doctypes_with_fields():
    g = graph()
    d = dummy()
    srcs = {e["src"].split(":", 1)[1] for e in g["edges"]
            if e["type"] == "contains" and e["src"].startswith("doctype:")}
    assert set(d["doctypes"]) == srcs
    assert len(d["doctypes"]) == 453
    recs = d["doctypes"]["customer"]["records"]
    assert len(recs) == 3
    assert all(r["email_id"].endswith("@example.com") for r in recs)
    assert all(r["name"].startswith("DEMO-CUSTOMER") for r in recs)


def test_dummy_other_node_types_have_samples_and_secrets_masked():
    d = dummy()
    assert d["nodes"]["volume:sites"]["samples"]
    assert d["nodes"]["service:backend"]["samples"]
    sec = d["nodes"]["secret:MYSQL_ROOT_PASSWORD"]
    assert all("***" in s for s in sec["samples"]), sec


def test_dummy_apply_populates_graph_and_preserves_schema():
    g = graph()
    applied = dummy_data.apply(g, dummy())
    assert applied is not g
    fn = [n for n in applied["nodes"] if n["id"] == "field:customer.email_id"][0]
    assert fn["props"]["sample_values"], fn["props"]
    dt = [n for n in applied["nodes"] if n["id"] == "doctype:customer"][0]
    assert len(dt["props"]["sample_records"]) == 3
    assert tools().validate_schema(applied) == []
    orig = [n for n in g["nodes"] if n["id"] == "field:customer.email_id"][0]
    assert "sample_values" not in orig["props"]


def test_dummy_render_shows_sample_values():
    applied = dummy_data.apply(graph(), dummy())
    text = render.to_mermaid(applied, focus="field:customer.email_id", depth=1)
    assert "example.com" in text, text[:200]


def test_dummy_cli_roundtrip():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        dpath = Path(td) / "dummy.json"
        gpath = Path(td) / "demo.json"
        rc1 = dummy_data.main(["generate", "--out", str(dpath), "--records", "2"])
        rc2 = dummy_data.main(["apply", "--data", str(dpath), "--out", str(gpath)])
        assert rc1 == 0 and rc2 == 0
        loaded = json.loads(gpath.read_text(encoding="utf-8"))
        assert any(n["props"].get("sample_values") for n in loaded["nodes"])
        assert len(json.loads(dpath.read_text(encoding="utf-8"))["doctypes"]["customer"]["records"]) == 2


# --------------------------------------- CATEGORY slices (L3: simple views)

def test_slices_categories_are_wellformed_and_bounded():
    cats = slices.build_categories(graph())
    assert set(cats) == {"deployment", "customer-flow", "pii-flow", "access",
                         "exits", "structure"}
    caps = {"deployment": 140, "customer-flow": 70, "pii-flow": 260,
            "access": 160, "exits": 80, "structure": 900}
    for name, c in cats.items():
        ids = {n["id"] for n in c["nodes"]}
        assert ids, name
        assert len(ids) <= caps[name], (name, len(ids))
        for e in c["edges"]:
            assert e["src"] in ids and e["dst"] in ids, (name, e["id"])
            assert e["evidence"], (name, e["id"])


def test_slices_customer_flow_contains_key_nodes():
    c = slices.build_categories(graph())["customer-flow"]
    ids = {n["id"] for n in c["nodes"]}
    for expected in ("doctype:customer", "field:customer.email_id",
                     "doctype:contact", "storage:hub", "transform:customer",
                     "role:sales-user"):
        assert expected in ids, (expected, sorted(ids)[:15])


def test_slices_pii_flow_is_self_describing():
    c = slices.build_categories(graph())["pii-flow"]
    types = {n["type"] for n in c["nodes"]}
    ids = {n["id"] for n in c["nodes"]}
    assert types <= {"doctype", "source", "sink"}, types
    assert "doctype:customer" in ids and "sink:email" in ids
    # uniform storage fact is stated in the caption, not drawn 88 times
    assert "storage:hub" not in ids
    assert not any(e["type"] == "stored_in" for e in c["edges"])
    # every doctype self-describes: PII count + field names + size by count
    cust = next(n for n in c["nodes"] if n["id"] == "doctype:customer")
    assert cust["props"]["pii_count"] == 8, cust["props"].get("pii_count")
    assert "email_id" in cust["props"]["pii_fields"]
    assert cust["props"]["viz_size"] >= 20
    sizes = {n["props"].get("viz_size") for n in c["nodes"]
             if n["type"] == "doctype"}
    assert len(sizes) > 3, sizes


def test_slices_cli_writes_index_and_every_view():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        rc = slices.main(["--outdir", td, "--graph", str(paths.default_graph())])
        assert rc == 0
        out = Path(td)
        assert (out / "index.html").exists()
        for name in ("deployment", "customer-flow", "pii-flow", "access",
                     "exits", "structure"):
            page = out / f"{name}.html"
            assert page.exists(), name
            assert "const GRAPH" in page.read_text(encoding="utf-8")
        assert (out / "vendor" / "sigma.min.js").exists()


# --------------------------------------- P6: flow layer (L1+L2)

def test_p6_fetch_from_field_precision():
    g = graph()
    ff = edges(g, "fetch_from")
    to_field = [e for e in ff if e["dst"].startswith("field:")]
    assert len(to_field) >= 150, len(to_field)
    assert len(ff) == 362, len(ff)


def test_p6_print_formats_display_fields():
    g = graph()
    disp = [e for e in edges(g, "displays") if e["dst"].startswith("field:")]
    assert len(disp) >= 50, len(disp)


def test_p6_reports_read_fields():
    g = graph()
    reads = [e for e in edges(g, "reads_from")
             if e["src"].startswith("report:") and e["dst"].startswith("field:")]
    assert len(reads) >= 100, len(reads)


def test_p6_code_nodes_and_flow_edges():
    g = graph()
    code = nodes(g, "code")
    assert len(code) >= 300, len(code)
    node(g, "code:erpnext/selling/doctype/customer/customer.py")
    code_edges = [e for e in g["edges"]
                  if e["src"].startswith("code:")
                  and e["type"] in ("reads_from", "writes_to")]
    assert len(code_edges) >= 800, len(code_edges)


def test_p6_sources_sinks_and_aggregates():
    g = graph()
    node(g, "source:api")
    node(g, "source:import")
    node(g, "sink:email")
    api_edges = [e for e in g["edges"] if e["src"] == "source:api"]
    assert len(api_edges) >= 100, len(api_edges)
    imports = [e for e in g["edges"] if e["src"] == "source:import"]
    assert len(imports) == 102, len(imports)
    assert len(edges(g, "emits_email")) >= 8
    assert len(edges(g, "wires")) >= 15


def test_p6_taint_reaches_email_sink():
    g = graph()
    t = tools()
    emitters = [e for e in g["edges"] if e["type"] == "emits_email"]
    assert emitters, "no emits_email edges"
    touched = [e["dst"] for e in g["edges"]
               if e["src"] == emitters[0]["src"]
               and e["type"] in ("reads_from", "writes_to")
               and e["dst"].startswith("doctype:")]
    assert touched, "emitter code touches no doctype"
    reached = {n["id"] for n in t.taint(g, touched[0])}
    assert "sink:email" in reached


def test_p6_graph_growth_is_bounded():
    g = graph()
    assert len(g["edges"]) <= 16000, len(g["edges"])
    assert len(g["nodes"]) <= 5600, len(g["nodes"])


# --------------------------------------- viewer UI (semantic zoom, polish)

def test_view_ui_has_controls_semantic_zoom_and_a11y():
    g = {"meta": {"node_count": 1},
         "nodes": [{"id": "doctype:x", "type": "doctype", "name": "X",
                    "label": "X", "tier": 3, "group": "data",
                    "sensitivity": "none", "boundary": False,
                    "confidence": "parsed", "evidence": [], "files": [],
                    "scenarios": ["repo"], "props": {}}],
         "edges": []}
    text = view.build_html(g, "t", vendor_tag="<script></script>")
    for needle in ('id="trace"', 'id="lrbtn"', "applyLOD", "sigma",
                   "d3.forceSimulation", "prefers-reduced-motion", 'aria-live',
                   "Nothing visible", 'id="tip"'):
        assert needle in text, needle
    rich = view.build_html(g, "t", vendor_tag="<script></script>",
                           caption="Each box is a DocType", default_lr=True)
    assert "Each box is a DocType" in rich
    assert "lrOn = true" in rich
    assert "runLayout('lr')" in rich


# --------------------------------------- audit fixes (regressions)

def test_fix_db_data_mount_is_scenario_precise():
    g = graph()
    ms = edges(g, "mounts", src="service:db", dst="volume:db-data")
    paths = {e["props"].get("path") for e in ms}
    assert "/var/lib/mysql" in paths, paths
    assert "/var/lib/postgresql/data" in paths, paths
    assert len({e["id"] for e in ms}) == len(ms), "edge ids must stay unique"
    mysql = [e for e in ms if e["props"]["path"] == "/var/lib/mysql"][0]
    pg = [e for e in ms if e["props"]["path"] == "/var/lib/postgresql/data"][0]
    assert "pwd.yml" in mysql["files"], mysql["files"]
    assert "overrides/compose.mariadb.yaml" in mysql["files"], mysql["files"]
    assert pg["files"] == ["overrides/compose.postgres.yaml"], pg["files"]


def test_fix_cross_file_depends_on_is_resolved():
    g = graph()
    e = edges(g, "depends_on", src="service:migrator", dst="service:configurator")
    assert e, "migrator -> configurator dependency missing"
    assert e[0]["props"].get("condition") == "service_completed_successfully"


def test_fix_wires_preserve_each_event():
    g = graph()
    w = edges(g, "wires", src="code:erpnext/stock/doctype/item/item_search.py",
              dst="doctype:item")
    assert {e["label"] for e in w} == {"on_update", "after_rename"}, w
    m = g["meta"]["erpnext"]
    assert m["wire_edges"] == len(edges(g, "wires")), \
        (m["wire_edges"], len(edges(g, "wires")))
    assert (m["wires_file_missing"] + m["wires_no_code_node"]
            == m["wires_unresolved"])


def test_fix_parallel_child_edges_have_own_evidence():
    g = graph()
    es = edges(g, "contains_child", src="doctype:job-card",
               dst="doctype:job-card-time-log")
    assert len(es) == 2, [e["id"] for e in es]
    lines = {e["evidence"][0]["line"] for e in es}
    assert len(lines) == 2, lines
    for e in es:
        assert "Job Card Time Log" in e["evidence"][0]["snippet"]


def test_fix_bind_override_wins_volume_kind():
    g = graph()
    v = node(g, "volume:sites")
    assert v["props"]["kind"] == "bind", v["props"]
    assert v["boundary"] is True
    files = {ev["file"] for ev in v["evidence"]}
    assert "overrides/compose.bind-mount-sites.yaml" in files, files


def test_fix_fetch_from_unresolved_is_classified():
    m = graph()["meta"]["erpnext"]
    assert m["fetch_from_unresolved"] == 6
    assert m["fetch_from_intra_document"] == 2
    assert m["fetch_from_dynamic_link"] == 2
    assert m["fetch_from_dangling"] == 2
    assert (m["fetch_from_intra_document"] + m["fetch_from_dynamic_link"]
            + m["fetch_from_dangling"]) == m["fetch_from_unresolved"]


# --------------------------------------- determinism (regression)

def test_evidence_eviction_is_hash_seed_stable():
    """Regenerating the graph must be stable: evidence eviction used to depend
    on set iteration order (PYTHONHASHSEED), silently changing 25 role nodes
    between runs. Two subprocesses with different hash seeds must agree."""
    import os
    import subprocess
    import textwrap
    script = textwrap.dedent("""
        import pathlib
        from lineage.core import Builder
        b = Builder(pathlib.Path("."))
        b.node("role:x", "role", name="X", group="ops")
        for i in range(14):
            f = f"f{i % 5}.json"
            b.attach("role:x", {"file": f, "line": i, "snippet": "s"}, f)
        b.attach("role:x", {"file": "new.json", "line": 99, "snippet": "s"}, "new.json")
        print([e["file"] + ":" + str(e["line"]) for e in b.nodes["role:x"]["evidence"]])
    """)
    env = {**os.environ,
           "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    outs = []
    for seed in ("1", "424242"):
        r = subprocess.run([sys.executable, "-c", script], capture_output=True,
                           text=True, env={**env, "PYTHONHASHSEED": seed})
        assert r.returncode == 0, r.stderr
        outs.append(r.stdout)
    assert outs[0] == outs[1], outs


# --------------------------------------- chat UI (lineage chat)

def test_viewer_chat_mode_adds_panel_and_wiring():
    text = view.build_html(graph(), "t", vendor_tag="<script></script>", chat=True)
    for needle in ('id="chat"', 'id="askform"', 'id="askinput"', "api/ask",
                   "focusNodes", "data-nodes"):
        assert needle in text, needle
    plain = view.build_html(graph(), "t", vendor_tag="<script></script>")
    assert 'id="chat"' not in plain, "chat panel must be opt-in"
    assert "api/ask" not in plain


def test_agent_loop_collects_focus_and_evidence():
    scripted = [
        {"tool_calls": [{"id": "c1", "function": {
            "name": "trace",
            "arguments": json.dumps({"id": "service:db", "direction": "in",
                                     "depth": 1})}}]},
        {"content": "Callers of db listed."},
    ]

    def fake_llm(messages, tool_specs):
        return scripted.pop(0)

    out = agent.run_loop(graph(), "who calls db?", llm=fake_llm)
    assert "service:db" in out["focus"], out["focus"][:10]
    assert out["evidence"], "tool results carry no evidence"
    assert all("file" in ev and "line" in ev for ev in out["evidence"])


def test_chat_server_roundtrip_answers_with_evidence_and_focus():
    import os
    import threading
    import urllib.error
    import urllib.request
    from lineage import chat

    os.environ.pop("OPENAI_API_KEY", None)  # force the deterministic router
    httpd = chat.make_server(graph(), host="127.0.0.1", port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            page = r.read().decode("utf-8")
        assert 'id="chat"' in page and "api/ask" in page

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/ask",
            data=json.dumps({"question": "Which PII fields does Customer have?"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        assert data["answer"], data
        assert data["evidence"], data
        assert any(f.startswith("field:customer.") for f in data["focus"]), \
            data["focus"][:10]
        assert data["mode"] == "router"

        bad = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/ask", data=b"{}",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(bad, timeout=10)
            assert False, "empty question must be rejected"
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------- P7: reports + chat history

def test_p7_doctype_report_customer():
    rep = tools().doctype_report(graph(), "Customer")
    assert rep["module"] == "Selling"
    pii_names = [p["field"] for p in rep["fields"]["pii"]]
    assert "email_id" in pii_names, pii_names
    assert len(pii_names) == 8, pii_names
    assert rep["links_in"]["total"] == 42, rep["links_in"]
    assert rep["fields"]["child_tables"], rep["fields"]
    assert rep["stored_in"] is True
    assert rep["evidence"], rep


def test_p7_doctype_report_unknown_suggests():
    try:
        tools().doctype_report(graph(), "customr")
        assert False, "expected KeyError"
    except KeyError as exc:
        assert "did you mean" in str(exc), str(exc)
        assert "customer" in str(exc), str(exc)


def test_p7_pii_report_totals():
    g = graph()
    rep = tools().pii_report(g)
    expected = len([n for n in nodes(g, "field") if n["sensitivity"] != "none"])
    assert rep["total_pii_fields"] == expected, (rep["total_pii_fields"], expected)
    assert rep["attachments"] == 48, rep["attachments"]
    assert rep["password_fields"] == 2, rep["password_fields"]
    ids = {r[0] for r in rep["top_doctypes"]}
    assert "doctype:customer" in ids or "doctype:lead" in ids, ids
    assert rep["emits_email_edges"] == len(
        [e for e in g["edges"] if e["type"] == "emits_email"])
    assert rep["stored_in_db_data_doctypes"] == len(
        [e for e in g["edges"] if e["type"] == "stored_in"
         and e["dst"] == "volume:db-data"])


def test_p7_run_loop_receives_history():
    scripted = [{"content": "Customer has 8 PII fields."}]
    seen = []

    def fake_llm(messages, tool_specs):
        seen.append(messages)
        return scripted.pop(0)

    history = [{"role": "user", "content": "hi"},
               {"role": "assistant", "content": "hello"},
               {"role": "user", "content": "tell me about Customer"}]
    out = agent.run_loop(graph(), "which PII fields?", llm=fake_llm, history=history)
    assert seen, "llm never called"
    messages = seen[0]
    assert messages[0]["role"] == "system"
    assert messages[1:4] == [{"role": "user", "content": "hi"},
                             {"role": "assistant", "content": "hello"},
                             {"role": "user", "content": "tell me about Customer"}]
    assert messages[-1] == {"role": "user", "content": "which PII fields?"}
    for key in ("answer", "tool_calls_used", "focus", "evidence"):
        assert key in out, key
    assert "8 PII" in out["answer"]


def test_p7_llm_error_envelope_clean_message():
    import os
    import urllib.request
    payloads = [
        b'{"type":"error","error":{"type":"FreeTierError","message":"boom-test"}}',
        b'{"error":{"message":"Insufficient account funds"}}',
    ]
    needles = ["boom-test", "Insufficient account funds"]

    class FakeResp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    orig = urllib.request.urlopen
    old_key = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = "test-key"
    try:
        for payload, needle in zip(payloads, needles):
            urllib.request.urlopen = lambda *a, _p=payload, **k: FakeResp(_p)
            try:
                agent.llm_openai([{"role": "user", "content": "hi"}], [])
                assert False, f"expected RuntimeError for {payload!r}"
            except RuntimeError as exc:
                assert needle in str(exc), (needle, str(exc))
                assert str(exc).startswith("LLM error:"), str(exc)
    finally:
        urllib.request.urlopen = orig
        if old_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = old_key


def test_p7_chat_accepts_history_and_rejects_bad():
    import os
    import threading
    import urllib.error
    import urllib.request
    from lineage import chat

    old_key = os.environ.pop("OPENAI_API_KEY", None)  # force the offline router
    httpd = chat.make_server(graph(), host="127.0.0.1", port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        good = {"question": "Which PII fields does Customer have?",
                "history": [{"role": "user", "content": "hi"},
                            {"role": "assistant", "content": "hello"}]}
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/ask",
            data=json.dumps(good).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            assert r.status == 200
            data = json.loads(r.read())
        assert data["answer"], data
        assert data["mode"] == "router", data

        for bad_history in ("not-a-list", [{"role": "system", "content": "x"}] * 9):
            bad = {"question": "x", "history": bad_history}
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/ask",
                data=json.dumps(bad).encode(),
                headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=10)
                assert False, f"bad history must be rejected: {bad_history!r}"
            except urllib.error.HTTPError as exc:
                assert exc.code == 400
    finally:
        if old_key is not None:
            os.environ["OPENAI_API_KEY"] = old_key
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------- runner

def run(phase: str | None = None, only: str | None = None) -> int:
    tests = sorted(
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and callable(fn)
    )
    passed = failed = skipped = 0
    for name, fn in tests:
        if phase and not name.startswith(f"test_{phase}_"):
            skipped += 1
            continue
        if only and only not in name.lower():
            skipped += 1
            continue
        try:
            fn()
            passed += 1
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001 - test runner must capture all
            failed += 1
            print(f"FAIL  {name}\n      {type(exc).__name__}: {exc}")

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


def main() -> int:
    args = sys.argv[1:]
    phase = args[args.index("--phase") + 1].lower() if "--phase" in args else None
    only = args[args.index("--only") + 1].lower() if "--only" in args else None
    return run(phase, only)


if __name__ == "__main__":
    raise SystemExit(main())
