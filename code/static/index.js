const DEFAULT_CAMERA = "Camera1";

const CAMERA_TYPE_LABELS = {
    USB: "USB camera",
    PICAMERA: "Pi camera",
};

// Browsers allow only ~6 connections per host, and the <img> stream holds one open.
// Release it while this tab is hidden so stream / snapshot links opened in other tabs can connect.
const BLANK_IMAGE = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==";

const streamImage = document.getElementById("camera-stream");
const cameraSelect = document.getElementById("camera-select");

// camera name -> camera from /api/cameras
let cameras = {};

async function loadCameras() {

    const response = await fetch("/api/cameras");

    const data = await response.json();

    cameras = {};
    for (const camera of data.cameras || []) {
        cameras[camera.name] = camera;
    }

    const names = Object.keys(cameras);
    const inUse = data.in_use || [];

    showInUseCameras(inUse);

    // Cameras that are only busy have been detected, so don't say none were.
    document.getElementById("empty-state").hidden = names.length > 0 || inUse.length > 0;
    document.getElementById("camera-view").hidden = names.length === 0;

    if (names.length === 0) {
        return;
    }

    cameraSelect.innerHTML = "";
    for (const name of names) {
        cameraSelect.appendChild(new Option(name, name));
    }

    cameraSelect.value = names.includes(DEFAULT_CAMERA) ? DEFAULT_CAMERA : names[0];
    showCamera(cameraSelect.value);
}

// Cameras skipped at startup because another program is using them.
function showInUseCameras(inUse) {
    const list = document.getElementById("in-use-list");
    list.replaceChildren(...inUse.map(camera => {
        const item = document.createElement("li");
        item.textContent = `${CAMERA_TYPE_LABELS[camera.cameratype] || camera.cameratype} ${camera.source}`;
        return item;
    }));
    document.getElementById("in-use-notice").hidden = inUse.length === 0;
}

function showCamera(cameraName) {

    const camera = cameras[cameraName];

    const streamUrl = new URL(`/${cameraName}/${camera.stream}`, window.location.href);
    const snapshotUrl = new URL(`/${cameraName}/${camera.snapshot}`, window.location.href);

    const streamLink = document.getElementById("stream-link");
    streamLink.href = streamUrl.href;
    streamLink.innerText = `Stream: ${streamUrl.href}`;

    const snapshotLink = document.getElementById("snapshot-link");
    snapshotLink.href = snapshotUrl.href;
    snapshotLink.innerText = `Snapshot: ${snapshotUrl.href}`;

    // WebKit keeps the previous MJPEG connection open when src changes; window.stop() aborts it.
    window.stop();
    streamImage.dataset.stream = streamUrl.href;
    streamImage.src = uniqueStreamUrl(streamImage.dataset.stream);
    streamImage.alt = cameraName;

    document.getElementById("camera-type").textContent = CAMERA_TYPE_LABELS[camera.cameratype] || camera.cameratype || "";

    showSettings(camera.settings || []);
}

// Settings fixed when the camera opens; changing one restarts the camera, so they are
// only sent once the edit is finished. Others are applied while the value is being changed.
const RESTART_SETTINGS = ["fps", "width", "height"];
const LIVE_UPDATE_DELAY_MS = 250;

// Changes are sent one at a time, in order.
let pendingUpdate = Promise.resolve();

function showSettings(settings) {

    const body = document.getElementById("settings-body");

    body.innerHTML = "";
    setStatus("");

    if (settings.length === 0) {
        const row = body.insertRow();
        const cell = row.insertCell();
        cell.colSpan = 4;
        cell.className = "no-settings";
        cell.textContent = "No settings available.";
        return;
    }

    for (const setting of settings) {
        const row = body.insertRow();
        row.insertCell().textContent = setting.setting;
        row.insertCell().textContent = `${formatValue(setting.min)} / ${formatValue(setting.max)}`;
        row.insertCell().textContent = formatValue(setting.default);
        row.insertCell().appendChild(createSettingInput(setting));
    }
}

function createSettingInput(setting) {

    let input;

    if (setting.choices) {
        input = document.createElement("select");
        for (const choice of setting.choices) {
            input.appendChild(new Option(formatValue(choice), choice));
        }
    } else {
        input = document.createElement("input");
        input.type = "number";
        input.step = setting.step === null ? "any" : setting.step;
        if (setting.min !== null) {
            input.min = setting.min;
        }
        if (setting.max !== null) {
            input.max = setting.max;
        }
    }

    input.className = "setting-input";
    input.dataset.setting = setting.setting;
    input.value = setting.current;
    input.setAttribute("aria-label", setting.setting);

    const cameraName = cameraSelect.value;

    if (RESTART_SETTINGS.includes(setting.setting)) {
        input.addEventListener("change", () => queueSettingUpdate(cameraName, input));
    } else {
        let timer = null;
        input.addEventListener("input", () => {
            clearTimeout(timer);
            timer = setTimeout(() => queueSettingUpdate(cameraName, input), LIVE_UPDATE_DELAY_MS);
        });
    }

    return input;
}

function queueSettingUpdate(cameraName, input) {

    const value = input.value;

    if (value === "" || Number.isNaN(Number(value))) {
        return;
    }

    pendingUpdate = pendingUpdate.then(() => sendSettingUpdate(cameraName, input.dataset.setting, Number(value)));
}

async function sendSettingUpdate(cameraName, setting, value) {

    const restarts = RESTART_SETTINGS.includes(setting);
    setStatus(restarts ? `Restarting ${cameraName} with new ${setting}...` : "");

    try {
        const response = await fetch("/api/camera-setting", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({name: cameraName, setting, value}),
        });
        const data = await response.json();

        if (data.status !== "success") {
            setStatus(data.message || `Could not change ${setting}`, true);
            // Show the value the camera is still using.
            refreshSettings(cameraName, cameras[cameraName].settings);
            return;
        }

        cameras[cameraName].settings = data.settings;
        if (restarts) {
            // The choices of the other settings depend on the new one (e.g. heights offered at a width).
            if (cameraSelect.value === cameraName) {
                showSettings(data.settings);
            }
        } else {
            refreshSettings(cameraName, data.settings);
        }

        const applied = data.settings.find((row) => row.setting === setting);
        if (applied && Number(applied.current) !== value) {
            setStatus(`${setting} adjusted to ${formatValue(applied.current)} (closest the camera supports)`);
        } else {
            setStatus("");
        }
    } catch (error) {
        setStatus(`Could not change ${setting}: ${error}`, true);
    }
}

function refreshSettings(cameraName, settings) {

    // The user may have switched to another camera while the change was applied.
    if (cameraSelect.value !== cameraName) {
        return;
    }

    for (const setting of settings) {
        const input = document.querySelector(`.setting-input[data-setting="${setting.setting}"]`);
        // Don't overwrite a value that is still being edited.
        if (input && (document.activeElement !== input || RESTART_SETTINGS.includes(setting.setting))) {
            input.value = setting.current;
        }
    }
}

function formatValue(value) {
    if (value === null || value === undefined) {
        return "-";
    }
    if (typeof value === "number" && !Number.isInteger(value)) {
        return value.toFixed(2).replace(/\.?0+$/, "");
    }
    return String(value);
}

function setStatus(message, isError = false) {
    const status = document.getElementById("settings-status");
    status.textContent = message;
    status.classList.toggle("error", isError);
}

// A stream response never completes, so browsers (notably WebKit / iOS) may hold or merge a second
// request for a URL already streaming in another tab. A unique query string forces a fresh connection.
function uniqueStreamUrl(stream) {
    const url = new URL(stream);
    url.searchParams.set("t", Date.now());
    return url.href;
}

cameraSelect.addEventListener("change", () => showCamera(cameraSelect.value));

document.addEventListener("visibilitychange", () => {

    if (document.hidden) {
        // WebKit (all iOS browsers) keeps an MJPEG connection open when an <img> src changes;
        // window.stop() is what actually aborts the in-flight stream loads.
        window.stop();
        streamImage.src = BLANK_IMAGE;
        return;
    }

    if (!streamImage.dataset.stream) {
        // Hidden before the initial camera list finished loading (window.stop() aborted it).
        loadCameras();
        return;
    }

    streamImage.src = uniqueStreamUrl(streamImage.dataset.stream);
});

loadCameras();
