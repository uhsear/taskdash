"""Phase 0 prod deploy: a dashboard update posts to the item owner's content path."""

from __future__ import annotations

from types import SimpleNamespace

import taskdash_common

taskdash_common.disable_arcpy_import()

import taskdash_dashboard as dash  # noqa: E402


class _Con:
    def __init__(self):
        self.urls = []

    def post(self, url, params):
        self.urls.append(url)
        return {"success": True, "id": "new"}


def _gis(con):
    return SimpleNamespace(_portal=SimpleNamespace(resturl="https://x/sharing/rest/"), _con=con,
                           users=SimpleNamespace(me=SimpleNamespace(username="collector")), url="https://x")


def test_update_posts_to_the_owner_of_the_item_not_the_signed_in_user():
    con = _Con()
    existing = SimpleNamespace(id="abc", owner="person")
    dash.post_dashboard(_gis(con), {}, "TaskDash", existing)
    assert con.urls == ["https://x/sharing/rest/content/users/person/items/abc/update"]


def test_create_still_adds_to_the_signed_in_user():
    con = _Con()
    dash.post_dashboard(_gis(con), {}, "TaskDash_TEST", None)
    assert con.urls == ["https://x/sharing/rest/content/users/collector/addItem"]
