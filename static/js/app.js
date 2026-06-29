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

async function requestJson(url, options = {}) {
    const response = await fetch(url, {
        headers: { "Content-Type": "application/json", ...(options.headers || {}) },
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
                window.setTimeout(() => window.location.reload(), 1200);
            } else {
                showToast("Sync completed");
                window.setTimeout(() => window.location.reload(), 700);
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
            window.setTimeout(() => window.location.href = `/playlist/${data.playlist_id}`, 700);
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

    const deleteButton = event.target.closest(".js-delete");
    if (deleteButton) {
        if (!window.confirm("Delete this playlist from the archive?")) return;
        try {
            const data = await requestJson(`/playlist/${deleteButton.dataset.playlistId}`, { method: "DELETE" });
            showToast(data.message || "Playlist deleted");
            window.setTimeout(() => window.location.reload(), 700);
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
});

document.addEventListener("DOMContentLoaded", () => {
    refreshActiveDownloads();
    window.setInterval(refreshActiveDownloads, 1500);
});
