import asyncio
import os
from pathlib import Path
import random
import re
import time
from urllib.parse import urlencode, unquote, urlparse

from playwright.async_api import async_playwright


_LINKEDIN_MIN_ACTION_GAP_SECONDS = max(
    0.0, float(os.getenv("LINKEDIN_MIN_ACTION_GAP_SECONDS", "4.0"))
)
_LINKEDIN_JITTER_MIN_SECONDS = max(
    0.0, float(os.getenv("LINKEDIN_JITTER_MIN_SECONDS", "0.6"))
)
_LINKEDIN_JITTER_MAX_SECONDS = max(
    _LINKEDIN_JITTER_MIN_SECONDS,
    float(os.getenv("LINKEDIN_JITTER_MAX_SECONDS", "1.8")),
)
_LINKEDIN_MAX_ACTIONS_PER_MINUTE = max(
    1, int(os.getenv("LINKEDIN_MAX_ACTIONS_PER_MINUTE", "12"))
)
_LINKEDIN_MAX_MUTATING_ACTIONS_PER_HOUR = max(
    1, int(os.getenv("LINKEDIN_MAX_MUTATING_ACTIONS_PER_HOUR", "20"))
)
_LINKEDIN_DETECTION_BACKOFF_SECONDS = max(
    30.0, float(os.getenv("LINKEDIN_DETECTION_BACKOFF_SECONDS", "900"))
)

_recent_action_timestamps: list[float] = []
_recent_mutating_action_timestamps: list[float] = []
_last_action_at = 0.0
_detection_backoff_until = 0.0
_safety_lock = asyncio.Lock()

_SUSPICIOUS_URL_MARKERS = (
    "/checkpoint/",
    "/challenge/",
    "/security",
    "/authwall",
)
_SUSPICIOUS_TEXT_MARKERS = (
    "captcha",
    "security verification",
    "unusual activity",
    "temporarily restricted",
    "verify your identity",
    "suspicious activity",
    "confirm it's you",
)


def _prune_rate_windows(now: float) -> None:
    global _recent_action_timestamps, _recent_mutating_action_timestamps
    _recent_action_timestamps = [t for t in _recent_action_timestamps if now - t <= 60.0]
    _recent_mutating_action_timestamps = [
        t for t in _recent_mutating_action_timestamps if now - t <= 3600.0
    ]


async def _pre_action_safety_gate(action_name: str, mutating: bool = False) -> None:
    del action_name
    global _last_action_at
    now = time.monotonic()
    sleep_seconds = 0.0
    async with _safety_lock:
        if now < _detection_backoff_until:
            wait_left = int(max(1, _detection_backoff_until - now))
            raise RuntimeError(
                f"LinkedIn safety backoff is active due to a prior detection signal. Retry in ~{wait_left}s."
            )

        _prune_rate_windows(now)
        if len(_recent_action_timestamps) >= _LINKEDIN_MAX_ACTIONS_PER_MINUTE:
            raise RuntimeError(
                "LinkedIn action rate limit reached (per-minute cap). Slow down and retry."
            )
        if mutating and len(_recent_mutating_action_timestamps) >= _LINKEDIN_MAX_MUTATING_ACTIONS_PER_HOUR:
            raise RuntimeError(
                "LinkedIn mutating action cap reached (per-hour cap). Retry later."
            )

        wait_gap = _LINKEDIN_MIN_ACTION_GAP_SECONDS - (now - _last_action_at)
        jitter = random.uniform(_LINKEDIN_JITTER_MIN_SECONDS, _LINKEDIN_JITTER_MAX_SECONDS)
        sleep_seconds = max(0.0, wait_gap) + jitter

        planned_at = now + sleep_seconds
        _recent_action_timestamps.append(planned_at)
        if mutating:
            _recent_mutating_action_timestamps.append(planned_at)
        _last_action_at = planned_at

    if sleep_seconds > 0:
        await asyncio.sleep(sleep_seconds)


async def _human_pause(min_seconds: float = 0.3, max_seconds: float = 1.1) -> None:
    if max_seconds < min_seconds:
        max_seconds = min_seconds
    await asyncio.sleep(random.uniform(min_seconds, max_seconds))


async def _page_safety_check(page, action_name: str) -> None:
    global _detection_backoff_until
    url = (page.url or "").lower()
    if any(marker in url for marker in _SUSPICIOUS_URL_MARKERS):
        async with _safety_lock:
            _detection_backoff_until = max(
                _detection_backoff_until, time.monotonic() + _LINKEDIN_DETECTION_BACKOFF_SECONDS
            )
        raise RuntimeError(
            f"LinkedIn suspicious checkpoint detected during {action_name}; entering cooldown backoff."
        )

    snippet = ""
    try:
        snippet = await page.evaluate(
            """() => ((document.body && document.body.innerText) || "").slice(0, 5000)"""
        )
    except Exception:
        return

    text = (snippet or "").lower()
    if any(marker in text for marker in _SUSPICIOUS_TEXT_MARKERS):
        async with _safety_lock:
            _detection_backoff_until = max(
                _detection_backoff_until, time.monotonic() + _LINKEDIN_DETECTION_BACKOFF_SECONDS
            )
        raise RuntimeError(
            f"LinkedIn suspicious challenge text detected during {action_name}; entering cooldown backoff."
        )


def _linkedin_search_url(search_type: str, query: str) -> str:
    cleaned = " ".join((query or "").split())
    if not cleaned:
        raise ValueError("query is required")
    if search_type not in {"people", "companies"}:
        raise ValueError("search_type must be 'people' or 'companies'")
    return (
        f"https://www.linkedin.com/search/results/{search_type}/?"
        + urlencode({"keywords": cleaned, "origin": "GLOBAL_SEARCH_HEADER"})
    )


async def _scrape_linkedin_search(
    search_type: str,
    query: str,
    state_file: str,
    max_results: int,
    headless: bool,
) -> list[dict]:
    state = Path(state_file).expanduser().resolve()
    if not state.exists():
        raise FileNotFoundError(f"Session state not found: {state}")
    await _pre_action_safety_gate("linkedin_search", mutating=False)

    search_url = _linkedin_search_url(search_type, query)
    max_results = max(1, min(max_results, 25))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(storage_state=str(state))
            page = await context.new_page()
            await page.goto(search_url, wait_until="domcontentloaded")
            await _page_safety_check(page, "linkedin_search")
            await page.wait_for_timeout(2500)
            for _ in range(3):
                await page.mouse.wheel(0, 1800)
                await page.wait_for_timeout(700)
                await _human_pause(0.25, 0.75)

            return await page.evaluate(
                """({ searchType, maxResults }) => {
                  const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
                  const absolute = (href) => {
                    href = (href || "").trim();
                    if (!href) return "";
                    if (href.startsWith("http")) return href.split("?")[0];
                    if (href.startsWith("/")) return `https://www.linkedin.com${href}`.split("?")[0];
                    return href;
                  };
                  const wantedPath = searchType === "people" ? "/in/" : "/company/";
                  const cards = Array.from(document.querySelectorAll(
                    "li.reusable-search__result-container, div.reusable-search__result-container, div.entity-result"
                  ));
                  const seen = new Set();
                  const out = [];

                  for (const card of cards) {
                    if (out.length >= maxResults) break;
                    const link = Array.from(card.querySelectorAll("a[href]"))
                      .find((a) => (a.getAttribute("href") || "").includes(wantedPath));
                    if (!link) continue;

                    const url = absolute(link.getAttribute("href"));
                    if (!url || seen.has(url)) continue;
                    seen.add(url);

                    const titleNode =
                      card.querySelector(".entity-result__title-text a span[aria-hidden='true']") ||
                      card.querySelector(".entity-result__title-text a") ||
                      link.querySelector("span[aria-hidden='true']") ||
                      link;
                    const primary =
                      card.querySelector(".entity-result__primary-subtitle") ||
                      card.querySelector(".reusable-search-simple-insight__text");
                    const secondary = card.querySelector(".entity-result__secondary-subtitle");
                    const summary =
                      card.querySelector(".entity-result__summary") ||
                      card.querySelector(".entity-result__simple-insight-text");
                    const badge = card.querySelector(".entity-result__badge-text");

                    const row = {
                      name: clean(titleNode?.innerText || titleNode?.textContent),
                      url,
                      subtitle: clean(primary?.innerText || primary?.textContent),
                      location_or_meta: clean(secondary?.innerText || secondary?.textContent),
                      summary: clean(summary?.innerText || summary?.textContent),
                    };
                    if (searchType === "people") {
                      row.profile_url = url;
                      row.headline = row.subtitle;
                      row.location = row.location_or_meta;
                      row.connection_degree = clean(badge?.innerText || badge?.textContent);
                    } else {
                      row.company_url = url;
                      row.industry_or_description = row.subtitle;
                      row.followers_or_location = row.location_or_meta;
                    }
                    out.push(row);
                  }

                  // Fallback for LinkedIn layouts where cards are flattened:
                  // derive rows from visible profile/company anchors.
                  if (out.length === 0) {
                    const anchors = Array.from(document.querySelectorAll("a[href]"))
                      .filter((a) => (a.getAttribute("href") || "").includes(wantedPath));
                    for (const a of anchors) {
                      if (out.length >= maxResults) break;
                      const url = absolute(a.getAttribute("href"));
                      if (!url || seen.has(url)) continue;
                      seen.add(url);

                      const text = clean(a.innerText || a.textContent);
                      if (!text) continue;

                      const parts = text.split("•").map((x) => clean(x)).filter(Boolean);
                      const name = parts[0] || text;
                      const subtitle = parts.slice(1).join(" • ");

                      const row = {
                        name,
                        url,
                        subtitle,
                        location_or_meta: "",
                        summary: "",
                      };
                      if (searchType === "people") {
                        row.profile_url = url;
                        row.headline = subtitle;
                        row.location = "";
                        row.connection_degree = "";
                      } else {
                        row.company_url = url;
                        row.industry_or_description = subtitle;
                        row.followers_or_location = "";
                      }
                      out.push(row);
                    }
                  }

                  return out;
                }""",
                {"searchType": search_type, "maxResults": max_results},
            )
        finally:
            await browser.close()


async def search_linkedin_people(
    query: str,
    company: str = "",
    location: str = "",
    title: str = "",
    state_file: str = "linkedin_state.json",
    max_results: int = 10,
    headless: bool = True,
) -> list[dict]:
    """
    Search LinkedIn people results using the saved browser session.
    Optional company/location/title terms are appended to the keyword query.
    """
    terms = [query, title, company, location]
    search_query = " ".join(t.strip() for t in terms if t and t.strip())
    return await _scrape_linkedin_search(
        search_type="people",
        query=search_query,
        state_file=state_file,
        max_results=max_results,
        headless=headless,
    )


async def search_linkedin_companies(
    query: str,
    industry: str = "",
    location: str = "",
    state_file: str = "linkedin_state.json",
    max_results: int = 10,
    headless: bool = True,
) -> list[dict]:
    """
    Search LinkedIn company results using the saved browser session.
    Optional industry/location terms are appended to the keyword query.
    """
    terms = [query, industry, location]
    search_query = " ".join(t.strip() for t in terms if t and t.strip())
    return await _scrape_linkedin_search(
        search_type="companies",
        query=search_query,
        state_file=state_file,
        max_results=max_results,
        headless=headless,
    )


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
    await _pre_action_safety_gate("linkedin_login_session", mutating=False)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
            await _page_safety_check(page, "linkedin_login_session")

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
    await _pre_action_safety_gate("linkedin_list_inbox_threads", mutating=False)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(storage_state=str(state))
            page = await context.new_page()
            await page.goto("https://www.linkedin.com/messaging/", wait_until="domcontentloaded")
            await _page_safety_check(page, "linkedin_list_inbox_threads")
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
                    await _human_pause(0.2, 0.6)
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
    await _pre_action_safety_gate("linkedin_get_last_thread_messages", mutating=False)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(storage_state=str(state))
            page = await context.new_page()
            await page.goto(thread_url, wait_until="domcontentloaded")
            await _page_safety_check(page, "linkedin_get_last_thread_messages")
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
    await _pre_action_safety_gate("linkedin_send_message", mutating=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(storage_state=str(state))
            page = await context.new_page()
            await page.goto(thread_url, wait_until="domcontentloaded")
            await _page_safety_check(page, "linkedin_send_message")
            await page.wait_for_timeout(1200)

            composer = page.locator(
                "div.msg-form__contenteditable[contenteditable='true'], "
                "div[role='textbox'][contenteditable='true']"
            ).first
            await composer.click(timeout=5000)
            await _human_pause(0.2, 0.5)
            await composer.fill(message)
            await _human_pause(0.3, 0.8)
            send_btn = page.locator("button.msg-form__send-button").first
            try:
                await send_btn.click(timeout=3000)
            except Exception:
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(max(1, post_send_delay_seconds) * 1000)
            return True
        finally:
            await browser.close()


def _profile_posts_url(profile_url: str) -> str:
    """
    Normalize a LinkedIn profile URL into a recent-activity posts URL.
    Accepts either /in/<slug>/... or an existing /recent-activity/posts/ URL.
    """
    raw = (profile_url or "").strip()
    if not raw:
        raise ValueError("profile_url is required")

    if not raw.startswith("http://") and not raw.startswith("https://"):
        raw = f"https://{raw}"

    parsed = urlparse(raw)
    if "linkedin.com" not in parsed.netloc:
        raise ValueError("profile_url must be a linkedin.com URL")

    path = parsed.path.rstrip("/")
    if "/recent-activity/posts" in path:
        return f"https://www.linkedin.com{path}/"

    match = re.search(r"/in/([^/]+)", path)
    if not match:
        raise ValueError("profile_url must look like https://www.linkedin.com/in/<slug>/")

    slug = match.group(1)
    return f"https://www.linkedin.com/in/{slug}/recent-activity/posts/"


def _profile_activity_urls(profile_url: str) -> list[str]:
    """
    Build a small set of candidate activity URLs that can contain posts.
    LinkedIn sometimes redirects between tabs/layouts per-account.
    """
    primary = _profile_posts_url(profile_url).rstrip("/")
    parsed = urlparse(primary)
    path = parsed.path.rstrip("/")
    candidates: list[str] = [primary + "/"]

    if "/recent-activity/posts" in path:
        base = path.replace("/recent-activity/posts", "")
        candidates.extend(
            [
                f"https://www.linkedin.com{base}/recent-activity/all/",
                f"https://www.linkedin.com{base}/details/recent-activity/shares/",
            ]
        )
    elif "/recent-activity/articles" in path:
        base = path.replace("/recent-activity/articles", "")
        candidates.extend(
            [
                f"https://www.linkedin.com{base}/recent-activity/posts/",
                f"https://www.linkedin.com{base}/recent-activity/all/",
                f"https://www.linkedin.com{base}/details/recent-activity/shares/",
            ]
        )

    seen: set[str] = set()
    out: list[str] = []
    for url in candidates:
        key = url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append(url if url.endswith("/") else f"{url}/")
    return out


def _extract_activity_id(post_url: str) -> str | None:
    """
    Extract LinkedIn activity id from post URL (if present).
    """
    raw = unquote((post_url or "").strip())
    if not raw:
        return None
    match = re.search(r"urn:li:activity:(\d+)", raw)
    if match:
        return match.group(1)
    match = re.search(r"activity-(\d+)", raw)
    if match:
        return match.group(1)
    return None


async def get_recent_posts_from_profile(
    profile_url: str,
    state_file: str = "linkedin_state.json",
    max_posts: int = 5,
    headless: bool = True,
) -> list[dict]:
    state = Path(state_file).expanduser().resolve()
    if not state.exists():
        raise FileNotFoundError(f"Session state not found: {state}")
    await _pre_action_safety_gate("linkedin_get_recent_posts", mutating=False)

    posts_urls = _profile_activity_urls(profile_url)
    max_posts = max(1, min(max_posts, 20))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(storage_state=str(state))
            page = await context.new_page()
            async def extract() -> list[dict]:
                return await page.evaluate(
                    """(maxPosts) => {
                  const seen = new Set();
                  const out = [];
                  const cards = Array.from(
                    document.querySelectorAll(
                      "article, li, div.feed-shared-update-v2, div[data-urn*='urn:li:activity:'], div[data-id*='urn:li:activity:']"
                    )
                  );

                  for (const card of cards) {
                    if (out.length >= maxPosts) break;

                    const urnEl =
                      card.querySelector('[data-urn*="urn:li:activity:"]') ||
                      card.querySelector('[data-id*="urn:li:activity:"]') ||
                      card;
                    const urn = (
                      urnEl?.getAttribute('data-urn') ||
                      urnEl?.getAttribute('data-id') ||
                      ''
                    ).trim();

                    let postUrl = '';
                    const anchors = Array.from(
                      card.querySelectorAll(
                        'a[href*="/feed/update/urn:li:activity:"], a[href*="/posts/"], a[href*="/urn:li:activity:"]'
                      )
                    );
                    for (const a of anchors) {
                      const href = (a.getAttribute('href') || '').trim();
                      if (href.includes('/feed/update/urn:li:activity:') || href.includes('/posts/')) {
                        postUrl = href.startsWith('http') ? href : `https://www.linkedin.com${href}`;
                        break;
                      }
                    }

                    const key = postUrl || urn;
                    if (!key || seen.has(key)) continue;
                    seen.add(key);

                    const textNode =
                      card.querySelector('.feed-shared-update-v2__description-wrapper') ||
                      card.querySelector('.feed-shared-text') ||
                      card.querySelector('[data-test-id="main-feed-activity-card__commentary"]') ||
                      card.querySelector('span.break-words');
                    const text = ((textNode?.innerText || textNode?.textContent || '')
                      .replace(/\\s+/g, ' ')
                      .trim());

                    const actorNode =
                      card.querySelector('.update-components-actor__title') ||
                      card.querySelector('.feed-shared-actor__name') ||
                      card.querySelector('a[href*="/in/"] span[aria-hidden="true"]');
                    const actor = ((actorNode?.innerText || actorNode?.textContent || '')
                      .replace(/\\s+/g, ' ')
                      .trim());

                    const dateNode =
                      card.querySelector('span.update-components-actor__sub-description') ||
                      card.querySelector('.feed-shared-actor__sub-description') ||
                      card.querySelector('time');
                    const dateText = ((dateNode?.innerText || dateNode?.textContent || '')
                      .replace(/\\s+/g, ' ')
                      .trim());

                    out.push({
                      actor,
                      text,
                      date: dateText,
                      post_url: postUrl,
                      urn,
                    });
                  }

                  return out.slice(0, maxPosts);
                }""",
                    max_posts,
                )

            posts: list[dict] = []
            for candidate_url in posts_urls:
                await page.goto(candidate_url, wait_until="domcontentloaded")
                await _page_safety_check(page, "linkedin_get_recent_posts")
                await page.wait_for_timeout(2200)
                for _ in range(3):
                    await page.mouse.wheel(0, 1800)
                    await page.wait_for_timeout(900)
                    await _human_pause(0.25, 0.8)

                posts = await extract()
                if posts:
                    break

            return posts
        finally:
            await browser.close()


async def like_linkedin_post(
    post_url: str,
    state_file: str = "linkedin_state.json",
    headless: bool = False,
) -> dict:
    state = Path(state_file).expanduser().resolve()
    if not state.exists():
        raise FileNotFoundError(f"Session state not found: {state}")
    await _pre_action_safety_gate("linkedin_like_post", mutating=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(storage_state=str(state))
            page = await context.new_page()
            await page.goto(post_url, wait_until="domcontentloaded")
            await _page_safety_check(page, "linkedin_like_post")
            await page.wait_for_timeout(1800)
            activity_id = _extract_activity_id(post_url)
            post_scope = None

            if activity_id:
                post_scope = page.locator(
                    (
                        f"article:has(a[href*='urn:li:activity:{activity_id}']), "
                        f"article:has([data-urn*='urn:li:activity:{activity_id}']), "
                        f"li:has(a[href*='urn:li:activity:{activity_id}']), "
                        f"div[data-urn*='urn:li:activity:{activity_id}'], "
                        f"div[data-id*='urn:li:activity:{activity_id}']"
                    )
                ).first
                if await post_scope.count() == 0:
                    # Direct post pages can render outside these containers; fallback to page scope.
                    post_scope = None

            scope = post_scope if post_scope is not None else page
            button = scope.locator(
                "button[aria-label*='Reaction button state'], "
                "button[aria-label*='React Like'], "
                "button[aria-label*='Like'], "
                "button[aria-label*='like'], "
                "button.react-button__trigger"
            ).first
            if await button.count() == 0:
                return {
                    "ok": False,
                    "liked": False,
                    "reason": "Like button not found for target post",
                    "target_activity_id": activity_id,
                    "post_url": page.url,
                }

            aria = (await button.get_attribute("aria-pressed")) or ""
            already_liked = aria.lower() == "true"
            if not already_liked:
                await _human_pause(0.25, 0.8)
                await button.click(timeout=6000)
                await page.wait_for_timeout(800)

            final_aria = (await button.get_attribute("aria-pressed")) or ""
            liked_now = final_aria.lower() == "true" or (not already_liked)
            return {
                "ok": liked_now,
                "liked": liked_now,
                "already_liked": already_liked,
                "target_activity_id": activity_id,
                "post_url": page.url,
            }
        finally:
            await browser.close()


async def comment_on_linkedin_post(
    post_url: str,
    comment: str,
    state_file: str = "linkedin_state.json",
    headless: bool = False,
) -> dict:
    state = Path(state_file).expanduser().resolve()
    if not state.exists():
        raise FileNotFoundError(f"Session state not found: {state}")
    if not (comment or "").strip():
        raise ValueError("comment must be non-empty")
    await _pre_action_safety_gate("linkedin_comment_on_post", mutating=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(storage_state=str(state))
            page = await context.new_page()
            await page.goto(post_url, wait_until="domcontentloaded")
            await _page_safety_check(page, "linkedin_comment_on_post")
            await page.wait_for_timeout(1800)
            activity_id = _extract_activity_id(post_url)

            post_scope = None
            if activity_id:
                post_scope = page.locator(
                    (
                        f"article:has(a[href*='urn:li:activity:{activity_id}']), "
                        f"article:has([data-urn*='urn:li:activity:{activity_id}']), "
                        f"li:has(a[href*='urn:li:activity:{activity_id}']), "
                        f"div[data-urn*='urn:li:activity:{activity_id}'], "
                        f"div[data-id*='urn:li:activity:{activity_id}']"
                    )
                ).first
                if await post_scope.count() == 0:
                    # Fallback: on direct /feed/update/urn:li:activity:* pages, LinkedIn
                    # can render the post outside these container selectors.
                    post_scope = None
            scope = post_scope if post_scope is not None else page
            action_click_info = None
            submit_click_info = None
            submit_method = None

            # Pick the true Comment action and avoid "Repost with comment"/share variants.
            comment_btn = None
            buttons = scope.locator("button")
            btn_count = min(await buttons.count(), 80)
            for i in range(btn_count):
                btn = buttons.nth(i)
                try:
                    if not await btn.is_visible():
                        continue
                    has_repost_icon = (
                        await btn.locator("svg[data-test-icon*='repost'], use[href*='repost']").count()
                    ) > 0
                    if has_repost_icon:
                        continue
                    txt = ((await btn.inner_text()) or "").strip().lower()
                    aria = ((await btn.get_attribute("aria-label")) or "").strip().lower()
                    ckey = ((await btn.get_attribute("componentkey")) or "").strip().lower()
                    label_blob = f"{txt} {aria} {ckey}"

                    if "repost" in label_blob or "share" in label_blob:
                        continue
                    if "commentbuttonsection" in ckey:
                        comment_btn = btn
                        action_click_info = {"text": txt, "aria_label": aria, "componentkey": ckey}
                        break
                    if txt == "comment":
                        comment_btn = btn
                        action_click_info = {"text": txt, "aria_label": aria, "componentkey": ckey}
                        break
                    if aria.startswith("comment") and "with comment" not in aria:
                        comment_btn = btn
                        action_click_info = {"text": txt, "aria_label": aria, "componentkey": ckey}
                        break
                except Exception:
                    continue

            if comment_btn is not None:
                try:
                    await _human_pause(0.25, 0.8)
                    await comment_btn.click(timeout=4000)
                    await page.wait_for_timeout(700)
                except Exception:
                    pass

            editor = scope.locator(
                "div.comments-comment-box__contenteditable[contenteditable='true'], "
                "div[role='textbox'][contenteditable='true']"
            ).first
            if await editor.count() == 0:
                return {
                    "ok": False,
                    "commented": False,
                    "reason": "Comment editor not found for target post",
                    "target_activity_id": activity_id,
                    "post_url": page.url,
                }
            await editor.click(timeout=7000)
            await _human_pause(0.25, 0.6)
            await editor.fill(comment)

            # Click submit from the same composer region (not action-row Repost/Comment controls).
            composer_scope = editor.locator(
                "xpath=ancestor::*[self::form or contains(@class,'comments-comment-box')][1]"
            ).first
            post_btn = composer_scope.locator(
                "button[componentkey*='commentButtonSection'], "
                "button.comments-comment-box__submit-button, "
                "button[aria-label*='Post comment'], "
                "button:has-text('Comment'), "
                "button:has-text('Post')"
            ).first
            if await post_btn.count() == 0:
                post_btn = scope.locator(
                    "button[componentkey*='commentButtonSection'], "
                    "button.comments-comment-box__submit-button, "
                    "button[aria-label*='Post comment'], "
                    "button:has-text('Comment'), "
                    "button:has-text('Post')"
                ).first

            posted = False
            if await post_btn.count() > 0:
                try:
                    submit_click_info = {
                        "text": (((await post_btn.inner_text()) or "").strip().lower()),
                        "aria_label": (((await post_btn.get_attribute("aria-label")) or "").strip().lower()),
                        "componentkey": (((await post_btn.get_attribute("componentkey")) or "").strip().lower()),
                    }
                    await _human_pause(0.25, 0.8)
                    await post_btn.click(timeout=4000)
                    posted = True
                    submit_method = "button_click"
                    await page.wait_for_timeout(5000)
                except Exception:
                    posted = False

            if not posted:
                try:
                    await editor.press("Enter")
                    posted = True
                    submit_method = "editor_enter"
                    await page.wait_for_timeout(5000)
                except Exception:
                    posted = False

            await page.wait_for_timeout(1500)
            if not posted:
                return {
                    "ok": False,
                    "commented": False,
                    "reason": "Failed to submit comment",
                    "target_activity_id": activity_id,
                    "post_url": page.url,
                    "action_click_info": action_click_info,
                    "submit_method": submit_method,
                    "submit_click_info": submit_click_info,
                }

            comment_text = comment.strip()

            async def verify_comment_visible() -> bool:
                # Re-open the target post and verify in rendered comment bodies only.
                await page.goto(post_url, wait_until="domcontentloaded")
                await _page_safety_check(page, "linkedin_comment_on_post_verify")
                await page.wait_for_timeout(1800)
                open_comments_btn = page.locator(
                    "button[aria-label*='Comment'], button[aria-label*='comment'], button.comments-comments-list__load-more-comments-button"
                ).first
                if await open_comments_btn.count() > 0:
                    try:
                        await open_comments_btn.click(timeout=3000)
                        await page.wait_for_timeout(900)
                    except Exception:
                        pass

                return await page.evaluate(
                    """(expected) => {
                      const target = (expected || "").replace(/\\s+/g, " ").trim();
                      if (!target) return false;
                      const nodes = Array.from(
                        document.querySelectorAll(
                          ".comments-comment-item-content-body, .comments-comment-item__main-content, .social-details-social-activity__comment-item .feed-shared-text, .comments-comments-list__comment-item .comments-comment-item-content-body"
                        )
                      );
                      return nodes.some((n) => {
                        if (!n) return false;
                        if (n.closest(".comments-comment-box")) return false;
                        const txt = (n.innerText || n.textContent || "").replace(/\\s+/g, " ").trim();
                        return txt.includes(target);
                      });
                    }""",
                    comment_text,
                )

            verified = await verify_comment_visible()
            if not verified:
                await page.wait_for_timeout(1500)
                verified = await verify_comment_visible()

            feedback_text = await page.evaluate(
                """() => {
                  const nodes = Array.from(
                    document.querySelectorAll(
                      "[role='alert'], .artdeco-inline-feedback__message, .artdeco-toast-item__message"
                    )
                  );
                  for (const n of nodes) {
                    const txt = (n.innerText || n.textContent || "").replace(/\\s+/g, " ").trim();
                    if (txt) return txt;
                  }
                  return "";
                }"""
            )

            return {
                "ok": bool(verified),
                "commented": bool(verified),
                "verified": bool(verified),
                "published": bool(verified),
                "target_activity_id": activity_id,
                "post_url": page.url,
                "reason": None if verified else "Comment was submitted in UI but not published/visible after reload",
                "linkedin_feedback": feedback_text or None,
                "action_click_info": action_click_info,
                "submit_method": submit_method,
                "submit_click_info": submit_click_info,
            }
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
    await _pre_action_safety_gate("linkedin_session_state_status", mutating=False)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(storage_state=str(state))
            page = await context.new_page()
            await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
            await _page_safety_check(page, "linkedin_session_state_status")
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
