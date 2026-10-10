"""Workout template loader, validator and resolver for `planning/library/*.yaml`.

Implements the spec in `planning/library/README.md`:

- §2 parameters (ranged / fixed scalars, loud validation, no silent clamp),
- §3 steps and the tiny integer expression grammar (`evaluate`),
- §4 outdoor transforms (`widen_pct`, `rests_as_freeride`, `ramps_as_range`),
- §5 HR fallback (`target="HR"`),
- §6 closed-form TSS (`closed_form_tss`),
- §8 load-time validation (`load_template`).

Rendering to intervals.icu text lives in `cyp.planning.renderer`.
"""

from __future__ import annotations

import itertools
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

import yaml

TargetMode = Literal["POWER", "HR"]

LIBRARY_DIR = Path(__file__).resolve().parent / "library"

INTENTS = frozenset(
    {
        "recovery",
        "endurance",
        "tempo",
        "sweetspot",
        "threshold",
        "vo2",
        "anaerobic",
        "test",
        "opener",
    }
)
PHASES = frozenset({"base", "build", "threshold", "test"})
SLOT_ROLES = frozenset({"long_ride", "hit", "endurance", "recovery", "test", "opener"})
KINDS = frozenset({"steady", "ramp", "work", "rest", "freeride"})
LEAF_KEYS = frozenset({"cue", "kind", "duration", "lo", "hi", "cadence", "tss_assume"})
REPEAT_KEYS = frozenset({"repeat", "cue", "steps"})

#: % FTP the closed-form model assumes for a rest leaf rendered as freeride outdoors (README §4).
OUTDOOR_REST_TSS_ASSUME = 50.0
#: Lower bound for widened outdoor targets (README §4).
OUTDOOR_WIDEN_FLOOR = 30.0
#: Upper bound on the number of combinations `param_grid` will enumerate.
PARAM_GRID_CAP = 5000

_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_TOKEN = re.compile(r"\s*(?:(\d+)|([-+*/]))")
_CADENCE = re.compile(r"^\d+(?:-\d+)?rpm$")


class TemplateError(ValueError):
    """A workout template (or a request to resolve one) violates the library spec."""


@dataclass(frozen=True)
class ParamSpec:
    """Declared range of one template parameter (README §2).

    A scalar param in YAML (`rest_min: 4`) becomes `ParamSpec(4, 4, 4, 1, fixed=True)`.
    """

    min: int
    max: int
    default: int
    step: int = 1
    fixed: bool = False

    def values(self) -> list[int]:
        """Return every valid value, ascending."""
        return list(range(self.min, self.max + 1, self.step))

    def is_valid(self, value: int) -> bool:
        """Return True when `value` is inside `[min, max]` and on the step grid from `min`."""
        return self.min <= value <= self.max and (value - self.min) % self.step == 0


@dataclass(frozen=True)
class Template:
    """One parsed and validated workout template (README §1)."""

    id: str
    version: int
    name: str
    name_zh: str
    purpose_zh: str
    intent: str
    phases: tuple[str, ...]
    slot_roles: tuple[str, ...]
    requires_power: bool
    indoor_ok: bool
    outdoor_ok: bool
    params: dict[str, ParamSpec]
    progression: tuple[str, ...]
    steps: list[dict[str, Any]]
    outdoor_rendering: dict[str, Any] | None
    hr_fallback: dict[str, Any] | None
    explain_zh: str
    test: dict[str, Any] | None = None
    hr_fallback_reason_zh: str | None = None
    tss_model: str = "closed_form"

    def defaults(self) -> dict[str, int]:
        """Return the default value of every param (fixed params included)."""
        return {name: spec.default for name, spec in self.params.items()}


@dataclass(frozen=True)
class Step:
    """A resolved leaf step. `lo`/`hi` are % FTP (or % LTHR in HR mode); None for freeride."""

    kind: str
    duration_s: int
    lo: float | None
    hi: float | None
    cadence: str | None = None
    cue: str | None = None
    tss_assume: float | None = None


@dataclass(frozen=True)
class Repeat:
    """A resolved repeat block containing leaves only."""

    count: int
    cue: str | None
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class ResolvedWorkout:
    """A template with concrete params, target mode and venue, ready to render."""

    template_id: str
    template_version: int
    params: dict[str, int]
    target: TargetMode
    outdoor: bool
    items: tuple[Step | Repeat, ...]
    name: str
    name_zh: str
    duration_s: int
    tss: float
    if_: float
    note_zh: str | None
    note: str | None = field(default=None)


# --------------------------------------------------------------------------------------------
# Expressions


def _substitute(text: str, params: Mapping[str, int]) -> str:
    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in params:
            raise TemplateError(f"unknown parameter {{{name}}} in {text!r}")
        return str(params[name])

    return _PLACEHOLDER.sub(repl, text)


def _placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER.findall(text))


def _round_half_up(x: Fraction) -> int:
    return math.floor(x + Fraction(1, 2))


def evaluate(expr: str | int, params: dict[str, int]) -> int:
    """Evaluate a template integer expression (README §3).

    Every `{name}` is replaced by its param value, then `+ - * /` are evaluated with the usual
    precedence (`*` `/` before `+` `-`, left to right, no parentheses). Division is exact; the
    result is rounded to the nearest integer (halves away from minus infinity, i.e. up).

    Args:
        expr: An int, or a string such as `"{total_min}-{tempo_min}-35"`.
        params: Param values by name.

    Returns:
        The rounded integer result.

    Raises:
        TemplateError: On an unknown `{name}`, a malformed expression or division by zero.
    """
    if isinstance(expr, bool):
        raise TemplateError(f"boolean is not a valid expression: {expr!r}")
    if isinstance(expr, int):
        return expr
    if not isinstance(expr, str):
        raise TemplateError(f"expression must be int or str, got {type(expr).__name__}")
    text = _substitute(expr, params)
    tokens: list[str] = []
    pos = 0
    stripped = text.rstrip()
    while pos < len(stripped):
        m = _TOKEN.match(stripped, pos)
        if m is None or m.end() == pos:
            raise TemplateError(f"malformed expression {expr!r}")
        tokens.append(m.group(1) or m.group(2))
        pos = m.end()
    if not tokens:
        raise TemplateError(f"empty expression {expr!r}")

    # Parse: [sign] term ((+|-) term)*, term = number ((*|/) number)*
    i = 0

    def number() -> Fraction:
        nonlocal i
        sign = 1
        while i < len(tokens) and tokens[i] in "+-":
            sign = -sign if tokens[i] == "-" else sign
            i += 1
        if i >= len(tokens) or not tokens[i].isdigit():
            raise TemplateError(f"malformed expression {expr!r}")
        value = Fraction(int(tokens[i]))
        i += 1
        return sign * value

    def term() -> Fraction:
        nonlocal i
        value = number()
        while i < len(tokens) and tokens[i] in "*/":
            op = tokens[i]
            i += 1
            rhs = number()
            if op == "*":
                value *= rhs
            else:
                if rhs == 0:
                    raise TemplateError(f"division by zero in {expr!r}")
                value /= rhs
        return value

    total = term()
    while i < len(tokens):
        op = tokens[i]
        if op not in "+-":
            raise TemplateError(f"malformed expression {expr!r}")
        i += 1
        rhs = term()
        total = total + rhs if op == "+" else total - rhs
    return _round_half_up(total)


def _duration_s(raw: Any, params: Mapping[str, int]) -> int:
    if isinstance(raw, bool) or not isinstance(raw, str | int):
        raise TemplateError(f"duration must be a string like '10m', got {raw!r}")
    text = str(raw).strip()
    if text.endswith("m"):
        mult = 60
    elif text.endswith("s"):
        mult = 1
    else:
        raise TemplateError(f"duration {raw!r} needs a unit 'm' or 's'")
    return evaluate(text[:-1], dict(params)) * mult


# --------------------------------------------------------------------------------------------
# Loading / validation


def _req(d: Mapping[str, Any], key: str, typ: type | tuple[type, ...], where: str) -> Any:
    if key not in d:
        raise TemplateError(f"{where}: missing required key {key!r}")
    value = d[key]
    if isinstance(value, bool) and typ is int:
        raise TemplateError(f"{where}: {key!r} must be int, got bool")
    if not isinstance(value, typ):
        raise TemplateError(f"{where}: {key!r} has wrong type {type(value).__name__}")
    return value


def _str_list(d: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = _req(d, key, list, where)
    if not all(isinstance(x, str) for x in value):
        raise TemplateError(f"{where}: {key!r} must be a list of strings")
    return tuple(value)


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TemplateError(f"{what} must be an integer, got {value!r}")
    return value


def _parse_params(raw: Any, where: str) -> dict[str, ParamSpec]:
    if not isinstance(raw, dict):
        raise TemplateError(f"{where}: params must be a mapping")
    out: dict[str, ParamSpec] = {}
    for name, spec in raw.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
            raise TemplateError(f"{where}: bad param name {name!r}")
        if isinstance(spec, dict):
            unknown = set(spec) - {"min", "max", "default", "step"}
            if unknown:
                raise TemplateError(f"{where}: param {name}: unknown keys {sorted(unknown)}")
            p = ParamSpec(
                min=_int(spec.get("min"), f"{where}: param {name}.min"),
                max=_int(spec.get("max"), f"{where}: param {name}.max"),
                default=_int(spec.get("default"), f"{where}: param {name}.default"),
                step=_int(spec.get("step", 1), f"{where}: param {name}.step"),
            )
            if p.step < 1 or p.min > p.max:
                raise TemplateError(f"{where}: param {name}: bad range {p}")
            if not p.is_valid(p.default):
                raise TemplateError(f"{where}: param {name}: default {p.default} not in range")
        else:
            v = _int(spec, f"{where}: param {name}")
            p = ParamSpec(min=v, max=v, default=v, step=1, fixed=True)
        out[name] = p
    return out


def _check_leaf(leaf: Any, where: str, names: set[str]) -> None:
    if not isinstance(leaf, dict):
        raise TemplateError(f"{where}: step must be a mapping, got {leaf!r}")
    if "repeat" in leaf:
        raise TemplateError(f"{where}: nested repeat blocks are not allowed")
    unknown = set(leaf) - LEAF_KEYS
    if unknown:
        raise TemplateError(f"{where}: unknown leaf keys {sorted(unknown)}")
    kind = leaf.get("kind")
    if kind not in KINDS:
        raise TemplateError(f"{where}: bad kind {kind!r}")
    if "duration" not in leaf:
        raise TemplateError(f"{where}: missing duration")
    exprs = [leaf["duration"]]
    if kind == "freeride":
        if "lo" in leaf or "hi" in leaf:
            raise TemplateError(f"{where}: freeride leaf must not have lo/hi")
        ta = leaf.get("tss_assume")
        if isinstance(ta, bool) or not isinstance(ta, int | float):
            raise TemplateError(f"{where}: freeride leaf needs numeric tss_assume")
    else:
        if "lo" not in leaf or "hi" not in leaf:
            raise TemplateError(f"{where}: {kind} leaf needs lo and hi")
        if "tss_assume" in leaf:
            raise TemplateError(f"{where}: tss_assume is only allowed on freeride leaves")
        exprs += [leaf["lo"], leaf["hi"]]
    for e in exprs:
        if isinstance(e, str):
            missing = _placeholders(e) - names
            if missing:
                raise TemplateError(f"{where}: unknown params {sorted(missing)} in {e!r}")
    cad = leaf.get("cadence")
    if cad is not None and (not isinstance(cad, str) or not _CADENCE.match(cad)):
        raise TemplateError(f"{where}: bad cadence {cad!r}")
    cue = leaf.get("cue")
    if cue is not None and not isinstance(cue, str):
        raise TemplateError(f"{where}: cue must be a string")


def _check_steps(steps: Any, where: str, names: set[str]) -> None:
    if not isinstance(steps, list) or not steps:
        raise TemplateError(f"{where}: steps must be a non-empty list")
    for idx, item in enumerate(steps):
        w = f"{where}[{idx}]"
        if isinstance(item, dict) and "repeat" in item:
            unknown = set(item) - REPEAT_KEYS
            if unknown:
                raise TemplateError(f"{w}: unknown repeat keys {sorted(unknown)}")
            rep = item["repeat"]
            if isinstance(rep, str):
                missing = _placeholders(rep) - names
                if missing:
                    raise TemplateError(f"{w}: unknown params {sorted(missing)} in {rep!r}")
            elif isinstance(rep, bool) or not isinstance(rep, int):
                raise TemplateError(f"{w}: repeat must be int or expression")
            cue = item.get("cue")
            if cue is not None and not isinstance(cue, str):
                raise TemplateError(f"{w}: cue must be a string")
            inner = item.get("steps")
            if not isinstance(inner, list) or not inner:
                raise TemplateError(f"{w}: repeat block needs a non-empty steps list")
            for j, leaf in enumerate(inner):
                _check_leaf(leaf, f"{w}.steps[{j}]", names)
        else:
            _check_leaf(item, w, names)


def _check_text(text: str, where: str, names: set[str]) -> None:
    missing = _placeholders(text) - names
    if missing:
        raise TemplateError(f"{where}: unknown params {sorted(missing)}")


def _parse(data: Any, where: str) -> Template:
    if not isinstance(data, dict):
        raise TemplateError(f"{where}: top level must be a mapping")
    tid = _req(data, "id", str, where)
    version = _req(data, "version", int, where)
    name = _req(data, "name", str, where)
    name_zh = _req(data, "name_zh", str, where)
    purpose_zh = _req(data, "purpose_zh", str, where)
    intent = _req(data, "intent", str, where)
    if intent not in INTENTS:
        raise TemplateError(f"{where}: intent {intent!r} not in {sorted(INTENTS)}")
    phases = _str_list(data, "phases", where)
    if not phases or not set(phases) <= PHASES:
        raise TemplateError(f"{where}: phases {phases} must be a non-empty subset of {PHASES}")
    slot_roles = _str_list(data, "slot_roles", where)
    if not slot_roles or not set(slot_roles) <= SLOT_ROLES:
        raise TemplateError(f"{where}: slot_roles {slot_roles} not a subset of {SLOT_ROLES}")
    requires_power = _req(data, "requires_power", bool, where)
    indoor_ok = _req(data, "indoor_ok", bool, where)
    outdoor_ok = _req(data, "outdoor_ok", bool, where)
    if not (indoor_ok or outdoor_ok):
        raise TemplateError(f"{where}: at least one of indoor_ok/outdoor_ok must be true")
    params = _parse_params(_req(data, "params", dict, where), where)
    names = set(params)
    progression = _str_list(data, "progression", where) if "progression" in data else ()
    if not set(progression) <= names:
        raise TemplateError(f"{where}: progression names unknown params")
    if any(params[p].fixed for p in progression):
        raise TemplateError(f"{where}: progression lists a fixed param")
    if _req(data, "tss_model", str, where) != "closed_form":
        raise TemplateError(f"{where}: tss_model must be closed_form")
    explain_zh = _req(data, "explain_zh", str, where)
    _check_text(name, f"{where}: name", names)
    _check_text(name_zh, f"{where}: name_zh", names)

    steps = _req(data, "steps", list, where)
    _check_steps(steps, f"{where}: steps", names)

    outdoor = data.get("outdoor_rendering")
    if outdoor is not None:
        if not isinstance(outdoor, dict):
            raise TemplateError(f"{where}: outdoor_rendering must be a mapping")
        widen = outdoor.get("widen_pct", 0)
        if isinstance(widen, bool) or not isinstance(widen, int) or widen < 0:
            raise TemplateError(f"{where}: outdoor_rendering.widen_pct must be an int >= 0")
        for flag in ("rests_as_freeride", "ramps_as_range"):
            if not isinstance(outdoor.get(flag, False), bool):
                raise TemplateError(f"{where}: outdoor_rendering.{flag} must be bool")
        for key in ("note", "note_zh"):
            if key in outdoor:
                if not isinstance(outdoor[key], str):
                    raise TemplateError(f"{where}: outdoor_rendering.{key} must be a string")
                _check_text(outdoor[key], f"{where}: outdoor_rendering.{key}", names)
    elif outdoor_ok:
        raise TemplateError(f"{where}: outdoor_rendering is required when outdoor_ok")

    if "hr_fallback" not in data:
        raise TemplateError(f"{where}: missing required key 'hr_fallback' (may be null)")
    hr = data["hr_fallback"]
    hr_reason = data.get("hr_fallback_reason_zh")
    if hr is None:
        if not isinstance(hr_reason, str) or not hr_reason:
            raise TemplateError(f"{where}: hr_fallback null requires hr_fallback_reason_zh")
    else:
        if not isinstance(hr, dict):
            raise TemplateError(f"{where}: hr_fallback must be a mapping or null")
        if "note_zh" in hr:
            if not isinstance(hr["note_zh"], str):
                raise TemplateError(f"{where}: hr_fallback.note_zh must be a string")
            _check_text(hr["note_zh"], f"{where}: hr_fallback.note_zh", names)
        _check_steps(hr.get("steps"), f"{where}: hr_fallback.steps", names)

    test = data.get("test")
    if test is not None and not isinstance(test, dict):
        raise TemplateError(f"{where}: test must be a mapping")

    t = Template(
        id=tid,
        version=version,
        name=name,
        name_zh=name_zh,
        purpose_zh=purpose_zh,
        intent=intent,
        phases=phases,
        slot_roles=slot_roles,
        requires_power=requires_power,
        indoor_ok=indoor_ok,
        outdoor_ok=outdoor_ok,
        params=params,
        progression=progression,
        steps=steps,
        outdoor_rendering=outdoor,
        hr_fallback=hr,
        explain_zh=explain_zh,
        test=test,
        hr_fallback_reason_zh=hr_reason if isinstance(hr_reason, str) else None,
        tss_model="closed_form",
    )
    _check_resolves(t, where)
    return t


def _check_resolves(t: Template, where: str) -> None:
    """Resolve with defaults in every allowed mode (README §8)."""
    modes: list[tuple[bool, TargetMode]] = []
    if t.indoor_ok:
        modes.append((False, "POWER"))
    if t.outdoor_ok:
        modes.append((True, "POWER"))
    base_duration = None
    for outdoor, target in modes:
        w = resolve(t, outdoor=outdoor, target=target)
        if w.duration_s <= 0:
            raise TemplateError(f"{where}: non-positive duration")
        leaves = [s for it in w.items for s in (it.steps if isinstance(it, Repeat) else (it,))]
        if not any(s.kind in ("work", "steady") for s in leaves):
            # Outdoor transforms never turn work/steady into anything else, so check indoor/raw.
            raise TemplateError(f"{where}: needs at least one work/steady step")
        if any(s.duration_s <= 0 for s in leaves) or any(
            isinstance(it, Repeat) and it.count < 1 for it in w.items
        ):
            raise TemplateError(f"{where}: steps and repeat counts must be positive")
        base_duration = w.duration_s
    if t.hr_fallback is not None:
        hr = resolve(t, outdoor=not t.indoor_ok, target="HR")
        if hr.duration_s != base_duration:
            raise TemplateError(
                f"{where}: hr_fallback duration {hr.duration_s}s != power {base_duration}s"
            )


def load_template(path: str | Path) -> Template:
    """Load and validate one template YAML file (README §8).

    Raises:
        TemplateError: If the file cannot be parsed or violates the spec.
    """
    p = Path(path)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TemplateError(f"{p.name}: cannot load: {exc}") from exc
    return _parse(data, p.name)


_LIBRARY_CACHE: dict[tuple[str, tuple[tuple[str, int], ...]], dict[str, Template]] = {}


def load_library(directory: str | Path | None = None) -> dict[str, Template]:
    """Load every `*.yaml` template in `directory` (default: the packaged library), keyed by id.

    Parsed libraries are cached per directory and invalidated when any file's name or mtime
    changes, so repeated planning runs never re-parse YAML. A fresh dict is returned each call
    (the templates themselves are immutable).

    Raises:
        TemplateError: If any template is invalid, or its `id` differs from the file stem.
    """
    d = Path(directory) if directory is not None else LIBRARY_DIR
    files = sorted(d.glob("*.yaml"))
    key = (str(d.resolve()), tuple((p.name, p.stat().st_mtime_ns) for p in files))
    cached = _LIBRARY_CACHE.get(key)
    if cached is not None:
        return dict(cached)
    out = _load_library_uncached(files)
    _LIBRARY_CACHE.clear()  # one library at a time is plenty
    _LIBRARY_CACHE[key] = out
    return dict(out)


def _load_library_uncached(files: list[Path]) -> dict[str, Template]:
    out: dict[str, Template] = {}
    for path in files:
        t = load_template(path)
        if t.id != path.stem:
            raise TemplateError(f"{path.name}: id {t.id!r} must equal file stem {path.stem!r}")
        out[t.id] = t
    return out


# --------------------------------------------------------------------------------------------
# Resolution


def _check_params(t: Template, params: Mapping[str, int] | None) -> dict[str, int]:
    values = t.defaults()
    for name, value in (params or {}).items():
        if name not in t.params:
            raise TemplateError(f"{t.id}: unknown parameter {name!r}")
        if isinstance(value, bool) or not isinstance(value, int):
            raise TemplateError(f"{t.id}: parameter {name} must be int, got {value!r}")
        spec = t.params[name]
        if not spec.min <= value <= spec.max:
            raise TemplateError(
                f"{t.id}: parameter {name}={value} outside [{spec.min}, {spec.max}]"
            )
        if (value - spec.min) % spec.step:
            raise TemplateError(
                f"{t.id}: parameter {name}={value} not on step {spec.step} from {spec.min}"
            )
        values[name] = value
    return values


def _resolve_leaf(raw: Mapping[str, Any], params: Mapping[str, int]) -> Step:
    kind = str(raw["kind"])
    p = dict(params)
    duration = _duration_s(raw["duration"], p)
    if kind == "freeride":
        lo = hi = None
        tss_assume: float | None = float(raw["tss_assume"])
    else:
        lo = float(evaluate(raw["lo"], p))
        hi = float(evaluate(raw["hi"], p))
        tss_assume = None
    return Step(
        kind=kind,
        duration_s=duration,
        lo=lo,
        hi=hi,
        cadence=raw.get("cadence"),
        cue=raw.get("cue"),
        tss_assume=tss_assume,
    )


def _resolve_steps(
    raw_steps: Sequence[Mapping[str, Any]], params: Mapping[str, int]
) -> tuple[Step | Repeat, ...]:
    items: list[Step | Repeat] = []
    for raw in raw_steps:
        if "repeat" in raw:
            items.append(
                Repeat(
                    count=evaluate(raw["repeat"], dict(params)),
                    cue=raw.get("cue"),
                    steps=tuple(_resolve_leaf(s, params) for s in raw["steps"]),
                )
            )
        else:
            items.append(_resolve_leaf(raw, params))
    return tuple(items)


def _outdoor_leaf(s: Step, widen: int, rests_as_freeride: bool, ramps_as_range: bool) -> Step:
    if s.kind in ("work", "steady") and widen and s.lo is not None and s.hi is not None:
        return replace(s, lo=max(OUTDOOR_WIDEN_FLOOR, s.lo - widen), hi=s.hi + widen)
    if s.kind == "rest" and rests_as_freeride:
        return replace(
            s, kind="freeride", lo=None, hi=None, cadence=None, tss_assume=OUTDOOR_REST_TSS_ASSUME
        )
    if s.kind == "ramp" and ramps_as_range:
        # "10m 50-70%": a plain range. Same mid, so the closed-form TSS is unchanged.
        return replace(s, kind="steady")
    return s


def _outdoor_transform(
    items: Iterable[Step | Repeat], rendering: Mapping[str, Any]
) -> tuple[Step | Repeat, ...]:
    widen = int(rendering.get("widen_pct", 0))
    rests = bool(rendering.get("rests_as_freeride", False))
    ramps = bool(rendering.get("ramps_as_range", False))
    out: list[Step | Repeat] = []
    for it in items:
        if isinstance(it, Repeat):
            out.append(
                replace(it, steps=tuple(_outdoor_leaf(s, widen, rests, ramps) for s in it.steps))
            )
        else:
            out.append(_outdoor_leaf(it, widen, rests, ramps))
    return tuple(out)


def closed_form_tss(items: Iterable[Step | Repeat]) -> tuple[float, int]:
    """Closed-form TSS and total duration over resolved items (README §6).

    `TSS = Σ (dur_s / 3600) × (mid / 100)² × 100`, with `mid = (lo + hi) / 2`, or `tss_assume`
    for freeride leaves.

    Returns:
        `(tss, duration_s)`; TSS is unrounded.
    """
    tss = 0.0
    duration = 0

    def leaf(s: Step) -> tuple[float, int]:
        if s.kind == "freeride" or s.lo is None or s.hi is None:
            mid = s.tss_assume if s.tss_assume is not None else 0.0
        else:
            mid = (s.lo + s.hi) / 2
        return (s.duration_s / 3600) * (mid / 100) ** 2 * 100, s.duration_s

    for it in items:
        if isinstance(it, Repeat):
            for s in it.steps:
                t, d = leaf(s)
                tss += t * it.count
                duration += d * it.count
        else:
            t, d = leaf(it)
            tss += t
            duration += d
    return tss, duration


#: % FTP intervals.icu effectively counts a freeride step at (fitted on published climb
#: repeats: its load matched ours with freeride at ~60–65 %). Verification only: our own TSS
#: keeps the template's ``tss_assume`` (descents really are easier than that).
ICU_FREERIDE_PCT = 62.0


def icu_expected_tss(w: ResolvedWorkout) -> float:
    """The load intervals.icu should compute for ``w`` (freeride at :data:`ICU_FREERIDE_PCT`)."""

    def as_icu(s: Step) -> Step:
        return replace(s, tss_assume=ICU_FREERIDE_PCT) if s.kind == "freeride" else s

    items = [
        Repeat(it.count, it.cue, tuple(as_icu(s) for s in it.steps))
        if isinstance(it, Repeat)
        else as_icu(it)
        for it in w.items
    ]
    return closed_form_tss(items)[0]


_RESOLVE_CACHE: dict[tuple[Any, ...], tuple[Template, ResolvedWorkout]] = {}
_RESOLVE_CACHE_MAX = 4096


def resolve(
    t: Template,
    params: dict[str, int] | None = None,
    *,
    outdoor: bool = False,
    target: TargetMode = "POWER",
) -> ResolvedWorkout:
    """Memoised :func:`resolve_uncached` (templates and results are immutable).

    The key holds the template object itself, so a template edited and reloaded under the same
    id/version never collides with the old one.
    """
    key = (id(t), t.id, t.version, tuple(sorted((params or {}).items())), outdoor, target)
    hit = _RESOLVE_CACHE.get(key)
    if hit is not None and hit[0] is t:
        return hit[1]
    w = resolve_uncached(t, params, outdoor=outdoor, target=target)
    if len(_RESOLVE_CACHE) >= _RESOLVE_CACHE_MAX:
        _RESOLVE_CACHE.clear()
    _RESOLVE_CACHE[key] = (t, w)
    return w


def resolve_uncached(
    t: Template,
    params: dict[str, int] | None = None,
    *,
    outdoor: bool = False,
    target: TargetMode = "POWER",
) -> ResolvedWorkout:
    """Resolve a template to concrete steps.

    Args:
        t: The template.
        params: Param values; missing ones take their default. Unknown names, values outside
            `[min, max]` or off the step grid raise (no silent clamp, README §2).
        outdoor: Apply `outdoor_rendering` transforms (power target only).
        target: `"POWER"` uses `steps`; `"HR"` uses `hr_fallback.steps` (% LTHR).

    Raises:
        TemplateError: On invalid params, an unsupported venue, or HR without `hr_fallback`.
    """
    values = _check_params(t, params)
    if outdoor and not t.outdoor_ok:
        raise TemplateError(f"{t.id}: template is not outdoor_ok")
    if not outdoor and not t.indoor_ok:
        raise TemplateError(f"{t.id}: template is not indoor_ok")
    note_zh: str | None = None
    note: str | None = None
    if target == "HR":
        if t.hr_fallback is None:
            raise TemplateError(f"{t.id}: no hr_fallback ({t.hr_fallback_reason_zh})")
        items = _resolve_steps(t.hr_fallback["steps"], values)
        if isinstance(t.hr_fallback.get("note_zh"), str):
            note_zh = _substitute(t.hr_fallback["note_zh"], values)
    elif target == "POWER":
        items = _resolve_steps(t.steps, values)
        if outdoor and t.outdoor_rendering is not None:
            items = _outdoor_transform(items, t.outdoor_rendering)
    else:
        raise TemplateError(f"{t.id}: unknown target {target!r}")
    if outdoor and t.outdoor_rendering is not None:
        if note_zh is None and isinstance(t.outdoor_rendering.get("note_zh"), str):
            note_zh = _substitute(t.outdoor_rendering["note_zh"], values)
        if isinstance(t.outdoor_rendering.get("note"), str):
            note = _substitute(t.outdoor_rendering["note"], values)
    tss, duration = closed_form_tss(items)
    if_ = math.sqrt(tss / (duration / 3600 * 100)) if duration > 0 else 0.0
    return ResolvedWorkout(
        template_id=t.id,
        template_version=t.version,
        params=values,
        target=target,
        outdoor=outdoor,
        items=items,
        name=_substitute(t.name, values),
        name_zh=_substitute(t.name_zh, values),
        duration_s=duration,
        tss=tss,
        if_=if_,
        note_zh=note_zh,
        note=note,
    )


def nominal_tss(t: Template, params: dict[str, int] | None = None) -> tuple[float, int]:
    """Closed-form TSS and duration of the untransformed power steps (README §9 table).

    This is the venue-independent value for `workout_templates` (no outdoor transforms), so it
    is defined for outdoor-only templates too. Params are validated as in `resolve`.

    Returns:
        `(tss, duration_s)`.
    """
    return closed_form_tss(_resolve_steps(t.steps, _check_params(t, params)))


def param_grid(t: Template) -> list[dict[str, int]]:
    """Enumerate every valid combination of the ranged params (fixed params at their value).

    Each dict is complete and can be passed straight to `resolve`. Ordered lexicographically by
    param declaration order, ascending values.

    Raises:
        TemplateError: If the grid would exceed `PARAM_GRID_CAP` combinations.
    """
    names = list(t.params)
    axes = [t.params[n].values() for n in names]
    size = math.prod(len(a) for a in axes)
    if size > PARAM_GRID_CAP:
        raise TemplateError(f"{t.id}: param grid has {size} combos (> {PARAM_GRID_CAP})")
    return [dict(zip(names, combo, strict=True)) for combo in itertools.product(*axes)]
