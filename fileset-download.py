import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import google.auth
from google.cloud import storage
from tqdm import tqdm

# --- CONFIGURATION DEFAULTS ---
JSON_BUCKET_NAME = "test-padep26r-output"
LOCAL_OUTPUT_DIR = Path("output")
EXCLUDE_BUCKET_KEYWORDS = ["-output"]
MAX_WORKERS = 12


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Download Document AI JSON outputs and pair them with source PDFs from GCS."
    )
    parser.add_argument(
        "-p",
        "--project",
        type=str,
        default=None,
        help="Google Cloud Project ID (e.g., --project tidy-outlet-412020).",
    )
    parser.add_argument(
        "-j",
        "--json-bucket",
        type=str,
        default=JSON_BUCKET_NAME,
        help=f"Bucket containing Document AI JSON outputs (default: {JSON_BUCKET_NAME}).",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=str,
        default=str(LOCAL_OUTPUT_DIR),
        help=f"Local target directory for downloads (default: {LOCAL_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=MAX_WORKERS,
        help=f"Number of parallel worker threads for indexing (default: {MAX_WORKERS}).",
    )
    parser.add_argument(
        "-l",
        "--limit",
        type=int,
        default=None,
        help="Maximum number of document pairs to download (default: None / all).",
    )
    return parser.parse_args()


def scan_single_bucket(bucket_name, project_id):
    """Worker function executed by threads to scan a single bucket."""
    client = storage.Client(project=project_id)
    bucket_index = {}

    try:
        blobs = client.list_blobs(bucket_name)
        for blob in blobs:
            if blob.name.endswith(".pdf"):
                filename = os.path.basename(blob.name)
                bucket_index[filename] = (bucket_name, blob.name)
    except Exception:
        pass

    return bucket_name, bucket_index


def build_fast_pdf_index(client, project_id, workers):
    """Scans all non-excluded buckets in parallel with a tqdm progress bar."""
    print("=" * 60)
    print("BUILDING FAST MULTI-THREADED PDF INDEX")
    print("=" * 60)
    start_time = time.perf_counter()

    all_buckets = list(client.list_buckets(project=project_id))
    target_buckets = [
        b.name
        for b in all_buckets
        if not any(kw in b.name for kw in EXCLUDE_BUCKET_KEYWORDS)
    ]

    pdf_index = {}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(scan_single_bucket, b_name, project_id): b_name
            for b_name in target_buckets
        }

        with tqdm(
            total=len(target_buckets),
            desc="Scanning Buckets",
            unit="bucket",
            dynamic_ncols=True,
        ) as pbar:
            for future in as_completed(futures):
                b_name, b_index = future.result()
                pdf_index.update(b_index)
                pbar.set_postfix_str(f"Last: {b_name} (+{len(b_index)} PDFs)")
                pbar.update(1)

    elapsed = time.perf_counter() - start_time
    print(f"\n✅ Indexed {len(pdf_index):,} PDFs across project in {elapsed:.2f} seconds.")
    print("=" * 60 + "\n")

    return pdf_index


def process_and_download():
    args = parse_arguments()
    project_id = args.project

    if not project_id:
        _, project_id = google.auth.default()

    if not project_id:
        print("❌ Error: No Project ID provided and could not detect a default gcloud project.")
        print("Please run with: python download_and_pair.py -p YOUR_PROJECT_ID")
        sys.exit(1)

    output_dir = Path(args.output_dir)
    client = storage.Client(project=project_id)

    # 1. Build parallel PDF index
    pdf_index = build_fast_pdf_index(client, project_id, args.workers)

    # 2. Prepare local output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # 3. Fetch JSON blobs
    print("STARTING FILE PAIRING & DOWNLOADS")
    print("=" * 60)

    try:
        json_bucket = client.bucket(args.json_bucket)
        json_blobs = [
            b for b in json_bucket.list_blobs()
            if b.name.endswith(".json") and "metadata" not in b.name and "manifest" not in b.name
        ]
    except Exception as e:
        print(f"❌ Error accessing JSON output bucket '{args.json_bucket}': {e}")
        return

    # Slice the blob list if a limit argument was passed
    if args.limit and args.limit > 0:
        json_blobs = json_blobs[:args.limit]
        print(f"ℹ️ Download limit applied: processing first {len(json_blobs)} document(s).\n")

    downloaded_count = 0
    missing_pdf_count = 0

    # Progress bar for processing and downloading JSON + PDF pairs
    with tqdm(
        total=len(json_blobs),
        desc="Processing Documents",
        unit="doc",
        dynamic_ncols=True,
    ) as pbar:
        for blob in json_blobs:
            json_path = Path(blob.name)
            file_stem = json_path.stem
            json_filename = json_path.name
            pdf_filename = f"{file_stem}.pdf"

            doc_folder = output_dir / file_stem
            doc_folder.mkdir(parents=True, exist_ok=True)

            # Download JSON
            local_json_path = doc_folder / json_filename
            blob.download_to_filename(local_json_path)

            # Look up PDF in index and download
            if pdf_filename in pdf_index:
                source_bucket_name, pdf_blob_path = pdf_index[pdf_filename]
                source_bucket = client.bucket(source_bucket_name)
                pdf_blob = source_bucket.blob(pdf_blob_path)

                local_pdf_path = doc_folder / pdf_filename
                pdf_blob.download_to_filename(local_pdf_path)
                downloaded_count += 1
            else:
                missing_pdf_count += 1

            pbar.set_postfix({"Paired": downloaded_count, "Missing": missing_pdf_count})
            pbar.update(1)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Successfully processed and paired: {downloaded_count} documents.")
    if missing_pdf_count > 0:
        print(f"Missing PDF matches: {missing_pdf_count}")
    print("=" * 60)


if __name__ == "__main__":
    process_and_download()