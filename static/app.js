const form = document.getElementById("mosaic-form");
const statusNode = document.getElementById("status");
const generateButton = document.getElementById("generateButton");
const previewImage = document.getElementById("previewImage");
const previewWrap = document.getElementById("previewWrap");
const downloadLink = document.getElementById("downloadLink");
const progressContainer = document.getElementById("progressContainer");
const progressFill = document.getElementById("progressFill");
const progressText = document.getElementById("progressText");

let currentObjectUrl = null;
let currentJobId = null;
let progressSocket = null;
let mosaicReady = false;

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
  progressContainer.hidden = false;
  progressFill.style.width = "0%";
  progressText.textContent = "Queued (0%)";
  statusNode.hidden = true;
  downloadLink.hidden = true;
  previewWrap.hidden = true;

  const data = new FormData(form);

  try {
    const startResponse = await fetch("/api/generate/start", {
      method: "POST",
      body: data,
    });

    if (!startResponse.ok) {
      progressContainer.hidden = true;
      statusNode.hidden = false;
      const payload = await startResponse.json().catch(() => ({ error: "Unexpected server error." }));
      setStatus(payload.error || "Generation failed.", "error");
      generateButton.disabled = false;
      return;
    }

    const startPayload = await startResponse.json();
    currentJobId = startPayload.jobId;

    const wsProtocol = window.location.protocol === "https:" ? "wss" : "ws";
    const wsUrl = `${wsProtocol}://${window.location.host}/api/generate/ws/${encodeURIComponent(currentJobId)}`;
    progressSocket = new WebSocket(wsUrl);

    progressSocket.onmessage = async (event) => {
      try {
        const statusPayload = JSON.parse(event.data);
        const progress = Math.max(0, Math.min(100, Number(statusPayload.progress || 0)));
        const stage = statusPayload.stage || "Working";

        progressFill.style.width = `${progress}%`;
        progressText.textContent = `${stage} (${progress}%)`;

        if (statusPayload.state === "error") {
          closeProgressSocket();
          progressContainer.hidden = true;
          statusNode.hidden = false;
          setStatus(statusPayload.error || "Generation failed.", "error");
          currentJobId = null;
          generateButton.disabled = false;
          return;
        }

        if (statusPayload.state !== "done") {
          return;
        }

        closeProgressSocket();

        const downloadResponse = await fetch(`/api/generate/download/${currentJobId}`);
        if (!downloadResponse.ok) {
          const payload = await downloadResponse.json().catch(() => ({ error: "Download failed." }));
          progressContainer.hidden = true;
          statusNode.hidden = false;
          setStatus(payload.error || "Download failed.", "error");
          currentJobId = null;
          generateButton.disabled = false;
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

        progressFill.style.width = "100%";
        progressText.textContent = "Done (100%)";

        setTimeout(() => {
          progressContainer.hidden = true;
          statusNode.hidden = false;
          setStatus("Mosaic ready!", "success");
        }, 600);

        currentJobId = null;
        mosaicReady = true;
        generateButton.disabled = true;
      } catch (messageError) {
        closeProgressSocket();
        progressContainer.hidden = true;
        statusNode.hidden = false;
        setStatus("Could not process live status updates. Try again.", "error");
        currentJobId = null;
        generateButton.disabled = false;
      }
    };

    progressSocket.onerror = () => {
      closeProgressSocket();
      progressContainer.hidden = true;
      statusNode.hidden = false;
      setStatus("Live connection failed. Try again.", "error");
      currentJobId = null;
      generateButton.disabled = false;
    };
  } catch (error) {
    closeProgressSocket();
    progressContainer.hidden = true;
    statusNode.hidden = false;
    setStatus("Could not reach the server. Try again.", "error");
    currentJobId = null;
    generateButton.disabled = false;
  }
});
