#!/usr/bin/env python3
"""
TMKOC Arc Clustering & Linked-List Builder (Ground-Truth Enabled)
================================================================
Maps canonical episodes directly to ground-truth story arcs (from storylines.json).
If ground truth is available:
  - Groups episodes into their official story arcs with authentic titles and categories.
  - Any new episodes beyond the ground truth (e.g. Ep > 4824) are clustered using
    heuristic fallback (max-arc cap & gap detection).
If ground truth is not provided:
  - Uses purely heuristic clustering (gap detection + title keywords + max length cap).

Outputs:
  data/arcs.json           → final artifact served to Chrome Extension
  data/cluster_report.json → arc statistics for inspection

Usage:
    python3 scripts/cluster.py
    python3 scripts/cluster.py --storylines data/storylines.json --verbose
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

DATA_DIR           = Path(__file__).resolve().parent.parent / "data"
DEFAULT_INPUT      = DATA_DIR / "canonical_episodes.jsonl"
DEFAULT_STORYLINES = DATA_DIR / "storylines.json"
ARCS_OUTPUT        = DATA_DIR / "arcs.json"
CLUSTER_REPORT     = DATA_DIR / "cluster_report.json"


# ──────────────────────────────────────────────────────────
# Heuristic Clustering Parameters (fallback)
# ──────────────────────────────────────────────────────────

DEFAULT_MAX_GAP = 1
DEFAULT_MIN_ARC = 3
DEFAULT_MAX_ARC_LENGTH = 40

ARC_SIGNAL_KEYWORDS: list[str] = [
    "bhootni", "cricket", "match", "batting", "balling", "fielding",
    "rishte", "shaadi", "wedding", "marriage",
    "dubai", "goa", "shimla", "nayi kahani", "new story",
    "part 1", "part1", "शुरू", "नई कहानी",
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
    prev_video_id:    Optional[str] = None
    next_video_id:    Optional[str] = None


@dataclass
class Arc:
    arc_id:              str                  # e.g. "arc_001" or ground truth id
    arc_title:           str                  # human-friendly narrative title
    category:            str = "General"      # Classic, Golden, Modern, Recent
    tagline:             str = ""
    start_episode:       int = 0
    end_episode:         int = 0
    expected_total:      int = 0              # Total episodes in the narrative arc
    possibly_incomplete: bool = False
    episodes:            list[EpisodeNode] = field(default_factory=list)


# ──────────────────────────────────────────────────────────
# Ground-Truth Arc Matcher
# ──────────────────────────────────────────────────────────

def cluster_with_ground_truth(
    episodes: list[EpisodeNode],
    storylines: list[dict],
    verbose: bool = False,
) -> list[Arc]:
    """
    Cluster episodes using human-curated ground-truth storylines.
    Episodes within startEp..endEp are assigned to the respective arc.
    Any episodes exceeding ground-truth coverage are clustered using heuristic fallback.
    """
    storylines_sorted = sorted(storylines, key=lambda x: (x.get("startEp", 0), x.get("endEp", 0)))

    # Map each episode number to its ground-truth storyline
    ep_to_storyline = {}
    for st in storylines_sorted:
        s = st.get("startEp", 0)
        e = st.get("endEp", 0)
        for num in range(s, e + 1):
            if num not in ep_to_storyline:
                ep_to_storyline[num] = st

    # Group canonical episodes by storyline
    storyline_buckets: dict[str, list[EpisodeNode]] = {}
    storyline_meta: dict[str, dict] = {}
    fallback_episodes: list[EpisodeNode] = []

    for ep in episodes:
        st = ep_to_storyline.get(ep.episode_number)
        if st:
            st_id = st.get("id") or f"arc_gt_{st.get('startEp')}"
            if st_id not in storyline_buckets:
                storyline_buckets[st_id] = []
                storyline_meta[st_id] = st
            storyline_buckets[st_id].append(ep)
        else:
            fallback_episodes.append(ep)

    arcs: list[Arc] = []
    arc_counter = 1

    # Preserve chronological order of ground-truth storylines
    for st in storylines_sorted:
        st_id = st.get("id")
        if st_id in storyline_buckets:
            eps_in_arc = sorted(storyline_buckets[st_id], key=lambda x: x.episode_number)
            expected_total = st.get("totalEpisodes", len(eps_in_arc))
            is_incomplete = len(eps_in_arc) < expected_total

            arc = Arc(
                arc_id=f"arc_{arc_counter:03d}",
                arc_title=st.get("title", f"Ep {st.get('startEp')}–{st.get('endEp')}"),
                category=st.get("category", "General"),
                tagline=st.get("tagline", ""),
                start_episode=st.get("startEp", eps_in_arc[0].episode_number),
                end_episode=st.get("endEp", eps_in_arc[-1].episode_number),
                expected_total=expected_total,
                possibly_incomplete=is_incomplete,
                episodes=eps_in_arc,
            )
            arcs.append(arc)
            arc_counter += 1

    if verbose:
        print(f"  ✅ Matched {sum(len(a.episodes) for a in arcs)} episodes to {len(arcs)} ground-truth arcs")

    # Handle remaining fallback episodes (e.g. newer episodes not in dataset)
    if fallback_episodes:
        if verbose:
            print(f"  ⚡ Running heuristic clustering on {len(fallback_episodes)} newer episodes...")
        fallback_arcs = heuristic_cluster(
            fallback_episodes,
            max_gap=DEFAULT_MAX_GAP,
            min_arc=DEFAULT_MIN_ARC,
            max_arc_length=DEFAULT_MAX_ARC_LENGTH,
            start_arc_index=arc_counter,
            verbose=verbose,
        )
        for fa in fallback_arcs:
            fa.category = "Recent"
        arcs.extend(fallback_arcs)

    return arcs


# ──────────────────────────────────────────────────────────
# Heuristic Clustering Engine (Fallback)
# ──────────────────────────────────────────────────────────

def generate_arc_title(episodes: list[EpisodeNode]) -> str:
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
    for theme_name, keywords in THEMES:
        count = sum(1 for ep in episodes if any(kw in ep.title_clean.lower() for kw in keywords))
        if count >= max(2, len(episodes) * 0.30):
            ep_range = f"Ep {episodes[0].episode_number}–{episodes[-1].episode_number}"
            return f"{theme_name} Arc ({ep_range})"

    return f"Ep {episodes[0].episode_number}–{episodes[-1].episode_number}"


def heuristic_cluster(
    episodes: list[EpisodeNode],
    max_gap: int,
    min_arc: int,
    max_arc_length: int,
    start_arc_index: int = 1,
    verbose: bool = False,
) -> list[Arc]:
    if not episodes:
        return []

    episodes = sorted(episodes, key=lambda x: x.episode_number)
    arcs: list[Arc] = []
    current_bucket: list[EpisodeNode] = [episodes[0]]
    kw_streak: dict[str, int] = {}
    arc_counter = start_arc_index

    def flush(bucket: list[EpisodeNode]) -> Optional[Arc]:
        nonlocal arc_counter
        if not bucket:
            return None
        arc = Arc(
            arc_id=f"arc_{arc_counter:03d}",
            arc_title=generate_arc_title(bucket),
            start_episode=bucket[0].episode_number,
            end_episode=bucket[-1].episode_number,
            expected_total=len(bucket),
            episodes=list(bucket),
        )
        arc_counter += 1
        return arc

    for i in range(1, len(episodes)):
        prev_ep = episodes[i - 1]
        curr_ep = episodes[i]
        gap = curr_ep.episode_number - prev_ep.episode_number

        # Gap trigger
        if gap > max_gap + 1:
            arc = flush(current_bucket)
            if arc:
                arcs.append(arc)
            current_bucket = [curr_ep]
            kw_streak = {}
            continue

        # Max length trigger
        if len(current_bucket) >= max_arc_length:
            arc = flush(current_bucket)
            if arc:
                arcs.append(arc)
            current_bucket = [curr_ep]
            kw_streak = {}
            continue

        # Keyword trigger
        title_lower = curr_ep.title_clean.lower()
        matched_kws = [kw for kw in ARC_SIGNAL_KEYWORDS if kw in title_lower]
        new_streak = {kw: kw_streak.get(kw, 0) + 1 for kw in matched_kws}
        kw_streak = new_streak

        triggered_kw = next((kw for kw, cnt in kw_streak.items() if cnt == 2), None)
        if triggered_kw and len(current_bucket) >= min_arc + 1:
            split_ep = current_bucket[-1]
            pre_bucket = current_bucket[:-1]
            arc = flush(pre_bucket)
            if arc:
                arcs.append(arc)
            current_bucket = [split_ep, curr_ep]
            kw_streak = {triggered_kw: 2}
            continue

        current_bucket.append(curr_ep)

    arc = flush(current_bucket)
    if arc:
        arcs.append(arc)

    return arcs


# ──────────────────────────────────────────────────────────
# Step 2: Build Doubly-Linked Lists
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
# Step 3: Serialize to arcs.json
# ──────────────────────────────────────────────────────────

def build_arcs_json(arcs: list[Arc]) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    total_episodes = sum(len(arc.episodes) for arc in arcs)

    arcs_list = []
    video_index: dict[str, dict] = {}

    for arc in arcs:
        eps = arc.episodes
        arc_dict = {
            "arc_id":              arc.arc_id,
            "arc_title":           arc.arc_title,
            "category":            arc.category,
            "tagline":             arc.tagline,
            "start_episode":       arc.start_episode,
            "end_episode":         arc.end_episode,
            "episode_count":       len(eps),
            "expected_total":      arc.expected_total,
            "possibly_incomplete": arc.possibly_incomplete,
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
            "schema_version":   "2.1.0",
            "curation":         "ground_truth_wikipedia_and_guide",
        },
        "arcs":        arcs_list,
        "video_index": video_index,
    }


def build_cluster_report(arcs: list[Arc]) -> dict:
    arc_sizes = [len(arc.episodes) for arc in arcs]
    return {
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "total_arcs":     len(arcs),
        "total_episodes": sum(arc_sizes),
        "arc_size_stats": {
            "min": min(arc_sizes) if arc_sizes else 0,
            "max": max(arc_sizes) if arc_sizes else 0,
            "avg": round(sum(arc_sizes) / len(arc_sizes), 1) if arc_sizes else 0,
        },
        "arcs": [
            {
                "arc_id":              arc.arc_id,
                "arc_title":           arc.arc_title,
                "category":            arc.category,
                "episode_count":       len(arc.episodes),
                "expected_total":      arc.expected_total,
                "start_episode":       arc.start_episode,
                "end_episode":         arc.end_episode,
                "possibly_incomplete": arc.possibly_incomplete,
            }
            for arc in arcs
        ]
    }


# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="TMKOC Ground-Truth Arc Clustering & Linker")
    parser.add_argument("--input",      default=str(DEFAULT_INPUT),      help="canonical_episodes.jsonl path")
    parser.add_argument("--storylines", default=str(DEFAULT_STORYLINES), help="Path to storylines.json ground truth")
    parser.add_argument("--max-gap",    type=int, default=DEFAULT_MAX_GAP, help="Max ep gap for heuristic fallback")
    parser.add_argument("--min-arc",    type=int, default=DEFAULT_MIN_ARC, help="Min eps for standalone arc (heuristic)")
    parser.add_argument("--max-arc",    type=int, default=DEFAULT_MAX_ARC_LENGTH, help="Max eps per arc (heuristic)")
    parser.add_argument("--verbose",    action="store_true", help="Print clustering details")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: {input_path} not found. Run transform.py first.")
        raise SystemExit(1)

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

    # ── Check Ground Truth ──
    storylines_path = Path(args.storylines)
    storylines = None
    if storylines_path.exists():
        try:
            with open(storylines_path) as f:
                storylines = json.load(f)
            print(f"📖 Loaded {len(storylines)} ground-truth storylines from {storylines_path.name}")
        except Exception as e:
            print(f"⚠️ Failed reading storylines.json: {e}")

    print(f"\n{'='*60}")
    print(f"🔍 Clustering episodes into Story Arcs...")
    print(f"{'='*60}\n")

    if storylines:
        arcs = cluster_with_ground_truth(episodes, storylines, verbose=args.verbose)
    else:
        print("ℹ️ No ground truth available, running pure heuristic clustering...")
        arcs = heuristic_cluster(episodes, args.max_gap, args.min_arc, args.max_arc, verbose=args.verbose)

    # ── Build Doubly Linked List ──
    arcs = build_linked_lists(arcs)

    # ── Display Summary Table ──
    print(f"\n{'Arc ID':<9} {'Episodes':>8}  {'Ep Range':<15}  {'Category':<10} Title")
    print("─" * 80)
    for arc in arcs[:20]:
        ep_range = f"{arc.start_episode}–{arc.end_episode}"
        incomplete = " ⚠️" if arc.possibly_incomplete else ""
        print(f"  {arc.arc_id:<7} {len(arc.episodes):>8}  {ep_range:<15}  {arc.category:<10} {arc.arc_title[:32]}{incomplete}")
    if len(arcs) > 20:
        print(f"  ... and {len(arcs) - 20} more arcs ...")
        for arc in arcs[-5:]:
            ep_range = f"{arc.start_episode}–{arc.end_episode}"
            incomplete = " ⚠️" if arc.possibly_incomplete else ""
            print(f"  {arc.arc_id:<7} {len(arc.episodes):>8}  {ep_range:<15}  {arc.category:<10} {arc.arc_title[:32]}{incomplete}")
    print("─" * 80)
    print(f"  {'TOTAL':<7} {sum(len(a.episodes) for a in arcs):>8} episodes across {len(arcs)} arcs")

    # ── Integrity Check ──
    errors = []
    for arc in arcs:
        eps = arc.episodes
        for i, ep in enumerate(eps):
            expected_prev = eps[i-1].video_id if i > 0 else None
            expected_next = eps[i+1].video_id if i < len(eps)-1 else None
            if ep.prev_video_id != expected_prev:
                errors.append(f"{arc.arc_id} pos {i}: bad prev pointer")
            if ep.next_video_id != expected_next:
                errors.append(f"{arc.arc_id} pos {i}: bad next pointer")

    if errors:
        print(f"\n❌ Linked-list integrity errors: {len(errors)}")
    else:
        print(f"\n✅ Linked-list integrity: OK (all prev/next pointers verified)")

    # ── Write output files ──
    arcs_json = build_arcs_json(arcs)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with open(ARCS_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(arcs_json, f, ensure_ascii=False, indent=2)

    report = build_cluster_report(arcs)
    CLUSTER_REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    import os
    size_kb = os.path.getsize(ARCS_OUTPUT) / 1024
    print(f"\n✅ arcs.json          → {size_kb:.1f} KB  ({len(arcs)} arcs, {len(arcs_json['video_index'])} indexed videos)")
    print(f"📋 cluster_report.json → written")


if __name__ == "__main__":
    main()
