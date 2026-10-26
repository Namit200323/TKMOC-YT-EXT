#!/usr/bin/env python3
"""
TMKOC YouTube Metadata Extractor
=================================
Fetches all video metadata from the TMKOC YouTube channel using
the YouTube Data API v3 and writes raw JSONL + a summary.

Usage:
    # Set your API key
    export YOUTUBE_API_KEY="your-key-here"

    # Full fetch (all videos from the channel)
    python3 scripts/extract.py

    # Limit pages for quick testing
    python3 scripts/extract.py --max-pages 2

    # Custom channel ID (default is TMKOC's official channel)
    python3 scripts/extract.py --channel-id UCnSFZ-olBoLGLRUS_3RI2Aw

    # Resume from a previous interrupted run
    python3 scripts/extract.py --resume  
"""

import argparse
import json
import os
import sys
import time 
from datetime import datetime, timezone
from pathlib import Path

try:
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    print("ERROR: google-api-python-client not installed.")
    print("Run:  pip3 install google-api-python-client")
    sys.exit(1)


# ──────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────

# TMKOC official channel: "Taarak Mehta Ka Ooltah Chashmah"
DEFAULT_CHANNEL_ID = "UCnSFZ-olBoLGLRUS_3RI2Aw"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RAW_OUTPUT = DATA_DIR / "raw_videos.jsonl"
CHECKPOINT_FILE = DATA_DIR / ".extract_checkpoint.json"
SUMMARY_FILE = DATA_DIR / "extract_summary.json"

PLAYLIST_ITEMS_PER_PAGE = 50   # API max
VIDEO_DETAIL_BATCH_SIZE = 50   # API max for videos.list
API_RETRY_ATTEMPTS = 3
API_RETRY_DELAY_SECS = 5

# ──────────────────────────────────────────────────────────
# Pre-filter: title-only signals (no API call needed)
# These patterns are strong enough to classify without duration data.
# Avoids ~64% of videos.list enrichment calls.
# ──────────────────────────────────────────────────────────

# Titles with any of these → definitely a Short, skip enrichment entirely
TITLE_SHORT_SIGNALS = ["#shorts", "#short", "#reels", "#ytshorts"]

# Titles with any of these → definitely non-episode content
TITLE_EXCLUDE_SIGNALS = [
    "full movie", "full film",
    "best of", "top 10", "top 5",
    "funny moments", "compilation",
    "promo", "teaser", "trailer",
    "tmkoc playschool",
]


def title_prefilter(video: dict) -> str:
    """
    Classify a video using title alone — no API data needed.
    Returns: 'short' | 'excluded' | 'needs_enrichment'
    """
    title_lower = (video.get("title") or "").lower()

    if any(sig in title_lower for sig in TITLE_SHORT_SIGNALS):
        return "short"

    if any(sig in title_lower for sig in TITLE_EXCLUDE_SIGNALS):
        return "excluded"

    return "needs_enrichment"


# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────

def get_api_key() -> str:
    """Read API key from env or .env file."""
    key = os.environ.get("YOUTUBE_API_KEY")
    if key:
        return key

    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("YOUTUBE_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")

    print("ERROR: YOUTUBE_API_KEY not found.")
    print("Set it via:  export YOUTUBE_API_KEY='your-key'")
    print("Or create a .env file in the project root with:  YOUTUBE_API_KEY=your-key")
    sys.exit(1)


def iso_duration_to_seconds(iso_duration: str) -> int:
    """Convert ISO 8601 duration (PT1H2M3S) to total seconds."""
    if not iso_duration or not iso_duration.startswith("PT"):
        return 0

    duration = iso_duration[2:]  # strip "PT"
    hours = minutes = seconds = 0

    if "H" in duration:
        h_part, duration = duration.split("H")
        hours = int(h_part)
    if "M" in duration:
        m_part, duration = duration.split("M")
        minutes = int(m_part)
    if "S" in duration:
        s_part, _ = duration.split("S")
        seconds = int(s_part)

    return hours * 3600 + minutes * 60 + seconds


def api_call_with_retry(request, description: str):
    """Execute an API request with retries on transient errors."""
    for attempt in range(1, API_RETRY_ATTEMPTS + 1):
        try:
            return request.execute()
        except HttpError as e:
            status = e.resp.status if e.resp else 0
            if status == 403 and "quotaExceeded" in str(e):
                print(f"\n⛔ YouTube API quota exceeded! Save checkpoint and retry tomorrow.")
                raise
            if status in (429, 500, 503) and attempt < API_RETRY_ATTEMPTS:
                wait = API_RETRY_DELAY_SECS * attempt
                print(f"  ⚠️  {description}: HTTP {status}, retrying in {wait}s (attempt {attempt}/{API_RETRY_ATTEMPTS})")
                time.sleep(wait)
            else:
                raise
    return None


# ──────────────────────────────────────────────────────────
# Core: Get the "Uploads" playlist ID for a channel
# ──────────────────────────────────────────────────────────

def get_uploads_playlist_id(youtube, channel_id: str) -> str:
    """Resolve a channel ID to its 'uploads' playlist ID."""
    print(f"🔍 Resolving uploads playlist for channel: {channel_id}")
    response = api_call_with_retry(
        youtube.channels().list(part="contentDetails", id=channel_id),
        "channels.list"
    )

    items = response.get("items", [])
    if not items:
        print(f"ERROR: Channel '{channel_id}' not found or has no content.")
        sys.exit(1)

    playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    print(f"✅ Uploads playlist: {playlist_id}")
    return playlist_id


# ──────────────────────────────────────────────────────────
# Core: Fetch all video IDs from the uploads playlist
# ──────────────────────────────────────────────────────────

def fetch_playlist_items(youtube, playlist_id: str, max_pages: int = None, resume_token: str = None):
    """
    Paginate through playlistItems.list and yield basic video metadata.
    
    Yields dicts with: video_id, title, description, published_at, 
                       position, thumbnail_url
    """
    page_token = resume_token
    page_num = 0
    total_fetched = 0

    while True:
        page_num += 1
        if max_pages and page_num > max_pages:
            print(f"  ⏹️  Reached max pages limit ({max_pages})")
            break

        request = youtube.playlistItems().list(
            part="snippet,contentDetails,status",
            playlistId=playlist_id,
            maxResults=PLAYLIST_ITEMS_PER_PAGE,
            pageToken=page_token
        )
        response = api_call_with_retry(request, f"playlistItems.list page {page_num}")

        items = response.get("items", [])
        total_results = response.get("pageInfo", {}).get("totalResults", "?")

        for item in items:
            snippet = item.get("snippet", {})
            content = item.get("contentDetails", {})
            status = item.get("status", {})

            # Skip private/deleted videos
            privacy = status.get("privacyStatus", "public")
            if privacy != "public":
                continue

            thumbnails = snippet.get("thumbnails", {})
            thumb_url = (
                thumbnails.get("maxres", {}).get("url")
                or thumbnails.get("high", {}).get("url")
                or thumbnails.get("default", {}).get("url")
                or ""
            )

            yield {
                "video_id": content.get("videoId", ""),
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "published_at": content.get("videoPublishedAt", snippet.get("publishedAt", "")),
                "position": snippet.get("position", -1),
                "thumbnail_url": thumb_url,
                "channel_title": snippet.get("channelTitle", ""),
            }

        total_fetched += len(items)
        print(f"  📄 Page {page_num}: fetched {len(items)} items (total: {total_fetched}/{total_results})")

        page_token = response.get("nextPageToken")
        if not page_token:
            print(f"  ✅ All pages fetched.")
            break

    return page_token  # None if completed, else the token for resume


# ──────────────────────────────────────────────────────────
# Core: Enrich videos with duration + stats via videos.list
# ──────────────────────────────────────────────────────────

def enrich_videos(youtube, videos: list) -> list:
    """
    Batch-fetch duration and statistics for a list of videos.
    Mutates and returns the same list with added fields.
    """
    video_ids = [v["video_id"] for v in videos if v.get("video_id")]
    detail_map = {}

    # Batch in groups of 50
    for i in range(0, len(video_ids), VIDEO_DETAIL_BATCH_SIZE):
        batch = video_ids[i : i + VIDEO_DETAIL_BATCH_SIZE]
        batch_num = (i // VIDEO_DETAIL_BATCH_SIZE) + 1
        total_batches = (len(video_ids) + VIDEO_DETAIL_BATCH_SIZE - 1) // VIDEO_DETAIL_BATCH_SIZE

        request = youtube.videos().list(
            part="contentDetails,statistics",
            id=",".join(batch)
        )
        response = api_call_with_retry(request, f"videos.list batch {batch_num}/{total_batches}")

        for item in response.get("items", []):
            vid = item["id"]
            content = item.get("contentDetails", {})
            stats = item.get("statistics", {})

            duration_iso = content.get("duration", "PT0S")
            detail_map[vid] = {
                "duration_iso": duration_iso,
                "duration_seconds": iso_duration_to_seconds(duration_iso),
                "view_count": int(stats.get("viewCount", 0)),
                "like_count": int(stats.get("likeCount", 0)),
                "comment_count": int(stats.get("commentCount", 0)),
            }

        print(f"  🎬 Enriched batch {batch_num}/{total_batches} ({len(batch)} videos)")

    # Merge details into video records
    for video in videos:
        vid = video.get("video_id", "")
        if vid in detail_map:
            video.update(detail_map[vid])
        else:
            video.update({
                "duration_iso": None,
                "duration_seconds": 0,
                "view_count": 0,
                "like_count": 0,
                "comment_count": 0,
            })

    return videos


# ──────────────────────────────────────────────────────────
# Checkpoint management
# ──────────────────────────────────────────────────────────

def save_checkpoint(page_token: str, videos_fetched: int):
    """Save progress for resumption."""
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = {
        "next_page_token": page_token,
        "videos_fetched": videos_fetched,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    CHECKPOINT_FILE.write_text(json.dumps(checkpoint, indent=2))
    print(f"  💾 Checkpoint saved ({videos_fetched} videos so far)")


def load_checkpoint() -> dict:
    """Load a previous checkpoint if it exists."""
    if CHECKPOINT_FILE.exists():
        return json.loads(CHECKPOINT_FILE.read_text())
    return None


def clear_checkpoint():
    """Remove checkpoint after successful completion."""
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()


# ──────────────────────────────────────────────────────────
# Classification: tag every video with a content type
# ──────────────────────────────────────────────────────────

# Duration thresholds (seconds)
SHORT_MAX_SECS    = 180    # < 3 min  → Short / promo / teaser
EPISODE_MIN_SECS  = 180    # >= 3 min → regular long-form territory
EPISODE_MAX_SECS  = 2400   # <= 40 min → likely a single episode
COMPILATION_SECS  = 2400   # > 40 min → multi-episode compilation or "Full Movie"

# Title keyword signals
SHORT_KEYWORDS    = ["#shorts", "#short", "#reels"]
COMPILATION_KEYWORDS = [
    "full movie", "full film", "best of", "top 10", "top 5",
    "funny moments", "compilation", "all episodes", "marathon"
]
PROMO_KEYWORDS    = ["promo", "teaser", "trailer", "preview", "coming soon"]


def classify_videos(videos: list) -> list:
    """
    Tag every video with a `_content_type` field:
      - "episode"      → long-form, 3–40 min, the ones we want
      - "short"        → YouTube Short (< 3 min or #shorts in title)
      - "compilation"  → Multi-episode dump / Full Movie (> 40 min or keyword)
      - "promo"        → Teaser / trailer
      - "unknown"      → Could not classify (no duration data)

    Also adds:
      - `_is_short`        (bool) strong short signal
      - `_is_compilation`  (bool)
    """
    for v in videos:
        # If already classified by pre-filter, preserve it
        if v.get("_prefiltered") and v.get("_content_type"):
            continue

        title_lower = (v.get("title") or "").lower()
        desc_lower  = (v.get("description") or "")[:200].lower()  # only check first 200 chars
        duration    = v.get("duration_seconds", 0)

        has_short_keyword       = any(kw in title_lower for kw in SHORT_KEYWORDS)
        has_compilation_keyword = any(kw in title_lower for kw in COMPILATION_KEYWORDS)
        has_promo_keyword       = any(kw in title_lower or kw in desc_lower for kw in PROMO_KEYWORDS)

        v["_is_short"]       = has_short_keyword or (0 < duration < SHORT_MAX_SECS)
        v["_is_compilation"] = has_compilation_keyword or (duration > COMPILATION_SECS)

        if not duration:
            v["_content_type"] = "unknown"
        elif has_short_keyword or duration < SHORT_MAX_SECS:
            v["_content_type"] = "short"
        elif has_promo_keyword and duration < EPISODE_MIN_SECS:
            v["_content_type"] = "promo"
        elif has_compilation_keyword or duration > COMPILATION_SECS:
            v["_content_type"] = "compilation"
        else:
            v["_content_type"] = "episode"

    return videos


# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="TMKOC YouTube Metadata Extractor")
    parser.add_argument("--channel-id", default=DEFAULT_CHANNEL_ID,
                        help=f"YouTube channel ID (default: {DEFAULT_CHANNEL_ID})")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="Limit number of playlist pages to fetch (for testing)")
    parser.add_argument("--skip-enrich", action="store_true",
                        help="Skip fetching video details (duration/stats)")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from last checkpoint")
    parser.add_argument("--output", type=str, default=None,
                        help=f"Output JSONL path (default: {RAW_OUTPUT})")
    args = parser.parse_args()

    output_path = Path(args.output) if args.output else RAW_OUTPUT
    output_path.parent.mkdir(parents=True, exist_ok=True)

    api_key = get_api_key()
    youtube = build("youtube", "v3", developerKey=api_key)

    # ── Step 1: Resolve uploads playlist ──
    uploads_playlist = get_uploads_playlist_id(youtube, args.channel_id)

    # ── Step 2: Fetch playlist items ──
    resume_token = None
    existing_videos = []

    if args.resume:
        checkpoint = load_checkpoint()
        if checkpoint:
            resume_token = checkpoint.get("next_page_token")
            print(f"🔄 Resuming from checkpoint ({checkpoint['videos_fetched']} videos previously fetched)")
            # Load existing partial output
            if output_path.exists():
                with open(output_path, "r") as f:
                    existing_videos = [json.loads(line) for line in f if line.strip()]
                print(f"  📂 Loaded {len(existing_videos)} existing records from {output_path.name}")
        else:
            print("ℹ️  No checkpoint found, starting fresh.")

    print(f"\n{'='*60}")
    print(f"📥 Fetching videos from uploads playlist...")
    print(f"{'='*60}\n")

    new_videos = list(fetch_playlist_items(
        youtube, uploads_playlist,
        max_pages=args.max_pages,
        resume_token=resume_token
    ))

    all_videos = existing_videos + new_videos
    print(f"\n📊 Fetched {len(new_videos)} new videos ({len(all_videos)} total)")

    # Deduplicate by video_id (keep latest)
    seen = {}
    for v in all_videos:
        seen[v["video_id"]] = v
    all_videos = list(seen.values())
    print(f"📊 After dedup: {len(all_videos)} unique videos")

    # ── Step 3: Title pre-filter (no API call needed) ──
    # Classify obvious shorts/excluded by title alone before enrichment.
    # This avoids videos.list calls for ~64% of videos.
    needs_enrichment = []
    title_filtered_out = []

    for v in all_videos:
        verdict = title_prefilter(v)
        if verdict == "short":
            v["_content_type"] = "short"
            v["_prefiltered"] = True
            # Assign zero duration so classify_videos() handles it consistently
            v.update({"duration_iso": None, "duration_seconds": 0,
                      "view_count": 0, "like_count": 0, "comment_count": 0})
            title_filtered_out.append(v)
        elif verdict == "excluded":
            v["_content_type"] = "compilation"
            v["_prefiltered"] = True
            v.update({"duration_iso": None, "duration_seconds": 0,
                      "view_count": 0, "like_count": 0, "comment_count": 0})
            title_filtered_out.append(v)
        else:
            needs_enrichment.append(v)

    print(f"\n⚡ Title pre-filter: {len(title_filtered_out)} videos skipped enrichment "
          f"({len(title_filtered_out)/len(all_videos)*100:.0f}%), "
          f"{len(needs_enrichment)} need enrichment")

    # ── Step 4: Enrich only non-obvious videos with duration + stats ──
    if not args.skip_enrich and needs_enrichment:
        print(f"\n{'='*60}")
        print(f"🎬 Enriching {len(needs_enrichment)} videos with duration & statistics...")
        print(f"{'='*60}\n")
        needs_enrichment = enrich_videos(youtube, needs_enrichment)

    # Recombine: enriched + title-prefiltered (already have zeroed-out duration fields)
    all_videos = needs_enrichment + title_filtered_out

    # ── Step 5: Classify & filter videos ──
    # classify_videos() will skip videos already tagged by title_prefilter
    all_videos = classify_videos(all_videos)

    # Partition into long-form (keep) and excluded (logged)
    longform = [v for v in all_videos if v["_content_type"] == "episode"]
    excluded = [v for v in all_videos if v["_content_type"] != "episode"]

    print(f"\n{'='*60}")
    print(f"🔎 Classification results:")
    print(f"{'='*60}")
    type_counts = {}
    for v in all_videos:
        t = v["_content_type"]
        type_counts[t] = type_counts.get(t, 0) + 1
    for t, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  {t:<20} {count:>5} videos")
    print(f"  {'─'*26}")
    print(f"  {'KEEPING (episodes)':<20} {len(longform):>5} videos")
    print(f"  {'EXCLUDING':<20} {len(excluded):>5} videos")

    # ── Step 5: Add ETL metadata ──
    ingested_at = datetime.now(timezone.utc).isoformat()
    for v in longform:
        v["_ingested_at"] = ingested_at
        v["_source"] = "youtube_api_v3"

    # ── Step 6: Write output ──
    with open(output_path, "w") as f:
        for v in longform:
            f.write(json.dumps(v, ensure_ascii=False) + "\n")

    # Write excluded log for inspection
    excluded_path = output_path.parent / "excluded_videos.jsonl"
    with open(excluded_path, "w") as f:
        for v in excluded:
            f.write(json.dumps(v, ensure_ascii=False) + "\n")
    print(f"\n🗑️  Excluded videos logged to {excluded_path}")

    print(f"\n✅ Wrote {len(longform)} long-form records to {output_path}")

    # ── Step 7: Write summary ──
    summary = {
        "channel_id": args.channel_id,
        "total_videos_fetched": len(all_videos),
        "total_longform_kept": len(longform),
        "total_excluded": len(excluded),
        "content_type_breakdown": type_counts,
        "extracted_at": ingested_at,
        "output_file": str(output_path),
        "sample_titles": [v.get("title", "") for v in longform[:5]],
    }

    if longform and "duration_seconds" in longform[0]:
        durations = [v["duration_seconds"] for v in longform if v.get("duration_seconds")]
        if durations:
            summary["duration_stats"] = {
                "min_seconds": min(durations),
                "max_seconds": max(durations),
                "avg_seconds": round(sum(durations) / len(durations)),
                "total_hours": round(sum(durations) / 3600, 1),
            }

    SUMMARY_FILE.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"📋 Summary written to {SUMMARY_FILE}")

    # ── Cleanup ──
    clear_checkpoint()
    print(f"\n🎉 Extraction complete!")

    # Quick peek
    print(f"\n{'='*60}")
    print(f"👀 Sample long-form records:")
    print(f"{'='*60}")
    for v in longform[:3]:
        print(f"  [{v.get('video_id', '?')}] {v.get('title', '?')}")
        if "duration_seconds" in v:
            mins = v["duration_seconds"] // 60
            print(f"       Duration: {mins}m | Views: {v.get('view_count', '?'):,}")
        print()


if __name__ == "__main__":
    main()
