# AGENTS.md

Notes for coding agents and contributors. User-facing usage (setup, CLI, YAML
manifests, Python API, limitations) lives in `README.md`; do not duplicate it here.

## Layout

```
src/moodlectl/
  cli.py        Typer app; sub-apps: courses, sections, resources, forums, discussions,
                posts, config, api, manifest, cache.
                Entry point `main()` turns `moodlectlError` into "Error: ..." + exit 1.
  browser.py    BrowserMoodle: Playwright persistent-browser backend (SSO session).
                Scrapes course pages (SECTION_JS) and drives /course/modedit.php forms.
  rest.py       MoodleREST: token-based /webservice/rest/server.php client (httpx),
                plus flatten_moodle_params() for Moodle's bracket-form params.
  models.py     Frozen dataclasses: Course, Section, Resource, ResourceSpec (+ validate()),
                Discussion, Post.
  manifest.py   YAML -> Batch(course, section, resources) with strict validation.
  importers.py  HTML table (title/PDF/online columns) -> Batch; write_manifest() -> YAML.
  config.py     Settings per profile (XDG paths, 0600/0700 perms), normalized_site().
  errors.py     moodlectlError (sic, lowercase) and subclasses LoginRequired,
                AmbiguousMatch, MoodleFormError.
tests/          pytest unit tests: browser, cli, config, forums, importers, manifest, rest.
examples/       module2.yaml manifest and python_api.py (writes are commented out).
```

## Commands

```bash
uv sync --all-groups          # env + dev tools (CI uses plain `uv sync`)
uv run poe test               # pytest
uv run poe static-checks      # compileall, ruff format --check, ruff check, mypy
uv run poe static-fixes       # ruff --fix + ruff format
uv run poe coverage && uv run poe coverage-report
```

CI (`.github/workflows/check.yml`) runs static-checks + coverage on 3.12, then tests
on ubuntu/windows/macos x Python 3.10-3.13, then the semantic-release deploy.

## Implementation status (as of 2026-10)

- Working and unit-tested: manifest loading/validation, table import, config/profile
  handling, REST client and param flattening.
- Validated live on Virtuale but only partly unit-tested (browser selection, session
  cookies, page cache): login and course/section/resource scraping in `browser.py`.
- Implemented but **not validated against a live Moodle**: resource add/update/inspect
  forms. Their selectors were derived from screenshots of Unibo Virtuale.
- Supported writes: create `url`, `page` and `resource` (File), update `url` and
  `resource`. A File's upload reuses the forum attachments' draft upload (`_upload`,
  now taking the file field's selector); `update_file` first empties the draft area
  through `draftfiles_ajax.php` (`list`, `delete`), and both writes then download
  `mod/resource/view.php?id=<cmid>&redirect=1` and compare it with the local file.
  Validated live on Virtuale (2026-10-13): `add-file`, `update-file` with and without a
  file, `--visible`. There the form's id is `mform1_<random>` (match `form:has(#id_name)`),
  section toggles work only after the `load` event (`_expand_all`), and site CSS hides
  `#fitem_id_visible`, so visibility goes through `core_course_edit_module` show/hide
  (`_set_visible`). `resources list` still reports hidden modules as visible.
  Other `kind`s go through the generic `modedit.php?add=<kind>` path and need
  site-specific `fields`.
- Forums (`list_forums`, `list_discussions`, `list_posts`, `add_discussion`, `reply`,
  `edit_post`, `delete_post`, `set_discussion`) were validated live on Virtuale (Moodle
  4.5). There only flagged-`ajax` web services work via `AJAX_JS`: the post listing and
  lock/pin/favourite/subscribe toggles do; get_forums/add_discussion/update/delete
  answer `servicenotavailable`, so writes submit `form#mformforum` via `POST_FORM_JS`.
  `mod_forum_set_lock_state`'s `targetstate` is the current lock time (0 locks).
  Attachments go to the form's draft itemid via `repository_ajax.php?action=upload`
  (`page.request`, same cookies), repo id scraped from the page's filepicker options.
- Not implemented: `label` (rejected in `ResourceSpec.validate()`), File resources
  with folders or several files, resource deletion, section creation, access restrictions.
- `static-checks` and tests pass; keep it that way before committing.

## Conventions and gotchas

- Raise `moodlectlError` (or a subclass) for anything the user can act on; the CLI
  prints its message. Never let raw Playwright/httpx exceptions be the UX.
- Writes must stay non-destructive and idempotent: `add_resource` re-reads the
  section, dedups on `(kind, name.casefold())`, and verifies the result after save.
  `resources sync` previews unless `--apply`. Keep that contract.
- Form helpers: `_locate()` accepts alternatives separated by `" || "` (custom, not
  Playwright syntax) and raises if none match; `_optional()` returns `None`.
  `_fill_field()` maps str/number -> fill, bool -> checkbox, `{label|value}` -> select.
- Unrecognized form controls must raise `MoodleFormError`, never be silently skipped.
- Security: never store or print SSO passwords (only `_terminal_login` reads one, via
  getpass or `MOODLE_PASSWORD`, and types it into the page); the profile dir is the session. `normalized_site()` rejects plain HTTP except localhost.
  REST token comes only from `MOODLE_WS_TOKEN` or the constructor.
- `BrowserMoodle` is headed by default (`headless=False`). The CLI's
  `--headless/--headed` (`MOODLE_HEADLESS`) sets `Settings.headless`; unset (None)
  means headed for `login` and headless for everything else (see `cli.browser()`).
  `Settings.browser` (CLI `--browser`, not persisted) picks the
  browser: `firefox`/`webkit` (Playwright builds) or a Chromium channel; `auto`
  follows `auto_browsers()`. Each engine has its own profile subdir
  (`<browser_dir>/{chromium,firefox,webkit}`).
- `--headless login` runs `_terminal_login()`: a generic loop over SSO pages driven
  by `STEP_JS` (main frame only), not tied to any IdP's selectors. Keep it generic.
- The browser starts lazily on the first `_goto()`. `_scrape(path, extract)` caches
  `extract(page)` JSON per URL in `Settings.cache_dir` for `cache_ttl` seconds
  (`--cache-ttl`/`--no-cache`); only listings use it, edit forms are always live.
  Write methods call `_writing()`: later reads skip the cache and `__exit__` clears it.
- `__exit__` saves session cookies (expires == -1) to
  `<browser_dir>/moodlectl-session-cookies.json` (0600); `__enter__` restores them.
  Without this, the browser drops `MoodleSession` and every run would need a new login.
- `tests/test_live.py` is the only exception to the next rule: opt-in
  (`MOODLE_LIVE_TEST=1` + credentials), run with `poe test-live`.
- Tests must not hit the network or a browser: use `httpx.MockTransport`-style
  fakes / static HTML (see `tests/test_rest.py`, `tests/test_importers.py`).
- Ruff: line length 100, target py310. Code must run on Python 3.10.

## Releases

Conventional Commits drive semantic-release (`release.config.mjs`), which runs
`uv version <next> && uv build`, then `uv publish`. `pyproject.toml` is the only
version source; `moodlectl.__version__` reads it via `importlib.metadata`.
