function showToast(message, tone = "success") {
    const area = document.getElementById("toastArea");
    if (!area) return;

    const toast = document.createElement("div");
    toast.className = `toast align-items-center text-bg-${tone} border-0`;
    toast.setAttribute("role", "alert");
    toast.innerHTML = `
        <div class="d-flex">
            <div class="toast-body">${message}</div>
            <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="Close"></button>
        </div>
    `;
    area.appendChild(toast);
    const instance = new bootstrap.Toast(toast, { delay: 3500 });
    instance.show();
    toast.addEventListener("hidden.bs.toast", () => toast.remove());
}

function readCookie(name) {
    const prefix = `${name}=`;
    return document.cookie
        .split(";")
        .map((item) => item.trim())
        .find((item) => item.startsWith(prefix))
        ?.slice(prefix.length) || "";
}

async function requestJson(url, options = {}) {
    const method = String(options.method || "GET").toUpperCase();
    const csrfCookieName = document.querySelector('meta[name="csrf-cookie-name"]')?.content || "ytarchive_csrf";
    const csrfHeaderName = document.querySelector('meta[name="csrf-header-name"]')?.content || "x-csrf-token";
    const csrfToken = method === "GET" ? "" : readCookie(csrfCookieName);
    const response = await fetch(url, {
        headers: {
            "Content-Type": "application/json",
            ...(csrfToken ? { [csrfHeaderName]: decodeURIComponent(csrfToken) } : {}),
            ...(options.headers || {}),
        },
        ...options,
    });
    if (response.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
        throw new Error("Authentication required");
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(data.detail || data.message || `Request failed with ${response.status}`);
    }
    return data;
}

function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function taskProgress(task) {
    return Math.max(0, Math.min(100, Number(task.progress || 0)));
}

function taskCountsText(task) {
    const total = Number(task.total || 0);
    const completed = Number(task.completed || 0);
    const failed = Number(task.failed || 0);
    const processed = completed + failed;
    if (!total) return "Preparing";
    return `${processed}/${total} processed | ${completed} downloaded | ${failed} failed`;
}

function taskStatusLabel(task) {
    if (task.status === "done" && Number(task.failed || 0) > 0) return "done with errors";
    return task.status;
}

function renderActiveDownloads(tasks) {
    const panel = document.getElementById("activeDownloads");
    const list = document.getElementById("activeDownloadsList");
    if (!panel || !list) return;

    if (!tasks.length) {
        panel.classList.add("d-none");
        list.innerHTML = "";
        return;
    }

    panel.classList.remove("d-none");
    list.innerHTML = tasks.map((task) => {
        const progress = taskProgress(task);
        const playlistTitle = task.playlist_title || `Playlist ${task.playlist_id || ""}`;
        const current = task.current_video ? ` | ${escapeHtml(task.current_video)}` : "";
        const tone = task.status === "failed" || Number(task.failed || 0) > 0 ? "bg-warning" : "bg-danger";
        const errors = Array.isArray(task.errors) ? task.errors : [];
        const visibleErrors = errors.slice(0, 5);
        const hiddenErrorCount = Math.max(0, errors.length - visibleErrors.length);
        const errorHtml = visibleErrors.length ? `
            <div class="active-download-errors">
                <div class="active-download-errors-title">
                    <i class="bi bi-exclamation-triangle me-1"></i>Failed videos
                </div>
                ${visibleErrors.map((item) => `
                    <div class="active-download-error-item">
                        <strong>${escapeHtml(item.title || "Unknown video")}</strong>
                        <span class="text-muted">(${escapeHtml(item.yt_video_id || "")})</span>:
                        ${escapeHtml(item.message || "Download failed")}
                    </div>
                `).join("")}
                ${hiddenErrorCount ? `<div class="active-download-error-item text-muted">+${hiddenErrorCount} more failed video(s)</div>` : ""}
            </div>
        ` : "";
        return `
            <div class="active-download-item" data-task-id="${escapeHtml(task.task_id)}">
                <div class="active-download-row">
                    <div>
                        <div class="active-download-title">${escapeHtml(playlistTitle)}</div>
                        <div class="active-download-meta">
                            ${escapeHtml(String(task.format || "").toUpperCase())}
                            | ${escapeHtml(taskStatusLabel(task))}
                            | ${escapeHtml(taskCountsText(task))}
                            ${current}
                        </div>
                    </div>
                    <a class="btn btn-sm btn-outline-secondary" href="/playlist/${task.playlist_id}" title="Open playlist">
                        <i class="bi bi-box-arrow-up-right"></i>
                    </a>
                </div>
                <div class="progress active-download-progress" role="progressbar" aria-label="Active download progress">
                    <div class="progress-bar ${tone}" style="width: ${progress}%"></div>
                </div>
                ${errorHtml}
            </div>
        `;
    }).join("");
}

async function refreshActiveDownloads() {
    if (!document.getElementById("activeDownloads")) return;

    try {
        const tasks = await requestJson("/tasks/active");
        renderActiveDownloads(tasks);

    } catch (error) {
        console.warn("Unable to refresh active downloads", error);
    }
}

async function pollTask(taskId) {
    let done = false;
    while (!done) {
        const task = await requestJson(`/task/${taskId}/status`);
        if (task.status === "done") {
            if (Number(task.failed || 0) > 0) {
                showToast(`Sync done: ${task.completed} downloaded, ${task.failed} failed`, "warning");
                window.setTimeout(() => refreshCurrentPage(), 1200);
            } else {
                showToast("Sync completed");
                window.setTimeout(() => refreshCurrentPage(), 700);
            }
            done = true;
        } else if (task.status === "failed") {
            showToast(task.error || "Sync failed", "danger");
            done = true;
        } else {
            await new Promise((resolve) => window.setTimeout(resolve, 1200));
        }
    }
}

const playbackState = {
    pageTracks: [],
    queue: [],
    currentIndex: -1,
    currentTrack: null,
};

function loadPlaybackSettings() {
    const defaults = { repeat: false, autoNext: true, shuffle: false };
    try {
        return { ...defaults, ...JSON.parse(window.localStorage.getItem("ytarchivePlaybackSettings") || "{}") };
    } catch {
        return defaults;
    }
}

const playbackSettings = loadPlaybackSettings();

function savePlaybackSettings() {
    try {
        window.localStorage.setItem("ytarchivePlaybackSettings", JSON.stringify(playbackSettings));
    } catch {
        // Playback still works when localStorage is unavailable.
    }
}

function normalizeSearch(value) {
    return String(value || "").trim().toLocaleLowerCase();
}

function updateVideoSearch() {
    const input = document.getElementById("videoSearch");
    const rows = Array.from(document.querySelectorAll(".js-video-row"));
    const count = document.getElementById("videoSearchCount");
    const empty = document.getElementById("videoSearchEmpty");
    if (!input || !rows.length) return;

    const query = normalizeSearch(input.value);
    let visibleCount = 0;

    rows.forEach((row) => {
        const searchableText = [
            row.dataset.title,
            row.dataset.channel,
            row.dataset.ytId,
        ].join(" ");
        const matches = !query || normalizeSearch(searchableText).includes(query);
        row.classList.toggle("d-none", !matches);
        if (matches) visibleCount += 1;
    });

    if (count) count.textContent = `${visibleCount}/${rows.length} videos shown`;
    if (empty) empty.classList.toggle("d-none", visibleCount > 0);
    syncVisiblePlaybackState();
}

function refreshPlaybackTracks() {
    playbackState.pageTracks = Array.from(document.querySelectorAll(".js-play-media")).map((button) => ({
        id: button.dataset.videoId || button.dataset.streamUrl,
        button,
        row: button.closest(".js-video-row"),
        title: button.dataset.title || "Untitled track",
        url: button.dataset.streamUrl,
    })).filter((track) => track.url);
    syncVisiblePlaybackState();
}

function visiblePlaybackTracks() {
    const visible = playbackState.pageTracks.filter((track) => !track.row?.classList.contains("d-none"));
    return visible.length ? visible : playbackState.pageTracks;
}

function toPlayableTrack(track) {
    return {
        id: track.id || track.url,
        title: track.title || "Untitled track",
        url: track.url,
    };
}

function trackKey(track) {
    if (!track?.url) return "";
    try {
        const url = new URL(track.url, window.location.origin);
        return `${url.pathname}${url.search}`;
    } catch {
        return track.url;
    }
}

function sameTrack(left, right) {
    return Boolean(left && right && trackKey(left) === trackKey(right));
}

function getPlayerElements() {
    return {
        bar: document.getElementById("globalPlayerBar"),
        media: document.getElementById("globalPlayerMedia"),
        title: document.getElementById("globalPlayerTitle"),
    };
}

function showGlobalPlayer() {
    const { bar } = getPlayerElements();
    if (!bar) return;
    bar.classList.remove("d-none");
    document.body.classList.add("has-player-bar");
}

function stopPlayback() {
    const { bar, media, title } = getPlayerElements();
    if (media) {
        media.pause();
        media.removeAttribute("src");
        media.load();
    }
    if (title) title.textContent = "No track selected";
    if (bar) bar.classList.add("d-none");
    document.body.classList.remove("has-player-bar");
    playbackState.currentTrack = null;
    playbackState.currentIndex = -1;
    syncVisiblePlaybackState();
}

function applyPlaybackSettingsToMedia() {
    const { media } = getPlayerElements();
    if (media) media.loop = playbackSettings.repeat;
}

function syncVisiblePlaybackState() {
    playbackState.pageTracks.forEach((item) => {
        const active = sameTrack(item, playbackState.currentTrack);
        item.row?.classList.toggle("table-active", active);
        item.button.classList.toggle("btn-danger", active);
        item.button.classList.toggle("btn-outline-secondary", !active);
        item.button.innerHTML = active ? '<i class="bi bi-volume-up"></i>' : '<i class="bi bi-play-circle"></i>';
    });
}

function resetQueueFromPage(track) {
    const visible = visiblePlaybackTracks();
    const source = visible.some((item) => sameTrack(item, track)) ? visible : playbackState.pageTracks;
    playbackState.queue = source.map(toPlayableTrack);
    playbackState.currentIndex = playbackState.queue.findIndex((item) => sameTrack(item, track));
    if (playbackState.currentIndex < 0) {
        playbackState.queue = [toPlayableTrack(track)];
        playbackState.currentIndex = 0;
    }
}

async function playTrack(track, options = {}) {
    const { resetQueue = true } = options;
    const { media, title } = getPlayerElements();
    if (!track || !track.url || !media || !title) return;

    if (resetQueue) resetQueueFromPage(track);

    const playableTrack = toPlayableTrack(track);
    playbackState.currentTrack = playableTrack;
    playbackState.currentIndex = playbackState.queue.findIndex((item) => sameTrack(item, playableTrack));
    if (playbackState.currentIndex < 0) {
        playbackState.queue = [playableTrack];
        playbackState.currentIndex = 0;
    }

    const absoluteUrl = new URL(playableTrack.url, window.location.origin).href;
    if (media.src !== absoluteUrl) {
        media.src = playableTrack.url;
    }
    title.textContent = playableTrack.title;
    applyPlaybackSettingsToMedia();
    showGlobalPlayer();
    syncVisiblePlaybackState();

    try {
        await media.play();
    } catch (error) {
        console.warn("Media playback was blocked", error);
        showToast("Press play in the media bar to start playback", "warning");
    }
}

function nextTrack(direction = 1) {
    const sequence = playbackState.queue.length ? playbackState.queue : visiblePlaybackTracks().map(toPlayableTrack);
    if (!sequence.length) return null;

    const current = playbackState.currentTrack || sequence[playbackState.currentIndex];

    if (playbackSettings.shuffle && direction > 0 && sequence.length > 1) {
        const choices = sequence.filter((track) => !sameTrack(track, current));
        return choices[Math.floor(Math.random() * choices.length)];
    }

    const currentInSequence = sequence.findIndex((track) => sameTrack(track, current));
    const startIndex = currentInSequence >= 0 ? currentInSequence : 0;
    const nextIndex = (startIndex + direction + sequence.length) % sequence.length;
    playbackState.currentIndex = nextIndex;
    return sequence[nextIndex];
}

async function handleTrackEnded() {
    const { media } = getPlayerElements();

    if (playbackSettings.repeat && media) {
        media.currentTime = 0;
        await media.play();
        return;
    }

    if (playbackSettings.autoNext) {
        await playTrack(nextTrack(1), { resetQueue: false });
    }
}

function bindPlaybackOptions() {
    const options = [
        ["playbackRepeat", "repeat"],
        ["playbackAutoNext", "autoNext"],
        ["playbackShuffle", "shuffle"],
    ];
    options.forEach(([elementId, settingKey]) => {
        const input = document.getElementById(elementId);
        if (!input) return;
        input.checked = Boolean(playbackSettings[settingKey]);
        input.addEventListener("change", () => {
            playbackSettings[settingKey] = input.checked;
            savePlaybackSettings();
            applyPlaybackSettingsToMedia();
        });
    });
}

let globalPlayerInitialized = false;

function initGlobalPlayer() {
    if (globalPlayerInitialized) return;
    globalPlayerInitialized = true;

    const { media } = getPlayerElements();
    if (media) media.addEventListener("ended", handleTrackEnded);

    document.getElementById("globalPlayerPrev")?.addEventListener("click", () => playTrack(nextTrack(-1), { resetQueue: false }));
    document.getElementById("globalPlayerNext")?.addEventListener("click", () => playTrack(nextTrack(1), { resetQueue: false }));
    document.getElementById("globalPlayerStop")?.addEventListener("click", stopPlayback);
    document.getElementById("globalPlayerClose")?.addEventListener("click", stopPlayback);
}

function initPlaylistTools() {
    const search = document.getElementById("videoSearch");
    const clearSearch = document.getElementById("clearVideoSearch");

    if (search) search.addEventListener("input", updateVideoSearch);
    if (clearSearch) {
        clearSearch.addEventListener("click", () => {
            search.value = "";
            updateVideoSearch();
            search.focus();
        });
    }

    bindPlaybackOptions();
    refreshPlaybackTracks();
    updateVideoSearch();
}

function shouldHandleSoftNavigation(event, link) {
    if (!link || event.defaultPrevented || event.button !== 0) return false;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;
    if (link.target && link.target !== "_self") return false;
    if (link.hasAttribute("download") || link.dataset.noSoftNav !== undefined) return false;

    const rawHref = link.getAttribute("href");
    if (!rawHref || rawHref.startsWith("#")) return false;

    const url = new URL(rawHref, window.location.href);
    if (url.origin !== window.location.origin) return false;
    if (url.pathname.startsWith("/stream/")) return false;
    if (url.pathname === "/login" || url.pathname === "/register") return false;

    return true;
}

function collapseTopNav() {
    const nav = document.getElementById("topNav");
    if (!nav?.classList.contains("show") || !window.bootstrap) return;
    bootstrap.Collapse.getOrCreateInstance(nav, { toggle: false }).hide();
}

async function navigateTo(url, options = {}) {
    const { push = true, scrollToTop = true } = options;
    const nextUrl = new URL(url, window.location.href);
    const response = await fetch(nextUrl.href, {
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" },
    });

    const finalUrl = new URL(response.url || nextUrl.href, window.location.href);
    if (finalUrl.pathname === "/login" || finalUrl.pathname === "/register") {
        window.location.href = finalUrl.href;
        return;
    }
    if (!response.ok) {
        throw new Error(`Navigation failed with ${response.status}`);
    }

    const html = await response.text();
    const nextDocument = new DOMParser().parseFromString(html, "text/html");
    const nextContent = nextDocument.getElementById("pageContent");
    const currentContent = document.getElementById("pageContent");
    if (!nextContent || !currentContent) {
        window.location.href = nextUrl.href;
        return;
    }

    document.title = nextDocument.title || document.title;
    currentContent.innerHTML = nextContent.innerHTML;
    if (push) {
        window.history.pushState({ softNavigation: true }, "", finalUrl.href);
    }
    collapseTopNav();
    initPlaylistTools();
    await refreshActiveDownloads();
    if (scrollToTop) window.scrollTo(0, 0);
}

async function refreshCurrentPage() {
    try {
        await navigateTo(window.location.href, { push: false, scrollToTop: false });
    } catch (error) {
        console.warn("Soft refresh failed; falling back to full reload", error);
        window.location.reload();
    }
}

document.addEventListener("submit", async (event) => {
    if (event.target.id === "addPlaylistForm") {
        event.preventDefault();
        const input = document.getElementById("playlistUrl");
        try {
            const data = await requestJson("/playlist/add", {
                method: "POST",
                body: JSON.stringify({ url: input.value }),
            });
            showToast(data.message || "Playlist added");
            window.setTimeout(() => {
                navigateTo(`/playlist/${data.playlist_id}`).catch(() => {
                    window.location.href = `/playlist/${data.playlist_id}`;
                });
            }, 700);
        } catch (error) {
            showToast(error.message, "danger");
        }
    }

    if (event.target.id === "syncIntervalForm") {
        event.preventDefault();
        const input = document.getElementById("syncHours");
        try {
            const data = await requestJson("/settings/sync-interval", {
                method: "PATCH",
                body: JSON.stringify({ hours: Number(input.value) }),
            });
            showToast(data.message || "Settings saved");
        } catch (error) {
            showToast(error.message, "danger");
        }
    }
});

document.addEventListener("click", async (event) => {
    if (event.target.closest("#activeDownloadsRefresh")) {
        await refreshActiveDownloads();
        return;
    }

    const syncButton = event.target.closest(".js-sync");
    if (syncButton) {
        syncButton.disabled = true;
        try {
            const data = await requestJson(`/playlist/${syncButton.dataset.playlistId}/sync`, {
                method: "POST",
                body: JSON.stringify({ format: syncButton.dataset.format || "mp3" }),
            });
            showToast("Sync queued");
            await refreshActiveDownloads();
            await pollTask(data.task_id);
        } catch (error) {
            showToast(error.message, "danger");
        } finally {
            syncButton.disabled = false;
        }
    }

    const playButton = event.target.closest(".js-play-media");
    if (playButton) {
        refreshPlaybackTracks();
        const track = playbackState.pageTracks.find((item) => item.button === playButton);
        await playTrack(track);
        return;
    }

    const deleteButton = event.target.closest(".js-delete");
    if (deleteButton) {
        if (!window.confirm("Delete this playlist from the archive?")) return;
        try {
            const data = await requestJson(`/playlist/${deleteButton.dataset.playlistId}`, { method: "DELETE" });
            showToast(data.message || "Playlist deleted");
            window.setTimeout(() => refreshCurrentPage(), 700);
        } catch (error) {
            showToast(error.message, "danger");
        }
    }

    const stepButton = event.target.closest(".js-step");
    if (stepButton) {
        const input = document.getElementById("syncHours");
        const next = Number(input.value) + Number(stepButton.dataset.step);
        input.value = Math.min(168, Math.max(1, next));
    }

    const link = event.target.closest("a");
    if (shouldHandleSoftNavigation(event, link)) {
        event.preventDefault();
        try {
            await navigateTo(link.href);
        } catch (error) {
            console.warn("Soft navigation failed; falling back to full navigation", error);
            window.location.href = link.href;
        }
    }
});

window.addEventListener("popstate", () => {
    navigateTo(window.location.href, { push: false }).catch((error) => {
        console.warn("History navigation failed; falling back to full navigation", error);
        window.location.reload();
    });
});

document.addEventListener("DOMContentLoaded", () => {
    window.history.replaceState({ softNavigation: true }, "", window.location.href);
    initGlobalPlayer();
    initPlaylistTools();
    refreshActiveDownloads();
    window.setInterval(refreshActiveDownloads, 1500);
});
