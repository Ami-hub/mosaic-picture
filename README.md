# Create Photo Mosaic

Generate a photo mosaic from an input image and a directory of piece images.

## 📚 Prerequisites

- [Python 3.10+](https://www.python.org/downloads/)
- [uv package manager](https://docs.astral.sh/uv/getting-started/installation/)

## 🔥 Let's get started!

### ⬇️ Clone the repo from github

```
git clone https://github.com/Ami-hub/mosaic
cd mosaic
```

### 📦 Install dependencies

```
uv sync
```

### 👟 Run the app

```
uv run photomosaic.py <image_path> <pieces_directory> [output_name]
```

**Example:**

````
uv run photomosaic.py sample.png smallPhotos
```

### 🎉 Done!

The mosaic will be generated and saved to the specified output file.

## Exmple output

<p align="center">
  <img src="https://i.ibb.co/TB2xrvNZ/outputasd.jpg" alt="sample-api-logo" width="50%">
</p>


<p align="center">
  <img src="https://i.ibb.co/Jwd4fqH9/yes.jpg" alt="sample-api-logo" width="50%">
</p>
