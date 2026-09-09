# OGRRE-Embed

`ogrre-embed` is a Python utility for aligning OCR/schema outputs with scanned PDF pages, embedding precise invisible searchable text layers, and injecting interactive PDF bookmark outline trees directly into documents.

It handles scaling mismatches between raw pixel resolutions, normalized bounding box spaces, and PDF page coordinates to ensure 1:1 searchable text alignment. It natively supports both Google Document AI schema structures (`entities`) and MongoDB/legacy schema formats (`attributesList`), as well as local file processing and Google Cloud Storage (GCS) buckets without temporary disk overhead.

---

## Key Features

- **Exact Bounding Box & Horizontal Scaling:** Maps normalized bounding vertices against PDF geometry and applies horizontal matrix transformations (`morph`) so invisible searchable text matches underlying scanned characters precisely.
- **Dual Schema Support:** Automatically handles both Document AI (`entities` with recursive properties) and MongoDB/legacy (`attributesList` with recursive subattributes) schema structures.
- **Interactive Bookmark Outlines:** Recursively constructs hierarchical navigation trees (bookmarks/outlines) using `pypdf`, embedding jump targets aligned to top-left entity coordinates with vertical padding buffers.
- **Dual Local & GCS Pipelines:** Reads and writes seamless text overlays and outline layers locally or directly to/from GCS buckets in memory via byte streams (`io.BytesIO`).
- **Automated Bucket Management:** Automatically detects missing destination GCS buckets and creates them on demand.
- **Dataset Downloader & Indexer:** Includes a fast, multi-threaded downloader (`fileset-download.py`) to index PDFs across GCP project buckets and pair them with Document AI JSON outputs.

---

## Requirements

- **Python:** 3.8+
- **Dependencies:** Install requirements via `requirements.txt`:
```bash
pip install -r requirements.txt
```

### `requirements.txt`
```text
# PDF Processing & Rendering
pymupdf>=1.23.0
pypdf>=4.0.0

# Google Cloud Storage & Authentication
google-cloud-storage>=2.10.0
google-auth>=2.20.0

# Terminal UI & Progress Monitoring
tqdm>=4.65.0
```

---

## Project Structure
```markdown
ogrre-embed/
├── ogrre_embed.py          # Main CLI, text overlay, and outline tree generation engine
├── gcs_storage_utils.py    # GCS helper utilities (listing, downloads, byte/file uploads)
├── fileset-download.py     # Parallel indexer & downloader for paired GCS datasets
├── requirements.txt        # Package dependencies
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
```

---

## Usage

### 1. Embedding Searchable Text & Outline Trees (`ogrre_embed.py`)

`ogrre_embed.py` processes pairs of PDF and JSON files, overlays the invisible text layer, builds the PDF bookmark tree, and outputs the result locally or to a GCS bucket.

#### Option A: Local Directory (Combined Input)

```bash
python ogrre_embed.py -i ./input -o ./output
```

#### Option B: Local Directory (Separate PDF and JSON Folders)

```bash
python ogrre_embed.py -p ./data/pdfs -j ./data/jsons -o ./output
```

#### Option C: Google Cloud Storage Buckets
When using GCS URIs, pass your GCP Project ID via `--project`:

```bash
python ogrre_embed.py -i gs://my-input-bucket/input -o gs://my-output-bucket/output --project my-gcp-project-id
```

#### CLI Arguments for `ogrre_embed.py`:
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

You can import and use `OGRREEmbed` or `make_pdf_searchable` directly inside custom Python pipelines:

```python
from ogrre_embed import OGRREEmbed, make_pdf_searchable

# 1. High-level helper function returning PDF bytes or output file
pdf_bytes = make_pdf_searchable(
    input_pdf="path/to/document.pdf",
    input_json="path/to/extraction.json"
)

# 2. Direct OGRREEmbed batch processing
embedder = OGRREEmbed(
    output_dir="gs://my-output-bucket/searchable",
    input_dir="gs://my-input-bucket/raw",
    project_id="my-gcp-project-id"
)
embedder.embed_pdfs()
```

---

## How Processing Works

1. **Geometry Mapping:** Normalized bounding box coordinates (`0.0` to `1.0`) are mapped against page background image dimensions (`image_w`, `image_h`) and user scale units to convert accurately into PDF point space.
2. **Invisible Text Injection:** PyMuPDF (`fitz`) places text strings (`key: value`) using `render_mode=3` (invisible text) with horizontal matrix transformation (`morph`) so character boundaries line up with physical document graphics.
3. **Outline Tree Assembly:** PyMuPDF exports the rendered canvas as an in-memory byte stream to `pypdf`. `pypdf` reads the stream, constructs the hierarchical outline/bookmark tree based on schema nestings, applies jump targets with Y-offset padding, and writes out the final PDF.

---

## License

MIT License. See `LICENSE` for details.