"""Smoke-test the web test client (spec §7) the way playtesters use it:
open /dev/client from phone-sized browsers, type a name, tick a category,
press Create and get a join code; join from a second browser context; start
the game with min_players 2; pick, answer one question, see the reveal.
Failures must be visible on the page, so the run also checks that a bad join
code and a create with no category each put an error on screen.

    cd api && python scripts/smoke_dev_client.py            # starts its own API
    cd api && python scripts/smoke_dev_client.py --base http://192.168.1.20:8001
    cd api && python scripts/smoke_dev_client.py --engine webkit   # iPhone-ish

One-time setup (Playwright is pinned in requirements.txt):

    pip install -r requirements.txt && playwright install chromium webkit

Without --base the script starts a second API on the `<DATABASE_URL db>_test`
scratch database (created, migrated and seeded with the six categories and
synthetic live questions if needed — never the dev database), bound to
0.0.0.0, and fetches the page at http://<LAN IP>:<port>/dev/client exactly
like a phone on the same Wi-Fi would. It stops that API at the end. With
--base it drives whatever is already serving there.

Every console message, page error and API request/response of every tab is
recorded and printed on failure (or always with --verbose). A console error,
an uncaught exception (the page's red banner), a non-2xx API response, a
missing join code or a phase that never arrives fails the run: exit 1.
Exit status 0 means the page works end to end.
"""
import argparse
import asyncio
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.sync_api import Browser, Page, Playwright, sync_playwright  # noqa: E402

API_DIR = Path(__file__).resolve().parent.parent
SYNTHETIC_QUESTIONS = 60  # ~10 per category: enough for question_count 3 with one category
GAME_OVERRIDES = {
    "min_players": 2,
    "question_count": 3,
    "pick_seconds": 3,
    "question_seconds": 10,
    "reveal_seconds": 2,
    "attack_window_seconds": 2,
    "block_seconds": 3,
}


# ---------------------------------------------------------------- server ----
def lan_ip() -> str:
    """The address a phone on the same Wi-Fi would use (not 127.0.0.1)."""
    try:
        out = subprocess.run(
            ["ipconfig", "getifaddr", "en0"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if out:
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    # Portable fallback: the interface that routes to the internet.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]


def wait_http(url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.25)
    return False


def start_api(port: int, log_path: Path) -> tuple[subprocess.Popen, str]:
    """Second API on the seeded test DB, bound to 0.0.0.0. Returns (process, db url)."""
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Question
    from synthetic_serves import (
        SYNTHETIC_TAG,
        ensure_categories,
        ensure_test_database,
        make_questions,
        test_database_url,
    )

    url = test_database_url()
    ensure_test_database(url)  # create + migrate; alembic runs its own loop

    async def seed() -> None:
        engine = create_async_engine(url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                categories = await ensure_categories(db)
                have = await db.scalar(
                    select(func.count()).select_from(Question).where(Question.tags.any(SYNTHETIC_TAG))
                )
                if have < SYNTHETIC_QUESTIONS:
                    await make_questions(db, categories, SYNTHETIC_QUESTIONS - have, random.Random(1))
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(seed())

    env = {**os.environ, "DATABASE_URL": url, "ENV": "dev"}
    log = log_path.open("w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", str(port)],
        cwd=API_DIR,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    return proc, url


# --------------------------------------------------------------- browser ----
@dataclass
class Tab:
    name: str
    page: Page
    console: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    bad_responses: list[str] = field(default_factory=list)

    def report(self) -> str:
        lines = [f"--- {self.name}: {len(self.requests)} API requests ---", *self.requests]
        if self.console:
            lines += [f"--- {self.name}: console ---", *self.console]
        if self.errors:
            lines += [f"--- {self.name}: page errors ---", *self.errors]
        return "\n".join(lines)

    def text(self, selector: str) -> str:
        return (self.page.text_content(selector) or "").strip()

    def phase(self) -> str:
        return self.text("#g-phase")

    def wait_phase(self, *phases: str, timeout: float = 20_000) -> str:
        self.page.wait_for_function(
            "phases => phases.includes(document.querySelector('#g-phase').textContent)",
            arg=list(phases),
            timeout=timeout,
        )
        return self.phase()


def open_tab(browser: Browser, pw: Playwright, engine: str, base: str, name: str) -> Tab:
    """Each tab is its own browser context: separate sessionStorage, like another phone."""
    if engine == "webkit":
        ctx = browser.new_context(**pw.devices["iPhone 13"])
    else:
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    tab = Tab(name, page)
    page.on("console", lambda m: tab.console.append(f"[{m.type}] {m.text}"))
    page.on("pageerror", lambda e: tab.errors.append(str(e)))

    def on_request(r):
        if not r.url.endswith("/dev/client"):
            tab.requests.append(f"→ {r.method} {r.url}")

    def on_response(r):
        if r.url.endswith("/dev/client"):
            return
        tab.requests.append(f"← {r.status} {r.url}")
        if r.status >= 400:
            tab.bad_responses.append(f"{r.status} {r.request.method} {r.url}")

    page.on("request", on_request)
    page.on("response", on_response)
    page.goto(base + "/dev/client", wait_until="load")
    return tab


def create_game(tab: Tab, name: str, *, host_first: bool, overrides: dict | None = None) -> str:
    """Create a game through the UI and return the join code shown in the lobby."""
    page = tab.page
    if not host_first:
        page.fill("#name", name)
    page.click("#btn-to-create")
    page.wait_for_selector("#cats input", timeout=10_000)  # categories loaded
    if host_first:
        page.fill("#name", name)
    page.locator("#cats input").first.check()
    if overrides:
        page.fill("#overrides", json.dumps(overrides))
    page.click("#btn-create")
    try:
        page.wait_for_selector("#game:not([hidden])", timeout=15_000)
    except Exception:
        raise AssertionError(
            f"{tab.name}: Create did not reach the lobby; create-msg={tab.text('#create-msg')!r}"
        ) from None
    code = tab.text("#lobby-code")
    if len(code) < 4:
        raise AssertionError(f"{tab.name}: no join code in #lobby-code (got {code!r})")
    page.wait_for_function("document.querySelector('#d-conn').textContent === 'open'", timeout=10_000)
    return code


def join_game(tab: Tab, name: str, code: str) -> None:
    page = tab.page
    page.fill("#name", name)
    page.fill("#joincode", code)
    page.click("#btn-join")
    try:
        page.wait_for_selector("#game:not([hidden])", timeout=15_000)
    except Exception:
        raise AssertionError(
            f"{tab.name}: Join did not reach the lobby; home-msg={tab.text('#home-msg')!r}"
        ) from None
    page.wait_for_function("document.querySelector('#d-conn').textContent === 'open'", timeout=10_000)


def roster(tab: Tab) -> list[str]:
    return [t.strip() for t in tab.page.locator("#roster li").all_text_contents()]


def play_one_question(tabs: list[Tab]) -> list[str]:
    """Start → pick → question → both answer → the server acknowledges. Returns log lines."""
    out = []
    host = tabs[0]
    host.page.click("#panel button:has-text('Start game')")
    for t in tabs:
        t.wait_phase("Pick a category")
    # Only the picker's board is enabled; the other tab just waits. If nobody
    # manages to click within pick_seconds the server auto-picks, which is fine.
    for t in tabs:
        try:
            board = t.page.locator("#panel button:not([disabled])").first
            label = board.text_content(timeout=1_500) or ""
            board.click(timeout=1_500)
            out.append(f"{t.name} picked {label.strip()!r}")
            break
        except Exception:
            continue
    for t in tabs:
        t.wait_phase("Question")
        stem = t.text("#panel .big")
        t.page.locator("#panel button:not([disabled])").first.click()
        # The panel shows "Answer accepted." only until the reveal, which comes
        # the moment everyone has answered — so read the ack from the event log.
        try:
            t.page.wait_for_function(
                """() => { const l = document.querySelector('#log').textContent;
                          return l.includes('"type":"answer_ack"') && l.includes('"accepted":true'); }""",
                timeout=10_000,
            )
        except Exception:
            raise AssertionError(
                f"{t.name}: answer was not acknowledged; phase {t.phase()!r}, conn {t.text('#d-conn')!r}, "
                f"panel: {t.text('#panel')[:300]!r}\n  event log:\n    " + "\n    ".join(t.text('#log').split("\n")[:12])
            ) from None
        out.append(f"{t.name} answered {stem[:55]!r} → accepted")
    for t in tabs:
        t.wait_phase("Reveal", "Attack window", "Block", "Pick a category", "Question", "Game over")
        t.page.wait_for_function(
            "document.querySelector('#log').textContent.includes('\"type\":\"reveal\"')", timeout=15_000
        )
        out.append(f"{t.name} saw the reveal · now {t.phase()!r} · {t.text('#g-you')} · rtt {t.text('#d-rtt')} offset {t.text('#d-off')}")
    return out


def check_visible_errors(tab: Tab) -> list[str]:
    """Failures must show on the page: a bad join code, a create without a category."""
    out = []
    page = tab.page
    page.fill("#name", "Nobody")
    page.fill("#joincode", "ZZZZZZ")
    page.click("#btn-join")
    page.wait_for_function(
        "['', 'Joining…'].includes(document.querySelector('#home-msg').textContent) === false", timeout=10_000
    )
    msg = tab.text("#home-msg")
    out.append(f"bad join code shows: {msg!r}")
    if "404" not in msg:
        raise AssertionError(f"{tab.name}: bad join code did not show a 404 error, got {msg!r}")
    # That 404 was the point: drop it from the failure scan (Chromium also
    # logs a console error for every 4xx fetch).
    tab.bad_responses.clear()
    tab.console[:] = [c for c in tab.console if "404" not in c]

    page.click("#btn-to-create")
    page.wait_for_selector("#cats input", timeout=10_000)
    page.click("#btn-create")
    msg = tab.text("#create-msg")
    out.append(f"create without a category shows: {msg!r}")
    if "category" not in msg:
        raise AssertionError(f"{tab.name}: create with no category did not complain, got {msg!r}")
    return out


def run(base: str, engine: str, verbose: bool) -> list[str]:
    """Returns a list of failures (empty = pass)."""
    failures: list[str] = []
    tabs: list[Tab] = []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        try:
            # 1. Host: type a name, tick a category, Create → join code.
            host = open_tab(browser, pw, engine, base, "host")
            tabs.append(host)
            code = create_game(host, "Hosty", host_first=False, overrides=GAME_OVERRIDES)
            print(f"[{engine}] host created game {code}")

            # 2. A second browser context joins with the code; both rosters show two seats.
            guest = open_tab(browser, pw, engine, base, "guest")
            tabs.append(guest)
            join_game(guest, "Guesty", code)
            for t in (host, guest):
                t.page.wait_for_function(
                    "document.querySelectorAll('#roster li').length === 2", timeout=10_000
                )
            print(f"[{engine}] guest joined {code}; host roster: {roster(host)}")

            # 3. Start with min_players 2, pick, answer one question, reveal.
            for line in play_one_question([host, guest]):
                print(f"[{engine}] {line}")

            # 4. The other ordering that used to fail silently: Host first, name on the create screen.
            host2 = open_tab(browser, pw, engine, base, "host(host-first)")
            tabs.append(host2)
            code2 = create_game(host2, "Hosty2", host_first=True)
            print(f"[{engine}] host-first create → join code {code2}")

            # 5. Failures are visible.
            checker = open_tab(browser, pw, engine, base, "errors")
            tabs.append(checker)
            for line in check_visible_errors(checker):
                print(f"[{engine}] {line}")
        except AssertionError as e:
            failures.append(str(e))
        except Exception as e:  # playwright timeouts etc.
            failures.append(f"{type(e).__name__}: {e}")
        finally:
            for tab in tabs:
                try:
                    banner = tab.page.locator("#fatal")
                    if banner.count() and banner.is_visible():
                        failures.append(f"{tab.name}: page banner: {banner.text_content()!r}")
                except Exception:
                    pass
                errs = [c for c in tab.console if c.startswith("[error]")]
                if errs:
                    failures.append(f"{tab.name}: console errors: {errs}")
                if tab.errors:
                    failures.append(f"{tab.name}: page errors: {tab.errors}")
                if tab.bad_responses:
                    failures.append(f"{tab.name}: failed API calls: {tab.bad_responses}")
            if verbose or failures:
                for tab in tabs:
                    print(tab.report())
            browser.close()
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", help="API already running (default: start one on the test DB)")
    parser.add_argument("--port", type=int, default=8001, help="port for the API this script starts")
    parser.add_argument("--engine", choices=["chromium", "webkit"], default="chromium")
    parser.add_argument("--verbose", action="store_true", help="print requests/console even on success")
    args = parser.parse_args()

    proc = None
    base = args.base
    log_path = Path(tempfile.gettempdir()) / "smoke_dev_client.uvicorn.log"
    try:
        if base is None:
            proc, url = start_api(args.port, log_path)
            base = f"http://{lan_ip()}:{args.port}"
            print(f"started API on {base} (test DB {url.rsplit('/', 1)[-1]}; log {log_path})")
            if not wait_http(base + "/health", timeout=20):
                print(log_path.read_text())
                print(f"FAIL: API did not come up on {base}")
                return 2
        base = base.rstrip("/")
        print(f"driving {base}/dev/client with {args.engine}")
        failures = run(base, args.engine, args.verbose)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nPASS: create → join code, second context joins, game starts, a question is answered, errors are visible.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
