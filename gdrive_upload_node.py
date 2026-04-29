"""
ComfyUI Google Drive Upload Node
Uploads generated images directly to a Google Drive folder via API.

Supports two auth methods:
  1. Service Account JSON key  (recommended for automation)
  2. OAuth2 Client credentials (user-consent flow, token cached locally)
"""

import io
import json
import os
import time
from pathlib import Path

import numpy as np
from PIL import Image


# ---------------------------------------------------------------------------
# Lazy imports so the node loads even if google libs are not installed yet.
# A clear error is shown inside the node execution instead of at startup.
# ---------------------------------------------------------------------------

def _import_google_libs():
    """Return (build_func, Credentials classes) or raise ImportError with hint."""
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseUpload
        from google.oauth2 import service_account
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        return build, MediaIoBaseUpload, service_account, Credentials, InstalledAppFlow, Request
    except ImportError as e:
        raise ImportError(
            f"Google API libraries not found: {e}\n"
            "Install them with:\n"
            "  pip install google-api-python-client google-auth-httplib2 "
            "google-auth-oauthlib"
        ) from e


# OAuth2 token cache location (next to this file)
_TOKEN_CACHE = Path(__file__).parent / "gdrive_oauth_token.json"
_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _tensor_to_pil(tensor) -> Image.Image:
    """Convert a ComfyUI image tensor (H,W,3) float32 [0-1] to PIL Image."""
    array = (tensor.cpu().numpy() * 255).clip(0, 255).astype(np.uint8)
    return Image.fromarray(array, "RGB")


def _build_service_from_service_account(json_key: str):
    """Authenticate with a Service Account JSON key string."""
    build, MediaIoBaseUpload, service_account, *_ = _import_google_libs()
    info = json.loads(json_key)
    creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
    return build("drive", "v3", credentials=creds), MediaIoBaseUpload


def _build_service_from_oauth(client_secret_json: str):
    """Authenticate with OAuth2 (opens browser on first run, caches token)."""
    build, MediaIoBaseUpload, _, Credentials, InstalledAppFlow, Request = _import_google_libs()

    creds = None
    if _TOKEN_CACHE.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_CACHE), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            client_config = json.loads(client_secret_json)
            flow = InstalledAppFlow.from_client_config(client_config, _SCOPES)
            creds = flow.run_local_server(port=0)
        _TOKEN_CACHE.write_text(creds.to_json())

    return build("drive", "v3", credentials=creds), MediaIoBaseUpload


def _get_or_create_folder(service, folder_name: str, parent_id: str = None) -> str:
    """Return the Drive folder ID, creating the folder if it doesn't exist."""
    query = (
        f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false"
    )
    if parent_id:
        query += f" and '{parent_id}' in parents"

    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        metadata["parents"] = [parent_id]

    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def _upload_image(service, MediaIoBaseUpload, image_bytes: bytes,
                  filename: str, folder_id: str) -> str:
    """Upload PNG bytes to Drive, return the file URL."""
    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaIoBaseUpload(
        io.BytesIO(image_bytes), mimetype="image/png", resumable=True
    )
    uploaded = (
        service.files()
        .create(body=file_metadata, media_body=media, fields="id, webViewLink")
        .execute()
    )
    return uploaded.get("webViewLink", uploaded.get("id", "unknown"))


# ---------------------------------------------------------------------------
# ComfyUI Node
# ---------------------------------------------------------------------------

class GoogleDriveUploadNode:
    """
    ComfyUI node that uploads one or more images to a Google Drive folder.

    Auth modes
    ----------
    service_account  – Paste the full contents of a Service Account JSON key.
    oauth2           – Paste the full contents of an OAuth2 client_secret JSON.
                       A browser window opens on the first run; token is cached.
    """

    CATEGORY = "image/upload"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("upload_log",)
    FUNCTION = "upload_images"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "folder_name": (
                    "STRING",
                    {
                        "default": "ComfyUI_Results",
                        "multiline": False,
                        "tooltip": "Drive folder name (will be created if absent)",
                    },
                ),
                "auth_mode": (
                    ["service_account", "oauth2"],
                    {
                        "default": "service_account",
                        "tooltip": (
                            "service_account: paste Service Account JSON key below.\n"
                            "oauth2: paste OAuth2 client_secret JSON below "
                            "(browser opens on first run)."
                        ),
                    },
                ),
                "credentials_json": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": True,
                        "tooltip": (
                            "Paste the full JSON key here.\n"
                            "Service Account: contents of the downloaded .json key file.\n"
                            "OAuth2: contents of the client_secret_*.json file."
                        ),
                    },
                ),
            },
            "optional": {
                "filename_prefix": (
                    "STRING",
                    {
                        "default": "image",
                        "multiline": False,
                        "tooltip": "Prefix for uploaded filenames, e.g. 'image' → image_001.png",
                    },
                ),
                "parent_folder_id": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "tooltip": (
                            "Optional: Drive folder ID of a parent folder.\n"
                            "Leave empty to create the folder at Drive root."
                        ),
                    },
                ),
            },
        }

    # ------------------------------------------------------------------

    def upload_images(
            self,
            images,
            folder_name: str,
            auth_mode: str,
            credentials_json: str,
            filename_prefix: str = "image",
            parent_folder_id: str = "",
    ):
        credentials_json = credentials_json.strip()
        if not credentials_json:
            msg = "❌ credentials_json is empty – paste your JSON key in the node."
            print(f"[GDriveUpload] {msg}")
            return (msg,)

        # ---- Authenticate ------------------------------------------------
        try:
            if auth_mode == "service_account":
                service, MediaIoBaseUpload = _build_service_from_service_account(
                    credentials_json
                )
            else:
                service, MediaIoBaseUpload = _build_service_from_oauth(
                    credentials_json
                )
        except ImportError as e:
            msg = f"❌ Import error: {e}"
            print(f"[GDriveUpload] {msg}")
            return (msg,)
        except Exception as e:
            msg = f"❌ Auth failed: {e}"
            print(f"[GDriveUpload] {msg}")
            return (msg,)

        # ---- Resolve / create target folder ------------------------------
        try:
            pid = parent_folder_id.strip() or None
            folder_id = _get_or_create_folder(service, folder_name, pid)
        except Exception as e:
            msg = f"❌ Folder error: {e}"
            print(f"[GDriveUpload] {msg}")
            return (msg,)

        # ---- Upload each image -------------------------------------------
        # `images` can be a single tensor (H,W,3) or a batch (N,H,W,3).
        import torch
        if isinstance(images, torch.Tensor):
            if images.ndim == 3:
                images = images.unsqueeze(0)  # add batch dim
        else:
            # fallback: wrap in list
            images = [images]

        timestamp = int(time.time())
        log_lines = []

        for idx, img_tensor in enumerate(images):
            pil_img = _tensor_to_pil(img_tensor)
            buf = io.BytesIO()
            pil_img.save(buf, format="PNG")
            png_bytes = buf.getvalue()

            filename = f"{filename_prefix}_{timestamp}_{idx + 1:03d}.png"

            try:
                url = _upload_image(
                    service, MediaIoBaseUpload, png_bytes, filename, folder_id
                )
                line = f"✅ {filename} → {url}"
            except Exception as e:
                line = f"❌ {filename} upload failed: {e}"

            print(f"[GDriveUpload] {line}")
            log_lines.append(line)

        result = "\n".join(log_lines)
        return (result,)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

NODE_CLASS_MAPPINGS = {
    "GoogleDriveUpload": GoogleDriveUploadNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "GoogleDriveUpload": "📤 Google Drive Upload",
}