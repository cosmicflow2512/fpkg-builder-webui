const form = document.querySelector("#build-form");
const runtime = document.querySelector("#runtime");
const log = document.querySelector("#log");
const jobState = document.querySelector("#job-state");
const startButton = document.querySelector("#start");
const cancelButton = document.querySelector("#cancel");
const compression = form.elements.compressionLevel;

let activeJobId = null;
let pollTimer = null;

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
  cancelButton.disabled = !running;
  if (!running) {
    clearInterval(pollTimer);
    pollTimer = null;
    activeJobId = null;
    await loadStatus();
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  startButton.disabled = true;
  log.textContent = "";
  try {
    const payload = await request("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(asPayload()),
    });
    activeJobId = payload.job.id;
    jobState.textContent = payload.job.status;
    cancelButton.disabled = false;
    pollTimer = setInterval(pollJob, 1200);
    await pollJob();
  } catch (error) {
    jobState.textContent = "Error";
    log.textContent = error.message;
    startButton.disabled = false;
  }
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
