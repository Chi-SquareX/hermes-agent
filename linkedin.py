from pathlib import Path
import re

from playwright.async_api import async_playwright


async def login_and_save_linkedin_session(
    state_file: str = "linkedin_state.json",
    headless: bool = False,
    timeout_seconds: int = 300,
) -> str:
    """
    Open LinkedIn login, let the user log in manually, then save browser storage state.
    Returns the absolute path to the saved state file.
    """
    out = Path(state_file).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

            # Wait until user is logged in and reaches feed/profile/home.
            await page.wait_for_url(
                re.compile(r"^https://www\.linkedin\.com/(feed/|in/|home/?).*"),
                timeout=timeout_seconds * 1000,
            )

            await context.storage_state(path=str(out))
        finally:
            await browser.close()

    return str(out)


async def scrape_linkedin_inbox(
    state_file: str = "linkedin_state.json",
    max_threads: int = 20,
    headless: bool = True,
) -> list[dict]:
    state = Path(state_file).expanduser().resolve()
    if not state.exists():
        raise FileNotFoundError(f"Session state not found: {state}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(storage_state=str(state))
            page = await context.new_page()
            await page.goto("https://www.linkedin.com/messaging/", wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            cards = page.locator(
                "li.msg-conversations-container__convo-item, li.msg-conversation-listitem, div.msg-conversation-card"
            )
            count = min(await cards.count(), max(1, min(max_threads, 50)))
            seen: set[str] = set()
            rows: list[dict] = []
            for i in range(count):
                try:
                    await cards.nth(i).click(timeout=3000)
                    await page.wait_for_timeout(900)
                    thread_url = page.url.split("?", 1)[0]
                    if "/messaging/thread/" not in thread_url or thread_url in seen:
                        continue
                    seen.add(thread_url)
                    name = await page.evaluate(
                        """() => {
                          const el =
                            document.querySelector("h2.msg-thread__topcard-title") ||
                            document.querySelector(".msg-thread__participant-names") ||
                            document.querySelector(".msg-thread__link-to-profile") ||
                            document.querySelector("header a[href*='/in/']");
                          const raw = ((el && (el.innerText || el.textContent)) || "").replace(/\\s+/g, " ").trim();
                          return raw.split("\\n")[0].replace(/^View\\s+|\\s+profile$/gi, "").trim();
                        }"""
                    )
                    rows.append({"name": name, "thread_url": thread_url})
                except Exception:
                    continue
            return rows
        finally:
            await browser.close()


async def get_last_thread_messages(
    thread_url: str,
    state_file: str = "linkedin_state.json",
    limit: int = 10,
    headless: bool = True,
) -> list[dict]:
    state = Path(state_file).expanduser().resolve()
    if not state.exists():
        raise FileNotFoundError(f"Session state not found: {state}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(storage_state=str(state))
            page = await context.new_page()
            await page.goto(thread_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)

            return await page.evaluate(
                """(limit) => {
                  const nodes = Array.from(document.querySelectorAll(
                    ".msg-s-message-list__event, .msg-s-message-group__messages li, li.msg-s-message-list__event"
                  ));
                  const out = [];
                  for (const n of nodes) {
                    const text = (n.innerText || "").replace(/\\s+/g, " ").trim();
                    if (!text) continue;
                    const label = ((n.getAttribute("aria-label") || "") + " " + text).toLowerCase();
                    const direction = label.includes("you:") || label.includes("from you") || label.startsWith("you ")
                      ? "outbound"
                      : "inbound";
                    out.push({ direction, text });
                  }
                  return out.slice(-Math.max(1, Math.min(limit, 50)));
                }""",
                limit,
            )
        finally:
            await browser.close()


async def send_message_to_thread(
    thread_url: str,
    message: str,
    state_file: str = "linkedin_state.json",
    headless: bool = False,
    post_send_delay_seconds: int = 8,
) -> bool:
    state = Path(state_file).expanduser().resolve()
    if not state.exists():
        raise FileNotFoundError(f"Session state not found: {state}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(storage_state=str(state))
            page = await context.new_page()
            await page.goto(thread_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1200)

            composer = page.locator(
                "div.msg-form__contenteditable[contenteditable='true'], "
                "div[role='textbox'][contenteditable='true']"
            ).first
            await composer.click(timeout=5000)
            await composer.fill(message)
            send_btn = page.locator("button.msg-form__send-button").first
            try:
                await send_btn.click(timeout=3000)
            except Exception:
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(max(1, post_send_delay_seconds) * 1000)
            return True
        finally:
            await browser.close()


async def linkedin_state_status(
    state_file: str = "linkedin_state.json",
    headless: bool = True,
    timeout_seconds: int = 20,
) -> dict:
    state = Path(state_file).expanduser().resolve()
    if not state.exists():
        return {"exists": False, "is_fresh": False, "state_file": str(state)}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(storage_state=str(state))
            page = await context.new_page()
            await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
            await page.wait_for_timeout(1200)
            url = page.url
            if "/feed" in url:
                return {"exists": True, "is_fresh": True, "state_file": str(state), "current_url": url}
            try:
                await page.wait_for_url(re.compile(r"^https://www\.linkedin\.com/feed/?"), timeout=timeout_seconds * 1000)
                return {"exists": True, "is_fresh": True, "state_file": str(state), "current_url": page.url}
            except Exception:
                return {"exists": True, "is_fresh": False, "state_file": str(state), "current_url": url}
        finally:
            await browser.close()
