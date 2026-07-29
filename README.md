# OGRRE-Embed

`ogrre-embed` is a Python utility for aligning Google Document AI OCR outputs with scanned PDF pages and embedding precise, invisible searchable text layers directly into the PDF.

It handles scaling mismatches between raw pixel resolutions, normalized Document AI bounding boxes, and PDF page coordinates to ensure 1:1 searchable text alignment. It natively supports both local file processing and Google Cloud Storage (GCS) buckets, including automatic bucket creation, horizontal text scaling, and automated dataset pairing.

---

## Key Features

- **Exact Bounding Box & Horizontal Scaling:** Maps Document AI `normalizedVertices` against PDF geometry and applies horizontal matrix transformations (`morph`) so character selection matches underlying scanned text exactly.
- **Dual Local & GCS Pipelines:** Reads and writes seamless text overlays locally or directly to/from GCS buckets in memory without temporary disk overhead.
- **Automated Bucket Management:** Automatically detects missing destination GCS buckets and creates them on demand.
- **Dataset Downloader & Indexer:** Includes a fast, multi-threaded downloader (`fileset-download.py`) to index PDFs across GCP project buckets and pair them with Document AI JSON output outputs.
- **Flexible File Inputs:** Accepts combined input folders, split PDF/JSON directories, or `gs://` bucket URIs.

---

## Requirements

- **Python:** 3.8+
- **Key Dependencies:**
  - `PyMuPDF` (`fitz`)
  - `google-cloud-storage`
  - `tqdm`

Install dependencies via `pip`:
```bash
pip install PyMuPDF google-cloud-storage tqdm
```
---

## Project Structure
```markdown
ogrre-embed/
├── ogrre-embed.py          # Main CLI and embedding pipeline engine
├── gcs_storage_utils.py    # GCS helper utilities (listing, downloads, bucket creation)
├── fileset-download.py     # Parallel indexer & downloader for paired GCS datasets
└── README.md
```
---

## Google Cloud Authentication

When using GCS URIs (`gs://`), make sure you are authenticated with Google Cloud:

# Option A: Authenticate via Google Cloud CLI
```bash
gcloud auth application-default login
```

# Option B: Set Service Account Credentials
```bash
set GOOGLE_APPLICATION_CREDENTIALS="C:\path\to\your\key.json"
````
---

## Usage

### 1. Embedding Invisible Searchable Text (`ogrre-embed.py`)

`ogrre-embed.py` processes pairs of PDF and JSON files, overlays the searchable text layer, and outputs the result locally or to a GCS bucket.

#### Option A: Local Directory (Combined Input)

```bash
python ogrre-embed.py -i ./input -o ./output
```

#### Option B: Local Directory (Separate PDF and JSON Folders)

```bash
python ogrre-embed.py -p ./data/pdfs -j ./data/jsons -o ./output
```

#### Option C: Google Cloud Storage Buckets
When using GCS URIs, pass your GCP Project ID via `--project`:

```bash
python ogrre-embed.py -i gs://my-input-bucket/input -o gs://my-output-bucket/output --project my-gcp-project-id
```

#### CLI Arguments for `ogrre-embed.py`:
| Argument | Short | Description |
| :--- | :--- | :--- |
| `--input` | `-i` | Combined directory or GCS URI containing both PDF and JSON files. |
| `--pdf` | `-p` | Directory or GCS URI containing input PDF files. |
| `--json` | `-j` | Directory or GCS URI containing input JSON files. |
| `--output` | `-o` | **(Required)** Target directory or GCS URI for searchable PDFs. |
| `--project` | `-f` | GCP Project ID (required when using GCS URIs). |

---

### 2. Dataset Downloader & Pair Indexer (`fileset-download.py`)

`fileset-download.py` scans your GCP project buckets in parallel, indexes all PDF source files, pairs them with Document AI JSON outputs from a target output bucket, and downloads structured local pairs into `./output/<doc_stem>/`.

#### Download paired JSON/PDF files across your GCP project
```bash
python fileset-download.py --project my-gcp-project-id --json-bucket test-padep26r-output -w 12
```

#### CLI Arguments for `fileset-download.py`:
| Argument | Short | Description | Default |
| :--- | :--- | :--- | :--- |
| `--project` | `-p` | Google Cloud Project ID. | Auto-detected |
| `--json-bucket` | `-j` | Bucket containing Document AI JSON outputs. | `test-padep26r-output` |
| `--output-dir` | `-o` | Local target directory for structured pairs. | `./output` |
| `--workers` | `-w` | Parallel worker threads for bucket indexing. | `12` |
| `--limit` | `-l` | Maximum number of document pairs to process. | `None` (All) |

---

## Python Module Usage

You can also import and use `OGRREEmbed` directly inside custom Python scripts:

```python
from ogrre_embed import OGRREEmbed

# Initialize for GCS processing
embedder = OGRREEmbed(
    output_dir="gs://my-output-bucket/searchable",
    input_dir="gs://my-input-bucket/raw",
    project_id="my-gcp-project-id"
)

# Run embedding pipeline
embedder.embed_pdfs()
```
---

## How Alignment Works

1. **Geometry Mapping:** Normalized bounding box coordinates (`0.0` to `1.0`) are multiplied against page background image dimensions (`image_w`, `image_h`) and scaled to PyMuPDF point coordinates.
2. **Horizontal Glyph Fitting:** Unscaled text length in standard Helvetica is compared against the target bounding box width. A transformation matrix (`fitz.Matrix(horizontal_scale, 1)`) is applied via `morph` to stretch or compress glyph spacing to span the exact bounding box width.
3. **Invisible Text Layer:** Words are placed using `render_mode=3` (invisible text) with `set_simple=True` to guarantee PDF searchability and text selection in PDF readers.

---

## License

MIT License. See `LICENSE` for details.