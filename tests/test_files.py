from types import SimpleNamespace

import pytest

from moodlectl.browser import RESOURCE_FILES, BrowserMoodle
from moodlectl.config import Settings
from moodlectl.errors import MoodleFormError, moodlectlError
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
