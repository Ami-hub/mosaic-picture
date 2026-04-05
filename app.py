from __future__ import annotations

import json
import shutil
from io import BytesIO
from pathlib import Path
from queue import Empty, Queue
from tempfile import TemporaryDirectory, gettempdir
from threading import Lock, Thread
from uuid import uuid4

from flask import Flask, jsonify, render_template, request, send_file, stream_with_context
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
UPLOAD_SESSION_ROOT = Path(gettempdir()) / "mosaic_upload_sessions"
UPLOAD_SESSION_ROOT.mkdir(parents=True, exist_ok=True)

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
    (_upload_session_dir(upload_id)).mkdir(parents=True, exist_ok=True)
    return upload_id


def _upload_session_dir(upload_id: str) -> Path:
    return UPLOAD_SESSION_ROOT / upload_id


def _upload_file_dir(upload_id: str, file_role: str, file_index: int | None = None) -> Path:
    session_dir = _upload_session_dir(upload_id)
    if file_role == "target":
        return session_dir / "target"
    if file_index is None:
        raise ValueError("fileIndex is required for piece uploads.")
    return session_dir / "pieces" / str(file_index)


def _load_upload_file_record(file_dir: Path) -> dict:
    meta_path = file_dir / "meta.json"
    if not meta_path.exists():
        raise ValueError(f"File '{file_dir.name}' is incomplete.")
    with meta_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _assemble_chunked_file(file_dir: Path) -> tuple[str, bytes]:
    file_record = _load_upload_file_record(file_dir)
    total_chunks = file_record["total_chunks"]
    chunks_dir = file_dir / "chunks"
    chunk_paths = sorted(
        chunks_dir.glob("*.part"),
        key=lambda path: int(path.stem),
    )
    if len(chunk_paths) != total_chunks:
        raise ValueError(f"File '{file_record['filename']}' is incomplete.")
    return file_record["filename"], b"".join(path.read_bytes() for path in chunk_paths)


def _delete_upload_session(upload_id: str) -> None:
    shutil.rmtree(_upload_session_dir(upload_id), ignore_errors=True)


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

    file_index: int | None = None
    if file_role == "piece":
        try:
            file_index = int(raw_file_index)
        except ValueError:
            return jsonify({"error": "fileIndex is required for piece uploads."}), 400
        if file_index < 0:
            return jsonify({"error": "fileIndex must be non-negative."}), 400

    chunk_bytes = chunk_file.read()

    session_dir = _upload_session_dir(upload_id)
    if not session_dir.exists():
        return jsonify({"error": "Upload session not found."}), 404

    try:
        file_dir = _upload_file_dir(upload_id, file_role, file_index)
    except ValueError as err:
        return jsonify({"error": str(err)}), 400

    chunks_dir = file_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    meta_path = file_dir / "meta.json"

    if meta_path.exists():
        with meta_path.open("r", encoding="utf-8") as handle:
            file_record = json.load(handle)
    else:
        file_record = {
            "filename": file_name,
            "total_chunks": total_chunks,
        }

    if file_record["filename"] != file_name:
        return jsonify({"error": "fileName mismatch for this upload target."}), 400
    if file_record["total_chunks"] != total_chunks:
        return jsonify({"error": "totalChunks mismatch for this upload target."}), 400

    chunk_path = chunks_dir / f"{chunk_index:06d}.part"
    if not chunk_path.exists():
        chunk_path.write_bytes(chunk_bytes)

    received_chunks = len(list(chunks_dir.glob("*.part")))
    meta_path.write_text(json.dumps(file_record), encoding="utf-8")

    return jsonify({
        "receivedChunks": received_chunks,
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

    try:
        session_dir = _upload_session_dir(upload_id)
        if not session_dir.exists():
            return jsonify({"error": "Upload session not found."}), 404

        target_dir = session_dir / "target"
        if not target_dir.exists():
            return jsonify({"error": "Please upload a main image."}), 400
        target_filename, target_bytes = _assemble_chunked_file(target_dir)

        pieces_root = session_dir / "pieces"
        piece_dirs = [
            (int(piece_dir.name), piece_dir)
            for piece_dir in pieces_root.iterdir()
            if piece_dir.is_dir() and piece_dir.name.isdigit()
        ] if pieces_root.exists() else []
        if len(piece_dirs) < 4:
            return jsonify({"error": "Upload at least 4 piece images."}), 400

        pieces_bytes = [
            _assemble_chunked_file(piece_dir)
            for _, piece_dir in sorted(piece_dirs, key=lambda item: item[0])
        ]
    except ValueError as err:
        return jsonify({"error": str(err)}), 400
    finally:
        _delete_upload_session(upload_id)

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


@app.get("/api/generate/events/<job_id>")
def generate_progress_events(job_id: str):
    try:
        subscriber_queue = _register_job_subscriber(job_id)
    except KeyError:
        return jsonify({"error": "Job not found."}), 404

    def event_stream():
        try:
            while True:
                try:
                    payload = subscriber_queue.get(timeout=20)
                except Empty:
                    # Keep the stream alive through intermediaries.
                    yield ": keepalive\n\n"
                    continue

                yield f"data: {json.dumps(payload)}\n\n"
                if payload["state"] in {"done", "error"}:
                    return
        finally:
            _unregister_job_subscriber(job_id, subscriber_queue)

    return stream_with_context(event_stream()), {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }


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
