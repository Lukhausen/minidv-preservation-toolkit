"use strict";

const $ = (s) => document.querySelector(s);

function initialCompareMode() {
  try {
    return localStorage.getItem("compareMode") === "video" ? "video" : "image";
  } catch (e) {
    return "image";
  }
}

function initialPlaybackRate() {
  try {
    return localStorage.getItem("videoPlaybackRate") === "0.5" ? 0.5 : 1;
  } catch (e) {
    return 1;
  }
}

const state = {
  pair: null,
  currentSide: "left",
  nextPlaybackSide: "left",
  compareMode: initialCompareMode(),
  pairingMode: "adaptive",
  peekingOriginal: false,
  zoomed: false,
  revealModes: false,
  videoPlaybackRate: initialPlaybackRate(),
  polling: null,
  busy: false,
  playingSide: null,
  videoLoopStarted: false,
  switchingVideo: false,
  top3: null,
  top3Index: 0,
  top3Polling: null,
  top3Busy: false,
  top3Switching: false,
  top3LoopStarted: false,
  compression: null,
  compressionIndex: 0,
  compressionPolling: null,
  compressionBusy: false,
  compressionSwitching: false,
  compressionLoopStarted: false,
  previewJobId: null,
  previewPolling: null,
  previewBusy: false,
};

const sideLabel = { left: "A", right: "B" };
const otherSide = { left: "right", right: "left" };

function pairURL() {
  return state.compareMode === "video" ? "/api/pair?media=video" : "/api/pair";
}

function pairReadyForMode(p = state.pair) {
  if (!p) return false;
  return state.compareMode === "video" ? Boolean(p.video_ready) : Boolean(p.ready);
}

function pairDisplayReady(p) {
  if (!p) return false;
  if (state.peekingOriginal && p.original.status !== "done") return false;
  return pairReadyForMode(p);
}

async function fetchJSON(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error(`${url}: ${res.status}`);
  return res.json();
}

function isTypingTarget(target) {
  return target && ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

function stopVideos() {
  ["left", "right"].forEach((side) => {
    const video = side === "left" ? $("#clip-left") : $("#clip-right");
    if (!video) return;
    video.pause();
    try {
      video.currentTime = 0;
    } catch (e) {
      // Some browsers reject currentTime before metadata is loaded.
    }
  });
  state.playingSide = null;
  state.videoLoopStarted = false;
}

function videoForSide(side) {
  return side === "left" ? $("#clip-left") : $("#clip-right");
}

function allVideoElements() {
  return [
    $("#clip-left"),
    $("#clip-right"),
    $("#top3-clip-0"),
    $("#top3-clip-1"),
    $("#compression-clip-0"),
    $("#compression-clip-1"),
    $("#preview-original-video"),
    $("#preview-video"),
    $("#preview-b-video"),
  ].filter(Boolean);
}

function applyPlaybackRate(video) {
  if (!video) return;
  video.playbackRate = state.videoPlaybackRate;
}

function applyPlaybackRateToAll() {
  allVideoElements().forEach(applyPlaybackRate);
}

function updateSpeedButton() {
  const button = $("#speed-toggle");
  if (!button) return;
  const slow = state.videoPlaybackRate === 0.5;
  button.textContent = slow ? "0.5x" : "1x";
  button.classList.toggle("active", slow);
  button.setAttribute("aria-pressed", slow ? "true" : "false");
  button.title = slow ? "Play videos at normal speed (S)" : "Play videos at half speed (S)";
}

function setVideoPlaybackRate(rate) {
  state.videoPlaybackRate = rate === 0.5 ? 0.5 : 1;
  try {
    localStorage.setItem("videoPlaybackRate", String(state.videoPlaybackRate));
  } catch (e) {
    // Ignore private-mode storage failures.
  }
  applyPlaybackRateToAll();
  updateSpeedButton();
}

function toggleVideoPlaybackRate() {
  setVideoPlaybackRate(state.videoPlaybackRate === 0.5 ? 1 : 0.5);
}

function videoElementReady(video) {
  return video && !video.error && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA;
}

function setVideoSrc(video, src) {
  if (!src) return;
  if (video.dataset.src !== src) {
    video.pause();
    video.dataset.ready = "0";
    video.src = src;
    video.dataset.src = src;
    applyPlaybackRate(video);
    video.load();
  }
}

function waitForDecodedVideoFrame(video, timeoutMs = 160) {
  if (!video || typeof video.requestVideoFrameCallback !== "function") return Promise.resolve(true);
  return new Promise((resolve) => {
    let done = false;
    const finish = (ok) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve(ok);
    };
    const timer = setTimeout(() => finish(false), timeoutMs);
    video.requestVideoFrameCallback(() => finish(true));
  });
}

async function waitForSeekedFrame(video, timeoutMs = 500) {
  const ready = () => video
    && !video.seeking
    && !video.error
    && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA;
  if (ready()) {
    await waitForDecodedVideoFrame(video);
    return true;
  }
  const ok = await new Promise((resolve) => {
    let done = false;
    const cleanup = () => {
      video.removeEventListener("seeked", onReady);
      video.removeEventListener("loadeddata", onReady);
      video.removeEventListener("canplay", onReady);
      video.removeEventListener("error", onError);
      clearTimeout(timer);
    };
    const finish = (ok) => {
      if (done) return;
      done = true;
      cleanup();
      resolve(ok);
    };
    const onReady = () => finish(ready());
    const onError = () => finish(false);
    const timer = setTimeout(() => finish(ready()), timeoutMs);
    video.addEventListener("seeked", onReady);
    video.addEventListener("loadeddata", onReady);
    video.addEventListener("canplay", onReady);
    video.addEventListener("error", onError);
  });
  if (ok) await waitForDecodedVideoFrame(video);
  return ok;
}

function currentVideoReadyForSide(side) {
  const data = state.pair && state.pair[side];
  const video = videoForSide(side);
  return Boolean(
    data
    && data.clip_status === "done"
    && data.clip
    && video
    && video.dataset.src === data.clip
    && videoElementReady(video)
  );
}

function comparisonVideosReady() {
  if (!state.pair || !state.pair.video_ready) return false;
  return currentVideoReadyForSide("left") && currentVideoReadyForSide("right");
}

function waitForVideoReady(video, timeoutMs = 7000) {
  if (videoElementReady(video)) return Promise.resolve(true);
  return new Promise((resolve) => {
    let done = false;
    const cleanup = () => {
      video.removeEventListener("loadeddata", onReady);
      video.removeEventListener("canplay", onReady);
      video.removeEventListener("error", onError);
      clearTimeout(timer);
    };
    const finish = (ok) => {
      if (done) return;
      done = true;
      cleanup();
      resolve(ok);
    };
    const onReady = () => finish(videoElementReady(video));
    const onError = () => finish(false);
    const timer = setTimeout(() => finish(videoElementReady(video)), timeoutMs);
    video.addEventListener("loadeddata", onReady);
    video.addEventListener("canplay", onReady);
    video.addEventListener("error", onError);
    video.load();
  });
}

function top3Videos() {
  return [0, 1].map((i) => $(`#top3-clip-${i}`));
}

function compressionVideos() {
  return [0, 1].map((i) => $(`#compression-clip-${i}`));
}

function setTop3VideoSrc(video, src) {
  if (!src) return;
  if (video.dataset.src !== src) {
    video.pause();
    video.src = src;
    video.dataset.src = src;
    applyPlaybackRate(video);
    video.load();
  }
}

function top3Ready() {
  if (!state.top3 || !state.top3.ready) return false;
  return state.top3.sides.every((side, i) => {
    const video = $(`#top3-clip-${i}`);
    return side.clip && video.dataset.src === side.clip && videoElementReady(video);
  });
}

function isTop3Open() {
  return !$("#top3").hidden;
}

function compressionReady() {
  if (!state.compression || !state.compression.ready) return false;
  return state.compression.sides.every((side, i) => {
    const video = $(`#compression-clip-${i}`);
    return side.clip && video.dataset.src === side.clip && videoElementReady(video);
  });
}

function isCompressionOpen() {
  return !$("#compression").hidden;
}

function stopTop3Polling() {
  if (state.top3Polling) {
    clearInterval(state.top3Polling);
    state.top3Polling = null;
  }
}

function stopCompressionPolling() {
  if (state.compressionPolling) {
    clearInterval(state.compressionPolling);
    state.compressionPolling = null;
  }
}

function stopTop3Videos() {
  top3Videos().forEach((video) => {
    if (!video) return;
    video.pause();
    try {
      video.currentTime = 0;
    } catch (e) {
      // Ignore videos without metadata yet.
    }
  });
  state.top3LoopStarted = false;
}

function stopCompressionVideos() {
  compressionVideos().forEach((video) => {
    video.pause();
    try {
      video.currentTime = 0;
    } catch (e) {
      // Ignore videos that have not loaded metadata yet.
    }
  });
  state.compressionLoopStarted = false;
}

function sideStatus(side) {
  const data = state.pair[side];
  return state.compareMode === "video" ? (data.clip_status || "pending") : data.status;
}

function sideError(side) {
  const data = state.pair[side];
  return state.compareMode === "video" ? data.clip_error : data.error;
}

function sideHasMedia(side) {
  const data = state.pair[side];
  if (state.compareMode === "video") return comparisonVideosReady();
  return data.status === "done" && data.image;
}

function setEnabled(on) {
  const currentMediaReady = state.compareMode !== "video"
    || state.peekingOriginal
    || comparisonVideosReady();
  const canChoose = on && !state.peekingOriginal && currentMediaReady && !state.switchingVideo;
  const canUseViewer = state.compareMode === "video" && state.pair && !state.peekingOriginal
    ? canChoose
    : on && !state.playingSide;
  document.querySelectorAll(".pick").forEach((b) => (b.disabled = !canChoose));
  $("#tie").disabled = !canChoose;
  $("#image-toggle").disabled = !state.pair
    || state.busy
    || (!state.peekingOriginal && !pairReadyForMode())
    || !canUseViewer;
}

function updateModeButtons() {
  document.querySelectorAll(".mode-option").forEach((button) => {
    const active = button.dataset.mode === state.compareMode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function updatePairingButtons() {
  document.querySelectorAll(".pairing-option").forEach((button) => {
    const active = button.dataset.pairing === state.pairingMode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function sideModeLabel(side) {
  const data = state.pair && state.pair[side];
  if (!data) return sideLabel[side];
  return data.mode_label || data.mode_id || sideLabel[side];
}

function updateReveal() {
  const button = $("#reveal-toggle");
  const note = $("#identity-note");
  const topNote = $("#top3-identity-note");
  const compressionNote = $("#compression-identity-note");
  if (!button || !note) return;

  button.classList.toggle("active", state.revealModes);
  button.setAttribute("aria-pressed", state.revealModes ? "true" : "false");
  button.textContent = state.revealModes ? "Hide A/B" : "Reveal A/B";

  note.replaceChildren();
  if (topNote) topNote.replaceChildren();
  if (compressionNote) compressionNote.replaceChildren();

  if (topNote) {
    if (state.revealModes && isTop3Open() && state.top3) {
      state.top3.sides.forEach((side) => {
        const item = document.createElement("span");
        item.textContent = `${side.key}: ${side.mode_label || side.mode_id || side.key}`;
        item.title = side.mode_id || side.mode_label || side.key;
        topNote.appendChild(item);
      });
      topNote.hidden = false;
    } else {
      topNote.hidden = true;
    }
  }

  if (compressionNote) {
    if (state.revealModes && isCompressionOpen() && state.compression) {
      state.compression.sides.forEach((side) => {
        const item = document.createElement("span");
        const settings = side.settings || {};
        const tune = settings.tune ? ` + tune ${settings.tune}` : "";
        item.textContent = `${side.key}: ${side.variant_label || side.variant_id}${tune}`;
        item.title = side.variant_id || side.variant_label || side.key;
        compressionNote.appendChild(item);
      });
      compressionNote.hidden = false;
    } else {
      compressionNote.hidden = true;
    }
  }

  if (!state.revealModes || !state.pair) {
    note.hidden = true;
    return;
  }

  ["left", "right"].forEach((side) => {
    const item = document.createElement("span");
    item.textContent = `${sideLabel[side]}: ${sideModeLabel(side)}`;
    item.title = state.pair[side].mode_id || sideModeLabel(side);
    note.appendChild(item);
  });
  note.hidden = false;
}

function toggleRevealModes() {
  state.revealModes = !state.revealModes;
  updateReveal();
}

function updateCurrentView() {
  const p = state.pair;
  if (!p) return;

  ["left", "right"].forEach((side) => {
    const data = p[side];
    const img = side === "left" ? $("#img-left") : $("#img-right");
    const video = side === "left" ? $("#clip-left") : $("#clip-right");
    const shown = !state.peekingOriginal && side === state.currentSide;
    const showImage = state.compareMode === "image" && shown && data.status === "done" && data.image;
    const mountVideo = state.compareMode === "video" && !state.peekingOriginal && data.clip_status === "done" && data.clip;
    const showVideo = mountVideo && shown && currentVideoReadyForSide(side);

    if (data.status === "done" && data.image) img.src = data.image;
    if (data.clip_status === "done" && data.clip) setVideoSrc(video, data.clip);

    img.hidden = !showImage;
    video.hidden = !mountVideo;
    img.classList.toggle("active", showImage);
    video.classList.toggle("active", showVideo);
  });

  const originalShown = state.peekingOriginal && p.original.status === "done" && p.original.image;
  const original = $("#img-original");
  if (p.original.status === "done" && p.original.image) original.src = p.original.image;
  original.hidden = !originalShown;
  original.classList.toggle("active", Boolean(originalShown));

  const current = state.peekingOriginal ? p.original : p[state.currentSide];
  const label = state.peekingOriginal ? "O" : sideLabel[state.currentSide];
  const nextLabel = sideLabel[otherSide[state.currentSide]];
  const spin = $("#spin-current");
  const canVote = pairReadyForMode(p) && !state.busy && !state.peekingOriginal && (state.compareMode !== "video" || comparisonVideosReady());
  const currentStatus = state.peekingOriginal ? current.status : sideStatus(state.currentSide);
  const currentError = state.peekingOriginal ? current.error : sideError(state.currentSide);
  const hasCurrentMedia = state.peekingOriginal
    ? current.status === "done" && current.image
    : sideHasMedia(state.currentSide);

  $("#viewer").dataset.side = state.peekingOriginal ? "original" : state.currentSide;
  $("#viewer").dataset.mode = state.compareMode;
  $("#current-key").textContent = label;
  $("#pick-current").dataset.pick = state.currentSide;
  $("#pick-current").textContent = state.peekingOriginal
    ? "Original source"
    : `Choose ${label} as better`;
  $("#pick-current").disabled = !canVote;
  $("#tie").disabled = !canVote;

  if (state.peekingOriginal) {
    $("#image-toggle").title = "Showing original source; click to return";
  } else if (state.compareMode === "video") {
    if (!pairReadyForMode(p)) {
      $("#image-toggle").title = "Rendering video clips";
    } else if (!comparisonVideosReady()) {
      $("#image-toggle").title = "Loading A/B clips";
    } else {
      $("#image-toggle").title = `Showing ${label}; click to switch to ${nextLabel} at the same timestamp`;
    }
  } else {
    $("#image-toggle").title = `Showing ${label}; click to show ${nextLabel}`;
  }

  $("#loading-note").textContent = state.compareMode === "video"
    ? " - rendering video clips..."
    : " - rendering frames...";
  $("#loading-note").hidden = pairDisplayReady(p);

  if (hasCurrentMedia) {
    spin.hidden = true;
    spin.classList.remove("err");
  } else if (typeof currentStatus === "string" && currentStatus.startsWith("error")) {
    spin.hidden = false;
    spin.classList.add("err");
    spin.querySelector("span").textContent = state.peekingOriginal
      ? "render error (original) - will retry on reload"
      : `render error (${label}) - will retry on reload`;
    if (currentError) console.error(currentError);
  } else {
    spin.hidden = false;
    spin.classList.remove("err");
    if (state.peekingOriginal) {
      spin.querySelector("span").textContent = "rendering original...";
    } else if (state.compareMode === "video") {
      spin.querySelector("span").textContent = pairReadyForMode(p)
        ? "loading A/B clips..."
        : "rendering A/B clips...";
    } else {
      spin.querySelector("span").textContent = `rendering ${label}...`;
    }
  }

  if (state.compareMode === "video" && !state.peekingOriginal && comparisonVideosReady()) {
    startComparisonVideoLoop();
  }
  setEnabled(pairReadyForMode(p) && !state.busy);
  updateReveal();
}

function showStackSide(side) {
  if (!state.pair || state.peekingOriginal) return;
  if (state.compareMode === "video") {
    switchComparisonVideoSide(side);
    return;
  } else {
    stopVideos();
  }
  state.currentSide = side;
  state.nextPlaybackSide = side;
  updateCurrentView();
}

async function startComparisonVideoLoop() {
  if (!comparisonVideosReady()) return;
  const videos = [videoForSide("left"), videoForSide("right")];
  if (!state.videoLoopStarted) {
    state.videoLoopStarted = true;
    videos.forEach((video) => {
      try {
        video.currentTime = 0;
      } catch (e) {
        // Ignore videos still settling.
      }
    });
  }
  try {
    videos.forEach(applyPlaybackRate);
    await Promise.all(videos.map((video) => video.play()));
  } catch (e) {
    console.error(e);
  }
}

function syncComparisonVideos(referenceTime) {
  ["left", "right"].forEach((side) => {
    const video = videoForSide(side);
    if (!videoElementReady(video)) return;
    try {
      if (Math.abs(video.currentTime - referenceTime) > 0.06) {
        video.currentTime = referenceTime;
      }
      if (video.paused) video.play().catch((e) => console.error(e));
    } catch (e) {
      console.error(e);
    }
  });
}

async function switchComparisonVideoSide(side) {
  if (!state.pair || state.busy || state.switchingVideo || side === state.currentSide) return;
  if (!comparisonVideosReady()) {
    startComparisonVideoLoop();
    return;
  }
  state.switchingVideo = true;
  const currentVideo = videoForSide(state.currentSide);
  const targetVideo = videoForSide(side);
  const referenceTime = currentVideo ? currentVideo.currentTime : 0;
  try {
    if (targetVideo && Math.abs(targetVideo.currentTime - referenceTime) > 0.04) {
      targetVideo.currentTime = referenceTime;
      await waitForSeekedFrame(targetVideo);
    }
    if (targetVideo && targetVideo.paused) await targetVideo.play();
    state.currentSide = side;
    state.nextPlaybackSide = side;
    updateCurrentView();
    const oldVideo = currentVideo;
    if (oldVideo && Math.abs(oldVideo.currentTime - targetVideo.currentTime) > 0.08) {
      try {
        oldVideo.currentTime = targetVideo.currentTime;
      } catch (e) {
        console.error(e);
      }
    }
  } catch (e) {
    console.error(e);
  } finally {
    state.switchingVideo = false;
    updateCurrentView();
  }
}

function toggleComparisonVideoSide() {
  switchComparisonVideoSide(otherSide[state.currentSide]);
}

function toggleStackSide() {
  if (!state.pair) return;
  if (state.peekingOriginal) {
    setOriginalPeek(false);
    return;
  }
  if (state.compareMode === "video") {
    toggleComparisonVideoSide();
    return;
  }
  showStackSide(otherSide[state.currentSide]);
}

function setOriginalPeek(on) {
  if (!state.pair || state.peekingOriginal === on) return;
  if (on) stopVideos();
  state.peekingOriginal = on;
  updateCurrentView();
  if (on && state.pair.original.status !== "done") startPolling();
}

function renderPair(p) {
  const isNewPair = !state.pair || p.pair_id !== state.pair.pair_id;
  if (isNewPair) stopVideos();
  state.pair = p;
  if (p.pairing_mode) {
    state.pairingMode = p.pairing_mode;
    updatePairingButtons();
  }
  if (isNewPair) {
    state.currentSide = "left";
    state.nextPlaybackSide = "left";
    state.peekingOriginal = false;
  }
  $("#video-label").textContent = `${p.video.label} - frame ${p.frame}`;
  updateCurrentView();
  $("#progress").textContent = `${p.progress ? p.progress.votes : 0} votes`;
}

async function loadPair() {
  const p = await fetchJSON(pairURL());
  renderPair(p);
  if (!pairDisplayReady(p)) startPolling();
  else stopPolling();
  refreshMiniLeader();
}

function updateTop3View() {
  const p = state.top3;
  if (!p) return;

  p.sides.forEach((side, i) => {
    const video = $(`#top3-clip-${i}`);
    if (side.clip_status === "done" && side.clip) setTop3VideoSrc(video, side.clip);
    video.classList.toggle("active", i === state.top3Index);
  });

  const side = p.sides[state.top3Index];
  const ready = top3Ready();
  const errorSide = p.sides.find((s) => typeof s.clip_status === "string" && s.clip_status.startsWith("error"));
  $("#top3-current-key").textContent = side ? side.key : "A";
  $("#top3-pick").textContent = side ? `Choose ${side.key} as best` : "Choose current as best";
  $("#top3-pick").disabled = !ready || state.top3Busy || state.top3Switching;
  $("#top3-skip").disabled = !p || state.top3Busy || state.top3Switching;
  $("#top3-viewer").disabled = !ready || state.top3Busy || state.top3Switching;
  $("#top3-loading-note").hidden = ready;
  $("#top3-spinner").hidden = ready;
  const prefetch = p.prefetch ? `, ${p.prefetch.queued}/${p.prefetch.target} queued` : "";
  $("#top3-votes").textContent = p.progress ? `- ${p.progress.votes} votes${prefetch}` : "";

  if (errorSide) {
    $("#top3-spinner").hidden = false;
    $("#top3-spinner").classList.add("err");
    $("#top3-spinner span").textContent = `render error (${errorSide.key}) - will retry on reload`;
    if (errorSide.clip_error) console.error(errorSide.clip_error);
  } else if (!ready) {
    $("#top3-spinner").classList.remove("err");
    $("#top3-spinner span").textContent = p.ready
      ? "loading focused pair clips..."
      : "rendering focused pair clips...";
  } else {
    $("#top3-spinner").classList.remove("err");
    startTop3Loop();
  }
  updateReveal();
}

function renderTop3(p) {
  const isNew = !state.top3 || p.top3_id !== state.top3.top3_id;
  if (isNew) {
    stopTop3Videos();
    state.top3Index = 0;
  }
  state.top3 = p;
  $("#top3-video-label").textContent = `${p.video.label} - frame ${p.frame}`;
  updateTop3View();
}

async function loadTop3() {
  const p = await fetchJSON("/api/top3");
  renderTop3(p);
  if (!p.ready) startTop3Polling();
  else stopTop3Polling();
  refreshMiniLeader();
}

function startTop3Polling() {
  if (state.top3Polling) return;
  state.top3Polling = setInterval(async () => {
    try {
      const p = await fetchJSON("/api/top3");
      if (state.top3 && p.top3_id === state.top3.top3_id) {
        renderTop3(p);
        if (p.ready) stopTop3Polling();
      }
    } catch (e) {
      // Keep polling while render jobs finish.
    }
  }, 700);
}

async function startTop3Loop() {
  if (!top3Ready()) return;
  const videos = top3Videos();
  if (!state.top3LoopStarted) {
    state.top3LoopStarted = true;
    videos.forEach((video) => {
      try {
        video.currentTime = 0;
      } catch (e) {
        // Ignore videos still settling.
      }
    });
  }
  try {
    videos.forEach(applyPlaybackRate);
    await Promise.all(videos.map((video) => video.play()));
  } catch (e) {
    // Muted autoplay should usually work; a click on the viewer will retry.
    console.error(e);
  }
}

function syncTop3Videos(referenceTime) {
  top3Videos().forEach((video) => {
    if (!videoElementReady(video)) return;
    try {
      if (Math.abs(video.currentTime - referenceTime) > 0.06) {
        video.currentTime = referenceTime;
      }
      if (video.paused) video.play().catch((e) => console.error(e));
    } catch (e) {
      console.error(e);
    }
  });
}

async function cycleTop3() {
  if (!state.top3 || state.top3Busy || state.top3Switching) return;
  if (!top3Ready()) {
    startTop3Loop();
    return;
  }
  state.top3Switching = true;
  startTop3Loop();
  const currentVideo = top3Videos()[state.top3Index];
  const nextIndex = (state.top3Index + 1) % state.top3.sides.length;
  const targetVideo = top3Videos()[nextIndex];
  const referenceTime = currentVideo ? currentVideo.currentTime : 0;
  try {
    if (targetVideo && Math.abs(targetVideo.currentTime - referenceTime) > 0.04) {
      targetVideo.currentTime = referenceTime;
      await waitForSeekedFrame(targetVideo);
    }
    if (targetVideo && targetVideo.paused) await targetVideo.play();
    state.top3Index = nextIndex;
    updateTop3View();
    syncTop3Videos(targetVideo ? targetVideo.currentTime : referenceTime);
  } catch (e) {
    console.error(e);
  } finally {
    state.top3Switching = false;
    updateTop3View();
  }
}

async function voteTop3() {
  if (!state.top3 || !top3Ready() || state.top3Busy) return;
  const side = state.top3.sides[state.top3Index];
  state.top3Busy = true;
  updateTop3View();
  try {
    await fetchJSON("/api/top3-vote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        top3_id: state.top3.top3_id,
        winner_key: side.key,
      }),
    });
    state.top3Busy = false;
    await loadTop3();
  } catch (e) {
    state.top3Busy = false;
    updateTop3View();
    console.error(e);
  }
}

async function skipTop3() {
  if (!state.top3 || state.top3Busy) return;
  state.top3Busy = true;
  updateTop3View();
  try {
    await fetchJSON("/api/top3-vote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        top3_id: state.top3.top3_id,
        winner_key: "skip",
      }),
    });
    state.top3Busy = false;
    await loadTop3();
  } catch (e) {
    state.top3Busy = false;
    updateTop3View();
    console.error(e);
  }
}

function updateCompressionView() {
  const p = state.compression;
  if (!p) return;

  p.sides.forEach((side, i) => {
    const video = $(`#compression-clip-${i}`);
    if (side.status === "done" && side.clip) setTop3VideoSrc(video, side.clip);
    video.classList.toggle("active", i === state.compressionIndex);
  });

  const side = p.sides[state.compressionIndex];
  const ready = compressionReady();
  const errorSide = p.sides.find((s) => typeof s.status === "string" && s.status.startsWith("error"));
  $("#compression-current-key").textContent = side ? side.key : "A";
  $("#compression-pick").textContent = side ? `Choose ${side.key} as best` : "Choose current as best";
  $("#compression-pick").disabled = !ready || state.compressionBusy || state.compressionSwitching;
  $("#compression-skip").disabled = !p || state.compressionBusy || state.compressionSwitching;
  $("#compression-viewer").disabled = !ready || state.compressionBusy || state.compressionSwitching;
  $("#compression-loading-note").hidden = ready;
  $("#compression-spinner").hidden = ready;
  const prefetch = p.prefetch ? `, ${p.prefetch.queued}/${p.prefetch.target} queued` : "";
  const scene = p.scene && p.scene.luma !== null && p.scene.luma !== undefined
    ? `, ${p.scene.kind} luma ${p.scene.luma}`
    : "";
  $("#compression-votes").textContent = p.progress ? `- ${p.progress.votes} votes${prefetch}${scene}` : "";

  if (errorSide) {
    $("#compression-spinner").hidden = false;
    $("#compression-spinner").classList.add("err");
    $("#compression-spinner span").textContent = `render error (${errorSide.key}) - will retry on reload`;
    if (errorSide.error) console.error(errorSide.error);
  } else if (!ready) {
    $("#compression-spinner").classList.remove("err");
    $("#compression-spinner span").textContent = p.ready
      ? "loading MP4 pair clips..."
      : "rendering MP4 pair clips...";
  } else {
    $("#compression-spinner").classList.remove("err");
    startCompressionLoop();
  }
  updateReveal();
}

function renderCompression(p) {
  const isNew = !state.compression || p.compression_id !== state.compression.compression_id;
  if (isNew) {
    stopCompressionVideos();
    state.compressionIndex = 0;
  }
  state.compression = p;
  const base = p.base_label ? ` via ${shortLabel(p.base_label)}` : "";
  $("#compression-video-label").textContent = `${p.video.label} - frame ${p.frame}${base}`;
  updateCompressionView();
}

async function loadCompression() {
  const p = await fetchJSON("/api/compression");
  renderCompression(p);
  if (!p.ready) startCompressionPolling();
  else stopCompressionPolling();
  refreshMiniLeader();
}

function startCompressionPolling() {
  if (state.compressionPolling) return;
  state.compressionPolling = setInterval(async () => {
    try {
      const p = await fetchJSON("/api/compression");
      if (state.compression && p.compression_id === state.compression.compression_id) {
        renderCompression(p);
        if (p.ready) stopCompressionPolling();
      }
    } catch (e) {
      // Keep polling while render jobs finish.
    }
  }, 900);
}

async function startCompressionLoop() {
  if (!compressionReady()) return;
  const videos = compressionVideos();
  if (!state.compressionLoopStarted) {
    state.compressionLoopStarted = true;
    videos.forEach((video) => {
      try {
        video.currentTime = 0;
      } catch (e) {
        // Ignore videos still settling.
      }
    });
  }
  try {
    videos.forEach(applyPlaybackRate);
    await Promise.all(videos.map((video) => video.play()));
  } catch (e) {
    console.error(e);
  }
}

function syncCompressionVideos(referenceTime) {
  compressionVideos().forEach((video) => {
    if (!videoElementReady(video)) return;
    try {
      if (Math.abs(video.currentTime - referenceTime) > 0.06) {
        video.currentTime = referenceTime;
      }
      if (video.paused) video.play().catch((e) => console.error(e));
    } catch (e) {
      console.error(e);
    }
  });
}

async function cycleCompression() {
  if (!state.compression || state.compressionBusy || state.compressionSwitching) return;
  if (!compressionReady()) {
    startCompressionLoop();
    return;
  }
  state.compressionSwitching = true;
  startCompressionLoop();
  const currentVideo = compressionVideos()[state.compressionIndex];
  const nextIndex = (state.compressionIndex + 1) % state.compression.sides.length;
  const targetVideo = compressionVideos()[nextIndex];
  const referenceTime = currentVideo ? currentVideo.currentTime : 0;
  try {
    if (targetVideo && Math.abs(targetVideo.currentTime - referenceTime) > 0.04) {
      targetVideo.currentTime = referenceTime;
      await waitForSeekedFrame(targetVideo);
    }
    if (targetVideo && targetVideo.paused) await targetVideo.play();
    state.compressionIndex = nextIndex;
    updateCompressionView();
    syncCompressionVideos(targetVideo ? targetVideo.currentTime : referenceTime);
  } catch (e) {
    console.error(e);
  } finally {
    state.compressionSwitching = false;
    updateCompressionView();
  }
}

async function voteCompression() {
  if (!state.compression || !compressionReady() || state.compressionBusy) return;
  const side = state.compression.sides[state.compressionIndex];
  state.compressionBusy = true;
  updateCompressionView();
  try {
    await fetchJSON("/api/compression-vote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        compression_id: state.compression.compression_id,
        winner_key: side.key,
      }),
    });
    state.compressionBusy = false;
    await loadCompression();
  } catch (e) {
    state.compressionBusy = false;
    updateCompressionView();
    console.error(e);
  }
}

async function skipCompression() {
  if (!state.compression || state.compressionBusy) return;
  state.compressionBusy = true;
  updateCompressionView();
  try {
    await fetchJSON("/api/compression-vote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        compression_id: state.compression.compression_id,
        winner_key: "skip",
      }),
    });
    state.compressionBusy = false;
    await loadCompression();
  } catch (e) {
    state.compressionBusy = false;
    updateCompressionView();
    console.error(e);
  }
}

function startPolling() {
  if (state.polling) return;
  state.polling = setInterval(async () => {
    try {
      const p = await fetchJSON(pairURL());
      if (state.pair && p.pair_id === state.pair.pair_id) {
        renderPair(p);
        if (pairDisplayReady(p)) stopPolling();
      }
    } catch (e) {
      // Keep polling while render jobs finish.
    }
  }, 700);
}

function stopPolling() {
  if (state.polling) {
    clearInterval(state.polling);
    state.polling = null;
  }
}

async function vote(winner) {
  if (!state.pair || !pairReadyForMode() || state.busy || state.playingSide) return;
  stopVideos();
  state.busy = true;
  setEnabled(false);
  const pair_id = state.pair.pair_id;
  try {
    await fetchJSON("/api/vote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        pair_id,
        winner,
        media_type: state.compareMode === "video" ? "video" : "photo",
      }),
    });
    state.busy = false;
    await loadPair();
  } catch (e) {
    state.busy = false;
    updateCurrentView();
    console.error(e);
  }
}

async function undo() {
  stopPolling();
  stopVideos();
  stopTop3Polling();
  stopTop3Videos();
  stopCompressionPolling();
  stopCompressionVideos();
  await fetchJSON("/api/undo", { method: "POST" });
  state.busy = false;
  state.top3Busy = false;
  state.compressionBusy = false;
  if (isTop3Open()) await loadTop3();
  else if (isCompressionOpen()) await loadCompression();
  else await loadPair();
}

async function reset() {
  if (!confirm("Wipe all ratings and votes?")) return;
  stopPolling();
  stopVideos();
  stopTop3Polling();
  stopTop3Videos();
  stopCompressionPolling();
  stopCompressionVideos();
  await fetchJSON("/api/reset", { method: "POST" });
  state.busy = false;
  state.top3Busy = false;
  state.compressionBusy = false;
  if (isTop3Open()) await loadTop3();
  else if (isCompressionOpen()) await loadCompression();
  else await loadPair();
}

function setZoom(on) {
  state.zoomed = on;
  document.body.classList.toggle("zoomed", on);
  $("#zoom-toggle").textContent = on ? "Zoom 200%" : "Zoom 100%";
}

function setCompareMode(mode) {
  if (!["image", "video"].includes(mode) || state.compareMode === mode) return;
  stopPolling();
  stopVideos();
  state.compareMode = mode;
  state.nextPlaybackSide = state.currentSide;
  try {
    localStorage.setItem("compareMode", mode);
  } catch (e) {
    // Ignore private-mode storage failures.
  }
  updateModeButtons();
  if (state.pair) updateCurrentView();
  loadPair().catch((err) => {
    console.error(err);
    $("#video-label").textContent = "Failed to load: " + err.message;
  });
}

async function setPairingMode(mode) {
  if (!["adaptive", "random"].includes(mode) || state.pairingMode === mode || state.busy) return;
  stopPolling();
  stopVideos();
  state.pairingMode = mode;
  updatePairingButtons();
  try {
    await fetchJSON("/api/pairing-mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode,
        media_type: state.compareMode === "video" ? "video" : "photo",
      }),
    });
    await loadPair();
  } catch (e) {
    console.error(e);
    await loadPair();
  }
}

function togglePairingMode() {
  setPairingMode(state.pairingMode === "random" ? "adaptive" : "random");
}

function setPreviewStatus(text, isError = false) {
  const el = $("#preview-status");
  el.textContent = text;
  el.classList.toggle("err", isError);
}

function setPreviewBusy(on) {
  state.previewBusy = on;
  $("#preview-mode").disabled = on;
  $("#preview-mode-b").disabled = on;
  $("#preview-frame").disabled = on;
  $("#preview-file").disabled = on;
  $("#preview-drop").classList.toggle("busy", on);
}

function stopPreviewPolling() {
  if (state.previewPolling) {
    clearInterval(state.previewPolling);
    state.previewPolling = null;
  }
}

function previewVideos() {
  return [$("#preview-original-video"), $("#preview-video"), $("#preview-b-video")];
}

function chooseDifferentPreviewB() {
  const select = $("#preview-mode");
  const selectB = $("#preview-mode-b");
  if (!select || !selectB || selectB.value !== select.value) return;
  const options = Array.from(selectB.options);
  const replacement = options.find((opt) => opt.value !== select.value);
  if (replacement) selectB.value = replacement.value;
}

function hidePreviewPlayers() {
  $("#preview-players").hidden = true;
  previewVideos().forEach((video) => {
    video.pause();
    video.removeAttribute("src");
    video.load();
  });
}

function syncPreviewVideos(source) {
  previewVideos().forEach((video) => {
    if (!video || video === source || !video.src) return;
    try {
      if (Math.abs(video.currentTime - source.currentTime) > 0.08) {
        video.currentTime = source.currentTime;
      }
      if (!source.paused && video.paused) video.play().catch(() => {});
      if (source.paused && !video.paused) video.pause();
    } catch (e) {
      console.error(e);
    }
  });
}

function renderPreviewJob(job) {
  if (!job) return;
  state.previewJobId = job.job_id;
  const video = $("#preview-video");
  const videoB = $("#preview-b-video");
  const original = $("#preview-original-video");
  if (job.status === "done" && job.clip && job.original_clip) {
    stopPreviewPolling();
    setPreviewBusy(false);
    const hasB = Boolean(job.clip_b && job.mode_b_label);
    setPreviewStatus(hasB
      ? `Preview ready: ${shortLabel(job.mode_label)} vs ${shortLabel(job.mode_b_label)}.`
      : `Preview ready: original vs ${shortLabel(job.mode_label)}.`);
    $("#preview-rendered-label").textContent = `A: ${shortLabel(job.mode_label)}`;
    $("#preview-rendered-b-label").textContent = hasB ? `B: ${shortLabel(job.mode_b_label)}` : "Deinterlacer B";
    original.src = job.original_clip;
    video.src = job.clip;
    if (hasB) videoB.src = job.clip_b;
    applyPlaybackRate(original);
    applyPlaybackRate(video);
    applyPlaybackRate(videoB);
    $("#preview-b-card").hidden = !hasB;
    $("#preview-players").hidden = false;
    original.load();
    video.load();
    if (hasB) videoB.load();
    Promise.all(previewVideos().filter((v) => v.src).map((v) => v.play())).catch(() => {
      // Controls are visible if autoplay is blocked.
    });
  } else if (job.status === "error") {
    stopPreviewPolling();
    setPreviewBusy(false);
    hidePreviewPlayers();
    setPreviewStatus(`Preview render failed: ${job.error || "unknown error"}`, true);
  } else {
    setPreviewBusy(true);
    hidePreviewPlayers();
    let phase = "Rendering deinterlacer A";
    if (job.status === "rendering_b") phase = "Rendering deinterlacer B";
    if (job.status === "rendering_original") phase = "Rendering original reference";
    const bText = job.mode_b_label ? ` vs ${shortLabel(job.mode_b_label)}` : "";
    setPreviewStatus(`${phase} for ${job.file_name}: ${shortLabel(job.mode_label)}${bText}...`);
  }
}

function startPreviewPolling(jobId) {
  stopPreviewPolling();
  state.previewPolling = setInterval(async () => {
    try {
      const job = await fetchJSON(`/api/preview/${jobId}`);
      renderPreviewJob(job);
    } catch (e) {
      stopPreviewPolling();
      setPreviewBusy(false);
      setPreviewStatus("Preview status failed: " + e.message, true);
    }
  }, 900);
}

async function loadPreviewModes() {
  const select = $("#preview-mode");
  const selectB = $("#preview-mode-b");
  const previous = select.value;
  const previousB = selectB.value;
  const d = await fetchJSON("/api/results");
  const rankedModes = d.rankings && d.rankings.combined && d.rankings.combined.vote_count
    ? d.rankings.combined.ranking
    : d.modes;

  const fillSelect = (target) => {
    target.replaceChildren();
    rankedModes.forEach((mode) => {
      const opt = document.createElement("option");
      opt.value = mode.id;
      opt.textContent = mode.rank ? `${mode.rank}. ${mode.label}` : mode.label;
      target.appendChild(opt);
    });
  };
  fillSelect(select);
  fillSelect(selectB);

  try {
    const saved = localStorage.getItem("previewMode");
    const savedB = localStorage.getItem("previewModeB");
    select.value = previous || saved || "vslow_default";
    selectB.value = previousB || savedB || "";
  } catch (e) {
    select.value = previous || "vslow_default";
    selectB.value = previousB || "";
  }
  if (!select.value && select.options.length) select.selectedIndex = 0;
  if ((!selectB.value || selectB.value === select.value) && selectB.options.length) {
    const fallback = rankedModes.find((mode) => mode.id !== select.value);
    selectB.value = fallback ? fallback.id : selectB.options[0].value;
  }
}

async function uploadPreviewFile(file) {
  if (!file || state.previewBusy) return;
  const mode = $("#preview-mode").value;
  const modeB = $("#preview-mode-b").value;
  if (!mode) {
    setPreviewStatus("Pick a deinterlacer first.", true);
    return;
  }
  if (!modeB || modeB === mode) {
    setPreviewStatus("Pick two different deinterlacers for A and B.", true);
    return;
  }
  const frame = Math.max(0, parseInt($("#preview-frame").value || "0", 10) || 0);
  stopPreviewPolling();
  setPreviewBusy(true);
  hidePreviewPlayers();
  setPreviewStatus(`Uploading ${file.name} for preview...`);
  try {
    try {
      localStorage.setItem("previewMode", mode);
      localStorage.setItem("previewModeB", modeB);
    } catch (e) {
      // Ignore private-mode storage failures.
    }
    const job = await fetchJSON("/api/preview", {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        "X-Mode-Id": mode,
        "X-Mode-B-Id": modeB,
        "X-Frame": String(frame),
        "X-File-Name": encodeURIComponent(file.name || "preview"),
      },
      body: file,
    });
    renderPreviewJob(job);
    if (job.status !== "done" && job.status !== "error") startPreviewPolling(job.job_id);
  } catch (e) {
    setPreviewBusy(false);
    setPreviewStatus("Preview upload failed: " + e.message, true);
  }
}

async function refreshMiniLeader() {
  try {
    const d = await fetchJSON("/api/results");
    if (!d.vote_count) {
      $("#mini-leader").textContent = "";
      return;
    }
    const combined = d.rankings ? d.rankings.combined.ranking : d.ranking;
    const top = combined.slice(0, 3)
      .map((r, i) => `${i === 0 ? "<b>" : ""}${r.rank}. ${shortLabel(r.label)} (${Math.round(r.elo)})${i === 0 ? "</b>" : ""}`)
      .join("  /  ");
    $("#mini-leader").innerHTML = `Combined leading: ${top}`;
  } catch (e) {
    // Ignore transient result refresh failures.
  }
}

function shortLabel(label) {
  return label.split("(")[0].trim();
}

function fmtCount(x) {
  return Number.isInteger(x) ? String(x) : x.toFixed(1);
}

function renderRankingTable(body, title, note, rankingData) {
  const section = document.createElement("section");
  section.className = "ranking-section";
  const h3 = document.createElement("h3");
  h3.className = "section";
  h3.textContent = title;
  section.appendChild(h3);

  const meta = document.createElement("p");
  meta.className = "ranking-note";
  meta.textContent = note;
  section.appendChild(meta);

  if (!rankingData || !rankingData.vote_count) {
    const empty = document.createElement("p");
    empty.className = "ranking-empty";
    empty.textContent = `No ${title.replace(" ranking", "").toLowerCase()} votes yet.`;
    section.appendChild(empty);
    body.appendChild(section);
    return;
  }

  const ranking = rankingData.ranking;
  const maxElo = Math.max(...ranking.map((r) => r.elo));
  const minElo = Math.min(...ranking.map((r) => r.elo));
  const span = Math.max(1, maxElo - minElo);
  const t = document.createElement("table");
  t.className = "ranking";
  t.innerHTML = `<thead><tr>
    <th>#</th><th>Mode</th><th class="num">Elo</th><th class="num">Games</th><th>Strength</th>
  </tr></thead>`;
  const tb = document.createElement("tbody");
  ranking.forEach((r) => {
    const tr = document.createElement("tr");
    if (r.rank === 1) tr.className = "top";
    const w = 30 + 170 * ((r.elo - minElo) / span);
    tr.innerHTML = `
      <td class="num">${r.rank}</td>
      <td title="${r.id}">${r.label}</td>
      <td class="num">${r.elo.toFixed(0)}</td>
      <td class="num">${fmtCount(r.games)}</td>
      <td><span class="bar" style="width:${w}px"></span></td>`;
    tb.appendChild(tr);
  });
  t.appendChild(tb);
  section.appendChild(t);
  body.appendChild(section);
}

async function showResults() {
  stopPolling();
  stopVideos();
  stopTop3Polling();
  stopTop3Videos();
  stopCompressionPolling();
  stopCompressionVideos();
  const d = await fetchJSON("/api/results");
  $("#compare").hidden = true;
  $("#top3").hidden = true;
  $("#compression").hidden = true;
  $("#results").hidden = false;
  const counts = d.media_counts || { photo: d.vote_count, video: 0, top_pairs: 0, compression: 0, combined_weight: d.vote_count };
  $("#results-votes").textContent =
    `- ${counts.photo} photo, ${counts.video} video, ${counts.top_pairs || 0} focused, ${counts.compression || 0} compression, ${fmtCount(counts.combined_weight)} combined weight`;
  const body = $("#results-body");
  body.innerHTML = "";

  if (!d.vote_count) {
    body.innerHTML = "<p style='color:var(--muted)'>No votes yet - go cast a few.</p>";
    return;
  }

  const rankings = d.rankings || {
    combined: { ranking: d.ranking, vote_count: d.vote_count, weighted_votes: d.vote_count },
    top_pairs: { ranking: [], vote_count: 0, weighted_votes: 0, mode_count: 4 },
    compression: { ranking: [], vote_count: 0, weighted_votes: 0, variant_count: 6 },
    video: { ranking: d.ranking, vote_count: 0, weighted_votes: 0 },
    photo: { ranking: d.ranking, vote_count: d.vote_count, weighted_votes: d.vote_count },
  };
  renderRankingTable(
    body,
    "Ranking 1 - Global",
    `All comparison votes. Video votes count double, photo votes count once. Weighted votes: ${fmtCount(rankings.combined.weighted_votes)}.`,
    rankings.combined,
  );
  renderRankingTable(
    body,
    `Ranking 2 - Focused ${rankings.top_pairs.mode_count || 4} pairs`,
    "Only votes cast on the focused pairwise page. Each pairwise vote counts once.",
    rankings.top_pairs,
  );
  renderRankingTable(
    body,
    `Ranking 3 - Compression ${rankings.compression.variant_count || 6}`,
    "Only blind votes comparing MP4 x264 settings after Slower deinterlacing. Each compression vote counts once.",
    rankings.compression,
  );
  renderRankingTable(
    body,
    "Video ranking",
    "Only votes cast while comparing one-second video clips.",
    rankings.video,
  );
  renderRankingTable(
    body,
    "Photo ranking",
    "Only votes cast while comparing still frames. Older votes are counted here.",
    rankings.photo,
  );

  const order = rankings.combined.ranking.map((r) => r.id);
  const idLabel = {};
  d.modes.forEach((m) => (idLabel[m.id] = shortLabel(m.label)));
  const h3 = document.createElement("h3");
  h3.className = "section";
  h3.textContent = "Combined head-to-head (video x2, photo x1)";
  body.appendChild(h3);

  const mt = document.createElement("table");
  mt.className = "matrix";
  let head = "<tr><th></th>";
  order.forEach((id, i) => (head += `<th>${i + 1}</th>`));
  head += "</tr>";
  let rows = "";
  order.forEach((rid, ri) => {
    rows += `<tr><td class="rowhead">${ri + 1}. ${idLabel[rid]}</td>`;
    order.forEach((cid) => {
      if (rid === cid) {
        rows += `<td class="diag">-</td>`;
        return;
      }
      const w = d.win_matrix[rid][cid] || 0;
      const l = d.win_matrix[cid][rid] || 0;
      const txt = (w || l) ? `${fmt(w)}-${fmt(l)}` : "";
      rows += `<td>${txt}</td>`;
    });
    rows += "</tr>";
  });
  mt.innerHTML = head + rows;
  body.appendChild(mt);

  const det = document.createElement("details");
  det.innerHTML = `<summary>Vote log (${d.votes.length} shown)</summary><div id="votelog"></div>`;
  body.appendChild(det);
  det.querySelector("#votelog").textContent = d.votes.slice().reverse().map((v) => {
    const win = v.winner === "tie" ? "tie"
      : (v.winner === "left" ? v.left_mode : v.right_mode);
    const media = v.media_type || "photo";
    return `${v.at.slice(0, 19).replace("T", " ")}  [${media}]  ${v.video_id} f${v.frame}  ${v.left_mode} vs ${v.right_mode}  ->  ${win}`;
  }).join("\n");
}

function fmt(x) {
  return Number.isInteger(x) ? x : x.toFixed(1);
}

function hideResults() {
  $("#results").hidden = true;
  $("#top3").hidden = true;
  $("#compression").hidden = true;
  $("#compare").hidden = false;
  loadPair();
}

function showTop3() {
  stopPolling();
  stopVideos();
  stopCompressionPolling();
  stopCompressionVideos();
  $("#compare").hidden = true;
  $("#results").hidden = true;
  $("#compression").hidden = true;
  $("#top3").hidden = false;
  loadTop3().catch((err) => {
    console.error(err);
    $("#top3-video-label").textContent = "Failed to load focused pairs: " + err.message;
  });
}

function hideTop3() {
  stopTop3Polling();
  stopTop3Videos();
  $("#top3").hidden = true;
  $("#compression").hidden = true;
  $("#results").hidden = true;
  $("#compare").hidden = false;
  loadPair();
}

function showCompression() {
  stopPolling();
  stopVideos();
  stopTop3Polling();
  stopTop3Videos();
  $("#compare").hidden = true;
  $("#results").hidden = true;
  $("#top3").hidden = true;
  $("#compression").hidden = false;
  loadCompression().catch((err) => {
    console.error(err);
    $("#compression-video-label").textContent = "Failed to load compression test: " + err.message;
  });
}

function hideCompression() {
  stopCompressionPolling();
  stopCompressionVideos();
  $("#compression").hidden = true;
  $("#top3").hidden = true;
  $("#results").hidden = true;
  $("#compare").hidden = false;
  loadPair();
}

document.querySelectorAll(".pick").forEach((b) =>
  b.addEventListener("click", () => vote(b.dataset.pick)));
document.querySelectorAll(".mode-option").forEach((b) =>
  b.addEventListener("click", () => setCompareMode(b.dataset.mode)));
document.querySelectorAll(".pairing-option").forEach((b) =>
  b.addEventListener("click", () => setPairingMode(b.dataset.pairing)));
["left", "right"].forEach((side) => {
  const video = videoForSide(side);
  ["loadeddata", "canplay", "error"].forEach((eventName) => {
    video.addEventListener(eventName, () => {
      if (eventName === "error") console.error(video.error);
      if (state.pair) updateCurrentView();
    });
  });
});
[0, 1].forEach((i) => {
  const video = $(`#top3-clip-${i}`);
  ["loadeddata", "canplay", "error"].forEach((eventName) => {
    video.addEventListener(eventName, () => {
      if (eventName === "error") console.error(video.error);
      if (state.top3) updateTop3View();
    });
  });
});
[0, 1].forEach((i) => {
  const video = $(`#compression-clip-${i}`);
  ["loadeddata", "canplay", "error"].forEach((eventName) => {
    video.addEventListener(eventName, () => {
      if (eventName === "error") console.error(video.error);
      if (state.compression) updateCompressionView();
    });
  });
});
$("#image-toggle").addEventListener("click", toggleStackSide);
$("#top3-viewer").addEventListener("click", cycleTop3);
$("#top3-pick").addEventListener("click", voteTop3);
$("#top3-skip").addEventListener("click", skipTop3);
$("#compression-viewer").addEventListener("click", cycleCompression);
$("#compression-pick").addEventListener("click", voteCompression);
$("#compression-skip").addEventListener("click", skipCompression);
$("#tie").addEventListener("click", () => vote("tie"));
$("#undo").addEventListener("click", undo);
$("#reset").addEventListener("click", reset);
$("#top3-btn").addEventListener("click", showTop3);
$("#top3-back").addEventListener("click", hideTop3);
$("#compression-btn").addEventListener("click", showCompression);
$("#compression-back").addEventListener("click", hideCompression);
$("#samples-btn").addEventListener("click", () => {
  window.location.href = "/web/final-samples.html";
});
$("#results-btn").addEventListener("click", showResults);
$("#back").addEventListener("click", hideResults);
$("#zoom-toggle").addEventListener("click", () => setZoom(!state.zoomed));
$("#reveal-toggle").addEventListener("click", toggleRevealModes);
$("#speed-toggle").addEventListener("click", toggleVideoPlaybackRate);
$("#preview-mode").addEventListener("change", () => {
  chooseDifferentPreviewB();
  try {
    localStorage.setItem("previewMode", $("#preview-mode").value);
    localStorage.setItem("previewModeB", $("#preview-mode-b").value);
  } catch (e) {
    // Ignore private-mode storage failures.
  }
});
$("#preview-mode-b").addEventListener("change", () => {
  if ($("#preview-mode-b").value === $("#preview-mode").value) chooseDifferentPreviewB();
  try {
    localStorage.setItem("previewModeB", $("#preview-mode-b").value);
  } catch (e) {
    // Ignore private-mode storage failures.
  }
});
$("#preview-file").addEventListener("change", (e) => {
  const file = e.target.files && e.target.files[0];
  uploadPreviewFile(file);
  e.target.value = "";
});
["dragenter", "dragover"].forEach((eventName) => {
  $("#preview-drop").addEventListener(eventName, (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (!state.previewBusy) $("#preview-drop").classList.add("dragover");
  });
});
["dragleave", "drop"].forEach((eventName) => {
  $("#preview-drop").addEventListener(eventName, (e) => {
    e.preventDefault();
    e.stopPropagation();
    $("#preview-drop").classList.remove("dragover");
  });
});
$("#preview-drop").addEventListener("drop", (e) => {
  const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  uploadPreviewFile(file);
});
previewVideos().forEach((video) => {
  video.addEventListener("play", () => syncPreviewVideos(video));
  video.addEventListener("pause", () => syncPreviewVideos(video));
  video.addEventListener("seeked", () => syncPreviewVideos(video));
  video.addEventListener("timeupdate", () => syncPreviewVideos(video));
});

window.addEventListener("keydown", (e) => {
  if (isTypingTarget(e.target)) return;
  const resultsOpen = !$("#results").hidden;
  const top3Open = isTop3Open();
  const compressionOpen = isCompressionOpen();
  if (top3Open) {
    switch (e.key.toLowerCase()) {
      case " ":
        e.preventDefault();
        cycleTop3();
        break;
      case "enter":
        e.preventDefault();
        voteTop3();
        break;
      case "t":
        e.preventDefault();
        skipTop3();
        break;
      case "i":
        e.preventDefault();
        toggleRevealModes();
        break;
      case "u":
        e.preventDefault();
        undo();
        break;
      case "z":
        e.preventDefault();
        setZoom(!state.zoomed);
        break;
      case "s":
        e.preventDefault();
        toggleVideoPlaybackRate();
        break;
      case "r":
        e.preventDefault();
        showResults();
        break;
      case "escape":
        e.preventDefault();
        hideTop3();
        break;
      default:
        break;
    }
    return;
  }
  if (compressionOpen) {
    switch (e.key.toLowerCase()) {
      case " ":
        e.preventDefault();
        cycleCompression();
        break;
      case "enter":
        e.preventDefault();
        voteCompression();
        break;
      case "t":
        e.preventDefault();
        skipCompression();
        break;
      case "i":
        e.preventDefault();
        toggleRevealModes();
        break;
      case "u":
        e.preventDefault();
        undo();
        break;
      case "z":
        e.preventDefault();
        setZoom(!state.zoomed);
        break;
      case "s":
        e.preventDefault();
        toggleVideoPlaybackRate();
        break;
      case "r":
        e.preventDefault();
        showResults();
        break;
      case "escape":
        e.preventDefault();
        hideCompression();
        break;
      default:
        break;
    }
    return;
  }
  switch (e.key.toLowerCase()) {
    case "arrowleft":
    case "a":
      if (!resultsOpen) {
        e.preventDefault();
        showStackSide("left");
      }
      break;
    case "arrowright":
    case "d":
      if (!resultsOpen) {
        e.preventDefault();
        showStackSide("right");
      }
      break;
    case " ":
      if (!resultsOpen) {
        e.preventDefault();
        toggleStackSide();
      }
      break;
    case "enter":
      if (!resultsOpen) {
        e.preventDefault();
        vote(state.currentSide);
      }
      break;
    case "o":
      if (!resultsOpen && !e.repeat) {
        e.preventDefault();
        setOriginalPeek(true);
      }
      break;
    case "t":
      if (!resultsOpen) {
        e.preventDefault();
        vote("tie");
      }
      break;
    case "u":
      e.preventDefault();
      undo();
      break;
    case "z":
      e.preventDefault();
      setZoom(!state.zoomed);
      break;
    case "s":
      e.preventDefault();
      toggleVideoPlaybackRate();
      break;
    case "i":
      if (!resultsOpen) {
        e.preventDefault();
        toggleRevealModes();
      }
      break;
    case "p":
      if (!resultsOpen) {
        e.preventDefault();
        togglePairingMode();
      }
      break;
    case "r":
      e.preventDefault();
      resultsOpen ? hideResults() : showResults();
      break;
    case "v":
      if (!resultsOpen) {
        e.preventDefault();
        setCompareMode(state.compareMode === "video" ? "image" : "video");
      }
      break;
  }
});

window.addEventListener("keyup", (e) => {
  if (e.key.toLowerCase() === "o") {
    e.preventDefault();
    setOriginalPeek(false);
  }
});

window.addEventListener("blur", () => {
  setOriginalPeek(false);
  stopVideos();
  stopTop3Videos();
  stopCompressionVideos();
  if (state.pair) updateCurrentView();
  if (state.top3) updateTop3View();
  if (state.compression) updateCompressionView();
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    setOriginalPeek(false);
    stopVideos();
    stopTop3Videos();
    stopCompressionVideos();
    if (state.pair) updateCurrentView();
    if (state.top3) updateTop3View();
    if (state.compression) updateCompressionView();
  }
});

updateModeButtons();
updatePairingButtons();
updateReveal();
updateSpeedButton();
applyPlaybackRateToAll();
loadPreviewModes().catch((err) => {
  console.error(err);
  setPreviewStatus("Failed to load deinterlacer list: " + err.message, true);
});
loadPair().catch((err) => {
  console.error(err);
  $("#video-label").textContent = "Failed to load: " + err.message;
});
