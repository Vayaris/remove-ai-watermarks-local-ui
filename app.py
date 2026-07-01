from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
LOG_DIR = BASE_DIR / "logs"
TMP_DIR = BASE_DIR / "tmp"
MODEL_CACHE = BASE_DIR / "models-cache"
CLI = BASE_DIR / ".venv" / "Scripts" / "remove-ai-watermarks.exe"

for directory in (INPUT_DIR, OUTPUT_DIR, LOG_DIR, TMP_DIR, MODEL_CACHE):
    directory.mkdir(parents=True, exist_ok=True)


app = FastAPI(title="Remove AI Watermarks Local")
job_lock = asyncio.Lock()


@dataclass
class Job:
    id: str
    input_path: Path
    output_path: Path
    log_path: Path
    status: str = "queued"
    progress: int = 0
    phase: str = "Queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    command: list[str] = field(default_factory=list)
    exit_code: int | None = None
    error: str | None = None
    active_max_resolution: int = 0
    profile: str = "auto"
    visible_mark: str = "auto"
    scan_result: dict[str, Any] | None = None


jobs: dict[str, Job] = {}


PROFILE_LABELS = {
    "auto": "Auto d'apres scan",
    "gemini": "Gemini / Google",
    "openai": "ChatGPT / OpenAI",
    "sdxl": "Stable Diffusion / FLUX",
    "metadata": "Metadonnees seulement",
    "visible": "Visible seulement",
}

VISIBLE_MARKS = {"auto", "gemini", "doubao", "jimeng", "samsung"}


def safe_name(name: str) -> str:
    stem = Path(name).stem or "image"
    suffix = Path(name).suffix.lower() or ".png"
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._") or "image"
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        suffix = ".png"
    return f"{stem[:80]}{suffix}"


def set_progress(job: Job, value: int, phase: str | None = None) -> None:
    job.progress = max(job.progress, max(0, min(100, int(value))))
    if phase:
        job.phase = phase
    job.updated_at = time.time()


def append_log(job: Job, text: str) -> None:
    with job.log_path.open("a", encoding="utf-8", errors="replace") as log:
        log.write(text)
        if not text.endswith("\n"):
            log.write("\n")


def job_json(job: Job) -> dict[str, object]:
    log_text = ""
    if job.log_path.exists():
        log_text = job.log_path.read_text(encoding="utf-8", errors="replace")[-30000:]
    return {
        "id": job.id,
        "status": job.status,
        "progress": job.progress,
        "phase": job.phase,
        "profile": job.profile,
        "visible_mark": job.visible_mark,
        "exit_code": job.exit_code,
        "error": job.error,
        "scan_result": job.scan_result,
        "input_url": f"/api/input/{job.id}",
        "output_url": f"/api/output/{job.id}" if job.output_path.exists() else None,
        "log": log_text,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "active_max_resolution": job.active_max_resolution,
    }


def build_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["HF_HOME"] = str(MODEL_CACHE)
    env["HF_HUB_CACHE"] = str(MODEL_CACHE / "hub")
    env["HUGGINGFACE_HUB_CACHE"] = str(MODEL_CACHE / "hub")
    env["TRANSFORMERS_CACHE"] = str(MODEL_CACHE / "transformers")
    env["DIFFUSERS_CACHE"] = str(MODEL_CACHE / "diffusers")
    env["TEMP"] = str(TMP_DIR)
    env["TMP"] = str(TMP_DIR)
    return env


def looks_like_oom(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "out of memory",
            "cuda error: out of memory",
            "not enough memory",
            "cuda out of memory",
        )
    )


def extract_json(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in identify output")
    return json.loads(text[start : end + 1])


def update_progress_from_line(job: Job, line: str) -> None:
    clean = line.replace("\r", "\n")
    lowered = clean.lower()
    if "1) visible" in lowered:
        set_progress(job, 10, "Visible watermark scan")
    elif "removing visible" in lowered:
        set_progress(job, 18, "Removing visible watermark")
    elif "visible watermark removed" in lowered or "skipped (no visible" in lowered:
        set_progress(job, 25, "Visible step complete")
    elif "2) invisible" in lowered:
        set_progress(job, 30, "Invisible watermark step")
    elif "fetching" in lowered or "loading model" in lowered or "loading controlnet" in lowered:
        set_progress(job, 38, "Loading GPU models")
    elif "moving model to device" in lowered:
        set_progress(job, 45, "Moving model to CUDA")
    elif "encoding image" in lowered:
        set_progress(job, 50, "Preparing image on GPU")
    elif "denoising" in lowered:
        match = re.search(r"(\d+)\s*/\s*(\d+)", clean)
        if match:
            current = int(match.group(1))
            total = max(1, int(match.group(2)))
            set_progress(job, 50 + round((current / total) * 35), "Denoising on GPU")
        else:
            set_progress(job, 60, "Denoising on GPU")
    elif "regeneration complete" in lowered or "invisible watermark removed" in lowered:
        set_progress(job, 85, "Invisible step complete")
    elif "3) ai metadata" in lowered or "metadata stripping" in lowered:
        set_progress(job, 90, "Stripping metadata")
    elif "ai metadata stripped" in lowered:
        set_progress(job, 94, "Metadata stripped")
    elif "done:" in lowered or "saved:" in lowered:
        set_progress(job, 98, "Saving result")


def run_command(job: Job, command: list[str], progress_mode: str, max_resolution: int = 0) -> tuple[int, str]:
    job.command = command
    job.active_max_resolution = max_resolution
    job.updated_at = time.time()

    with job.log_path.open("a", encoding="utf-8", errors="replace") as log:
        log.write("\n\n")
        log.write("=" * 72 + "\n")
        log.write(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        log.write("Command: " + subprocess.list2cmdline(command) + "\n")
        log.write("=" * 72 + "\n")
        log.flush()

        proc = subprocess.Popen(
            command,
            cwd=str(BASE_DIR),
            env=build_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        output_parts: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            output_parts.append(line)
            log.write(line)
            log.flush()
            if progress_mode == "scan":
                set_progress(job, 70, "Reading provenance")
            elif progress_mode == "process":
                update_progress_from_line(job, line)
        code = proc.wait()
        log.write(f"\nExit code: {code}\n")
        log.flush()

    return code, "".join(output_parts)


def create_job(name: str) -> Job:
    safe = safe_name(name)
    job_id = uuid.uuid4().hex[:12]
    input_path = INPUT_DIR / f"{job_id}_{safe}"
    output_path = OUTPUT_DIR / f"{Path(safe).stem}_{job_id}_clean{Path(safe).suffix}"
    log_path = LOG_DIR / f"{job_id}.log"
    return Job(id=job_id, input_path=input_path, output_path=output_path, log_path=log_path)


def save_upload(job: Job, file: UploadFile) -> None:
    with job.input_path.open("wb") as target:
        shutil.copyfileobj(file.file, target)
    job.log_path.write_text(
        f"Queued: {time.strftime('%Y-%m-%d %H:%M:%S')}\nInput: {job.input_path}\nOutput: {job.output_path}\n",
        encoding="utf-8",
    )
    set_progress(job, 15, "Uploaded")


def scan_job_sync(job_id: str) -> None:
    job = jobs[job_id]
    job.status = "scanning"
    set_progress(job, 20, "Scanning provenance")

    if not CLI.exists():
        job.status = "failed"
        job.error = f"CLI not found: {CLI}"
        job.updated_at = time.time()
        return

    command = [str(CLI), "identify", str(job.input_path), "--json"]
    code, text = run_command(job, command, "scan")
    job.exit_code = code

    if code != 0:
        job.status = "failed"
        job.error = text.strip().splitlines()[-1][-500:] if text.strip() else "Scan failed"
        job.updated_at = time.time()
        return

    try:
        job.scan_result = extract_json(text)
    except Exception as exc:
        job.status = "failed"
        job.error = f"Could not parse scan JSON: {exc}"
        job.updated_at = time.time()
        return

    job.status = "scanned"
    set_progress(job, 100, "Scan complete")


async def scan_job(job_id: str) -> None:
    async with job_lock:
        await asyncio.to_thread(scan_job_sync, job_id)


def build_process_command(job: Job, profile: str, mark: str, max_resolution: int = 0) -> tuple[list[str], bool]:
    if profile == "metadata":
        return [str(CLI), "metadata", str(job.input_path), "--remove", "-o", str(job.output_path)], False

    if profile == "visible":
        chosen_mark = mark if mark in VISIBLE_MARKS else "auto"
        return [
            str(CLI),
            "visible",
            str(job.input_path),
            "-o",
            str(job.output_path),
            "--mark",
            chosen_mark,
        ], False

    command = [str(CLI), "all", str(job.input_path), "-o", str(job.output_path), "--device", "cuda"]
    if profile in {"gemini", "openai"}:
        command.extend(["--force", "--pipeline", "controlnet"])
    elif profile == "sdxl":
        command.extend(["--force", "--pipeline", "sdxl"])
    elif profile != "auto":
        profile = "auto"
    if max_resolution:
        command.extend(["--max-resolution", str(max_resolution)])
    return command, True


def process_job_sync(job_id: str, profile: str = "auto", mark: str = "auto") -> None:
    job = jobs[job_id]
    job.status = "running"
    job.exit_code = None
    job.error = None
    job.profile = profile if profile in PROFILE_LABELS else "auto"
    job.visible_mark = mark if mark in VISIBLE_MARKS else "auto"
    job.progress = 0
    set_progress(job, 5, "Queued for processing")

    if not CLI.exists():
        job.status = "failed"
        job.error = f"CLI not found: {CLI}"
        job.updated_at = time.time()
        return

    attempts = [0, 1536, 1024]
    first_command, retryable = build_process_command(job, job.profile, job.visible_mark, 0)
    if not retryable:
        attempts = [0]
    del first_command

    last_text = ""
    for index, max_resolution in enumerate(attempts):
        if job.output_path.exists():
            job.output_path.unlink()
        command, _ = build_process_command(job, job.profile, job.visible_mark, max_resolution)
        code, text = run_command(job, command, "process", max_resolution)
        last_text = text
        job.exit_code = code
        job.updated_at = time.time()
        if code == 0 and job.output_path.exists():
            job.status = "done"
            set_progress(job, 100, "Done")
            return
        if retryable and code != 0 and looks_like_oom(text) and index < len(attempts) - 1:
            append_log(job, "\nCUDA OOM detected. Retrying with lower max resolution.")
            continue
        break

    job.status = "failed"
    job.error = "Processing failed. Check logs for details."
    if last_text.strip():
        job.error = last_text.strip().splitlines()[-1][-500:]
    job.updated_at = time.time()


async def process_job(job_id: str, profile: str = "auto", mark: str = "auto") -> None:
    async with job_lock:
        await asyncio.to_thread(process_job_sync, job_id, profile, mark)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML


@app.post("/api/scan")
async def scan(background_tasks: BackgroundTasks, file: UploadFile = File(...)) -> JSONResponse:
    job = create_job(file.filename or "image.png")
    save_upload(job, file)
    jobs[job.id] = job
    background_tasks.add_task(scan_job, job.id)
    return JSONResponse(job_json(job))


@app.post("/api/process/{job_id}")
async def process_existing(
    job_id: str,
    background_tasks: BackgroundTasks,
    payload: dict[str, Any] = Body(default_factory=dict),
) -> JSONResponse:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    if job.status in {"queued", "scanning", "running"}:
        raise HTTPException(status_code=409, detail="Job is busy")
    profile = str(payload.get("profile", "auto"))
    mark = str(payload.get("mark", "auto"))
    background_tasks.add_task(process_job, job.id, profile, mark)
    return JSONResponse(job_json(job))


@app.post("/api/process-direct")
async def process_direct(background_tasks: BackgroundTasks, file: UploadFile = File(...)) -> JSONResponse:
    job = create_job(file.filename or "image.png")
    save_upload(job, file)
    jobs[job.id] = job
    background_tasks.add_task(process_job, job.id, "auto", "auto")
    return JSONResponse(job_json(job))


@app.post("/api/process")
async def process_legacy(background_tasks: BackgroundTasks, file: UploadFile = File(...)) -> JSONResponse:
    return await process_direct(background_tasks, file)


@app.get("/api/status/{job_id}")
def status(job_id: str) -> JSONResponse:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    return JSONResponse(job_json(job))


@app.get("/api/input/{job_id}")
def input_file(job_id: str) -> FileResponse:
    job = jobs.get(job_id)
    if job is None or not job.input_path.exists():
        raise HTTPException(status_code=404, detail="Input not found")
    return FileResponse(job.input_path)


@app.get("/api/output/{job_id}")
def output_file(job_id: str) -> FileResponse:
    job = jobs.get(job_id)
    if job is None or not job.output_path.exists():
        raise HTTPException(status_code=404, detail="Output not found")
    return FileResponse(job.output_path, filename=job.output_path.name)


@app.post("/api/open-output")
def open_output() -> dict[str, str]:
    os.startfile(str(OUTPUT_DIR))
    return {"status": "ok"}


@app.get("/api/health")
def health() -> dict[str, object]:
    return {
        "ok": True,
        "base": str(BASE_DIR),
        "cli": CLI.exists(),
        "queue_locked": job_lock.locked(),
    }


HTML = r"""
<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Remove AI Watermarks</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101114;
      --panel: #181a1f;
      --panel-2: #20242b;
      --line: #333843;
      --text: #f4f5f7;
      --muted: #aeb5c2;
      --accent: #46c2a3;
      --danger: #ff6b6b;
      --warn: #f2b84b;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: Segoe UI, system-ui, -apple-system, sans-serif;
      letter-spacing: 0;
    }
    main {
      display: grid;
      grid-template-rows: auto 1fr;
      min-height: 100vh;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 16px 20px;
      border-bottom: 1px solid var(--line);
      background: #14161a;
    }
    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 650;
    }
    .status {
      min-width: 180px;
      text-align: right;
      color: var(--muted);
      font-size: 13px;
    }
    .workspace {
      display: grid;
      grid-template-columns: 390px 1fr;
      min-height: 0;
    }
    aside {
      border-right: 1px solid var(--line);
      background: var(--panel);
      padding: 18px;
      display: flex;
      flex-direction: column;
      gap: 14px;
      overflow: auto;
    }
    .drop {
      display: grid;
      place-items: center;
      min-height: 180px;
      border: 1px dashed #596171;
      background: var(--panel-2);
      border-radius: 8px;
      padding: 20px;
      text-align: center;
      cursor: pointer;
      transition: border-color .15s, background .15s;
    }
    .drop.drag {
      border-color: var(--accent);
      background: #1d2b29;
    }
    .drop strong {
      display: block;
      font-size: 17px;
      margin-bottom: 8px;
    }
    .drop span, .meta, .scan-result {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }
    input[type=file] { display: none; }
    button, select {
      min-height: 40px;
      border: 1px solid var(--line);
      background: #252a32;
      color: var(--text);
      border-radius: 6px;
      padding: 0 12px;
      font: inherit;
    }
    button { cursor: pointer; }
    button.primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #071411;
      font-weight: 700;
    }
    button:disabled, select:disabled {
      opacity: .5;
      cursor: not-allowed;
    }
    .actions {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .stack {
      display: grid;
      gap: 10px;
    }
    label.field {
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
    }
    .progress {
      height: 12px;
      background: #0c0d10;
      border: 1px solid var(--line);
      border-radius: 999px;
      overflow: hidden;
    }
    .progress > div {
      width: 0%;
      height: 100%;
      background: var(--accent);
      transition: width .2s ease;
    }
    .scan-result {
      min-height: 120px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #111318;
      overflow-wrap: anywhere;
    }
    .scan-result strong {
      color: var(--text);
      display: block;
      margin-bottom: 4px;
    }
    section {
      min-width: 0;
      min-height: 0;
      display: grid;
      grid-template-rows: minmax(260px, 1fr) 280px;
    }
    .preview {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 1px;
      background: var(--line);
      min-height: 0;
    }
    figure {
      margin: 0;
      background: #0c0d10;
      display: grid;
      grid-template-rows: auto 1fr;
      min-width: 0;
      min-height: 0;
    }
    figcaption {
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font-size: 13px;
    }
    .image-box {
      display: grid;
      place-items: center;
      min-height: 0;
      padding: 12px;
    }
    img {
      max-width: 100%;
      max-height: 100%;
      object-fit: contain;
    }
    .empty {
      color: #6f7786;
      font-size: 14px;
    }
    .log-wrap {
      border-top: 1px solid var(--line);
      background: #0b0c0f;
      min-height: 0;
      display: grid;
      grid-template-rows: auto 1fr;
    }
    .log-head {
      padding: 9px 12px;
      color: var(--muted);
      border-bottom: 1px solid #222630;
      font-size: 13px;
    }
    pre {
      margin: 0;
      padding: 12px;
      overflow: auto;
      white-space: pre-wrap;
      color: #d7dde8;
      font: 12px Consolas, ui-monospace, monospace;
    }
    .good { color: var(--accent); }
    .bad { color: var(--danger); }
    .warn { color: var(--warn); }
    @media (max-width: 900px) {
      .workspace { grid-template-columns: 1fr; }
      aside { border-right: 0; border-bottom: 1px solid var(--line); }
      section { grid-template-rows: minmax(360px, 1fr) 260px; }
      .preview { grid-template-columns: 1fr; }
      .status { text-align: left; min-width: 0; }
      header { align-items: flex-start; flex-direction: column; }
    }
  </style>
</head>
<body>
<main>
  <header>
    <h1>Remove AI Watermarks</h1>
    <div id="status" class="status">Pret</div>
  </header>
  <div class="workspace">
    <aside>
      <label id="drop" class="drop">
        <input id="file" type="file" accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp">
        <div>
          <strong>Drop image</strong>
          <span id="fileName">PNG, JPG, JPEG, WEBP</span>
        </div>
      </label>

      <div class="progress"><div id="bar"></div></div>
      <div id="meta" class="meta">Mode scan puis suppression, ou traitement direct.</div>

      <div class="actions">
        <button id="scan" class="primary" disabled>Scanner</button>
        <button id="direct" disabled>Traiter direct</button>
      </div>

      <div class="stack">
        <label class="field">Profil
          <select id="profile">
            <option value="auto">Auto d'apres scan</option>
            <option value="gemini">Gemini / Google</option>
            <option value="openai">ChatGPT / OpenAI</option>
            <option value="sdxl">Stable Diffusion / FLUX</option>
            <option value="metadata">Metadonnees seulement</option>
            <option value="visible">Visible seulement</option>
          </select>
        </label>
        <label class="field">Marque visible
          <select id="mark" disabled>
            <option value="auto">Auto</option>
            <option value="gemini">Gemini</option>
            <option value="doubao">Doubao</option>
            <option value="jimeng">Jimeng</option>
            <option value="samsung">Samsung</option>
          </select>
        </label>
      </div>

      <button id="clean" class="primary" disabled>Supprimer avec choix</button>
      <div class="actions">
        <button id="download" disabled>Telecharger</button>
        <button id="folder">Output</button>
      </div>
      <div id="scanResult" class="scan-result">Aucun scan pour le moment.</div>
    </aside>
    <section>
      <div class="preview">
        <figure>
          <figcaption>Input</figcaption>
          <div class="image-box"><img id="before" alt=""><div id="beforeEmpty" class="empty">Aucune image</div></div>
        </figure>
        <figure>
          <figcaption>Output</figcaption>
          <div class="image-box"><img id="after" alt=""><div id="afterEmpty" class="empty">En attente</div></div>
        </figure>
      </div>
      <div class="log-wrap">
        <div class="log-head">Logs</div>
        <pre id="log"></pre>
      </div>
    </section>
  </div>
</main>
<script>
const fileInput = document.getElementById('file');
const drop = document.getElementById('drop');
const scanBtn = document.getElementById('scan');
const directBtn = document.getElementById('direct');
const cleanBtn = document.getElementById('clean');
const download = document.getElementById('download');
const folder = document.getElementById('folder');
const profile = document.getElementById('profile');
const mark = document.getElementById('mark');
const statusEl = document.getElementById('status');
const fileName = document.getElementById('fileName');
const before = document.getElementById('before');
const after = document.getElementById('after');
const beforeEmpty = document.getElementById('beforeEmpty');
const afterEmpty = document.getElementById('afterEmpty');
const logEl = document.getElementById('log');
const meta = document.getElementById('meta');
const scanResult = document.getElementById('scanResult');
const bar = document.getElementById('bar');
let selectedFile = null;
let currentJob = null;
let outputUrl = null;

function setStatus(text, cls='') {
  statusEl.className = 'status ' + cls;
  statusEl.textContent = text;
}

function setProgress(value) {
  const v = Math.max(0, Math.min(100, Number(value) || 0));
  bar.style.width = v + '%';
}

function setBusy(isBusy) {
  scanBtn.disabled = isBusy || !selectedFile;
  directBtn.disabled = isBusy || !selectedFile;
  cleanBtn.disabled = isBusy || !currentJob;
  profile.disabled = isBusy;
  mark.disabled = isBusy || profile.value !== 'visible';
}

function selectFile(file) {
  selectedFile = file;
  currentJob = null;
  outputUrl = null;
  fileName.textContent = file.name;
  download.disabled = true;
  after.removeAttribute('src');
  afterEmpty.style.display = 'block';
  before.src = URL.createObjectURL(file);
  beforeEmpty.style.display = 'none';
  logEl.textContent = '';
  scanResult.textContent = 'Pret a scanner.';
  setProgress(0);
  setStatus('Pret');
  setBusy(false);
}

function renderScan(result) {
  if (!result) {
    scanResult.textContent = 'Aucun scan disponible.';
    return;
  }
  const verdict = result.is_ai_generated === true
    ? 'IA detectee'
    : result.is_ai_generated === false
      ? 'Non IA selon signaux locaux'
      : 'Inconnue / non detectable localement';
  const lines = [];
  lines.push('<strong>' + verdict + '</strong>');
  lines.push('Plateforme: ' + (result.platform || 'indeterminee'));
  lines.push('Confiance: ' + (result.confidence || 'none'));
  if (result.ai_source_kind) lines.push('Type: ' + result.ai_source_kind);
  const watermarks = result.watermarks || [];
  lines.push('Watermarks: ' + (watermarks.length ? watermarks.join(', ') : 'aucun signal lisible'));
  const signals = result.signals || [];
  if (signals.length) lines.push('Signaux: ' + signals.join(', '));
  const caveats = result.caveats || [];
  if (caveats.length) lines.push('<br><span class="warn">' + caveats.join(' ') + '</span>');
  else lines.push('<br><span class="warn">Unknown ne veut pas dire clean: certains watermarks pixel ne sont pas localement detectables.</span>');
  scanResult.innerHTML = lines.join('<br>');
}

fileInput.addEventListener('change', () => {
  if (fileInput.files && fileInput.files[0]) selectFile(fileInput.files[0]);
});

drop.addEventListener('dragover', e => {
  e.preventDefault();
  drop.classList.add('drag');
});
drop.addEventListener('dragleave', () => drop.classList.remove('drag'));
drop.addEventListener('drop', e => {
  e.preventDefault();
  drop.classList.remove('drag');
  if (e.dataTransfer.files && e.dataTransfer.files[0]) selectFile(e.dataTransfer.files[0]);
});

profile.addEventListener('change', () => {
  mark.disabled = profile.value !== 'visible';
});

scanBtn.addEventListener('click', async () => {
  if (!selectedFile) return;
  setBusy(true);
  download.disabled = true;
  logEl.textContent = 'Upload + scan...\n';
  setProgress(5);
  setStatus('Scan', 'warn');
  const data = new FormData();
  data.append('file', selectedFile);
  const response = await fetch('/api/scan', { method: 'POST', body: data });
  const job = await response.json();
  currentJob = job.id;
  poll(job.id);
});

directBtn.addEventListener('click', async () => {
  if (!selectedFile) return;
  setBusy(true);
  download.disabled = true;
  scanResult.textContent = 'Traitement direct sans scan prealable.';
  logEl.textContent = 'Upload + traitement direct...\n';
  setProgress(5);
  setStatus('Traitement direct', 'warn');
  const data = new FormData();
  data.append('file', selectedFile);
  const response = await fetch('/api/process-direct', { method: 'POST', body: data });
  const job = await response.json();
  currentJob = job.id;
  poll(job.id);
});

cleanBtn.addEventListener('click', async () => {
  if (!currentJob) return;
  setBusy(true);
  download.disabled = true;
  setProgress(0);
  setStatus('Suppression', 'warn');
  await fetch('/api/process/' + currentJob, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ profile: profile.value, mark: mark.value })
  });
  poll(currentJob);
});

async function poll(jobId) {
  const response = await fetch('/api/status/' + jobId);
  const job = await response.json();
  currentJob = job.id;
  logEl.textContent = job.log || '';
  logEl.scrollTop = logEl.scrollHeight;
  setProgress(job.progress || 0);
  meta.textContent = (job.progress || 0) + '% - ' + (job.phase || job.status);
  if (job.scan_result) renderScan(job.scan_result);
  if (job.status === 'queued') setStatus('En file', 'warn');
  if (job.status === 'scanning') setStatus('Scan ' + (job.progress || 0) + '%', 'warn');
  if (job.status === 'scanned') {
    setStatus('Scan termine', 'good');
    setBusy(false);
    cleanBtn.disabled = false;
    return;
  }
  if (job.status === 'running') setStatus('Traitement ' + (job.progress || 0) + '%', 'warn');
  if (job.status === 'done') {
    setStatus('Termine', 'good');
    outputUrl = job.output_url;
    after.src = outputUrl + '?t=' + Date.now();
    afterEmpty.style.display = 'none';
    download.disabled = false;
    setBusy(false);
    return;
  }
  if (job.status === 'failed') {
    setStatus('Erreur', 'bad');
    meta.textContent = job.error || 'Erreur';
    setBusy(false);
    return;
  }
  setTimeout(() => poll(jobId), 1000);
}

download.addEventListener('click', () => {
  if (outputUrl) window.location.href = outputUrl;
});

folder.addEventListener('click', async () => {
  await fetch('/api/open-output', { method: 'POST' });
});
</script>
</body>
</html>
"""
