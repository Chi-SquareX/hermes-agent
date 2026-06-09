from pathlib import Path
import re
from urllib.parse import unquote, urlparse

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
                await page.wait_for_timeout(2200)
                for _ in range(3):
                    await page.mouse.wheel(0, 1800)
                    await page.wait_for_timeout(900)

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

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(storage_state=str(state))
            page = await context.new_page()
            await page.goto(post_url, wait_until="domcontentloaded")
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
                    return {
                        "ok": False,
                        "liked": False,
                        "reason": "Target post not found on page",
                        "target_activity_id": activity_id,
                        "post_url": page.url,
                    }

            scope = post_scope if post_scope is not None else page
            button = scope.locator(
                "button[aria-label*='Like'], button[aria-label*='like'], button.react-button__trigger"
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

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(storage_state=str(state))
            page = await context.new_page()
            await page.goto(post_url, wait_until="domcontentloaded")
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
            await editor.fill(comment)

            post_btn = scope.locator(
                "button.comments-comment-box__submit-button, button[aria-label*='Post comment']"
            ).first

            posted = False
            if await post_btn.count() > 0:
                try:
                    submit_click_info = {
                        "text": (((await post_btn.inner_text()) or "").strip().lower()),
                        "aria_label": (((await post_btn.get_attribute("aria-label")) or "").strip().lower()),
                        "componentkey": (((await post_btn.get_attribute("componentkey")) or "").strip().lower()),
                    }
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
