from rpg.routes import _fix_loc


def test_location_aliases_are_owned_by_the_script():
    script = {"location_aliases": {"old_room": "new_room"}}
    assert _fix_loc("old_room", script) == "new_room"
    assert _fix_loc("old_room", {}) == "old_room"
    assert _fix_loc("other_room", script) == "other_room"
    assert _fix_loc(None, script) == ""
