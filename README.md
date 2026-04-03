# Create Photo Mosaic

Generate an amazing photo mosaic from your images!

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

Then open http://127.0.0.1:8000

As simple as that!😍

### Alternatively:
### 📦 Install dependencies

```
uv sync
```

### 🖥️ Run the local web UI

```
uv run flask run
```

Then open http://127.0.0.1:5000 

### ⌨️ Or use CLI mode

```
uv run photomosaic.py <image_path> <pieces_directory>
```

run `uv run photomosaic.py -h` for more information!


**Example:**

```
uv run photomosaic.py sample.png smallPhotos
```

The mosaic will be generated and saved to the specified output file 🎉

## Example output

<p align="center">
  <img src="https://i.ibb.co/Jwd4fqH9/yes.jpg" alt="sample-api-logo" width="50%">
</p>
