import json
from pathlib import Path
import fitz  # PyMuPDF
import time
import argparse
import os
import sys
from google.cloud import storage
from gcs_storage_utils import GCSStorageUtils
from tqdm import tqdm

class OGRREEmbed:
    def __init__(self, output_dir, input_dir=None, pdf_dir=None, json_dir=None, project_id=None):
        self.input_dir = input_dir
        self.pdf_dir = pdf_dir
        self.json_dir = json_dir
        self.output_dir = output_dir

        # Check if Google Cloud Storage is required for this run
        self.is_input_gcs = \
        (
            (self.input_dir and self.input_dir.startswith("gs://") or
             self.pdf_dir and self.pdf_dir.startswith("gs://") or
             self.json_dir and self.json_dir.startswith("gs://"))
        )
        self.is_output_gcs = self.output_dir.startswith("gs://")
        self.project_id = project_id

        # Set up output directories if not using GCS
        if not self.is_output_gcs:
            os.makedirs(self.output_dir, exist_ok=True)
            
        # Authenticate Google Cloud access
        if self.is_input_gcs or self.is_output_gcs:
            print(f"Initializing GCS client (Target Project ID: {self.project_id or 'gcloud Default'})...")
            try:
                # Attempt to instantiate storage client and make a fast, basic API metadata call
                if self.project_id:
                    self.storage_client = storage.Client(project=self.project_id)
                else:
                    self.storage_client = storage.Client()
                # Attempt to access service metadata to confirm valid active tokens
                self.storage_client.list_buckets(max_results=1)
                # create GCSStorageUtils instance to handle GCS operations
                self.gcs_utils = GCSStorageUtils(self.project_id)
                print(" -> Google Cloud Authentication verified successfully.")
            except Exception as e:
                print("\n" + "!"*70)
                print("         GOOGLE CLOUD AUTHENTICATION FAILED")
                print("!"*70)
                print("The script could not connect to Google Cloud Storage.")
                print("To resolve this issue, please follow one of these steps:")
                print("\n[OPTION A] Authenticate your Windows terminal using the Google Cloud CLI:")
                print("   1. Open Command Prompt/PowerShell and run:")
                print("      gcloud auth application-default login")
                print("   2. Complete the login flow in your web browser.")
                print("\n[OPTION B] Use a Google Service Account Private Key:")
                print("   1. Obtain your service account JSON credentials key file.")
                print("   2. Set the Windows environment variable to point to it:")
                print("      set GOOGLE_APPLICATION_CREDENTIALS=C:\\path\\to\\key.json")
                print("!"*70 + "\n")

                sys.exit(1)

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


    def get_embedded_image_dimensions(self, doc, page_num):
        """
        Extracts the pixel dimensions (width, height) of the primary
        embedded background image on the specified PDF page.
        """
        page = doc[page_num]
        image_list = page.get_images(full=True)

        if image_list:
            # Inspect the first primary embedded image on the page
            xref = image_list[0][0]
            base_image = doc.extract_image(xref)
            return float(base_image["width"]), float(base_image["height"])

        return None, None

    def extract_page_items_with_canvas_size(self, page_data, doc_text):
        """Extracts tokens and dynamically calculates the DocAI canvas dimensions for the page."""
        tokens = page_data.get("tokens", [])
        page_items = []

        for token in tokens:
            layout = token.get("layout", {})
            bounding_poly = layout.get("boundingPoly", {})

            raw_verts = bounding_poly.get("vertices", [])
            norm_verts = bounding_poly.get("normalizedVertices", [])

            # Parse text from text anchors
            text_anchors = layout.get("textAnchor", {}).get("textSegments", [])
            token_text = "".join(
                [
                    doc_text[int(s.get("startIndex", 0)) : int(s.get("endIndex", 0))]
                    for s in text_anchors
                ]
            ).strip()

            if not token_text or len(raw_verts) < 2 or len(norm_verts) < 2:
                continue

            xs_raw = [v.get("x", 0) for v in raw_verts]
            ys_raw = [v.get("y", 0) for v in raw_verts]
            xs_norm = [v.get("x", 0) for v in norm_verts]
            ys_norm = [v.get("y", 0) for v in norm_verts]

            page_items.append(
                {
                    "text": token_text,
                    "x_min_raw": min(xs_raw),
                    "x_max_raw": max(xs_raw),
                    "y_min_raw": min(ys_raw),
                    "y_max_raw": max(ys_raw),
                    "x_min_norm": min(xs_norm),
                    "x_max_norm": max(xs_norm),
                    "y_min_norm": min(ys_norm),
                    "y_max_norm": max(ys_norm),
                }
            )

        return page_items


    def make_pdf_searchable(self, input_pdf_path, input_json_path, output_pdf_path):
        """Overlays invisible text on a scanned PDF using dynamic DocAI canvas scaling."""
        if self.is_input_gcs:
            pdf_bytes = self.gcs_utils.download_gcs_to_memory(input_pdf_path)
            json_bytes = self.gcs_utils.download_gcs_to_memory(input_json_path)
            doc_json = json.loads(json_bytes.decode("utf-8"))
            pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        else:
            with open(input_json_path, "r", encoding="utf-8") as f:
                doc_json = json.load(f)

            pdf_doc = fitz.open(input_pdf_path)

        doc_text = doc_json.get("text", "")
        json_pages = doc_json.get("pages", [])

        for page_num, page_data in enumerate(json_pages):
            if page_num >= len(pdf_doc):
                break

            page = pdf_doc[page_num]
            image_w, image_h = self.get_embedded_image_dimensions(pdf_doc, page_num)
            pdf_w = page.rect.width
            pdf_h = page.rect.height

            # Draw calibration rectangles
            # calibration_coords = [
            #     [0,0,10,10],
            #     [image_w-10,0,image_w,10],
            #     [image_w-10,image_h-10,image_w,image_h],
            #     [0,image_h-10,10,image_h],
            # ]
            # for coords in calibration_coords:
            #     page.draw_rect(fitz.Rect(*coords), color=(0, 0, 1), width=0.5)

            page_items = self.extract_page_items_with_canvas_size(
                page_data, doc_text
            )

            for item in page_items:
                text = item["text"]

                # Map bounding box coordinates to PDF page coordinates
                x0 = item["x_min_norm"] * image_w
                x1 = item["x_max_norm"] * image_w
                y0 = (item["y_min_norm"] * image_h)+(pdf_h-image_h)
                y1 = (item["y_max_norm"] * image_h)+(pdf_h-image_h)

                rect = fitz.Rect(x0, y0, x1, y1)

                if rect.width <= 0 or rect.height <= 0:
                    continue

                # --- INVISIBLE SEARCHABLE TEXT ---
                font_size = max(rect.height * 0.85, 3)
                baseline_point = fitz.Point(rect.x0, rect.y1 - (rect.height * 0.15))

                # Calculate default unscaled text width in Helvetica
                font = fitz.Font("helv")
                unscaled_text_width = font.text_length(text, fontsize=font_size)

                # Calculate the horizontal scale factor to fit the text within the rectangle
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

        if self.is_output_gcs:
            self.gcs_utils.upload_file_to_gcs(output_pdf_path, pdf_doc=pdf_doc)
        else:
            pdf_doc.save(output_pdf_path)
        pdf_doc.close()

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
            print("Error: A Google Cloud Project ID is required when using GCS "
                  "buckets or output directories.")

    embed = OGRREEmbed(args.output, args.input, args.pdf, args.json, args.project)
    embed.embed_pdfs()