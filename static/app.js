const form = document.getElementById("mosaic-form");
const statusNode = document.getElementById("status");
const generateButton = document.getElementById("generateButton");
const previewImage = document.getElementById("previewImage");
const previewWrap = document.getElementById("previewWrap");
const downloadLink = document.getElementById("downloadLink");

let currentObjectUrl = null;

function setStatus(message, kind = "") {
  statusNode.className = `status ${kind}`.trim();
  statusNode.textContent = message;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();

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
  setStatus("Generating mosaic. This may take a minute...", "");

  const data = new FormData(form);

  try {
    const response = await fetch("/api/generate", {
      method: "POST",
      body: data,
    });

    if (!response.ok) {
      const payload = await response.json().catch(() => ({ error: "Unexpected server error." }));
      setStatus(payload.error || "Generation failed.", "error");
      return;
    }

    const blob = await response.blob();
    if (currentObjectUrl) {
      URL.revokeObjectURL(currentObjectUrl);
    }
    currentObjectUrl = URL.createObjectURL(blob);

    previewImage.src = currentObjectUrl;
    previewWrap.hidden = false;

    downloadLink.href = currentObjectUrl;
    downloadLink.hidden = false;

    setStatus("Mosaic ready.", "success");
  } catch (error) {
    setStatus("Could not reach the server. Try again.", "error");
  } finally {
    generateButton.disabled = false;
  }
});
