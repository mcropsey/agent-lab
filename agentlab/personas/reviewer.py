"""Code reviewer (OpenAI Agents SDK): review a diff with surrounding file context -> long-context requests.

Diff sources: bundled sample diffs with a planted issue (data/diffs), or a synthetic diff made by
applying a small mutation to one of this lab's own source files.
"""
import difflib
import random
import re
from typing import Literal

from agents import Agent, ModelSettings, OpenAIChatCompletionsModel, Runner, StopAtTools, function_tool, \
    set_tracing_disabled
from pydantic import BaseModel, Field

from .. import config
from ..llm import Run

FRAMEWORK = "openai-agents"
set_tracing_disabled(True)
MAX_CONTEXT_CHARS = 60_000  # ~15k tokens, leaving room for up to 12k output tokens within 32k
SRC_ROOT = config.ROOT

MUTATIONS = [
    ("disable retries", r"max_retries=\d+", "max_retries=0"),
    ("loosen bounds check", r" < ", " <= "),
    ("invert equality", r" == ", " != "),
    ("drop a timeout", r"timeout=[\w.]+, ?", ""),
    ("swallow errors", r"except (\w+(?:\.\w+)*) as e:", r"except Exception:  # \1"),
    ("remove tool accounting", r"\n\s*run\.tool_used\(\)", ""),
]


class Comment(BaseModel):
    file: str
    line: int
    severity: Literal["nit", "minor", "major", "critical"]
    message: str


class Review(BaseModel):
    summary: str = Field(description="two or three sentences")
    verdict: Literal["approve", "request_changes", "comment"]
    comments: list[Comment]


def bundled_diff() -> dict:
    path = random.choice(sorted((config.DATA_DIR / "diffs").glob("*.diff")))
    text = path.read_text()
    header, _, diff = text.partition("\n")
    m = re.match(r"# planted: (.+); keywords: (.+)", header)
    return {"source": f"bundled:{path.name}", "diff": diff, "context": "",
            "planted": m.group(1) if m else None, "keywords": [k.strip() for k in m.group(2).split(",")] if m else []}


def self_mutation_diff() -> dict | None:
    files = sorted(p for p in (SRC_ROOT / "agentlab").rglob("*.py") if p.stat().st_size > 800)
    random.shuffle(files)
    for path in files:
        original = path.read_text()
        name, pattern, repl = random.choice(MUTATIONS)
        matches = list(re.finditer(pattern, original))
        if not matches:
            continue
        m = random.choice(matches)
        mutated = original[:m.start()] + m.expand(repl) + original[m.end():]
        rel = path.relative_to(SRC_ROOT)
        diff = "".join(difflib.unified_diff(original.splitlines(True), mutated.splitlines(True),
                                            f"a/{rel}", f"b/{rel}", n=5))
        context = [f"### {rel} (after change)\n{mutated}"]
        for extra in random.sample([f for f in files if f != path], k=min(len(files) - 1, random.randint(0, 3))):
            context.append(f"### {extra.relative_to(SRC_ROOT)}\n{extra.read_text()}")
        return {"source": f"self:{rel}", "diff": diff, "context": "\n\n".join(context)[:MAX_CONTEXT_CHARS],
                "planted": name, "keywords": []}
    return None


async def run(run: Run) -> None:
    change = (random.random() < 0.5 and self_mutation_diff()) or bundled_diff()

    @function_tool
    def get_file(path: str) -> str:
        """Fetch a file from the repository under review (lab source tree only)."""
        run.tool_used()
        target = (SRC_ROOT / path).resolve()
        if SRC_ROOT.resolve() not in target.parents or not target.is_file() or target.suffix not in {".py", ".yaml", ".md"}:
            return "file not found"
        return target.read_text()[:20_000]

    # The review is returned through a tool call: LM Studio rejects the JSON-schema grammar the SDK
    # sends for structured output, while tool calling works reliably with this model.
    submitted: dict = {}

    @function_tool
    def submit_review(summary: str, verdict: Literal["approve", "request_changes", "comment"],
                      comments: list[Comment]) -> str:
        """Submit the finished review. Call exactly once, when you are done reviewing."""
        submitted["review"] = Review(summary=summary, verdict=verdict, comments=comments)
        return "review submitted"

    reviewer = Agent(
        name="Reviewer",
        model=OpenAIChatCompletionsModel(model=run.model, openai_client=run.openai_client()),
        model_settings=ModelSettings(max_tokens=12288, temperature=0.2, tool_choice="required"),
        tools=[get_file, submit_review],
        tool_use_behavior=StopAtTools(stop_at_tool_names=["submit_review"]),
        reset_tool_choice=False,
        instructions=("You are a senior engineer doing code review. Review the diff for correctness, security, "
                      "reliability and readability. Comment on specific lines of the changed files; do not nitpick "
                      "unchanged code. Request changes for anything that could break production. Use get_file only "
                      "if you need more context, then call submit_review with your review."),
    )
    prompt = f"Please review this change.\n\n```diff\n{change['diff']}\n```"
    if change["context"]:
        prompt += f"\n\nRepository context:\n\n{change['context']}"
    await Runner.run(reviewer, prompt, max_turns=6)
    if "review" not in submitted:
        raise RuntimeError("reviewer finished without submitting a review")
    review: Review = submitted["review"]

    found = None
    if change["keywords"]:
        text = (review.summary + " ".join(c.message for c in review.comments)).lower()
        found = any(k in text for k in change["keywords"])
    run.extra.update({"diff_source": change["source"], "planted_issue": change["planted"], "prompt_chars": len(prompt),
                      "verdict": review.verdict, "comments": len(review.comments), "planted_issue_found": found})
