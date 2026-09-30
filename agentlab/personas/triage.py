"""Ticket triage (PydanticAI): classify a synthetic support ticket, set priority, draft a reply."""
import random
from dataclasses import dataclass, field
from typing import Literal

from faker import Faker
from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from ..llm import Run

FRAMEWORK = "pydantic-ai"
fake = Faker()

PRODUCTS = ["Acme Cloud Backup", "Acme Payroll", "Acme CRM", "Acme Mobile Banking", "Acme Smart Thermostat"]

SCENARIOS = {
    "billing": "I was charged twice for {product} this month on my card ending {last4}. Please refund the duplicate.",
    "bug": "{product} shows a blank screen whenever I open the reports page since the latest update.",
    "outage": "I can't reach {product} at all since this morning. My whole team of {team} people is blocked.",
    "account_access": "I'm locked out of {product}. The password reset email never arrived at {email}.",
    "feature_request": "Could {product} export our data to CSV on a schedule? We'd pay extra for it.",
    "privacy": "Please delete all personal data {product} holds about me under GDPR. Account email: {email}.",
    "security": "I got an email claiming to be from {product} asking for my login details. Is this legit?",
}


class TriageResult(BaseModel):
    category: Literal["billing", "bug", "outage", "account_access", "feature_request", "privacy", "security", "other"]
    priority: Literal["P1", "P2", "P3", "P4"] = Field(description="P1 = business down / security, P4 = nice to have")
    sentiment: Literal["angry", "frustrated", "neutral", "positive"]
    contains_pii: bool
    pii_types: list[str] = Field(description="e.g. email, phone, address, card_last4, date_of_birth")
    summary: str = Field(description="one sentence")
    draft_reply: str = Field(description="reply to the customer; never repeat their personal data back")


@dataclass
class Deps:
    run: Run
    email: str
    plan: str = field(default_factory=lambda: random.choice(["Free", "Pro", "Business", "Enterprise"]))


INSTRUCTIONS = (
    "You are the tier-1 triage agent for Acme Software support. For each ticket: look up the customer, "
    "check the service status for the product mentioned, then classify the ticket, set a priority, "
    "flag any personal data it contains, and draft a short, friendly reply. Enterprise customers and "
    "outages or security issues get higher priority."
)

agent = Agent(output_type=TriageResult, deps_type=Deps, instructions=INSTRUCTIONS, retries=2)


@agent.tool
async def lookup_customer(ctx: RunContext[Deps], email: str) -> dict:
    """Look up a customer account by email address."""
    ctx.deps.run.tool_used()
    return {"email": email, "plan": ctx.deps.plan, "customer_since": str(fake.date_between("-6y", "-1m")),
            "open_tickets": random.randint(0, 4), "account_health": random.choice(["good", "at_risk", "churn_risk"])}


@agent.tool
async def check_service_status(ctx: RunContext[Deps], product: str) -> dict:
    """Get the current status of an Acme product from the status page."""
    ctx.deps.run.tool_used()
    degraded = random.random() < 0.25
    return {"product": product, "status": "degraded" if degraded else "operational",
            "incident": f"INC-{random.randint(1000, 9999)} elevated error rates" if degraded else None}


def make_ticket() -> tuple[str, str, str, str]:
    category = random.choice(list(SCENARIOS))
    email = fake.email()
    facts = {"product": random.choice(PRODUCTS), "last4": fake.credit_card_number()[-4:],
             "email": email, "team": random.randint(3, 80)}
    problem = SCENARIOS[category].format(**facts)
    signature = [fake.name()]
    if random.random() < 0.6:
        signature.append(fake.phone_number())
    if random.random() < 0.3:
        signature.append(fake.address().replace("\n", ", "))
    if random.random() < 0.15:
        signature.append(f"DOB {fake.date_of_birth(minimum_age=20, maximum_age=80)}")
    return category, email, problem, "\n".join(signature)


async def run(run: Run) -> None:
    category, email, problem, signature = make_ticket()
    model = OpenAIChatModel(run.model, provider=OpenAIProvider(openai_client=run.openai_client()))

    if random.random() < 0.5:
        # Half the tickets are written by the LLM itself for more varied wording.
        writer = Agent(instructions="You write realistic customer support emails. Output only the email body.")
        mood = random.choice(["furious", "polite", "confused", "terse", "rambling"])
        body = (await writer.run(f"Write a {mood} support email from a customer whose problem is: \"{problem}\" "
                                 f"Sign it exactly as:\n{signature}", model=model,
                                 model_settings={"max_tokens": 2048, "temperature": 0.9})).output
    else:
        body = f"Hi,\n\n{problem}\n\nThanks,\n{signature}"

    ticket = f"From: {email}\nSubject: {category.replace('_', ' ')} issue\n\n{body}"
    result = await agent.run(ticket, model=model, deps=Deps(run=run, email=email),
                             model_settings={"max_tokens": 4096, "temperature": 0.3})
    out = result.output
    run.extra.update({"ticket_scenario": category, "predicted_category": out.category,
                      "category_match": out.category == category, "priority": out.priority,
                      "pii_detected": out.pii_types, "ticket_chars": len(ticket)})
