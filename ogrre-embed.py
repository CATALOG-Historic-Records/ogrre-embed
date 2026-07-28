import json
from pathlib import Path
import fitz  # PyMuPDF
import time

def get_embedded_image_dimensions(doc, page_num):
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

def extract_page_items_with_canvas_size(page_data, doc_text):
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


def make_pdf_searchable(
    input_pdf_path, input_json_path, output_pdf_path
):
    """Overlays invisible text on a scanned PDF using dynamic DocAI canvas scaling."""
    with open(input_json_path, "r", encoding="utf-8") as f:
        doc_json = json.load(f)

    doc_text = doc_json.get("text", "")
    json_pages = doc_json.get("pages", [])

    pdf_doc = fitz.open(input_pdf_path)

    for page_num, page_data in enumerate(json_pages):
        if page_num >= len(pdf_doc):
            break

        page = pdf_doc[page_num]
        image_w, image_h = get_embedded_image_dimensions(pdf_doc, page_num)
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

        page_items = extract_page_items_with_canvas_size(
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

            page.insert_text(
                baseline_point,
                text,
                fontsize=font_size,
                fontname="helv",
                render_mode=3,
                overlay=True,
            )

    pdf_doc.save(output_pdf_path)
    pdf_doc.close()
    print(f"✅ Created searchable PDF: {output_pdf_path}")

def process_output_directory(base_output_dir):
    base_path = Path(base_output_dir)
    start_time = time.time()
    for doc_folder in base_path.iterdir():
        if doc_folder.is_dir():
            stem = doc_folder.name
            pdf_file = doc_folder / f"{stem}.pdf"
            json_file = doc_folder / f"{stem}.json"
            searchable_pdf = doc_folder / f"{stem}_searchable.pdf"

            if pdf_file.exists() and json_file.exists():
                make_pdf_searchable(pdf_file, json_file, searchable_pdf)

    end_time = time.time()
    print(f"PDF embedding completed in {end_time - start_time:.2f} seconds.")

if __name__ == "__main__":
    process_output_directory("output")