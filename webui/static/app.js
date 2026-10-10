const jobForm = document.querySelector("#job-form");
const settingsForm = document.querySelector("#settings-form");
const runtime = document.querySelector("#runtime");
const log = document.querySelector("#log");
const jobState = document.querySelector("#job-state");
const startButton = document.querySelector("#start");
const cancelButton = document.querySelector("#cancel");
const compression = jobForm.elements.compressionLevel;
const browserDialog = document.querySelector("#browser");
const browserPath = document.querySelector("#browser-path");
const browserRoots = document.querySelector("#browser-roots");
const browserList = document.querySelector("#browser-list");
const browserSelect = document.querySelector("#browser-select");
const jobsList = document.querySelector("#jobs-list");
const packagesList = document.querySelector("#packages-list");
const diagnostics = document.querySelector("#diagnostics");
const progressTitle = document.querySelector("#progress-title");
const progressSubtitle = document.querySelector("#progress-subtitle");
const progressFill = document.querySelector("#progress-fill");
const modeHint = document.querySelector("#mode-hint");
const sourceLabel = document.querySelector("#source-label");
const sourceHint = document.querySelector("#source-hint");

let activeJobId = null;
let pollTimer = null;
let browseInput = null;
let browseType = "any";
let currentBrowsePath = "/";
let lastStatus = null;

for (let level = -4; level <= 9; level += 1) {
  const option = document.createElement("option");
  option.value = String(level);
  option.textContent = String(level);
  if (level === 7) option.selected = true;
  compression.append(option);
}

function isArchive(path) {
  return /\.(zip|7z|rar|tar|tgz|txz|tbz2|tar\.gz|tar\.xz|tar\.bz2)$/i.test(path || "");
}

function isPackage(path) {
  return /\.pkg$/i.test(path || "");
}

function formValue(name) {
  return String(new FormData(jobForm).get(name) || "").trim();
}

function containerWorkPath(path) {
  const value = String(path || "").trim();
  if (!value || value.startsWith("/mnt/cache/appdata/fpkg-builder")) return "/config/work";
  return value;
}

function payloadFromForm() {
  const data = new FormData(jobForm);
  const source = String(data.get("source") || "").trim();
  const mode = data.get("mode");
  const gameSource = String(data.get("gameSource") || "").trim();
  const payload = {
    outputFolder: data.get("outputFolder"),
    name: data.get("name"),
    compressionLevel: data.get("compressionLevel"),
    workFolder: data.get("workFolder"),
    rebuildBase: data.get("rebuildBase") === "on",
    keepWork: data.get("keepWork") === "on",
    force: data.get("force") === "on",
  };

  if (mode === "extract") {
    return {
      kind: "extract",
      package: source,
      destination: data.get("outputFolder"),
    };
  }

  if (mode === "game") {
    if (isArchive(source)) {
      return { ...payload, kind: "archive_build", archive: source };
    }
    return { ...payload, gameFolder: source };
  }

  if (mode === "backport-only") {
    if (isArchive(source)) {
      return { ...payload, kind: "archive_build", archive: source };
    }
    return {
      ...payload,
      kind: "folder_backport",
      backportFolder: source,
    };
  }

  if (mode === "backport-base") {
    return {
      ...payload,
      kind: isArchive(source) ? "archive_build" : "folder_backport",
      archive: isArchive(source) ? source : undefined,
      backportFolder: isArchive(source) ? undefined : source,
      basePackage: data.get("basePackage"),
      gameArchive: isArchive(gameSource) ? gameSource : "",
      gameFolder: gameSource && !isArchive(gameSource) ? gameSource : "",
    };
  }

  return { ...payload, gameFolder: source };
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || response.statusText);
  }
  return payload;
}

function renderRuntime(status) {
  const missing = Object.entries(status.scripts)
    .filter(([, value]) => !value.exists)
    .map(([name]) => name);
  runtime.innerHTML = `
    <strong>${status.canStart ? "Bereit" : "Konfiguration pruefen"}</strong>
    <span>PowerShell: ${status.powershell || "nicht gefunden"}</span>
    <span>Root: ${status.root}</span>
    ${missing.length ? `<span>Fehlt: ${missing.join(", ")}</span>` : ""}
  `;
  startButton.disabled = !status.canStart;
}

const modeUi = {
  game: {
    sourceLabel: "Game/RAW-Quelle",
    sourcePlaceholder: "/mnt/user/NZB/game-dump-oder-raw.zip",
    sourceHint: "Game-Dump-Ordner oder Archiv. Archive werden erst entpackt, danach wird daraus eine FPKG gebaut.",
    hint: "Aus einem Game/RAW-Dump eine FPKG bauen.",
    fields: ["source", "output", "name"],
  },
  "backport-base": {
    sourceLabel: "1. Backport ZIP/Ordner",
    sourcePlaceholder: "/mnt/user/NZB/backport.zip oder backport-ordner",
    sourceHint: "Hier kommt die Backport-ZIP, ein RAR/7z-Archiv oder der entpackte Backport-Ordner hinein.",
    hint: "Backport gegen eine Base bauen: erst Backport-Quelle, danach Base-PKG oder Base-Ordner auswaehlen.",
    fields: ["source", "output", "base", "name"],
  },
  "backport-only": {
    sourceLabel: "1. Backport ZIP/Ordner",
    sourcePlaceholder: "/mnt/user/NZB/backport.zip oder backport-ordner",
    sourceHint: "Nur den Backport bauen. Der Name muss zur vorhandenen Base-PKG im Output passen, falls keine Base-PKG gewaehlt wird.",
    hint: "Nur Backport-PKG bauen, wenn die passende Base schon im Output liegt.",
    fields: ["source", "output", "name"],
  },
  extract: {
    sourceLabel: "PKG zum Entpacken",
    sourcePlaceholder: "/mnt/user/NZB/base-oder-patch.pkg",
    sourceHint: "Eine vorhandene .pkg wird in den Output-Ordner entpackt.",
    hint: "Nur entpacken, nichts neu bauen.",
    fields: ["source", "output"],
  },
};

function updateModeUi() {
  const mode = formValue("mode") || "game";
  const config = modeUi[mode] || modeUi.game;
  const visible = new Set(config.fields);
  document.querySelectorAll("[data-field]").forEach((field) => {
    field.classList.toggle("hidden", !visible.has(field.dataset.field));
  });
  sourceLabel.textContent = config.sourceLabel;
  sourceHint.textContent = config.sourceHint;
  modeHint.textContent = config.hint;
  jobForm.elements.source.placeholder = config.sourcePlaceholder;
  jobForm.elements.basePackage.required = mode === "backport-base";
}

function renderJobs(jobs) {
  if (!jobs.length) {
    jobsList.className = "list empty";
    jobsList.textContent = "Noch keine Auftraege.";
    return;
  }
  jobsList.className = "list";
  jobsList.innerHTML = jobs
    .slice()
    .reverse()
    .map((job) => {
      const exit = job.returnCode === null ? "" : `Exit ${job.returnCode}`;
      const date = new Date(job.createdAt * 1000).toLocaleString();
      return `<article class="list-row">
        <strong>${job.id}</strong>
        <span>${job.status}</span>
        <small>${date} ${exit}</small>
      </article>`;
    })
    .join("");
}

async function loadStatus() {
  const status = await request("/api/status");
  lastStatus = status;
  renderRuntime(status);
  const jobs = await request("/api/jobs");
  renderJobs(jobs.jobs);
  activeJobId = jobs.activeJobId || activeJobId;
}

function renderProgress(job, lines = []) {
  const running = job.status === "queued" || job.status === "running";
  const failed = job.status === "failed";
  progressTitle.textContent = running ? "Auftrag laeuft" : failed ? "Fehlgeschlagen" : job.status === "succeeded" ? "Fertig" : "Bereit";
  progressSubtitle.textContent = lines.at(-1) || "Kein Auftrag gestartet.";
  progressFill.style.width = running ? "45%" : job.status === "succeeded" ? "100%" : failed ? "100%" : "0%";
  progressFill.classList.toggle("failed", failed);
}

async function pollJob() {
  if (!activeJobId) return;
  const payload = await request(`/api/jobs/${activeJobId}/log`);
  jobState.textContent = payload.job.status;
  log.textContent = payload.log.join("\n");
  log.scrollTop = log.scrollHeight;
  renderProgress(payload.job, payload.log);
  const running = payload.job.status === "queued" || payload.job.status === "running";
  startButton.disabled = running;
  cancelButton.disabled = !running;
  if (!running) {
    clearInterval(pollTimer);
    pollTimer = null;
    activeJobId = null;
    await loadStatus();
    await loadPackages().catch(() => {});
  }
}

async function startJob(payload) {
  startButton.disabled = true;
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
    jobState.textContent = "Fehler";
    log.textContent = error.message;
    progressTitle.textContent = "Fehler";
    progressSubtitle.textContent = error.message;
    progressFill.style.width = "100%";
    progressFill.classList.add("failed");
    startButton.disabled = false;
  }
}

function selectTab(name) {
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  document.querySelectorAll(".tab-view").forEach((view) => view.classList.toggle("hidden", view.id !== `tab-${name}`));
  if (name === "packages") loadPackages().catch((error) => renderError(packagesList, error));
  if (name === "diagnostics") loadDiagnostics().catch((error) => renderError(diagnostics, error));
}

function renderError(target, error) {
  target.className = "list empty";
  target.textContent = error.message;
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
    row.innerHTML = `<span>${entry.type === "dir" ? "Ordner" : "Datei"}</span><strong>${entry.name}</strong>`;
    row.addEventListener("click", () => {
      if (entry.type === "dir") {
        if (browseType === "dir") {
          currentBrowsePath = entry.path;
          browserPath.textContent = entry.path;
        }
        loadBrowse(entry.path);
      } else {
        if (browseInput) browseInput.value = entry.path;
        browserDialog.close();
      }
    });
    browserList.append(row);
  }
}

async function loadPackages() {
  const output = encodeURIComponent(formValue("outputFolder") || "/mnt/user/fpkg-output");
  const payload = await request(`/api/packages?path=${output}`);
  if (!payload.packages.length) {
    packagesList.className = "list empty";
    packagesList.textContent = "Keine .pkg im Ausgabeordner gefunden.";
    return;
  }
  packagesList.className = "list";
  packagesList.innerHTML = payload.packages
    .map((pkg) => `<article class="list-row">
      <strong>${pkg.name}</strong>
      <span>${pkg.sizeHuman}</span>
      <small>${pkg.modified}</small>
    </article>`)
    .join("");
}

async function loadSettings() {
  const settings = await request("/api/settings");
  settingsForm.elements.defaultOutput.value = settings.defaultOutput || "/mnt/user/fpkg-output";
  settingsForm.elements.defaultWork.value = containerWorkPath(settings.defaultWork);
  settingsForm.elements.archivePasswords.value = (settings.archivePasswords || []).join("\n");
  settingsForm.elements.overlapPrepare.checked = settings.overlapPrepare !== false;
  settingsForm.elements.verifyChecksums.checked = settings.verifyChecksums !== false;
  jobForm.elements.outputFolder.value = settings.defaultOutput || jobForm.elements.outputFolder.value;
  jobForm.elements.workFolder.value = containerWorkPath(settings.defaultWork);
}

async function saveSettings() {
  const data = new FormData(settingsForm);
  const payload = {
    defaultOutput: data.get("defaultOutput"),
    defaultWork: data.get("defaultWork"),
    archivePasswords: String(data.get("archivePasswords") || "").split(/\r?\n/).filter(Boolean),
    overlapPrepare: data.get("overlapPrepare") === "on",
    verifyChecksums: data.get("verifyChecksums") === "on",
  };
  await request("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  await loadSettings();
}

async function loadDiagnostics() {
  const payload = await request("/api/diagnostics");
  diagnostics.innerHTML = Object.entries(payload)
    .map(([key, value]) => `<article class="diag-row"><strong>${key}</strong><pre>${JSON.stringify(value, null, 2)}</pre></article>`)
    .join("");
}

jobForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await startJob(payloadFromForm());
});

settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await saveSettings();
});

cancelButton.addEventListener("click", async () => {
  if (!activeJobId) return;
  cancelButton.disabled = true;
  await request(`/api/jobs/${activeJobId}/cancel`, { method: "POST" });
  await pollJob();
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => selectTab(tab.dataset.tab));
});

jobForm.querySelectorAll('input[name="mode"]').forEach((input) => {
  input.addEventListener("change", updateModeUi);
});

document.querySelectorAll(".browse").forEach((button) => {
  button.addEventListener("click", async () => {
    browseInput = button.closest(".path-row")?.querySelector("input") || null;
    browseType = button.dataset.type || "any";
    currentBrowsePath = browseInput?.value || (lastStatus?.browseRoots?.[0] || "/mnt/user");
    browserDialog.showModal();
    await loadBrowse(currentBrowsePath).catch(async () => loadBrowse(lastStatus?.browseRoots?.[0] || "/mnt/user"));
  });
});

browserRoots.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-path]");
  if (button) loadBrowse(button.dataset.path);
});

browserSelect.addEventListener("click", () => {
  if (browseInput) browseInput.value = currentBrowsePath;
  browserDialog.close();
});

document.querySelector("#browser-close").addEventListener("click", () => browserDialog.close());
document.querySelector("#refresh-jobs").addEventListener("click", () => loadStatus());
document.querySelector("#refresh-packages").addEventListener("click", () => loadPackages().catch((error) => renderError(packagesList, error)));
document.querySelector("#refresh-diagnostics").addEventListener("click", () => loadDiagnostics().catch((error) => renderError(diagnostics, error)));
document.querySelector("#copy-log").addEventListener("click", () => navigator.clipboard?.writeText(log.textContent || ""));

loadStatus().catch((error) => {
  runtime.textContent = error.message;
  startButton.disabled = true;
});
loadSettings().catch(() => {});
updateModeUi();
