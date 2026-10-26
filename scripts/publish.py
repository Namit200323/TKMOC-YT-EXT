#!/usr/bin/env python3
"""
TMKOC Publisher — Upload arcs.json to Cloudflare R2
=====================================================
Compresses arcs.json with Brotli (or gzip fallback), computes SHA-256,
diffs against the previously published version, and uploads only if changed.

Prerequisites:
    pip install boto3 brotli

Required env vars (add to .env):
    R2_ACCOUNT_ID       → From Cloudflare Dashboard → R2 → Overview
    R2_ACCESS_KEY_ID    → From R2 → Manage API Tokens → Create API Token
    R2_SECRET_ACCESS_KEY
    R2_BUCKET_NAME      → e.g. "tmkoc-arc-data"

Usage:
    python3 scripts/publish.py                  # compress + diff + upload if changed
    python3 scripts/publish.py --force          # upload unconditionally
    python3 scripts/publish.py --dry-run        # compress + diff, skip upload
    python3 scripts/publish.py --object-key v2/arcs.json   # custom key
"""

import argparse
import gzip
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Optional: brotli for better compression ──
from typing import Optional

try:
    import brotli
    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:
    print("ERROR: boto3 not installed.  Run: pip install boto3")
    sys.exit(1)


# ──────────────────────────────────────────────────────────
# Paths & Constants
# ──────────────────────────────────────────────────────────

DATA_DIR         = Path(__file__).resolve().parent.parent / "data"
ARCS_JSON        = DATA_DIR / "arcs.json"
LAST_HASH_FILE   = DATA_DIR / ".last_published_hash"

DEFAULT_OBJECT_KEY = "v2/arcs.json"


# ──────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────

def load_env():
    """Load .env file from project root into os.environ if not already set."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key not in os.environ:
                    os.environ[key] = val


def get_r2_config() -> dict:
    """Read R2 credentials from environment."""
    load_env()

    missing = []
    config = {}
    for var in ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"]:
        val = os.environ.get(var)
        if not val:
            missing.append(var)
        else:
            config[var] = val

    if missing:
        print("ERROR: Missing required environment variables:")
        for v in missing:
            print(f"  {v}")
        print("\nAdd them to your .env file:")
        print("  R2_ACCOUNT_ID=<your-account-id>")
        print("  R2_ACCESS_KEY_ID=<your-r2-token-access-key>")
        print("  R2_SECRET_ACCESS_KEY=<your-r2-token-secret-key>")
        print("  R2_BUCKET_NAME=tmkoc-arc-data")
        print("\nGet these from: Cloudflare Dashboard → R2 → Manage R2 API Tokens")
        sys.exit(1)

    return config


# ──────────────────────────────────────────────────────────
# Compression
# ──────────────────────────────────────────────────────────

def compress(data: bytes) -> tuple[bytes, str]:
    """
    Compress bytes. Returns (compressed_bytes, content_encoding).
    Prefers Brotli (better ratio), falls back to gzip.
    """
    if HAS_BROTLI:
        compressed = brotli.compress(data, quality=11)
        return compressed, "br"
    else:
        compressed = gzip.compress(data, compresslevel=9)
        return compressed, "gzip"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ──────────────────────────────────────────────────────────
# R2 Client
# ──────────────────────────────────────────────────────────

def make_r2_client(config: dict):
    """Create a boto3 S3 client pointed at Cloudflare R2."""
    endpoint = f"https://{config['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url          = endpoint,
        aws_access_key_id     = config["R2_ACCESS_KEY_ID"],
        aws_secret_access_key = config["R2_SECRET_ACCESS_KEY"],
        region_name           = "auto",
    )


# ──────────────────────────────────────────────────────────
# Diff check
# ──────────────────────────────────────────────────────────

def get_remote_etag(client, bucket: str, key: str) -> Optional[str]:
    """Fetch the ETag of the currently published object, or None if not found."""
    try:
        response = client.head_object(Bucket=bucket, Key=key)
        return response.get("ETag", "").strip('"')
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return None
        raise


def has_changed(raw_bytes: bytes) -> bool:
    """Compare SHA-256 of current arcs.json against last published hash."""
    current_hash = sha256(raw_bytes)
    if LAST_HASH_FILE.exists():
        last_hash = LAST_HASH_FILE.read_text().strip()
        return current_hash != last_hash
    return True   # no previous hash → always upload


def save_hash(raw_bytes: bytes):
    LAST_HASH_FILE.write_text(sha256(raw_bytes))


# ──────────────────────────────────────────────────────────
# Upload
# ──────────────────────────────────────────────────────────

def upload(
    client,
    bucket:           str,
    object_key:       str,
    compressed_bytes: bytes,
    content_encoding: str,
    raw_size:         int,
):
    """Upload compressed arcs.json to R2 with correct headers."""
    compressed_size = len(compressed_bytes)
    ratio = (1 - compressed_size / raw_size) * 100 if raw_size else 0

    print(f"  Uncompressed : {raw_size / 1024:.1f} KB")
    print(f"  Compressed   : {compressed_size / 1024:.1f} KB  "
          f"({content_encoding}, {ratio:.0f}% smaller)")
    print(f"  Uploading to : s3://{bucket}/{object_key}")

    client.put_object(
        Bucket          = bucket,
        Key             = object_key,
        Body            = compressed_bytes,
        ContentType     = "application/json",
        ContentEncoding = content_encoding,
        CacheControl    = "public, max-age=86400, stale-while-revalidate=604800",
        Metadata        = {
            "generated-at": datetime.now(timezone.utc).isoformat(),
            "sha256":        sha256(compressed_bytes),
        }
    )

    # Also upload an uncompressed copy at v2/arcs.uncompressed.json (debug use)
    debug_key = object_key.replace("arcs.json", "arcs.uncompressed.json")
    # (skipped in production — uncomment if needed for debugging)
    # client.put_object(Bucket=bucket, Key=debug_key, Body=raw_bytes,
    #                   ContentType="application/json")

    print(f"  ✅ Upload complete")


# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="TMKOC arcs.json Publisher → Cloudflare R2")
    parser.add_argument("--object-key", default=DEFAULT_OBJECT_KEY,
                        help=f"R2 object key (default: {DEFAULT_OBJECT_KEY})")
    parser.add_argument("--force",   action="store_true",
                        help="Upload even if content hasn't changed")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compress and diff but don't upload")
    args = parser.parse_args()

    # ── Validate input ──
    if not ARCS_JSON.exists():
        print(f"ERROR: {ARCS_JSON} not found. Run cluster.py first.")
        sys.exit(1)

    raw_bytes = ARCS_JSON.read_bytes()
    print(f"📂 Loaded arcs.json  ({len(raw_bytes)/1024:.1f} KB)")

    # ── Quick sanity check on JSON ──
    try:
        payload = json.loads(raw_bytes)
        n_arcs = len(payload.get("arcs", []))
        n_eps  = payload.get("meta", {}).get("total_episodes", "?")
        n_idx  = len(payload.get("video_index", {}))
        print(f"   {n_arcs} arcs | {n_eps} episodes | {n_idx} indexed videos")
    except json.JSONDecodeError as e:
        print(f"ERROR: arcs.json is invalid JSON: {e}")
        sys.exit(1)

    # ── Compress ──
    compressed_bytes, encoding = compress(raw_bytes)
    print(f"\n🗜️  Compression: {encoding}  "
          f"{len(raw_bytes)/1024:.1f} KB → {len(compressed_bytes)/1024:.1f} KB")

    # ── Diff check ──
    if not args.force and not has_changed(raw_bytes):
        print("\n✅ No changes detected vs. last publish — skipping upload.")
        print("   Use --force to upload anyway.")
        return

    if args.dry_run:
        print("\n🔍 Dry run — skipping upload.")
        print(f"   Would upload to: {args.object_key}")
        return

    # ── R2 upload ──
    config = get_r2_config()
    print(f"\n{'='*60}")
    print(f"☁️  Uploading to Cloudflare R2 bucket: {config['R2_BUCKET_NAME']}")
    print(f"{'='*60}\n")

    try:
        client = make_r2_client(config)
        upload(
            client,
            bucket           = config["R2_BUCKET_NAME"],
            object_key       = args.object_key,
            compressed_bytes = compressed_bytes,
            content_encoding = encoding,
            raw_size         = len(raw_bytes),
        )
    except NoCredentialsError:
        print("ERROR: R2 credentials are invalid or expired.")
        sys.exit(1)
    except ClientError as e:
        print(f"ERROR: R2 upload failed: {e}")
        sys.exit(1)

    # ── Save hash for next run ──
    save_hash(raw_bytes)
    print(f"\n💾 Hash saved → future runs will skip upload if unchanged")

    # ── Public URL ──
    print(f"\n🌐 Published URL (set up custom domain on R2 bucket to use):")
    print(f"   https://<your-r2-domain>/{args.object_key}")
    print(f"\n🎉 Done!")


if __name__ == "__main__":
    main()
