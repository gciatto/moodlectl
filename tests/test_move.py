import pytest

from moodlectl.browser import BrowserMoodle
from moodlectl.config import Settings
from moodlectl.errors import MoodleFormError, moodlectlError
from moodlectl.models import Resource


class FakeCourse:
    """A course whose module order changes only through core_courseformat_update_course."""

    def __init__(self, cmids, *, obey=True):
        self.cmids = list(cmids)
        self.obey = obey
        self.calls = []

    def list_resources(self, _course_id):
        return [Resource(c, f"m{c}", "resource", "") for c in self.cmids]

    def ajax(self, _page, name, **args):
        self.calls.append((name, args))
        if self.obey:
            (cmid,) = args["ids"]
            self.cmids.remove(cmid)
            self.cmids.insert(self.cmids.index(args["targetcmid"]), cmid)


@pytest.fixture
def moodle(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return BrowserMoodle(Settings(profile="test"))


def _wire(moodle, monkeypatch, course):
    monkeypatch.setattr(moodle, "list_resources", course.list_resources)
    monkeypatch.setattr(moodle, "_goto", lambda _path: None)
    monkeypatch.setattr(BrowserMoodle, "_ajax", staticmethod(course.ajax))


def test_move_puts_the_module_above_the_target(moodle, monkeypatch):
    course = FakeCourse([1, 2, 3, 4, 5])
    _wire(moodle, monkeypatch, course)
    moodle.move_resource(80509, 5, before=2)
    moodle.move_resource(80509, 4, before=2)
    assert course.cmids == [1, 5, 4, 2, 3]
    assert course.calls[0] == (
        "core_courseformat_update_course",
        {"action": "cm_move", "courseid": 80509, "ids": [5], "targetcmid": 2},
    )


def test_move_checks_the_order_moodle_reports(moodle, monkeypatch):
    _wire(moodle, monkeypatch, FakeCourse([1, 2, 3], obey=False))
    with pytest.raises(MoodleFormError, match="not above"):
        moodle.move_resource(80509, 3, before=1)


@pytest.mark.parametrize("cmid, before", [(9, 1), (1, 9), (2, 2)])
def test_move_refuses_unknown_or_identical_modules(moodle, monkeypatch, cmid, before):
    course = FakeCourse([1, 2, 3])
    _wire(moodle, monkeypatch, course)
    with pytest.raises(moodlectlError):
        moodle.move_resource(80509, cmid, before=before)
    assert course.calls == []
