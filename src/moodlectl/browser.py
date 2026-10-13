from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import time
import warnings
from contextlib import AbstractContextManager
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from getpass import getpass
from typing import Any, Callable, Sequence

from playwright.sync_api import sync_playwright, Page, Locator, TimeoutError as PWTimeout
from playwright.sync_api import Error as PWError
from bs4 import BeautifulSoup

from .config import Settings
from .errors import AmbiguousMatch, LoginRequired, moodlectlError, MoodleFormError
from .models import Course, Discussion, Post, Resource, ResourceSpec, Section


# Engines Playwright drives with its own builds; any other name is a Chromium channel
# (chromium = bundled, chrome/msedge = installed browsers, chrome-beta, ...).
OWN_ENGINES = ("firefox", "webkit")


def auto_browsers() -> tuple[str, ...]:
    """`--browser auto` order: the OS default browser where drivable (installed Edge,
    Playwright's Firefox), then Chromium, then Chrome. macOS skips WebKit (Safari's
    engine): it rejects the Shibboleth session cookie, so Unibo SSO loops."""
    if sys.platform == "darwin":
        return ("chrome", "chromium")
    first = "msedge" if sys.platform == "win32" else "firefox"
    return (first, "chromium", "chrome")


# Academic year ("2025/2026") of enrolled courses, parsed from their category name
# (e.g. Virtuale's "Anno Accademico 2025/2026"); same AJAX service as My Courses.
YEARS_JS = r"""async () => {
 const r = await fetch(M.cfg.wwwroot + '/lib/ajax/service.php?sesskey=' + M.cfg.sesskey, {
  method: 'POST', body: JSON.stringify([{index: 0,
   methodname: 'core_course_get_enrolled_courses_by_timeline_classification',
   args: {offset: 0, limit: 0, classification: 'allincludinghidden', sort: 'fullname'}}])});
 const [res] = await r.json();
 return res.error ? [] : res.data.courses.map(c =>
  [c.id, ((c.coursecategory || '').match(/\d{4}\s*[/-]\s*\d{4}/) || [null])[0]]);
}"""


# Moodle has no universal create-URL external web-service endpoint. Use the
# supported browser form with a real lecturer SSO session instead.
SECTION_JS = r"""() => {
 const nodes = [...document.querySelectorAll('.course-section, li.section, section[id^="section-"]')];
 const used = new Set(), result = [];
 for (const node of nodes) {
   if (nodes.some(n => n !== node && node.contains(n))) continue;
   const id = node.id || '';
   const rawNumber = node.getAttribute('data-sectionnum') || id.match(/^section-(\d+)$/)?.[1];
   const h = node.querySelector('.sectionname, .section-title, h3[id^="sectionid-"], h3');
   const rawId = node.getAttribute('data-sectionid') || h?.id?.match(/^sectionid-(\d+)-/)?.[1];
   const number = rawNumber !== null && rawNumber !== undefined ? Number(rawNumber) : result.length;
   const key = rawId || id || String(number);
   if (used.has(key)) continue;
   used.add(key);
   const title = h?.querySelector('.inplaceeditable-text')?.textContent || h?.textContent || `Section ${number}`;
   const activities = node.querySelectorAll('li.activity, .activity-item');
   const seen = new Set();
   const resources = [];
   for (const el of activities) {
     if (el.matches('.activity-item') && el.closest('li.activity')) continue;
     const a = el.querySelector('.activityname a[href], a.aalink[href*="/mod/"], a[href*="/mod/"]');
     if (!a) continue;
     const href = a.href;
     const mod = href.match(/\/mod\/([a-z0-9_]+)\/view\.php/i);
     if (!mod) continue;
     const label = (a.querySelector('.instancename') || a).cloneNode(true);
     label.querySelectorAll('.accesshide').forEach(h => h.remove());  // screen-reader-only " URL"/" File" suffix
     const name = (label.textContent || '').replace(/\s+/g,' ').trim();
     const cm = (el.id || el.closest('[id^="module-"]')?.id || '').match(/module-(\d+)/);
     const cmid = cm ? Number(cm[1]) : Number(new URL(href).searchParams.get('id')) || null;
     const dup = cmid ? `id:${cmid}` : `url:${href}`;
     if (seen.has(dup)) continue;
     seen.add(dup);
     resources.push({name, kind:mod[1], url:href, cmid, visible:!el.classList.contains('dimmed')});
   }
   result.push({number, name:title.replace(/\s+/g,' ').trim(), section_id: rawId ? Number(rawId) : null, resources});
 }
 return result;
}"""


# Moodle AJAX call with the page's session; only functions flagged `ajax` work here.
AJAX_JS = r"""async ([methodname, args]) => {
 const r = await fetch(M.cfg.wwwroot + '/lib/ajax/service.php?sesskey=' + M.cfg.sesskey, {
  method: 'POST', body: JSON.stringify([{index: 0, methodname, args}])});
 return (await r.json())[0];
}"""


# Rows of a forum's discussion list (mod/forum/view.php), plus whether a next page exists.
DISCUSSIONS_JS = r"""(page) => {
 const txt = e => (e?.textContent || '').replace(/\s+/g, ' ').trim();
 const ts = e => e ? Number(e.dataset.timestamp) || null : null;
 const target = (row, type) => row.querySelector(`[data-type="${type}"]`)?.dataset.targetstate;
 // Replies column: the one sorted by o=5/6 (reply count).
 const col = [...document.querySelectorAll('table.discussion-list thead th')]
  .findIndex(th => th.querySelector('a[href*="o=5"], a[href*="o=6"]'));
 const rows = [...document.querySelectorAll('tr[data-region="discussion-list-item"]')].map(row => {
  const people = row.querySelectorAll('.author-info');
  const lock = row.querySelector('[data-type="lock-toggle"]');
  const fav = target(row, 'favorite-toggle'), pin = target(row, 'pin-toggle');
  const sub = row.querySelector('[data-type="subscription-toggle"]');
  return {id: Number(row.dataset.discussionid), subject: txt(row.querySelector('th.topic a')),
   author: txt(people[0]?.firstElementChild), created: ts(people[0]?.querySelector('time')),
   last_author: txt(people[1]?.firstElementChild), modified: ts(people[1]?.querySelector('time')),
   replies: col < 0 ? null : parseInt(txt(row.cells[col])) || 0,
   url: row.querySelector('th.topic a')?.href,
   locked: lock ? lock.dataset.state !== '0'
     : !row.querySelector('[data-region="locked-label"]')?.hidden,
   pinned: pin === undefined ? null : pin === '0',
   favourite: fav === undefined ? null : fav === '0',
   subscribed: sub ? sub.checked : null};
 });
 const next = [...document.querySelectorAll('.pagination a[href]')]
  .some(a => new URL(a.href).searchParams.get('p') === String(page + 1));
 return {rows, next};
}"""


# Submit the page's forum post form (add discussion, reply, edit) with `values` overriding
# its fields, like the web UI does, bypassing the rich-text editor widget.
POST_FORM_JS = r"""async (values) => {
 const form = document.querySelector('form#mformforum');
 if (!form) return {missing: ['form#mformforum']};
 const missing = Object.keys(values).filter(k => !form.elements[k]);
 if (missing.length) return {missing};
 const data = new FormData(form);
 for (const [k, v] of Object.entries(values)) data.set(k, v);
 data.set('submitbutton', form.elements.submitbutton.value);
 const r = await fetch(form.action, {method: 'POST', body: data});
 const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
 const errors = [...doc.querySelectorAll('.invalid-feedback, .alert-danger, .errorbox')]
  .map(e => e.textContent.replace(/\s+/g, ' ').trim()).filter(Boolean);
 return {url: r.url, errors};
}"""

# Moodle FORMAT_* constants: 0=Moodle auto-format, 1=HTML, 2=plain, 4=Markdown.
TEXT_FORMATS = {"auto": "0", "html": "1", "plain": "2", "markdown": "4"}


def _time(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="minutes") if ts else None


def html_text(html: str) -> str:
    """Readable plain text of a Moodle post body: paragraphs, line breaks, bullets."""
    # ponytail: drops <pre> indentation and table layout; use --html to see the original.
    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        # \0 marks indentation, so that stripping source whitespace below keeps it.
        li.insert(0, "\0\0" * (len(li.find_parents(["ul", "ol"])) - 1) + "- ")
    for block in soup.find_all(["p", "div", "li", "tr", "pre", "blockquote", "h1", "h2", "h3"]):
        block.append("\n")
    text = "\n".join(line.strip() for line in soup.get_text().splitlines()).replace("\0", " ")
    text = re.sub(r"\n\n+(?= *- )", "\n", text)  # no blank lines between list items
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def similarity(a: str, b: str) -> float:
    """0..1 likeness of two messages, ignoring case, spacing and Markdown emphasis marks."""
    # ponytail: character diff of the plain texts; fine for "did I already say this?".
    x, y = (" ".join(re.sub(r"[*_`#>]", "", t).casefold().split()) for t in (a, b))
    return SequenceMatcher(None, x, y).ratio()


SIMILAR = 0.8  # replies at least this similar to one by the same user trigger a warning


def _post(x: dict, html: bool = False) -> Post:
    return Post(
        id=x["id"],
        discussion=x["discussionid"],
        parent=x["parentid"],
        subject=x["subject"],
        author=x["author"]["fullname"],
        created=_time(x["timecreated"]) or "",
        modified=_time(x["timemodified"]) or "",
        message=x["message"] if html else html_text(x["message"] or ""),
        private=x["isprivatereply"],
        deleted=x["isdeleted"],
        attachments=tuple(a["url"] for a in x.get("attachments") or ()),
        can=tuple(k for k, v in x["capabilities"].items() if v is True),
        url=x["urls"]["view"],
    )


def _warn_similar(similar: list[Post]) -> bool:
    ids = ", ".join(str(x.id) for x in similar)
    warnings.warn(f"You already posted a very similar reply in this discussion: {ids}")
    return True


# One terminal-login step: tag visible text fields and clickable elements with
# data-moodlectl keys, and return them with the page's visible text.
STEP_JS = r"""() => {
 const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
 const textOf = e => (e.innerText || e.value || e.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
 document.querySelectorAll('[data-moodlectl]').forEach(e => e.removeAttribute('data-moodlectl'));
 const tag = (e, key) => { e.setAttribute('data-moodlectl', key); return key; };
 const skip = /^(hidden|submit|button|checkbox|radio|image|reset|file)$/;
 const fields = [...document.querySelectorAll('input, textarea')]
   .filter(e => vis(e) && !e.disabled && !e.readOnly && !skip.test(e.type))
   .map((e, i) => ({key: tag(e, 'f' + i), secret: e.type === 'password',
     user: e.type === 'email' || /user|mail|login/i.test(`${e.name} ${e.id} ${e.autocomplete}`),
     label: e.labels?.[0]?.innerText.trim() || e.placeholder || e.getAttribute('aria-label') || e.name || e.type}));
 const all = [...document.querySelectorAll(
   'a[href], button, input[type=submit], input[type=button], [role=button], [onclick]')].filter(vis);
 const seen = new Set();
 const actions = all.filter(e => !all.some(o => o !== e && e.contains(o)))
   .map(e => [e, textOf(e)]).filter(([e, t]) => t && !seen.has(t) && seen.add(t))
   .map(([e, t], i) => ({key: tag(e, 'a' + i), label: t.slice(0, 80)}));
 return {text: document.body.innerText.replace(/\s+/g, ' ').trim().slice(0, 1000), fields, actions};
}"""


class BrowserMoodle(AbstractContextManager):
    """Browser-backed Moodle operations using a persistent browser SSO profile.

    Start with `with BrowserMoodle(Settings.load()) as moodle:`. The browser is
    visible so the user can complete Unibo SSO/MFA using official login pages.
    It starts on first navigation, so reads served from the page cache need none.
    """

    def __init__(self, settings: Settings, *, headless: bool = False, timeout_ms: int = 25000):
        self.settings = settings
        self.site = settings.site
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._pw: Any = None
        self.context: Any = None
        self.page: Page | None = None
        self._profile_dir = settings.browser_dir
        self._wrote = False

    def __enter__(self):
        return self

    def _start(self) -> Page:
        if self.page is not None:
            return self.page
        self.settings.browser_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.settings.browser_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.settings.browser_dir.chmod(0o700)
        self._pw = sync_playwright().start()
        choice = self.settings.browser
        candidates = auto_browsers() if choice == "auto" else (choice,)
        errors = []
        try:
            for name in candidates:
                # Engines cannot share a profile; Chromium channels share one.
                engine = name if name in OWN_ENGINES else "chromium"
                self._profile_dir = self.settings.browser_dir / engine
                self._profile_dir.mkdir(exist_ok=True, mode=0o700)
                try:
                    self.context = getattr(self._pw, engine).launch_persistent_context(
                        str(self._profile_dir),
                        headless=self.headless,
                        accept_downloads=True,
                        viewport={"width": 1380, "height": 960},
                        **({} if engine in OWN_ENGINES else {"channel": name}),
                    )
                    break
                except PWError as exc:
                    errors.append(f"{name}: {exc.message.strip().splitlines()[0]}")
            else:
                raise moodlectlError(
                    "No usable browser found (" + "; ".join(errors) + "). Install one with "
                    "`playwright install webkit|firefox|chromium`, or install Chrome or Edge."
                )
            if self._session_file.is_file():
                self.context.add_cookies(json.loads(self._session_file.read_text("utf-8")))
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.set_default_timeout(self.timeout_ms)
        except Exception:
            self._pw.stop()
            self._pw = None
            raise
        return self.page

    @property
    def _session_file(self):
        return self._profile_dir / "moodlectl-session-cookies.json"

    def __exit__(self, *_):
        if self._wrote:
            self.clear_cache()
        if self.context is not None:
            # Browsers drop session cookies (e.g. MoodleSession) on close; keep them
            # next to the profile, with the same protection, and restore on next start.
            session = [c for c in self.context.cookies() if c.get("expires", -1) == -1]
            self._session_file.write_text(json.dumps(session), "utf-8")
            os.chmod(self._session_file, 0o600)
            self.context.close()
        if self._pw is not None:
            self._pw.stop()
        self._pw = self.context = self.page = None

    def _goto(self, path: str, *, login_ok: bool = False) -> Page:
        self._start()
        assert self.page is not None
        self.page.goto(f"{self.site}{path}", wait_until="domcontentloaded")
        if not login_ok:
            self._require_login()
        return self.page

    def _require_login(self) -> None:
        assert self.page is not None
        url = urlsplit(self.page.url)
        # SAML/OIDC systems may redirect to an entirely different domain.
        site_path = urlsplit(self.site).path.rstrip("/")
        if url.netloc != urlsplit(self.site).netloc or url.path.startswith(
            (site_path + "/login/", site_path + "/auth/")
        ):
            raise LoginRequired(
                "Moodle redirected to login/SSO. Run `moodlectl login` interactively "
                "using the same --profile and --site."
            )
        if self.page.locator('form#login, input[name="password"]').count():
            raise LoginRequired("Moodle login form displayed; run `moodlectl login` first")

    def _scrape(self, path: str, extract: Any) -> Any:
        """`extract(page)` at `path`, as JSON data cached per URL for `cache_ttl` seconds."""
        key = hashlib.sha256(f"{self.site}{path}".encode()).hexdigest()
        file = self.settings.cache_dir / f"{key}.json"
        ttl = 0 if self._wrote else self.settings.cache_ttl
        # Explicit ttl check: on Windows a fresh mtime can be ahead of time.time().
        if ttl > 0 and file.is_file() and time.time() - file.stat().st_mtime < ttl:
            return json.loads(file.read_text("utf-8"))
        data = extract(self._goto(path))
        file.parent.parent.mkdir(exist_ok=True, mode=0o700)
        file.parent.mkdir(exist_ok=True, mode=0o700)
        file.write_text(json.dumps(data), "utf-8")
        os.chmod(file, 0o600)
        return data

    def clear_cache(self) -> None:
        shutil.rmtree(self.settings.cache_dir, ignore_errors=True)

    def _writing(self) -> None:
        # Writes read live pages (dedup/verify) and drop the cache on exit.
        self._wrote = True

    def login(self) -> None:
        """Interactive SSO in the visible browser, or step by step in the terminal if headless."""
        self._goto("/my/courses.php", login_ok=True)
        if self.headless:
            self._terminal_login()
        else:
            print("Complete the University's SSO/MFA login in the opened browser window.")
            input("When the authenticated Moodle page is visible, press Enter here: ")
        # Recheck a protected route; do not persist passwords or request tokens.
        p = self._goto("/my/courses.php")
        if p.locator("body.notloggedin, #page-login-index").count():
            raise LoginRequired("Moodle does not report an authenticated account")

    def _terminal_login(self) -> None:
        """Walk the SSO pages headlessly: show each page's text, prompt for its fields,
        and let the user pick what to click. Passwords are read with getpass and only
        typed into the page; they are never printed or stored.
        """
        # ponytail: main frame only; add frame traversal if an IdP puts its form in an iframe.
        try:
            self._terminal_login_steps()
        except EOFError:
            raise LoginRequired("Login aborted: no more terminal input") from None

    def _terminal_login_steps(self) -> None:
        p = self.page
        assert p is not None
        while True:
            try:
                self._require_login()
                return
            except LoginRequired:
                pass
            step = p.evaluate(STEP_JS)
            fields, actions = step["fields"], step["actions"]
            print(f"\n--- {p.title() or urlsplit(p.url).netloc} ---\n{step['text']}\n")
            for field in fields:
                env = "MOODLE_PASSWORD" if field["secret"] else "MOODLE_USERNAME"
                value = os.environ.get(env) if field["secret"] or field["user"] else None
                if value:
                    print(f"{field['label']}: (from {env})")
                else:
                    value = (getpass if field["secret"] else input)(f"{field['label']}: ")
                if value:
                    p.locator(f'[data-moodlectl="{field["key"]}"]').fill(value)
            for i, action in enumerate(actions, 1):
                print(f"  {i}) {action['label']}")
            default = "submit" if fields else "wait for the page to change"
            while True:
                choice = input(f"Choose 1-{len(actions)}, Enter = {default}, q = quit: ").strip()
                if choice == "q":
                    raise LoginRequired("Login aborted")
                if choice == "" or (choice.isdigit() and 1 <= int(choice) <= len(actions)):
                    break
            if choice:
                p.locator(f'[data-moodlectl="{actions[int(choice) - 1]["key"]}"]').click()
            elif fields:
                p.locator(f'[data-moodlectl="{fields[-1]["key"]}"]').press("Enter")
            p.wait_for_timeout(1500)
            try:
                p.wait_for_load_state("networkidle", timeout=self.timeout_ms)
            except PWTimeout:
                pass

    def list_courses(self, search: str | None = None) -> list[Course]:
        path = (
            "/course/search.php?" + urlencode({"search": search}) if search else "/my/courses.php"
        )

        def extract(p: Page) -> dict:
            # Moodle 4+ renders the My Courses overview via AJAX after page load.
            try:
                p.wait_for_load_state("networkidle", timeout=self.timeout_ms)
            except PWTimeout:
                pass
            links = p.eval_on_selector_all(
                'a[href*="/course/view.php?id="]',
                # Drop screen-reader/hidden spans (e.g. "Course is starred") from card links.
                """els => els.map(a => {
                  const c = a.cloneNode(true);
                  c.querySelectorAll('.sr-only, .visually-hidden, .hidden')
                    .forEach(e => e.remove());
                  return {name: c.textContent.replace(/\\s+/g, ' ').trim(), url: a.href};
                })""",
            )
            try:
                years = p.evaluate(YEARS_JS)
            except PWError:
                years = []
            return {"links": links, "years": years}

        data = self._scrape(path, extract)
        raw, years = data["links"], {cid: year for cid, year in data["years"]}
        courses: dict[int, Course] = {}
        for item in raw:
            parsed = urlsplit(item["url"])
            if parsed.netloc != urlsplit(self.site).netloc:
                continue
            ids = parse_qs(parsed.query).get("id", [])
            if not ids or not ids[0].isdigit():
                continue
            cid = int(ids[0])
            if cid not in courses or len(item["name"]) > len(courses[cid].name):
                courses[cid] = Course(cid, item["name"] or str(cid), item["url"], years.get(cid))
        result = sorted(courses.values(), key=lambda c: c.name.casefold())
        return [c for c in result if not search or search.casefold() in c.name.casefold()]

    def course_sections(self, course_id: int) -> list[Section]:
        raw: list[dict] = self._scrape(
            f"/course/view.php?id={int(course_id)}", lambda p: p.evaluate(SECTION_JS)
        )
        if not raw:
            raise moodlectlError("Cannot find Moodle course sections; check course access/layout")
        return [
            Section(
                number=s["number"],
                name=s["name"],
                section_id=s["section_id"],
                resources=tuple(Resource(**r) for r in s["resources"]),
            )
            for s in raw
        ]

    def select_section(self, course_id: int, selector: str | int) -> Section:
        sections = self.course_sections(course_id)
        if isinstance(selector, int) or (isinstance(selector, str) and selector.isdigit()):
            hits = [s for s in sections if s.number == int(selector)]
        else:
            exact = [s for s in sections if s.name.casefold() == str(selector).casefold()]
            hits = exact or [s for s in sections if str(selector).casefold() in s.name.casefold()]
        if not hits:
            raise moodlectlError(
                f"Section {selector!r} not found in course {course_id}. "
                f"Available: {[(s.number, s.name) for s in sections]}"
            )
        if len(hits) > 1:
            raise AmbiguousMatch(f"Section selector {selector!r} matched multiple sections")
        return hits[0]

    def list_resources(self, course_id: int, section: str | int | None = None) -> list[Resource]:
        if section is not None:
            return list(self.select_section(course_id, section).resources)
        return [
            resource
            for section in self.course_sections(course_id)
            for resource in section.resources
        ]

    def _edit_form(
        self,
        *,
        course_id: int | None = None,
        section_no: int | None = None,
        kind: str = "url",
        cmid: int | None = None,
    ) -> Page:
        if cmid is not None:
            path = "/course/modedit.php?" + urlencode({"update": cmid, "return": 0})
        else:
            if course_id is None or section_no is None:
                raise moodlectlError("course_id and section_no required for creation")
            path = "/course/modedit.php?" + urlencode(
                {
                    "add": kind,
                    "course": course_id,
                    "section": section_no,
                    "return": 0,
                    "beforemod": 0,
                }
            )
            # Moodle's `type` parameter is optional; choose generic module type.
        p = self._goto(path)
        if not p.locator("form#mform1, form:has(#id_name)").count():
            raise MoodleFormError(f"Moodle did not display the {kind} edit form: {p.url}")
        return p

    @staticmethod
    def _locate(p: Page, selectors: str) -> Locator:
        for selector in selectors.split(" || "):
            loc = p.locator(selector).first
            if loc.count():
                return loc
        raise MoodleFormError(f"Required field missing: {selectors}")

    @staticmethod
    def _optional(p: Page, selector: str) -> Locator | None:
        loc = p.locator(selector).first
        return loc if loc.count() else None

    @staticmethod
    def _select_format(p: Page, selector: str, fmt: str) -> None:
        loc = BrowserMoodle._optional(p, selector)
        if loc is None:
            if fmt != "auto":
                raise MoodleFormError(f"Formatting selector missing: {selector}")
            return
        # Standard Moodle FORMAT_* constants: 0=Moodle, 1=HTML, 2=plain, 4=Markdown.
        format_values = {"auto": "0", "html": "1", "plain": "2", "markdown": "4"}
        if fmt not in format_values:
            raise moodlectlError(f"Invalid description format: {fmt!r}")
        value = format_values[fmt]
        if value not in loc.locator("option").evaluate_all("els => els.map(e => e.value)"):
            raise MoodleFormError(f"Formatting type {fmt} not offered by this Moodle form")
        loc.select_option(value=value)

    @staticmethod
    def _fill_field(p: Page, selector: str, value: Any) -> None:
        """Apply a declared custom UI control. Errors are explicit, never skipped."""
        loc = BrowserMoodle._locate(p, selector)
        tag = loc.evaluate("el => el.tagName.toLowerCase()")
        typ = loc.get_attribute("type")
        if tag == "select":
            if isinstance(value, dict):
                if "label" in value:
                    loc.select_option(label=str(value["label"]))
                elif "value" in value:
                    loc.select_option(value=str(value["value"]))
                else:
                    raise MoodleFormError(f"Selector {selector}: expected label or value")
            else:
                loc.select_option(value=str(value))
        elif typ in ("checkbox", "radio"):
            loc.check() if bool(value) else loc.uncheck()
        elif tag in ("input", "textarea"):
            loc.fill(str(value))
        else:
            raise MoodleFormError(f"Unsupported field widget: {selector} ({tag})")

    def _fill_rich_text(self, p: Page, field: str, text: str) -> None:
        # Support native textarea and common Atto/TinyMCE editor widgets.
        wrappers = [
            f'#fitem_id_{field} [contenteditable="true"]',
            f'#{field}editable[contenteditable="true"]',
        ]
        for selector in wrappers:
            loc = self._optional(p, selector)
            if loc and loc.is_visible():
                loc.fill(text)
                return
        selector = f'textarea#id_{field}, textarea[name="{field}[text]"]'
        loc = self._optional(p, selector)
        if loc and loc.is_visible():
            loc.fill(text)
            return
        # TinyMCE within editor iframe; scope to form component.
        frames = p.frame_locator(f"#fitem_id_{field} iframe").locator(
            'body[contenteditable="true"]'
        )
        if frames.count():
            frames.first.fill(text)
            return
        if loc:
            # An editor plugin may have kept the textarea hidden. Do not replace
            # its value behind the editor; doing so would risk saving stale text.
            raise MoodleFormError(
                f"Rich text editor for '{field}' is not recognized. "
                "Use a form-specific adapter via extra fields."
            )
        raise MoodleFormError(f"Content field '{field}' not found")

    def _populate(self, p: Page, spec: ResourceSpec, *, updating: bool = False) -> None:
        spec.validate()
        expand = p.get_by_text(re.compile(r"^(Expand all|Espandi tutto)$", re.I)).first
        if expand.count() and expand.is_visible():
            expand.click()
        self._locate(p, "#id_name").fill(spec.name)
        if spec.kind == "url":
            self._locate(p, "#id_externalurl").fill(spec.url or "")
        if spec.description:
            self._select_format(p, "#menuintroeditorformat", spec.description_format)
            desc = self._locate(p, "#id_introeditor")
            desc.fill(spec.description)
        elif not updating:
            blank = self._optional(p, "#id_introeditor")
            if blank and blank.is_visible():
                blank.fill("")
        show = self._optional(p, '#id_showdescription, input[type="checkbox"][name="showdescription"]')
        if show is not None:
            show.check() if spec.show_description else show.uncheck()
        elif spec.show_description:
            raise MoodleFormError("Moodle form lacks show-description checkbox")
        if spec.visible is not None:
            self._fill_field(p, "#id_visible", "1" if spec.visible else "0")
        if spec.display is not None:
            self._fill_field(p, "#id_display", {"label": spec.display})
        if spec.kind == "page":
            self._fill_rich_text(p, "page", spec.content or "")
        for selector, value in spec.fields.items():
            self._fill_field(p, selector, value)

    def _save(self, p: Page) -> None:
        button = self._locate(
            p, '#id_submitbutton2 || input[name="submitbutton2"] || #id_submitbutton'
        )
        button.click()
        try:
            p.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
        except PWTimeout as exc:
            raise MoodleFormError("Timed out waiting for Moodle to save") from exc
        if "/course/modedit.php" in p.url:
            errors = p.locator(
                '.invalid-feedback, .error, .alert-danger, [role="alert"]'
            ).all_text_contents()
            raise MoodleFormError(
                "Moodle did not leave the editing form. Validation errors: "
                + (" | ".join(dict.fromkeys(e.strip() for e in errors if e.strip())) or "unknown")
            )
        if urlsplit(p.url).netloc != urlsplit(self.site).netloc:
            raise MoodleFormError(f"Unexpected post-save navigation: {p.url}")

    def add_resource(
        self, course_id: int, section: str | int, spec: ResourceSpec, *, if_exists: str = "skip"
    ) -> str:
        """Create one resource; skip or error if its name+kind already exists.

        Section resolution and duplicate detection re-read the current course.
        Return 'created' or 'skipped'. No destructive overwrite occurs.
        """
        spec.validate()
        self._writing()
        target = self.select_section(course_id, section)
        existing = [
            r
            for r in target.resources
            if r.name.casefold() == spec.name.casefold() and r.kind == spec.kind
        ]
        if existing:
            if if_exists == "skip":
                return "skipped"
            raise moodlectlError(f"Resource already exists in section {target.name}: {spec.name}")
        p = self._edit_form(course_id=course_id, section_no=target.number, kind=spec.kind)
        self._populate(p, spec)
        self._save(p)
        refreshed = self.select_section(course_id, target.number)
        if not any(
            r.name.casefold() == spec.name.casefold() and r.kind == spec.kind
            for r in refreshed.resources
        ):
            raise MoodleFormError(
                f"Save returned but {spec.name!r} was not found in section {target.name}. "
                "Inspect the course before retrying."
            )
        return "created"

    def inspect_form(self, course_id: int, section: str | int, *, kind: str = "url") -> list[dict]:
        """Inspect actual Moodle input/select/textarea controls without submitting."""
        target = self.select_section(course_id, section)
        p = self._edit_form(course_id=course_id, section_no=target.number, kind=kind)
        return p.locator(
            "form#mform1 input, form#mform1 select, form#mform1 textarea"
        ).evaluate_all(
            """els => els.filter(e => e.name || e.id).map(e => ({
              id:e.id, name:e.name, type:e.type,
              options:e.tagName==='SELECT' ? [...e.options].map(o => ({value:o.value,label:o.textContent.trim()})) : null,
              checked:e.type==='checkbox' ? e.checked : null
            }))"""
        )

    def get_url_details(self, course_id: int, cmid: int) -> dict:
        """Read the destination/description from a URL's edit form (teacher role)."""
        resources = [
            r for r in self.list_resources(course_id) if r.cmid == cmid and r.kind == "url"
        ]
        if len(resources) != 1:
            raise moodlectlError(f"URL cmid={cmid} not found in course {course_id}")
        p = self._edit_form(cmid=cmid)
        desc = self._optional(p, "#id_introeditor")
        fmt = self._optional(p, "#menuintroeditorformat")
        show = self._optional(p, '#id_showdescription, input[type="checkbox"][name="showdescription"]')
        visible = self._optional(p, "#id_visible")
        display = self._optional(p, "#id_display")
        return {
            "cmid": cmid,
            "course": course_id,
            "name": self._locate(p, "#id_name").input_value(),
            "url": self._locate(p, "#id_externalurl").input_value(),
            "description": desc.input_value() if desc else None,
            "description_format_value": fmt.input_value() if fmt else None,
            "show_description": show.is_checked() if show else None,
            "visible_value": visible.input_value() if visible else None,
            "display_value": display.input_value() if display else None,
        }

    def update_url(
        self,
        course_id: int,
        cmid: int,
        *,
        name: str | None = None,
        url: str | None = None,
        description: str | None = None,
        description_format: str | None = None,
        show_description: bool | None = None,
        visible: bool | None = None,
        display: str | None = None,
        fields: dict | None = None,
    ) -> None:
        """Update a URL activity using only the fields explicitly supplied."""
        self._writing()
        existing = [r for r in self.list_resources(course_id) if r.cmid == cmid and r.kind == "url"]
        if len(existing) != 1:
            raise moodlectlError(f"URL activity cmid={cmid} not found in course {course_id}")
        if url is not None:
            ResourceSpec(kind="url", name=name or existing[0].name, url=url).validate()
        p = self._edit_form(cmid=cmid)
        if name is not None:
            if not name.strip():
                raise moodlectlError("Updated name cannot be blank")
            self._locate(p, "#id_name").fill(name)
        if url is not None:
            self._locate(p, "#id_externalurl").fill(url)
        if description is not None or description_format is not None:
            if description_format:
                self._select_format(p, "#menuintroeditorformat", description_format)
            if description is not None:
                self._locate(p, "#id_introeditor").fill(description)
        if show_description is not None:
            checkbox = self._locate(p, '#id_showdescription || input[type="checkbox"][name="showdescription"]')
            checkbox.check() if show_description else checkbox.uncheck()
        if visible is not None:
            self._fill_field(p, "#id_visible", "1" if visible else "0")
        if display is not None:
            self._fill_field(p, "#id_display", {"label": display})
        for selector, value in (fields or {}).items():
            self._fill_field(p, selector, value)
        self._save(p)
        if not any(r.cmid == cmid for r in self.list_resources(course_id)):
            raise MoodleFormError(f"Resource cmid={cmid} missing after edit; inspect manually")

    def move_resource(self, course_id: int, cmid: int, *, before: int) -> None:
        """Move module `cmid` just above module `before` (in its section), as the course
        page's drag and drop does, and check the new order."""
        if cmid == before:
            raise moodlectlError("A module cannot be moved above itself")
        self._writing()
        cmids = [r.cmid for r in self.list_resources(course_id)]
        for c in (cmid, before):
            if c not in cmids:
                raise moodlectlError(f"Module cmid={c} not found in course {course_id}")
        p = self._goto(f"/course/view.php?id={int(course_id)}")
        self._ajax(
            p,
            "core_courseformat_update_course",
            action="cm_move",
            courseid=course_id,
            ids=[cmid],
            targetcmid=before,
        )
        cmids = [r.cmid for r in self.list_resources(course_id)]
        if cmid not in cmids or cmids.index(cmid) + 1 != cmids.index(before):
            raise MoodleFormError(f"Moodle answered, but cmid={cmid} is not above cmid={before}")

    # --- Forums -------------------------------------------------------------------------

    @staticmethod
    def _ajax(p: Page, name: str, **args: Any) -> Any:
        res = p.evaluate(AJAX_JS, [name, args])
        if not res or res.get("error"):
            message = (res or {}).get("exception", {}).get("message", "no response")
            raise moodlectlError(f"Moodle refused {name}: {message}")
        return res["data"]

    def list_forums(self, course_id: int) -> list[Resource]:
        return [r for r in self.list_resources(course_id) if r.kind == "forum"]

    def list_discussions(self, forum_cmid: int) -> list[Discussion]:
        """All discussions of the forum with course-module id `forum_cmid`, every page."""
        rows: list[dict] = []
        page = 0
        while True:
            path = f"/mod/forum/view.php?id={int(forum_cmid)}&p={page}"
            data = self._scrape(path, lambda p, n=page: p.evaluate(DISCUSSIONS_JS, n))
            rows += data["rows"]
            if not data["next"]:
                break
            page += 1
        for r in rows:
            r["created"], r["modified"] = _time(r["created"]), _time(r["modified"])
        return [Discussion(**r) for r in rows]

    def _raw_posts(self, p: Page, discussion_id: int) -> dict:
        try:
            return self._ajax(
                p,
                "mod_forum_get_discussion_posts",
                discussionid=int(discussion_id),
                sortby="created",
                sortdirection="ASC",
            )
        except moodlectlError as exc:
            raise moodlectlError(
                f"Cannot read discussion {discussion_id} ({exc}). Is it a discussion id "
                "(discuss.php?d=)? A forum's id (view.php?id=) goes to `forums discussions`."
            ) from None

    def list_posts(self, discussion_id: int, *, html: bool = False) -> list[Post]:
        """Posts of a discussion, oldest first; message as plain text unless `html`."""
        path = f"/mod/forum/discuss.php?d={int(discussion_id)}"
        raw = self._scrape(path, lambda p: self._raw_posts(p, discussion_id))["posts"]
        return [_post(x, html) for x in raw]

    def _submit_post(
        self,
        path: str,
        values: dict[str, str],
        p: Page | None = None,
        attachments: Sequence[Path] = (),
    ) -> None:
        """Submit the forum post form at `path` (already open as `p`, if given)."""
        self._writing()
        p = p or self._goto(path)
        self._upload(p, attachments)
        res = p.evaluate(POST_FORM_JS, values)
        if res.get("missing"):
            detail = f": {self._main_text(p)}" if "form#mformforum" in res["missing"] else ""
            raise MoodleFormError(f"Moodle post form at {path} lacks {res['missing']}{detail}")
        if "/mod/forum/post.php" in urlsplit(res["url"]).path:
            raise MoodleFormError(
                "Moodle rejected the post: " + (" | ".join(res["errors"]) or "unknown error")
            )

    def _upload(self, p: Page, files: Sequence[Path]) -> None:
        """Upload `files` into the open post form's attachments draft area, like its
        file picker's "Upload a file" does."""
        if not files:
            return
        field = self._optional(p, 'form#mformforum input[name="attachments"]')
        if field is None:
            raise MoodleFormError("This Moodle post form does not accept attachments")
        repo = re.search(r'"(\d+)":\{"id":"\d+","name":"[^"]*","type":"upload"', p.content())
        if not repo:
            raise MoodleFormError("Moodle offers no file upload repository on this form")
        sesskey, context = p.evaluate("() => [M.cfg.sesskey, M.cfg.contextid]")
        for file in files:
            res = p.request.post(
                f"{self.site}/repository/repository_ajax.php?action=upload",
                multipart={
                    "repo_upload_file": {
                        "name": file.name,
                        "mimeType": mimetypes.guess_type(file.name)[0]
                        or "application/octet-stream",
                        "buffer": file.read_bytes(),
                    },
                    "sesskey": sesskey,
                    "repo_id": repo[1],
                    "itemid": field.input_value(),
                    "ctx_id": str(context),
                    "savepath": "/",
                    "title": file.name,
                },
            )
            try:
                data = res.json()
            except ValueError:
                data = {"error": f"HTTP {res.status}"}
            if "error" in data or data.get("event") == "fileexists":
                problem = data.get("error") or "a file with that name is already attached"
                raise moodlectlError(f"Moodle refused to upload {file}: {problem}")

    @staticmethod
    def _files(paths: Sequence[str | Path]) -> list[Path]:
        files = [Path(f) for f in paths]
        missing = [str(f) for f in files if not f.is_file()]
        if missing:
            raise moodlectlError(f"Attachment not found: {', '.join(missing)}")
        return files

    @staticmethod
    def _main_text(p: Page) -> str:
        main = p.locator('[role="main"]').first
        text = main.inner_text() if main.count() else p.title()
        return re.sub(r"\s*\n\s*", "\n", text).strip()[:1000]

    @staticmethod
    def _message_values(message: str | None, text_format: str) -> dict[str, str]:
        if message is None:
            return {}
        if text_format not in TEXT_FORMATS:
            raise moodlectlError(f"Invalid format {text_format!r}; use {list(TEXT_FORMATS)}")
        if not message.strip():
            raise moodlectlError("Message must not be blank")
        return {"message[text]": message, "message[format]": TEXT_FORMATS[text_format]}

    def add_discussion(
        self,
        forum_cmid: int,
        subject: str,
        message: str,
        *,
        text_format: str = "markdown",
        if_exists: str = "skip",
        attachments: Sequence[str | Path] = (),
    ) -> tuple[str, int]:
        """Start a discussion; skip or error if one with the same subject exists.

        Return ('created' | 'skipped', discussion id).
        """
        if not subject.strip():
            raise moodlectlError("Subject must not be blank")
        values = {"subject": subject, **self._message_values(message, text_format)}
        files = self._files(attachments)
        self._writing()
        before = {d.id: d for d in self.list_discussions(forum_cmid)}
        same = [d for d in before.values() if d.subject.casefold() == subject.casefold()]
        if same:
            if if_exists == "skip":
                return "skipped", same[0].id
            raise moodlectlError(f"Discussion already exists: {subject!r} (id {same[0].id})")
        # The forum page's inline "Add discussion" form names the forum instance; its
        # full ("Advanced") form at post.php also takes attachments.
        p = self._goto(f"/mod/forum/view.php?id={int(forum_cmid)}")
        forum = p.evaluate("() => document.forms.mformforum?.forum?.value")
        if not forum:
            raise MoodleFormError(f"Cannot start a discussion here: {self._main_text(p)}")
        self._submit_post(f"/mod/forum/post.php?forum={forum}", values, attachments=files)
        new = [d.id for d in self.list_discussions(forum_cmid) if d.id not in before]
        if len(new) != 1:
            raise MoodleFormError(f"Posted, but cannot identify the new discussion: {new}")
        return "created", new[0]

    def reply(
        self,
        post_id: int,
        message: str,
        *,
        subject: str | None = None,
        private: bool = False,
        text_format: str = "markdown",
        attachments: Sequence[str | Path] = (),
        confirm_similar: Callable[[list[Post]], bool] = _warn_similar,
    ) -> int | None:
        """Reply to a post (privately to its author if `private`); return the new post id.

        If the current user already posted a very similar message in the discussion,
        `confirm_similar(those posts)` decides whether to go on (default: warn and post);
        return None if it declines.
        """
        values = self._message_values(message, text_format)
        files = self._files(attachments)
        if subject is not None:
            values["subject"] = subject  # default: Moodle's "Re: <parent subject>"
        if private:
            values["isprivatereply"] = "1"
        self._writing()
        path = f"/mod/forum/post.php?reply={int(post_id)}"
        p = self._goto(path)
        discussion = p.evaluate("() => Number(document.forms.mformforum?.discussion?.value)")
        if not discussion:
            raise MoodleFormError(f"Cannot reply to post {post_id}: {self._main_text(p)}")
        posts = self._raw_posts(p, discussion)["posts"]
        me = p.evaluate("() => M.cfg.userId")
        similar = [
            _post(x)
            for x in posts
            if x["author"]["id"] == me
            and not x["isdeleted"]
            and similarity(message, html_text(x["message"] or "")) >= SIMILAR
        ]
        if similar and not confirm_similar(similar):
            return None
        before = {x["id"] for x in posts}
        self._submit_post(path, values, p, files)
        new = [x for x in self._raw_posts(p, discussion)["posts"] if x["id"] not in before]
        if len(new) != 1:
            raise MoodleFormError(f"Replied, but cannot identify the new post: {new}")
        if len(new[0]["attachments"] or ()) != len(files):
            raise MoodleFormError(f"Post {new[0]['id']} lacks some attachments; inspect it")
        return new[0]["id"]

    def edit_post(
        self,
        post_id: int,
        *,
        subject: str | None = None,
        message: str | None = None,
        text_format: str = "markdown",
        attachments: Sequence[str | Path] = (),
    ) -> None:
        """Change only the supplied subject and/or message of a post; add `attachments`."""
        values = self._message_values(message, text_format)
        files = self._files(attachments)
        if subject is not None:
            if not subject.strip():
                raise moodlectlError("Subject must not be blank")
            values["subject"] = subject
        if not values and not files:
            raise moodlectlError("Nothing to edit: give a subject, a message or attachments")
        self._submit_post(f"/mod/forum/post.php?edit={int(post_id)}", values, attachments=files)

    def delete_post(self, post_id: int, confirm: Any = lambda _text: True) -> bool:
        """Delete a post; deleting a discussion's first post deletes the whole discussion.

        `confirm(text)` gets Moodle's confirmation page text; returning False aborts.
        """
        self._writing()
        p = self._goto(f"/mod/forum/post.php?delete={int(post_id)}")
        button = self._optional(p, 'form:has(input[name="confirm"]) [type="submit"]')
        if button is None:
            raise MoodleFormError(f"Cannot delete post {post_id}: {self._main_text(p)}")
        if not confirm(self._main_text(p)):
            return False
        button.click()
        p.wait_for_load_state("domcontentloaded")
        if "/mod/forum/post.php" in urlsplit(p.url).path:
            raise MoodleFormError(f"Moodle did not delete post {post_id}: {self._main_text(p)}")
        return True

    def set_discussion(
        self,
        discussion_id: int,
        *,
        locked: bool | None = None,
        pinned: bool | None = None,
        favourite: bool | None = None,
        subscribed: bool | None = None,
    ) -> dict[str, bool]:
        """Lock, pin, favourite and/or subscribe a discussion; return the resulting states."""
        self._writing()
        d = int(discussion_id)
        p = self._goto(f"/mod/forum/discuss.php?d={d}")
        forum = self._raw_posts(p, d)["forumid"]
        state: dict[str, bool] = {}
        if locked is not None:
            # Moodle's targetstate is the current lock time: nonzero unlocks, 0 locks.
            res = self._ajax(
                p,
                "mod_forum_set_lock_state",
                forumid=forum,
                discussionid=d,
                targetstate=0 if locked else 1,
            )
            state["locked"] = res["locked"]
        if pinned is not None:
            res = self._ajax(p, "mod_forum_set_pin_state", discussionid=d, targetstate=int(pinned))
            state["pinned"] = res["pinned"]
        if favourite is not None:
            res = self._ajax(
                p, "mod_forum_toggle_favourite_state", discussionid=d, targetstate=favourite
            )
            state["favourite"] = res["userstate"]["favourited"]
        if subscribed is not None:
            res = self._ajax(
                p,
                "mod_forum_set_subscription_state",
                forumid=forum,
                discussionid=d,
                targetstate=subscribed,
            )
            state["subscribed"] = res["userstate"]["subscribed"]
        return state
