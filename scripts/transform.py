#!/usr/bin/env python3
"""
TMKOC Transform Pipeline
=========================
Parses raw video metadata into structured episode records.

- Extracts episode numbers from titles using a regex cascade
- Cleans boilerplate from titles
- Tags each video with a series_tag and is_canonical flag
- Outputs parsed_episodes.jsonl (all), canonical_episodes.jsonl (sorted, arc-ready)
- Writes a parse_report.json with match stats and any failures

Usage:
    python3 scripts/transform.py
    python3 scripts/transform.py --input data/raw_videos.jsonl
    python3 scripts/transform.py --input data/raw_videos.jsonl --verbose
"""

import argparse
import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ──────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────

DATA_DIR               = Path(__file__).resolve().parent.parent / "data"
DEFAULT_INPUT          = DATA_DIR / "raw_videos.jsonl"
PARSED_OUTPUT          = DATA_DIR / "parsed_episodes.jsonl"
CANONICAL_OUTPUT       = DATA_DIR / "canonical_episodes.jsonl"
PARSE_REPORT           = DATA_DIR / "parse_report.json"


# ──────────────────────────────────────────────────────────
# Regex Patterns  (applied in priority order)
# ──────────────────────────────────────────────────────────

# Format A: "SE 01 EP 440 - ..."
# Captures season + episode number
RE_SE_EP = re.compile(
    r'SE\s*0*(\d+)\s+EP\s*0*(\d+)',
    re.IGNORECASE
)

# Format B: "NEW! Episode 4849 - ..." OR "NEW Episode 4837" (no !) OR "NEW! Ep 4819"
RE_NEW_EPISODE = re.compile(
    r'NEW!?\s*Ep(?:isode)?\s+0*(\d+)',
    re.IGNORECASE
)

# Format C: "FULL EPISODE 4830 - ..."
RE_FULL_EPISODE = re.compile(
    r'FULL\s+EPISODE!?\s*0*(\d+)',
    re.IGNORECASE
)

# Format E: "NEW! Ep 4819 - ..." standalone Ep number (already covered by B above)

# Format F: Bare episode number patterns used by older/alternate uploads
# e.g. "Episode 1234 |", "Ep. 1234 -", "Ep 1234:"
RE_BARE_EP = re.compile(
    r'\bEp(?:isode)?\.?\s*[-–#.:]?\s*0*(\d{1,4})\b',
    re.IGNORECASE
)

# Format D: Haste Raho clips (no episode number — presence check only)
RE_HASTE_RAHO = re.compile(
    r'Haste\s+Raho\s+Hasate\s+Raho',
    re.IGNORECASE
)

# Titles to explicitly exclude — not real episodes
RE_EXCLUDE = re.compile(
    r'Guess\s+The\s+Episode|Weekly\s+Rewind|Janmashtami\s+Special|Comedy\s+Scenes|Trip\s+To|'
    r'Asian\s+Paints|CoinSwitch|Citro[eë]n|Ather\s+Rizta|Medimix|Seekho\s+App|Unfiltered|'
    r'Years\s+Completed|Years\s+of\s+TMKOC|Special\s+Song|Proud\s+Moment|Iconic.*Moments|Insane\s+Moments',
    re.IGNORECASE
)


# ──────────────────────────────────────────────────────────
# Title Cleaning
# ──────────────────────────────────────────────────────────

# Boilerplate suffixes to strip (ordered: most specific first)
_SUFFIX_PATTERNS = [
    re.compile(r'\|?\s*Haste\s+Raho\s+Hasate\s+Raho.*$',    re.IGNORECASE),
    re.compile(r'\|?\s*Taarak\s+Mehta\s+Ka\s+O+ltah.*$',    re.IGNORECASE),
    re.compile(r'\|?\s*Taarak\s+Mehta\s+ka\s+Ooltah.*$',    re.IGNORECASE),
    re.compile(r'\|?\s*Taarak\s+Mehta\s+Ka\s+oltah.*$',     re.IGNORECASE),
    re.compile(r'\|?\s*Taarak\s+Mehta.*$',                  re.IGNORECASE),
]

# Prefixes to strip (after extracting the episode number)
_PREFIX_PATTERNS = [
    re.compile(r'^SE\s*\d+\s+EP\s*\d+\s*[-–]\s*',          re.IGNORECASE),
    re.compile(r'^NEW!\s*Episode\s+\d+\s*[-–]\s*',          re.IGNORECASE),
    re.compile(r'^FULL\s+EPISODE\s+\d+\s*[-–]\s*',          re.IGNORECASE),
]


def clean_title(raw_title: str) -> str:
    """Strip boilerplate prefixes and suffixes, normalize whitespace."""
    t = raw_title.strip()

    # Strip trailing boilerplate
    for pat in _SUFFIX_PATTERNS:
        t = pat.sub('', t).strip()

    # Strip leading episode prefix
    for pat in _PREFIX_PATTERNS:
        t = pat.sub('', t).strip()

    # Normalize multiple spaces / trailing punctuation
    t = re.sub(r'\s+', ' ', t)
    t = re.sub(r'\s+[|!]\s*$', '', t).strip()

    return t


# ──────────────────────────────────────────────────────────
# Data Model
# ──────────────────────────────────────────────────────────

@dataclass
class ParsedEpisode:
    # Identity
    video_id:              str
    title:                 str
    title_clean:           str

    # Episode numbering
    episode_number:        Optional[int]    # None for Haste Raho
    season_number:         Optional[int]    # Only for SE_EP format
    episode_number_source: str
    #   "se_ep_regex"        → Format A
    #   "new_episode_regex"  → Format B
    #   "full_episode_regex" → Format C
    #   "haste_raho"         → Format D (no number)
    #   "unresolved"         → matched nothing (shouldn't happen)

    # Series classification
    series_tag:   str    # "se_ep" | "new_episode" | "full_episode" | "haste_raho" | "unknown"
    is_canonical: bool   # True only if episode_number is known

    # Media & stats (pass-through)
    thumbnail_url:    str
    duration_seconds: int
    view_count:       int
    published_at:     str   # upload date — for reference only

    # ETL
    _content_type: str
    _ingested_at:  str
    _parsed_at:    str


# ──────────────────────────────────────────────────────────
# Core Parser
# ──────────────────────────────────────────────────────────

def parse_episode(raw: dict) -> ParsedEpisode:
    """Parse a single raw video record into a ParsedEpisode."""
    title = raw.get("title", "")

    # ── Explicit exclusions (not real episodes) ──
    if RE_EXCLUDE.search(title):
        return ParsedEpisode(
            video_id              = raw["video_id"],
            title                 = title,
            title_clean           = clean_title(title),
            episode_number        = None,
            season_number         = None,
            episode_number_source = "excluded",
            series_tag            = "excluded",
            is_canonical          = False,
            thumbnail_url         = raw.get("thumbnail_url", ""),
            duration_seconds      = raw.get("duration_seconds", 0),
            view_count            = raw.get("view_count", 0),
            published_at          = raw.get("published_at", ""),
            _content_type         = raw.get("_content_type", "episode"),
            _ingested_at          = raw.get("_ingested_at", ""),
            _parsed_at            = datetime.now(timezone.utc).isoformat(),
        )

    # ── Priority 1: SE 01 EP xxx ──
    m = RE_SE_EP.search(title)
    if m:
        return ParsedEpisode(
            video_id              = raw["video_id"],
            title                 = title,
            title_clean           = clean_title(title),
            episode_number        = int(m.group(2)),
            season_number         = int(m.group(1)),
            episode_number_source = "se_ep_regex",
            series_tag            = "se_ep",
            is_canonical          = True,
            thumbnail_url         = raw.get("thumbnail_url", ""),
            duration_seconds      = raw.get("duration_seconds", 0),
            view_count            = raw.get("view_count", 0),
            published_at          = raw.get("published_at", ""),
            _content_type         = raw.get("_content_type", "episode"),
            _ingested_at          = raw.get("_ingested_at", ""),
            _parsed_at            = datetime.now(timezone.utc).isoformat(),
        )

    # ── Priority 2: NEW!? Episode/Ep xxxx ──
    m = RE_NEW_EPISODE.search(title)
    if m:
        return ParsedEpisode(
            video_id              = raw["video_id"],
            title                 = title,
            title_clean           = clean_title(title),
            episode_number        = int(m.group(1)),
            season_number         = None,
            episode_number_source = "new_episode_regex",
            series_tag            = "new_episode",
            is_canonical          = True,
            thumbnail_url         = raw.get("thumbnail_url", ""),
            duration_seconds      = raw.get("duration_seconds", 0),
            view_count            = raw.get("view_count", 0),
            published_at          = raw.get("published_at", ""),
            _content_type         = raw.get("_content_type", "episode"),
            _ingested_at          = raw.get("_ingested_at", ""),
            _parsed_at            = datetime.now(timezone.utc).isoformat(),
        )

    # ── Priority 3: FULL EPISODE xxxx ──
    m = RE_FULL_EPISODE.search(title)
    if m:
        return ParsedEpisode(
            video_id              = raw["video_id"],
            title                 = title,
            title_clean           = clean_title(title),
            episode_number        = int(m.group(1)),
            season_number         = None,
            episode_number_source = "full_episode_regex",
            series_tag            = "full_episode",
            is_canonical          = True,
            thumbnail_url         = raw.get("thumbnail_url", ""),
            duration_seconds      = raw.get("duration_seconds", 0),
            view_count            = raw.get("view_count", 0),
            published_at          = raw.get("published_at", ""),
            _content_type         = raw.get("_content_type", "episode"),
            _ingested_at          = raw.get("_ingested_at", ""),
            _parsed_at            = datetime.now(timezone.utc).isoformat(),
        )

    # ── Priority 4: Bare Ep/Episode number (fallback pattern) ──
    m = RE_BARE_EP.search(title)
    if m:
        ep_num = int(m.group(1))
        # Sanity check: reject if looks like a year
        if not (1990 <= ep_num <= 2030):
            return ParsedEpisode(
                video_id              = raw["video_id"],
                title                 = title,
                title_clean           = clean_title(title),
                episode_number        = ep_num,
                season_number         = None,
                episode_number_source = "bare_ep_regex",
                series_tag            = "bare_ep",
                is_canonical          = True,
                thumbnail_url         = raw.get("thumbnail_url", ""),
                duration_seconds      = raw.get("duration_seconds", 0),
                view_count            = raw.get("view_count", 0),
                published_at          = raw.get("published_at", ""),
                _content_type         = raw.get("_content_type", "episode"),
                _ingested_at          = raw.get("_ingested_at", ""),
                _parsed_at            = datetime.now(timezone.utc).isoformat(),
            )

    # ── Priority 5: Haste Raho clip (no episode number) ──
    if RE_HASTE_RAHO.search(title):
        return ParsedEpisode(
            video_id              = raw["video_id"],
            title                 = title,
            title_clean           = clean_title(title),
            episode_number        = None,
            season_number         = None,
            episode_number_source = "haste_raho",
            series_tag            = "haste_raho",
            is_canonical          = False,
            thumbnail_url         = raw.get("thumbnail_url", ""),
            duration_seconds      = raw.get("duration_seconds", 0),
            view_count            = raw.get("view_count", 0),
            published_at          = raw.get("published_at", ""),
            _content_type         = raw.get("_content_type", "episode"),
            _ingested_at          = raw.get("_ingested_at", ""),
            _parsed_at            = datetime.now(timezone.utc).isoformat(),
        )

    # ── Fallback: Unresolved ──
    return ParsedEpisode(
        video_id              = raw["video_id"],
        title                 = title,
        title_clean           = clean_title(title),
        episode_number        = None,
        season_number         = None,
        episode_number_source = "unresolved",
        series_tag            = "unknown",
        is_canonical          = False,
        thumbnail_url         = raw.get("thumbnail_url", ""),
        duration_seconds      = raw.get("duration_seconds", 0),
        view_count            = raw.get("view_count", 0),
        published_at          = raw.get("published_at", ""),
        _content_type         = raw.get("_content_type", "episode"),
        _ingested_at          = raw.get("_ingested_at", ""),
        _parsed_at            = datetime.now(timezone.utc).isoformat(),
    )


# ──────────────────────────────────────────────────────────
# Dedup: same episode_number → keep highest view_count
# ──────────────────────────────────────────────────────────

def dedup_canonical(episodes: list[ParsedEpisode]) -> list[ParsedEpisode]:
    """
    If the same episode_number appears more than once (re-uploads),
    keep the record with the highest view_count as the canonical one.
    Returns a deduplicated list.
    """
    seen: dict[int, ParsedEpisode] = {}
    dupes = []

    for ep in episodes:
        existing = seen.get(ep.episode_number)
        if existing is None:
            seen[ep.episode_number] = ep
        else:
            dupes.append((ep.episode_number, ep.video_id, existing.video_id))
            if ep.view_count > existing.view_count:
                seen[ep.episode_number] = ep  # replace with higher-viewed

    return list(seen.values()), dupes


# ──────────────────────────────────────────────────────────
# Report
# ──────────────────────────────────────────────────────────

def build_report(
    all_parsed:  list[ParsedEpisode],
    canonical:   list[ParsedEpisode],
    dupes:       list,
    unresolved:  list[ParsedEpisode],
) -> dict:
    source_counts = {}
    for ep in all_parsed:
        source_counts[ep.episode_number_source] = source_counts.get(ep.episode_number_source, 0) + 1

    return {
        "generated_at":      datetime.now(timezone.utc).isoformat(),
        "total_parsed":      len(all_parsed),
        "total_canonical":   len(canonical),
        "total_haste_raho":  source_counts.get("haste_raho", 0),
        "total_unresolved":  len(unresolved),
        "match_breakdown":   source_counts,
        "episode_range": {
            "min": min(ep.episode_number for ep in canonical) if canonical else None,
            "max": max(ep.episode_number for ep in canonical) if canonical else None,
        },
        "duplicate_episode_numbers": [
            {"episode_number": ep_num, "kept_video": kept, "dropped_video": dropped}
            for ep_num, dropped, kept in dupes
        ],
        "unresolved_titles": [
            {"video_id": ep.video_id, "title": ep.title}
            for ep in unresolved
        ],
    }


# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="TMKOC Transform Pipeline")
    parser.add_argument("--input",   default=str(DEFAULT_INPUT),   help="Input JSONL path")
    parser.add_argument("--verbose", action="store_true",          help="Print each parsed record")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}")
        print("Run extract.py first.")
        raise SystemExit(1)

    # ── Load raw records ──
    with open(input_path) as f:
        raw_records = [json.loads(line) for line in f if line.strip()]
    print(f"📂 Loaded {len(raw_records)} raw records from {input_path.name}")

    # ── Parse ──
    print(f"\n{'='*60}")
    print(f"🔍 Parsing episode numbers from titles...")
    print(f"{'='*60}\n")

    all_parsed = [parse_episode(r) for r in raw_records]

    # ── Partition ──
    canonical_raw = [ep for ep in all_parsed if ep.is_canonical]
    haste_raho    = [ep for ep in all_parsed if ep.series_tag == "haste_raho"]
    unresolved    = [ep for ep in all_parsed if ep.episode_number_source == "unresolved"]

    # ── Dedup canonical ──
    canonical_deduped, dupes = dedup_canonical(canonical_raw)

    # ── Sort canonical by episode number ──
    canonical_sorted = sorted(canonical_deduped, key=lambda ep: ep.episode_number)

    # ── Print summary ──
    source_counts = {}
    for ep in all_parsed:
        source_counts[ep.episode_number_source] = source_counts.get(ep.episode_number_source, 0) + 1

    print(f"{'Source':<25} {'Count':>6}")
    print(f"{'─'*32}")
    for source, count in sorted(source_counts.items(), key=lambda x: -x[1]):
        tag = " ✅" if source not in ("haste_raho", "unresolved") else " ⏸️ " if source == "haste_raho" else " ❌"
        print(f"  {source:<23} {count:>5}{tag}")
    print(f"{'─'*32}")
    print(f"  {'CANONICAL (arc-ready)':<23} {len(canonical_sorted):>5} ✅")
    print(f"  {'Haste Raho (no ep#)':<23} {len(haste_raho):>5} ⏸️ ")
    print(f"  {'Unresolved':<23} {len(unresolved):>5} ❌")

    if dupes:
        print(f"\n⚠️  Duplicate episode numbers found: {len(dupes)}")
        for ep_num, dropped, kept in dupes:
            print(f"   Ep {ep_num}: kept {kept}, dropped {dropped}")

    if unresolved:
        print(f"\n❌ UNRESOLVED TITLES ({len(unresolved)}) — needs manual review:")
        for ep in unresolved:
            print(f"   [{ep.video_id}] {ep.title}")

    # ── Verbose mode: print all canonical ──
    if args.verbose:
        print(f"\n{'='*60}")
        print(f"📋 Canonical episodes (sorted by episode number):")
        print(f"{'='*60}")
        for ep in canonical_sorted:
            season = f"S{ep.season_number:02d}" if ep.season_number else "   "
            print(f"  {season} E{ep.episode_number:04d} | {ep.series_tag:<15} | {ep.title_clean[:55]}")

    # ── Write outputs ──
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with open(PARSED_OUTPUT, "w") as f:
        for ep in all_parsed:
            f.write(json.dumps(asdict(ep), ensure_ascii=False) + "\n")
    print(f"\n✅ parsed_episodes.jsonl   → {len(all_parsed)} records")

    with open(CANONICAL_OUTPUT, "w") as f:
        for ep in canonical_sorted:
            f.write(json.dumps(asdict(ep), ensure_ascii=False) + "\n")
    print(f"✅ canonical_episodes.jsonl → {len(canonical_sorted)} records (sorted by ep#)")

    report = build_report(all_parsed, canonical_sorted, dupes, unresolved)
    PARSE_REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"📋 parse_report.json       → written")

    # ── Quick peek at canonical range ──
    if canonical_sorted:
        first, last = canonical_sorted[0], canonical_sorted[-1]
        print(f"\n📊 Canonical episode range:")
        print(f"   First: Ep {first.episode_number:,}  →  {first.title_clean[:60]}")
        print(f"   Last:  Ep {last.episode_number:,}  →  {last.title_clean[:60]}")


if __name__ == "__main__":
    main()
