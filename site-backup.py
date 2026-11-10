#!/usr/bin/env python3
"""
site_backup.py - Mirror/back up a website that requires login (SSO), with Playwright.

Setup (an embedded Python works fine):
    python -m pip install playwright
    python -m playwright install chromium

Usage:
    python site_backup.py login                          # one-time: a real browser opens, YOU
                                                         # log in via SSO; session saved to auth.json
    python site_backup.py crawl                          # crawl the site using the saved session
    python site_backup.py crawl --max 200 --headful --pdf

URL options (CLI overrides the constants in the config block):
    --base-url https://intranet.example.com/
    --login-url https://intranet.example.com/login   (default: --base-url)
    --start-urls https://a.example.com/ https://b.example.com/deep/page
                                                 (default: --base-url; relative paths
                                                  are resolved against --base-url)

What it saves (into OUT_DIR):
    - every visited page as fully rendered .html (after JavaScript ran)
    - all assets (images, css, js, fonts, media) under assets/
    - url_map.json (original URL -> local file)
    - index.html (browseable list of everything)
    - optionally a .pdf per page (--pdf)
    - links between saved pages/assets are rewritten so the backup browses OFFLINE

Notes / caveats:
    - auth.json expires with the SSO session. If crawls start redirecting to the SSO
      login page, just run `login` again.
    - Crawl gently: keep DELAY >= 1s and check the site's terms of use.
    - Single-file JS apps (React/Vue/Angular) save fine as rendered HTML, but
      client-side routing may hide content from the link crawler.
    - Lazy-loaded content: increase EXTRA_WAIT.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import deque
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse, unquote

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ----------------------------- configuration -----------------------------
# All three can be overridden on the command line:
#   --base-url / --login-url / --start-urls
# If --login-url is not given, --base-url is used.
# If --start-urls is not given, --base-url is used as the single start URL.
BASE_URL    = "https://intranet.example.com/"   # fallback + used to resolve relative URLs
LOGIN_URL   = "https://intranet.example.com/login"
START_URLS  = ["https://intranet.example.com/"]  # where crawling begins
OUT_DIR     = "site_backup"
AUTH_FILE   = "auth.json"
MAX_PAGES   = 500          # safety cap on visited pages
DELAY       = 1.0          # polite delay between pages (seconds)
HEADLESS    = True
SAVE_PDF    = False
STAY_ON_HOST = True        # only crawl hosts of the START_URLS
USER_AGENT  = None         # set a real-browser UA string if the site blocks automation
EXTRA_WAIT  = 0            # extra ms to wait after networkidle (lazy content)

PAGE_EXT  = ".html"
ASSET_TYPES = {"image", "stylesheet", "script", "font", "media"}
# URL paths with these extensions are downloaded as files, not saved as pages
BINARY_EXT = {".pdf", ".zip", ".docx", ".xlsx", ".pptx", ".csv", ".png", ".jpg",
              ".jpeg", ".gif", ".svg", ".webp", ".ico", ".woff", ".woff2",
              ".ttf", ".eot", ".mp4", ".mp3", ".webm", ".wav", ".json"}

# ----------------------------- url helpers -----------------------------
INVALID_CHARS = re.compile(r'[<>:\"/\\|?*\x00-\x1f]')


def normalize_url(url):
    """Strip fragment, lowercase scheme/host, collapse slashes. None if not http(s)."""
    if not url:
        return None
    url, _frag = urldefrag(url.strip())
    p = urlparse(url)
    if p.scheme.lower() not in ("http", "https"):
        return None
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", p.query, ""))


def host_of(url):
    return urlparse(url).netloc.lower()


def url_to_relpath(url, is_asset=False):
    """Map a URL to a local relative path. Query strings become a short hash suffix."""
    p = urlparse(url)
    parts = [INVALID_CHARS.sub("_", seg) for seg in unquote(p.path).split("/") if seg]
    if not parts:
        parts = ["index"]
    if p.query:
        h = hashlib.sha1(p.query.encode()).hexdigest()[:8]
        parts[-1] = f"{parts[-1]}__q{h}"
    last_has_ext = bool(os.path.splitext(parts[-1])[1])
    if is_asset:
        if not last_has_ext:
            parts[-1] += ".bin"
        return os.path.join("assets", *parts)
    ext = os.path.splitext(parts[-1])[1].lower()
    if ext and ext in BINARY_EXT:
        return os.path.join("assets", *parts)
    if not last_has_ext:
        parts[-1] += PAGE_EXT
    return os.path.join(p.netloc, *parts)


# ----------------------------- step 1: login -----------------------------
def do_login(login_url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(login_url)
        print("\n>>> A browser window is open. Log in via SSO there.")
        input(">>> When you see the site logged-in, come back and press Enter...")
        ctx.storage_state(path=AUTH_FILE)
        browser.close()
    print(f"Session saved to {AUTH_FILE} (expires with the SSO session).")


# ----------------------------- step 2: crawl -----------------------------
ATTR_RE = re.compile(
    r'((?:href|src|action)\s*=\s*)(["\'])([^"\']*)(["\'])', re.IGNORECASE)


def crawl(max_pages, headless, save_pdf, base_url, start_urls):
    out = os.path.abspath(OUT_DIR)
    os.makedirs(os.path.join(out, "assets"), exist_ok=True)

    url_map = {}            # normalized url -> relative local path
    queue = deque()
    allowed_hosts = set()
    for s in start_urls:
        s = normalize_url(urljoin(base_url, s))
        if not s:
            print(f"WARNING: skipping invalid start URL {s!r}")
            continue
        queue.append(s)
        allowed_hosts.add(host_of(s))

    visited, failed, warned_sso = set(), [], False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx_kw = {}
        if os.path.exists(AUTH_FILE):
            ctx_kw["storage_state"] = AUTH_FILE
            print(f"Using saved session from {AUTH_FILE}")
        else:
            print("WARNING: no auth.json - crawling anonymously. Run `login` first.")
        if USER_AGENT:
            ctx_kw["user_agent"] = USER_AGENT
        ctx = browser.new_context(**ctx_kw)
        page = ctx.new_page()

        # ---- asset interceptor: save css/js/images/fonts while browsing ----
        def handle_route(route):
            req = route.request
            if req.resource_type == "websocket":
                route.continue_()
                return
            try:
                resp = route.fetch(timeout=30000)
            except Exception:
                route.abort()
                return
            try:
                url = normalize_url(resp.url or req.url)
                ctype = resp.headers.get("content-type", "")
                if (url and resp.ok and req.resource_type in ASSET_TYPES
                        and "text/html" not in ctype):
                    rel = url_to_relpath(url, is_asset=True)
                    fpath = os.path.join(out, rel)
                    if not os.path.exists(fpath):
                        os.makedirs(os.path.dirname(fpath), exist_ok=True)
                        with open(fpath, "wb") as f:
                            f.write(resp.body())
                    url_map[url] = rel
            except Exception:
                pass
            route.fulfill(response=resp)

        page.route(re.compile(r"https?://.*"), handle_route)

        # ---- main crawl loop ----
        try:
            while queue and len(visited) < max_pages:
                url = queue.popleft()
                if url in visited:
                    continue
                visited.add(url)
                print(f"[{len(visited)}/{max_pages}] {url}")
                try:
                    response = page.goto(url, wait_until="networkidle", timeout=45000)
                except PWTimeout:
                    print("  ! timeout - saving whatever rendered")
                    response = None
                except Exception as e:
                    print(f"  ! navigation failed: {e}")
                    failed.append({"url": url, "error": str(e)})
                    continue

                final_url = normalize_url(page.url) or url

                # SSO session expired? we got bounced to an unknown host
                global_warn = None
                if STAY_ON_HOST and host_of(final_url) not in allowed_hosts and not warned_sso:
                    warned_sso = True
                    global_warn = ("  ! landed on a different host - your SSO session "
                                   "probably expired. Stop, run `login` again.")
                    print(global_warn)

                if response is not None and response.status >= 400:
                    print(f"  ! HTTP {response.status}")
                    failed.append({"url": url, "status": response.status})
                    continue

                if EXTRA_WAIT:
                    page.wait_for_timeout(EXTRA_WAIT)

                # binary document (e.g. a .pdf link)? the asset interceptor saved it
                ext = os.path.splitext(urlparse(final_url).path)[1].lower()
                if ext in BINARY_EXT:
                    rel = url_to_relpath(final_url, is_asset=True)
                    url_map[url] = rel
                    url_map[final_url] = rel
                    continue

                html = page.content()
                rel = url_to_relpath(final_url)
                fpath = os.path.join(out, rel)
                os.makedirs(os.path.dirname(fpath), exist_ok=True)
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(html)
                url_map[final_url] = rel
                if final_url != url:
                    url_map[url] = rel

                if save_pdf:
                    try:
                        page.pdf(path=os.path.splitext(fpath)[0] + ".pdf")
                    except Exception as e:
                        print(f"  ! pdf failed: {e}")

                # collect links
                try:
                    hrefs = page.eval_on_selector_all(
                        "a[href]", "els => els.map(e => e.getAttribute('href'))")
                except Exception:
                    hrefs = []
                for h in hrefs or []:
                    u = normalize_url(urljoin(final_url, h))
                    if not u or u.startswith(("mailto:", "tel:", "javascript:")):
                        continue
                    if STAY_ON_HOST and host_of(u) not in allowed_hosts:
                        continue
                    if u not in visited and u not in queue:
                        queue.append(u)

                time.sleep(DELAY)
        finally:
            browser.close()

    with open(os.path.join(out, "url_map.json"), "w", encoding="utf-8") as f:
        json.dump(url_map, f, indent=1, ensure_ascii=False)
    if failed:
        with open(os.path.join(out, "failed.json"), "w", encoding="utf-8") as f:
            json.dump(failed, f, indent=1, ensure_ascii=False)

    rewritten = rewrite_links(out, url_map)
    write_index(out, url_map)
    print(f"\nDone: {sum(1 for v in url_map.values() if v.endswith(PAGE_EXT))} pages, "
          f"{sum(1 for v in url_map.values() if v.startswith('assets'))} assets "
          f"-> {out}  ({rewritten} links rewritten)")


# --------------------- offline link rewriting ---------------------
def rewrite_links(out_dir, url_map):
    """Rewrite href/src/action in saved pages to relative local paths."""
    pages = {rel: u for u, rel in url_map.items()
             if not rel.startswith("assets") and rel.endswith(PAGE_EXT)
             and os.path.isfile(os.path.join(out_dir, rel))}
    lookup = {}
    for u, rel in url_map.items():
        lookup[u] = rel
        lookup[u.replace("&", "&amp;")] = rel   # HTML-escaped variant

    count = 0
    for rel, base_url in pages.items():
        fpath = os.path.join(out_dir, rel)
        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
            html = f.read()

        def repl(m, fpath=fpath):
            nonlocal count
            prefix, q1, val, q2 = m.groups()
            v = val.strip()
            if not v or v.startswith(("#", "data:", "mailto:", "tel:", "javascript:")):
                return m.group(0)
            frag = ""
            if "#" in v:
                v, frag = v.split("#", 1)
                frag = "#" + frag
            absu = normalize_url(urljoin(base_url, v))
            target = lookup.get(absu) if absu else None
            if target is None:
                return m.group(0)
            link = os.path.relpath(os.path.join(out_dir, target),
                                   os.path.dirname(fpath)).replace(os.sep, "/")
            count += 1
            return f"{prefix}{q1}{link}{frag}{q2}"

        new = ATTR_RE.sub(repl, html)
        if new != html:
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(new)
    return count


def write_index(out_dir, url_map):
    rows = "\n".join(
        f'<li><a href="{rel.replace(os.sep, "/")}">{u}</a> '
        f'<small>({rel.replace(os.sep, "/")})</small></li>'
        for u, rel in sorted(url_map.items()))
    doc = ("<!doctype html><meta charset='utf-8'><title>Backup index</title>\n"
           f"<h1>Backup index ({len(url_map)} files)</h1>\n<ul>\n{rows}\n</ul>")
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(doc)


# ----------------------------- CLI -----------------------------
def main():
    ap = argparse.ArgumentParser(description="SSO-aware website backup with Playwright")
    ap.add_argument("command", choices=["login", "crawl"])
    ap.add_argument("--max", type=int, default=MAX_PAGES)
    ap.add_argument("--headful", action="store_true", help="show the browser")
    ap.add_argument("--pdf", action="store_true", help="also save a PDF per page")
    ap.add_argument("--base-url", default=BASE_URL,
                    help="base URL (default: config or https://intranet.example.com/)")
    ap.add_argument("--login-url", default=None,
                    help="login page URL (default: --base-url)")
    ap.add_argument("--start-urls", nargs="+", default=None, metavar="URL",
                    help="one or more crawl start URLs (default: --base-url)")
    args = ap.parse_args()

    base_url = args.base_url
    login_url = args.login_url or base_url          # fallback: base_url
    start_urls = args.start_urls or [base_url]      # fallback: base_url

    if args.command == "login":
        do_login(login_url)
    else:
        crawl(args.max, headless=not args.headful, save_pdf=args.pdf,
              base_url=base_url, start_urls=start_urls)


if __name__ == "__main__":
    main()
