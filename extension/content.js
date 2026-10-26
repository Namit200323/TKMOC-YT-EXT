/**
 * TMKOC Arc Navigator - Content Script
 * Injects a story arc navigation bar directly into YouTube watch pages.
 */

(function () {
  "use strict";

  let arcData = null;
  let currentVideoId = null;
  let isDrawerOpen = false;

  // ──────────────────────────────────────────────────────────
  // Data Loader: Cache in chrome.storage.local with bundled fallback
  // ──────────────────────────────────────────────────────────

  async function loadArcData() {
    if (arcData) return arcData;

    try {
      // 1. Try local storage cache
      const cached = await chrome.storage.local.get(["tmkoc_arcs_data", "tmkoc_cached_at"]);
      const now = Date.now();
      const oneDay = 24 * 60 * 60 * 1000;

      if (cached.tmkoc_arcs_data && cached.tmkoc_cached_at && now - cached.tmkoc_cached_at < oneDay) {
        arcData = cached.tmkoc_arcs_data;
        return arcData;
      }
    } catch (e) {
      console.warn("[TMKOC Ext] Storage access failed, loading bundled file:", e);
    }

    // 2. Fallback to bundled data/arcs.json
    try {
      const url = chrome.runtime.getURL("data/arcs.json");
      const res = await fetch(url);
      arcData = await res.json();

      try {
        await chrome.storage.local.set({
          tmkoc_arcs_data: arcData,
          tmkoc_cached_at: Date.now()
        });
      } catch (err) {
        // storage quota or permission issue
      }
      return arcData;
    } catch (err) {
      console.error("[TMKOC Ext] Failed to load arcs data:", err);
      return null;
    }
  }

  // ──────────────────────────────────────────────────────────
  // YouTube DOM Helpers
  // ──────────────────────────────────────────────────────────

  function getVideoIdFromUrl() {
    const params = new URLSearchParams(window.location.search);
    return params.get("v");
  }

  function getTargetMountPoint() {
    // Standard YouTube watch page layout targets
    return (
      document.querySelector("#above-the-fold") ||
      document.querySelector("#primary-inner") ||
      document.querySelector("#primary")
    );
  }

  function removeExistingBar() {
    const existing = document.getElementById("tmkoc-arc-nav");
    if (existing) existing.remove();
  }

  // ──────────────────────────────────────────────────────────
  // UI Renderer
  // ──────────────────────────────────────────────────────────

  function renderNavigationBar(videoInfo, arc) {
    removeExistingBar();

    const mount = getTargetMountPoint();
    if (!mount) {
      // Retry in 300ms if YouTube DOM hasn't rendered yet
      setTimeout(() => renderNavigationBar(videoInfo, arc), 300);
      return;
    }

    const { position } = videoInfo;
    const episodes = arc.episodes;
    const currentEp = episodes[position];
    const prevEp = position > 0 ? episodes[position - 1] : null;
    const nextEp = position < episodes.length - 1 ? episodes[position + 1] : null;

    const container = document.createElement("div");
    container.id = "tmkoc-arc-nav";
    container.className = "tmkoc-nav-container";

    // Header: Badge + Arc Title + Ep X of Y
    const header = document.createElement("div");
    header.className = "tmkoc-header";

    const badgeWrap = document.createElement("div");
    badgeWrap.className = "tmkoc-badge-wrap";
    badgeWrap.innerHTML = `
      <span class="tmkoc-brand-tag">TMKOC Arc</span>
      <span class="tmkoc-arc-title">${escapeHtml(arc.arc_title)}</span>
      <span class="tmkoc-ep-progress">Ep ${currentEp.episode_number} (${position + 1}/${episodes.length})</span>
    `;

    header.appendChild(badgeWrap);

    // Controls Row: Prev / Next / Toggle All Episodes
    const controls = document.createElement("div");
    controls.className = "tmkoc-controls";

    // Prev Button
    const prevBtn = document.createElement("a");
    prevBtn.className = `tmkoc-btn ${!prevEp ? "tmkoc-disabled" : ""}`;
    if (prevEp) {
      prevBtn.href = `/watch?v=${prevEp.video_id}`;
      prevBtn.innerHTML = `‹ Ep ${prevEp.episode_number}`;
      prevBtn.title = prevEp.title_clean;
    } else {
      prevBtn.innerHTML = `‹ Start of Arc`;
    }

    // Next Button
    const nextBtn = document.createElement("a");
    nextBtn.className = `tmkoc-btn tmkoc-btn-primary ${!nextEp ? "tmkoc-disabled" : ""}`;
    if (nextEp) {
      nextBtn.href = `/watch?v=${nextEp.video_id}`;
      nextBtn.innerHTML = `Next: Ep ${nextEp.episode_number} ›`;
      nextBtn.title = nextEp.title_clean;
    } else {
      nextBtn.innerHTML = `End of Arc ›`;
    }

    // Drawer Toggle
    const drawerBtn = document.createElement("button");
    drawerBtn.className = "tmkoc-btn tmkoc-btn-drawer";
    drawerBtn.innerHTML = `☰ All Episodes (${episodes.length})`;
    drawerBtn.addEventListener("click", () => {
      isDrawerOpen = !isDrawerOpen;
      drawer.style.display = isDrawerOpen ? "flex" : "none";
      drawerBtn.style.background = isDrawerOpen ? "rgba(255, 255, 255, 0.15)" : "transparent";
    });

    controls.appendChild(prevBtn);
    controls.appendChild(nextBtn);
    controls.appendChild(drawerBtn);

    // Episode List Drawer (collapsible)
    const drawer = document.createElement("div");
    drawer.className = "tmkoc-drawer";
    drawer.style.display = isDrawerOpen ? "flex" : "none";

    episodes.forEach((ep, idx) => {
      const epItem = document.createElement("a");
      epItem.href = `/watch?v=${ep.video_id}`;
      epItem.className = `tmkoc-ep-item ${idx === position ? "tmkoc-active" : ""}`;
      epItem.innerHTML = `
        <span class="tmkoc-ep-num">Ep ${ep.episode_number}</span>
        <span class="tmkoc-ep-title">${escapeHtml(ep.title_clean)}</span>
      `;
      drawer.appendChild(epItem);
    });

    container.appendChild(header);
    container.appendChild(controls);
    container.appendChild(drawer);

    // Insert at the top of #above-the-fold or mount
    mount.insertBefore(container, mount.firstChild);
  }

  function escapeHtml(str) {
    if (!str) return "";
    return str
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // ──────────────────────────────────────────────────────────
  // Page Transition Handler
  // ──────────────────────────────────────────────────────────

  async function handleNavigation() {
    const videoId = getVideoIdFromUrl();

    if (!videoId) {
      removeExistingBar();
      currentVideoId = null;
      return;
    }

    if (videoId === currentVideoId && document.getElementById("tmkoc-arc-nav")) {
      return; // Already rendered for this video
    }

    currentVideoId = videoId;
    const data = await loadArcData();
    if (!data || !data.video_index) return;

    const indexed = data.video_index[videoId];
    if (!indexed) {
      // Not a TMKOC episode in our dataset
      removeExistingBar();
      return;
    }

    // Find the corresponding arc
    const arc = data.arcs.find((a) => a.arc_id === indexed.arc_id);
    if (!arc) {
      removeExistingBar();
      return;
    }

    renderNavigationBar(indexed, arc);
  }

  // ──────────────────────────────────────────────────────────
  // SPA Lifecycle Listeners for YouTube
  // ──────────────────────────────────────────────────────────

  // 1. YouTube SPA navigation event
  window.addEventListener("yt-navigate-finish", handleNavigation);

  // 2. Popstate (back / forward buttons)
  window.addEventListener("popstate", handleNavigation);

  // 3. Initial execution
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", handleNavigation);
  } else {
    handleNavigation();
  }
})();
