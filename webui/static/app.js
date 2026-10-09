const form = document.querySelector("#build-form");
const extractForm = document.querySelector("#extract-form");
const runtime = document.querySelector("#runtime");
const log = document.querySelector("#log");
const jobState = document.querySelector("#job-state");
const startButton = document.querySelector("#start");
const extractButton = document.querySelector("#extract");
const cancelButton = document.querySelector("#cancel");
const compression = form.elements.compressionLevel;
const browserDialog = document.querySelector("#browser");
const browserPath = document.querySelector("#browser-path");
const browserRoots = document.querySelector("#browser-roots");
const browserList = document.querySelector("#browser-list");
const browserSelect = document.querySelector("#browser-select");

let activeJobId = null;
let pollTimer = null;
let browseTarget = null;
let browseType = "any";
let currentBrowsePath = "/";

for (let level = -4; level <= 9; level += 1) {
  const option = document.createElement("option");
  option.value = String(level);
  option.textContent = String(level);
  if (level === 7) option.selected = true;
  compression.append(option);
}

function asPayload() {
  const data = new FormData(form);
  return {
    gameFolder: data.get("gameFolder"),
    backportFolder: data.get("backportFolder"),
    outputFolder: data.get("outputFolder"),
    workFolder: data.get("workFolder"),
    name: data.get("name"),
    contentVersion: data.get("contentVersion"),
    toolkitRoot: data.get("toolkitRoot"),
    temporaryDirectory: data.get("temporaryDirectory"),
    compressionLevel: data.get("compressionLevel"),
    rebuildBase: data.get("rebuildBase") === "on",
    keepWork: data.get("keepWork") === "on",
    force: data.get("force") === "on",
  };
}

function asExtractPayload() {
  const data = new FormData(extractForm);
  return {
    kind: "extract",
    package: data.get("package"),
    destination: data.get("destination"),
  };
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || response.statusText);
  }
  return payload;
}

async function loadStatus() {
  const status = await request("/api/status");
  const missing = Object.entries(status.scripts)
    .filter(([, value]) => !value.exists)
    .map(([name]) => name);
  runtime.innerHTML = `
    <strong>${status.canStart ? "Ready" : "Needs configuration"}</strong>
    <span>PowerShell: ${status.powershell || "not found"}</span>
    <span>Root: ${status.root}</span>
    <span>Browse: ${status.browseRoots.join(", ")}</span>
    ${missing.length ? `<span>Missing: ${missing.join(", ")}</span>` : ""}
  `;
  startButton.disabled = !status.canStart;
}

async function pollJob() {
  if (!activeJobId) return;
  const payload = await request(`/api/jobs/${activeJobId}/log`);
  jobState.textContent = `${payload.job.status} ${payload.job.returnCode === null ? "" : `(exit ${payload.job.returnCode})`}`;
  log.textContent = payload.log.join("\n");
  log.scrollTop = log.scrollHeight;
  const running = payload.job.status === "queued" || payload.job.status === "running";
  startButton.disabled = running;
  extractButton.disabled = running;
  cancelButton.disabled = !running;
  if (!running) {
    clearInterval(pollTimer);
    pollTimer = null;
    activeJobId = null;
    await loadStatus();
  }
}

async function startJob(payload) {
  startButton.disabled = true;
  extractButton.disabled = true;
  log.textContent = "";
  try {
    const response = await request("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    activeJobId = response.job.id;
    jobState.textContent = response.job.status;
    cancelButton.disabled = false;
    pollTimer = setInterval(pollJob, 1200);
    await pollJob();
  } catch (error) {
    jobState.textContent = "Error";
    log.textContent = error.message;
    startButton.disabled = false;
    extractButton.disabled = false;
  }
}

function selectTab(name) {
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  form.classList.toggle("hidden", name !== "build");
  extractForm.classList.toggle("hidden", name !== "extract");
}

function setInputValue(name, value) {
  const input = document.querySelector(`[name="${name}"]`);
  if (input) input.value = value;
}

async function loadBrowse(path = currentBrowsePath) {
  const payload = await request(`/api/browse?path=${encodeURIComponent(path)}&type=${encodeURIComponent(browseType)}`);
  currentBrowsePath = payload.path;
  browserPath.textContent = payload.path;
  browserRoots.innerHTML = payload.roots
    .map((root) => `<button type="button" class="root" data-path="${root}">${root}</button>`)
    .join("");
  browserList.innerHTML = "";

  if (payload.parent) {
    const up = document.createElement("button");
    up.type = "button";
    up.className = "entry";
    up.textContent = "..";
    up.addEventListener("click", () => loadBrowse(payload.parent));
    browserList.append(up);
  }

  for (const entry of payload.entries) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = `entry ${entry.type}`;
    row.textContent = `${entry.type === "dir" ? "Folder" : "File"}  ${entry.name}`;
    row.addEventListener("click", () => {
      if (entry.type === "dir") {
        if (browseType === "dir") {
          currentBrowsePath = entry.path;
          browserPath.textContent = entry.path;
        }
        loadBrowse(entry.path);
      } else {
        setInputValue(browseTarget, entry.path);
        browserDialog.close();
      }
    });
    browserList.append(row);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  await startJob(asPayload());
});

extractForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await startJob(asExtractPayload());
});

cancelButton.addEventListener("click", async () => {
  if (!activeJobId) return;
  cancelButton.disabled = true;
  await request(`/api/jobs/${activeJobId}/cancel`, { method: "POST" });
  await pollJob();
});

loadStatus().catch((error) => {
  runtime.textContent = error.message;
  startButton.disabled = true;
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => selectTab(tab.dataset.tab));
});

document.querySelectorAll(".browse").forEach((button) => {
  button.addEventListener("click", async () => {
    browseTarget = button.dataset.target;
    browseType = button.dataset.type || "any";
    const input = document.querySelector(`[name="${browseTarget}"]`);
    currentBrowsePath = input?.value || "/mnt/user";
    browserDialog.showModal();
    await loadBrowse(currentBrowsePath).catch(async () => loadBrowse("/mnt/user"));
  });
});

browserRoots.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-path]");
  if (button) loadBrowse(button.dataset.path);
});

browserSelect.addEventListener("click", () => {
  setInputValue(browseTarget, currentBrowsePath);
  browserDialog.close();
});

document.querySelector("#browser-close").addEventListener("click", () => browserDialog.close());
