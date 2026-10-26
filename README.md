# TMKOC Arc Navigator & Pipeline

An automated ETL pipeline and Chrome Extension (Manifest V3) that processes YouTube metadata from the official **"Taarak Mehta Ka Ooltah Chashmah" (TMKOC)** channel, structures backfilled and ongoing episodes into chronological **Story Arcs**, and injects an overlay navigation bar for seamless episodic binge-watching on YouTube.

---

## 🚀 Features

- **Sequential Arc Navigation**: Overlays a clean Netflix-style navigation bar above YouTube videos with `‹ Prev`, `Next ›`, and a collapsible `☰ All Episodes` drawer.
- **YouTube SPA Aware**: Seamlessly updates on navigation events (`yt-navigate-finish`) without full page refreshes.
- **Client-Side $O(1)$ Lookup**: Indexed by video ID, cached using `chrome.storage.local`.
- **Automated Python ETL**:
  - `scripts/extract.py`: Paginates YouTube Data API v3, pre-filters Shorts and compilations to conserve quota.
  - `scripts/transform.py`: Cascade regex parser extracting episode numbers, seasons, and clean titles.
  - `scripts/cluster.py`: Clusters episodes into story arcs using gap detection and keyword signals, building verified doubly-linked lists.
  - `scripts/publish.py`: Brotli compression & diff-based publishing to Cloudflare R2 / Object Storage.

---

## 📁 Repository Structure

```text
├── extension/                 # Chrome Extension (Manifest V3)
│   ├── manifest.json
│   ├── content.js             # YouTube content script & DOM injector
│   ├── styles.css             # YouTube-native dark theme styling
│   └── data/
│       └── arcs.json          # Pre-packaged 40 story arcs & video index
├── scripts/
│   ├── extract.py             # YouTube API metadata crawler
│   ├── transform.py           # Title parsing & canonical episode cleaner
│   ├── cluster.py             # Arc clustering & linked-list generator
│   └── publish.py             # Cloudflare R2 uploader with Brotli compression
├── requirements.txt
└── README.md
```

---

## 🛠️ Getting Started

### 1. Load the Chrome Extension
1. Open Google Chrome and go to `chrome://extensions/`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and select the `extension/` folder in this repo.
4. Open any TMKOC episode on YouTube (e.g., Episode 1: `https://www.youtube.com/watch?v=hr7KzzUPk6c`).

### 2. Running the ETL Pipeline (Optional)
```bash
# Install dependencies
pip install -r requirements.txt

# Configure your YouTube Data API key in .env
echo "YOUTUBE_API_KEY=your_key_here" > .env

# Run extract, transform, and cluster
python scripts/extract.py
python scripts/transform.py
python scripts/cluster.py
```
