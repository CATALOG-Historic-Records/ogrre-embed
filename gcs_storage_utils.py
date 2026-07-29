from google.cloud import storage
import os

class GCSStorageUtils:
    def __init__(self, project_id):
        self.project_id = project_id
        self.storage_client = storage.Client(project=project_id)

    @staticmethod
    def parse_gcs_uri(gcs_uri):
        if not gcs_uri.startswith("gs://"):
            raise ValueError(f"Invalid GCS URI: {gcs_uri}")
        parts = gcs_uri[5:].split("/", 1)
        bucket_name = parts[0]
        prefix = parts[1] if len(parts) > 1 else ""
        return bucket_name, prefix

    def list_gcs_files(self, gcs_uri, suffix=".pdf"):
        bucket_name, prefix = self.parse_gcs_uri(gcs_uri)
        if prefix and not prefix.endswith("/") and not prefix.lower().endswith(suffix.lower()):
            prefix += "/"
        bucket = self.storage_client.bucket(bucket_name)
        blobs = bucket.list_blobs(prefix=prefix)
        suffix_lower = suffix.lower()
        return [f"gs://{bucket_name}/{blob.name}"
                for blob in blobs
                if blob.name.lower().endswith(suffix_lower)]

    def download_gcs_to_memory(self, gcs_uri):
        bucket_name, blob_name = self.parse_gcs_uri(gcs_uri)
        bucket = self.storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        return blob.download_as_bytes()

    def upload_file_to_gcs(self, gcs_uri, pdf_doc=None, local_file_path=None):
        bucket_name, blob_name = self.parse_gcs_uri(gcs_uri)
        bucket = self.storage_client.bucket(bucket_name)
        if not bucket.exists():
            print(
                f"Destination bucket '{bucket_name}' does not exist. Attempting to create it now..."
            )
            try:
                # Create the bucket on GCS
                bucket = self.storage_client.create_bucket(
                    bucket_name, location="US", project=self.project_id
                )
                print(f"Created bucket '{bucket_name}' successfully.")
            except Exception as creation_error:
                print(
                    f"Failed to create bucket '{bucket_name}': {str(creation_error)}"
                )
                raise creation_error

        blob = bucket.blob(blob_name)
        if pdf_doc:
            output_bytes = pdf_doc.tobytes()
            blob.upload_from_string(output_bytes, content_type="application/pdf")
        elif local_file_path:
            with open(local_file_path, "rb") as f:
                blob.upload_from_file(f, content_type="application/pdf")