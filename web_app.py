from __future__ import annotations

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from flask import Flask, jsonify, render_template, request, send_file
from PIL import Image
from werkzeug.datastructures import FileStorage

from photomosaic import create_mosaic_from_paths, set_runtime_config


app = Flask(__name__, template_folder="templates", static_folder="static")

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


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

        set_runtime_config(
            block_size_px=block_size_px,
            block_match_res=block_match_res,
            enlargement=enlargement,
            overlay_alpha=overlay_alpha,
        )

        try:
            create_mosaic_from_paths(str(source_path), str(pieces_path), str(output_path))
        except Exception as err:
            return jsonify({"error": f"Generation failed: {err}"}), 500

        output_bytes = output_path.read_bytes()

        return send_file(
            BytesIO(output_bytes),
            mimetype="image/jpeg",
            as_attachment=True,
            download_name="mosaic-output.jpeg",
        )


def main() -> None:
    app.run(debug=True)


if __name__ == "__main__":
    main()
