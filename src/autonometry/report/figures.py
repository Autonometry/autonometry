"""The report's figures as inline SVG (definitions.md 11), drawn with matplotlib from numbers and
fixed labels only: no text from logs, registries or the command line ever reaches a figure.

Colours are the Okabe-Ito palette (colour-blind safe); each state always has the same colour and
``unknown`` is also hatched, so it never depends on colour alone. Output is deterministic: a fixed
``svg.hashsalt``, no date or creator metadata, and every element id prefixed with the figure's
name, so several figures can sit in one HTML page.
"""

import io
import re
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any, Final

import matplotlib as mpl
from matplotlib.axes import Axes
from matplotlib.figure import Figure

Json = dict[str, Any]

#: Fill colour of each whole-robot state (Okabe-Ito; idle and unknown are neutral greys).
STATE_COLOURS: Final[Mapping[str, str]] = {
    "ai": "#0072B2",
    "scripted": "#56B4E9",
    "shared": "#009E73",
    "human_local": "#E69F00",
    "human_remote": "#D55E00",
    "safety_stop": "#CC79A7",
    "idle": "#BBBBBB",
    "unknown": "#595959",
}
STATE_LABELS: Final[Mapping[str, str]] = {
    "ai": "AI",
    "scripted": "scripted",
    "shared": "shared",
    "human_local": "human, local",
    "human_remote": "human, remote",
    "safety_stop": "safety stop",
    "idle": "idle",
    "unknown": "unknown",
}
INK: Final = "#222222"
MUTED: Final = "#767676"
#: The time share and its interval share the colour of AI time; bounds and variants are ink.
SHARE_COLOUR: Final = STATE_COLOURS["ai"]
OUTER_COLOUR: Final = "#C9DDEB"
_SALT: Final = "autonometry-report"
_WIDTH_IN: Final = 7.0
_RC: Final[Mapping[str, Any]] = {
    "svg.hashsalt": _SALT,
    "svg.fonttype": "path",
    "font.size": 8.5,
    "axes.edgecolor": MUTED,
    "axes.labelcolor": INK,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "text.color": INK,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "hatch.color": "#FFFFFF",
}
_METADATA: Final = {"Date": None, "Creator": None, "Format": None, "Type": None}


def _style() -> AbstractContextManager[None]:
    """The report's matplotlib settings for the duration of a block."""
    rc: Any = dict(_RC)  # rc_context's key type differs between matplotlib versions
    return mpl.rc_context(rc)


def _svg(figure: Figure, name: str, label: str) -> str:
    """The figure as an ``<svg>`` element without XML prolog or doctype, every id prefixed with
    ``name``, sized by CSS and labelled for screen readers with the fixed ``label``."""
    buffer = io.StringIO()
    with _style():
        figure.savefig(buffer, format="svg", metadata=_METADATA)
    text = buffer.getvalue()
    text = text[text.index("<svg") :]
    text = re.sub(r'\bid="([^"]+)"', rf'id="{name}-\1"', text)
    text = re.sub(r'href="#([^"]+)"', rf'href="#{name}-\1"', text)
    text = re.sub(r"url\(#([^)]+)\)", rf"url(#{name}-\1)", text)
    return re.sub(
        r' width="[0-9.]+pt" height="[0-9.]+pt"',
        f' class="figure" role="img" aria-label="{label}"',
        text,
        count=1,
    )


def _figure(height_in: float) -> Figure:
    with _style():
        return Figure(figsize=(_WIDTH_IN, height_in), layout="constrained")


def _percent_axis(axes: Axes) -> None:
    axes.set_xlim(0.0, 1.0)
    axes.set_xticks([0.0, 0.25, 0.5, 0.75, 1.0], ["0 %", "25 %", "50 %", "75 %", "100 %"])
    axes.grid(axis="x", color="#E5E5E5", linewidth=0.6)
    axes.set_axisbelow(True)


def state_shares(state_seconds: Mapping[str, Json]) -> str | None:
    """Whole-robot state shares of observed time, one stacked bar per group."""
    groups = [g for g in sorted(state_seconds) if state_seconds[g]["observed_s"] > 0]
    if not groups:
        return None
    with _style():
        figure = _figure(0.9 + 0.45 * len(groups))
        axes = figure.subplots()
        for row, group in enumerate(groups):
            observed = state_seconds[group]["observed_s"]
            left = 0.0
            for state, colour in STATE_COLOURS.items():
                share = state_seconds[group]["states_s"].get(state, 0.0) / observed
                axes.barh(
                    row,
                    share,
                    left=left,
                    height=0.6,
                    color=colour,
                    edgecolor="#FFFFFF",
                    linewidth=1.0,
                    hatch="///" if state == "unknown" else None,
                    label=STATE_LABELS[state] if row == 0 else None,
                )
                left += share
        axes.set_yticks(range(len(groups)), groups)
        axes.invert_yaxis()
        _percent_axis(axes)
        axes.set_xlabel("share of observed time, whole robot")
        figure.legend(
            loc="outside lower center", ncols=4, frameon=False, handlelength=1.2, fontsize=8
        )
        return _svg(figure, "states", "Whole-robot state shares of observed time per group")


#: Rows of the time-share figure: label, path into the metric block.
_SHARE_ROWS: Final[Sequence[tuple[str, tuple[str, ...]]]] = (
    ("point estimate", ()),
    ("unknown as human", ("unknown_as_human",)),
    ("unknown as AI", ("unknown_as_ai",)),
    ("with approvals", ("variants", "with_approvals")),
    ("shared as AI", ("variants", "shared_as_ai")),
    ("scope weighted", ("variants", "scope_weighted")),
    ("unsupported", ("variants", "unsupported")),
)


def _at(block: Json, path: Sequence[str]) -> Json:
    for key in path:
        block = block[key]
    return block


def time_share(groups: Mapping[str, Json]) -> str | None:
    """The time autonomy share per group: point estimate with its interval, both bounds with
    theirs, the outer interval as a band behind them, and the variants."""
    names = [g for g in sorted(groups) if groups[g]["time_autonomy_share"]["estimate"] is not None]
    if not names:
        return None
    with _style():
        figure = _figure(0.4 + 1.9 * len(names))
        panels = figure.subplots(len(names), 1, squeeze=False)[:, 0]
        for axes, name in zip(panels, names, strict=True):
            share = groups[name]["time_autonomy_share"]
            outer = share.get("outer_interval")
            if outer is not None:
                axes.axvspan(outer[0], outer[1], color=OUTER_COLOUR, linewidth=0)
            for row, (_, path) in enumerate(_SHARE_ROWS):
                block = _at(share, path)
                estimate, ci = block.get("estimate"), block.get("ci")
                if estimate is None:
                    continue
                colour = SHARE_COLOUR if row == 0 else INK
                if ci is not None and None not in ci:
                    axes.plot(ci, [row, row], color=colour, linewidth=2.0, solid_capstyle="butt")
                axes.plot([estimate], [row], "o", color=colour, markersize=5)
            axes.set_yticks(range(len(_SHARE_ROWS)), [label for label, _ in _SHARE_ROWS])
            axes.set_ylim(len(_SHARE_ROWS) - 0.5, -0.5)
            _percent_axis(axes)
            axes.set_title(f"{name}: time autonomy share", loc="left", fontsize=9)
        panels[-1].set_xlabel("dot: estimate; line: confidence interval; band: outer interval")
        return _svg(figure, "share", "Time autonomy share with intervals, bounds and variants")


def gaps_km(curves: Mapping[str, Json | None]) -> str | None:
    """Kaplan-Meier curve of the gaps between interventions (active hours) with its pointwise
    band, one panel per group."""
    present = [(g, curve) for g in sorted(curves) if (curve := curves[g]) is not None]
    if not present:
        return None
    with _style():
        figure = _figure(0.4 + 1.9 * len(present))
        panels = figure.subplots(len(present), 1, squeeze=False)[:, 0]
        for axes, (name, curve) in zip(panels, present, strict=True):
            axes.fill_between(
                curve["times"],
                curve["lower"],
                curve["upper"],
                step="post",
                color=OUTER_COLOUR,
                linewidth=0,
            )
            axes.step(curve["times"], curve["survival"], where="post", color=INK, linewidth=1.5)
            axes.axhline(0.5, color=MUTED, linewidth=0.6, linestyle=":")
            axes.set_ylim(0.0, 1.02)
            axes.set_xlim(left=0.0)
            axes.set_ylabel("S(t)")
            axes.set_title(f"{name}: gap without an intervention", loc="left", fontsize=9)
        panels[-1].set_xlabel("active hours since the previous intervention (band: pointwise CI)")
        return _svg(figure, "gaps", "Kaplan-Meier curve of the gaps between interventions")


def drift(bins: Mapping[str, Sequence[Json]]) -> str | None:
    """Interventions per active hour per calendar bin with exact Poisson intervals, one panel
    per group."""
    names = [g for g in sorted(bins) if any(b["rate"] is not None for b in bins[g])]
    if not names:
        return None
    with _style():
        figure = _figure(0.6 + 1.8 * len(names))
        panels = figure.subplots(len(names), 1, squeeze=False)[:, 0]
        for axes, name in zip(panels, names, strict=True):
            rows = list(bins[name])
            for index, item in enumerate(rows):
                if item["ci"] is not None and None not in item["ci"]:
                    axes.plot([index, index], item["ci"], color=INK, linewidth=1.5)
                if item["rate"] is not None:
                    axes.plot([index], [item["rate"]], "o", color=INK, markersize=5)
            axes.set_xticks(range(len(rows)), [str(item["bin"]) for item in rows])
            axes.set_xlim(-0.5, len(rows) - 0.5)
            axes.set_ylim(bottom=0.0)
            axes.grid(axis="y", color="#E5E5E5", linewidth=0.6)
            axes.set_axisbelow(True)
            axes.set_ylabel("per active hour")
            axes.set_title(f"{name}: interventions per active hour", loc="left", fontsize=9)
        panels[-1].set_xlabel("calendar bin (UTC); line: exact Poisson interval")
        return _svg(figure, "drift", "Interventions per active hour per calendar bin")


def figures(report: Json) -> dict[str, str | None]:
    """Every figure of a report dict, by name; ``None`` where there is nothing to draw."""
    data = report["figures"]
    return {
        "states": state_shares(data["state_seconds"]),
        "share": time_share(report["metrics"]["groups"]),
        "gaps": gaps_km(data["gaps_km"]),
        "drift": drift(data["drift"]),
    }
