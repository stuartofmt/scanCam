// The page is a Vue 3 app using Vuetify 4 (both in vendor, loaded by index.html); its template is #page in index.html.
const { createApp, reactive, ref, computed, onMounted } = Vue;

const DEFAULT_CAMERA = "Camera1";

const CAMERA_TYPE_LABELS = {
    USB: "USB camera",
    PICAMERA: "Pi camera",
};

// Browsers allow only ~6 connections per host, and the <img> stream holds one open.
// Release it while this tab is hidden so stream / snapshot links opened in other tabs can connect.
const BLANK_IMAGE = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==";

// Settings fixed when the camera opens; changing one restarts the camera, so they are
// only sent once the edit is finished. Others are applied while the value is being changed.
const RESTART_SETTINGS = ["fps", "width", "height"];
const LIVE_UPDATE_DELAY_MS = 250;

function typeLabel(cameraType) {
    return CAMERA_TYPE_LABELS[cameraType] || cameraType || "";
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

// A stream response never completes, so browsers (notably WebKit / iOS) may hold or merge a second
// request for a URL already streaming in another tab. A unique query string forces a fresh connection.
function uniqueStreamUrl(stream) {
    const url = new URL(stream);
    url.searchParams.set("t", Date.now());
    return url.href;
}

function setup() {

    // camera name -> camera from /api/cameras
    const cameras = reactive({});
    const names = ref([]);
    // Cameras skipped at startup because another program is using them.
    const inUse = ref([]);
    const loaded = ref(false);
    const selected = ref("");

    const streamImage = ref(null);
    const streamUrl = ref("");
    const snapshotUrl = ref("");
    let stream = "";

    // setting name -> the value in its input; focused is the setting whose input is being edited
    const values = reactive({});
    const focused = ref("");
    const status = reactive({text: "", error: false});

    const selectedCamera = computed(() => cameras[selected.value]);
    const settings = computed(() => (selectedCamera.value && selectedCamera.value.settings) || []);

    function setStatus(message, isError = false) {
        status.text = message;
        status.error = isError;
    }

    async function loadCameras() {

        const response = await fetch("/api/cameras");

        const data = await response.json();

        for (const name of Object.keys(cameras)) {
            delete cameras[name];
        }
        for (const camera of data.cameras || []) {
            cameras[camera.name] = camera;
        }

        names.value = Object.keys(cameras);
        inUse.value = data.in_use || [];
        loaded.value = true;

        if (names.value.length === 0) {
            return;
        }

        selected.value = names.value.includes(DEFAULT_CAMERA) ? DEFAULT_CAMERA : names.value[0];
        showCamera(selected.value);
    }

    function showCamera(cameraName) {

        const camera = cameras[cameraName];

        streamUrl.value = new URL(`/${cameraName}/${camera.stream}`, window.location.href).href;
        snapshotUrl.value = new URL(`/${cameraName}/${camera.snapshot}`, window.location.href).href;

        // WebKit keeps the previous MJPEG connection open when src changes; window.stop() aborts it.
        // Not before the first stream: it would also abort the page's own loads, such as the icon font.
        if (stream) {
            window.stop();
        }
        stream = streamUrl.value;
        streamImage.value.src = uniqueStreamUrl(stream);

        showSettings(camera.settings || []);
    }

    function showSettings(cameraSettings) {
        for (const name of Object.keys(values)) {
            delete values[name];
        }
        for (const setting of cameraSettings) {
            values[setting.setting] = setting.current;
            delete sent[setting.setting];
        }
        setStatus("");
    }

    // ----- sending changes -----

    // Changes are sent one at a time, in order.
    let pendingUpdate = Promise.resolve();
    // setting name -> timer of a live change waiting to be sent
    const timers = {};
    // setting name -> the value last sent for a setting that restarts the camera (Enter then leaving the box sends it once)
    const sent = {};

    function queueSettingUpdate(cameraName, setting, value) {

        if (value === "" || value === null || Number.isNaN(Number(value))) {
            return;
        }

        pendingUpdate = pendingUpdate.then(() => sendSettingUpdate(cameraName, setting, Number(value)));
    }

    function selectChanged(setting) {
        queueSettingUpdate(selected.value, setting.setting, values[setting.setting]);
    }

    // Live settings are sent once typing (or the arrows) pause.
    function numberTyped(setting) {
        if (RESTART_SETTINGS.includes(setting.setting)) {
            return;
        }
        const cameraName = selected.value, value = values[setting.setting];
        clearTimeout(timers[setting.setting]);
        timers[setting.setting] = setTimeout(() => queueSettingUpdate(cameraName, setting.setting, value), LIVE_UPDATE_DELAY_MS);
    }

    // Settings that restart the camera are sent when the edit is finished (Enter or leaving the box), if changed.
    function numberDone(setting) {
        focused.value = "";
        const value = Number(values[setting.setting]);
        if (RESTART_SETTINGS.includes(setting.setting) && value !== Number(setting.current) && value !== sent[setting.setting]) {
            sent[setting.setting] = value;
            queueSettingUpdate(selected.value, setting.setting, values[setting.setting]);
        }
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
                if (selected.value === cameraName) {
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

    function refreshSettings(cameraName, cameraSettings) {

        // The user may have switched to another camera while the change was applied.
        if (selected.value !== cameraName) {
            return;
        }

        for (const setting of cameraSettings) {
            // Don't overwrite a value that is still being edited.
            if (focused.value !== setting.setting || RESTART_SETTINGS.includes(setting.setting)) {
                values[setting.setting] = setting.current;
            }
            delete sent[setting.setting];
        }
    }

    // ----- the page's own life -----

    document.addEventListener("visibilitychange", () => {

        if (document.hidden) {
            // WebKit (all iOS browsers) keeps an MJPEG connection open when an <img> src changes;
            // window.stop() is what actually aborts the in-flight stream loads.
            window.stop();
            streamImage.value.src = BLANK_IMAGE;
            return;
        }

        if (!stream) {
            // Hidden before the initial camera list finished loading (window.stop() aborted it).
            loadCameras();
            return;
        }

        streamImage.value.src = uniqueStreamUrl(stream);
    });

    onMounted(loadCameras);

    return {
        names, inUse, loaded, selected, selectedCamera, settings, values, focused, status,
        streamImage, streamUrl, snapshotUrl,
        showCamera, selectChanged, numberTyped, numberDone, typeLabel, formatValue,
    };
}

// Tell scanCam when this page opens and closes; it exits once the last open page is closed.
// pagehide fires when the tab is closed, reloaded or navigated away from - not when it is
// hidden or loses focus. pageshow fires on load and when the page comes back from the back/forward cache.
const PAGE_ID = Math.random().toString(36).slice(2) + Date.now().toString(36);

function reportPage(state) {
    navigator.sendBeacon(`/api/page-${state}?id=${PAGE_ID}`);
}

window.addEventListener("pageshow", () => reportPage("opened"));
window.addEventListener("pagehide", () => reportPage("closed"));

const app = createApp({template: "#page", setup});
app.use(Vuetify.createVuetify({theme: {defaultTheme: "dark"}}));
app.mount("#app");
