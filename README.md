# Create Photo Mosaic

Generate a photo mosaic from an input image and a directory of piece images.

## 📚 Prerequisites

- [Docker Desktop](https://docs.docker.com/get-started/get-docker/) 

or

- [Python 3.10+](https://www.python.org/downloads/)
- [uv package manager](https://docs.astral.sh/uv/getting-started/installation/)


## 🔥 Let's get started!

### ⬇️ Clone the repo

```
git clone https://github.com/Ami-hub/mosaic
cd mosaic
```

### 🐳 Quick start with docker

```bash
docker compose up
```

Open http://127.0.0.1:8000

Thats it!😍

### Alternatively:
### 📦 Install dependencies

```
uv sync
```

### 🖥️ Run the local web UI

```
uv run flask --app web_app run
```

Then open http://127.0.0.1:5000 and use the upload form to generate and download your mosaic.

### ⌨️ Use CLI

```
uv run photomosaic.py <image_path> <pieces_directory>
```

run `uv run photomosaic.py -h` for more information


**Example:**

```
uv run photomosaic.py sample.png smallPhotos
```

### 🎉 Done!

The mosaic will be generated and saved to the specified output file.

## Example output

<p align="center">
  <img src="https://i.ibb.co/Jwd4fqH9/yes.jpg" alt="sample-api-logo" width="50%">
</p>
