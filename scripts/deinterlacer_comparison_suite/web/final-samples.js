"use strict";

const $ = (s) => document.querySelector(s);

const state = {
  samples: [],
  index: 0,
  syncing: false,
  rate: 1,
};

function videos() {
  return [$("#sample-original"), $("#sample-final")];
}

function setStatus(text, meta = "") {
  $("#sample-title").textContent = text;
  $("#sample-meta").textContent = meta;
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "unknown size";
  const mb = bytes / 1000000;
  const mib = bytes / 1048576;
  return `${mb.toFixed(2)} MB (${mib.toFixed(2)} MiB)`;
}

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url}: ${res.status}`);
  return res.json();
}

function applyRate() {
  videos().forEach((video) => {
    video.playbackRate = state.rate;
  });
  const button = $("#sample-speed");
  button.textContent = state.rate === 0.5 ? "0.5x" : "1x";
  button.classList.toggle("active", state.rate === 0.5);
  button.setAttribute("aria-pressed", state.rate === 0.5 ? "true" : "false");
}

function syncFrom(source) {
  if (state.syncing) return;
  state.syncing = true;
  videos().forEach((video) => {
    if (video === source || !video.src) return;
    if (Number.isFinite(source.currentTime) && Math.abs(video.currentTime - source.currentTime) > 0.08) {
      try {
        video.currentTime = source.currentTime;
      } catch (e) {
        // Ignore seek races while metadata is loading.
      }
    }
    if (source.paused && !video.paused) video.pause();
    if (!source.paused && video.paused) video.play().catch(() => {});
  });
  state.syncing = false;
}

function renderList() {
  const list = $("#sample-list");
  list.textContent = "";
  state.samples.forEach((sample, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${index + 1}. ${sample.label || sample.id}`;
    button.className = index === state.index ? "active" : "";
    button.addEventListener("click", () => showSample(index));
    list.appendChild(button);
  });
}

function showSample(index) {
  if (!state.samples.length) return;
  state.index = (index + state.samples.length) % state.samples.length;
  const sample = state.samples[state.index];
  renderList();
  setStatus(
    sample.label || sample.id,
    `Average luma ${sample.avg_luma ?? "?"} | ${sample.duration_s ?? "?"}s | source: ${sample.source_file || ""}`,
  );
  const [original, final] = videos();
  original.pause();
  final.pause();
  original.src = sample.original_url;
  final.src = sample.final_url;
  $("#sample-original-size").textContent = `File size: ${formatBytes(sample.original_size_bytes)}`;
  $("#sample-final-size").textContent = `File size: ${formatBytes(sample.final_size_bytes)}`;
  original.currentTime = 0;
  final.currentTime = 0;
  applyRate();
  original.load();
  final.load();
  $("#play-toggle").textContent = "Play";
}

async function togglePlay() {
  const [original, final] = videos();
  if (!original.src || !final.src) return;
  if (original.paused && final.paused) {
    try {
      await Promise.all(videos().map((video) => video.play()));
      $("#play-toggle").textContent = "Pause";
    } catch (e) {
      setStatus("Could not start playback", e.message);
    }
  } else {
    videos().forEach((video) => video.pause());
    $("#play-toggle").textContent = "Play";
  }
}

async function init() {
  $("#back-main").addEventListener("click", () => {
    window.location.href = "/";
  });
  $("#prev-sample").addEventListener("click", () => showSample(state.index - 1));
  $("#next-sample").addEventListener("click", () => showSample(state.index + 1));
  $("#play-toggle").addEventListener("click", togglePlay);
  $("#sample-speed").addEventListener("click", () => {
    state.rate = state.rate === 0.5 ? 1 : 0.5;
    applyRate();
  });
  videos().forEach((video) => {
    ["play", "pause", "seeked", "timeupdate"].forEach((eventName) => {
      video.addEventListener(eventName, () => syncFrom(video));
    });
  });
  try {
    const data = await fetchJSON("/api/final-samples");
    state.samples = (data.samples || []).filter((sample) => sample.ready);
    if (!state.samples.length) {
      setStatus("No final samples are ready yet.");
      return;
    }
    showSample(0);
  } catch (e) {
    setStatus("Failed to load final samples", e.message);
  }
}

init();
