import json
from pathlib import Path
import fitz  # PyMuPDF
import time
import argparse
import os
import sys
import io
from gcs_storage_utils import GCSStorageUtils
from tqdm import tqdm
from pypdf import PdfReader, PdfWriter
from pypdf.generic import Fit


class OGRREEmbed:
    def __init__(self, output_dir, input_dir=None, pdf_dir=None, json_dir=None, project_id=None):
        self.input_dir = input_dir
        self.pdf_dir = pdf_dir
        self.json_dir = json_dir
        self.output_dir = output_dir
        self.searchable_layers = []
        self.json_type = "MongoDB"

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
            self.gcs_utils = GCSStorageUtils(self.project_id)
        else:
            self.gcs_utils = None

    def gather_files(self):
        pdf_files = []
        json_files = []

        if self.is_input_gcs:
            if self.input_dir and self.gcs_utils:
                pdf_files_temp = self.gcs_utils.list_gcs_files(self.input_dir, '.pdf')
                json_files_temp = self.gcs_utils.list_gcs_files(self.input_dir, '.json')
            elif self.pdf_dir and self.gcs_utils and self.json_dir:
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


    @staticmethod
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

    def extract_searchable_entities(self, entities_list, parent_key=None):
        """
        Recursively extracts leaf schema keys, values, and bounding boxes
        for the search overlay while bypassing parent container elements.
        """
        for entity in entities_list:
            schema_key = entity.get("type")
            value_text = entity.get("mentionText", "").strip()

            # Build namespace: 'parent_type.child_type' (optional, omit parent_key if unwanted)
            full_key = f"{parent_key}.{schema_key}" if parent_key else schema_key

            # 1. Early Return: If entity is a container, delegate to properties & skip parent
            sub_properties = entity.get("properties")
            if sub_properties:
                self.extract_searchable_entities(sub_properties, parent_key=full_key)
                continue

            # 2. Extract Geometry for Leaf Nodes
            page_anchor = entity.get("pageAnchor", {})
            page_refs = page_anchor.get("pageRefs", [])

            if page_refs:
                ref = page_refs[0]
                page_num = int(ref.get("page", 0))
                poly = ref.get("boundingPoly", {})
                vertices = poly.get("normalizedVertices", [])

                # Safely append only if we have non-empty text and valid vertices
                if value_text and vertices:
                    searchable_text = f"{full_key}: {value_text}"
                    self.searchable_layers.append({
                        "page": page_num,
                        "key": full_key,
                        "value": value_text,
                        "search_string": searchable_text,
                        "normalized_vertices": vertices
                    })

    def extract_searchable_attributes(self, attributeList):
        """
        Extracts schema keys, values, and bounding boxes for invisible text overlay.
        """
        for attribute in attributeList:
            # Extract page number and bounding box coordinates
            try:
                page_num = int(attribute.get("page", None))
            except:
                continue

            try:
                vertices = attribute.get("normalized_vertices", [])
            except:
                continue

            schema_key = attribute.get("key")
            value_text = attribute.get("value")

            # Combine key and value into a queryable string
            # Formats like "key: value" or "key=value" work exceptionally well for text search engines
            searchable_text = f"{schema_key}: {value_text}"

            if attribute.get("subattributes"):
                self.extract_searchable_attributes(attribute.get("subattributes"))

            self.searchable_layers.append({
                "page": page_num,
                "key": schema_key,
                "value": value_text,
                "search_string": searchable_text,
                "normalized_vertices": vertices
            })

    def normalize_bbox_to_pdf_space(
        self, bbox, pdf_rect, image_size, user_scale=1.0
    ):
        """Maps bounding boxes into target canvas space while accounting for

        UserUnit page scaling factors.
        """
        # Adjust base PDF dimensions by the UserUnit scale factor
        effective_pdf_width = pdf_rect.width * user_scale
        effective_pdf_height = pdf_rect.height * user_scale

        if self.json_type == "MongoDB":
            # MongoDB normalized coordinates map directly to effective PDF Point dimensions
            return {
                "x1": bbox["x1"] * effective_pdf_width,
                "y1": bbox["y1"] * effective_pdf_height,
                "x2": bbox["x2"] * effective_pdf_width,
                "y2": bbox["y2"] * effective_pdf_height,
            }

        elif self.json_type == "DocAI":
            # DocAI normalized coordinates are relative to the high-res image grid.
            # Compute canvas scaling ratio relative to effective PDF point space.
            scale_x = image_size[0] / effective_pdf_width
            scale_y = image_size[1] / effective_pdf_height

            y_offset = image_size[1] - effective_pdf_height

            return {
                "x1": (bbox["x1"] * effective_pdf_width) * scale_x,
                "y1": ((bbox["y1"] * effective_pdf_height) * scale_y) - y_offset,
                "x2": (bbox["x2"] * effective_pdf_width) * scale_x,
                "y2": ((bbox["y2"] * effective_pdf_height) * scale_y) - y_offset,
            }
        return {}

    @staticmethod
    def get_page_user_scale(doc, page):
        """Safely extracts the /UserUnit scale factor from a PDF page dictionary."""
        try:
            key_type, val_str = doc.xref_get_key(page.xref, "UserUnit")
            if key_type in ("int", "real"):
                return float(val_str)
        except Exception(BaseException):
            pass
        return 1.0

    def add_entity_outlines(self, writer, entities, layout_map=None, parent_outline=None):
        """
        Recursively builds PDF bookmarks (outlines) using entity types and positions.
        """
        for entity in entities:
            schema_key = entity.get("type", "Entity")
            value_text = entity.get("mentionText", "").strip()

            # Format bookmark label (e.g., "Facility Name: Carbon Limestone")
            if value_text:
                # Truncate long values for clean sidebar display
                display_value = (value_text[:30] + '...') if len(value_text) > 30 else value_text
                title = f"{schema_key}: {display_value}"
            else:
                title = schema_key

            # Resolve page and coordinates for jump target
            page_anchor = entity.get("pageAnchor", {})
            page_refs = page_anchor.get("pageRefs", [])

            current_outline = None
            if page_refs:
                page_num = int(page_refs[0].get("page", 0))

                # Ensure page index is valid in the writer
                if page_num < len(writer.pages):
                    page_ref = writer.pages[page_num]
                    page_h = float(page_ref.mediabox.height)

                    # Extract top-left Y coordinate to position viewer jump location
                    poly = page_refs[0].get("boundingPoly", {})
                    vertices = poly.get("normalizedVertices", [])

                    if vertices:
                        top_y = page_h - (vertices[0].get("y", 0.0) * page_h)
                        top_y = max(0, top_y + 20.0)
                    else:
                        top_y = page_h  # Default to top of page if no vertices

                    # Create outline item pointing to page and top offset
                    current_outline = writer.add_outline_item(
                        title=title,
                        page_number=page_num,
                        parent=parent_outline,
                        fit=Fit.fit_horizontally(top=top_y)
                    )

            # If entity has no location but has children, fallback parent outline container
            if not current_outline and parent_outline:
                current_outline = parent_outline

            # Recurse through child properties to build sub-level bookmarks
            sub_properties = entity.get("properties", [])
            if sub_properties:
                self.add_entity_outlines(
                    writer=writer,
                    entities=sub_properties,
                    layout_map=layout_map,
                    parent_outline=current_outline
                )

    def add_attribute_outlines(self, writer, attributes, parent_outline=None):
        """
        Recursively builds PDF bookmarks (outlines) for MongoDB / attributesList schemas.
        """
        for attr in attributes:
            schema_key = attr.get("key", "Attribute")
            value_text = str(attr.get("value", "")).strip()

            if value_text:
                display_value = (value_text[:30] + '...') if len(value_text) > 30 else value_text
                title = f"{schema_key}: {display_value}"
            else:
                title = schema_key

            current_outline = None
            try:
                page_num = int(attr.get("page", 0))
            except (ValueError, TypeError):
                page_num = 0

            if page_num < len(writer.pages):
                page_ref = writer.pages[page_num]
                page_h = float(page_ref.mediabox.height)

                # Extract Y-coordinate if available in normalized_vertices
                vertices = attr.get("normalized_vertices", [])
                if vertices:
                    # Check if vertices are dicts or lists
                    top_y_norm = vertices[0].get("y", 0.0) if isinstance(vertices[0], dict) else vertices[0][1]
                    top_y = page_h - (top_y_norm * page_h)
                    top_y = max(0, top_y + 20.0)
                else:
                    top_y = page_h

                current_outline = writer.add_outline_item(
                    title=title,
                    page_number=page_num,
                    parent=parent_outline,
                    fit=Fit.fit_horizontally(top=top_y)
                )

            if not current_outline and parent_outline:
                current_outline = parent_outline

            # Recurse into nested subattributes
            sub_attributes = attr.get("subattributes", [])
            if sub_attributes:
                self.add_attribute_outlines(
                    writer=writer,
                    attributes=sub_attributes,
                    parent_outline=current_outline
                )

    def make_pdf_searchable(self, input_pdf, input_json, output_pdf=None, gcs_utils=None):
        """Overlays invisible text on a scanned PDF using dynamic DocAI canvas scaling.

        Args:
            input_pdf: File path (str), bytes, or fitz.Document instance.
            input_json: File path (str), bytes, or Python dict (DocAI JSON or record attributes).
            output_pdf: File path (str) to save to, or None to return PDF bytes.
            gcs_utils: GCSStorageUtils instance if loading GCS URIs.

        Returns:
            bytes if output_pdf is None, or saved file path if output_pdf is provided.
        """
        # Load PDF Document
        pdf_doc = None
        if isinstance(input_pdf, fitz.Document):
            pdf_doc = input_pdf
        elif isinstance(input_pdf, bytes):
            pdf_doc = fitz.open(stream=input_pdf, filetype="pdf")
        elif isinstance(input_pdf, (str, Path)):
            input_pdf_str = str(input_pdf)
            if input_pdf_str.startswith("gs://") and self.gcs_utils:
                pdf_bytes = self.gcs_utils.download_gcs_to_memory(input_pdf_str)
                pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            else:
                pdf_doc = fitz.open(input_pdf_str)
        else:
            raise ValueError("Invalid input_pdf format. Must be file path, bytes, or fitz.Document.")

        if not pdf_doc.is_pdf:
            pdf_bytes = pdf_doc.convert_to_pdf()
            pdf_doc.close()
            pdf_doc = fitz.open("pdf", pdf_bytes)

        # Load JSON Data
        doc_json = None
        if isinstance(input_json, dict):
            doc_json = input_json
        elif isinstance(input_json, bytes):
            doc_json = json.loads(input_json.decode("utf-8"))
        elif isinstance(input_json, (str, Path)):
            input_json_str = str(input_json)
            if input_json_str.startswith("gs://") and self.gcs_utils:
                json_bytes = self.gcs_utils.download_gcs_to_memory(input_json_str)
                doc_json = json.loads(json_bytes.decode("utf-8"))
            else:
                with open(input_json_str, "r", encoding="utf-8") as f:
                    doc_json = json.load(f)
        else:
            raise ValueError("Invalid input_json format. Must be file path, bytes, or dict.")

        self.searchable_layers = []

        if doc_json.get("entities"):
            entities_list = doc_json.get("entities", [])
            attributes_list = []
            self.extract_searchable_entities(entities_list)
            self.json_type = "DocAI"

        elif doc_json.get("attributesList"):
            attributes_list = doc_json.get("attributesList", [])
            entities_list = []
            self.extract_searchable_attributes(attributes_list)

        else:
            print(f"No entities or attributesList found in JSON.")
            sys.exit()

        # Process each page using the collected entity/attribute layers
        for page_num in range(len(pdf_doc)):
            page = pdf_doc[page_num]
            page_rect = page.rect
            user_scale = self.get_page_user_scale(pdf_doc, page)
            image_w, image_h = self.get_embedded_image_dimensions(pdf_doc, page_num)

            # Filter layers belonging to the current page
            page_layers = [
                layer for layer in self.searchable_layers
                if layer.get("page") == page_num
            ]

            for item in page_layers:
                text = item.get("search_string", "")
                verts = item.get("normalized_vertices", [])

                if not text or len(verts) < 2:
                    continue

                xs, ys = [], []
                if isinstance(verts[0], dict):
                    xs = [v.get("x", 0) for v in verts]
                    ys = [v.get("y", 0) for v in verts]
                else:
                    xs = [v[0] for v in verts]
                    ys = [v[1] for v in verts]

                if not xs or not ys:
                    continue

                # Map normalized coordinates directly to PDF dimensions
                raw_bbox = {"x1": min(xs), "y1": min(ys), "x2": max(xs), "y2": max(ys)}
                scaled_bbox = self.normalize_bbox_to_pdf_space(
                    bbox=raw_bbox,
                    pdf_rect=page_rect,
                    image_size=(image_w, image_h),
                    user_scale=user_scale
                )

                rect = fitz.Rect(scaled_bbox["x1"], scaled_bbox["y1"], scaled_bbox["x2"], scaled_bbox["y2"])

                if rect.width <= 0 or rect.height <= 0:
                    continue

                # --- INVISIBLE SEARCHABLE TEXT ---
                font_size = max(rect.height * 0.85, 3)
                baseline_point = fitz.Point(rect.x0, rect.y1 - (rect.height * 0.15))

                font = fitz.Font("helv")
                unscaled_text_width = font.text_length(text, fontsize=font_size)

                horizontal_scale = (rect.width / unscaled_text_width) if unscaled_text_width > 0 else 1.0
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

        # Export PyMuPDF modified document to in-memory bytes buffer
        searchable_pdf_bytes = pdf_doc.tobytes()
        pdf_doc.close()

        # Attach bookmark outlines with pypdf
        reader = PdfReader(io.BytesIO(searchable_pdf_bytes))
        writer = PdfWriter()

        for page in reader.pages:
            writer.add_page(page)

        # Route outline construction based on JSON structure (DocAI vs MongoDB/attributesList)
        if self.json_type == "DocAI" and entities_list:
            self.add_entity_outlines(writer, doc_json.get("entities", []))
        elif self.json_type == "MongoDB" and attributes_list:
            self.add_attribute_outlines(writer, doc_json.get("attributesList", []))

        if output_pdf:
            output_pdf_str = str(output_pdf)
            if output_pdf_str.startswith("gs://"):
                if not gcs_utils:
                    gcs_utils = GCSStorageUtils(self.project_id)
                out_buffer = io.BytesIO()
                writer.write(out_buffer)
                out_buffer.seek(0)
                gcs_utils.upload_file_to_gcs(output_pdf_str, pdf_bytes=out_buffer.getvalue())
            else:
                with open(output_pdf_str, "wb") as f:
                    writer.write(f)
            return output_pdf_str
        else:
            out_buffer = io.BytesIO()
            writer.write(out_buffer)
            return out_buffer.getvalue()



    def embed_pdfs(self):
        start_time = time.time()

        pdf_files, json_files, output_dir = self.gather_files()
        with tqdm(total=len(pdf_files), desc="Embedding PDFs") as pbar:
            for pdf_file, json_file in zip(pdf_files, json_files):
                pdf_name = Path(pdf_file).stem
                json_name = Path(json_file).stem
                output_path = output_dir + "/" + f"{pdf_name}_searchable.pdf"
                if pdf_name == json_name:
                    self.make_pdf_searchable(pdf_file, json_file, output_path, gcs_utils=self.gcs_utils)
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
