"""
Report generator: takes BenchmarkResult lists and produces
  1. A summary table (stdout / text)
  2. matplotlib figures saved to disk
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runner import BenchmarkResult

ALGO_LABELS = {
    "token_bucket":           "Token Bucket",
    "sliding_window_log":     "Sliding Window Log",
    "sliding_window_counter": "Sliding Window Counter",
}

ALGO_COLORS = {
    "token_bucket":           "#4C9BE8",
    "sliding_window_log":     "#E8854C",
    "sliding_window_counter": "#5CBE6A",
}


def print_table(results: list["BenchmarkResult"]) -> None:
    header = (
        f"{'Algorithm':<26} {'Pattern':<24} {'Total':>7} "
        f"{'Allowed':>8} {'Rejected':>9} {'Allow%':>7} "
        f"{'Mem(B)':>8} {'p50µs':>7} {'p99µs':>7}"
    )
    sep = "-" * len(header)
    print(sep)
    print(header)
    print(sep)
    for r in results:
        print(
            f"{ALGO_LABELS.get(r.algorithm, r.algorithm):<26} "
            f"{r.pattern_name:<24} "
            f"{r.total_requests:>7} "
            f"{r.allowed:>8} "
            f"{r.rejected:>9} "
            f"{r.allow_rate * 100:>6.1f}% "
            f"{r.memory_bytes:>8} "
            f"{r.latency_p50_us:>7.1f} "
            f"{r.latency_p99_us:>7.1f} "
        )
    print(sep)


def save_charts(results: list["BenchmarkResult"], output_dir: str = ".") -> list[str]:
    """Generate comparison charts. Returns list of saved file paths."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import numpy as np
    except ImportError:
        print("matplotlib not available — skipping charts")
        return []

    os.makedirs(output_dir, exist_ok=True)
    saved = []

    # -----------------------------------------------------------------------
    # 1. Allow rate by (algorithm × pattern)
    # -----------------------------------------------------------------------
    patterns = sorted({r.pattern_name for r in results})
    algorithms = ["token_bucket", "sliding_window_log", "sliding_window_counter"]

    fig, ax = plt.subplots(figsize=(12, 5))
    x = np.arange(len(patterns))
    width = 0.25
    for i, algo in enumerate(algorithms):
        vals = []
        for pat in patterns:
            match = [r for r in results if r.algorithm == algo and r.pattern_name == pat]
            vals.append(match[0].allow_rate * 100 if match else 0)
        bars = ax.bar(x + i * width, vals, width, label=ALGO_LABELS[algo],
                      color=ALGO_COLORS[algo], edgecolor="white", linewidth=0.8)
        for bar, v in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5,
                    f"{v:.0f}%", ha="center", va="bottom", fontsize=7)

    ax.set_xlabel("Traffic Pattern")
    ax.set_ylabel("Allow Rate (%)")
    ax.set_title("Allow Rate by Algorithm × Traffic Pattern")
    ax.set_xticks(x + width)
    ax.set_xticklabels([p.replace("_", "\n") for p in patterns], fontsize=8)
    ax.legend()
    ax.set_ylim(0, 115)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    path1 = os.path.join(output_dir, "allow_rate.png")
    fig.savefig(path1, dpi=150)
    plt.close(fig)
    saved.append(path1)

    # -----------------------------------------------------------------------
    # 2. Memory footprint comparison
    # -----------------------------------------------------------------------
    fig, ax = plt.subplots(figsize=(9, 4))
    # Use the pattern with highest memory demand (sliding_window_log / burst)
    mem_data = {}
    for algo in algorithms:
        mem_vals = [r.memory_bytes for r in results if r.algorithm == algo]
        mem_data[algo] = max(mem_vals) if mem_vals else 0

    bars = ax.bar(
        [ALGO_LABELS[a] for a in algorithms],
        [mem_data[a] for a in algorithms],
        color=[ALGO_COLORS[a] for a in algorithms],
        edgecolor="white",
    )
    for bar, algo in zip(bars, algorithms):
        v = mem_data[algo]
        ax.text(bar.get_x() + bar.get_width() / 2, v + 5,
                f"{v} B", ha="center", va="bottom", fontsize=9, fontweight="bold")
    ax.set_ylabel("Peak Redis Memory (bytes, single key)")
    ax.set_title("Memory Footprint — Peak per Key")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    path2 = os.path.join(output_dir, "memory_footprint.png")
    fig.savefig(path2, dpi=150)
    plt.close(fig)
    saved.append(path2)

    # -----------------------------------------------------------------------
    # 3. Latency (p50 vs p99) per algorithm
    # -----------------------------------------------------------------------
    fig, ax = plt.subplots(figsize=(9, 4))
    x = np.arange(len(algorithms))
    p50_vals = []
    p99_vals = []
    for algo in algorithms:
        algo_results = [r for r in results if r.algorithm == algo]
        p50_vals.append(np.mean([r.latency_p50_us for r in algo_results]) if algo_results else 0)
        p99_vals.append(np.mean([r.latency_p99_us for r in algo_results]) if algo_results else 0)

    w = 0.35
    b1 = ax.bar(x - w / 2, p50_vals, w, label="p50", color="#7EB5E8", edgecolor="white")
    b2 = ax.bar(x + w / 2, p99_vals, w, label="p99", color="#E87E7E", edgecolor="white")
    for bar, v in list(zip(b1, p50_vals)) + list(zip(b2, p99_vals)):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.2,
                f"{v:.0f}", ha="center", va="bottom", fontsize=8)
    ax.set_ylabel("Latency (µs, in-process fakeredis)")
    ax.set_title("Check() Latency (mean across patterns)")
    ax.set_xticks(x)
    ax.set_xticklabels([ALGO_LABELS[a] for a in algorithms], fontsize=8)
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    path3 = os.path.join(output_dir, "latency.png")
    fig.savefig(path3, dpi=150)
    plt.close(fig)
    saved.append(path3)

    print(f"\nCharts saved: {saved}")
    return saved
