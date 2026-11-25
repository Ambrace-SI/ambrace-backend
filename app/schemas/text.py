"""Validation shared by every field a person types that is shown to someone else."""

import unicodedata

# Characters that reorder surrounding text when displayed. They are format
# characters (like the joiners Indian scripts need, which stay allowed), but
# their only use in a name or address is to disguise it.
_BIDI_CONTROLS = frozenset("‪‫‬‭‮⁦⁧⁨⁩")


def reject_control_characters(value: str, *, allow_newline: bool = False) -> str:
    """Typed text is shown back in pages, reports and exports; keep it printable."""
    for character in value:
        if allow_newline and character == "\n":
            continue
        if unicodedata.category(character) == "Cc" or character in _BIDI_CONTROLS:
            raise ValueError("must not contain control characters")
    return value
