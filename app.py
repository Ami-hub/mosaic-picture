from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from queue import Empty, Queue
from tempfile import TemporaryDirectory
from threading import Lock, Thread
from uuid import uuid4

from flask import Flask, jsonify, render_template, request, send_file
from flask_sock import Sock
from PIL import Image
from werkzeug.datastructures import FileStorage
from pillow_heif import register_heif_opener
from photomosaic import create_mosaic_from_paths, create_mosaic_config

app = Flask(__name__, template_folder="templates", static_folder="static")
sock = Sock(app)

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".heif"}
JOBS_LOCK = Lock()
JOBS: dict[str, dict] = {}
JOB_SUBSCRIBERS: dict[str, list[Queue]] = {}
UPLOAD_SESSIONS_LOCK = Lock()
UPLOAD_SESSIONS: dict[str, dict] = {}

register_heif_opener()

def _job_payload(job: dict) -> dict:
    return {
        "state": job["state"],
        "stage": job["stage"],
        "progress": job["progress"],
        "error": job["error"],
        "ready": job["state"] == "done",
    }


def _publish_job_update(job_id: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        payload = _job_payload(job)
        subscribers = list(JOB_SUBSCRIBERS.get(job_id, []))

    for subscriber_queue in subscribers:
        try:
            subscriber_queue.put_nowait(payload)
        except Exception:
            pass


def _register_job_subscriber(job_id: str) -> Queue:
    subscriber_queue: Queue = Queue()
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise KeyError(job_id)
        JOB_SUBSCRIBERS.setdefault(job_id, []).append(subscriber_queue)
        subscriber_queue.put_nowait(_job_payload(job))
    return subscriber_queue


def _unregister_job_subscriber(job_id: str, subscriber_queue: Queue) -> None:
    with JOBS_LOCK:
        queues = JOB_SUBSCRIBERS.get(job_id)
        if not queues:
            return
        if subscriber_queue in queues:
            queues.remove(subscriber_queue)
        if not queues:
            JOB_SUBSCRIBERS.pop(job_id, None)


def _set_job(job_id: str, **fields) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(fields)
    _publish_job_update(job_id)


def _get_job(job_id: str) -> dict | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        return dict(job)


def _create_job() -> str:
    job_id = uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "state": "queued",
            "stage": "Queued",
            "progress": 0,
            "error": None,
            "result": None,
        }
        JOB_SUBSCRIBERS[job_id] = []
    return job_id


def _create_upload_session() -> str:
    upload_id = uuid4().hex
    with UPLOAD_SESSIONS_LOCK:
        UPLOAD_SESSIONS[upload_id] = {
            "target": None,
            "pieces": {},
        }
    return upload_id


def _assemble_chunked_file(file_record: dict) -> bytes:
    total_chunks = file_record["total_chunks"]
    chunks: dict[int, bytes] = file_record["chunks"]
    if file_record["received_chunks"] != total_chunks:
        raise ValueError(f"File '{file_record['filename']}' is incomplete.")
    return b"".join(chunks[idx] for idx in range(total_chunks))


def _has_allowed_extension(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def _parse_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = request.form.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _parse_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = request.form.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/favicon.ico")
def favicon():
    return send_file("static/favicon.ico", mimetype="image/x-icon") 

@app.post("/api/generate")
def generate():
    target = request.files.get("targetImage")
    pieces = request.files.getlist("pieceImages")

    if target is None or target.filename == "":
        return jsonify({"error": "Please upload a main image."}), 400
    target_filename = target.filename
    if target_filename is None or target_filename == "":
        return jsonify({"error": "Please upload a main image."}), 400
    if not _has_allowed_extension(target_filename):
        return jsonify({"error": "Main image format is not supported."}), 400

    valid_piece_files: list[tuple[FileStorage, str]] = []
    for uploaded_piece in pieces:
        filename = uploaded_piece.filename if uploaded_piece else None
        if uploaded_piece and filename:
            valid_piece_files.append((uploaded_piece, filename))

    if len(valid_piece_files) < 4:
        return jsonify({"error": "Upload at least 4 piece images."}), 400

    for _, piece_filename in valid_piece_files:
        if not _has_allowed_extension(piece_filename):
            return jsonify({"error": f"Unsupported piece image: {piece_filename}"}), 400

    try:
        block_size_px = _parse_int("blockSize", 42, 10, 128)
        block_match_res = _parse_int("matchResolution", 18, 4, 64)
        enlargement = _parse_int("enlargement", 4, 1, 8)
        overlay_alpha = _parse_float("overlayAlpha", 0.42, 0.0, 1.0)
    except ValueError as err:
        return jsonify({"error": str(err)}), 400

    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        source_path = temp_path / "source" / target_filename
        pieces_path = temp_path / "pieces"
        output_path = temp_path / f"mosaic-{uuid4().hex}.jpeg"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        pieces_path.mkdir(parents=True, exist_ok=True)

        target.save(source_path)
        try:
            # Validate the source image early for clearer errors.
            with Image.open(source_path):
                pass
        except Exception:
            return jsonify({"error": "The main image appears to be invalid."}), 400

        for index, (piece, piece_filename) in enumerate(valid_piece_files, start=1):
            piece_name = f"piece-{index}{Path(piece_filename).suffix.lower()}"
            piece.save(pieces_path / piece_name)

        config = create_mosaic_config(
            block_size_px=block_size_px,
            block_match_res=block_match_res,
            enlargement=enlargement,
            overlay_alpha=overlay_alpha,
        )

        try:
            create_mosaic_from_paths(str(source_path), str(pieces_path), str(output_path), config)
        except Exception as err:
            return jsonify({"error": f"Generation failed: {err}"}), 500

        output_bytes = output_path.read_bytes()

        return send_file(
            BytesIO(output_bytes),
            mimetype="image/jpeg",
            as_attachment=True,
            download_name="mosaic-output.jpeg",
        )


@app.post("/api/generate/start")
def start_generate_job():
    target = request.files.get("targetImage")
    pieces = request.files.getlist("pieceImages")

    if target is None or target.filename == "":
        return jsonify({"error": "Please upload a main image."}), 400
    target_filename = target.filename
    if target_filename is None or target_filename == "":
        return jsonify({"error": "Please upload a main image."}), 400
    if not _has_allowed_extension(target_filename):
        return jsonify({"error": "Main image format is not supported."}), 400

    valid_piece_files: list[tuple[FileStorage, str]] = []
    for uploaded_piece in pieces:
        filename = uploaded_piece.filename if uploaded_piece else None
        if uploaded_piece and filename:
            valid_piece_files.append((uploaded_piece, filename))

    if len(valid_piece_files) < 4:
        return jsonify({"error": "Upload at least 4 piece images."}), 400

    for _, piece_filename in valid_piece_files:
        if not _has_allowed_extension(piece_filename):
            return jsonify({"error": f"Unsupported piece image: {piece_filename}"}), 400

    try:
        block_size_px = _parse_int("blockSize", 42, 10, 128)
        block_match_res = _parse_int("matchResolution", 18, 4, 64)
        enlargement = _parse_int("enlargement", 4, 1, 8)
        overlay_alpha = _parse_float("overlayAlpha", 0.42, 0.0, 1.0)
    except ValueError as err:
        return jsonify({"error": str(err)}), 400

    target_bytes = target.read()
    pieces_bytes: list[tuple[str, bytes]] = []
    for uploaded_piece, piece_filename in valid_piece_files:
        pieces_bytes.append((piece_filename, uploaded_piece.read()))

    config = create_mosaic_config(
        block_size_px=block_size_px,
        block_match_res=block_match_res,
        enlargement=enlargement,
        overlay_alpha=overlay_alpha,
    )

    job_id = _create_job()

    def run_job() -> None:
        _set_job(job_id, state="running", stage="Starting", progress=1)

        def on_progress(stage: str, progress: int) -> None:
            _set_job(job_id, state="running", stage=stage, progress=max(0, min(100, int(progress))))

        try:
            with TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                source_path = temp_path / "source" / target_filename
                pieces_path = temp_path / "pieces"
                output_path = temp_path / f"mosaic-{uuid4().hex}.jpeg"
                source_path.parent.mkdir(parents=True, exist_ok=True)
                pieces_path.mkdir(parents=True, exist_ok=True)

                source_path.write_bytes(target_bytes)

                try:
                    with Image.open(source_path):
                        pass
                except Exception:
                    _set_job(job_id, state="error", stage="Validation failed", error="The main image appears to be invalid.")
                    return

                for index, (piece_filename, piece_data) in enumerate(pieces_bytes, start=1):
                    piece_name = f"piece-{index}{Path(piece_filename).suffix.lower()}"
                    (pieces_path / piece_name).write_bytes(piece_data)

                create_mosaic_from_paths(
                    str(source_path),
                    str(pieces_path),
                    str(output_path),
                    config,
                    progress_callback=on_progress,
                )

                result_bytes = output_path.read_bytes()

            _set_job(job_id, state="done", stage="Done", progress=100, result=result_bytes)
        except Exception as err:
            _set_job(job_id, state="error", stage="Failed", error=f"Generation failed: {err}")

    Thread(target=run_job, daemon=True).start()
    return jsonify({"jobId": job_id})


@app.post("/api/upload/session")
def create_upload_session():
    upload_id = _create_upload_session()
    return jsonify({"uploadId": upload_id})


@app.post("/api/upload/chunk")
def upload_chunk():
    upload_id = request.form.get("uploadId", "")
    file_role = request.form.get("fileRole", "")
    file_name = request.form.get("fileName", "")
    raw_chunk_index = request.form.get("chunkIndex", "")
    raw_total_chunks = request.form.get("totalChunks", "")
    raw_file_index = request.form.get("fileIndex", "")
    chunk_file = request.files.get("chunk")

    if not upload_id:
        return jsonify({"error": "uploadId is required."}), 400
    if file_role not in {"target", "piece"}:
        return jsonify({"error": "fileRole must be 'target' or 'piece'."}), 400
    if not file_name:
        return jsonify({"error": "fileName is required."}), 400
    if not _has_allowed_extension(file_name):
        return jsonify({"error": f"Unsupported image format: {file_name}"}), 400
    if chunk_file is None:
        return jsonify({"error": "chunk file is required."}), 400

    try:
        chunk_index = int(raw_chunk_index)
        total_chunks = int(raw_total_chunks)
    except ValueError:
        return jsonify({"error": "chunkIndex and totalChunks must be integers."}), 400

    if total_chunks <= 0:
        return jsonify({"error": "totalChunks must be greater than 0."}), 400
    if chunk_index < 0 or chunk_index >= total_chunks:
        return jsonify({"error": "chunkIndex is out of range."}), 400

    file_index = None
    if file_role == "piece":
        try:
            file_index = int(raw_file_index)
        except ValueError:
            return jsonify({"error": "fileIndex is required for piece uploads."}), 400
        if file_index < 0:
            return jsonify({"error": "fileIndex must be non-negative."}), 400

    chunk_bytes = chunk_file.read()

    with UPLOAD_SESSIONS_LOCK:
        session = UPLOAD_SESSIONS.get(upload_id)
        if not session:
            return jsonify({"error": "Upload session not found."}), 404

        if file_role == "target":
            file_record = session.get("target")
            if file_record is None:
                file_record = {
                    "filename": file_name,
                    "total_chunks": total_chunks,
                    "chunks": {},
                    "received_chunks": 0,
                }
                session["target"] = file_record
        else:
            pieces: dict[int, dict] = session["pieces"]
            file_record = pieces.get(file_index)
            if file_record is None:
                file_record = {
                    "filename": file_name,
                    "total_chunks": total_chunks,
                    "chunks": {},
                    "received_chunks": 0,
                }
                pieces[file_index] = file_record

        if file_record["filename"] != file_name:
            return jsonify({"error": "fileName mismatch for this upload target."}), 400
        if file_record["total_chunks"] != total_chunks:
            return jsonify({"error": "totalChunks mismatch for this upload target."}), 400

        chunks: dict[int, bytes] = file_record["chunks"]
        if chunk_index not in chunks:
            chunks[chunk_index] = chunk_bytes
            file_record["received_chunks"] += 1

        return jsonify({
            "receivedChunks": file_record["received_chunks"],
            "totalChunks": file_record["total_chunks"],
        })


@app.post("/api/generate/start-chunked")
def start_generate_job_chunked():
    upload_id = request.form.get("uploadId", "")
    if not upload_id:
        return jsonify({"error": "uploadId is required."}), 400

    try:
        block_size_px = _parse_int("blockSize", 42, 10, 128)
        block_match_res = _parse_int("matchResolution", 18, 4, 64)
        enlargement = _parse_int("enlargement", 4, 1, 8)
        overlay_alpha = _parse_float("overlayAlpha", 0.42, 0.0, 1.0)
    except ValueError as err:
        return jsonify({"error": str(err)}), 400

    with UPLOAD_SESSIONS_LOCK:
        session = UPLOAD_SESSIONS.pop(upload_id, None)

    if not session:
        return jsonify({"error": "Upload session not found."}), 404

    target_record = session.get("target")
    if not target_record:
        return jsonify({"error": "Please upload a main image."}), 400

    pieces_by_index: dict[int, dict] = session.get("pieces", {})
    if len(pieces_by_index) < 4:
        return jsonify({"error": "Upload at least 4 piece images."}), 400

    try:
        target_filename = target_record["filename"]
        target_bytes = _assemble_chunked_file(target_record)
        pieces_bytes = [
            (record["filename"], _assemble_chunked_file(record))
            for _, record in sorted(pieces_by_index.items(), key=lambda item: item[0])
        ]
    except ValueError as err:
        return jsonify({"error": str(err)}), 400

    config = create_mosaic_config(
        block_size_px=block_size_px,
        block_match_res=block_match_res,
        enlargement=enlargement,
        overlay_alpha=overlay_alpha,
    )

    job_id = _create_job()

    def run_job() -> None:
        _set_job(job_id, state="running", stage="Starting", progress=1)

        def on_progress(stage: str, progress: int) -> None:
            _set_job(job_id, state="running", stage=stage, progress=max(0, min(100, int(progress))))

        try:
            with TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                source_path = temp_path / "source" / target_filename
                pieces_path = temp_path / "pieces"
                output_path = temp_path / f"mosaic-{uuid4().hex}.jpeg"
                source_path.parent.mkdir(parents=True, exist_ok=True)
                pieces_path.mkdir(parents=True, exist_ok=True)

                source_path.write_bytes(target_bytes)

                try:
                    with Image.open(source_path):
                        pass
                except Exception:
                    _set_job(job_id, state="error", stage="Validation failed", error="The main image appears to be invalid.")
                    return

                for index, (piece_filename, piece_data) in enumerate(pieces_bytes, start=1):
                    piece_name = f"piece-{index}{Path(piece_filename).suffix.lower()}"
                    (pieces_path / piece_name).write_bytes(piece_data)

                create_mosaic_from_paths(
                    str(source_path),
                    str(pieces_path),
                    str(output_path),
                    config,
                    progress_callback=on_progress,
                )

                result_bytes = output_path.read_bytes()

            _set_job(job_id, state="done", stage="Done", progress=100, result=result_bytes)
        except Exception as err:
            _set_job(job_id, state="error", stage="Failed", error=f"Generation failed: {err}")

    Thread(target=run_job, daemon=True).start()
    return jsonify({"jobId": job_id})


@app.get("/api/generate/status/<job_id>")
def generate_status(job_id: str):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404

    return jsonify(_job_payload(job))


@sock.route("/api/generate/ws/<job_id>")
def generate_progress_ws(ws, job_id: str):
    try:
        subscriber_queue = _register_job_subscriber(job_id)
    except KeyError:
        ws.send(
            json.dumps(
                {
                    "state": "error",
                    "stage": "Not found",
                    "progress": 0,
                    "error": "Job not found.",
                    "ready": False,
                }
            )
        )
        return

    try:
        while True:
            try:
                payload = subscriber_queue.get(timeout=25)
            except Empty:
                continue

            ws.send(json.dumps(payload))
            if payload["state"] in {"done", "error"}:
                return
    finally:
        _unregister_job_subscriber(job_id, subscriber_queue)


@app.get("/api/generate/download/<job_id>")
def generate_download(job_id: str):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    if job["state"] != "done" or not job["result"]:
        return jsonify({"error": "Result is not ready yet."}), 409

    return send_file(
        BytesIO(job["result"]),
        mimetype="image/jpeg",
        as_attachment=True,
        download_name="mosaic-output.jpeg",
    )


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
