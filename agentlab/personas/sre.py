"""SRE log analyst (LangGraph): investigate generated app logs with file tools, write an incident summary."""
import random
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from .. import config
from ..llm import Run

FRAMEWORK = "langgraph"
SERVICES = ["checkout-api", "payments", "auth-service", "inventory", "search", "notifications"]
KEEP_RUN_DIRS = 20

# name -> (service, error lines, keywords a correct diagnosis should mention)
SCENARIOS = {
    "db_pool_exhausted": ("checkout-api", [
        "WARN  HikariPool-1 - Connection pool at 95% (19/20 active)",
        "ERROR HikariPool-1 - Connection is not available, request timed out after 30000ms",
        "ERROR POST /api/checkout 503 Service Unavailable (db connection timeout)"], ["pool", "connection"]),
    "oom_killed": ("inventory", [
        "WARN  GC overhead high: 92% time in GC over last 60s",
        "ERROR java.lang.OutOfMemoryError: Java heap space",
        "ERROR container inventory-7d9f exited with code 137 (OOMKilled); restarting"], ["memory", "oom"]),
    "cert_expired": ("payments", [
        "WARN  TLS certificate for api.payprovider.example expires in 0 days",
        "ERROR x509: certificate has expired or is not yet valid",
        "ERROR POST /v1/charge upstream handshake failed; 502 returned to caller"], ["certificate", "tls", "x509"]),
    "disk_full": ("search", [
        "WARN  /var/lib/search usage 97%",
        "ERROR failed to flush segment: No space left on device",
        "ERROR index writer closed; search results degraded"], ["disk", "space"]),
    "upstream_latency": ("notifications", [
        "WARN  smtp relay p99 latency 8400ms (threshold 1000ms)",
        "ERROR upstream request timeout after 10000ms to smtp-relay:587",
        "ERROR queue depth 18234 and growing; retries scheduled"], ["latency", "timeout", "slow"]),
    "login_failures": ("auth-service", [
        "WARN  52 failed logins for user=admin from 203.0.113.77 in 60s",
        "ERROR account admin temporarily locked (too many failed attempts)",
        "WARN  403 rate limit applied to 203.0.113.77"], ["login", "brute", "failed"]),
}

NORMAL = ["INFO  GET /health 200 {ms}ms", "INFO  GET /api/items?page={n} 200 {ms}ms",
          "INFO  POST /api/cart 201 {ms}ms", "DEBUG cache hit ratio 0.{n}", "INFO  job sync-{n} completed in {ms}ms",
          "INFO  user session refreshed", "WARN  slow query {ms}ms on table orders"]


def generate_logs(workdir: Path) -> tuple[str, int]:
    scenario = random.choice(list(SCENARIOS))
    bad_service, errors, _ = SCENARIOS[scenario]
    total = random.randint(300, 1500)
    incident_at = random.randint(total // 3, total - 60)
    t = datetime(2026, 9, 29, random.randint(0, 22), random.randint(0, 59))
    lines = {s: [] for s in SERVICES}
    for i in range(total):
        t += timedelta(milliseconds=random.randint(50, 3000))
        service = random.choice(SERVICES)
        if i >= incident_at and random.random() < 0.35:
            service, msg = bad_service, random.choice(errors)
        else:
            msg = random.choice(NORMAL).format(ms=random.randint(3, 900), n=random.randint(1, 99))
        lines[service].append(f"{t.isoformat(timespec='milliseconds')}Z {service} {msg}")
    for service, rows in lines.items():
        (workdir / f"{service}.log").write_text("\n".join(rows) + "\n")
    return scenario, total


def make_tools(run: Run, workdir: Path):
    def resolve(name: str) -> Path:
        path = (workdir / name).resolve()
        if path.parent != workdir.resolve() or not path.exists():
            raise ValueError(f"no such log file: {name}")
        return path

    @tool
    def list_log_files() -> str:
        """List available log files with their line counts."""
        run.tool_used()
        return "\n".join(f"{p.name}: {sum(1 for _ in p.open())} lines" for p in sorted(workdir.glob("*.log")))

    @tool
    def read_log(filename: str, start_line: int = 1, max_lines: int = 100) -> str:
        """Read lines from a log file (1-based start_line, at most 200 lines)."""
        run.tool_used()
        rows = resolve(filename).read_text().splitlines()
        start = max(start_line, 1) - 1
        return "\n".join(rows[start:start + min(max_lines, 200)]) or "(no lines in that range)"

    @tool
    def search_logs(pattern: str, filename: str = "") -> str:
        """Case-insensitive regex search across all log files (or one file). Returns up to 40 matches."""
        run.tool_used()
        files = [resolve(filename)] if filename else sorted(workdir.glob("*.log"))
        try:
            rx = re.compile(pattern, re.I)
        except re.error as e:
            return f"invalid regex: {e}"
        hits = [f"{p.name}: {line}" for p in files for line in p.read_text().splitlines() if rx.search(line)]
        return "\n".join(hits[:40]) + (f"\n... {len(hits) - 40} more" if len(hits) > 40 else "") if hits else "no matches"

    return [list_log_files, read_log, search_logs]


SYSTEM = ("You are an on-call SRE. Investigate the application logs with the tools provided: find which "
          "service is failing, when it started, the probable root cause and the customer impact. Use "
          "search_logs for ERROR/WARN lines rather than reading whole files. Finish with an incident summary "
          "in markdown: Title, Severity (SEV1-SEV4), Affected service, Timeline, Root cause, Impact, Next steps.")


async def run(run: Run) -> None:
    base = config.WORK_DIR / "sre"
    workdir = base / run.id
    workdir.mkdir(parents=True, exist_ok=True)
    for old in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime)[:-KEEP_RUN_DIRS]:
        shutil.rmtree(old, ignore_errors=True)

    scenario, line_count = generate_logs(workdir)
    tools = make_tools(run, workdir)
    llm = ChatOpenAI(model=run.model, base_url=config.BASE_URL, api_key=run.api_key(),
                     http_async_client=run.http_client(), max_tokens=4096, temperature=0.2, max_retries=4)
    llm_with_tools = llm.bind_tools(tools)

    async def agent(state: MessagesState):
        return {"messages": [await llm_with_tools.ainvoke(state["messages"])]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")
    app = graph.compile()

    result = await app.ainvoke(
        {"messages": [SystemMessage(SYSTEM), HumanMessage("Alerts are firing. Logs are in the incident workspace. Investigate.")]},
        {"recursion_limit": 20},
    )
    report = result["messages"][-1].content or ""
    (workdir / "incident.md").write_text(report)
    keywords = SCENARIOS[scenario][2]
    run.extra.update({"scenario": scenario, "log_lines": line_count, "report_chars": len(report),
                      "diagnosis_correct": any(k in report.lower() for k in keywords)})
