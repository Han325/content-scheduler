/* ===================================================================
   LINEUP — client-side interactivity
   Clock · airtime "now" detection · mark-as-watched
   =================================================================== */

function pad(n) { return String(n).padStart(2, "0"); }

/* --- live broadcast clock --- */
function tickClock() {
  const now = new Date();
  const el = document.getElementById("clock-time");
  if (el) el.textContent = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
}

/* --- detect which program is on air and style it --- */
function updateNow() {
  const now = new Date();
  const nowMin = now.getHours() * 60 + now.getMinutes();

  document.querySelectorAll(".program[data-start-min]").forEach(row => {
    const startMin = +row.dataset.startMin;
    const endMin   = +row.dataset.endMin;
    const isNow    = nowMin >= startMin && nowMin < endMin;
    const wasNow   = row.classList.contains("is-now");

    if (isNow && !wasNow && !row.classList.contains("is-watched")) {
      row.classList.add("is-now");
      const meta    = row.querySelector(".prog-meta");
      const channel = meta.querySelector(".prog-channel");
      const endTime = row.querySelector(".slot-time .end")?.textContent ?? "";
      const flag = document.createElement("div");
      flag.className = "now-flag";
      flag.innerHTML = `<span class="blip"></span> On air now &middot; until ${endTime}`;
      meta.insertBefore(flag, channel);
      const btn = row.querySelector(".btn-play");
      if (btn) {
        btn.classList.add("live");
        btn.innerHTML = `<span class="tri">&#9654;</span> Watch live`;
      }
    } else if (!isNow && wasNow) {
      row.classList.remove("is-now");
      row.querySelector(".now-flag")?.remove();
      const btn = row.querySelector(".btn-play");
      if (btn) {
        btn.classList.remove("live");
        btn.innerHTML = `<span class="tri">&#9654;</span> Watch`;
      }
    }
  });
}

/* --- mark as watched + open YouTube --- */
async function playVideo(youtubeId) {
  try {
    await fetch(`/watch/${youtubeId}`, { method: "POST" });
  } catch (e) {
    console.warn("Could not mark as watched:", e);
  }
  window.open(`https://www.youtube.com/watch?v=${youtubeId}`, "_blank");

  const btn = document.querySelector(`[data-youtube-id="${youtubeId}"]`);
  if (btn) {
    const row = btn.closest(".program");
    if (row) {
      row.classList.add("is-watched");
      row.classList.remove("is-now");
      row.querySelector(".now-flag")?.remove();
      const action = row.querySelector(".prog-action");
      if (action) action.innerHTML = `<span class="done">&#10003; Watched</span>`;
    }
  }

  const unwatched = document.querySelectorAll(".program:not(.is-watched)[data-start-min]");
  if (unwatched.length === 0) setTimeout(() => location.reload(), 700);
}

/* --- skip a video and pull in a backlog replacement --- */
async function skipVideo(youtubeId) {
  const skipBtn = document.querySelector(`.btn-skip[data-youtube-id="${youtubeId}"]`);
  if (skipBtn) { skipBtn.disabled = true; skipBtn.textContent = "…"; }

  try {
    const res = await fetch(`/skip/${youtubeId}`, { method: "POST" });
    if (res.ok) {
      location.reload();
    } else {
      if (skipBtn) { skipBtn.disabled = false; skipBtn.textContent = "✕"; }
    }
  } catch (e) {
    console.warn("Skip failed:", e);
    if (skipBtn) { skipBtn.disabled = false; skipBtn.textContent = "✕"; }
  }
}

/* --- live quota bar --- */
async function fetchQuota() {
  try {
    const res = await fetch("/api/quota");
    if (!res.ok) return;
    const { used, limit, pct } = await res.json();
    document.querySelectorAll(".quota-fill").forEach(el => {
      el.style.width = Math.min(pct, 100) + "%";
    });
    document.querySelectorAll(".quota-label").forEach(el => {
      el.textContent = `${used} / ${limit} API units`;
    });
  } catch (e) { /* silently ignore — bar keeps server-rendered value */ }
}

/* --- trigger curation refresh --- */
async function triggerRefresh() {
  const btn = document.getElementById("refresh-btn");
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  btn.classList.add("is-refreshing");
  btn.classList.remove("refresh-done");
  btn.textContent = "Building…";

  try {
    const res = await fetch("/refresh", { method: "POST" });
    const data = await res.json();
    if (res.ok) {
      btn.classList.remove("is-refreshing");
      btn.classList.add("refresh-done");
      btn.textContent = `✓ ${data.lineup_count ?? "?"} videos`;
      btn.disabled = false;
      await fetchQuota();
      sessionStorage.setItem("lineup-refreshed", JSON.stringify({
        count: data.lineup_count ?? "?",
        minutes: data.lineup_minutes ?? "?"
      }));
      setTimeout(() => location.reload(), 1000);
    } else {
      btn.classList.remove("is-refreshing");
      btn.textContent = "Error";
      btn.disabled = false;
    }
  } catch (e) {
    btn.classList.remove("is-refreshing");
    btn.textContent = "Error";
    btn.disabled = false;
  }
}

/* --- post-reload refresh toast --- */
function showRefreshToast(count, minutes) {
  const toast = document.createElement("div");
  toast.className = "refresh-toast";
  toast.textContent = `Lineup rebuilt — ${count} videos · ${minutes}m`;
  document.body.prepend(toast);
  setTimeout(() => toast.remove(), 3200);
}

/* --- boot --- */
document.addEventListener("DOMContentLoaded", () => {
  const d = new Date();
  const days   = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"];
  const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const dateEl = document.getElementById("clock-date");
  if (dateEl) dateEl.textContent = `${days[d.getDay()]} · ${months[d.getMonth()]} ${d.getDate()}`;

  tickClock();
  setInterval(tickClock, 1000);

  updateNow();
  setInterval(updateNow, 60_000);

  fetchQuota();
  setInterval(fetchQuota, 60_000);

  const refreshed = sessionStorage.getItem("lineup-refreshed");
  if (refreshed) {
    sessionStorage.removeItem("lineup-refreshed");
    try {
      const { count, minutes } = JSON.parse(refreshed);
      showRefreshToast(count, minutes);
    } catch (_) {}
  }
});
