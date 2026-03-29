# Mosaic

Generate a photo mosaic from an input image and a directory of piece images.

## Setup (uv)


```bash
python -m uv sync
```

## Configure (.env)

Copy [.env.example](.env.example) to `.env` and adjust values:

```bash
copy .env.example .env
```

Supported variables:

- `MOSAIC_BLOCK_SIZE_PX`
- `MOSAIC_BLOCK_MATCH_RES`
- `MOSAIC_ENLARGEMENT`
- `MOSAIC_OVERLAY_ALPHA`
- `MOSAIC_WORKER_COUNT`
- `MOSAIC_DEFAULT_OUT_FILE`

## Run

### CLI

```bash
python -m uv run mosaic <image> <pieces_directory> [output_name_without_extension]
```

Example:

```bash
python -m uv run mosaic ./myphoto.jpg ./pieces mosaic_output
```

This writes `mosaic_output.jpeg` (or `output.jpeg` when output name is omitted).

