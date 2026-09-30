"""Customer <-> support conversation (OpenAI Agents SDK): two agents, two keys, 3-8 customer turns.

The customer gets a random persona and goal; the support agent has tools over a fake per-run CRM.
"""
import random

from agents import Agent, ModelSettings, OpenAIChatCompletionsModel, Runner, function_tool, set_tracing_disabled
from faker import Faker

from ..llm import Run

FRAMEWORK = "openai-agents"
set_tracing_disabled(True)
fake = Faker()

TEMPERAMENTS = ["impatient and annoyed", "polite but confused", "terse", "chatty and off-topic", "anxious", "skeptical"]
GOALS = [
    "get a refund for order {order} because the item arrived damaged",
    "find out where order {order} is; it is a week late",
    "cancel your subscription and make sure you are not billed again",
    "change the shipping address on order {order} before it ships",
    "reset your password because you are locked out",
    "complain that you were charged twice and get one charge reversed",
    "ask whether the premium plan includes phone support before upgrading",
]


def build_crm(email: str) -> dict:
    orders = {}
    for _ in range(random.randint(1, 3)):
        oid = f"ORD-{random.randint(100000, 999999)}"
        orders[oid] = {"item": random.choice(["wireless headphones", "standing desk", "coffee grinder", "4K monitor",
                                              "hiking backpack"]),
                       "amount": round(random.uniform(20, 900), 2),
                       "status": random.choice(["processing", "shipped", "delivered", "delayed"]),
                       "ship_to": fake.address().replace("\n", ", ")}
    return {"email": email, "plan": random.choice(["basic", "plus", "premium"]), "orders": orders}


def support_tools(run: Run, crm: dict):
    @function_tool
    def lookup_account(email: str) -> dict:
        """Look up a customer account and their orders by email."""
        run.tool_used()
        return crm if email.lower() == crm["email"].lower() else {"error": "no account with that email"}

    @function_tool
    def lookup_order(order_id: str) -> dict:
        """Get the details and shipping status of an order."""
        run.tool_used()
        return crm["orders"].get(order_id.strip().upper(), {"error": "order not found"})

    @function_tool
    def issue_refund(order_id: str, amount: float, reason: str) -> dict:
        """Refund all or part of an order. Refunds over $500 need supervisor approval."""
        run.tool_used()
        run.extra["actions"].append("refund")
        if amount > 500:
            return {"status": "pending_approval", "ticket": f"SUP-{random.randint(1000, 9999)}"}
        return {"status": "refunded", "order_id": order_id, "amount": amount}

    @function_tool
    def update_shipping_address(order_id: str, new_address: str) -> dict:
        """Change the shipping address on an order that has not shipped yet."""
        run.tool_used()
        run.extra["actions"].append("address_change")
        order = crm["orders"].get(order_id.strip().upper())
        if not order:
            return {"error": "order not found"}
        return {"status": "updated"} if order["status"] == "processing" else {"error": "order already shipped"}

    @function_tool
    def send_password_reset(email: str) -> dict:
        """Send a password reset link to the account email."""
        run.tool_used()
        run.extra["actions"].append("password_reset")
        return {"status": "sent", "to": email}

    @function_tool
    def create_ticket(summary: str, priority: str) -> dict:
        """Escalate to a human specialist. priority: low, normal or high."""
        run.tool_used()
        run.extra["actions"].append("escalated")
        return {"ticket": f"SUP-{random.randint(1000, 9999)}", "priority": priority}

    return [lookup_account, lookup_order, issue_refund, update_shipping_address, send_password_reset, create_ticket]


async def run(run: Run) -> None:
    name, email = fake.name(), fake.email()
    crm = build_crm(email)
    goal = random.choice(GOALS).format(order=random.choice(list(crm["orders"])))
    temperament = random.choice(TEMPERAMENTS)
    turns = random.randint(3, 8)
    run.extra.update({"actions": [], "customer_goal": goal, "temperament": temperament, "planned_turns": turns})

    settings = ModelSettings(max_tokens=3072, temperature=0.7)
    customer = Agent(
        name="Customer",
        model=OpenAIChatCompletionsModel(model=run.model, openai_client=run.openai_client("customer")),
        model_settings=settings,
        instructions=(f"You are {name} ({email}), a customer of an online store, chatting with support. You are "
                      f"{temperament}. Your goal: {goal}. Write only your next chat message (under 70 words), in "
                      "character. Give details like your email or order number only when asked. When your issue is "
                      "resolved, or when told it is your last message, wrap up politely or not."),
    )
    support = Agent(
        name="Support",
        model=OpenAIChatCompletionsModel(model=run.model, openai_client=run.openai_client("support")),
        model_settings=settings,
        tools=support_tools(run, crm),
        instructions=("You are a support agent for an online store. Verify the customer's email before changing "
                      "anything on their account, use the tools to look things up and act, and keep replies short "
                      "and friendly. Escalate with create_ticket when you cannot resolve something yourself."),
    )

    customer_view: list = [{"role": "user", "content": "You have just opened the support chat. Write your first message."}]
    support_view: list = []
    transcript_chars = 0
    for turn in range(1, turns + 1):
        message = (await Runner.run(customer, customer_view, max_turns=2)).final_output
        support_result = await Runner.run(support, support_view + [{"role": "user", "content": message}], max_turns=10)
        support_view = support_result.to_input_list()
        reply = support_result.final_output
        transcript_chars += len(message) + len(reply)
        note = " This is your last message." if turn == turns - 1 else ""
        customer_view += [{"role": "assistant", "content": message},
                          {"role": "user", "content": f"Support: {reply}{note}"}]

    run.extra.update({"turns": turns, "transcript_chars": transcript_chars})
