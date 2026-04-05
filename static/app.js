const form = document.getElementById("mosaic-form");
const statusNode = document.getElementById("status");
const generateButton = document.getElementById("generateButton");
const previewImage = document.getElementById("previewImage");
const previewWrap = document.getElementById("previewWrap");
const downloadLink = document.getElementById("downloadLink");
const progressContainer = document.getElementById("progressContainer");
const progressFill = document.getElementById("progressFill");
const progressText = document.getElementById("progressText");

const CHUNK_SIZE_BYTES = 512 * 1024;

let currentObjectUrl = null;
let currentJobId = null;
let progressSocket = null;
let progressEventSource = null;
let mosaicReady = false;
let displayedProgress = 0;
let displayedGenerationPercent = 0;

function setProgress(percent, text) {
  const numericPercent = Number(percent);
  const clamped = Number.isFinite(numericPercent)
    ? Math.max(0, Math.min(100, Math.round(numericPercent)))
    : displayedProgress;
  displayedProgress = Math.max(displayedProgress, clamped);
  progressFill.style.width = `${displayedProgress}%`;
  if (text) {
    progressText.textContent = text;
  }
}

function closeProgressSocket() {
  if (!progressSocket) {
    return;
  }
  progressSocket.onopen = null;
  progressSocket.onmessage = null;
  progressSocket.onerror = null;
  progressSocket.onclose = null;
  progressSocket.close();
  progressSocket = null;
}

function closeProgressEventSource() {
  if (!progressEventSource) {
    return;
  }
  progressEventSource.onopen = null;
  progressEventSource.onmessage = null;
  progressEventSource.onerror = null;
  progressEventSource.close();
  progressEventSource = null;
}

function setStatus(message, kind = "") {
  statusNode.className = `status ${kind}`.trim();
  statusNode.textContent = message;
}

function updateFileDisplay(inputId) {
  const input = document.getElementById(inputId);
  const label = input.nextElementSibling;
  const filenameSpan = label.querySelector(".upload-filename");
  
  if (input.files.length > 0) {
    if (input.multiple) {
      filenameSpan.textContent = `${input.files.length} files selected`;
    } else {
      filenameSpan.textContent = input.files[0].name;
    }
    label.classList.add("has-files");
  } else {
    filenameSpan.textContent = "";
    label.classList.remove("has-files");
  }
}

function toSafeNumber(value, fallback = 0) {
  const parsed = Number(value);
  if (Number.isFinite(parsed)) {
    return parsed;
  }
  return fallback;
}

function resetGenerationState() {
  currentJobId = null;
  generateButton.disabled = false;
}

function showGenerationError(message) {
  progressContainer.hidden = true;
  statusNode.hidden = false;
  setStatus(message, "error");
  resetGenerationState();
}

async function showGeneratedMosaic(jobId) {
  const downloadResponse = await fetch(`/api/generate/download/${jobId}`);
  if (!downloadResponse.ok) {
    const payload = await downloadResponse.json().catch(() => ({ error: "Download failed." }));
    showGenerationError(payload.error || "Download failed.");
    return;
  }

  const blob = await downloadResponse.blob();

  if (currentObjectUrl) {
    URL.revokeObjectURL(currentObjectUrl);
  }
  currentObjectUrl = URL.createObjectURL(blob);

  previewImage.src = currentObjectUrl;
  previewWrap.hidden = false;

  downloadLink.href = currentObjectUrl;
  downloadLink.hidden = false;

  setProgress(100, "Done (100%)");

  setTimeout(() => {
    progressContainer.hidden = true;
    statusNode.hidden = false;
    setStatus("Mosaic ready!", "success");
  }, 600);

  currentJobId = null;
  mosaicReady = true;
  generateButton.disabled = true;
}

async function processJobStatus(statusPayload, jobId) {
  const progress = Math.max(0, Math.min(100, toSafeNumber(statusPayload.progress, 0)));
  displayedGenerationPercent = Math.max(displayedGenerationPercent, progress);
  const stage = statusPayload.stage || "Working";
  const visualProgress = 20 + Math.round(displayedGenerationPercent * 0.8);

  setProgress(visualProgress, `${stage} (${displayedGenerationPercent}%)`);

  if (statusPayload.state === "error") {
    showGenerationError(statusPayload.error || "Generation failed.");
    return true;
  }

  if (statusPayload.state !== "done") {
    return false;
  }

  await showGeneratedMosaic(jobId);
  return true;
}

async function connectProgressSocket(jobId) {
  const wsProtocol = window.location.protocol === "https:" ? "wss" : "ws";
  const wsUrl = `${wsProtocol}://${window.location.host}/api/generate/ws/${encodeURIComponent(jobId)}`;

  return new Promise((resolve, reject) => {
    let opened = false;

    try {
      progressSocket = new WebSocket(wsUrl);
    } catch (wsConstructionError) {
      reject(wsConstructionError);
      return;
    }

    progressSocket.onopen = () => {
      opened = true;
      resolve();
    };

    progressSocket.onmessage = async (event) => {
      try {
        const statusPayload = JSON.parse(event.data);
        const finished = await processJobStatus(statusPayload, currentJobId);
        if (finished) {
          closeProgressSocket();
        }
      } catch (messageError) {
        closeProgressSocket();
        reject(messageError);
      }
    };

    progressSocket.onerror = () => {
      if (!opened) {
        reject(new Error("WebSocket connection failed."));
      }
    };

    progressSocket.onclose = () => {
      if (!opened) {
        reject(new Error("WebSocket closed before opening."));
      }
    };
  });
}

async function connectProgressEventStream(jobId) {
  if (typeof EventSource === "undefined") {
    throw new Error("EventSource is not available.");
  }

  const eventsUrl = `/api/generate/events/${encodeURIComponent(jobId)}`;

  return new Promise((resolve, reject) => {
    let opened = false;

    progressEventSource = new EventSource(eventsUrl);

    progressEventSource.onopen = () => {
      opened = true;
      resolve();
    };

    progressEventSource.onmessage = async (event) => {
      try {
        const statusPayload = JSON.parse(event.data);
        const finished = await processJobStatus(statusPayload, currentJobId);
        if (finished) {
          closeProgressEventSource();
        }
      } catch (messageError) {
        closeProgressEventSource();
        showGenerationError("Progress stream returned invalid data.");
      }
    };

    progressEventSource.onerror = () => {
      if (!opened) {
        closeProgressEventSource();
        reject(new Error("Event stream connection failed."));
        return;
      }

      if (currentJobId) {
        closeProgressEventSource();
        showGenerationError("Live progress stream disconnected.");
      }
    };
  });
}

async function createUploadSession() {
  const response = await fetch("/api/upload/session", { method: "POST" });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ error: "Failed to create upload session." }));
    throw new Error(payload.error || "Failed to create upload session.");
  }
  const payload = await response.json();
  return payload.uploadId;
}

async function uploadFileInChunks(uploadId, file, fileRole, fileIndex, onChunkUploaded) {
  const totalChunks = Math.max(1, Math.ceil(file.size / CHUNK_SIZE_BYTES));

  for (let chunkIndex = 0; chunkIndex < totalChunks; chunkIndex += 1) {
    const start = chunkIndex * CHUNK_SIZE_BYTES;
    const end = Math.min(start + CHUNK_SIZE_BYTES, file.size);
    const chunkBlob = file.slice(start, end);

    const data = new FormData();
    data.append("uploadId", uploadId);
    data.append("fileRole", fileRole);
    data.append("fileName", file.name);
    data.append("fileIndex", String(fileIndex));
    data.append("chunkIndex", String(chunkIndex));
    data.append("totalChunks", String(totalChunks));
    data.append("chunk", chunkBlob, `${file.name}.part`);

    const response = await fetch("/api/upload/chunk", {
      method: "POST",
      body: data,
    });

    if (!response.ok) {
      const payload = await response.json().catch(() => ({ error: `Failed uploading ${file.name}.` }));
      throw new Error(payload.error || `Failed uploading ${file.name}.`);
    }

    onChunkUploaded(chunkBlob.size);
  }
}

async function uploadAllFilesChunked(targetFile, pieceFiles) {
  const uploadId = await createUploadSession();
  const totalBytes = targetFile.size + pieceFiles.reduce((sum, file) => sum + file.size, 0);
  let uploadedBytes = 0;

  const handleProgress = (chunkSize) => {
    uploadedBytes += chunkSize;
    const ratio = totalBytes > 0 ? uploadedBytes / totalBytes : 1;
    const percent = Math.max(0, Math.min(100, Math.round(ratio * 100)));
    setProgress(Math.round(percent * 0.2), `Uploading files (${percent}%)`);
  };

  await uploadFileInChunks(uploadId, targetFile, "target", 0, handleProgress);

  for (let index = 0; index < pieceFiles.length; index += 1) {
    await uploadFileInChunks(uploadId, pieceFiles[index], "piece", index, handleProgress);
  }

  return uploadId;
}

async function startChunkedGeneration(uploadId) {
  const data = new FormData();
  data.append("uploadId", uploadId);
  data.append("blockSize", document.getElementById("blockSize").value);
  data.append("matchResolution", document.getElementById("matchResolution").value);
  data.append("enlargement", document.getElementById("enlargement").value);
  data.append("overlayAlpha", document.getElementById("overlayAlpha").value);

  const response = await fetch("/api/generate/start-chunked", {
    method: "POST",
    body: data,
  });

  if (!response.ok) {
    const payload = await response.json().catch(() => ({ error: "Generation failed." }));
    throw new Error(payload.error || "Generation failed.");
  }

  return response.json();
}

document.getElementById("targetImage").addEventListener("change", () => {
  updateFileDisplay("targetImage");
});

document.getElementById("pieceImages").addEventListener("change", () => {
  updateFileDisplay("pieceImages");
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();

  if (mosaicReady) {
    setStatus("Mosaic is already ready.", "success");
    return;
  }

  const targetInput = document.getElementById("targetImage");
  const piecesInput = document.getElementById("pieceImages");

  if (!targetInput.files.length) {
    setStatus("Please choose a main image.", "error");
    return;
  }

  if (piecesInput.files.length < 4) {
    setStatus("Please add at least 4 piece images.", "error");
    return;
  }

  generateButton.disabled = true;
  closeProgressSocket();
  closeProgressEventSource();
  progressContainer.hidden = false;
  displayedProgress = 0;
  displayedGenerationPercent = 0;
  setProgress(0, "Preparing upload...");
  statusNode.hidden = true;
  downloadLink.hidden = true;
  previewWrap.hidden = true;

  try {
    const targetFile = targetInput.files[0];
    const pieceFiles = Array.from(piecesInput.files);

    progressText.textContent = "Creating upload session...";
    const uploadId = await uploadAllFilesChunked(targetFile, pieceFiles);

    setProgress(20, "Starting generation...");
    const startPayload = await startChunkedGeneration(uploadId);
    currentJobId = startPayload.jobId;

    try {
      await connectProgressSocket(currentJobId);
      return;
    } catch (connectionError) {
      closeProgressSocket();
      setProgress(displayedProgress, "WebSocket unavailable. Switching to stream...");
    }

    try {
      await connectProgressEventStream(currentJobId);
    } catch (connectionError) {
      closeProgressEventSource();
      showGenerationError("Could not open a live progress channel.");
      return;
    }
  } catch (error) {
    closeProgressSocket();
    closeProgressEventSource();
    showGenerationError("Could not reach the server. Try again.");
  }
});
