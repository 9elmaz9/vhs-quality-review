"use strict";
const config = JSON.parse(document.getElementById("app-config").textContent);
function t(message, values = {}) {
  const translated = config.translations[message] || message;
  return translated.replace(/\{(\w+)\}/g, (match, key) => Object.hasOwn(values, key) ? String(values[key]) : match);
}
const $ = (id) => document.getElementById(id);
const labels = {BLACK: t("Zwart beeld"), FREEZE: t("Stilstaand beeld"), GLITCH: t("Kleurafwijking"), "RUIS/STRIPES": t("Ruis / strepen"), "1KHZ_TONE": t("1 kHz-toon")};
const statuses = {queued: t("In wachtrij"), running: t("Bezig"), completed: t("Afgerond"), failed: t("Mislukt"), cancelled: t("Geannuleerd")};
const reviews = {pending: t("Nog beoordelen"), confirmed: t("Defect"), dismissed: t("Geen defect")};
const active = (job) => ["queued", "running"].includes(job.status);

function el(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function notice(message) {
  $("notice").textContent = message || "";
  $("notice").hidden = !message;
}
function hms(seconds, precise = false) {
  const value = Math.max(0, Math.round((seconds || 0) * (precise ? 1000 : 1)));
  const whole = precise ? Math.floor(value / 1000) : value;
  const text = [Math.floor(whole / 3600), Math.floor(whole / 60) % 60, whole % 60].map(x => String(x).padStart(2, "0")).join(":");
  return precise ? `${text}.${String(value % 1000).padStart(3, "0")}` : text;
}
function size(bytes) {
  return bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}
function date(value) {
  return new Date(value).toLocaleString(config.locale, {dateStyle: "medium", timeStyle: "short"});
}
function percent(value) { return `${value.toLocaleString(config.locale, {maximumFractionDigits: 1})}%`; }
async function api(path, method = "GET", body) {
  const options = {method, headers: {"X-CSRF-Token": config.csrf}, cache: "no-store"};
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try { response = await fetch(path, options); }
  catch { throw new Error(t("Geen verbinding met de app. Controleer of de app nog draait.")); }
  const result = await response.json().catch(() => ({}));
  if (response.status === 401) window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
  if (!response.ok) throw new Error(result.error || t("De aanvraag is mislukt. Probeer opnieuw."));
  return result;
}
function badge(job) { return el("span", `status-badge status-${job.status}`, statuses[job.status] || job.status); }
function progress(job) {
  const wrapper = el("div", "job-progress");
  const line = el("div", "job-progress-line");
  const completedPercent = job.total_steps ? Math.min(100, Math.max(0, Math.round(job.completed_steps / job.total_steps * 100))) : 0;
  const amount = el("span", "progress-amount mono");
  amount.append(el("strong", "progress-percent", `${completedPercent}%`), el("span", "", t(" · {done}/{total} stappen", {done: job.completed_steps, total: job.total_steps})));
  line.append(el("span", "", job.cancel_requested ? t("Annuleren…") : job.stage), amount);
  const bar = el("progress"); bar.max = job.total_steps; bar.value = job.completed_steps;
  bar.setAttribute("aria-label", t("{percent}%: {done} van {total} stappen voltooid", {percent: completedPercent, done: job.completed_steps, total: job.total_steps}));
  wrapper.append(line, bar);
  return wrapper;
}

let jobs = [], jobsSignature = "", selectedFiles = [], source = "upload", submitting = false;
const selectedJobIds = new Set(), selectedLibraryFiles = new Set();
let deletingJobs = false, deletingLibrary = false, visibleJobs = [];
function renderFiles() {
  $("picked-files").replaceChildren();
  $("clear-picked").hidden = !selectedFiles.length;
  selectedFiles.forEach((file, index) => {
    const row = el("li", "picked-file");
    const remove = el("button", "remove-file", "×");
    remove.type = "button"; remove.setAttribute("aria-label", t("{name} verwijderen", {name: file.name}));
    remove.addEventListener("click", () => { selectedFiles.splice(index, 1); renderFiles(); });
    row.append(el("span", "file-description", file.name), el("span", "file-size", size(file.size)), remove);
    $("picked-files").append(row);
  });
}
function addFiles(files) {
  const allowed = /\.(mp4|mov|mkv|avi|m4v)$/i;
  const invalid = [];
  for (const file of files) {
    if (!allowed.test(file.name)) { invalid.push(file.name); continue; }
    if (!selectedFiles.some(x => x.name === file.name && x.size === file.size && x.lastModified === file.lastModified)) selectedFiles.push(file);
  }
  if (invalid.length) notice(t("Niet toegevoegd: {names}. Kies MP4, MOV, MKV, AVI of M4V.", {names: invalid.join(", ")}));
  renderFiles();
}
function readSettings() {
  const detectors = Array.from(document.querySelectorAll(".detector-toggle:checked"), x => x.dataset.kind);
  const min_durations = {};
  document.querySelectorAll(".duration-input").forEach(x => { min_durations[x.dataset.kind] = Number(x.value); });
  return {detectors, min_durations};
}
function updateSettingsLabel() {
  const count = readSettings().detectors.length;
  $("settings-label").textContent = t("{count} controles · {profile}", {count, profile: $("preset").value === "standard" ? t("standaard") : $("preset").value === "short" ? t("kort") : t("aangepast")});
}
function uploadFiles(form) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/jobs");
    xhr.setRequestHeader("X-CSRF-Token", config.csrf);
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) {
        const value = Math.round(event.loaded / event.total * 100);
        $("upload-progress").value = value;
        $("upload-label").textContent = value < 100 ? t("Uploaden… {percent}%", {percent: value}) : t("Upload ontvangen; bestanden opslaan…");
      }
    });
    xhr.addEventListener("load", () => {
      if (xhr.status === 401) window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
      let result;
      try { result = JSON.parse(xhr.responseText); }
      catch { reject(new Error(t("De server gaf geen geldig antwoord op de upload."))); return; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(result);
      else reject(new Error(result.error || t("De upload is mislukt.")));
    });
    xhr.addEventListener("error", () => reject(new Error(t("De upload is onderbroken. Controleer de verbinding met de app."))));
    xhr.addEventListener("abort", () => reject(new Error(t("De upload is geannuleerd."))));
    xhr.send(form);
  });
}
async function loadLibrary() {
  try {
    const result = await api("/api/library");
    const names = new Set(result.files.map(file => file.name));
    for (const name of selectedLibraryFiles) if (!names.has(name)) selectedLibraryFiles.delete(name);
    $("library-files").replaceChildren();
    $("library-toolbar").hidden = !result.files.length;
    if (!result.files.length) $("library-files").append(el("p", "muted small", t("Geen bestaande video's gevonden. Upload een video om te beginnen.")));
    for (const file of result.files) {
      const row = el("div", "library-item"); row.dataset.filename = file.name;
      const label = el("label", "library-label");
      const input = el("input"); input.type = "checkbox"; input.value = file.name;
      input.checked = selectedLibraryFiles.has(file.name);
      input.addEventListener("change", () => {
        if (input.checked) selectedLibraryFiles.add(file.name); else selectedLibraryFiles.delete(file.name);
        updateLibrarySelection();
      });
      label.append(input, el("span", "", file.name), el("span", "file-size", size(file.size)));
      const remove = el("button", "mini-button delete-button library-delete", t("Verwijderen"));
      remove.type = "button"; remove.setAttribute("aria-label", t("{name} verwijderen", {name: file.name}));
      remove.addEventListener("click", () => deleteLibraryFiles([file.name]));
      row.append(label, remove); $("library-files").append(row);
    }
    updateLibrarySelection();
  } catch (error) { $("library-files").textContent = error.message; }
}
function updateLibrarySelection() {
  const boxes = Array.from(document.querySelectorAll("#library-files input"));
  $("library-select-all").checked = boxes.length > 0 && boxes.every(input => input.checked);
  $("library-select-all").indeterminate = boxes.some(input => input.checked) && !$("library-select-all").checked;
  $("delete-library-selected").disabled = deletingLibrary || selectedLibraryFiles.size === 0;
  $("delete-library-selected").textContent = deletingLibrary ? t("Verwijderen…") : t("Verwijder selectie") + (selectedLibraryFiles.size ? " (" + selectedLibraryFiles.size + ")" : "");
}
async function deleteLibraryFiles(names) {
  if (deletingLibrary || !names.length) return;
  const subject = names.length === 1 ? t("Het videobestand “{name}”", {name: names[0]}) : t("Deze {count} videobestanden", {count: names.length});
  if (!window.confirm(t("{subject} verwijderen? Lopende analyses van deze bestanden worden gestopt. Bestaande resultaten blijven bewaard, maar de video's kunnen daarin niet meer worden afgespeeld.", {subject}))) return;
  deletingLibrary = true;
  document.querySelectorAll("#library-source button, #library-source input").forEach(input => { input.disabled = true; });
  $("start-analysis").disabled = true; updateLibrarySelection();
  try {
    notice(""); await api("/api/library", "DELETE", {files: names});
    names.forEach(name => selectedLibraryFiles.delete(name));
  } catch (error) { notice(error.message); }
  finally {
    deletingLibrary = false; await loadLibrary();
    $("library-select-all").disabled = false; $("start-analysis").disabled = submitting;
    await refreshJobs().catch(error => notice(error.message));
  }
}
function updateJobSelection() {
  const count = visibleJobs.filter(job => selectedJobIds.has(job.id)).length;
  $("jobs-select-all").checked = visibleJobs.length > 0 && count === visibleJobs.length;
  $("jobs-select-all").indeterminate = count > 0 && count < visibleJobs.length;
  $("delete-jobs-selected").disabled = deletingJobs || selectedJobIds.size === 0;
  $("delete-jobs-selected").textContent = deletingJobs ? t("Verwijderen…") : t("Verwijder selectie") + (selectedJobIds.size ? " (" + selectedJobIds.size + ")" : "");
}
function confirmJobDeletion(count) {
  return window.confirm(t("{subject} en de bijbehorende beoordelingen verwijderen? Lopende analyses worden eerst gestopt. Een nieuw geüploade video wordt ook verwijderd als geen andere analyse deze gebruikt.", {subject: count === 1 ? t("Deze analyse") : t("Deze {count} analyses", {count})}));
}
async function deleteJobs(ids) {
  if (deletingJobs || !ids.length || !confirmJobDeletion(ids.length)) return;
  deletingJobs = true;
  document.querySelectorAll("#history button, #history input, #history select").forEach(input => { input.disabled = true; });
  updateJobSelection();
  const errors = [];
  try {
    notice("");
    for (const id of ids) {
      try { await api(`/api/jobs/${id}`, "DELETE"); selectedJobIds.delete(id); }
      catch (error) { errors.push(error.message); }
    }
    if (errors.length) notice(t("{count} analyse(s) niet verwijderd. {error}", {count: errors.length, error: errors[0]}));
  } finally {
    deletingJobs = false; $("jobs-select-all").disabled = false; jobsSignature = "";
    document.querySelectorAll("#history input, #history select").forEach(input => { input.disabled = false; });
    await refreshJobs().catch(error => notice(error.message));
  }
}
function renderJobs() {
  const query = $("history-search").value.toLowerCase();
  const filter = $("history-filter").value;
  const visible = jobs.filter(job => job.filename.toLowerCase().includes(query) &&
    (filter === "all" || (filter === "active" ? active(job) : filter === "failed" ? ["failed", "cancelled"].includes(job.status) : job.status === filter)));
  visibleJobs = visible;
  for (const id of selectedJobIds) if (!jobs.some(job => job.id === id)) selectedJobIds.delete(id);
  $("jobs-toolbar").hidden = !jobs.length; updateJobSelection();
  $("job-count").textContent = jobs.length;
  $("jobs").replaceChildren();
  if (!visible.length) {
    const empty = el("div", "empty-state");
    empty.append(el("span", "empty-icon", "▤"), el("h3", "", jobs.length ? t("Geen passende analyses") : t("Je eerste analyse begint hier")),
      el("p", "", jobs.length ? t("Pas de zoekopdracht of het filter aan.") : t("Voeg een video toe. Je resultaten en beoordelingen verschijnen hier en blijven bewaard.")));
    $("jobs").append(empty); return;
  }
  for (const job of visible) {
    const card = el("article", "job-card");
    card.dataset.jobId = job.id;
    const top = el("div", "job-top"), titleGroup = el("div");
    const selection = el("input", "job-select"); selection.type = "checkbox";
    selection.checked = selectedJobIds.has(job.id);
    selection.setAttribute("aria-label", t("{name} selecteren", {name: job.filename}));
    selection.addEventListener("change", () => {
      if (selection.checked) selectedJobIds.add(job.id); else selectedJobIds.delete(job.id);
      updateJobSelection();
    });
    const title = el("a", "job-title", job.filename); title.href = job.detail_url;
    titleGroup.append(title, el("div", "job-date", `${date(job.created_at)}${job.duration ? " · " + hms(job.duration) : ""}`));
    titleGroup.className = "job-title-group";
    top.append(selection, titleGroup, badge(job)); card.append(top);
    if (active(job)) card.append(progress(job));
    const bottom = el("div", "job-bottom");
    let text = job.status === "completed" ? t("{count} meldingen · {pending} nog te beoordelen", {count: job.summary.event_count, pending: job.summary.pending_count}) : job.status === "failed" ? t("Open de analyse voor meer informatie.") : job.status === "cancelled" ? t("Deze analyse is geannuleerd.") : t("Je kunt ondertussen andere video's toevoegen.");
    bottom.append(el("span", "muted", text));
    const actions = el("div", "row-actions");
    if (active(job)) {
      const cancel = el("button", "mini-button", job.cancel_requested ? t("Annuleren…") : t("Annuleren"));
      cancel.type = "button"; cancel.disabled = Boolean(job.cancel_requested);
      cancel.addEventListener("click", async () => {
        cancel.disabled = true;
        try { await api(`/api/jobs/${job.id}/cancel`, "POST"); await refreshJobs(); }
        catch (error) { notice(error.message); cancel.disabled = false; }
      });
      actions.append(cancel);
    } else {
      const link = el("a", "text-link", job.status === "completed" ? t("Bekijken →") : t("Openen →")); link.href = job.detail_url; actions.append(link);
    }
    const remove = el("button", "mini-button delete-button job-delete", t("Verwijderen"));
    remove.type = "button"; remove.setAttribute("aria-label", t("Analyse van {name} verwijderen", {name: job.filename}));
    remove.addEventListener("click", () => deleteJobs([job.id]));
    actions.append(remove); bottom.append(actions);
    card.append(bottom); $("jobs").append(card);
  }
}
async function refreshJobs() {
  if (deletingJobs) return;
  const result = await api("/api/jobs");
  if (deletingJobs) return;
  const signature = JSON.stringify(result.jobs);
  if (signature !== jobsSignature) { jobsSignature = signature; jobs = result.jobs; renderJobs(); }
}
async function dashboard() {
  $("clear-picked").addEventListener("click", () => { selectedFiles = []; renderFiles(); });
  $("library-select-all").addEventListener("change", event => {
    document.querySelectorAll("#library-files input").forEach(input => {
      input.checked = event.target.checked;
      if (input.checked) selectedLibraryFiles.add(input.value); else selectedLibraryFiles.delete(input.value);
    });
    updateLibrarySelection();
  });
  $("delete-library-selected").addEventListener("click", () => deleteLibraryFiles([...selectedLibraryFiles]));
  $("jobs-select-all").addEventListener("change", event => {
    visibleJobs.forEach(job => { if (event.target.checked) selectedJobIds.add(job.id); else selectedJobIds.delete(job.id); });
    renderJobs();
  });
  $("delete-jobs-selected").addEventListener("click", () => deleteJobs([...selectedJobIds]));
  for (const value of ["upload", "library"]) $("source-" + value).addEventListener("click", () => {
    source = value;
    for (const key of ["upload", "library"]) {
      $(key + "-source").hidden = key !== source;
      $("source-" + key).classList.toggle("active", key === source);
      $("source-" + key).setAttribute("aria-pressed", key === source);
    }
  });
  $("videos").addEventListener("change", event => { addFiles(event.target.files); event.target.value = ""; });
  const drop = $("drop-zone");
  drop.addEventListener("dragover", event => { event.preventDefault(); drop.classList.add("drag"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
  drop.addEventListener("drop", event => { event.preventDefault(); drop.classList.remove("drag"); addFiles(event.dataTransfer.files); });
  $("preset").addEventListener("change", () => {
    if ($("preset").value !== "custom") {
      document.querySelectorAll(".duration-input").forEach(input => { input.value = $("preset").value === "short" ? 1 : input.dataset.default; });
      document.querySelectorAll(".detector-toggle").forEach(input => { input.checked = true; });
    }
    updateSettingsLabel();
  });
  document.querySelectorAll(".detector-toggle,.duration-input").forEach(input => input.addEventListener("change", () => { $("preset").value = "custom"; updateSettingsLabel(); }));
  const changeHistoryFilter = () => { selectedJobIds.clear(); renderJobs(); };
  $("history-search").addEventListener("input", changeHistoryFilter);
  $("history-filter").addEventListener("change", changeHistoryFilter);
  $("upload-form").addEventListener("submit", async event => {
    event.preventDefault(); if (submitting || deletingLibrary) return;
    notice("");
    const settings = readSettings();
    const libraryFiles = Array.from(document.querySelectorAll("#library-files input:checked"), input => input.value);
    if (!settings.detectors.length) { notice(t("Selecteer minimaal één controle in de analyse-instellingen.")); return; }
    if (source === "upload" ? !selectedFiles.length : !libraryFiles.length) { notice(t("Selecteer eerst minimaal één video.")); return; }
    submitting = true; $("start-analysis").disabled = true;
    try {
      let result;
      if (source === "upload") {
        const form = new FormData(); form.append("settings", JSON.stringify(settings));
        selectedFiles.forEach(file => form.append("videos", file));
        $("upload-status").hidden = false; $("upload-progress").value = 0; $("upload-label").textContent = t("Uploaden…");
        result = await uploadFiles(form);
        selectedFiles = []; renderFiles();
      } else {
        result = await api("/api/jobs/from-library", "POST", {files: libraryFiles, settings});
        document.querySelectorAll("#library-files input").forEach(input => { input.checked = false; });
        selectedLibraryFiles.clear(); updateLibrarySelection();
      }
      if (result.job_ids.length === 1) window.location.href = `/jobs/${result.job_ids[0]}`;
      else { await refreshJobs(); $("history").scrollIntoView({behavior: "smooth"}); }
    } catch (error) { notice(error.message); }
    finally { submitting = false; $("start-analysis").disabled = false; $("upload-status").hidden = true; }
  });
  window.addEventListener("beforeunload", event => { if (submitting) { event.preventDefault(); event.returnValue = ""; } });
  loadLibrary();
  poll(refreshJobs);
}

let currentJob = null, detailSignature = "", selectedEvent = null, detailGeneration = 0, reviewInFlight = false;
function selectEvent(eventId) {
  if (!currentJob.media_available) return;
  selectedEvent = eventId;
  const event = currentJob.events.find(item => item.id === eventId);
  if (!event) return;
  if (!document.querySelector(`#events-body tr[data-event-id="${eventId}"]`)) {
    $("type-filter").value = "all"; $("review-filter").value = "all";
  }
  renderEvents();
  const seek = () => { $("player").currentTime = event.start_sec; $("player").play().catch(() => {}); };
  if ($("player").readyState) seek(); else $("player").addEventListener("loadedmetadata", seek, {once: true});
}
function renderEvents() {
  const events = currentJob.events;
  $("event-count").textContent = events.length;
  const filtered = events.filter(event => ($("type-filter").value === "all" || event.type === $("type-filter").value) &&
    ($("review-filter").value === "all" || event.review === $("review-filter").value));
  $("events-body").replaceChildren();
  $("events-empty").hidden = Boolean(filtered.length);
  $("events-table").hidden = !filtered.length;
  $("events-empty").textContent = currentJob.status !== "completed" ? t("Gebeurtenissen verschijnen zodra de analyse is afgerond.") : events.length ? t("Geen gebeurtenissen met deze filters.") : t("Geen verdachte fragmenten gevonden met deze instellingen. Dit is geen garantie dat de opname foutloos is.");
  for (const event of filtered) {
    const row = el("tr", event.id === selectedEvent ? "selected" : ""); row.dataset.eventId = event.id;
    const kind = el("td"); kind.append(el("span", "event-type", labels[event.type] || event.type));
    const fragment = el("td"), jump = el("button", "event-jump mono", "▶ " + hms(event.start_sec, true));
    jump.type = "button"; jump.setAttribute("aria-label", t("Bekijk {type} op {time}", {type: labels[event.type], time: hms(event.start_sec, true)}));
    jump.disabled = !currentJob.media_available;
    jump.addEventListener("click", () => selectEvent(event.id));
    fragment.append(jump, el("span", "event-end mono", t("tot ") + hms(event.end_sec, true)));
    const cell = el("td"), select = el("select", `review-select ${event.review}`);
    select.setAttribute("aria-label", t("Beoordeling van {type} op {time}", {type: labels[event.type], time: hms(event.start_sec, true)}));
    for (const [value, label] of Object.entries(reviews)) { const option = el("option", "", label); option.value = value; select.append(option); }
    select.value = event.review;
    select.addEventListener("change", async () => {
      reviewInFlight = true; detailGeneration += 1;
      document.querySelectorAll(".review-select").forEach(input => { input.disabled = true; });
      try {
        const job = await api(`/api/jobs/${currentJob.id}/events/${event.id}`, "PATCH", {review: select.value});
        renderDetail(job);
      } catch (error) { notice(error.message); select.value = event.review; }
      finally { reviewInFlight = false; document.querySelectorAll(".review-select").forEach(input => { input.disabled = false; }); }
    });
    cell.append(select);
    row.append(kind, fragment, el("td", "mono", (event.end_sec - event.start_sec).toFixed(2) + " s"), el("td", "event-detail", event.details), cell);
    $("events-body").append(row);
  }
  $("timeline").replaceChildren();
  $("timeline-count").textContent = t("{count} meldingen", {count: events.length});
  const timelineKinds = [...new Set(events.map(event => event.type))];
  $("timeline").style.height = `${Math.max(36, timelineKinds.length * 14 + 8)}px`;
  for (const event of events) {
    const marker = el("button", `timeline-event ${event.review}${event.id === selectedEvent ? " selected" : ""}`);
    marker.type = "button";
    marker.disabled = !currentJob.media_available;
    marker.style.left = `${event.start_sec / currentJob.duration * 100}%`;
    marker.style.width = `${(event.end_sec - event.start_sec) / currentJob.duration * 100}%`;
    marker.style.top = `${4 + timelineKinds.indexOf(event.type) * 14}px`;
    marker.style.height = "10px";
    marker.title = `${labels[event.type]} · ${hms(event.start_sec, true)} · ${reviews[event.review]}`;
    marker.setAttribute("aria-label", marker.title);
    marker.addEventListener("click", () => selectEvent(event.id));
    $("timeline").append(marker);
  }
}
function renderDetail(job) {
  currentJob = job; detailSignature = JSON.stringify(job);
  document.title = `${job.filename} · Zwartruimte`;
  $("detail-filename").textContent = job.filename;
  $("detail-meta").textContent = t("Gestart {date} · {count} controles", {date: date(job.created_at), count: job.settings.detectors.length});
  $("video-duration").textContent = job.duration ? hms(job.duration) : "";
  $("events-export").href = `/api/jobs/${job.id}/events.csv`;
  $("events-export").hidden = job.status !== "completed";
  $("retry-job").hidden = active(job) || !job.media_available; $("delete-job").hidden = false;
  $("player").hidden = !job.media_available;
  $("player-missing").hidden = job.media_available;
  $("download-video").hidden = !job.media_available;
  if (!job.media_available) {
    $("player-error").hidden = true;
    if ($("player").hasAttribute("src")) { $("player").pause(); $("player").removeAttribute("src"); $("player").load(); }
  }
  if (job.media_available && $("player").dataset.jobId !== job.id) {
    $("player").dataset.jobId = job.id; $("player").src = job.media_url;
    $("download-video").href = job.media_url + "?download=1";
  }
  const content = el("div", "status-content"), line = el("div", "status-line");
  line.append(badge(job), el("span", "", job.cancel_requested && active(job) ? t("Analyse wordt gestopt…") : job.stage));
  content.append(line);
  if (active(job)) content.append(progress(job), el("p", "", t("De balk toont afgeronde stappen. De duur verschilt per controle; dit is geen schatting van de resterende tijd.")));
  if (job.error) content.append(el("p", "inline-warning", job.error));
  if (job.warning) content.append(el("p", "", job.warning));
  if (job.status === "completed") content.append(el("p", "", t("{count} meldingen gevonden. Bekijk de fragmenten en kies per melding 'Defect' of 'Geen defect'. Je beoordeling wordt direct opgeslagen.", {count: job.summary.event_count})));
  $("job-status").replaceChildren(content);
  if (active(job)) {
    const cancel = el("button", "button secondary", job.cancel_requested ? t("Annuleren…") : t("Annuleer analyse"));
    cancel.type = "button"; cancel.disabled = Boolean(job.cancel_requested);
    cancel.addEventListener("click", async () => {
      cancel.disabled = true;
      try { await api(`/api/jobs/${job.id}/cancel`, "POST"); await refreshDetail(); }
      catch (error) { notice(error.message); cancel.disabled = false; }
    });
    $("job-status").append(cancel);
  }
  $("pending-count").textContent = job.status === "completed" ? job.summary.pending_count : "—";
  $("suspected-percent").textContent = job.status === "completed" ? percent(job.summary.suspected_percent) : "—";
  $("confirmed-percent").textContent = job.status === "completed" ? percent(job.summary.confirmed_percent) : "—";
  $("job-settings").replaceChildren(...job.settings.detectors.map(key => el("p", "", t("{type}: vanaf {seconds} sec", {type: labels[key], seconds: job.settings.min_durations[key]}))));
  renderEvents();
}
async function refreshDetail() {
  if (reviewInFlight) return;
  const generation = detailGeneration;
  const job = await api(`/api/jobs/${config.activeJob}`);
  if (!reviewInFlight && generation === detailGeneration && JSON.stringify(job) !== detailSignature) renderDetail(job);
}
async function detail() {
  $("player").addEventListener("error", () => { $("player-error").hidden = false; });
  $("type-filter").addEventListener("change", () => { if (currentJob) renderEvents(); });
  $("review-filter").addEventListener("change", () => { if (currentJob) renderEvents(); });
  $("retry-job").addEventListener("click", async () => {
    $("retry-job").disabled = true;
    try { const result = await api(`/api/jobs/${config.activeJob}/retry`, "POST"); window.location.href = `/jobs/${result.job_ids[0]}`; }
    catch (error) { notice(error.message); $("retry-job").disabled = false; }
  });
  $("delete-job").addEventListener("click", async () => {
    if (!confirmJobDeletion(1)) return;
    $("delete-job").disabled = true;
    try { await api(`/api/jobs/${config.activeJob}`, "DELETE"); window.location.href = "/"; }
    catch (error) { notice(error.message); $("delete-job").disabled = false; }
  });
  poll(refreshDetail);
}
async function poll(refresh) {
  try { await refresh(); }
  catch (error) { notice(error.message); }
  finally { window.setTimeout(() => poll(refresh), document.hidden ? 8000 : 2000); }
}
if (config.activeJob) detail(); else dashboard();
