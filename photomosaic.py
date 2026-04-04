import argparse
from os import walk, path
from sys import argv, maxsize, stdout
from multiprocessing import Process, Queue, cpu_count
from time import perf_counter
from typing import Callable, Iterable, List, Sequence, Tuple, TypedDict
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

register_heif_opener()


RGBImage = Image.Image
PiecePair = Tuple[RGBImage | None, RGBImage | None]
BlockBounds = Tuple[int, int, int, int]
ProgressCallback = Callable[[str, int], None]
EOQ_VALUE = None


class MosaicConfig(TypedDict):
    block_size_px: int
    block_match_res: int
    enlargement: int
    overlay_alpha: float
    worker_count: int
    block_sample_ratio: float
    match_block_size_px: int


def create_mosaic_config(
    block_size_px: int = 50,
    block_match_res: int = 20,
    enlargement: int = 4,
    overlay_alpha: float = 0.5,
    worker_count: int | None = None,
) -> MosaicConfig:
    """Create validated mosaic configuration dictionary."""
    block_size_px = max(1, int(block_size_px))
    block_match_res = max(1, int(block_match_res))
    enlargement = max(1, int(enlargement))
    overlay_alpha = min(1.0, max(0.0, float(overlay_alpha)))
    if worker_count is None:
        worker_count = max(cpu_count() - 1, 1)
    
    block_sample_ratio = block_size_px / max(min(block_match_res, block_size_px), 1)
    match_block_size_px = max(1, int(block_size_px / block_sample_ratio))
    
    return {
        "block_size_px": block_size_px,
        "block_match_res": block_match_res,
        "enlargement": enlargement,
        "overlay_alpha": overlay_alpha,
        "worker_count": worker_count,
        "block_sample_ratio": block_sample_ratio,
        "match_block_size_px": match_block_size_px,
    }

def crop_center_square(image: RGBImage) -> RGBImage:
    """Return a centered square crop of the input image."""
    width, height = image.size
    min_side = min(width, height)
    x_margin = (width - min_side) / 2
    y_margin = (height - min_side) / 2
    return image.crop((x_margin, y_margin, width - x_margin, height - y_margin))


def resize_to_rgb(image: RGBImage, width: int, height: int) -> RGBImage:
    """Resize image with Lanczos filtering and convert to RGB."""
    return image.resize((width, height), Image.Resampling.LANCZOS).convert("RGB")


def prepare_piece_images(piece_path: str, config: MosaicConfig) -> PiecePair:
    """Load a piece image and return large/small RGB variants for matching and output."""
    try:
        image = ImageOps.exif_transpose(Image.open(piece_path))
        image = crop_center_square(image)
        return (
            resize_to_rgb(image, config["block_size_px"], config["block_size_px"]),
            resize_to_rgb(image, config["match_block_size_px"], config["match_block_size_px"]),
        )
    except Exception:
        return (None, None)


def iterate_piece_paths(pieces_directory: str) -> Iterable[str]:
    """Yield all candidate piece file paths under a directory tree."""
    for root, _, files in walk(pieces_directory):
        for piece_name in files:
            yield path.join(root, piece_name)


def load_piece_sets(
    pieces_directory: str,
    config: MosaicConfig,
    progress_callback: ProgressCallback | None = None,
) -> Tuple[List[RGBImage], List[RGBImage]]:
    """Load all valid pieces and split them into large and small prepared variants."""
    print(f"Reading pieces from {pieces_directory}...")
    piece_paths = list(iterate_piece_paths(pieces_directory))
    total_piece_count = len(piece_paths)
    pieces: list[PiecePair] = []
    for idx, piece_path in enumerate(piece_paths, start=1):
        pieces.append(prepare_piece_images(piece_path, config))
        if progress_callback:
            loading_progress = 10 + int((idx / max(total_piece_count, 1)) * 5)
            progress_callback(f"Reading pieces ({idx}/{total_piece_count})", loading_progress)
    large_pieces = [t[0] for t in pieces if t[0]]
    small_pieces = [t[1] for t in pieces if t[1]]
    print(f"{len(large_pieces)} valid pieces found and processed.")
    return (large_pieces, small_pieces)


def prepare_target_images(image_path: str, config: MosaicConfig) -> Tuple[RGBImage, RGBImage]:
    """Load target image, enlarge/crop to block grid, and return large/small RGB versions."""
    print("Processing main image...")
    image = Image.open(image_path)
    width = image.size[0] * config["enlargement"]
    height = image.size[1] * config["enlargement"]
    large_img = image.resize((width, height), Image.Resampling.LANCZOS)
    width_trim = (width % config["block_size_px"]) / 2
    height_trim = (height % config["block_size_px"]) / 2
    if width_trim or height_trim:
        large_img = large_img.crop((width_trim, height_trim, width - width_trim, height - height_trim))
    large_w, large_h = large_img.size
    small_img = large_img.resize(
        (
            int(large_w / config["block_size_px"]) * config["match_block_size_px"],
            int(large_h / config["block_size_px"]) * config["match_block_size_px"],
        ),
        Image.Resampling.LANCZOS,
    )
    print("Main image prepared for mosaic creation.")
    return (large_img.convert("RGB"), small_img.convert("RGB"))


def sum_pixel_distance_bytes(left: bytes, right: bytes, early_exit_threshold: int) -> int:
    """Return squared RGB distance sum for RGB byte blocks, with early bailout."""
    diff = 0
    for i in range(0, len(left), 3):
        dr = left[i] - right[i]
        dg = left[i + 1] - right[i + 1]
        db = left[i + 2] - right[i + 2]
        diff += dr * dr + dg * dg + db * db
        if diff > early_exit_threshold:
            return diff
    return diff


def pick_best_piece_index_bytes(target_block: bytes, piece_blocks: Sequence[bytes]) -> int | None:
    """Return the index of the piece byte block that best matches the target block."""
    min_diff = maxsize
    best_index = None
    for idx, piece_data in enumerate(piece_blocks):
        diff = sum_pixel_distance_bytes(target_block, piece_data, min_diff)
        if diff < min_diff:
            min_diff = diff
            best_index = idx
    return best_index


def run_piece_match_worker(work_queue: Queue, result_queue: Queue, piece_blocks_small: Sequence[bytes]):
    """Consume work items and emit best piece indices to the result queue."""
    match_cache: dict[bytes, int | None] = {}
    work_item = work_queue.get(True)
    while work_item[0] != EOQ_VALUE:
        try:
            block_pixels, block_bounds = work_item
            piece_index = match_cache.get(block_pixels)
            if piece_index is None and block_pixels not in match_cache:
                piece_index = pick_best_piece_index_bytes(block_pixels, piece_blocks_small)
                match_cache[block_pixels] = piece_index
            result_queue.put((block_bounds, piece_index))
            work_item = work_queue.get(True)
        except KeyboardInterrupt:
            pass

    result_queue.put((EOQ_VALUE, EOQ_VALUE))


def build_canvas_and_grid(image_size: Tuple[int, int], image_mode: str, config: MosaicConfig) -> Tuple[RGBImage, int, int]:
    """Create empty output canvas and return block-grid dimensions."""
    width, height = image_size
    x_block_count = int(width / config["block_size_px"])
    y_block_count = int(height / config["block_size_px"])
    return Image.new(image_mode, (width, height)), x_block_count, y_block_count


def paste_piece_into_canvas(image: RGBImage, piece_image: RGBImage, coords: BlockBounds):
    """Paste one prepared piece image into the output image."""
    image.paste(piece_image, coords)


def enqueue_piece_match_jobs(
    target_img_small: RGBImage,
    work_queue: Queue,
    x_block_count: int,
    y_block_count: int,
    config: MosaicConfig,
    progress_callback: ProgressCallback | None = None,
) -> int:
    """Slice target image into matching blocks and enqueue worker jobs."""
    total = x_block_count * y_block_count
    progress_step = max(total // 120, 1)
    for idx, (x, y) in enumerate(((x, y) for x in range(x_block_count) for y in range(y_block_count)), start=1):
        output_bounds = (
            x * config["block_size_px"],
            y * config["block_size_px"],
            (x + 1) * config["block_size_px"],
            (y + 1) * config["block_size_px"],
        )
        sample_bounds = (
            x * config["match_block_size_px"],
            y * config["match_block_size_px"],
            (x + 1) * config["match_block_size_px"],
            (y + 1) * config["match_block_size_px"],
        )
        work_queue.put((target_img_small.crop(sample_bounds).tobytes(), output_bounds))
        if progress_callback and (idx % progress_step == 0 or idx == total):
            queue_progress = 20 + int((idx / max(total, 1)) * 5)
            progress_callback(f"Queueing tile jobs ({idx}/{total})", queue_progress)
    return total


def run_mosaic_builder(
    result_queue: Queue,
    piece_images_large: List[RGBImage],
    target_img_large: RGBImage,
    out_file: str,
    config: MosaicConfig,
    total_blocks: int = 0,
    progress_callback: ProgressCallback | None = None,
):
    """Build and save output image from worker results."""
    image, _, _ = build_canvas_and_grid(target_img_large.size, target_img_large.mode, config)
    # Console progress is only useful for direct CLI runs.
    show_console_progress = stdout.isatty() and progress_callback is None
    active_workers = config["worker_count"]
    completed_blocks = 0
    while active_workers > 0:
        try:
            block_bounds, best_fit_piece_index = result_queue.get()
            if block_bounds == EOQ_VALUE:
                active_workers -= 1
            else:
                piece_image = piece_images_large[best_fit_piece_index]
                paste_piece_into_canvas(image, piece_image, block_bounds)
                completed_blocks += 1
                if total_blocks > 0:
                    # Reserve early and late percentages for non-matching stages.
                    percentage = 25 + int((completed_blocks / total_blocks) * 70)
                    if show_console_progress:
                        print(f"Progress: {completed_blocks}/{total_blocks} ({percentage}%)", end="\r", flush=True)
                    if progress_callback:
                        progress_callback("Matching tiles", min(95, percentage))
        except KeyboardInterrupt:
            pass
    if total_blocks > 0:
        if show_console_progress:
            print(f"Progress: {total_blocks}/{total_blocks} (95%)")
    if progress_callback:
        progress_callback("Blending final image", 97)
    image = Image.blend(image, target_img_large, config["overlay_alpha"])
    if progress_callback:
        progress_callback("Saving output", 99)
    image.save(out_file)
    if progress_callback:
        progress_callback("Done", 100)
    print(f"Output is in {out_file}")


def compose_mosaic_image(
    target_images: Tuple[RGBImage, RGBImage],
    piece_images: Tuple[List[RGBImage], List[RGBImage]],
    out_file: str,
    config: MosaicConfig,
    progress_callback: ProgressCallback | None = None,
):
    """Orchestrate worker processes that match pieces and build mosaic output."""
    print("Building the mosaic...")
    target_img_large, target_img_small = target_images
    pieces_large, pieces_small = piece_images
    _, x_block_count, y_block_count = build_canvas_and_grid(target_img_large.size, target_img_large.mode, config)
    piece_blocks_small = [piece.tobytes() for piece in pieces_small]
    work_queue = Queue(config["worker_count"])
    result_queue = Queue()
    worker_processes: List[Process] = []
    try:
        if progress_callback:
            progress_callback("Starting workers", 16)
        total_blocks = x_block_count * y_block_count
        worker_count = config["worker_count"]
        for worker_index in range(1, worker_count + 1):
            worker = Process(
                target=run_piece_match_worker,
                args=(work_queue, result_queue, piece_blocks_small),
            )
            worker.start()
            worker_processes.append(worker)
            if progress_callback:
                startup_progress = 16 + int((worker_index / max(worker_count, 1)) * 4)
                progress_callback(f"Starting workers ({worker_index}/{worker_count})", startup_progress)

        if progress_callback:
            progress_callback(f"Queueing tile jobs (0/{total_blocks})", 20)

        enqueue_piece_match_jobs(
            target_img_small,
            work_queue,
            x_block_count,
            y_block_count,
            config,
            progress_callback,
        )

        for _ in range(config["worker_count"]):
            work_queue.put((EOQ_VALUE, EOQ_VALUE))

        run_mosaic_builder(
            result_queue,
            pieces_large,
            target_img_large,
            out_file,
            config,
            total_blocks,
            progress_callback,
        )
    except KeyboardInterrupt:
        pass
    finally:
        for worker in worker_processes:
            worker.join()


def abort_with_error(msg: str):
    """Print an error and terminate the script with exit code 1."""
    print(f"ERROR: {msg}")
    exit(1)


def create_mosaic_from_paths(
    img_path: str,
    pieces_path: str,
    out_file: str,
    config: MosaicConfig,
    progress_callback: ProgressCallback | None = None,
):
    """Build a photomosaic from an input image and directory of piece images."""
    total_start = perf_counter()
    stage_start = perf_counter()
    if progress_callback:
        progress_callback("Preparing main image", 3)
    image_data = prepare_target_images(img_path, config)
    if progress_callback:
        progress_callback("Main image ready", 8)
    print(f"Target image prepared in {perf_counter() - stage_start:.2f}s")
    stage_start = perf_counter()
    if progress_callback:
        progress_callback("Loading piece images", 10)
    pieces_data = load_piece_sets(pieces_path, config, progress_callback)
    if progress_callback:
        progress_callback("Piece images ready", 15)
    print(f"Piece set prepared in {perf_counter() - stage_start:.2f}s")
    if pieces_data[0]:
        stage_start = perf_counter()
        compose_mosaic_image(image_data, pieces_data, out_file, config, progress_callback)
        print(f"Mosaic jobs queued in {perf_counter() - stage_start:.2f}s")
        print(f"Total pipeline setup took {perf_counter() - total_start:.2f}s")
    else:
        abort_with_error(f"No images found in pieces directory '{pieces_path}'")


def parse_cli_args(args: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments for CLI."""
    parser = argparse.ArgumentParser(description="Generate a photo mosaic from local images.")
    parser.add_argument("image", nargs="?", help="Path to source image file")
    parser.add_argument("pieces_directory", nargs="?", help="Path to directory of tile images")
    parser.add_argument("output_name", nargs="?", help="Output name without extension")
    
    parser.add_argument(
        "--block-size", type=int, default=50,
        help="Tile pixel size in the final mosaic (default: 50)"
    )
    parser.add_argument(
        "--match-res", type=int, default=20,
        help="Downscaled size used to compare each tile (default: 20)"
    )
    parser.add_argument(
        "--enlargement", type=int, default=4,
        help="Upscales the source image before tiling (default: 4)"
    )
    parser.add_argument(
        "--overlay-alpha", type=float, default=0.5,
        help="Mix level of original image over mosaic, 0.0-1.0 (default: 0.5)"
    )
    
    return parser.parse_args(args)


def run_cli() -> None:
    """CLI entrypoint for direct script invocation."""
    args = parse_cli_args(argv[1:])
    if not args.image or not args.pieces_directory:
        abort_with_error(
            f"Usage: {argv[0]} <image> <pieces directory> [out name]\n\tExample: {argv[0]} myphoto.jpg ./mypieces mosaic_output"
        )
    source_image = args.image
    pieces_dir = args.pieces_directory
    if not path.isfile(source_image):
        abort_with_error(f"Unable to find image file '{source_image}'")
    if not path.isdir(pieces_dir):
        abort_with_error(f"Unable to find piece directory '{pieces_dir}'")
    out_file = args.output_name + ".jpeg" if args.output_name else "output.jpeg"
    
    config = create_mosaic_config(
        block_size_px=args.block_size,
        block_match_res=args.match_res,
        enlargement=args.enlargement,
        overlay_alpha=args.overlay_alpha,
    )
    
    create_mosaic_from_paths(source_image, pieces_dir, out_file, config)


def main() -> None:
    run_cli()


if __name__ == "__main__":
    main()
