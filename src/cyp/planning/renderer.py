"""Render a `ResolvedWorkout` to intervals.icu workout text (library README §7).

Layout: one section per top-level cue or repeat block, sections separated by a blank line (so
every repeat block has a blank line before and after it). A section is a title line (the cue,
plus ` Nx` for repeats) followed by `- <duration> <target>[ <cadence>]` step lines. The footer
is appended by `publish`, not here. The returned text has no trailing newline.
"""

from __future__ import annotations

from cyp.planning.templates import Repeat, ResolvedWorkout, Step, TemplateError


def format_duration(seconds: int) -> str:
    """Format a step duration: `30s`, `10m`, `145m`, `1m30s`.

    Whole minutes are always written in minutes (README §7 Example 3 renders `145m`, not
    `2h25m`); a sub-minute remainder is appended in seconds.
    """
    if seconds <= 0:
        raise TemplateError(f"step duration must be positive, got {seconds}s")
    minutes, secs = divmod(seconds, 60)
    if minutes and secs:
        return f"{minutes}m{secs}s"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def _pct(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _target(step: Step, hr: bool) -> str:
    if step.kind == "freeride":
        return "freeride"
    if step.lo is None or step.hi is None:
        raise TemplateError(f"{step.kind} step without lo/hi")
    suffix = "% LTHR" if hr else "%"
    if step.kind == "ramp":
        return f"ramp {_pct(step.lo)}-{_pct(step.hi)}{suffix}"
    if step.lo == step.hi:
        return f"{_pct(step.lo)}{suffix}"
    return f"{_pct(step.lo)}-{_pct(step.hi)}{suffix}"


def _line(step: Step, hr: bool) -> str:
    parts = [format_duration(step.duration_s), _target(step, hr)]
    if step.cadence:
        parts.append(step.cadence)
    return "- " + " ".join(parts)


def render(w: ResolvedWorkout) -> str:
    """Render the workout as intervals.icu text (power or HR targets, never both)."""
    hr = w.target == "HR"
    sections: list[list[str]] = []
    current: list[str] | None = None
    for item in w.items:
        if isinstance(item, Repeat):
            title = f"{item.cue} {item.count}x" if item.cue else f"{item.count}x"
            sections.append([title, *(_line(s, hr) for s in item.steps)])
            current = None
            continue
        if item.cue is not None or current is None:
            current = [item.cue] if item.cue is not None else []
            sections.append(current)
        current.append(_line(item, hr))
    return "\n\n".join("\n".join(lines) for lines in sections)
