"""Phase 9 acceptance: the full demo runs from the browser, including the inbox, the approval and
the trace viewer. Three people sign in to the real web app in separate browser sessions and play
the section 2 story against a live Setu with the demo agents running.

Skipped if Playwright's Chromium or Node is not available (`playwright install chromium`).
Set E2E_SCREENSHOTS=<dir> to keep screenshots of each step.
"""

import asyncio
import contextlib
import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from cross_department import run_demo
from playwright.async_api import Browser, BrowserContext, Page, async_playwright, expect
from setu_sdk.a2a_serve import free_port

from tests.conftest import LiveHub
from tests.helpers import PASSWORD, Acme
from tests.sdk_support import live_acme

pytestmark = pytest.mark.e2e
if SHOTS_DIR := os.environ.get("E2E_SCREENSHOTS"):
    Path(SHOTS_DIR).mkdir(parents=True, exist_ok=True)
WEB = Path(__file__).resolve().parents[3] / "web"
SHOTS = os.environ.get("E2E_SCREENSHOTS")
T = 30_000  # ms: generous, the story crosses four processes


def _chromium_missing() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return not Path(p.chromium.executable_path).exists()
    except Exception:
        return True


if shutil.which("npm") is None or os.environ.get("SKIP_E2E") or _chromium_missing():
    pytest.skip(
        "needs Node and Playwright's Chromium (playwright install chromium)",
        allow_module_level=True,
    )


@pytest.fixture(scope="module")
def web_url(live_hub: LiveHub) -> Iterator[str]:
    """The built web app, served by `vite preview`, proxying /v1 (and its WebSockets) to the hub."""
    build = ["npm", "run", "build"]
    if os.environ.get("E2E_UNMINIFIED"):  # readable stack traces when debugging
        build = ["npx", "vite", "build", "--minify", "false"]
    subprocess.run(build, cwd=WEB, check=True, capture_output=True)
    port = free_port()
    proc = subprocess.Popen(
        ["npx", "vite", "preview", "--port", str(port), "--strictPort", "--host", "127.0.0.1"],
        cwd=WEB,
        env={**os.environ, "SETU_API": live_hub.url},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.2):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("vite preview did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


class Person:
    """One person in their own browser session, with page errors collected."""

    def __init__(self, name: str, ctx: BrowserContext, page: Page, base: str) -> None:
        self.name, self.ctx, self.page, self.base = name, ctx, page, base
        self.errors: list[str] = []
        page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        page.on(
            "console",
            lambda m: (
                m.type == "error"
                and "Failed to load resource" not in m.text
                and self.errors.append(f"console: {m.text}")
            ),
        )

    async def login(self, email: str, password: str) -> None:
        p = self.page
        await p.goto(self.base)
        await p.get_by_label("Email").fill(email)
        await p.get_by_label("Password").fill(password)
        await p.get_by_role("button", name="Sign in").click()
        await expect(p.get_by_test_id("whoami")).to_be_visible(timeout=T)

    async def go(self, hash_: str) -> None:
        await self.page.evaluate(f"window.location.hash = '{hash_}'")

    async def shot(self, label: str) -> None:
        if SHOTS:
            await self.page.screenshot(path=f"{SHOTS}/{self.name}-{label}.png", full_page=True)


@pytest.fixture
def acme(live_hub: LiveHub) -> Acme:
    """Built in a sync fixture: the world builder bootstraps the org with asyncio.run."""
    return live_acme(live_hub.url, live_hub.database_url)


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        try:
            yield b
        finally:
            await b.close()


async def person(browser: Browser, name: str, base: str) -> Person:
    ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
    return Person(name, ctx, await ctx.new_page(), base)


async def test_the_demo_runs_from_the_browser(
    live_hub: LiveHub, acme: Acme, web_url: str, browser: Browser
) -> None:
    async with run_demo.environment(
        live_hub.url, f"admin-{acme.tag}@acme.test", PASSWORD, max_wait=90
    ) as s:
        olga = await person(browser, "olga", web_url)
        carl = await person(browser, "carl", web_url)
        ada = await person(browser, "ada", web_url)
        people = (olga, carl, ada)
        try:
            # ---- Olga signs in; the app knows her department and shows only what it allows.
            await olga.login(s.emails["olga"], s.password)
            nav = olga.page.get_by_label("Main")
            await expect(nav.get_by_text("Operations")).to_be_visible()
            await expect(nav.get_by_role("link", name="Audit")).to_have_count(0)  # not an auditor
            await olga.go("#/rooms")
            await olga.page.get_by_role("link", name=re.compile(f"claims-{s.tag}")).click()
            await expect(olga.page.get_by_test_id("live")).to_contain_text("live", timeout=T)

            # ---- The room shows its participants with their department badges.
            members = olga.page.get_by_test_id("members")
            for who, dept in (
                ("Triage-Agent", "Operations"),
                ("Risk-Agent", "Risk"),
                ("Compliance-Agent", "Compliance"),
            ):
                row = members.locator("li", has_text=who)
                await expect(row).to_contain_text(dept, timeout=T)
                await expect(row).to_contain_text("agent")
            await olga.shot("1-room")

            # ---- She mentions the Triage agent with the @ picker and sends the request.
            box = olga.page.get_by_label("Message")
            await box.click()
            await box.press_sequentially("@triage")
            options = olga.page.get_by_role("listbox", name="Mention suggestions")
            await expect(options).to_be_visible()
            await expect(options.get_by_role("option")).to_have_count(1)
            await box.press("Enter")  # picks the highlighted suggestion
            await expect(box).to_have_value(f"@{s.triage['handle']} ")
            await box.press_sequentially("Triage claim #123, amount 12500")
            await expect(olga.page.get_by_text("Addressing 1")).to_be_visible()
            await olga.page.get_by_role("button", name="Send").click()
            messages = olga.page.get_by_test_id("messages")
            mention = messages.locator("[data-mention]").first
            await expect(mention).to_have_text(f"@{s.triage['handle']}", timeout=T)  # highlighted

            # ---- A task card appears and waits: policy needs Compliance's sign-off.
            held = olga.page.locator('[data-testid="task-card"][data-state="input_required"]')
            await expect(held).to_have_count(1, timeout=T)
            await expect(held).to_contain_text("Risk-Agent")
            await olga.shot("2-waiting-for-approval")

            # ---- Carl (Compliance) is told live, opens his inbox and approves inline.
            await carl.login(s.emails["carl"], s.password)
            await expect(carl.page.get_by_test_id("unread-badge")).to_be_visible(timeout=T)
            await carl.go("#/inbox")
            asked = carl.page.locator(
                '[data-testid="notification"][data-event="approval.requested"]'
            )
            await expect(asked).to_have_count(1, timeout=T)
            await expect(asked).to_contain_text("Approval needed from Compliance")
            await expect(asked).to_contain_text("Your decision is needed")
            await carl.shot("3-inbox-approval")
            await asked.get_by_role("button", name="Approve").click()
            await expect(carl.page.get_by_role("status").filter(has_text="Approved")).to_be_visible(
                timeout=T
            )
            await expect(asked.get_by_text("Your decision is needed")).to_have_count(0, timeout=T)

            # ---- Back in the room, the work finishes live: two tasks, both completed, one answer.
            done = olga.page.locator('[data-testid="task-card"][data-state="completed"]')
            await expect(done).to_have_count(2, timeout=T)
            # Each card carries its artifact: Risk's assessment and Compliance's verdict.
            await expect(done.filter(has_text="risk-assessment")).to_have_count(1)
            await expect(done.filter(has_text="compliance-verdict")).to_have_count(1)
            await expect(done.filter(has_text="review_required")).to_have_count(2)  # Risk embeds it
            await expect(messages).to_contain_text("Claim #123: risk HIGH (83)", timeout=T)
            await expect(messages).to_contain_text("needs compliance review")
            await olga.shot("4-finished")

            # ---- The trace viewer shows one tree: who asked whom, policy, delegation, approval.
            await olga.page.get_by_role("tab", name="Work").click()  # task cards live in Work
            await olga.page.get_by_role("link", name="View trace").first.click()
            await expect(olga.page.get_by_test_id("trace-summary")).to_contain_text(
                "9 steps", timeout=T
            )
            tree = olga.page.get_by_test_id("trace-tree")
            kinds = await tree.locator('[data-testid="span"]').evaluate_all(
                "els => els.map(e => e.getAttribute('data-kind')).sort()"
            )
            assert kinds == sorted(
                ["message"] * 2
                + ["delegation"] * 2
                + ["policy"] * 2
                + ["approval"]
                + ["a2a_call"] * 2
            )
            await expect(tree).to_contain_text("require_approval")
            await expect(tree).to_contain_text("an allow_talk rule matches")
            await expect(tree.get_by_text("approved", exact=True).first).to_be_visible()
            second = tree.locator('[data-kind="delegation"]').nth(1)  # the Risk -> Compliance hop
            await second.get_by_role("button", name="Show delegation chain").click()
            chain = second.get_by_test_id("delegation-chain")
            await expect(chain.locator("li")).to_have_count(2, timeout=T)  # Olga -> Triage -> Risk
            await expect(chain).to_contain_text("Olga")
            await expect(chain).to_contain_text("Triage-Agent")
            await expect(chain).to_contain_text("Risk-Agent")
            await olga.shot("5-trace")

            # ---- Olga's inbox: unread count, the headline sentence, and mark-all-read.
            await olga.go("#/inbox")
            await expect(olga.page.get_by_test_id("inbox-unread")).to_be_visible(timeout=T)
            headline = "Risk-Agent finished risk.score for Triage-Agent. Result attached."
            await expect(
                olga.page.get_by_test_id("notification").filter(has_text=headline)
            ).to_have_count(1)
            await olga.page.get_by_role("button", name="Mark all read").click()
            await expect(olga.page.get_by_test_id("inbox-unread")).to_have_count(0, timeout=T)
            await expect(olga.page.get_by_test_id("unread-badge")).to_have_count(0)

            # ---- The agent registry: skills, owner, version, health.
            await olga.go("#/agents")
            for handle, skill, dept in (
                (s.risk["handle"], "risk.score", "Risk"),
                (s.compliance["handle"], "compliance.check", "Compliance"),
            ):
                card = olga.page.locator(f'[data-testid="agent"][data-handle="{handle}"]')
                await expect(card).to_contain_text(skill, timeout=T)
                await expect(card).to_contain_text(dept)
                await expect(card).to_contain_text("1.0")
                await expect(card).to_contain_text("ok")
                await expect(card).not_to_contain_text(
                    "127.0.0.1"
                )  # members never see the endpoint
            await expect(
                olga.page.get_by_text("Only department admins can register agents.")
            ).to_be_visible()
            await olga.shot("6-agents")

            # ---- Ada (auditor) sees the audit log and verifies the chain; policies are readable.
            await ada.login(s.emails["ada"], s.password)
            await ada.page.get_by_label("Main").get_by_role("link", name="Audit").click()
            await expect(ada.page.get_by_test_id("audit-row").first).to_be_visible(timeout=T)
            await ada.page.get_by_role("button", name="Verify chain").click()
            await expect(ada.page.get_by_test_id("verify-result")).to_contain_text(
                "Intact", timeout=T
            )
            await ada.shot("7-audit")
            await ada.go("#/policies")
            await expect(ada.page.get_by_test_id("policy")).to_have_count(3, timeout=T)
            await expect(
                ada.page.get_by_test_id("policy").filter(
                    has_text="Allow Operations to talk to Risk"
                )
            ).to_have_count(1)
            await expect(
                ada.page.get_by_text("Only org admins can change policies.")
            ).to_be_visible()
            # Policies are read-only for everyone but org admins; members get an explanation.
            await olga.go("#/policies")
            await expect(
                olga.page.get_by_text("Policies are visible to org admins and auditors.")
            ).to_be_visible()

            for p in people:
                assert p.errors == [], f"{p.name}: {p.errors}"
        finally:
            for p in people:
                if p.errors:
                    print(f"BROWSER ERRORS for {p.name}:", *p.errors, sep="\n  ")
                if p.page.url != "about:blank":
                    await p.shot("final")
                await p.ctx.close()


async def test_department_scoping_registration_and_the_policy_editor(
    live_hub: LiveHub, acme: Acme, web_url: str, browser: Browser
) -> None:
    from cross_department import compliance_agent

    from tests.sdk_support import Served

    async with run_demo.environment(
        live_hub.url, f"admin-{acme.tag}@acme.test", PASSWORD, max_wait=60
    ) as s:
        rita = await person(browser, "rita", web_url)  # Risk department admin
        admin = await person(browser, "admin", web_url)  # org admin
        try:
            # ---- Department scoping: the demo room belongs to Operations, so Risk's people
            #      do not see it, cannot open it, and are not told it exists by the app.
            await rita.login(s.emails["rita"], s.password)
            await rita.go("#/rooms")
            await expect(rita.page.get_by_text("No rooms yet")).to_be_visible(timeout=T)
            await rita.go(f"#/rooms/{s.room_id}")
            await expect(rita.page.get_by_role("alert")).to_contain_text(
                "room not found", timeout=T
            )
            await rita.shot("1-no-access")

            # ---- Registering an agent by Agent Card URL, through the three-step wizard.
            await rita.go("#/agents/new")
            port = free_port()
            second = compliance_agent.build_app(f"http://127.0.0.1:{port}")
            form = rita.page.get_by_label("Agent Card URL")
            with Served(second, port):
                await form.fill(f"http://127.0.0.1:{port}")
                await rita.page.get_by_role("button", name="Test connection").click()
                await expect(rita.page.get_by_test_id("connection-check")).to_contain_text(
                    "compliance.check", timeout=T
                )
                await rita.page.get_by_role("button", name="Continue").click()
                await rita.page.get_by_label("Handle (optional)").fill(f"second-check-{s.tag}")
                await rita.page.get_by_role("button", name="Continue").click()
                await rita.page.get_by_role("button", name="Register agent").click()
                card = rita.page.locator(
                    f'[data-testid="agent"][data-handle="second-check-{s.tag}"]'
                )
                await expect(card).to_contain_text("compliance.check", timeout=T)
                await expect(card).to_contain_text(
                    "Risk"
                )  # owned by the department she registered for
                await expect(card).to_contain_text(
                    "127.0.0.1"
                )  # her department's admin sees the endpoint
                await card.get_by_role("button", name="Refresh card").click()
                await expect(
                    rita.page.get_by_role("status").filter(has_text="card is unchanged")
                ).to_be_visible(timeout=T)
                await card.get_by_role("button", name="Disable").click()
                await expect(card).to_contain_text("disabled", timeout=T)
                await rita.shot("2-registered")
            # A card that cannot be fetched is refused with the reason, at the connection test.
            await rita.go("#/agents/new")
            await rita.page.get_by_label("Agent Card URL").fill(f"http://127.0.0.1:{free_port()}")
            await rita.page.get_by_role("button", name="Test connection").click()
            await expect(rita.page.get_by_role("alert")).to_contain_text("cannot reach", timeout=T)
            # Her own people hear about what happened to her department's agents.
            await rita.go("#/inbox")
            await expect(
                rita.page.get_by_test_id("notification").filter(has_text="was registered for Risk")
            ).to_have_count(2, timeout=T)

            # ---- The policy editor (org admin): add a rule, switch it off, delete it.
            await admin.login(f"admin-{acme.tag}@acme.test", PASSWORD)
            await admin.go("#/policies")
            policies = admin.page.get_by_test_id("policy")
            await expect(policies).to_have_count(3, timeout=T)
            await admin.page.get_by_label("Name", exact=True).fill("No Ops to Compliance")
            await admin.page.get_by_label("Type").select_option("deny_talk")
            await admin.page.get_by_label("From department").select_option(label="Operations")
            await admin.page.get_by_label("To department").select_option(label="Compliance")
            await admin.page.get_by_role("button", name="Add rule").click()
            mine = policies.filter(has_text="No Ops to Compliance")
            await expect(mine).to_have_count(1, timeout=T)
            await expect(mine).to_contain_text("Deny Operations talking to Compliance")
            await mine.get_by_role("button", name="Turn off").click()
            await expect(mine).to_contain_text("off", timeout=T)
            admin.page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
            await mine.get_by_role("button", name="Delete").click()
            await expect(policies).to_have_count(3, timeout=T)
            await admin.shot("3-policies")
            # The server judged every one of those changes, and the audit log recorded them.
            await admin.go("#/audit")
            await expect(
                admin.page.get_by_test_id("audit-row").filter(has_text="policy.created").last
            ).to_be_visible(timeout=T)
            await expect(
                admin.page.get_by_test_id("audit-row").filter(has_text="policy.deleted")
            ).to_have_count(1)

            for p in (rita, admin):
                assert p.errors == [], f"{p.name}: {p.errors}"
        finally:
            for p in (rita, admin):
                if p.errors:
                    print(f"BROWSER ERRORS for {p.name}:", *p.errors, sep="\n  ")
                await p.ctx.close()


async def test_home_teams_contacts_and_analytics(
    live_hub: LiveHub, acme: Acme, web_url: str, browser: Browser
) -> None:
    """The four pages added with the Band-style shell, as an org admin in a fresh org."""
    admin = await person(browser, "admin", web_url)
    try:
        await admin.login(f"admin-{acme.tag}@acme.test", PASSWORD)
        # Home is the landing page.
        await expect(
            admin.page.get_by_role("heading", name=re.compile("Welcome back"))
        ).to_be_visible(timeout=T)
        await expect(admin.page.get_by_text("You’re all caught up")).to_be_visible(timeout=T)

        # Contacts lists the people in the org.
        await admin.go("#/contacts")
        await expect(admin.page.get_by_test_id("contact").first).to_be_visible(timeout=T)

        # Create a team through the form, then it shows up with its member.
        await admin.go("#/teams")
        await expect(admin.page.get_by_text("No teams yet")).to_be_visible(timeout=T)
        await admin.page.get_by_role("button", name="New team").first.click()
        await admin.page.get_by_label("Team name").fill("Night shift")
        await admin.page.get_by_role("checkbox").first.check()
        await admin.page.get_by_role("button", name="Create team").click()
        team = admin.page.get_by_test_id("team")
        await expect(team).to_contain_text("Night shift", timeout=T)
        await expect(team).to_contain_text("1 member")

        # Analytics has nothing to chart yet and says so.
        await admin.go("#/analytics")
        await expect(admin.page.get_by_text("No tasks in this period")).to_be_visible(timeout=T)
        assert admin.errors == [], admin.errors
    finally:
        if admin.errors:
            print("BROWSER ERRORS:", *admin.errors, sep="\n  ")
        await admin.ctx.close()
