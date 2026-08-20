#!/usr/bin/env python3
"""Render the handbook's static figures into docs/assets/img/figures/.

Deterministic (seeded RNGs, Agg backend, bundled DejaVu Sans font) and
dependency-light: stdlib + matplotlib (+ numpy, which matplotlib requires).

    .venv/bin/python scripts/gen_figures.py
"""

from __future__ import annotations

import math
import random
import time
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.patches import Circle, ConnectionPatch, FancyBboxPatch, Patch, Polygon, Rectangle
from matplotlib.ticker import NullLocator

matplotlib.use("Agg")

OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "assets" / "img" / "figures"
DPI = 200

# Palette ---------------------------------------------------------------------
INDIGO = "#3f51b5"
ORANGE = "#ff7043"
ORANGE_DARK = "#d84315"
TEAL = "#26a69a"
AMBER = "#ffa726"
INK = "#263238"
GREY_DARK = "#424242"
GREY = "#757575"
GREY_LIGHT = "#bdbdbd"
GREY_FAINT = "#ececec"
BORDER = "#d4d4d4"
INDIGO_TINT = "#e8eaf6"
ORANGE_TINT = "#fff3e0"
TEAL_TINT = "#e0f2f1"
AMBER_TINT = "#fff8e1"
NOTE_BG = "#fafafa"


def _style() -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 11,
            "axes.titlesize": 13,
            "axes.titleweight": "bold",
            "axes.labelsize": 11,
            "xtick.labelsize": 11,
            "ytick.labelsize": 11,
            "legend.fontsize": 11,
            "figure.titlesize": 14,
            "figure.titleweight": "bold",
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "axes.edgecolor": GREY_LIGHT,
            "axes.labelcolor": GREY_DARK,
            "xtick.color": GREY_DARK,
            "ytick.color": GREY_DARK,
            "text.color": INK,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "legend.frameon": True,
            "legend.framealpha": 0.95,
            "legend.edgecolor": GREY_FAINT,
            "savefig.dpi": DPI,
        }
    )


def _new_figure(width: float, height: float) -> plt.Figure:
    fig = plt.figure(figsize=(width, height), facecolor="white", layout="constrained")
    # A thin light-grey frame around the whole image (half of the stroke is clipped).
    fig.patch.set_edgecolor(BORDER)
    fig.patch.set_linewidth(3.0)
    return fig


def _save(fig: plt.Figure, name: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    fig.savefig(path, dpi=DPI, facecolor="white", edgecolor=BORDER)
    plt.close(fig)
    return path


def _box(
    ax: plt.Axes,
    x: float,
    y: float,
    w: float,
    h: float,
    text: str,
    *,
    fc: str = "white",
    ec: str = INDIGO,
    tc: str = INK,
    fontsize: float = 11,
    bold: bool = False,
    lw: float = 1.4,
    radius: float = 0.8,
) -> None:
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle=f"round,pad=0,rounding_size={radius}",
            fc=fc,
            ec=ec,
            lw=lw,
            zorder=3,
        )
    )
    ax.text(
        x + w / 2,
        y + h / 2,
        text,
        ha="center",
        va="center",
        fontsize=fontsize,
        color=tc,
        fontweight="bold" if bold else "normal",
        linespacing=1.3,
        zorder=4,
    )


def _arrow(
    ax: plt.Axes,
    p0: tuple[float, float],
    p1: tuple[float, float],
    *,
    color: str = GREY_DARK,
    lw: float = 1.4,
    ls: str = "-",
) -> None:
    ax.annotate(
        "",
        xy=p1,
        xytext=p0,
        arrowprops={
            "arrowstyle": "-|>",
            "color": color,
            "lw": lw,
            "linestyle": ls,
            "shrinkA": 0,
            "shrinkB": 0,
            "mutation_scale": 14,
        },
        zorder=5,
    )


# 1. Latency ladder -------------------------------------------------------------
LATENCIES: list[tuple[str, float, str]] = [
    ("L1 cache reference", 1, "cpu"),
    ("Branch mispredict", 3, "cpu"),
    ("L2 cache reference", 4, "cpu"),
    ("Mutex lock/unlock", 17, "cpu"),
    ("Main memory reference", 100, "cpu"),
    ("Compress 1 KB (Snappy)", 2_000, "cpu"),
    ("Read 1 MB sequentially from memory", 3_000, "cpu"),
    ("SSD random read", 16_000, "storage"),
    ("Read 1 MB sequentially from SSD", 50_000, "storage"),
    ("Round trip within same datacenter", 500_000, "network"),
    ("Read 1 MB sequentially from HDD", 1_000_000, "storage"),
    ("HDD seek", 2_000_000, "storage"),
    ("Round trip between US regions (east-west)", 70_000_000, "network"),
    ("Round trip California ↔ Netherlands", 150_000_000, "network"),
]
TIER_COLORS = {"cpu": INDIGO, "storage": TEAL, "network": ORANGE}
TIER_NAMES = {"cpu": "CPU / memory", "storage": "Storage", "network": "Network"}


def fmt_ns(ns: float) -> str:
    """1 -> '1 ns', 2000 -> '2 µs', 150_000_000 -> '150 ms'."""
    for unit, scale in (("s", 1e9), ("ms", 1e6), ("µs", 1e3)):
        if ns >= scale:
            return f"{ns / scale:g} {unit}"
    return f"{ns:g} ns"


def fig_latency_ladder() -> Path:
    fig = _new_figure(10, 6)
    ax = fig.add_subplot(111)
    labels = [row[0] for row in LATENCIES]
    values = [row[1] for row in LATENCIES]
    colors = [TIER_COLORS[row[2]] for row in LATENCIES]
    ys = list(range(len(LATENCIES)))
    left = 0.4
    ax.barh(ys, [v - left for v in values], left=left, color=colors, height=0.68, zorder=3)
    ax.set_xscale("log")
    ax.set_xlim(left, 4e9)
    ax.set_yticks(ys)
    ax.set_yticklabels(labels)
    ax.invert_yaxis()  # fastest at the top
    ticks = [1, 10, 100, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8, 1e9]
    ax.set_xticks(ticks)
    ax.set_xticklabels([fmt_ns(t) for t in ticks])
    ax.xaxis.set_minor_locator(NullLocator())
    ax.set_xlabel("time, log scale (each gridline is 10× the previous one)")
    ax.grid(axis="x", color=GREY_FAINT, zorder=0)
    ax.set_axisbelow(True)
    ax.tick_params(axis="y", length=0)
    for yi, v in zip(ys, values, strict=True):
        ax.text(v * 1.3, yi, fmt_ns(v), va="center", ha="left", fontsize=11, color=GREY_DARK)
    handles = [
        Patch(color=TIER_COLORS[t], label=TIER_NAMES[t]) for t in ("cpu", "storage", "network")
    ]
    ax.legend(handles=handles, loc="upper right", title="Tier")
    ax.set_title(
        "Latency numbers every engineer should know (order of magnitude, 2020s hardware)",
        loc="center",
        x=0.22,  # centre the title on the figure rather than on the (narrow) axes
    )
    return _save(fig, "latency_ladder.png")


# 2. Consistent-hashing ring -----------------------------------------------------
def _ring_xy(pos_deg: float, radius: float) -> tuple[float, float]:
    """Ring position measured clockwise from 12 o'clock -> screen x, y."""
    a = math.radians(pos_deg)
    return radius * math.sin(a), radius * math.cos(a)


def _cw_distance(from_deg: float, to_deg: float) -> float:
    """Clockwise (increasing-position) distance from one ring position to another."""
    return (to_deg - from_deg) % 360.0


def _angular_gap(a: float, b: float) -> float:
    return min(_cw_distance(a, b), _cw_distance(b, a))


def _spaced_angles(
    rng: random.Random, n: int, min_sep: float, avoid: list[tuple[float, float]]
) -> list[float]:
    """Deterministic pseudo-random ring positions that keep labels apart.

    `avoid` lists (position, clearance) pairs that candidates must stay away from.
    """
    out: list[float] = []
    while len(out) < n:
        cand = rng.uniform(0, 360)
        if all(_angular_gap(a, cand) >= min_sep for a in out) and all(
            _angular_gap(a, cand) >= sep for a, sep in avoid
        ):
            out.append(cand)
    return out


def fig_hash_ring() -> Path:
    rng = random.Random(42)
    nodes = ["A", "B", "C", "D"]
    node_colors = {"A": INDIGO, "B": ORANGE, "C": TEAL, "D": AMBER}
    hint_pos, hint_clearance = 15.0, 24.0  # the "clockwise" hint lives inside the ring here
    vnode_pos = _spaced_angles(rng, 12, min_sep=16, avoid=[(0.0, 14.0)])
    vnodes = [
        (vnode_pos[i * 3 + k], node, f"{node}{k + 1}")
        for i, node in enumerate(nodes)
        for k in range(3)
    ]
    key_avoid = [(p, 8.0) for p in vnode_pos] + [(0.0, 10.0), (hint_pos, hint_clearance)]
    key_pos = _spaced_angles(rng, 6, min_sep=22, avoid=key_avoid)
    keys = [(pos, f"k{i + 1}") for i, pos in enumerate(key_pos)]

    def owner(pos: float) -> tuple[float, str, str]:
        # clockwise = increasing ring position; wrap around past 360 -> 0
        return min(vnodes, key=lambda vn: _cw_distance(pos, vn[0]))

    fig = _new_figure(8.5, 8.5)
    ax = fig.add_subplot(111)
    ax.set_aspect("equal")
    ax.set_xlim(-1.3, 1.3)
    ax.set_ylim(-1.3, 1.3)
    ax.axis("off")

    ax.add_patch(Circle((0, 0), 1.0, fill=False, lw=3, ec=GREY_LIGHT, zorder=1))
    # hash-space origin + direction hint
    ax.plot([0, 0], [0.95, 1.05], color=GREY_DARK, lw=2, zorder=2)
    ax.text(0, 1.09, "0 = 2³²", ha="center", va="bottom", fontsize=11, color=GREY_DARK)
    ax.annotate(
        "",
        xy=_ring_xy(hint_pos + 9, 0.86),
        xytext=_ring_xy(hint_pos - 9, 0.86),
        arrowprops={
            "arrowstyle": "-|>",
            "color": GREY,
            "lw": 1.2,
            "connectionstyle": "arc3,rad=-0.12",
        },
    )
    ax.text(
        *_ring_xy(hint_pos, 0.72), "clockwise", ha="center", va="center", fontsize=11, color=GREY
    )

    key_r = 0.90
    lookups: list[str] = []
    for pos, name in keys:
        vpos, node, vlabel = owner(pos)
        color = node_colors[node]
        delta = _cw_distance(pos, vpos)
        n_pts = max(12, int(delta / 1.5))
        ts = [i / n_pts for i in range(n_pts + 1)]
        # arc along the ring that drifts outwards at the end to touch the vnode marker
        pts = [_ring_xy(pos + t * delta, key_r + 0.055 * t**4) for t in ts]
        split = max(1, n_pts - max(2, int(3.0 / (delta / n_pts))))
        xs, ys = zip(*pts[: split + 1], strict=True)
        ax.plot(xs, ys, color=color, lw=1.3, zorder=3)
        _arrow(ax, pts[split], pts[-1], color=color, lw=1.3)
        ax.plot(*_ring_xy(pos, key_r), "o", color="black", ms=7, zorder=6)
        ax.text(
            *_ring_xy(pos, 0.79), name, ha="center", va="center", fontsize=11, fontweight="bold"
        )
        lookups.append(f"{name} → {vlabel}  (node {node})")

    for pos, node, label in vnodes:
        x, y = _ring_xy(pos, 1.0)
        ax.plot(x, y, "o", ms=15, mfc=node_colors[node], mec="white", mew=1.5, zorder=7)
        ax.text(
            *_ring_xy(pos, 1.16), label, ha="center", va="center", fontsize=11, fontweight="bold"
        )

    ax.text(
        0,
        -0.05,
        "lookup (first vnode clockwise)\n" + "\n".join(lookups),
        ha="center",
        va="center",
        fontsize=11,
        color=INK,
        linespacing=1.5,
        bbox={"boxstyle": "round,pad=0.5", "fc": NOTE_BG, "ec": GREY_FAINT},
        zorder=2,
    )

    handles = [Patch(color=node_colors[n], label=f"node {n}") for n in nodes]
    handles.append(Line2D([], [], marker="o", color="black", ls="none", ms=7, label="key"))
    fig.legend(
        handles=handles,
        loc="outside lower center",
        ncol=5,
        frameon=False,
        handlelength=1.2,
        title="4 physical nodes × 3 virtual nodes each; 6 keys (k1…k6)",
    )
    ax.set_title("Consistent hashing with virtual nodes\n(keys map clockwise to the next vnode)")
    return _save(fig, "hash_ring.png")


# 3. LSM tree -------------------------------------------------------------------
def fig_lsm_compaction() -> Path:
    fig = _new_figure(10, 6.4)
    ax = fig.add_subplot(111)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")

    # write path (top row)
    top_y, top_h = 84, 10
    _box(ax, 2, top_y, 8, top_h, "write", fc=GREY_FAINT, ec=GREY, bold=True)
    _box(ax, 14, top_y, 23, top_h, "WAL\n(append-only log on disk)", fc=ORANGE_TINT, ec=ORANGE)
    _box(ax, 41, top_y, 24, top_h, "MemTable\n(in memory, sorted)", fc=INDIGO_TINT, ec=INDIGO)
    _arrow(ax, (10, top_y + top_h / 2), (14, top_y + top_h / 2))
    _arrow(ax, (37, top_y + top_h / 2), (41, top_y + top_h / 2))
    ax.text(12, top_y + top_h + 1, "append", ha="center", va="bottom", fontsize=11, color=GREY)
    ax.text(39, top_y + top_h + 1, "insert", ha="center", va="bottom", fontsize=11, color=GREY)

    # read-path note (top right)
    ax.text(
        69,
        top_y + top_h / 2 - 1,
        "Read path: MemTable, then every\n"
        "L0 file (key ranges overlap),\n"
        "then ≤ 1 file per level L1…L3.\n"
        "Index + Bloom filter per SSTable\n"
        "skip most files and blocks.",
        ha="left",
        va="center",
        fontsize=11,
        color=GREY_DARK,
        linespacing=1.35,
        bbox={"boxstyle": "round,pad=0.5", "fc": NOTE_BG, "ec": GREY_FAINT},
    )

    # levels
    levels = [
        ("L0", 4, 8.0, 2.0, "L0 ~ 4 files"),
        ("L1", 4, 10.0, 2.0, "L1 10 MB"),
        ("L2", 6, 10.0, 1.2, "L2 100 MB"),
        ("L3", 8, 8.2, 1.0, "L3 1 GB"),
    ]
    row_h = 8
    row_ys = [61, 46, 31, 16]
    box_x0 = 14
    for (name, count, width, gap, note), y in zip(levels, row_ys, strict=True):
        ax.text(7, y + row_h / 2, name, ha="center", va="center", fontsize=13, fontweight="bold")
        for i in range(count):
            _box(
                ax,
                box_x0 + i * (width + gap),
                y,
                width,
                row_h,
                "SST",
                fc=TEAL_TINT if name != "L0" else ORANGE_TINT,
                ec=TEAL if name != "L0" else ORANGE,
                lw=1.2,
                radius=0.6,
            )
        ax.text(88, y + row_h / 2, note, ha="left", va="center", fontsize=11, color=GREY_DARK)

    # flush: MemTable -> L0
    _arrow(ax, (50, top_y), (50, row_ys[0] + row_h), color=INDIGO, lw=1.8)
    ax.text(
        52,
        (top_y + row_ys[0] + row_h) / 2,
        "flush when full\n(immutable → SSTable)",
        ha="left",
        va="center",
        fontsize=11,
        color=INDIGO,
        linespacing=1.3,
    )

    # compaction arrows between levels
    notes = [
        "compaction: merge + dedupe + drop tombstones",
        "compaction (each level ≈ 10× the previous)",
        "compaction",
    ]
    for i, note in enumerate(notes):
        y_from = row_ys[i]
        y_to = row_ys[i + 1] + row_h
        _arrow(ax, (7, y_from), (7, y_to), color=TEAL, lw=1.8)
        ax.text(10, (y_from + y_to) / 2, note, ha="left", va="center", fontsize=11, color=TEAL)

    # bloom filter callout pointing at one SSTable
    ax.text(
        58,
        5,
        "Bloom filter per SSTable: answers\n\"definitely absent\" without a disk read",
        ha="left",
        va="center",
        fontsize=11,
        color=GREY_DARK,
        linespacing=1.35,
        bbox={"boxstyle": "round,pad=0.4", "fc": NOTE_BG, "ec": GREY_FAINT},
    )
    target_x = box_x0 + 5 * (8.2 + 1.0) + 4.1  # 6th SSTable in L3
    _arrow(ax, (66, 9.5), (target_x, row_ys[3]), color=GREY, lw=1.1, ls="--")
    ax.set_title("LSM tree: writes go to memory, reads may touch several levels")
    return _save(fig, "lsm_compaction.png")


# 4. CAP + PACELC -----------------------------------------------------------------
def fig_cap_pacelc() -> Path:
    fig = _new_figure(12, 6.2)
    ax_cap, ax_pac = fig.subplots(1, 2)

    # --- CAP triangle (both panels use the same data aspect so their titles line up)
    ax = ax_cap
    ax.set_xlim(0, 11.4)
    ax.set_ylim(0, 10.4)
    ax.set_aspect("equal")
    ax.axis("off")
    c, a, p = (6.5, 9.0), (3.5, 3.8), (9.5, 3.8)
    ax.add_patch(Polygon([c, a, p], closed=True, fc=INDIGO_TINT, ec="none", zorder=1))
    ax.plot([c[0], p[0]], [c[1], p[1]], color=INDIGO, lw=3, zorder=2)  # CP edge
    ax.plot([a[0], p[0]], [a[1], p[1]], color=ORANGE, lw=3, zorder=2)  # AP edge
    ax.plot([c[0], a[0]], [c[1], a[1]], color=GREY, lw=2, ls=(0, (5, 4)), zorder=2)  # CA edge
    vertices = (
        (c, "C", "Consistency", 0.3),
        (a, "A", "Availability", -0.3),
        (p, "P", "Partition tolerance", -0.3),
    )
    for (x, y), letter, word, dy in vertices:
        ax.plot(x, y, "o", ms=14, color=INK, zorder=3)
        ax.text(
            x, y, letter, ha="center", va="center", color="white", fontsize=11,
            fontweight="bold", zorder=4,
        )
        ax.text(
            x, y + dy, word, ha="center", va="bottom" if dy > 0 else "top", fontsize=11,
            fontweight="bold", color=INK,
        )
    ax.text(
        8.35, 6.6, "CP\nZooKeeper, etcd,\nHBase, Spanner\n(under partition)",
        ha="left", va="center", fontsize=11, color=INDIGO, linespacing=1.35,
    )
    ax.text(
        6.5, 2.75, "AP: Dynamo, Cassandra, Riak, CouchDB",
        ha="center", va="top", fontsize=11, color=ORANGE_DARK,
    )
    ax.text(
        4.65, 6.6, "CA (single node /\nno partitions):\nclassic single-node RDBMS",
        ha="right", va="center", fontsize=11, color=GREY_DARK, linespacing=1.35,
    )
    ax.text(
        6.5, 1.1,
        "Distributed systems must tolerate P:\nthe real choice is C vs A during a partition",
        ha="center", va="center", fontsize=11, style="italic", color=INK, linespacing=1.4,
        bbox={"boxstyle": "round,pad=0.5", "fc": AMBER_TINT, "ec": AMBER},
    )
    ax.set_title("CAP: pick two (during a partition)")

    # --- PACELC matrix
    ax = ax_pac
    ax.set_xlim(-1.6, 2.1)  # width 3.7
    ax.set_ylim(-0.28, 3.1)  # height 3.38 -> same 11.4:10.4 aspect as the left panel
    ax.set_aspect("equal")
    ax.axis("off")
    cells = {
        (0, 1): ("PA / EL", "Dynamo, Cassandra,\nRiak", ORANGE_TINT, ORANGE),
        (1, 1): ("PA / EC", "MongoDB\n(default)", AMBER_TINT, AMBER),
        (0, 0): ("PC / EL", "PNUTS", TEAL_TINT, TEAL),
        (1, 0): ("PC / EC", "Spanner, VoltDB,\nHBase, BigTable", INDIGO_TINT, INDIGO),
    }
    for (cx, cy), (tag, systems, fc, ec) in cells.items():
        ax.add_patch(Rectangle((cx, cy), 1, 1, fc=fc, ec=ec, lw=2, zorder=2))
        ax.text(
            cx + 0.06, cy + 0.93, tag, ha="left", va="top", fontsize=11, fontweight="bold", color=ec
        )
        ax.text(
            cx + 0.5, cy + 0.42, systems, ha="center", va="center", fontsize=11, color=INK,
            linespacing=1.4,
        )
    ax.text(
        1.0, 2.78, "Else (no partition): latency vs consistency", ha="center", va="center",
        fontsize=11, fontweight="bold", color=GREY_DARK,
    )
    header_kw = {"ha": "center", "va": "center", "fontsize": 11, "color": GREY_DARK, "linespacing": 1.3}
    ax.text(0.5, 2.32, "EL\nfavour latency", **header_kw)
    ax.text(1.5, 2.32, "EC\nfavour consistency", **header_kw)
    row_kw = {"ha": "right", "va": "center", "fontsize": 11, "color": GREY_DARK, "linespacing": 1.3}
    ax.text(-0.15, 1.5, "PA\nstay available", **row_kw)
    ax.text(-0.15, 0.5, "PC\nstay consistent", **row_kw)
    ax.text(
        -1.4, 1.0, "Partition: availability vs consistency", ha="center", va="center",
        rotation=90, fontsize=11, fontweight="bold", color=GREY_DARK,
    )
    ax.set_title("PACELC: if Partition then A or C, Else L or C")

    fig.suptitle("CAP and PACELC: what you give up, and when")
    return _save(fig, "cap_pacelc.png")


# 5. Backoff + jitter -------------------------------------------------------------
def _expo(base: float, cap: float, attempt: int) -> float:
    return min(cap, base * 2 ** (attempt - 1))


def fig_backoff_jitter() -> Path:
    rng = random.Random(42)
    base, cap, n_samples = 100.0, 10_000.0, 30
    attempts = list(range(1, 9))
    expo = [_expo(base, cap, n) for n in attempts]
    full = [[rng.uniform(0, e) for _ in range(n_samples)] for e in expo]
    equal = [[e / 2 + rng.uniform(0, e / 2) for _ in range(n_samples)] for e in expo]
    decor: list[list[float]] = []
    for _ in range(n_samples):
        sleep, path = base, []
        for _attempt in attempts:
            sleep = min(cap, rng.uniform(base, sleep * 3))
            path.append(sleep)
        decor.append(path)

    def mean(xs: list[float]) -> float:
        return sum(xs) / len(xs)

    fig = _new_figure(12, 6.2)
    ax1, ax2 = fig.subplots(1, 2, width_ratios=[1.25, 1])

    ax = ax1
    ax.set_yscale("log")
    for n, samples in zip(attempts, full, strict=True):
        ax.scatter([n - 0.16] * n_samples, samples, s=16, color=INDIGO, alpha=0.3, lw=0, zorder=2)
    for n, samples in zip(attempts, equal, strict=True):
        ax.scatter([n + 0.16] * n_samples, samples, s=16, color=TEAL, alpha=0.3, lw=0, zorder=2)
    for path in decor:
        ax.plot(attempts, path, color=ORANGE, lw=0.8, alpha=0.22, zorder=1)
    ax.plot(
        attempts, expo, color=INK, lw=2.2, marker="s", ms=6, zorder=4,
        label="exponential, no jitter: min(cap, base · 2ⁿ⁻¹)",
    )
    ax.plot(
        attempts, [mean(s) for s in full], color=INDIGO, lw=2.2, marker="o", ms=6, zorder=4,
        label="full jitter: U(0, exp) — mean of 30 samples (dots)",
    )
    ax.plot(
        attempts, [mean(s) for s in equal], color=TEAL, lw=2.2, ls="--", marker="D", ms=5,
        zorder=4, label="equal jitter: exp/2 + U(0, exp/2) — mean (dots)",
    )
    ax.plot(
        attempts, [mean(col) for col in zip(*decor, strict=True)], color=ORANGE, lw=2.2,
        marker="^", ms=6, zorder=4,
        label="decorrelated: min(cap, U(base, 3 · prev)) — mean of 30 paths",
    )
    ax.axhline(cap, color=GREY, lw=1.2, ls=":")
    ax.text(8.4, cap * 1.12, "cap = 10 s", ha="right", va="bottom", fontsize=11, color=GREY)
    ax.set_xticks(attempts)
    ax.set_xlim(0.5, 8.5)
    ax.set_ylim(2, 4e5)
    ax.set_yticks([10, 100, 1_000, 10_000])
    ax.set_yticklabels(["10 ms", "100 ms", "1 s", "10 s"])
    ax.yaxis.set_minor_locator(NullLocator())
    ax.grid(axis="y", color=GREY_FAINT)
    ax.set_axisbelow(True)
    ax.set_xlabel("attempt")
    ax.set_ylabel("sleep before the retry (log scale)")
    ax.legend(loc="upper left", fontsize=11, handlelength=2.2)
    ax.set_title("Delay per attempt (base 100 ms, cap 10 s)")

    # --- retry storm histogram
    ax = ax2
    n_clients = 200
    d3 = _expo(base, cap, 3)  # 400 ms
    no_jitter = [d3] * n_clients
    full_jitter = [rng.uniform(0, d3) for _ in range(n_clients)]
    bins = np.arange(0, d3 + 41, 20)
    ax.hist(no_jitter, bins=bins, color=INK, label="no jitter: all 200 at t = 400 ms")
    counts, _edges, _patches = ax.hist(
        full_jitter, bins=bins, color=INDIGO, alpha=0.9, label="full jitter: U(0, 400 ms)"
    )
    ax.annotate(
        "retry storm:\n200 requests in\nthe same instant",
        xy=(d3 + 10, n_clients),
        xytext=(d3 - 110, n_clients * 0.78),
        ha="right",
        va="center",
        fontsize=11,
        color=INK,
        linespacing=1.3,
        arrowprops={"arrowstyle": "-|>", "color": INK, "lw": 1.2},
    )
    ax.annotate(
        f"at most {int(counts.max())} per 20 ms window",
        xy=(bins[int(counts.argmax())] + 10, counts.max()),
        xytext=(40, n_clients * 0.33),
        ha="left",
        va="center",
        fontsize=11,
        color=INDIGO,
        arrowprops={"arrowstyle": "-|>", "color": INDIGO, "lw": 1.2},
    )
    ax.set_xlim(0, d3 + 40)
    ax.set_ylim(0, n_clients * 1.3)
    ax.set_xlabel("retry time after the shared failure at t = 0 (ms)")
    ax.set_ylabel("clients retrying in the same 20 ms window")
    ax.legend(loc="upper left", fontsize=11)
    ax.set_title("200 clients that failed together, attempt 3")

    fig.suptitle("Exponential backoff: jitter spreads the retry storm")
    return _save(fig, "backoff_jitter.png")


# 6. Geohash ----------------------------------------------------------------------
BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash_encode(lat: float, lon: float, precision: int) -> str:
    """Standard geohash: interleave lon/lat bisection bits (lon first), 5 bits per char."""
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    chars: list[str] = []
    bits, n_bits, even = 0, 0, True
    while len(chars) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                bits, lon_lo = bits * 2 + 1, mid
            else:
                bits, lon_hi = bits * 2, mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                bits, lat_lo = bits * 2 + 1, mid
            else:
                bits, lat_hi = bits * 2, mid
        even = not even
        n_bits += 1
        if n_bits == 5:
            chars.append(BASE32[bits])
            bits, n_bits = 0, 0
    return "".join(chars)


def geohash_bounds(gh: str) -> tuple[float, float, float, float]:
    """-> (lat_lo, lat_hi, lon_lo, lon_hi) of the cell named by `gh`."""
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    even = True
    for ch in gh:
        idx = BASE32.index(ch)
        for shift in (4, 3, 2, 1, 0):
            bit = (idx >> shift) & 1
            if even:
                mid = (lon_lo + lon_hi) / 2
                lon_lo, lon_hi = (mid, lon_hi) if bit else (lon_lo, mid)
            else:
                mid = (lat_lo + lat_hi) / 2
                lat_lo, lat_hi = (mid, lat_hi) if bit else (lat_lo, mid)
            even = not even
    return lat_lo, lat_hi, lon_lo, lon_hi


def _cell_center(gh: str) -> tuple[float, float]:
    lat_lo, lat_hi, lon_lo, lon_hi = geohash_bounds(gh)
    return (lat_lo + lat_hi) / 2, (lon_lo + lon_hi) / 2


def _geohash_self_test() -> None:
    # 37.7749, -122.4194 is the textbook "9q8yy" example; also round-trip every cell.
    checks = {
        (37.7749, -122.4194, 5): "9q8yy",
        (37.77, -122.42, 2): "9q",
        (51.5074, -0.1278, 4): "gcpv",
        (-33.8688, 151.2093, 4): "r3gx",
    }
    for (lat, lon, precision), expected in checks.items():
        got = geohash_encode(lat, lon, precision)
        if got != expected:
            raise RuntimeError(f"geohash_encode({lat}, {lon}, {precision}) = {got!r} != {expected!r}")
    for gh in ["9" + ch for ch in BASE32]:
        lat, lon = _cell_center(gh)
        if geohash_encode(lat, lon, 2) != gh:
            raise RuntimeError(f"geohash round trip failed for {gh!r}")


def fig_geohash_grid() -> Path:
    _geohash_self_test()
    sf_lat, sf_lon = 37.77, -122.42
    sf1 = geohash_encode(sf_lat, sf_lon, 1)
    sf2 = geohash_encode(sf_lat, sf_lon, 2)
    lat_lo, lat_hi, lon_lo, lon_hi = geohash_bounds(sf1)
    sub_lat_lo, sub_lat_hi, sub_lon_lo, sub_lon_hi = geohash_bounds(sf2)
    d_lat, d_lon = sub_lat_hi - sub_lat_lo, sub_lon_hi - sub_lon_lo
    c_lat, c_lon = _cell_center(sf2)
    neighbours = {
        geohash_encode(c_lat + i * d_lat, c_lon + j * d_lon, 2)
        for i in (-1, 0, 1)
        for j in (-1, 0, 1)
        if (i, j) != (0, 0)
    }

    fig = _new_figure(12.5, 6.4)
    ax_w, ax_z = fig.subplots(1, 2, width_ratios=[1.75, 1])

    # --- world grid, precision 1
    ax = ax_w
    ax.set_aspect("equal")
    ax.set_xlim(-180, 180)
    ax.set_ylim(-90, 90)
    for ch in BASE32:
        b_lat_lo, b_lat_hi, b_lon_lo, b_lon_hi = geohash_bounds(ch)
        is_sf = ch == sf1
        ax.add_patch(
            Rectangle(
                (b_lon_lo, b_lat_lo),
                b_lon_hi - b_lon_lo,
                b_lat_hi - b_lat_lo,
                fc=ORANGE_TINT if is_sf else (INDIGO_TINT if (BASE32.index(ch) % 2) else "white"),
                ec=GREY_LIGHT,
                lw=0.8,
                zorder=1,
            )
        )
        ax.text(
            (b_lon_lo + b_lon_hi) / 2,
            (b_lat_lo + b_lat_hi) / 2,
            ch,
            ha="center",
            va="center",
            fontsize=14,
            fontweight="bold",
            color=ORANGE if is_sf else GREY_DARK,
            zorder=3,
        )
    ax.add_patch(
        Rectangle(
            (lon_lo, lat_lo), lon_hi - lon_lo, lat_hi - lat_lo, fc="none", ec=ORANGE, lw=2.5,
            zorder=4,
        )
    )
    ax.plot(sf_lon, sf_lat, marker="*", ms=13, color=ORANGE_DARK, mec="white", mew=0.8, zorder=6)
    ax.axhline(0, color=GREY, lw=0.8, ls=":")
    ax.axvline(0, color=GREY, lw=0.8, ls=":")
    ax.set_xticks(range(-180, 181, 45))
    ax.set_xticklabels([f"{t}°" for t in range(-180, 181, 45)])
    ax.set_yticks(range(-90, 91, 45))
    ax.set_yticklabels([f"{t}°" for t in range(-90, 91, 45)])
    ax.set_xlabel("longitude (first bit: lon ≥ 0 → 1)")
    ax.set_ylabel("latitude (second bit: lat ≥ 0 → 1)")
    ax.set_title("precision 1: 32 cells (8 × 4), 5 bits = lon, lat, lon, lat, lon")
    for side in ("top", "right"):
        ax.spines[side].set_visible(True)

    # --- zoom: the 32 precision-2 sub-cells of the cell containing San Francisco
    ax = ax_z
    ax.set_aspect("equal")
    ax.set_xlim(lon_lo, lon_hi)
    ax.set_ylim(lat_lo, lat_hi)
    for ch in BASE32:
        gh = sf1 + ch
        b_lat_lo, b_lat_hi, b_lon_lo, b_lon_hi = geohash_bounds(gh)
        if gh == sf2:
            fc, tc, weight = ORANGE, "white", "bold"
        elif gh in neighbours:
            fc, tc, weight = "#ffccbc", INK, "bold"
        else:
            fc, tc, weight = "white", GREY_DARK, "normal"
        ax.add_patch(
            Rectangle(
                (b_lon_lo, b_lat_lo), b_lon_hi - b_lon_lo, b_lat_hi - b_lat_lo, fc=fc,
                ec=GREY_LIGHT, lw=0.8, zorder=1,
            )
        )
        ax.text(
            (b_lon_lo + b_lon_hi) / 2,
            (b_lat_lo + b_lat_hi) / 2,
            gh,
            ha="center",
            va="center",
            fontsize=11,
            fontweight=weight,
            color=tc,
            zorder=3,
        )
    ax.plot(sf_lon, sf_lat, marker="*", ms=15, color=ORANGE_DARK, mec="white", mew=0.8, zorder=6)
    # cells just north of this cell's top edge belong to a different first character
    for ch in BASE32:
        b_lat_lo, b_lat_hi, b_lon_lo, b_lon_hi = geohash_bounds(sf1 + ch)
        if b_lat_hi == lat_hi:
            north_gh = geohash_encode(lat_hi + d_lat / 2, (b_lon_lo + b_lon_hi) / 2, 2)
            ax.text(
                (b_lon_lo + b_lon_hi) / 2, lat_hi + 0.8, north_gh, ha="center", va="bottom",
                fontsize=11, color=GREY, clip_on=False,
            )
    x_ticks = (lon_lo, (lon_lo + lon_hi) / 2, lon_hi)
    y_ticks = (lat_lo, (lat_lo + lat_hi) / 2, lat_hi)
    ax.set_xticks(x_ticks)
    ax.set_xticklabels([f"{t:g}°" for t in x_ticks])
    ax.set_yticks(y_ticks)
    ax.set_yticklabels([f"{t:g}°" for t in y_ticks])
    ax.set_title(f"cell “{sf1}” at precision 2: 32 sub-cells (4 × 8)", pad=24)
    ax.set_xlabel(
        f"★ San Francisco ({sf_lat}, {sf_lon}) → “{sf2}”\n"
        f"{sf2}'s 8 neighbours all start with “{sf1}”; the cells\n"
        "across the top edge (grey) share no prefix",
        fontsize=11,
        linespacing=1.35,
    )
    for side in ("top", "right"):
        ax.spines[side].set_visible(True)

    for y in (lat_lo, lat_hi):
        fig.add_artist(
            ConnectionPatch(
                xyA=(lon_hi, y), coordsA=ax_w.transData, xyB=(lon_lo, y), coordsB=ax_z.transData,
                color=ORANGE, lw=1.2, ls="--", zorder=0,
            )
        )

    fig.suptitle(
        "Geohash: each extra character subdivides the cell 32 ways;\n"
        "neighbours share prefixes — except across cell boundaries"
    )
    return _save(fig, "geohash_grid.png")


# 7. Quadtree ----------------------------------------------------------------------
class QuadNode:
    """Point-region quadtree node; leaves hold up to `capacity` points."""

    __slots__ = ("capacity", "children", "depth", "h", "points", "w", "x", "y")

    def __init__(self, x: float, y: float, w: float, h: float, depth: int, capacity: int) -> None:
        self.x, self.y, self.w, self.h = x, y, w, h
        self.depth = depth
        self.capacity = capacity
        self.points: list[tuple[float, float]] = []
        self.children: list[QuadNode] | None = None

    def contains(self, p: tuple[float, float]) -> bool:
        return self.x <= p[0] < self.x + self.w and self.y <= p[1] < self.y + self.h

    def insert(self, p: tuple[float, float]) -> bool:
        if not self.contains(p):
            return False
        if self.children is None:
            if len(self.points) < self.capacity:
                self.points.append(p)
                return True
            self._subdivide()
        assert self.children is not None
        return any(child.insert(p) for child in self.children)

    def _subdivide(self) -> None:
        hw, hh = self.w / 2, self.h / 2
        self.children = [
            QuadNode(self.x + dx, self.y + dy, hw, hh, self.depth + 1, self.capacity)
            for dy in (0, hh)
            for dx in (0, hw)
        ]
        old, self.points = self.points, []
        for p in old:
            self.insert(p)

    def intersects(self, rect: tuple[float, float, float, float]) -> bool:
        x0, y0, x1, y1 = rect
        return not (self.x > x1 or self.x + self.w < x0 or self.y > y1 or self.y + self.h < y0)

    def all_nodes(self) -> list[QuadNode]:
        out = [self]
        for child in self.children or []:
            out.extend(child.all_nodes())
        return out

    def range_query(
        self,
        rect: tuple[float, float, float, float],
        visited: list[QuadNode],
        found: list[tuple[float, float]],
    ) -> None:
        """Collect points inside `rect`; `visited` receives every node whose box intersects it."""
        if not self.intersects(rect):
            return
        visited.append(self)
        x0, y0, x1, y1 = rect
        if self.children is None:
            found.extend(p for p in self.points if x0 <= p[0] <= x1 and y0 <= p[1] <= y1)
            return
        for child in self.children:
            child.range_query(rect, visited, found)


def fig_quadtree() -> tuple[Path, int]:
    rng = random.Random(42)
    points = [(rng.uniform(0, 100), rng.uniform(0, 100)) for _ in range(60)]
    root = QuadNode(0, 0, 100, 100, depth=0, capacity=4)
    for p in points:
        if not root.insert(p):
            raise RuntimeError(f"point {p} not inserted")
    query = (60.0, 15.0, 90.0, 45.0)
    visited: list[QuadNode] = []
    found: list[tuple[float, float]] = []
    root.range_query(query, visited, found)
    n_visited = len(visited)
    n_nodes = len(root.all_nodes())

    fig = _new_figure(8.5, 8.5)
    ax = fig.add_subplot(111)
    ax.set_aspect("equal")
    ax.set_xlim(-1, 101)
    ax.set_ylim(-1, 101)
    ax.set_xticks(range(0, 101, 25))
    ax.set_yticks(range(0, 101, 25))
    for side in ("top", "right"):
        ax.spines[side].set_visible(True)
    for node in root.all_nodes():
        lw = max(0.35, 1.7 - 0.35 * node.depth)
        ax.add_patch(
            Rectangle((node.x, node.y), node.w, node.h, fc="none", ec=GREY, lw=lw, zorder=2)
        )
    for node in visited:
        ax.add_patch(
            Rectangle(
                (node.x, node.y), node.w, node.h, fc="none", ec=ORANGE, lw=2.2, zorder=3, alpha=0.9
            )
        )
    x0, y0, x1, y1 = query
    ax.add_patch(
        Rectangle(
            (x0, y0), x1 - x0, y1 - y0, fc=ORANGE, ec=ORANGE_DARK, alpha=0.28, lw=1.5, zorder=4
        )
    )
    xs, ys = zip(*points, strict=True)
    ax.scatter(xs, ys, s=26, color=INDIGO, zorder=5)
    if found:
        fx, fy = zip(*found, strict=True)
        ax.scatter(fx, fy, s=70, color=ORANGE, edgecolors=INK, linewidths=1.0, zorder=6)
    handles = [
        Line2D([], [], color=GREY, lw=1.5, label=f"quadtree node ({n_nodes} total; thinner = deeper)"),
        Line2D([], [], color=ORANGE, lw=2.2, label=f"node visited by the range query ({n_visited})"),
        Line2D([], [], marker="o", color=INDIGO, ls="none", ms=6, label="point (60, seeded)"),
        Patch(
            fc=ORANGE, ec=ORANGE_DARK, alpha=0.4,
            label=f"query box x {x0:g}–{x1:g}, y {y0:g}–{y1:g}",
        ),
        Line2D(
            [], [], marker="o", color=ORANGE, mec=INK, ls="none", ms=9,
            label=f"point inside the query box ({len(found)})",
        ),
    ]
    fig.legend(handles=handles, loc="outside lower center", ncol=2, frameon=False)
    ax.set_title(
        f"Quadtree (capacity 4): range query only visits the {n_visited} nodes that intersect the box"
    )
    return _save(fig, "quadtree.png"), n_visited


def main() -> None:
    _style()
    t0 = time.perf_counter()
    written: list[Path] = []
    written.append(fig_latency_ladder())
    written.append(fig_hash_ring())
    written.append(fig_lsm_compaction())
    written.append(fig_cap_pacelc())
    written.append(fig_backoff_jitter())
    written.append(fig_geohash_grid())
    quadtree_path, n_visited = fig_quadtree()
    written.append(quadtree_path)
    for path in written:
        print(path)
    print(f"quadtree range query visited {n_visited} nodes")
    print(f"wrote {len(written)} figures in {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()
