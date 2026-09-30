"""Data analyst (LangGraph): answer randomized business questions against Chinook (SQLite) with a SQL tool.

Failed queries come back to the model as error text, so it retries with a corrected query.
Some runs get a stale schema hint (wrong table names) to make those retries happen regularly.
"""
import random
import sqlite3

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from .. import config
from ..llm import Run

FRAMEWORK = "langgraph"
DB_PATH = config.DATA_DIR / "chinook.sqlite"
MAX_ROWS = 50

COUNTRIES = ["USA", "Canada", "Brazil", "France", "Germany", "United Kingdom", "India", "Portugal", "Czech Republic"]
QUESTIONS = [
    "Which {n} customers from {country} spent the most in total? Give names and amounts.",
    "What were total sales per year, and which year was best?",
    "Which {n} genres generated the most revenue in {year}?",
    "Which sales support agent is responsible for the most invoice revenue?",
    "What is the average invoice total for customers in {country}?",
    "Which artist has the most tracks in the catalog, and how many?",
    "How many tracks longer than {minutes} minutes are there per media type?",
    "Which month of {year} had the highest sales, and how much?",
    "List the top {n} best-selling tracks by quantity sold, with their artist.",
    "Which playlist contains the most tracks?",
    "What share of {year} revenue came from customers outside the USA?",
]
STALE_HINT = "Schema note from the old wiki: invoices live in `Invoices` and `InvoiceItems`, customers in `Customers`."


def make_tools(run: Run):
    def connect():
        return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

    @tool
    def list_tables() -> str:
        """List the tables in the database."""
        run.tool_used()
        with connect() as db:
            return ", ".join(r[0] for r in db.execute("select name from sqlite_master where type='table' order by 1"))

    @tool
    def describe_table(table: str) -> str:
        """Show the columns of a table."""
        run.tool_used()
        with connect() as db:
            cols = db.execute("select name, type from pragma_table_info(?)", (table,)).fetchall()
        return ", ".join(f"{n} {t}" for n, t in cols) if cols else f"ERROR: no such table: {table}"

    @tool
    def run_sql(query: str) -> str:
        """Run a read-only SQL SELECT against the SQLite database. Returns up to 50 rows."""
        run.tool_used()
        run.extra["sql_attempts"] = run.extra.get("sql_attempts", 0) + 1
        if not query.lstrip().lower().startswith(("select", "with")):
            run.extra["sql_errors"] = run.extra.get("sql_errors", 0) + 1
            return "ERROR: only SELECT queries are allowed"
        try:
            with connect() as db:
                cur = db.execute(query)
                rows = cur.fetchmany(MAX_ROWS)
                header = [d[0] for d in cur.description or []]
        except sqlite3.Error as e:
            run.extra["sql_errors"] = run.extra.get("sql_errors", 0) + 1
            return f"ERROR: {e}"
        return "\n".join([" | ".join(header)] + [" | ".join(map(str, r)) for r in rows]) or "(no rows)"

    return [list_tables, describe_table, run_sql]


async def run(run: Run) -> None:
    question = random.choice(QUESTIONS).format(n=random.choice([3, 5, 10]), country=random.choice(COUNTRIES),
                                               year=random.randint(2009, 2013), minutes=random.choice([5, 6, 8]))
    stale = random.random() < 0.2
    system = ("You are a data analyst with access to a SQLite music-store database (tables for artists, albums, "
              "tracks, customers, invoices and employees). Use the tools to inspect the schema and query it. "
              "If a query fails, read the error, fix the query and try again. Answer with the figures and a "
              "one-paragraph explanation, and include the final SQL you used.")
    if stale:
        system += "\n" + STALE_HINT

    tools = make_tools(run)
    llm = ChatOpenAI(model=run.model, base_url=config.BASE_URL, api_key=run.api_key(),
                     http_async_client=run.http_client(), max_tokens=4096, temperature=0.1, max_retries=4)
    llm_with_tools = llm.bind_tools(tools)

    async def agent(state: MessagesState):
        return {"messages": [await llm_with_tools.ainvoke(state["messages"])]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")
    result = await graph.compile().ainvoke({"messages": [SystemMessage(system), HumanMessage(question)]},
                                           {"recursion_limit": 20})
    answer = result["messages"][-1].content or ""
    run.extra.update({"question": question, "stale_schema_hint": stale, "answer_chars": len(answer)})
    run.extra.setdefault("sql_attempts", 0)
    run.extra.setdefault("sql_errors", 0)
