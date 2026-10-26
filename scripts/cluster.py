#!/usr/bin/env python3
"""
TMKOC Arc Clustering & Linked-List Builder
===========================================
Reads canonical_episodes.jsonl, groups episodes into Story Arcs
using episode-number gap detection + title keyword signals, then
builds a doubly-linked list (prev/next video_id) within each arc.

Outputs:
  data/arcs.json         → final artifact served to Chrome Extension
  data/cluster_report.json → arc stats for inspection

Usage:
    python3 scripts/cluster.py
    python3 scripts/cluster.py --max-gap 2 --min-arc 3 --verbose
"""

import argparse
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ──────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────

DATA_DIR          = Path(__file__).resolve().parent.parent / "data"
DEFAULT_INPUT     = DATA_DIR / "canonical_episodes.jsonl"
ARCS_OUTPUT       = DATA_DIR / "arcs.json"
CLUSTER_REPORT    = DATA_DIR / "cluster_report.json"


# ──────────────────────────────────────────────────────────
# Clustering Parameters (tune here)
# ──────────────────────────────────────────────────────────

# Episodes with a number-gap LARGER than this trigger a new arc
DEFAULT_MAX_GAP = 1       # gap of 1 = consecutive; allows 1 missing ep

# Arcs smaller than this get merged into adjacent arc or "Misc"
DEFAULT_MIN_ARC = 3

# Max episodes in a single arc before forcing a split regardless of gaps/keywords.
# TMKOC story arcs typically run 5–30 episodes. 40 is a generous ceiling.
DEFAULT_MAX_ARC_LENGTH = 40

# Title keywords that signal a new story arc is beginning.
# Checked against the INCOMING episode's title (lowercased).
# Only triggers a split if the keyword persists for 2+ consecutive eps
# to avoid false positives from a single mention.
ARC_SIGNAL_KEYWORDS: list[str] = [
    # Recurring story themes seen in real data
    "bhootni", "bhootni",
    "cricket", "match", "batting", "balling", "fielding",
    "rishte", "shaadi", "wedding", "marriage",
    "dubai", "goa", "shimla",        # travel arcs
    "nayi kahani", "new story",
    "part 1", "part1",
    # Hindi signals
    "शुरू", "नई कहानी",
]


# ──────────────────────────────────────────────────────────
# Data Models
# ──────────────────────────────────────────────────────────

@dataclass
class EpisodeNode:
    video_id:         str
    episode_number:   int
    season_number:    Optional[int]
    series_tag:       str
    title:            str
    title_clean:      str
    thumbnail_url:    str
    duration_seconds: int
    view_count:       int
    published_at:     str
    # Linked-list pointers — filled during linking step
    prev_video_id:    Optional[str] = None
    next_video_id:    Optional[str] = None


@dataclass
class Arc:
    arc_id:         str                       # "arc_001", "arc_002", ...
    arc_title:      str                       # auto-generated placeholder
    episodes:       list[EpisodeNode] = field(default_factory=list)
    possibly_incomplete: bool = False         # True if large gaps within arc


# ──────────────────────────────────────────────────────────
# Arc title generator (auto-placeholder until manual curation)
# ──────────────────────────────────────────────────────────

def generate_arc_title(episodes: list[EpisodeNode]) -> str:
    """
    Generate a placeholder arc title from recurring title keywords.
    Falls back to "Ep {start}–{end}" if nothing distinctive found.
    """
    # Named theme keywords to look for (order = priority)
    THEMES = [
        ("Bhootni",   ["bhootni"]),
        ("Cricket",   ["cricket", "match", "balling", "batting", "fielding", "runout"]),
        ("Sodhi",     ["sodhi", "party"]),
        ("Popatlal",  ["popatlal", "rishte", "shaadi"]),
        ("Sundar",    ["sundar"]),
        ("Daya",      ["daya", "behosh"]),
        ("Bapuji",    ["bapuji", "champaklal"]),
        ("Dubai",     ["dubai"]),
        ("Goa",       ["goa"]),
    ]

    all_titles = " ".join(ep.title_clean.lower() for ep in episodes)

    for theme_name, keywords in THEMES:
        count = sum(1 for ep in episodes
                    if any(kw in ep.title_clean.lower() for kw in keywords))
        # Theme must appear in at least 30% of episodes to name the arc
        if count >= max(2, len(episodes) * 0.30):
            ep_range = f"Ep {episodes[0].episode_number}–{episodes[-1].episode_number}"
            return f"{theme_name} Arc ({ep_range})"

    # Generic fallback
    return f"Ep {episodes[0].episode_number}–{episodes[-1].episode_number}"


# ──────────────────────────────────────────────────────────
# Step 1: Cluster episodes into arcs
# ──────────────────────────────────────────────────────────

def cluster_episodes(
    episodes:   list[EpisodeNode],
    max_gap:        int,
    min_arc:        int,
    max_arc_length: int,
    verbose:        bool = False,
) -> list[Arc]:
    """
    Group sorted episodes into arcs using two signals:
      1. Episode-number gap > max_gap → hard boundary
      2. ARC_SIGNAL_KEYWORDS appearing 2+ consecutive times → soft boundary
    """
    if not episodes:
        return []

    arcs: list[Arc] = []
    current_bucket: list[EpisodeNode] = [episodes[0]]
    # Track keyword streak for soft-boundary detection
    kw_streak: dict[str, int] = {}   # keyword → consecutive-episode count

    def flush(bucket: list[EpisodeNode]) -> Optional[Arc]:
        if not bucket:
            return None
        arc_idx = len(arcs) + 1
        return Arc(
            arc_id=f"arc_{arc_idx:03d}",
            arc_title=generate_arc_title(bucket),
            episodes=list(bucket),
        )

    for i in range(1, len(episodes)):
        prev_ep = episodes[i - 1]
        curr_ep = episodes[i]

        gap = curr_ep.episode_number - prev_ep.episode_number

        # ── Signal 1: Hard episode-number gap ──
        if gap > max_gap + 1:
            arc = flush(current_bucket)
            if arc:
                arcs.append(arc)
                if verbose:
                    print(f"  ✂️  Arc boundary (ep gap={gap}): "
                          f"Ep {prev_ep.episode_number} → Ep {curr_ep.episode_number}")
            current_bucket = [curr_ep]
            kw_streak = {}
            continue

        # ── Signal 2: Max arc length cap ──
        if len(current_bucket) >= max_arc_length:
            arc = flush(current_bucket)
            if arc:
                arcs.append(arc)
                if verbose:
                    print(f"  ✂️  Arc boundary (max length={max_arc_length}): forced split at Ep {curr_ep.episode_number}")
            current_bucket = [curr_ep]
            kw_streak = {}
            continue

        # ── Signal 3: Title keyword streak (soft boundary) ──
        title_lower = curr_ep.title_clean.lower()
        matched_kws = [kw for kw in ARC_SIGNAL_KEYWORDS if kw in title_lower]

        # Update streaks
        new_streak: dict[str, int] = {}
        for kw in matched_kws:
            new_streak[kw] = kw_streak.get(kw, 0) + 1
        kw_streak = new_streak

        # If any keyword just hit a streak of exactly 2, we split BEFORE
        # the first occurrence of that keyword (i.e., 2 episodes ago)
        triggered_kw = next(
            (kw for kw, cnt in kw_streak.items() if cnt == 2), None
        )

        if triggered_kw and len(current_bucket) >= min_arc + 1:
            # Split: the episode that started the streak becomes the new arc's first ep
            split_ep = current_bucket[-1]   # the one that first had the keyword
            pre_bucket = current_bucket[:-1]

            arc = flush(pre_bucket)
            if arc:
                arcs.append(arc)
                if verbose:
                    print(f"  ✂️  Arc boundary (keyword '{triggered_kw}'): "
                          f"Ep {pre_bucket[-1].episode_number} | "
                          f"new arc starts Ep {split_ep.episode_number}")
            current_bucket = [split_ep, curr_ep]
            kw_streak = {triggered_kw: 2}
            continue

        current_bucket.append(curr_ep)

    # Flush last bucket
    arc = flush(current_bucket)
    if arc:
        arcs.append(arc)

    # ── Merge tiny arcs ──
    arcs = merge_small_arcs(arcs, min_arc, verbose)

    return arcs


def merge_small_arcs(arcs: list[Arc], min_arc: int, verbose: bool) -> list[Arc]:
    """
    Arcs smaller than min_arc episodes are merged into their nearest neighbor,
    BUT ONLY if the episode number gap between them is reasonably small (<= 5).
    Otherwise, keeping the isolated arc prevents accidental leaps across thousands of episodes!
    """
    changed = True
    while changed:
        changed = False
        merged: list[Arc] = []
        i = 0
        while i < len(arcs):
            arc = arcs[i]
            if len(arc.episodes) < min_arc:
                merged_flag = False
                # Try merging into previous arc if gap is small
                if merged:
                    prev_end = merged[-1].episodes[-1].episode_number
                    curr_start = arc.episodes[0].episode_number
                    if curr_start - prev_end <= 5:
                        if verbose:
                            print(f"  🔗 Merging small arc ({len(arc.episodes)} eps: "
                                  f"Ep {arc.episodes[0].episode_number}–"
                                  f"{arc.episodes[-1].episode_number}) into previous arc")
                        merged[-1].episodes.extend(arc.episodes)
                        merged[-1].arc_title = generate_arc_title(merged[-1].episodes)
                        merged_flag = True
                        changed = True

                # Try merging into next arc if previous wasn't possible
                if not merged_flag and i + 1 < len(arcs):
                    curr_end = arc.episodes[-1].episode_number
                    next_start = arcs[i + 1].episodes[0].episode_number
                    if next_start - curr_end <= 5:
                        if verbose:
                            print(f"  🔗 Merging small arc ({len(arc.episodes)} eps) into next arc")
                        arcs[i + 1].episodes = arc.episodes + arcs[i + 1].episodes
                        arcs[i + 1].arc_title = generate_arc_title(arcs[i + 1].episodes)
                        merged_flag = True
                        changed = True

                if not merged_flag:
                    # Isolated small arc / gap is too wide to merge cleanly, keep it intact
                    arc.possibly_incomplete = True
                    merged.append(arc)
            else:
                merged.append(arc)
            i += 1
        arcs = merged

    # Renumber arc IDs
    for idx, arc in enumerate(arcs):
        arc.arc_id = f"arc_{idx + 1:03d}"

    return arcs


# ──────────────────────────────────────────────────────────
# Step 2: Build doubly-linked list within each arc
# ──────────────────────────────────────────────────────────

def build_linked_lists(arcs: list[Arc]) -> list[Arc]:
    """
    For each arc, set prev_video_id / next_video_id on every EpisodeNode.
    First episode: prev = None. Last episode: next = None.
    """
    for arc in arcs:
        eps = arc.episodes
        for i, ep in enumerate(eps):
            ep.prev_video_id = eps[i - 1].video_id if i > 0 else None
            ep.next_video_id = eps[i + 1].video_id if i < len(eps) - 1 else None
    return arcs


# ──────────────────────────────────────────────────────────
# Step 3: Serialise to arcs.json
# ──────────────────────────────────────────────────────────

def build_arcs_json(arcs: list[Arc]) -> dict:
    """
    Build the final arcs.json payload:
      - meta
      - arcs[]  with episodes[] (linked list nodes)
      - video_index  for O(1) lookup by video_id
    """
    now = datetime.now(timezone.utc).isoformat()
    total_episodes = sum(len(arc.episodes) for arc in arcs)

    arcs_list = []
    video_index: dict[str, dict] = {}

    for arc in arcs:
        eps = arc.episodes
        arc_dict = {
            "arc_id":             arc.arc_id,
            "arc_title":          arc.arc_title,
            "possibly_incomplete": arc.possibly_incomplete,
            "episode_count":      len(eps),
            "start_episode":      eps[0].episode_number,
            "end_episode":        eps[-1].episode_number,
            "episodes": []
        }

        for position, ep in enumerate(eps):
            ep_dict = {
                "video_id":         ep.video_id,
                "episode_number":   ep.episode_number,
                "season_number":    ep.season_number,
                "series_tag":       ep.series_tag,
                "title":            ep.title,
                "title_clean":      ep.title_clean,
                "thumbnail_url":    ep.thumbnail_url,
                "duration_seconds": ep.duration_seconds,
                "view_count":       ep.view_count,
                "prev_video_id":    ep.prev_video_id,
                "next_video_id":    ep.next_video_id,
            }
            arc_dict["episodes"].append(ep_dict)

            # Populate O(1) lookup index
            video_index[ep.video_id] = {
                "arc_id":   arc.arc_id,
                "position": position,
            }

        arcs_list.append(arc_dict)

    return {
        "meta": {
            "version":          2,
            "generated_at":     now,
            "total_episodes":   total_episodes,
            "total_arcs":       len(arcs),
            "schema_version":   "2.0.0",
        },
        "arcs":        arcs_list,
        "video_index": video_index,
    }


# ──────────────────────────────────────────────────────────
# Report
# ──────────────────────────────────────────────────────────

def build_cluster_report(arcs: list[Arc]) -> dict:
    arc_sizes = [len(arc.episodes) for arc in arcs]
    return {
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "total_arcs":     len(arcs),
        "total_episodes": sum(arc_sizes),
        "arc_size_stats": {
            "min":    min(arc_sizes) if arc_sizes else 0,
            "max":    max(arc_sizes) if arc_sizes else 0,
            "avg":    round(sum(arc_sizes) / len(arc_sizes), 1) if arc_sizes else 0,
        },
        "arcs": [
            {
                "arc_id":            arc.arc_id,
                "arc_title":         arc.arc_title,
                "episode_count":     len(arc.episodes),
                "start_episode":     arc.episodes[0].episode_number,
                "end_episode":       arc.episodes[-1].episode_number,
                "possibly_incomplete": arc.possibly_incomplete,
            }
            for arc in arcs
        ]
    }


# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="TMKOC Arc Clustering & Linker")
    parser.add_argument("--input",    default=str(DEFAULT_INPUT),  help="canonical_episodes.jsonl path")
    parser.add_argument("--max-gap",  type=int, default=DEFAULT_MAX_GAP, help="Max episode-number gap within an arc")
    parser.add_argument("--min-arc",  type=int, default=DEFAULT_MIN_ARC, help="Min episodes to form a standalone arc")
    parser.add_argument("--max-arc",  type=int, default=DEFAULT_MAX_ARC_LENGTH, help="Max episodes per arc before forcing a split")
    parser.add_argument("--verbose",  action="store_true",  help="Print arc boundary decisions")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: {input_path} not found. Run transform.py first.")
        raise SystemExit(1)

    # ── Load ──
    with open(input_path) as f:
        raw = [json.loads(l) for l in f if l.strip()]

    episodes = [
        EpisodeNode(
            video_id         = r["video_id"],
            episode_number   = r["episode_number"],
            season_number    = r.get("season_number"),
            series_tag       = r["series_tag"],
            title            = r["title"],
            title_clean      = r["title_clean"],
            thumbnail_url    = r.get("thumbnail_url", ""),
            duration_seconds = r.get("duration_seconds", 0),
            view_count       = r.get("view_count", 0),
            published_at     = r.get("published_at", ""),
        )
        for r in raw
    ]
    print(f"📂 Loaded {len(episodes)} canonical episodes")
    print(f"   Ep range: {episodes[0].episode_number} → {episodes[-1].episode_number}")

    # ── Cluster ──
    print(f"\n{'='*60}")
    print(f"🔍 Clustering (max_gap={args.max_gap}, min_arc={args.min_arc})...")
    print(f"{'='*60}\n")

    arcs = cluster_episodes(episodes, args.max_gap, args.min_arc, args.max_arc, args.verbose)

    # ── Link ──
    arcs = build_linked_lists(arcs)

    # ── Print arc table ──
    print(f"\n{'Arc ID':<10} {'Episodes':>8}  {'Ep Range':<15}  Title")
    print("─" * 70)
    for arc in arcs:
        ep_range = f"{arc.episodes[0].episode_number}–{arc.episodes[-1].episode_number}"
        incomplete = " ⚠️" if arc.possibly_incomplete else ""
        print(f"  {arc.arc_id:<8} {len(arc.episodes):>8}  {ep_range:<15}  {arc.arc_title}{incomplete}")
    print("─" * 70)
    print(f"  {'TOTAL':<8} {sum(len(a.episodes) for a in arcs):>8}  across {len(arcs)} arcs")

    # ── Verify linked list integrity ──
    errors = []
    for arc in arcs:
        eps = arc.episodes
        for i, ep in enumerate(eps):
            expected_prev = eps[i-1].video_id if i > 0 else None
            expected_next = eps[i+1].video_id if i < len(eps)-1 else None
            if ep.prev_video_id != expected_prev:
                errors.append(f"{arc.arc_id} pos {i}: bad prev_video_id")
            if ep.next_video_id != expected_next:
                errors.append(f"{arc.arc_id} pos {i}: bad next_video_id")

    if errors:
        print(f"\n❌ Linked-list integrity errors:")
        for e in errors:
            print(f"   {e}")
    else:
        print(f"\n✅ Linked-list integrity: OK (all prev/next pointers verified)")

    # ── Write outputs ──
    arcs_json = build_arcs_json(arcs)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with open(ARCS_OUTPUT, "w") as f:
        json.dump(arcs_json, f, ensure_ascii=False, indent=2)

    report = build_cluster_report(arcs)
    CLUSTER_REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    import os
    size_kb = os.path.getsize(ARCS_OUTPUT) / 1024
    print(f"\n✅ arcs.json          → {size_kb:.1f} KB  ({len(arcs)} arcs, {len(arcs_json['video_index'])} indexed videos)")
    print(f"📋 cluster_report.json → written")


if __name__ == "__main__":
    main()
