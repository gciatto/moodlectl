# moodlectl

A Python CLI and reusable API to inspect Moodle courses, manage learning
resources, and read and write forum discussions (posts, replies, attachments),
including Moodle instances whose users authenticate using a university
SSO provider (SAML / Shibboleth / OIDC). Designed for Moodle 4.x and 5.x forms.

This is an independent utility, not an official University of Bologna/Moodle integration.

## Why two backends?

* **Browser automation (default)** uses Playwright (WebKit, Firefox, Chromium, Chrome or Edge). You authenticate
  using the institution's real browser SSO/MFA flow; no passwords or SSO bypasses.
  This is the practical route for creating standard URL resources when REST tokens
  are not enabled or when a create-activity web service is not provisioned.
* **Moodle REST (optional)** can call external functions authorized for an existing
  Moodle web-service token. Moodle web services are configurable per installation:
  the client cannot enable them for you. `moodlepy` is a relevant library for
  this interface, but it neither solves SSO nor supplies an unrestricted
  create-activity endpoint. This project uses a tiny generic REST wrapper instead.

## Install

Requires Python >= 3.10 on macOS, Linux or Windows. `moodlectl` is on
[PyPI](https://pypi.org/project/moodlectl/); install it as a standalone command with
[uv](https://docs.astral.sh/uv/getting-started/installation/) (recommended) or
[pipx](https://pipx.pypa.io/), or with plain `pip` into an environment of your choice:

```bash
uv tool install moodlectl        # or: pipx install moodlectl
pip install moodlectl            # e.g. inside a virtual environment
```

Upgrade later with `uv tool upgrade moodlectl` (or `pipx upgrade moodlectl`,
`pip install -U moodlectl`).

If PyPI is not an option, install from GitHub instead: the wheel attached to a
[release](https://github.com/gciatto/moodlectl/releases), or any tag/branch built
from source:

```bash
pip install https://github.com/gciatto/moodlectl/releases/download/1.0.0/moodlectl-1.0.0-py3-none-any.whl
pip install "git+https://github.com/gciatto/moodlectl@main"
```

(`uv tool install` and `pipx install` accept the same URLs.)

`moodlectl` drives a browser through Playwright. An installed Google Chrome (or
Microsoft Edge on Windows) is used as is; otherwise install Playwright's Chromium once
(see `--browser` below for other choices):

```bash
uvx --from moodlectl playwright install chromium   # uv tool / pipx installs
python -m playwright install chromium              # pip installs, same environment
```

## Setup

Point `moodlectl` at your Moodle site and log in once:

```bash
moodlectl config set-site https://virtuale.unibo.it
moodlectl login
```

To work on `moodlectl` itself, clone the repository and run `uv sync --all-groups`
instead: it creates `.venv` with the project plus test and development tools (see
[`AGENTS.md`](AGENTS.md)); prefix commands with `uv run`, or `source .venv/bin/activate`.

`login` opens a **visible, dedicated browser window**. Complete the normal
Unibo SSO process there, including MFA when requested. When Moodle is visible,
return to the terminal and press Enter. The persistent browser profile
is reused by subsequent commands (session cookies are kept in the profile directory
too, since the browser would otherwise drop them on exit). In this mode the tool never
sees your password. Use the same `--profile` for login and later operations.

Without a display (e.g. over SSH), use `moodlectl --headless login`: no window
opens, and each SSO page is shown in the terminal as text, followed by prompts
for its fields (passwords via a hidden prompt, never stored) and a numbered list
of buttons/links to click. Press Enter on a page with no fields to wait, e.g. while
approving an MFA push. If `MOODLE_USERNAME` / `MOODLE_PASSWORD` are set, username
and password fields are filled from them instead of prompting (set the password with
`read -s` rather than typing it on the command line).

All other commands run headless by default, since they only reuse the saved session;
pass `--headed` (or `MOODLE_HEADLESS=0`) to watch the browser, e.g. when debugging a
form. `MOODLE_HEADLESS=1` makes `login` headless too.

`--browser` (or `MOODLE_BROWSER`), placed before the subcommand, picks the browser:

| Value | Browser | Install |
|---|---|---|
| `webkit` | Playwright's WebKit (Safari's engine) | `playwright install webkit` |
| `firefox` | Playwright's Firefox build | `playwright install firefox` |
| `chromium` | Playwright's Chromium | `playwright install chromium` |
| `chrome`, `msedge` | the installed Google Chrome / Microsoft Edge | none |

The default, `auto`, tries Edge on Windows and Firefox on Linux, then `chromium`,
then `chrome`; on macOS it tries `chrome`, then `chromium`. WebKit is not used by
default: it rejects the Shibboleth session cookie, so Unibo SSO login loops.
Playwright cannot drive the installed Safari or Firefox apps, only its own
builds. Each engine keeps its own login session, so log in again after switching
between engines (Chromium, Chrome and Edge share one).

## CLI essentials

Commands print their results as YAML (multi-line texts, such as forum messages, as
`|` blocks), so they are readable and can be piped to tools like `yq`.
Listings accept `--sort KEY` (any key of the listed items) and `--reverse`. Dates sort
most recent first. Defaults: courses by `name`, sections by `number`, discussions by
`modified`; resources and forums stay in page order, posts in chronological order.
Add `--pretty` for human-readable output: tables, and for `discussions show` the
conversation as an indented reply tree with author/date headers.

```bash
# Read courses from your My Courses view (this view may be paginated)
moodlectl courses list

# Search the site's visible course catalogue
moodlectl courses list --search "Agenti Intelligenti"

# Set the default course for the current profile
moodlectl courses use 80509

moodlectl sections list
moodlectl resources list --section "Module 2"

# Create an URL pointing to a PDF, with visible Markdown description
moodlectl resources add-url "L2 - My lecture" \
  "https://example.org/slides.pdf" \
  --section "Module 2" \
  --description '[Online slides](https://example.org/slides/)' \
  --format markdown \
  --show-description

# Update an existing URL module by course-module ID (cmid)
moodlectl resources update-url 12345 --name "Updated title" \
  --url https://example.org/new.pdf --description '[Slides](https://example.org/)'

# Upload a local file as a File resource, then replace its file in place: the cmid,
# and so every link to the resource, stays; the served file is checked after saving
moodlectl resources add-file "M5 - Agents" slides/m5.pdf --section "Module 2"
moodlectl resources update-file 12346 slides/m5.pdf
moodlectl resources update-file 12346 --fields '{"#id_showsize": true}'  # settings only

# Create a Page resource (editor support varies per site, see below)
moodlectl resources add-page "Course info" --content "<p>Hello</p>" --section "Module 2"

# Inspect the real URL field, visibility, description etc.
moodlectl resources show 12345

# Forums: list them, their discussions, and a discussion's posts (as plain text, or --html)
moodlectl forums list
moodlectl discussions list 2263032            # forum cmid
moodlectl discussions show 230424             # discussion id
moodlectl discussions list 'https://virtuale.unibo.it/mod/forum/view.php?id=2263032'  # URLs too
# Write: start a discussion, reply (optionally privately), edit, delete, set states
moodlectl discussions add 2263032 "Office hours" -m "Moved to **Friday**"   # --format markdown
moodlectl posts reply 315396 -m "Thanks!" --private                      # post id
moodlectl posts reply 315396 -m "Slides attached" -a l1.pdf -a l2.pdf   # --attach, repeatable
echo "Longer text" | moodlectl posts edit 315849 -m - --subject "New subject"
moodlectl posts delete 315849                 # asks after showing Moodle's confirmation; -y skips
moodlectl discussions delete 230424           # deletes the whole discussion (same prompt)
moodlectl discussions set 230424 --lock --pin --favourite --unsubscribe

# Course, section and forum listings are cached per URL for 1h (in the temp dir)
moodlectl --cache-ttl 600 sections list   # or MOODLE_CACHE_TTL=600
moodlectl --no-cache sections list        # or MOODLE_NO_CACHE=1; refreshes the cache
moodlectl cache clear

# See all form control selectors and select options for customization
moodlectl resources inspect-form --section "Module 2" --kind url

# Generate an actual YAML manifest from the current public APICe slide table
moodlectl manifest from-table \
  'https://apice.unibo.it/bin/view/Course/Iag2627M2/?mode=data&data=course&display=Slides' \
  --course 80509 --section "Module 2" --output module2-2026.yaml

# Batch from a YAML file: preview first, then apply
moodlectl resources sync module2-2026.yaml
moodlectl resources sync module2-2026.yaml --apply

# Provisioned native Moodle REST functions (token supplied via environment)
export MOODLE_WS_TOKEN='YOUR_PROVISIONED_TOKEN'
moodlectl api courses
moodlectl api contents --course 80509
moodlectl api call mod_url_get_urls_by_courses \
  --params '{"courseids":[80509]}'
```

Use `--profile NAME` and `--site URL` (or `MOODLE_SITE_URL`) **before** the subcommand to select an
independent account or Moodle installation:

```bash
moodlectl --profile teaching --site https://virtuale.unibo.it login
moodlectl --profile teaching courses use 80509
moodlectl --profile teaching resources list --section "Module 2"
```

The saved default site/course are in
`~/.config/moodlectl/PROFILE.json` (or `$XDG_CONFIG_HOME`), and the browser
profile is under `~/.local/share/moodlectl/profiles/PROFILE` (or
`$XDG_DATA_HOME`); `moodlectl config show` prints both. Treat the browser profile as an authenticated session.

## YAML batch files

```yaml
course: 80509
section: "Module 2"
resources:
  - kind: url
    name: "L1 - Intro"
    url: "https://example.org/l1.pdf"
    description: "[Rolling version](https://example.org/l1/)"
    description_format: markdown
    show_description: true
    visible: false
    display: "Open" # Must match a dropdown label in the site's UI; optional
    fields:
      "#id_visible": "0" # Arbitrary form fields; optional
  - kind: resource # a File resource
    name: "M5 - Agents"
    file: "slides/m5.pdf" # Relative to this manifest
```

`resources sync` is a **preview** unless `--apply` is provided. The batch stops
on the first form error and prints the successful operations completed so far.
Names are checked for duplicates within the selected section (kind + name):
existing resources are skipped by default, or `--if-exists error` can be used.
It does not delete or overwrite anything. `--fields` on CLI accepts the same
custom field mapping in JSON. A field key is a CSS selector; choose values
such as a string, a boolean for a checkbox, or `{label: ...}` for dropdowns.

The browser supports `url` activities as the stable, screenshot-grounded
workflow. `get_url_details` and `inspect_form` expose read-only inspection of
URL settings and arbitrary Moodle form controls; their CLI counterparts are
`resources show` and `resources inspect-form`. The generic resource creator also
accepts named module kinds (e.g. `forum` or `assign`) with site-specific required
fields provided in `fields`, but those kinds are experimental. An adapter
for Moodle `page` resources is also included; it recognizes plain textarea,
Atto contenteditable and TinyMCE iframe configurations.
`page` remains dependent on the specific site's editor configuration; test on
an expendable course first. `resource` (File) modules take a local `file`:
it is uploaded through the form's file manager, which is emptied first on
`update-file`, and the file Moodle serves afterwards is compared with it. Labels, activity deletion, complex
access restrictions and section creation are not implemented. Custom form
fields accommodate additional activity settings where Moodle exposes them.

## Forums

Forums are course resources of kind `forum`. Commands follow Moodle's hierarchy:
`forums` (find them), `discussions` (take a forum cmid, list/read/start/manage its
discussions), `posts` (take a post id: reply, edit, delete). Forums and discussions
can also be given as pasted `view.php?id=` / `discuss.php?d=` URLs; ids are shown by
the listing commands.

Reading uses Moodle's own AJAX service (`mod_forum_get_discussion_posts`) through
the browser session; starting, replying to, editing and deleting posts submit the
same `mod/forum/post.php` forms as the web UI, and lock/pin/favourite/subscribe call
the UI's AJAX toggles. Discussion lists and posts are cached like other listings
(`--no-cache` for fresh ones).

`discussions add` skips (or `--if-exists error`) when a discussion with the same
subject exists. `posts reply` warns, and asks before posting (`-y` skips), when you
already posted a very similar message (>= 80% alike) in the discussion. `--attach`
on `discussions add`, `posts reply` and `posts edit` uploads files like the form's
file picker (`edit` adds to the existing ones; same-named files are refused);
`discussions show` lists attachment URLs. Deleting a discussion's first post deletes
the whole discussion. Not implemented: removing attachments, splitting/moving
discussions, ratings, export, read tracking, group selection.

## Programmatic Python API

```python
from moodlectl import BrowserMoodle, ResourceSpec
from moodlectl.config import Settings

with BrowserMoodle(Settings.load()) as moodle:
    for section in moodle.course_sections(80509):
        print(section.number, section.name)
        for resource in section.resources:
            print(resource.kind, resource.cmid, resource.name, resource.url)

    status = moodle.add_resource(
        80509,
        "Module 2",
        ResourceSpec(
            kind="url",
            name="L2 - Agents",
            url="https://example.org/l2.pdf",
            description="[Online](https://example.org/l2/)",
            description_format="markdown",
            show_description=True,
            fields={"#id_display": {"label": "Open"}},
        ),
    )
    print(status)  # 'created' or 'skipped'

    # Forums -> discussions -> posts (messages as plain text; html=True for Moodle's HTML)
    for forum in moodle.list_forums(80509):
        for discussion in moodle.list_discussions(forum.cmid):
            for post in moodle.list_posts(discussion.id):
                print(post.author, post.created, post.subject, post.attachments)
                print(post.message)

    new_post = moodle.reply(315396, "Slides **attached**", attachments=["l1.pdf"])
    moodle.edit_post(new_post, message="Slides attached (updated)")
    moodle.set_discussion(230424, pinned=True)
```

Write methods mirror the CLI: `add_discussion`, `reply` (its `confirm_similar`
callback decides on near-duplicate replies; by default it warns and posts),
`edit_post`, `delete_post` (its `confirm` callback sees Moodle's confirmation text),
`set_discussion`.

REST usage:

```python
from moodlectl import MoodleREST

with MoodleREST("https://virtuale.unibo.it") as ws:
    print(ws.site_info())
    print(ws.course_contents(80509))
    print(ws.call("mod_url_get_urls_by_courses", courseids=[80509]))
```

## Limitations and operational notes

* Validated live on Virtuale (Moodle 4.5): login, course/section/resource listings
  and all forum commands. Resource **writes** (`add-url`, `add-page`, `update-url`,
  `add-file`, `update-file`, `sync`) match screenshots of the forms but are not yet validated live. Site
  plugins, roles, Moodle versions and updates can change selectors or permissions.
* Moodle core REST exposes course reading and module management functions,
  but resource creation is not universally exposed as a generic external
  function. A REST token must be provisioned with adequate functions/capabilities
  by the site operator; SSO cookies are not a REST `wstoken`.
* Browser course discovery is limited to course links rendered in the My Courses
  or course-search page. Explicit `--course` works even if the list is incomplete.
* The browser profile stores authenticating cookies; protect it as you would
  a web browser user profile. Passwords are never stored; only
  `--headless login` reads one, and passes it straight to the SSO page.
* Moodle rich-text editors and course themes vary: unrecognized controls raise
  a clear `MoodleFormError` instead of silently submitting incomplete data.
* A stable independent test Moodle instance or a dedicated test course is
  recommended for live integration validation before making bulk course edits.

## Tests

```bash
uv run poe test
MOODLE_LIVE_TEST=1 uv run poe test-live   # real SSO login per browser, see tests/test_live.py
uv run poe static-checks
uv run poe format-check
```

Unit tests cover manifest validation, HTML table import, configuration, REST parameter
serialization, browser selection, session cookies and page cache, CLI headless defaults,
forum message rendering, near-duplicate detection and forum URL parsing. Integration against live Virtuale requires running
`moodlectl login` locally with an authorized account; no stored credentials
or Moodle tokens are included.

Contributor/agent notes (code layout, conventions, implementation status) are in
[`AGENTS.md`](AGENTS.md).

## Releases and dependency updates

Releases use semantic-release and Conventional Commit messages. Pushes to
`main` or `master` can publish the package to PyPI; other branches and the
initial commit run in dry-run mode. Configure `PYPI_TOKEN` and `RELEASE_TOKEN`
as repository secrets before enabling releases. `TEST_PYPI_TOKEN` is used when
the repository is a template or its name contains `template`.

Renovate is configured in `renovate.json` to open dependency update pull
requests and enable automerge after checks pass. Install the Renovate GitHub App
and enable pull request automerge in repository settings to activate it.

## References

* Moodle native web services: https://moodledev.io/docs/5.0/apis/subsystems/external
* Web service function list: https://docs.moodle.org/dev/Web_service_API_functions
* `moodlepy`: https://pypi.org/project/moodlepy/
* Playwright Python: https://playwright.dev/python/
