from types import SimpleNamespace

import pytest

from moodlectl.browser import RESOURCE_FILES, BrowserMoodle, served_filename
from moodlectl.config import Settings
from moodlectl.errors import LoginRequired, MoodleFormError, moodlectlError
from moodlectl.manifest import load_manifest
from moodlectl.models import ResourceSpec

REPOS = '{"4":{"id":"4","name":"Upload a file","type":"upload"}}'


class FakeDraftPage:
    """A modedit form whose file area is served by an in-memory draftfiles_ajax."""

    def __init__(self, draft, *, refuse_delete=False, serve=b""):
        self.draft = list(draft)
        self.refuse_delete = refuse_delete
        self.serve = serve
        self.calls = []
        self.request = SimpleNamespace(post=self._post, get=self._get)

    def locator(self, selector):
        found = 'input[name="files"]' in selector and selector.startswith("form#mform1")
        loc = SimpleNamespace(count=lambda: int(found), input_value=lambda: "777")
        return SimpleNamespace(first=loc)

    def evaluate(self, script):
        return "KEY" if "sesskey" in script else 99

    def content(self):
        return REPOS

    @staticmethod
    def _json(value, status=200):
        return SimpleNamespace(json=lambda: value, status=status, ok=status < 400)

    def _post(self, url, *, form=None, multipart=None):
        action = url.rsplit("action=", 1)[1]
        fields = form or {k: v for k, v in multipart.items() if k != "repo_upload_file"}
        self.calls.append((action, fields))
        if action == "list":
            return self._json({"list": [dict(e) for e in self.draft]})
        if action == "delete":
            if self.refuse_delete:
                return self._json(False)
            self.draft = [e for e in self.draft if e["filename"] != form["filename"]]
            return self._json("/")
        self.draft.append({"type": "file", "filepath": "/", "filename": multipart["title"]})
        return self._json({"url": "draftfile.php"})

    def _get(self, url):
        self.calls.append(("get", url))
        return SimpleNamespace(body=lambda: self.serve, ok=True, status=200)


@pytest.fixture
def moodle(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return BrowserMoodle(Settings(profile="test"))


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "M5.pdf"
    path.write_bytes(b"%PDF-new")
    return path


def test_replace_files_empties_the_area_then_uploads(moodle, pdf):
    page = FakeDraftPage([{"type": "file", "filepath": "/", "filename": "old.pdf"}])
    moodle._replace_files(page, RESOURCE_FILES, [pdf])
    assert [c[0] for c in page.calls] == ["list", "delete", "list", "upload"]
    deleted = {"sesskey": "KEY", "itemid": "777", "filepath": "/", "filename": "old.pdf"}
    assert page.calls[1][1] == deleted
    upload = page.calls[3][1]
    assert (upload["itemid"], upload["repo_id"], upload["title"]) == ("777", "4", "M5.pdf")
    assert page.draft == [{"type": "file", "filepath": "/", "filename": "M5.pdf"}]


def test_replace_files_stops_before_uploading_when_moodle_keeps_the_old_file(moodle, pdf):
    old = {"type": "file", "filepath": "/", "filename": "old.pdf"}
    page = FakeDraftPage([old], refuse_delete=True)
    with pytest.raises(MoodleFormError, match="old.pdf"):
        moodle._replace_files(page, RESOURCE_FILES, [pdf])
    assert "upload" not in [c[0] for c in page.calls]


def test_replace_files_leaves_folders_alone(moodle, pdf):
    page = FakeDraftPage([{"type": "folder", "filepath": "/sub/", "filename": "sub"}])
    with pytest.raises(MoodleFormError, match="folder"):
        moodle._replace_files(page, RESOURCE_FILES, [pdf])
    assert [c[0] for c in page.calls] == ["list"]


def test_check_file_compares_what_moodle_serves(moodle, pdf):
    moodle._check_file(FakeDraftPage([], serve=b"%PDF-new"), 42, pdf)
    with pytest.raises(MoodleFormError, match="cmid=42"):
        moodle._check_file(FakeDraftPage([], serve=b"<html>login</html>"), 42, pdf)


def test_resource_spec_needs_an_existing_file_and_only_resources_take_one(pdf):
    ResourceSpec(kind="resource", name="M5", file=pdf).validate()
    with pytest.raises(moodlectlError, match="existing file"):
        ResourceSpec(kind="resource", name="M5", file=pdf.with_name("missing.pdf")).validate()
    with pytest.raises(moodlectlError, match="existing file"):
        ResourceSpec(kind="resource", name="M5").validate()
    with pytest.raises(moodlectlError, match="takes a file"):
        ResourceSpec(kind="url", name="M5", url="https://example.org", file=pdf).validate()


def test_manifest_file_is_relative_to_the_manifest(tmp_path, pdf):
    manifest = tmp_path / "batch.yaml"
    manifest.write_text(
        "course: 1\nsection: 2\nresources:\n  - kind: resource\n    name: M5\n    file: M5.pdf\n"
    )
    assert load_manifest(manifest).resources[0].file == pdf


PLUGINFILE = (
    "https://virtuale.unibo.it/pluginfile.php/123/mod_resource/content/4/M5%20-%20Agents.pdf"
)


class FakeServedPage:
    """A page whose request.get answers like mod/resource/view.php?redirect=1."""

    def __init__(self, url=PLUGINFILE, body=b"%PDF-served", status=200, headers=None):
        self.got = []

        def get(u):
            self.got.append(u)
            return SimpleNamespace(
                url=url,
                body=lambda: body,
                status=status,
                ok=status < 400,
                headers=headers or {},
            )

        self.request = SimpleNamespace(get=get)


@pytest.fixture
def served(moodle, monkeypatch):
    def serve(**kwargs):
        page = FakeServedPage(**kwargs)
        monkeypatch.setattr(moodle, "_start", lambda: page)
        return page

    moodle.site = "https://virtuale.unibo.it"
    return serve


def test_served_filename_prefers_content_disposition():
    assert served_filename({}, "/pluginfile.php/1/x/M5%20-%20Agents.pdf") == "M5 - Agents.pdf"
    assert served_filename({"Content-Disposition": 'inline; filename="a b.pdf"'}, "/x") == "a b.pdf"
    assert served_filename({"content-disposition": "attachment; filename=plain.pdf"}, "/x") == (
        "plain.pdf"
    )
    utf8 = "attachment; filename=\"x.pdf\"; filename*=UTF-8''Lezione%20%C3%A8.pdf"
    assert served_filename({"Content-Disposition": utf8}, "/x") == "Lezione è.pdf"
    # Moodle's raw UTF-8 name, as Playwright hands it over (decoded as Latin-1)
    raw = 'inline; filename="A0 – About the Course.pdf"'.encode().decode("latin-1")
    assert served_filename({"content-disposition": raw}, "/x") == "A0 – About the Course.pdf"
    assert served_filename({"Content-Disposition": 'filename="caf\xe9.pdf"'}, "/x") == "café.pdf"


def test_served_filename_never_returns_a_path():
    bad = {"Content-Disposition": 'attachment; filename="../../etc/passwd"'}
    assert served_filename(bad, "/x") == "passwd"
    assert served_filename({"Content-Disposition": 'filename="..\\evil.pdf"'}, "/x") == "evil.pdf"
    with pytest.raises(moodlectlError):
        served_filename({}, "/pluginfile.php/1/..")


def test_download_keeps_the_served_name_in_a_directory(moodle, served, tmp_path):
    page = served()
    path = moodle.download_file(42, tmp_path)
    assert path == tmp_path / "M5 - Agents.pdf"
    assert path.read_bytes() == b"%PDF-served"
    assert page.got == ["https://virtuale.unibo.it/mod/resource/view.php?id=42&redirect=1"]
    assert not list(tmp_path.glob("*.part"))


def test_download_to_a_file_path_and_overwrite(moodle, served, tmp_path):
    served()
    target = tmp_path / "mine.pdf"
    target.write_bytes(b"old")
    with pytest.raises(moodlectlError, match="already exists"):
        moodle.download_file(42, target)
    assert target.read_bytes() == b"old"
    assert moodle.download_file(42, target, overwrite=True) == target
    assert target.read_bytes() == b"%PDF-served"


def test_download_defaults_to_the_current_directory(moodle, served, tmp_path, monkeypatch):
    served()
    monkeypatch.chdir(tmp_path)
    assert moodle.download_file(42) == tmp_path / "M5 - Agents.pdf"


@pytest.mark.parametrize(
    "url",
    [
        "https://virtuale.unibo.it/login/index.php",
        "https://idp.unibo.it/adfs/ls/?SAMLRequest=x",
    ],
)
def test_download_reports_an_expired_session(moodle, served, tmp_path, url):
    served(url=url, body=b"<html>login</html>")
    with pytest.raises(LoginRequired):
        moodle.download_file(42, tmp_path)
    assert not any(tmp_path.iterdir())


def test_download_refuses_what_is_not_a_file(moodle, served, tmp_path):
    served(url="https://virtuale.unibo.it/mod/url/view.php?id=42", body=b"<html/>")
    with pytest.raises(moodlectlError, match="did not lead to a file"):
        moodle.download_file(42, tmp_path)
    served(status=404)
    with pytest.raises(moodlectlError, match="HTTP 404"):
        moodle.download_file(42, tmp_path)
    assert not any(tmp_path.iterdir())
