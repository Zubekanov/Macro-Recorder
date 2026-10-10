"""Key vocabulary: display text and parsing."""

from macro_recorder.model.keys import display_key, format_combo, parse_combo


def test_space_displays_as_its_name_and_parses_back():
    assert display_key("space") == "Space"
    assert format_combo(("space",)) == "Space"
    assert format_combo(("Control_L", "space")) == "Ctrl+Space"
    assert parse_combo("Ctrl+Space") == ("Control_L", "space")
    assert parse_combo(format_combo(("space",))) == ("space",)


def test_punctuation_still_displays_as_the_character():
    assert display_key("comma") == ","
    assert format_combo(("Control_L", "plus")) == "Ctrl++"
