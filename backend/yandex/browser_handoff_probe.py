#!/usr/bin/env python3
"""Opt-in native Chromium regression for the production AISIS tab handoff.

Requires Python 3.11+, aiohttp, Playwright, the installed agenttools_chatgpt
package, and an explicitly supplied Chrome ELF executable. It uses a NEW
temporary isolated Chromium profile, fixture-only navigation and temporary SQLite files.
It launches its own Chrome ELF child with an ephemeral loopback CDP port and
connects only to the endpoint in that new profile's DevToolsActivePort file,
using production's connect_over_cdp(no_defaults=True). It never starts
CDPDriver, opens the user's profile, loads runtime.env, constructs a real
Guardian Store, or sends a prompt.

Run after reviewing the candidate pm_cdp_server.py, for example:
  python browser_handoff_probe.py --chrome /absolute/path/to/chrome

If headless Chromium keeps every tab visible, use headed Chromium inside a NEW
isolated Xvfb display (requires Xvfb, xvfb-run, and xauth):
  xvfb-run -a python browser_handoff_probe.py --headed --chrome /absolute/path/to/chrome
Never run --headed on an existing human DISPLAY. The genuine background-tab
guard remains required; the probe does not override visibility or history.
If host restrictions prevent Chrome's sandbox from starting, --no-sandbox is
an explicit opt-in for this isolated trusted-fixture child only. The default
retains Chrome's sandbox; no host policy or profile settings are changed.

The break this catches: a full six-tab browser cannot hand off its one owned
tab between saved conversations using native location.replace, or the handoff
changes target identity/history, touches a human tab, or accepts a draft race.
This proves native handoff mechanics against fixtures, not live ChatGPT UI,
account access, model selection, answer delivery, or physical speaker operation.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import html
import importlib.util
import json
import math
from pathlib import Path
import re
import sqlite3
import tempfile


class ProbeFailure(Exception):
    pass


def require(condition, code):
    if not condition:
        raise ProbeFailure(code)


def sha256(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


CONVERSATIONS = {
    "a": ("https://chatgpt.com/c/aisis-fixture-a", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          "11111111-1111-4111-8111-111111111111", "Saved fixture question A", "Saved fixture answer A"),
    "b": ("https://chatgpt.com/c/aisis-fixture-b", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
          "22222222-2222-4222-8222-222222222222", "Saved fixture question B", "Saved fixture answer B"),
    "c": ("https://chatgpt.com/c/aisis-fixture-c", "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
          "33333333-3333-4333-8333-333333333333", "Saved fixture question C", "Saved fixture answer C"),
}
HUMAN_URLS = [f"https://chatgpt.com/c/aisis-human-fixture-{index}" for index in range(5)]
HISTORY_URL = "https://chatgpt.com/c/aisis-history-fixture"
COMPOSER = '[contenteditable="true"][data-composer-markdown][role="textbox"][aria-label="Ask ChatGPT"]'
FIXTURE_CSP = ("default-src 'none'; style-src 'unsafe-inline'; connect-src 'none'; "
               "form-action 'none'; base-uri 'none'; frame-src 'none'")
HYDRATION_DELAY_MS = 150


def conversation_html(record):
    url, turn, assistant, prompt, answer = record
    conversation = url.rsplit("/", 1)[1]
    # These are the actual shared conversation-root and AISIS DOM contracts.
    # No root/visibility/navigation function is replaced by the probe.
    return f'''<!doctype html><html><head><meta charset="utf-8"><title>ChatGPT fixture</title>
<style>body {{font:16px sans-serif;margin:24px}} button {{padding:10px}}
section,article,div {{display:block}} [contenteditable] {{min-height:36px;border:1px solid}}</style>
</head><body>
<button aria-label="Select ChatGPT model" data-codex-intelligence-trigger="true"
 data-selected-reasoning-effort="Extra High">Thinking</button>
<button aria-label="Open profile menu">Profile</button>
<main><section data-chatgpt-conversation-selection-target="{conversation}"
 data-thread-find-target="conversation" data-chatgpt-selection-conversation-id="{conversation}">
<article data-turn-key="{turn}"><div data-user-message-bubble="true">{html.escape(prompt)}</div>
<div data-talvt-turn-state="complete">
<div data-chatgpt-search-unit-key="fixture:assistant" data-chatgpt-search-message-ids="{assistant}">
<div data-chatgpt-selection-message-id="{assistant}">
<div data-markdown-text-style="assistant-message">{html.escape(answer)}</div>
</div></div></div></article></section></main>
<div contenteditable="true" data-composer-markdown role="textbox" aria-label="Ask ChatGPT"></div>
</body></html>'''


def human_html(index):
    return f'''<!doctype html><html><head><meta charset="utf-8"><title>Human fixture {index}</title></head>
<body><main>Human page {index}: preserve this document.</main>
<textarea>Unsaved human fixture {index}</textarea></body></html>'''


def delayed_conversation_html(record):
    document = conversation_html(record)
    head, body = document.split("</head><body>", 1)
    markup = body.rsplit("</body></html>", 1)[0]
    serialized = json.dumps(markup).replace("<", "\\u003c")
    # Start the delay AT DOMContentLoaded, so reaching that lifecycle event does
    # not imply that ChatGPT's composer/history contract has hydrated. The marker
    # is diagnostics only; production page_for must discover the real DOM itself.
    script = f'''(() => {{
const diagnostic = window.__aisisFixtureHydration = {{ready:false, fromDOMContentLoaded:false,
  delayMs:{HYDRATION_DELAY_MS}, startedAt:null, readyAt:null}};
document.addEventListener('DOMContentLoaded', () => {{
  diagnostic.fromDOMContentLoaded = true;
  diagnostic.startedAt = performance.now();
  setTimeout(() => {{
    document.getElementById('fixture-pending').remove();
    document.body.insertAdjacentHTML('beforeend', {serialized});
    diagnostic.readyAt = performance.now();
    diagnostic.ready = true;
  }}, {HYDRATION_DELAY_MS});
}}, {{once:true}});
}})();'''
    allowed_hash = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    delayed = head + '</head><body><div id="fixture-pending">Loading fixture history</div><script>' + script + '</script></body></html>'
    return delayed, FIXTURE_CSP + "; script-src 'sha256-" + allowed_hash + "'"


class FixtureStore:
    """Only the handoff's control-query boundary, backed by a temporary DB."""
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE controls(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE attempts(chat TEXT, state TEXT);
            INSERT INTO controls VALUES('fixture:preserve', 'unchanged');
        """)
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def async_guard(self):
        async with self.lock:
            yield self.db

    def _api_allowed(self, db, chat, model, effort, verified):
        return not db.execute(
            "SELECT 1 FROM controls WHERE key IN ('stop:*', ?, 'astra_quota')",
            ("stop:" + chat,),
        ).fetchone() and not db.execute(
            "SELECT 1 FROM attempts WHERE chat=? AND state IN ('reserved','uncertain')", (chat,),
        ).fetchone()


async def human_state(driver, page):
    return {
        "target": await driver.target(page),
        "html_sha256": sha256(await page.content()),
        "state": await page.evaluate("""() => ({url:location.href, history:history.length,
            visibility:document.visibilityState, draft:document.querySelector('textarea').value})"""),
    }


async def exercise(server, context, ledger, store, root_script, dispatch):
    fixtures = {record[0]: conversation_html(record) for record in CONVERSATIONS.values()}
    delayed_fixtures = {record[0]: delayed_conversation_html(record) for record in CONVERSATIONS.values()}
    fixtures.update({url: human_html(index) for index, url in enumerate(HUMAN_URLS)})
    fixtures[HISTORY_URL] = "<!doctype html><html><head><title>History fixture</title></head><body>Earlier page</body></html>"
    document_requests, blocked_requests = [], []
    handoffs_started = False

    async def fulfill_fixture(route):
        request = route.request
        if request.method == "GET" and request.resource_type == "document" and request.url in fixtures:
            document_requests.append(request.url)
            body, policy = delayed_fixtures[request.url] if handoffs_started and request.url in delayed_fixtures else (fixtures[request.url], FIXTURE_CSP)
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body,
                headers={"Cache-Control": "no-store", "Content-Security-Policy": policy})
        else:
            blocked_requests.append((request.method, request.resource_type))
            await route.abort("blockedbyclient")

    await context.route("**/*", fulfill_fixture)
    require(not await context.cookies(), "temporary_profile_has_cookies")
    require(not context.service_workers, "temporary_profile_has_service_workers")
    require(len(context.pages) <= 1, "unexpected_initial_pages")
    humans = []
    for index, url in enumerate(HUMAN_URLS):
        page = context.pages[0] if index == 0 and context.pages else await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=10000)
        humans.append(page)
    worker = await context.new_page()
    await worker.goto(HISTORY_URL, wait_until="domcontentloaded", timeout=10000)
    await worker.goto(CONVERSATIONS["a"][0], wait_until="domcontentloaded", timeout=10000)
    await humans[-1].bring_to_front()
    # Chromium may retain the initial about:blank entry. Exercise a history of
    # more than one entry and preserve the actual native baseline throughout.
    history_length = await worker.evaluate("history.length")
    require(type(history_length) is int and history_length > 1,
            f"fixture_history_not_multiple:actual_count={history_length!r}")
    visibility = None
    try:
        async with asyncio.timeout(3):
            while True:
                visibility = await worker.evaluate("document.visibilityState")
                if visibility == "hidden":
                    break
                await asyncio.sleep(.05)
    except TimeoutError:
        pass
    require(visibility == "hidden",
            f"native_worker_is_not_background:visibility={visibility!r},history_count={history_length}")

    # Deliberately bypass lifecycle/real-state initialization, not the handoff.
    driver = server.CDPDriver.__new__(server.CDPDriver)
    driver.context, driver.ledger, driver.store = context, ledger, store
    driver.browser = context.browser
    driver.allowed, driver.max_tabs = {"a", "b", "c"}, 6
    driver.initial_target, driver.initial_used = "", True
    driver.script = server.DOM.replace("/*@conversation-root*/", root_script)
    driver.dispatch = dispatch
    owned_target = await driver.target(worker)
    for station, record in CONVERSATIONS.items():
        url, anchor, _, prompt, _ = record
        ledger.reserve({"station_id": station, "turn_id": "saved-" + station, "text": prompt})
        ledger.remember(station, owned_target if station == "a" else "fixture-missing-" + station,
                        url, anchor, sha256(prompt), navigation_state="ready")
        ledger.mark("saved-" + station, "complete")

    initial = await driver.snapshot(worker, expected_chat=CONVERSATIONS["a"][0])
    driver.transfer_snapshot(initial, ledger.session("a"))
    require(initial["root_match"] == "conversation-id", "shared_root_contract_not_exercised")
    require(initial["turns"][0]["assistant_id"] == CONVERSATIONS["a"][2], "assistant_dom_identity_not_exercised")
    baseline_humans = [await human_state(driver, page) for page in humans]
    baseline_targets = {await driver.target(page) for page in context.pages}
    require(len(baseline_targets) == 6, "fixture_does_not_have_six_unique_targets")
    controls_before = store.db.execute("SELECT * FROM controls ORDER BY key").fetchall()
    created, closed, human_navigations = [], [], []
    context.on("page", lambda page: created.append(page))
    for page in context.pages:
        page.on("close", lambda *args, tracked=page: closed.append(tracked))
    for index, page in enumerate(humans):
        page.on("framenavigated", lambda frame, tracked=page, number=index:
                human_navigations.append(number) if frame == tracked.main_frame else None)

    async def allocate(station, turn_id):
        ledger.reserve({"station_id": station, "turn_id": turn_id,
                        "text": "Fixture allocation only; stop before any prompt."})
        session = ledger.session(station)

        def remember(target, url, *args, **kwargs):
            ledger.remember(station, target, url, *args, **kwargs)

        return await driver.page_for(session, remember, station=station, turn_id=turn_id)

    async def unchanged_tabs():
        require(not created and not closed, "handoff_created_or_closed_a_tab")
        require(len(context.pages) == 6, "handoff_changed_tab_count")
        require({await driver.target(page) for page in context.pages} == baseline_targets, "native_target_set_changed")
        require(not human_navigations, "human_page_was_navigated")
        require([await human_state(driver, page) for page in humans] == baseline_humans, "human_page_state_changed")
        current_history = await worker.evaluate("history.length")
        require(current_history == history_length,
                f"native_replace_changed_history_length:baseline={history_length},actual={current_history}")
        require(await driver.target(worker) == owned_target, "owned_native_target_changed")

    sequence_start = len(document_requests)
    handoffs_started = True
    hydration_diagnostics = []
    for index, station in enumerate(("b", "c", "a", "b")):
        page, target = await allocate(station, f"allocation-{index}")
        require(page is worker and target == owned_target, "handoff_did_not_reuse_owned_page")
        require(page.url == CONVERSATIONS[station][0], "wrong_saved_conversation_loaded")
        # Read only AFTER production page_for returns; never await a fixture
        # readiness promise or marker in place of production readiness checks.
        hydration = await page.evaluate("window.__aisisFixtureHydration || null")
        require(isinstance(hydration, dict) and hydration.get("ready") is True
                and hydration.get("fromDOMContentLoaded") is True
                and hydration.get("delayMs") == HYDRATION_DELAY_MS, "returned_before_fixture_hydration")
        hydration_diagnostics.append(hydration)
        require(ledger.session(station)["navigation_state"] == "ready", "handoff_not_settled")
        require(ledger.db.execute("SELECT station FROM sessions WHERE target=?", (owned_target,)).fetchall()
                == [(station,)], "native_target_has_wrong_ledger_owner")
        require(ledger.db.execute("SELECT state FROM tab_transfers WHERE target=?", (owned_target,)).fetchone()
                == ("complete",), "handoff_claim_not_completed")
        for name, record in CONVERSATIONS.items():
            session = ledger.session(name)
            require((session["url"], session["anchor_turn"], session["anchor_digest"])
                    == (record[0], record[1], sha256(record[3])), "saved_conversation_lineage_changed")
        # We test allocation, not generation. End this fixture request before
        # dispatch; the prior genuine fixture completion remains its anchor.
        ledger.mark(f"allocation-{index}", "failed")
        await unchanged_tabs()
    require(document_requests[sequence_start:] == [CONVERSATIONS[name][0] for name in ("b", "c", "a", "b")],
            "handoff_repeated_or_added_navigation")

    # A draft appears AFTER Python's snapshot/claim but BEFORE the actual native
    # JS boundary. Keep the real evaluate/location implementation and require a
    # durable unresolved claim, rather than retrying or declaring a handoff done.
    async def introduce_draft(operation):
        try:
            await worker.evaluate("selector => {document.querySelector(selector).textContent = 'Unsaved fixture draft';}", COMPOSER)
        except BaseException:
            operation.close()
            raise
        return await dispatch(operation)

    driver.dispatch = introduce_draft
    requests_before_guard = list(document_requests)
    try:
        await allocate("c", "guarded-allocation")
    except server.Refusal as error:
        require(error.code == "tab_transfer_refused", "unsafe_draft_failed_at_wrong_boundary")
    else:
        raise ProbeFailure("native_guard_accepted_a_changed_draft")
    require(worker.url == CONVERSATIONS["b"][0], "unsafe_draft_navigated")
    require(document_requests == requests_before_guard, "unsafe_draft_started_a_document_request")
    require(await worker.locator(COMPOSER).text_content() == "Unsaved fixture draft", "unsafe_draft_was_modified")
    require(ledger.transfer_pending("b") and ledger.transfer_pending("c"), "unsafe_handoff_not_durably_blocked")
    require(ledger.db.execute("SELECT state FROM tab_transfers WHERE target=?", (owned_target,)).fetchone()
            == ("pending",), "unsafe_claim_was_declared_complete")
    await unchanged_tabs()
    require(store.db.execute("SELECT * FROM controls ORDER BY key").fetchall() == controls_before, "fixture_controls_changed")
    require(not await context.cookies(), "fixture_created_cookies")
    require(not context.service_workers, "fixture_created_service_workers")
    require(all(method == "GET" for method, _ in blocked_requests), "unexpected_non_get_request")
    return {"status": "passed", "sequence": ["a", "b", "c", "a", "b"],
            "native_target_retained": True, "history_length": history_length, "unchanged_human_pages": 5,
            "new_tabs_during_handoffs": len(created), "closed_tabs_during_handoffs": len(closed),
            "native_draft_race_refused": True, "unsafe_claim": "pending", "prompts_sent": 0,
            "fixture_document_requests": len(document_requests), "other_requests_blocked": len(blocked_requests),
            "delayed_destinations": len(hydration_diagnostics), "hydration_delay_ms": HYDRATION_DELAY_MS,
            "observed_hydration_ms": [round(item["readyAt"] - item["startedAt"], 3) for item in hydration_diagnostics]}


async def owned_cdp_endpoint(process, profile):
    """Read only this newly created profile; never discover an existing browser."""
    active_port = profile / "DevToolsActivePort"
    async with asyncio.timeout(15):
        while True:
            require(process.returncode is None,
                    f"owned_chrome_exited_before_cdp:exit={process.returncode}")
            try:
                content = active_port.read_bytes()
            except FileNotFoundError:
                await asyncio.sleep(.05)
                continue
            require(not active_port.is_symlink() and len(content) <= 1024, "invalid_owned_cdp_file")
            lines = content.decode("ascii").splitlines()
            if len(lines) < 2:
                await asyncio.sleep(.05)
                continue
            require(len(lines) == 2 and re.fullmatch(r"[0-9]{1,5}", lines[0]) is not None,
                    "invalid_owned_cdp_port")
            port = int(lines[0])
            require(1 <= port <= 65535, "invalid_owned_cdp_port")
            require(re.fullmatch(r"/devtools/browser/[A-Za-z0-9_-]+", lines[1]) is not None,
                    "invalid_owned_cdp_browser_path")
            return f"ws://127.0.0.1:{port}{lines[1]}"


async def close_owned_chrome(browser, process):
    """At most eight seconds; signals address only our captured child PID."""
    if process is None:
        return
    if browser is not None and process.returncode is None:
        try:
            async with asyncio.timeout(2):
                session = await browser.new_browser_cdp_session()
                # Browser.close disposes this browser's sessions and child tabs.
                await session.send("Browser.close")
        except Exception:
            pass
    for operation, seconds in ((None, 2), ("terminate", 3), ("kill", 1)):
        if process.returncode is not None:
            return
        if operation is not None:
            try:
                getattr(process, operation)()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), seconds)
            return
        except TimeoutError:
            pass
    raise ProbeFailure("owned_chrome_cleanup_timeout")


async def run(args):
    chrome = Path(args.chrome).expanduser().resolve(strict=True)
    with chrome.open("rb") as executable:
        require(executable.read(4) == b"\x7fELF", "chrome_must_be_an_explicit_elf_binary_not_a_profile_wrapper")
    source = Path(args.server).expanduser().resolve(strict=True)
    source_hash = sha256(source.read_bytes())
    spec = importlib.util.spec_from_file_location("aisis_handoff_server_under_test", source)
    require(spec is not None and spec.loader is not None, "server_module_unavailable")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    from agenttools_chatgpt import scripts
    from agenttools_chatgpt.core import protected_dispatch
    from playwright.async_api import async_playwright
    root_script = (Path(scripts.__file__).parent / "conversation_root.js").read_text()

    with tempfile.TemporaryDirectory(prefix="aisis-native-handoff-") as temporary:
        directory = Path(temporary)
        profile = directory / "chromium-profile"
        profile.mkdir(mode=0o700)
        ledger = server.Ledger(directory / "pm.sqlite3")
        store = FixtureStore(directory / "fixture-guardian.sqlite3")
        process = playwright = browser = None
        try:
            async with asyncio.timeout(args.timeout):
                require(not list(profile.iterdir()), "chrome_profile_is_not_new")
                command = [str(chrome), "--user-data-dir=" + str(profile),
                           "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
                           "--disable-background-networking", "--disable-component-update",
                           "--disable-component-extensions-with-background-pages",
                           "--disable-extensions", "--disable-default-apps", "--disable-sync",
                           "--no-first-run", "--no-default-browser-check",
                           "--host-resolver-rules=MAP * ~NOTFOUND",
                           "--proxy-server=http://127.0.0.1:9", "--proxy-bypass-list=<-loopback>"]
                if not args.headed:
                    command.append("--headless=new")
                if args.no_sandbox:
                    command.append("--no-sandbox")
                command.append("about:blank")
                process = await asyncio.create_subprocess_exec(*command,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
                endpoint = await owned_cdp_endpoint(process, profile)
                playwright = await async_playwright().start()
                browser = await playwright.chromium.connect_over_cdp(endpoint, timeout=10000, no_defaults=True)
                require(len(browser.contexts) == 1, "owned_browser_context_ambiguous")
                context = browser.contexts[0]
                require(all(page.url == "about:blank" for page in context.pages), "unexpected_initial_fixture_url")
                context.set_default_timeout(5000)
                result = await exercise(server, context, ledger, store, root_script, protected_dispatch)
        finally:
            try:
                await close_owned_chrome(browser, process)
            finally:
                try:
                    if playwright is not None:
                        await asyncio.wait_for(playwright.stop(), 2)
                finally:
                    store.db.close()
                    ledger.db.close()
    require(sha256(source.read_bytes()) == source_hash, "server_source_changed_during_probe")
    result.update(server_sha256=source_hash, shared_root_sha256=sha256(root_script),
                  temporary_profile_removed=not directory.exists(), headless=not args.headed)
    require(result["temporary_profile_removed"], "temporary_profile_not_removed")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chrome", required=True, help="Explicit Chrome ELF executable, never the managed-profile wrapper")
    parser.add_argument("--headed", action="store_true",
                        help="Use only inside a NEW isolated xvfb-run -a display, never an existing human DISPLAY")
    parser.add_argument("--no-sandbox", action="store_true",
                        help="Explicitly disable the sandbox only for this isolated trusted-fixture Chrome child; default keeps it enabled")
    parser.add_argument("--server", default=str(Path(__file__).with_name("pm_cdp_server.py")),
                        help="Reviewed production module under test; defaults to the adjacent file")
    parser.add_argument("--timeout", type=float, default=90, help="Whole launch/probe budget in seconds (10..180); cleanup has 10s")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or not 10 <= args.timeout <= 180:
        parser.error("--timeout must be finite and between 10 and 180 seconds")
    try:
        result = asyncio.run(run(args))
    except Exception as error:
        print(json.dumps({"status": "failed", "type": type(error).__name__, "detail": str(error)[:1500]}), flush=True)
        return 1
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
