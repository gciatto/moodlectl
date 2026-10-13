"""Typer CLI. Run `moodlectl --help` for all commands."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, Optional, cast
from urllib.parse import parse_qs, urlsplit

import typer
import yaml
from rich.console import Console, Group, RenderableType
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from .browser import BrowserMoodle
from .config import Settings, normalized_site
from .errors import moodlectlError
from .manifest import load_manifest
from .importers import import_table, write_manifest
from .models import Format, Post, ResourceSpec
from .rest import MoodleREST

app = typer.Typer(no_args_is_help=True, help="Moodle resources CLI (browser SSO + REST)")
courses = typer.Typer(no_args_is_help=True, help="Discover and select a course")
sections = typer.Typer(no_args_is_help=True, help="Browse course sections")
resources = typer.Typer(no_args_is_help=True, help="List, create, and update resources")
config = typer.Typer(no_args_is_help=True, help="Manage local configuration")
api = typer.Typer(no_args_is_help=True, help="Call Moodle native REST web services")
manifest = typer.Typer(no_args_is_help=True, help="Build declarative YAML manifests")
cache = typer.Typer(no_args_is_help=True, help="Manage the local page cache")
forums = typer.Typer(no_args_is_help=True, help="Find a course's forums")
discussions = typer.Typer(no_args_is_help=True, help="List, read, start and manage discussions")
posts = typer.Typer(no_args_is_help=True, help="Reply to, edit and delete forum posts")
app.add_typer(courses, name="courses")
app.add_typer(sections, name="sections")
app.add_typer(resources, name="resources")
app.add_typer(config, name="config")
app.add_typer(api, name="api")
app.add_typer(manifest, name="manifest")
app.add_typer(cache, name="cache")
app.add_typer(forums, name="forums")
app.add_typer(discussions, name="discussions")
app.add_typer(posts, name="posts")


@app.callback()
def global_options(
    ctx: typer.Context,
    profile: str = typer.Option("default", "--profile", help="Isolated SSO browser profile"),
    site: Optional[str] = typer.Option(None, "--site", help="Moodle base URL (or MOODLE_SITE_URL)"),
    browser: str = typer.Option(
        "auto",
        "--browser",
        envvar="MOODLE_BROWSER",
        help="auto, webkit, firefox, chromium (Playwright builds), chrome, msedge (installed)",
    ),
    headless: Optional[bool] = typer.Option(
        None,
        "--headless/--headed",
        envvar="MOODLE_HEADLESS",
        show_default=False,
        help="Hide/show the browser window. Default: shown for `login` only; "
        "--headless login asks for SSO credentials in the terminal",
    ),
    cache_ttl: float = typer.Option(
        3600, "--cache-ttl", envvar="MOODLE_CACHE_TTL", help="Seconds to reuse scraped pages"
    ),
    no_cache: bool = typer.Option(
        False, "--no-cache", envvar="MOODLE_NO_CACHE", help="Ignore cached pages (still refreshes)"
    ),
):
    ctx.obj = Settings.load(profile, site)
    ctx.obj.browser = browser
    ctx.obj.headless = headless
    ctx.obj.cache_ttl = 0 if no_cache else cache_ttl


def _settings(ctx: typer.Context) -> Settings:
    return ctx.find_root().obj


def _cid(ctx: typer.Context, provided: int | None) -> int:
    selected = provided if provided is not None else _settings(ctx).course_id
    if selected is None:
        raise moodlectlError("Select a course with `moodlectl courses use ID` or pass --course")
    return selected


@contextmanager
def browser(ctx: typer.Context, *, headless: bool = True):
    """Browser session; `headless` is the default when neither flag nor env var is set."""
    chosen = _settings(ctx).headless
    with BrowserMoodle(_settings(ctx), headless=headless if chosen is None else chosen) as moodle:
        yield moodle


class _Dumper(yaml.SafeDumper):
    pass


# Multi-line strings (e.g. forum messages) as readable `|` blocks instead of escaped lines.
_Dumper.add_representer(
    str,
    lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None),
)


def _print(value: Any) -> None:
    typer.echo(
        yaml.dump(value, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=1000), nl=False
    )


# Keys sorted most recent first by default; everything else ascending.
_CHRONO = {"created", "modified", "academic_year"}


def _sort_option(default: str | None) -> Any:
    return typer.Option(
        default,
        "--sort",
        help="Sort by this key of the listed items (dates: most recent first)"
        + ("" if default else "; default: page order"),
    )


_REVERSE = typer.Option(False, "--reverse", help="Reverse the sort order")


def _sorted(rows: list[dict], key: str | None, reverse: bool) -> list[dict]:
    """Rows sorted by `key` (case-insensitive, missing values last); None keeps their order."""
    if key is None:
        return rows[::-1] if reverse else rows
    if rows and key not in rows[0]:
        raise moodlectlError(f"Cannot sort by {key!r}; choose one of: {', '.join(rows[0])}")
    present = [r for r in rows if r[key] is not None]
    present.sort(
        key=lambda r: r[key].casefold() if isinstance(r[key], str) else r[key],
        reverse=(key in _CHRONO) != reverse,
    )
    return present + [r for r in rows if r[key] is None]


_PRETTY = typer.Option(False, "--pretty", help="Human-readable output instead of YAML")


def _cell(value: Any) -> Text:
    if isinstance(value, bool):
        return Text("✓" if value else "")
    if isinstance(value, (list, tuple)):
        return Text(", ".join(map(str, value)))
    return Text("" if value is None else str(value))


def _table(rows: list[dict]) -> RenderableType:
    if not rows:
        return Text("Nothing found.", style="dim")
    table = Table(*rows[0])
    for row in rows:
        table.add_row(*map(_cell, row.values()))
    return table


def _post(p: dict) -> Group:
    header = Text.assemble((p["subject"], "bold"), "\n", "by ", (p["author"], "cyan"))
    header.append(f" · {p['created']}", style="dim")
    if p["modified"] != p["created"]:
        header.append(f" · edited {p['modified']}", style="dim")
    for flag in ("private", "deleted"):
        if p[flag]:
            header.append(f" [{flag}]", style="yellow")
    header.append(f" #{p['id']}", style="dim")
    files = [Text(f"📎 {name}", style="blue") for name in p["attachments"]]
    return Group(header, Text(p["message"]), *files, Text())


def _thread(posts: list[dict]) -> RenderableType:
    """Posts as reply trees, like Moodle's nested view; siblings keep the list's order."""
    if not posts:
        return _table(posts)
    ids = {p["id"] for p in posts}
    children: dict[int | None, list[dict]] = {}
    for p in posts:
        children.setdefault(p["parent"] if p["parent"] in ids else None, []).append(p)

    def tree(p: dict, node: Tree | None = None) -> Tree:
        node = Tree(_post(p), guide_style="dim") if node is None else node.add(_post(p))
        for child in children.get(p["id"], []):
            tree(child, node)
        return node

    return Group(*(tree(p) for p in children.get(None, [])))


def _list(rows: list[dict], pretty: bool, render=_table) -> None:
    if pretty:
        Console().print(render(rows))
    else:
        _print(rows)


@app.command()
def login(ctx: typer.Context):
    """Log in via SSO in a visible browser (or in the terminal with --headless)."""
    with browser(ctx, headless=False) as moodle:
        moodle.login()
    typer.echo("Authenticated browser profile saved locally.")


@config.command("show")
def config_show(ctx: typer.Context):
    s = _settings(ctx)
    _print(
        {
            "site": s.site,
            "profile": s.profile,
            "course_id": s.course_id,
            "config_path": str(s.path),
            "browser_dir": str(s.browser_dir),
            "cache_dir": str(s.cache_dir),
            "cache_ttl": s.cache_ttl,
        }
    )


@config.command("set-site")
def config_set_site(ctx: typer.Context, url: str):
    s = _settings(ctx)
    s.site = normalized_site(url)
    s.save()
    typer.echo(f"Moodle site: {s.site}")


@cache.command("clear")
def cache_clear(ctx: typer.Context):
    """Delete this profile's cached pages."""
    BrowserMoodle(_settings(ctx)).clear_cache()
    typer.echo(f"Cleared {_settings(ctx).cache_dir}")


@courses.command("list")
def courses_list(
    ctx: typer.Context,
    search: Optional[str] = typer.Option(None, "--search"),
    sort: Optional[str] = _sort_option("name"),
    reverse: bool = _REVERSE,
    pretty: bool = _PRETTY,
):
    """List links from My Courses; --search searches the site course catalogue."""
    with browser(ctx) as moodle:
        _list(_sorted([asdict(c) for c in moodle.list_courses(search)], sort, reverse), pretty)


@courses.command("use")
def courses_use(ctx: typer.Context, course: int):
    """Save default course ID (does not modify Moodle)."""
    s = _settings(ctx)
    s.course_id = course
    s.save()
    typer.echo(f"Default course: {course}")


@sections.command("list")
def sections_list(
    ctx: typer.Context,
    course: Optional[int] = typer.Option(None, "--course"),
    sort: Optional[str] = _sort_option("number"),
    reverse: bool = _REVERSE,
    pretty: bool = _PRETTY,
):
    with browser(ctx) as moodle:
        _list(
            _sorted(
                [
                    {
                        "number": x.number,
                        "name": x.name,
                        "section_id": x.section_id,
                        "resource_count": len(x.resources),
                    }
                    for x in moodle.course_sections(_cid(ctx, course))
                ],
                sort,
                reverse,
            ),
            pretty,
        )


@resources.command("list")
def resources_list(
    ctx: typer.Context,
    course: Optional[int] = typer.Option(None, "--course"),
    section: Optional[str] = typer.Option(None, "--section"),
    sort: Optional[str] = _sort_option(None),
    reverse: bool = _REVERSE,
    pretty: bool = _PRETTY,
):
    with browser(ctx) as moodle:
        rows = [asdict(r) for r in moodle.list_resources(_cid(ctx, course), section)]
        _list(_sorted(rows, sort, reverse), pretty)


@resources.command("show")
def resources_show(
    ctx: typer.Context,
    cmid: int,
    course: Optional[int] = typer.Option(None, "--course"),
):
    """Show the underlying URL and edit-form metadata for a URL activity."""
    with browser(ctx) as moodle:
        _print(moodle.get_url_details(_cid(ctx, course), cmid))


@resources.command("inspect-form")
def resources_inspect_form(
    ctx: typer.Context,
    section: str = typer.Option(..., "--section"),
    course: Optional[int] = typer.Option(None, "--course"),
    kind: str = typer.Option("url", "--kind"),
):
    """List Moodle form field names, IDs and options (no write)."""
    with browser(ctx) as moodle:
        _print(moodle.inspect_form(_cid(ctx, course), section, kind=kind))


@resources.command("add-url")
def add_url(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    url: str = typer.Argument(...),
    section: str = typer.Option(..., "--section", help="Section number or title"),
    course: Optional[int] = typer.Option(None, "--course"),
    description: str = typer.Option("", "--description"),
    text_format: str = typer.Option("markdown", "--format"),
    show_description: bool = typer.Option(False, "--show-description"),
    hidden: bool = typer.Option(False, "--hidden"),
    display: Optional[str] = typer.Option(None, "--display", help="Moodle display mode label"),
    fields: str = typer.Option(
        "{}", "--fields", help="JSON mapping extra form selectors to values"
    ),
    if_exists: str = typer.Option("skip", "--if-exists", help="skip or error"),
):
    if if_exists not in ("skip", "error"):
        raise moodlectlError("--if-exists supports skip or error")
    spec = ResourceSpec(
        kind="url",
        name=name,
        url=url,
        description=description,
        description_format=cast(Format, text_format),  # checked by ResourceSpec.validate()
        show_description=show_description,
        visible=False if hidden else None,
        display=display,
        fields=_parse_json_object(fields),
    )
    with browser(ctx) as moodle:
        result = moodle.add_resource(_cid(ctx, course), section, spec, if_exists=if_exists)
    typer.echo(f"{result}: {name}")


@resources.command("add-page")
def add_page(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    content: str = typer.Option(..., "--content", help="Page body text/HTML"),
    section: str = typer.Option(..., "--section"),
    course: Optional[int] = typer.Option(None, "--course"),
    description: str = typer.Option("", "--description"),
    hidden: bool = typer.Option(False, "--hidden"),
    fields: str = typer.Option("{}", "--fields"),
):
    spec = ResourceSpec(
        kind="page",
        name=name,
        content=content,
        description=description,
        visible=False if hidden else None,
        fields=_parse_json_object(fields),
    )
    with browser(ctx) as moodle:
        result = moodle.add_resource(_cid(ctx, course), section, spec)
    typer.echo(f"{result}: {name}")


@resources.command("update-url")
def update_url(
    ctx: typer.Context,
    cmid: int,
    course: Optional[int] = typer.Option(None, "--course"),
    name: Optional[str] = typer.Option(None, "--name"),
    url: Optional[str] = typer.Option(None, "--url"),
    description: Optional[str] = typer.Option(None, "--description"),
    text_format: Optional[str] = typer.Option(None, "--format"),
    show_description: Optional[bool] = typer.Option(None, "--show-description/--hide-description"),
    visibility: Optional[bool] = typer.Option(None, "--visible/--hidden"),
    display: Optional[str] = typer.Option(None, "--display"),
    fields: str = typer.Option("{}", "--fields"),
):
    with browser(ctx) as moodle:
        moodle.update_url(
            _cid(ctx, course),
            cmid,
            name=name,
            url=url,
            description=description,
            description_format=text_format,
            show_description=show_description,
            visible=visibility,
            display=display,
            fields=_parse_json_object(fields),
        )
    typer.echo(f"updated: {cmid}")


@resources.command("add-file")
def add_file(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    file: str = typer.Argument(..., help="Local file to upload"),
    section: str = typer.Option(..., "--section", help="Section number or title"),
    course: Optional[int] = typer.Option(None, "--course"),
    description: str = typer.Option("", "--description"),
    text_format: str = typer.Option("markdown", "--format"),
    show_description: bool = typer.Option(False, "--show-description"),
    hidden: bool = typer.Option(False, "--hidden"),
    display: Optional[str] = typer.Option(None, "--display", help="Moodle display mode label"),
    fields: str = typer.Option("{}", "--fields"),
    if_exists: str = typer.Option("skip", "--if-exists", help="skip or error"),
):
    """Create a File resource from a local file."""
    if if_exists not in ("skip", "error"):
        raise moodlectlError("--if-exists supports skip or error")
    spec = ResourceSpec(
        kind="resource",
        name=name,
        file=file,
        description=description,
        description_format=cast(Format, text_format),  # checked by ResourceSpec.validate()
        show_description=show_description,
        visible=False if hidden else None,
        display=display,
        fields=_parse_json_object(fields),
    )
    with browser(ctx) as moodle:
        result = moodle.add_resource(_cid(ctx, course), section, spec, if_exists=if_exists)
    typer.echo(f"{result}: {name}")


@resources.command("update-file")
def update_file(
    ctx: typer.Context,
    cmid: int,
    file: Optional[str] = typer.Argument(
        None, help="Local file replacing the current one; omit to change settings only"
    ),
    course: Optional[int] = typer.Option(None, "--course"),
    name: Optional[str] = typer.Option(None, "--name"),
    visibility: Optional[bool] = typer.Option(None, "--visible/--hidden"),
    fields: str = typer.Option("{}", "--fields"),
):
    """Replace the file of a File resource and/or change its settings; its cmid (and
    every link to it) stays."""
    with browser(ctx) as moodle:
        moodle.update_file(
            _cid(ctx, course),
            cmid,
            file,
            name=name,
            visible=visibility,
            fields=_parse_json_object(fields),
        )
    typer.echo(f"updated: {cmid}")


@forums.command("list")
def forums_list(
    ctx: typer.Context,
    course: Optional[int] = typer.Option(None, "--course"),
    sort: Optional[str] = _sort_option(None),
    reverse: bool = _REVERSE,
    pretty: bool = _PRETTY,
):
    """List the course's forums (their cmid selects them in `discussions` commands)."""
    with browser(ctx) as moodle:
        _list(
            _sorted([asdict(r) for r in moodle.list_forums(_cid(ctx, course))], sort, reverse),
            pretty,
        )


# Which page/query key identifies each kind in a pasted URL, and the command reading it.
_REF_URLS = {
    "forum": ("view.php", "id", "discussions list"),
    "discussion": ("discuss.php", "d", "discussions show"),
}


def forum_ref(value: str, kind: str) -> int:
    """Id of a `kind` ("forum" cmid or "discussion"), bare or in a pasted Moodle URL."""
    if value.isdigit():
        return int(value)
    url = urlsplit(value)
    query = parse_qs(url.query)
    for other, (page, key, command) in _REF_URLS.items():
        if url.path.endswith(f"/mod/forum/{page}") and query.get(key, [""])[0].isdigit():
            if other != kind:
                raise moodlectlError(f"That is a {other}; use `{command} {query[key][0]}`")
            return int(query[key][0])
    raise moodlectlError(f"Not a {kind} id or URL: {value!r}")


@discussions.command("list")
def discussions_list(
    ctx: typer.Context,
    forum: str = typer.Argument(..., help="Forum cmid or view.php URL"),
    sort: Optional[str] = _sort_option("modified"),
    reverse: bool = _REVERSE,
    pretty: bool = _PRETTY,
):
    """List a forum's discussions, with authors, dates, replies and states."""
    with browser(ctx) as moodle:
        rows = [asdict(d) for d in moodle.list_discussions(forum_ref(forum, "forum"))]
        _list(_sorted(rows, sort, reverse), pretty)


@discussions.command("show")
def discussions_show(
    ctx: typer.Context,
    discussion: str = typer.Argument(..., help="Discussion id or discuss.php URL"),
    html: bool = typer.Option(False, "--html", help="Print messages as Moodle's HTML"),
    sort: Optional[str] = _sort_option(None),
    reverse: bool = _REVERSE,
    pretty: bool = _PRETTY,
):
    """Show a discussion's posts (oldest first) with authors, metadata and attachments."""
    ref = forum_ref(discussion, "discussion")
    with browser(ctx) as moodle:
        rows = [asdict(x) for x in moodle.list_posts(ref, html=html)]
        _list(_sorted(rows, sort, reverse), pretty, _thread)


def _text(value: str | None) -> str | None:
    return sys.stdin.read() if value == "-" else value


_MESSAGE_HELP = "Message text, or - to read it from stdin"
_FORMAT_HELP = "markdown, html, plain or auto"
_ATTACH_HELP = "File to attach (repeatable)"


@discussions.command("add")
def discussions_add(
    ctx: typer.Context,
    forum: str = typer.Argument(..., help="Forum cmid or view.php URL"),
    subject: str = typer.Argument(...),
    message: str = typer.Option(..., "--message", "-m", help=_MESSAGE_HELP),
    text_format: str = typer.Option("markdown", "--format", help=_FORMAT_HELP),
    if_exists: str = typer.Option("skip", "--if-exists", help="skip or error"),
    attach: list[str] = typer.Option([], "--attach", "-a", help=_ATTACH_HELP),
):
    """Start a new discussion (skipped if one with the same subject exists)."""
    if if_exists not in ("skip", "error"):
        raise moodlectlError("--if-exists supports skip or error")
    with browser(ctx) as moodle:
        result, discussion = moodle.add_discussion(
            forum_ref(forum, "forum"),
            subject,
            _text(message) or "",
            text_format=text_format,
            if_exists=if_exists,
            attachments=attach,
        )
    _print({result: {"discussion": discussion, "subject": subject}})


@posts.command("reply")
def posts_reply(
    ctx: typer.Context,
    post: int = typer.Argument(..., help="Post id to reply to"),
    message: str = typer.Option(..., "--message", "-m", help=_MESSAGE_HELP),
    subject: Optional[str] = typer.Option(None, "--subject", help="Default: Re: <subject>"),
    private: bool = typer.Option(False, "--private", help="Visible to the author only"),
    text_format: str = typer.Option("markdown", "--format", help=_FORMAT_HELP),
    attach: list[str] = typer.Option([], "--attach", "-a", help=_ATTACH_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="Post even if you said the same"),
):
    """Reply to a post; warns first if you already posted a very similar message."""

    def confirm_similar(similar: list[Post]) -> bool:
        typer.echo("Warning: you already posted a very similar message here:", err=True)
        typer.echo(
            yaml.dump(
                [asdict(x) for x in similar],
                Dumper=_Dumper,
                allow_unicode=True,
                sort_keys=False,
                width=1000,
            ),
            err=True,
            nl=False,
        )
        return yes or typer.confirm("Post anyway?", default=False, err=True)

    with browser(ctx) as moodle:
        new = moodle.reply(
            post,
            _text(message) or "",
            subject=subject,
            private=private,
            text_format=text_format,
            attachments=attach,
            confirm_similar=confirm_similar,
        )
    if new is None:
        typer.echo("aborted")
        raise typer.Exit(1)
    _print({"created": {"post": new, "parent": post}})


@posts.command("edit")
def posts_edit(
    ctx: typer.Context,
    post: int = typer.Argument(..., help="Post id"),
    message: Optional[str] = typer.Option(None, "--message", "-m", help=_MESSAGE_HELP),
    subject: Optional[str] = typer.Option(None, "--subject"),
    text_format: str = typer.Option("markdown", "--format", help=_FORMAT_HELP),
    attach: list[str] = typer.Option([], "--attach", "-a", help="File to add (repeatable)"),
):
    """Change a post's subject and/or message, or add attachments (the rest is kept)."""
    with browser(ctx) as moodle:
        moodle.edit_post(
            post,
            subject=subject,
            message=_text(message),
            text_format=text_format,
            attachments=attach,
        )
    typer.echo(f"updated: {post}")


def _confirm_delete(yes: bool):
    def confirm(text: str) -> bool:
        if yes:
            return True
        typer.echo(text)
        return typer.confirm("Delete?", default=False)

    return confirm


_YES_HELP = "Do not ask for confirmation"


@posts.command("delete")
def posts_delete(
    ctx: typer.Context,
    post: int = typer.Argument(..., help="Post id (a first post deletes its discussion)"),
    yes: bool = typer.Option(False, "--yes", "-y", help=_YES_HELP),
):
    """Delete a post and its replies, after showing Moodle's confirmation."""
    with browser(ctx) as moodle:
        deleted = moodle.delete_post(post, _confirm_delete(yes))
    typer.echo(f"deleted: {post}" if deleted else "aborted")


@discussions.command("delete")
def discussions_delete(
    ctx: typer.Context,
    discussion: str = typer.Argument(..., help="Discussion id or discuss.php URL"),
    yes: bool = typer.Option(False, "--yes", "-y", help=_YES_HELP),
):
    """Delete a whole discussion (its first post), after showing Moodle's confirmation."""
    ref = forum_ref(discussion, "discussion")
    with browser(ctx) as moodle:
        first = next(x for x in moodle.list_posts(ref) if x.parent is None)
        deleted = moodle.delete_post(first.id, _confirm_delete(yes))
    typer.echo(f"deleted: discussion {ref}" if deleted else "aborted")


@discussions.command("set")
def discussions_set(
    ctx: typer.Context,
    discussion: str = typer.Argument(..., help="Discussion id or discuss.php URL"),
    locked: Optional[bool] = typer.Option(None, "--lock/--unlock"),
    pinned: Optional[bool] = typer.Option(None, "--pin/--unpin"),
    favourite: Optional[bool] = typer.Option(None, "--favourite/--unfavourite"),
    subscribed: Optional[bool] = typer.Option(None, "--subscribe/--unsubscribe"),
):
    """Lock, pin, star or subscribe to a discussion."""
    if (locked, pinned, favourite, subscribed) == (None,) * 4:
        raise moodlectlError("Nothing to set; see --help")
    with browser(ctx) as moodle:
        _print(
            moodle.set_discussion(
                forum_ref(discussion, "discussion"),
                locked=locked,
                pinned=pinned,
                favourite=favourite,
                subscribed=subscribed,
            )
        )


def _parse_json_object(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise moodlectlError(f"Invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise moodlectlError("Expected a JSON object")
    return value


@resources.command("sync")
def resources_sync(
    ctx: typer.Context,
    manifest: str = typer.Argument(..., help="YAML manifest to apply"),
    apply: bool = typer.Option(
        False, "--apply", help="Actually create resources (default: preview)"
    ),
    if_exists: str = typer.Option("skip", "--if-exists", help="skip or error"),
):
    if if_exists not in ("skip", "error"):
        raise moodlectlError("--if-exists supports skip or error")
    batch = load_manifest(manifest)
    with browser(ctx) as moodle:
        sect = moodle.select_section(batch.course, batch.section)
        existing = {(r.kind, r.name.casefold()) for r in sect.resources}
        plan = [
            {
                "kind": spec.kind,
                "name": spec.name,
                "action": "exists" if (spec.kind, spec.name.casefold()) in existing else "create",
            }
            for spec in batch.resources
        ]
        _print({"course": batch.course, "section": sect.name, "apply": apply, "plan": plan})
        if not apply:
            typer.echo("Preview only. Re-run with --apply to create resources.")
            return
        outcomes: list[dict[str, str]] = []
        for spec in batch.resources:
            # Re-resolve and check before every write for idempotency.
            try:
                outcome = moodle.add_resource(batch.course, sect.number, spec, if_exists=if_exists)
            except Exception:
                _print({"completed": outcomes, "failed_on": spec.name})
                raise
            outcomes.append({"resource": spec.name, "outcome": outcome})
        _print({"completed": outcomes})


@manifest.command("from-table")
def manifest_from_table(
    source_url: str = typer.Argument(..., help="Public HTML slide-table URL"),
    course: int = typer.Option(..., "--course"),
    section: str = typer.Option(..., "--section"),
    output: str = typer.Option("resources.yaml", "--output", "-o"),
):
    """Import linked PDF+online slide tables into a YAML manifest."""
    batch = import_table(source_url, course, section)
    write_manifest(batch, output)
    typer.echo(f"Wrote {len(batch.resources)} resources to {output}")


@api.command("call")
def api_call(
    ctx: typer.Context,
    function: str,
    params: str = typer.Option("{}", "--params", help="Nested JSON parameters"),
):
    """Call any provisioned Moodle REST function (requires MOODLE_WS_TOKEN)."""
    with MoodleREST(_settings(ctx).site) as ws:
        _print(ws.call(function, **_parse_json_object(params)))


@api.command("courses")
def api_courses(ctx: typer.Context):
    """List enrolled courses through native Moodle REST (requires token)."""
    with MoodleREST(_settings(ctx).site) as ws:
        _print(ws.courses())


@api.command("contents")
def api_contents(ctx: typer.Context, course: Optional[int] = typer.Option(None, "--course")):
    """Read detailed course structure through Moodle REST (requires token)."""
    with MoodleREST(_settings(ctx).site) as ws:
        _print(ws.course_contents(_cid(ctx, course)))


def main() -> None:
    try:
        app()
    except moodlectlError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
