"""UI-side access to the shared display helpers (implementation lives in coi.display)."""
from coi.display import (CARD_FREE_TEXT_LIMIT, CARD_TEXT_LIMIT, MAX_KEY_CARDS, SECTION_TITLES, build_declaration,
                         card_text, concise_summary, key_items, major_number, next_action, notification_label,
                         other_relevant_items, overview_line, question_title, split_cards)

__all__ = ["CARD_FREE_TEXT_LIMIT", "CARD_TEXT_LIMIT", "MAX_KEY_CARDS", "SECTION_TITLES", "build_declaration",
           "card_text", "concise_summary", "key_items", "major_number", "next_action", "notification_label",
           "other_relevant_items", "overview_line", "question_title", "split_cards"]
