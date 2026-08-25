import json
from pathlib import Path
import fitz  # PyMuPDF
import time
import argparse
import os
import sys
from google.cloud import storage
from tqdm import tqdm


def get_embedded_image_dimensions(doc, page_num):
    """
    Extracts the pixel dimensions (width, height) of the primary
    embedded background image on the specified PDF page.
    """
    page = doc[page_num]
    image_list = page.get_images(full=True)

    if image_list:
        xref = image_list[0][0]
        base_image = doc.extract_image(xref)
        return float(base_image["width"]), float(base_image["height"])

    return page.rect.width, page.rect.height


def extract_page_items_with_canvas_size(page_data, doc_text=""):
    """Extracts tokens or attributes and layout coordinates for a page."""
    page_items = []

    # Handle Document AI JSON page structure (tokens)
    tokens = page_data.get("tokens", [])
    if tokens:
        for token in tokens:
            layout = token.get("layout", {})
            bounding_poly = layout.get("boundingPoly", {})

            raw_verts = bounding_poly.get("vertices", [])
            norm_verts = bounding_poly.get("normalizedVertices", [])

            text_anchors = layout.get("textAnchor", {}).get("textSegments", [])
            token_text = "".join(
                [
                    doc_text[int(s.get("startIndex", 0)) : int(s.get("endIndex", 0))]
                    for s in text_anchors
                ]
            ).strip()

            if not token_text:
                continue

            if norm_verts and len(norm_verts) >= 2:
                xs_norm = [v.get("x", 0) for v in norm_verts]
                ys_norm = [v.get("y", 0) for v in norm_verts]
            else:
                continue

            page_items.append(
                {
                    "text": token_text,
                    "x_min_norm": min(xs_norm),
                    "x_max_norm": max(xs_norm),
                    "y_min_norm": min(ys_norm),
                    "y_max_norm": max(ys_norm),
                }
            )
        return page_items

    # Handle custom items or attributesList if provided in page_data
    items = page_data.get("items") or page_data.get("attributesList") or []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = item.get("text") or item.get("raw_text") or item.get("value")
        if not text or not isinstance(text, str):
            continue

        norm_verts = item.get("normalized_vertices") or item.get("normalizedVertices")
        if not norm_verts:
            continue

        xs_norm = []
        ys_norm = []
        if isinstance(norm_verts, list):
            for v in norm_verts:
                if isinstance(v, (list, tuple)) and len(v) >= 2:
                    xs_norm.append(float(v[0]))
                    ys_norm.append(float(v[1]))
                elif isinstance(v, dict):
                    xs_norm.append(float(v.get("x", 0)))
                    ys_norm.append(float(v.get("y", 0)))

        if len(xs_norm) >= 2 and len(ys_norm) >= 2:
            page_items.append({
                "text": text.strip(),
                "x_min_norm": min(xs_norm),
                "x_max_norm": max(xs_norm),
                "y_min_norm": min(ys_norm),
                "y_max_norm": max(ys_norm),
            })

    return page_items


def make_pdf_searchable(input_pdf, input_json, output_pdf=None, gcs_utils=None):
    """Overlays invisible text on a scanned PDF using dynamic DocAI canvas scaling.

    Args:
        input_pdf: File path (str), bytes, or fitz.Document instance.
        input_json: File path (str), bytes, or Python dict (DocAI JSON or record attributes).
        output_pdf: File path (str) to save to, or None to return PDF bytes.
        gcs_utils: GCSStorageUtils instance if loading GCS URIs.

    Returns:
        bytes if output_pdf is None, or saved file path if output_pdf is provided.
    """
    # 1. Load PDF Document
    pdf_doc = None
    if isinstance(input_pdf, fitz.Document):
        pdf_doc = input_pdf
    elif isinstance(input_pdf, bytes):
        pdf_doc = fitz.open(stream=input_pdf, filetype="pdf")
    elif isinstance(input_pdf, (str, Path)):
        input_pdf_str = str(input_pdf)
        if input_pdf_str.startswith("gs://"):
            if not gcs_utils:
                from gcs_storage_utils import GCSStorageUtils
                gcs_utils = GCSStorageUtils()
            pdf_bytes = gcs_utils.download_gcs_to_memory(input_pdf_str)
            pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        else:
            pdf_doc = fitz.open(input_pdf_str)
    else:
        raise ValueError("Invalid input_pdf format. Must be file path, bytes, or fitz.Document.")

    # 2. Load JSON Data
    doc_json = None
    if isinstance(input_json, dict):
        doc_json = input_json
    elif isinstance(input_json, bytes):
        doc_json = json.loads(input_json.decode("utf-8"))
    elif isinstance(input_json, (str, Path)):
        input_json_str = str(input_json)
        if input_json_str.startswith("gs://"):
            if not gcs_utils:
                from gcs_storage_utils import GCSStorageUtils
                gcs_utils = GCSStorageUtils()
            json_bytes = gcs_utils.download_gcs_to_memory(input_json_str)
            doc_json = json.loads(json_bytes.decode("utf-8"))
        else:
            with open(input_json_str, "r", encoding="utf-8") as f:
                doc_json = json.load(f)
    else:
        raise ValueError("Invalid input_json format. Must be file path, bytes, or dict.")

    doc_text = doc_json.get("text", "")
    json_pages = doc_json.get("pages", [])

    # If json_pages is not present, check if doc_json has single attributesList/items
    if not json_pages and ("attributesList" in doc_json or "items" in doc_json):
        json_pages = [doc_json]

    for page_num in range(len(pdf_doc)):
        page = pdf_doc[page_num]
        image_w, image_h = get_embedded_image_dimensions(pdf_doc, page_num)
        pdf_w = page.rect.width
        pdf_h = page.rect.height

        page_data = json_pages[page_num] if page_num < len(json_pages) else (json_pages[0] if json_pages else {})
        page_items = extract_page_items_with_canvas_size(page_data, doc_text)

        for item in page_items:
            text = item["text"]

            # Map bounding box coordinates to PDF page coordinates
            x0 = item["x_min_norm"] * image_w
            x1 = item["x_max_norm"] * image_w
            y0 = (item["y_min_norm"] * image_h) + (pdf_h - image_h)
            y1 = (item["y_max_norm"] * image_h) + (pdf_h - image_h)

            rect = fitz.Rect(x0, y0, x1, y1)

            if rect.width <= 0 or rect.height <= 0:
                continue

            # --- INVISIBLE SEARCHABLE TEXT ---
            font_size = max(rect.height * 0.85, 3)
            baseline_point = fitz.Point(rect.x0, rect.y1 - (rect.height * 0.15))

            font = fitz.Font("helv")
            unscaled_text_width = font.text_length(text, fontsize=font_size)

            if unscaled_text_width > 0:
                horizontal_scale = rect.width / unscaled_text_width
            else:
                horizontal_scale = 1.0

            matrix = fitz.Matrix(horizontal_scale, 1)

            page.insert_text(
                baseline_point,
                text,
                fontsize=font_size,
                fontname="helv",
                morph=(baseline_point, matrix),
                set_simple=True,
                render_mode=3,
                overlay=True,
            )

    if output_pdf:
        output_pdf_str = str(output_pdf)
        if output_pdf_str.startswith("gs://"):
            if not gcs_utils:
                from gcs_storage_utils import GCSStorageUtils
                gcs_utils = GCSStorageUtils()
            gcs_utils.upload_file_to_gcs(output_pdf_str, pdf_doc=pdf_doc)
        else:
            pdf_doc.save(output_pdf_str)
        pdf_doc.close()
        return output_pdf_str
    else:
        result_bytes = pdf_doc.tobytes()
        pdf_doc.close()
        return result_bytes


class OGRREEmbed:
    def __init__(self, output_dir, input_dir=None, pdf_dir=None, json_dir=None, project_id=None):
        self.input_dir = input_dir
        self.pdf_dir = pdf_dir
        self.json_dir = json_dir
        self.output_dir = output_dir

        self.is_input_gcs = (
            (self.input_dir and self.input_dir.startswith("gs://") or
             self.pdf_dir and self.pdf_dir.startswith("gs://") or
             self.json_dir and self.json_dir.startswith("gs://"))
        )
        self.is_output_gcs = self.output_dir.startswith("gs://")
        self.project_id = project_id

        if not self.is_output_gcs:
            os.makedirs(self.output_dir, exist_ok=True)

        if self.is_input_gcs or self.is_output_gcs:
            from gcs_storage_utils import GCSStorageUtils
            self.gcs_utils = GCSStorageUtils(self.project_id)
        else:
            self.gcs_utils = None

    def gather_files(self):
        pdf_files = []
        json_files = []

        if self.is_input_gcs:
            if self.input_dir:
                pdf_files_temp = self.gcs_utils.list_gcs_files(self.input_dir, '.pdf')
                json_files_temp = self.gcs_utils.list_gcs_files(self.input_dir, '.json')
            elif self.pdf_dir and self.json_dir:
                pdf_files_temp = self.gcs_utils.list_gcs_files(self.pdf_dir, '.pdf')
                json_files_temp = self.gcs_utils.list_gcs_files(self.json_dir, '.json')
            else:
                print("Error: Either --input or both --pdf and --json must be provided.")
                sys.exit(1)

            json_lookup = {Path(json_file).stem: json_file for json_file in json_files_temp}
            for pdf_file in pdf_files_temp:
                pdf_name = Path(pdf_file).stem
                json_file = json_lookup.get(pdf_name)
                if json_file:
                    pdf_files.append(pdf_file)
                    json_files.append(json_file)
                else:
                    print(f"Matching JSON file missing. PDF file skipped.")

        else:
            if self.input_dir:
                pdf_path = Path(os.path.abspath(self.input_dir))
                json_path = Path(os.path.abspath(self.input_dir))
            elif self.pdf_dir and self.json_dir:
                pdf_path = Path(os.path.abspath(self.pdf_dir))
                json_path = Path(os.path.abspath(self.json_dir))
            else:
                print("Error: Either --input or both --pdf and --json must be provided.")
                sys.exit(1)

            for pdf_file in pdf_path.rglob("*.pdf"):
                relative_path = pdf_file.relative_to(pdf_path)
                expected_json = (json_path / relative_path).with_suffix(".json")
                if expected_json.exists():
                    pdf_files.append(str(pdf_file))
                    json_files.append(str(expected_json))
                else:
                    print(f"Matching JSON file missing. PDF file skipped.")

        if len(pdf_files) != len(json_files):
            print("Error: The number of PDF and JSON files must match.")
            sys.exit(1)
        elif not pdf_files:
            print("Error: No PDF files found in the specified input directory.")
            sys.exit(1)
        elif not json_files:
            print("Error: No JSON files found in the specified input directory.")
            sys.exit(1)

        if not self.is_output_gcs:
            output_dir = os.path.abspath(self.output_dir)
            os.makedirs(output_dir, exist_ok=True)
        else:
            output_dir = self.output_dir

        return pdf_files, json_files, output_dir

    def make_pdf_searchable(self, input_pdf_path, input_json_path, output_pdf_path):
        return make_pdf_searchable(input_pdf_path, input_json_path, output_pdf_path, gcs_utils=self.gcs_utils)

    def embed_pdfs(self):
        start_time = time.time()

        pdf_files, json_files, output_dir = self.gather_files()
        with tqdm(total=len(pdf_files), desc="Embedding PDFs") as pbar:
            for pdf_file, json_file in zip(pdf_files, json_files):
                pdf_name = Path(pdf_file).stem
                json_name = Path(json_file).stem
                output_path = output_dir + "/" + f"{pdf_name}_searchable.pdf"
                if pdf_name == json_name:
                    self.make_pdf_searchable(pdf_file, json_file, output_path)
                else:
                    print(f"Skipping {pdf_file} because it does not match the JSON file name.")
                pbar.update(1)

        end_time = time.time()
        print(f"PDF embedding completed in {end_time - start_time:.2f} seconds.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Embed PDFs with invisible searchable text.")
    parser.add_argument("-i", "--input", default=None, help="Input GCS or local directory if containing both PDF and JSON files.")
    parser.add_argument("-p", "--pdf", default=None, help="Input GCS or local PDF file directory.")
    parser.add_argument("-j", "--json", default=None, help="Input GCS or local JSON file directory.")
    parser.add_argument("-o", "--output", required=True, help="Output GCS or local directory.")
    parser.add_argument("-f", "--project", default=None, help="GCP Project ID.")

    args = parser.parse_args()

    if not args.input and not (args.pdf and args.json):
        print("Error: Either --input or both --pdf and --json must be provided.")
        sys.exit(1)
    elif args.input and (args.pdf or args.json):
        print("Warning: Both --input and --pdf/--json were provided. Only --input will be used.")
        args.pdf = None
        args.json = None
    elif args.input and not (args.pdf and args.json):
        args.pdf = None
        args.json = None
    elif not args.input and (args.pdf and args.json):
        args.input = None

    if not args.output:
        print("Error: An output directory, either a Google Storage bucket or local directory, is required.")
        sys.exit(1)

    if not args.project:
        if (
           (args.input and args.input.startswith("gs://")) or
           (args.output and args.output.startswith("gs://")) or
           (args.pdf and args.pdf.startswith("gs://")) or
           (args.json and args.json.startswith("gs://"))
        ):
            print("Error: A Google Cloud Project ID is required when using GCS buckets or output directories.")

    embed = OGRREEmbed(args.output, args.input, args.pdf, args.json, args.project)
    embed.embed_pdfs()
