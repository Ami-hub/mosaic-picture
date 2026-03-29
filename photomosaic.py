import argparse
from os import getenv, walk, path
from sys import argv, maxsize
from multiprocessing import Process, Queue, cpu_count
from time import perf_counter
from typing import Iterable, List, Sequence, Tuple
from dotenv import load_dotenv
from PIL import Image, ImageOps

load_dotenv()


def read_env_int(name: str, default: int, min_value: int = 1) -> int:
    """Read an integer from environment with fallback and minimum bound."""
    raw = getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"WARNING: Invalid integer for {name}: '{raw}'. Using {default}.")
        return default
    if value < min_value:
        print(f"WARNING: {name} must be >= {min_value}. Using {default}.")
        return default
    return value


def read_env_float(name: str, default: float, min_value: float = 0.0, max_value: float = 1.0) -> float:
    """Read a float from environment with fallback and inclusive bounds."""
    raw = getenv(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        print(f"WARNING: Invalid float for {name}: '{raw}'. Using {default}.")
        return default
    if value < min_value or value > max_value:
        print(f"WARNING: {name} must be between {min_value} and {max_value}. Using {default}.")
        return default
    return value


BLOCK_SIZE_PX = read_env_int("MOSAIC_BLOCK_SIZE_PX", 50)
BLOCK_MATCH_RES = read_env_int("MOSAIC_BLOCK_MATCH_RES", 20)
ENLARGEMENT = read_env_int("MOSAIC_ENLARGEMENT", 4)
OVERLAY_ALPHA = read_env_float("MOSAIC_OVERLAY_ALPHA", 0.5, 0.0, 1.0)

DEFAULT_OUT_FILE = getenv("MOSAIC_DEFAULT_OUT_FILE", "output.jpeg")

BLOCK_SAMPLE_RATIO = BLOCK_SIZE_PX / max(min(BLOCK_MATCH_RES, BLOCK_SIZE_PX), 1)
MATCH_BLOCK_SIZE_PX = int(BLOCK_SIZE_PX / BLOCK_SAMPLE_RATIO)
WORKER_COUNT = read_env_int("MOSAIC_WORKER_COUNT", max(cpu_count() - 1, 1))
EOQ_VALUE = None

RGBImage = Image.Image
PiecePair = Tuple[RGBImage | None, RGBImage | None]
BlockBounds = Tuple[int, int, int, int]

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


def prepare_piece_images(piece_path: str) -> PiecePair:
    """Load a piece image and return large/small RGB variants for matching and output."""
    try:
        image = ImageOps.exif_transpose(Image.open(piece_path))
        image = crop_center_square(image)
        return (
            resize_to_rgb(image, BLOCK_SIZE_PX, BLOCK_SIZE_PX),
            resize_to_rgb(image, MATCH_BLOCK_SIZE_PX, MATCH_BLOCK_SIZE_PX),
        )
    except Exception:
        return (None, None)


def iterate_piece_paths(pieces_directory: str) -> Iterable[str]:
    """Yield all candidate piece file paths under a directory tree."""
    for root, _, files in walk(pieces_directory):
        for piece_name in files:
            yield path.join(root, piece_name)


def load_piece_sets(pieces_directory: str) -> Tuple[List[RGBImage], List[RGBImage]]:
    """Load all valid pieces and split them into large and small prepared variants."""
    print(f"Reading pieces from {pieces_directory}...")
    pieces = list(map(prepare_piece_images, iterate_piece_paths(pieces_directory)))
    large_pieces = [t[0] for t in pieces if t[0]]
    small_pieces = [t[1] for t in pieces if t[1]]
    print(f"{len(large_pieces)} valid pieces found and processed.")
    return (large_pieces, small_pieces)


def prepare_target_images(image_path: str) -> Tuple[RGBImage, RGBImage]:
    """Load target image, enlarge/crop to block grid, and return large/small RGB versions."""
    print("Processing main image...")
    image = Image.open(image_path)
    width = image.size[0] * ENLARGEMENT
    height = image.size[1] * ENLARGEMENT
    large_img = image.resize((width, height), Image.Resampling.LANCZOS)
    width_trim = (width % BLOCK_SIZE_PX) / 2
    height_trim = (height % BLOCK_SIZE_PX) / 2
    if width_trim or height_trim:
        large_img = large_img.crop((width_trim, height_trim, width - width_trim, height - height_trim))
    large_w, large_h = large_img.size
    small_img = large_img.resize(
        (
            int(large_w / BLOCK_SIZE_PX) * MATCH_BLOCK_SIZE_PX,
            int(large_h / BLOCK_SIZE_PX) * MATCH_BLOCK_SIZE_PX,
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


def build_canvas_and_grid(image_size: Tuple[int, int], image_mode: str) -> Tuple[RGBImage, int, int]:
    """Create empty output canvas and return block-grid dimensions."""
    width, height = image_size
    x_block_count = int(width / BLOCK_SIZE_PX)
    y_block_count = int(height / BLOCK_SIZE_PX)
    return Image.new(image_mode, (width, height)), x_block_count, y_block_count


def paste_piece_into_canvas(image: RGBImage, piece_image: RGBImage, coords: BlockBounds):
    """Paste one prepared piece image into the output image."""
    image.paste(piece_image, coords)


def enqueue_piece_match_jobs(
    target_img_small: RGBImage,
    work_queue: Queue,
    x_block_count: int,
    y_block_count: int,
) -> int:
    """Slice target image into matching blocks and enqueue worker jobs."""
    total = x_block_count * y_block_count
    for idx, (x, y) in enumerate(((x, y) for x in range(x_block_count) for y in range(y_block_count))):
        output_bounds = (
            x * BLOCK_SIZE_PX,
            y * BLOCK_SIZE_PX,
            (x + 1) * BLOCK_SIZE_PX,
            (y + 1) * BLOCK_SIZE_PX,
        )
        sample_bounds = (
            x * MATCH_BLOCK_SIZE_PX,
            y * MATCH_BLOCK_SIZE_PX,
            (x + 1) * MATCH_BLOCK_SIZE_PX,
            (y + 1) * MATCH_BLOCK_SIZE_PX,
        )
        work_queue.put((target_img_small.crop(sample_bounds).tobytes(), output_bounds))
    return total


def run_mosaic_builder(
    result_queue: Queue,
    piece_images_large: List[RGBImage],
    target_img_large: RGBImage,
    out_file: str,
    overlay_alpha: float,
    total_blocks: int = 0,
):
    """Build and save output image from worker results."""
    image, _, _ = build_canvas_and_grid(target_img_large.size, target_img_large.mode)
    active_workers = WORKER_COUNT
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
                    percentage = int((completed_blocks / total_blocks) * 100)
                    print(f"Progress: {completed_blocks}/{total_blocks} ({percentage}%)", end="\r", flush=True)
        except KeyboardInterrupt:
            pass
    if total_blocks > 0:
        print(f"Progress: {total_blocks}/{total_blocks} (100%)")
    image = Image.blend(image, target_img_large, overlay_alpha)
    image.save(out_file)
    print(f"Output is in {out_file}")


def compose_mosaic_image(
    target_images: Tuple[RGBImage, RGBImage],
    piece_images: Tuple[List[RGBImage], List[RGBImage]],
    out_file: str = DEFAULT_OUT_FILE,
):
    """Orchestrate worker processes that match pieces and build mosaic output."""
    print("Building the mosaic...")
    target_img_large, target_img_small = target_images
    pieces_large, pieces_small = piece_images
    _, x_block_count, y_block_count = build_canvas_and_grid(target_img_large.size, target_img_large.mode)
    piece_blocks_small = [piece.tobytes() for piece in pieces_small]
    work_queue = Queue(WORKER_COUNT)
    result_queue = Queue()
    worker_processes: List[Process] = []
    builder_process: Process | None = None
    try:
        total_blocks = x_block_count * y_block_count
        builder_process = Process(
            target=run_mosaic_builder,
            args=(result_queue, pieces_large, target_img_large, out_file, OVERLAY_ALPHA, total_blocks),
        )
        builder_process.start()
        for _ in range(WORKER_COUNT):
            worker = Process(
                target=run_piece_match_worker,
                args=(work_queue, result_queue, piece_blocks_small),
            )
            worker.start()
            worker_processes.append(worker)

        enqueue_piece_match_jobs(
            target_img_small,
            work_queue,
            x_block_count,
            y_block_count,
        )
    except KeyboardInterrupt:
        pass
    finally:
        for _ in range(WORKER_COUNT):
            work_queue.put((EOQ_VALUE, EOQ_VALUE))
        for worker in worker_processes:
            worker.join()
        if builder_process:
            builder_process.join()


def abort_with_error(msg: str):
    """Print an error and terminate the script with exit code 1."""
    print(f"ERROR: {msg}")
    exit(1)


def create_mosaic_from_paths(
    img_path: str,
    pieces_path: str,
    out_file: str = DEFAULT_OUT_FILE,
):
    """Build a photomosaic from an input image and directory of piece images."""
    total_start = perf_counter()
    stage_start = perf_counter()
    image_data = prepare_target_images(img_path)
    print(f"Target image prepared in {perf_counter() - stage_start:.2f}s")
    stage_start = perf_counter()
    pieces_data = load_piece_sets(pieces_path)
    print(f"Piece set prepared in {perf_counter() - stage_start:.2f}s")
    if pieces_data[0]:
        stage_start = perf_counter()
        compose_mosaic_image(image_data, pieces_data, out_file)
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
    out_file = args.output_name + ".jpeg" if args.output_name else DEFAULT_OUT_FILE
    create_mosaic_from_paths(source_image, pieces_dir, out_file)


def main() -> None:
    run_cli()


if __name__ == "__main__":
    main()
