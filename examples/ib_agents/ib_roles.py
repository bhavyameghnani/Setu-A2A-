"""The investment-banking demo: departments, people and agents. Everything here is fictional and
illustrative (a made-up client, made-up people); nothing is real bank data or advice."""

from dataclasses import dataclass

DISCLAIMER = (
    "This is a fictional demo environment. Use only the facts in the request, invent nothing "
    "about real firms or people, and end with the line 'Illustrative only, not advice.'"
)


@dataclass(frozen=True)
class Role:
    key: str  # also the agent's handle in Setu
    name: str
    port: int
    dept: str  # department slug
    skill: str
    skill_name: str
    system: str
    peer: str | None = None  # key of the agent this one consults through Setu, if any


ROLES: dict[str, Role] = {
    r.key: r
    for r in (
        Role(
            "valuation-analyst",
            "Valuation-Analyst",
            9301,
            "finance",
            "valuation.analyse",
            "Valuation analysis",
            "You are a valuation analyst in the Finance department of an investment bank. Given a "
            "deal or instrument, outline the valuation approach (comparables, DCF, precedent "
            "transactions), key assumptions and sensitivities, in at most 8 short sentences. "
            + DISCLAIMER,
            peer="credit-risk",
        ),
        Role(
            "credit-risk",
            "Credit-Risk-Analyst",
            9302,
            "finance",
            "credit.assess",
            "Counterparty credit assessment",
            "You are a counterparty credit risk analyst in Finance. Given a counterparty and a "
            "proposed exposure, give a brief credit view: rating drivers, exposure and limit "
            "considerations, and mitigants (collateral, netting), as at most 5 bullets. "
            + DISCLAIMER,
        ),
        Role(
            "contract-reviewer",
            "Contract-Reviewer",
            9303,
            "legal",
            "contract.review",
            "Contract review",
            "You are a contract reviewer in the Legal department. Given a clause or agreement "
            "type (ISDA schedule, NDA, engagement letter), flag the risky terms (termination, "
            "set-off, governing law, indemnity, confidentiality scope) and suggest redlines, in "
            "at most 6 short bullets. " + DISCLAIMER,
            peer="conflicts-checker",
        ),
        Role(
            "kyc-screener",
            "KYC-AML-Screener",
            9304,
            "compliance",
            "kyc.screen",
            "KYC and AML screening",
            "You are a KYC/AML screener in Compliance. Given a client onboarding request, list "
            "what must be verified (beneficial owners, sanctions and PEP screening, source of "
            "funds, jurisdiction risk), mark an overall risk level LOW, MEDIUM or HIGH with a "
            "one-line reason, and list the documents still missing, as at most 6 bullets. "
            + DISCLAIMER,
        ),
        Role(
            "conflicts-checker",
            "MNPI-Conflicts-Checker",
            9305,
            "compliance",
            "conflicts.check",
            "MNPI and conflicts check",
            "You are a conflicts and information-barrier analyst in Compliance. Given a proposed "
            "engagement or draft, say whether it touches material non-public information, a "
            "possible restricted-list name, or a conflict between divisions, and what the "
            "information-barrier steps would be, in at most 5 bullets. " + DISCLAIMER,
        ),
    )
}

DEPARTMENTS = [("finance", "Finance"), ("legal", "Legal"), ("compliance", "Compliance")]

# dept slug -> (admin, member): (handle, display name)
PEOPLE = {
    "finance": (("fiona", "Fiona Fairbank"), ("finn", "Finn Marsh")),
    "legal": (("leila", "Leila Okafor"), ("luca", "Luca Bianchi")),
    "compliance": (("carmen", "Carmen Reyes"), ("colin", "Colin Wright")),
}
PASSWORD = "demo-password-123"  # dev only
ROOM = "Project Falcon: client onboarding"
ROOM_TOPIC = "Onboarding Falcon Industries (fictional): valuation, contracts and checks"
