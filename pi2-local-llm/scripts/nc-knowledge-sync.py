#!/usr/bin/env python3
"""
nc-knowledge-sync.py

Syncs files from a local directory (e.g. /mnt/nextcloud mounted via WebDAV)
into an Open WebUI Knowledge Base via its REST API.

Designed for headless Raspberry Pi with Open WebUI + Ollama.

Features:
  - Creates the knowledge base if it doesn't exist
  - Uploads new files, updates changed files, removes deleted files
  - Waits for async file processing to complete before adding to KB
  - Tracks sync state via a JSON manifest to detect changes
  - Reindexes the knowledge base after sync
  - Dry-run mode for safe testing
  - Supports file extension filtering

Usage:
  # First run — creates KB and uploads everything:
  python3 nc-knowledge-sync.py \
    --url https://YOUR_PI2_TAILSCALE_IP \
    --token YOUR_API_KEY \
    --knowledge "Nextcloud Documents" \
    --folder /mnt/nextcloud/Documents

  # Dry run — see what would change without doing anything:
  python3 nc-knowledge-sync.py \
    --url https://YOUR_PI2_TAILSCALE_IP \
    --token YOUR_API_KEY \
    --knowledge "Nextcloud Documents" \
    --folder /mnt/nextcloud/Documents \
    --dry-run

  # With extension filter:
  python3 nc-knowledge-sync.py \
    --url https://YOUR_PI2_TAILSCALE_IP \
    --token YOUR_API_KEY \
    --knowledge "Nextcloud Documents" \
    --folder /mnt/nextcloud/Documents \
    --extensions .pdf .md .txt .docx

API key: Open WebUI → Admin Panel → enable API Keys + Default Permissions →
         then Settings → Account → API Keys → Create

Author: Vincent Vikor Part of the Raspberry Pi local LLM stack (https://github.com/vincevv017/pi-homelab)
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib3

# Suppress SSL warnings for self-signed Tailscale certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    import requests
except ImportError:
    print("ERROR: 'requests' is required. Install with:")
    print("  sudo apt install python3-requests -y")
    sys.exit(1)


# ── Defaults ─────────────────────────────────────────────────────────

DEFAULT_EXTENSIONS = {
    ".pdf", ".md", ".txt", ".docx", ".doc",
    ".csv", ".json", ".html", ".htm",
    ".rtf", ".odt", ".pptx", ".xlsx",
}
MANIFEST_FILENAME = ".nc-sync-manifest.json"
POLL_INTERVAL = 2       # seconds between processing status checks
POLL_TIMEOUT = 300      # max seconds to wait for file processing


# ── Helpers ──────────────────────────────────────────────────────────

def sha256_file(filepath):
    """Compute SHA-256 hash of a file's contents."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(manifest_path):
    """Load the sync manifest (tracks previously synced files and their hashes)."""
    if os.path.exists(manifest_path):
        with open(manifest_path, "r") as f:
            return json.load(f)
    return {}


def save_manifest(manifest_path, manifest):
    """Persist the sync manifest to disk."""
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)


# ── API Client ───────────────────────────────────────────────────────

class OpenWebUIClient:
    def __init__(self, base_url, token, verify_ssl=False):
        self.base_url = base_url.rstrip("/")
        self.api = f"{self.base_url}/api/v1"
        self.token = token
        self.verify = verify_ssl
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        self.session.verify = self.verify

    def _get(self, path):
        r = self.session.get(f"{self.api}{path}")
        r.raise_for_status()
        return r.json()

    def _post(self, path, json_data=None, files=None):
        r = self.session.post(f"{self.api}{path}", json=json_data, files=files)
        r.raise_for_status()
        return r.json()

    def _delete(self, path, json_data=None):
        r = self.session.delete(f"{self.api}{path}", json=json_data)
        r.raise_for_status()
        return r.json()

    # ── Knowledge Base operations ────────────────────────────────

    def list_knowledge_bases(self):
        """List all knowledge bases."""
        data = self._get("/knowledge/")
        # API returns {"items": [...], "total": N} in recent versions
        if isinstance(data, dict) and "items" in data:
            return data["items"]
        return data

    def find_knowledge_by_name(self, name):
        """Find a KB by name, return its ID or None."""
        for kb in self.list_knowledge_bases():
            if kb["name"] == name:
                return kb["id"]
        return None

    def create_knowledge_base(self, name, description=""):
        """Create a new knowledge base, return its ID."""
        data = {"name": name, "description": description}
        r = self._post("/knowledge/create", json_data=data)
        return r["id"]

    def get_knowledge_files(self, kb_id):
        """Get list of files in a knowledge base."""
        data = self._get(f"/knowledge/{kb_id}")
        return data.get("files", []) or []

    def add_file_to_knowledge(self, kb_id, file_id):
        """Associate an uploaded file with a knowledge base."""
        try:
            self._post(f"/knowledge/{kb_id}/file/add", json_data={"file_id": file_id})
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 400 and "uplicate content" in e.response.text:
                # File content already exists in this KB — treat as success
                return "duplicate"
            raise
        return "added"

    def remove_file_from_knowledge(self, kb_id, file_id):
        """Remove a file from a knowledge base."""
        self._post(
            f"/knowledge/{kb_id}/file/remove",
            json_data={"file_id": file_id},
        )

    def reindex_knowledge(self, kb_id):
        """Trigger reindexing of a knowledge base."""
        try:
            self._post(f"/knowledge/{kb_id}/reindex")
        except requests.exceptions.HTTPError:
            # Some versions don't have this endpoint — non-fatal
            pass

    # ── File operations ──────────────────────────────────────────

    def upload_file(self, filepath):
        """Upload a file, return the file metadata dict."""
        with open(filepath, "rb") as f:
            r = self._post("/files/", files={"file": f})
        return r

    def wait_for_processing(self, file_id, timeout=POLL_TIMEOUT):
        """Poll until file processing completes or times out."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                status_data = self._get(f"/files/{file_id}/process/status")
                status = status_data.get("status", "")
                if status == "completed":
                    return True
                if status == "failed":
                    error = status_data.get("error", "unknown")
                    print(f"    ⚠ Processing failed: {error}")
                    return False
            except requests.exceptions.HTTPError as e:
                # Some versions return 404 if processing hasn't started yet
                if e.response.status_code == 404:
                    pass
                else:
                    raise
            time.sleep(POLL_INTERVAL)
        print(f"    ⚠ Processing timed out after {timeout}s")
        return False

    def delete_file(self, file_id):
        """Delete a file from Open WebUI."""
        self._delete(f"/files/{file_id}")

    def get_file_info(self, file_id):
        """Get metadata for a file."""
        return self._get(f"/files/{file_id}")


# ── Sync Logic ───────────────────────────────────────────────────────

def collect_local_files(folder, extensions):
    """Walk folder recursively and return dict of {relative_path: absolute_path}."""
    files = {}
    for root, _, filenames in os.walk(folder):
        for fname in filenames:
            # Skip hidden files and the manifest
            if fname.startswith("."):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if extensions and ext not in extensions:
                continue
            abs_path = os.path.join(root, fname)
            rel_path = os.path.relpath(abs_path, folder)
            files[rel_path] = abs_path
    return files


def sync(client, kb_id, folder, extensions, manifest_path, dry_run=False):
    """
    Perform an incremental sync between local folder and Open WebUI KB.

    Uses a local manifest to track which files have been synced and their
    content hashes, so only new/changed files are uploaded.
    """
    manifest = load_manifest(manifest_path)
    local_files = collect_local_files(folder, extensions)

    # Categorise files
    to_upload = []    # new or changed
    to_delete = []    # in manifest but no longer on disk
    unchanged = []

    for rel_path, abs_path in local_files.items():
        current_hash = sha256_file(abs_path)
        prev = manifest.get(rel_path)
        if prev is None:
            to_upload.append((rel_path, abs_path, current_hash))
        elif prev.get("hash") != current_hash:
            to_upload.append((rel_path, abs_path, current_hash))
        else:
            unchanged.append(rel_path)

    for rel_path in list(manifest.keys()):
        if rel_path not in local_files:
            to_delete.append(rel_path)

    # Summary
    print(f"\n{'═' * 60}")
    print(f"  Sync: {folder} → KB '{kb_id}'")
    print(f"{'═' * 60}")
    print(f"  Local files found:  {len(local_files)}")
    print(f"  Unchanged:          {len(unchanged)}")
    print(f"  New / changed:      {len(to_upload)}")
    print(f"  Deleted from disk:  {len(to_delete)}")
    if dry_run:
        print(f"  Mode:               DRY RUN (no changes)")
    print(f"{'═' * 60}\n")

    if dry_run:
        if to_upload:
            print("Would upload:")
            for rel, _, _ in to_upload:
                print(f"  + {rel}")
        if to_delete:
            print("Would delete:")
            for rel in to_delete:
                print(f"  - {rel}")
        if not to_upload and not to_delete:
            print("Nothing to do.")
        return

    # ── Delete removed files ─────────────────────────────────────
    for rel_path in to_delete:
        entry = manifest[rel_path]
        file_id = entry.get("file_id")
        if file_id:
            print(f"  ✕ Removing: {rel_path}")
            try:
                client.remove_file_from_knowledge(kb_id, file_id)
                client.delete_file(file_id)
            except requests.exceptions.HTTPError as e:
                print(f"    ⚠ Error removing {rel_path}: {e}")
        del manifest[rel_path]

    # ── Upload new / changed files ───────────────────────────────
    for rel_path, abs_path, file_hash in to_upload:
        print(f"  ↑ Uploading: {rel_path}")

        # If this file was previously synced (changed), remove old version first
        prev = manifest.get(rel_path)
        if prev and prev.get("file_id"):
            try:
                client.remove_file_from_knowledge(kb_id, prev["file_id"])
                client.delete_file(prev["file_id"])
                print(f"    (replaced previous version)")
            except requests.exceptions.HTTPError:
                pass

        try:
            # Upload
            file_data = client.upload_file(abs_path)
            file_id = file_data["id"]

            # Wait for processing
            print(f"    Processing...", end="", flush=True)
            if client.wait_for_processing(file_id):
                print(" done")
                # Add to KB
                result = client.add_file_to_knowledge(kb_id, file_id)
                if result == "duplicate":
                    print(f"    ℹ Already in KB (duplicate content) — skipping")
                manifest[rel_path] = {
                    "file_id": file_id,
                    "hash": file_hash,
                    "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            else:
                print(" failed — skipping")
                # Still record it so we retry on next sync with a different hash
                manifest[rel_path] = {
                    "file_id": file_id,
                    "hash": "",  # empty hash forces re-upload next run
                    "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "status": "processing_failed",
                }

        except requests.exceptions.HTTPError as e:
            print(f"\n    ⚠ Upload failed: {e}")
            continue

    # ── Reindex ──────────────────────────────────────────────────
    if to_upload or to_delete:
        print("\n  Reindexing knowledge base...")
        client.reindex_knowledge(kb_id)
        print("  Done.")

    # ── Save manifest ────────────────────────────────────────────
    save_manifest(manifest_path, manifest)
    print(f"\n  Manifest saved: {manifest_path}")
    print(f"  Total files in KB: {len(manifest)}")


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sync a local folder into an Open WebUI Knowledge Base",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full sync
  %(prog)s --url https://100.x.x.x --token sk-xxx \\
           --knowledge "Nextcloud Documents" --folder /mnt/nextcloud/Documents

  # Dry run
  %(prog)s --url https://100.x.x.x --token sk-xxx \\
           --knowledge "Nextcloud Documents" --folder /mnt/nextcloud/Documents \\
           --dry-run

  # Only PDFs and Markdown
  %(prog)s --url https://100.x.x.x --token sk-xxx \\
           --knowledge "Nextcloud Documents" --folder /mnt/nextcloud/Documents \\
           --extensions .pdf .md
        """,
    )
    parser.add_argument(
        "--url", required=True,
        help="Open WebUI base URL (e.g. https://100.x.x.x)",
    )
    parser.add_argument(
        "--token", required=True,
        help="Open WebUI API key (Settings → Account → API Keys — requires admin to enable first)",
    )
    parser.add_argument(
        "--knowledge", required=True,
        help="Knowledge base name (created if it doesn't exist)",
    )
    parser.add_argument(
        "--folder", required=True,
        help="Local folder to sync (e.g. /mnt/nextcloud/Documents)",
    )
    parser.add_argument(
        "--extensions", nargs="*", default=None,
        help="File extensions to include (e.g. .pdf .md .txt). Default: all common document types",
    )
    parser.add_argument(
        "--manifest", default=None,
        help="Path to sync manifest file (default: FOLDER/.nc-sync-manifest.json)",
    )
    parser.add_argument(
        "--description", default="Auto-synced from Nextcloud via nc-knowledge-sync",
        help="Description for new knowledge base (only used on creation)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without making changes",
    )

    args = parser.parse_args()

    # Validate folder
    if not os.path.isdir(args.folder):
        print(f"ERROR: Folder not found: {args.folder}")
        print("Is the WebDAV mount active? Check: ls /mnt/nextcloud")
        sys.exit(1)

    # Extension filter
    extensions = None
    if args.extensions:
        extensions = {e if e.startswith(".") else f".{e}" for e in args.extensions}
    else:
        extensions = DEFAULT_EXTENSIONS

    # Manifest path
    manifest_path = args.manifest or os.path.join(args.folder, MANIFEST_FILENAME)
    # If folder is read-only (WebDAV), fall back to home directory
    if not os.access(os.path.dirname(manifest_path) or ".", os.W_OK):
        manifest_path = os.path.expanduser(
            f"~/{MANIFEST_FILENAME.replace('.json', '')}-{args.knowledge.replace(' ', '_')}.json"
        )
        print(f"Note: Folder is read-only, manifest stored at: {manifest_path}")

    # Connect
    print(f"Connecting to {args.url}...")
    client = OpenWebUIClient(args.url, args.token)

    # Verify connection
    try:
        client.list_knowledge_bases()
        print("Connected successfully.")
    except requests.exceptions.ConnectionError:
        print(f"ERROR: Cannot connect to {args.url}")
        print("Is Open WebUI running? Is Tailscale connected?")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            print("ERROR: Authentication failed. Check your API token.")
            print("Generate one at: Open WebUI → Settings → Account → API Keys")
            print("(Requires: Admin Panel → Settings → General → Enable API Keys + Default Permissions → API Keys)")
        else:
            print(f"ERROR: {e}")
        sys.exit(1)

    # Find or create KB
    kb_id = client.find_knowledge_by_name(args.knowledge)
    if kb_id:
        print(f"Found knowledge base: '{args.knowledge}' (id: {kb_id})")
    else:
        if args.dry_run:
            print(f"Would create knowledge base: '{args.knowledge}'")
            kb_id = "dry-run-placeholder"
        else:
            print(f"Creating knowledge base: '{args.knowledge}'...")
            kb_id = client.create_knowledge_base(args.knowledge, args.description)
            print(f"Created with id: {kb_id}")

    # Sync
    sync(client, kb_id, args.folder, extensions, manifest_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
