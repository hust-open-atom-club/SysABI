from __future__ import annotations


def guest_crash_detail(console_text: str) -> str | None:
    if "Printing stack trace:" in console_text:
        return "guest crashed before emitting autorun markers (kernel stack trace observed)"
    lowered = console_text.lower()
    if "panicked at" in lowered or "kernel panic" in lowered:
        return "guest crashed before emitting autorun markers (kernel panic observed)"
    return None
