"""
ComfyUI Google Drive Upload Node
Uploads generated images directly to a Google Drive folder via API.

Auth modes:
  1. Service Account JSON key  – paste JSON, no browser needed
  2. OAuth2                    – click "Authorize" button in the node UI
"""

import io
import json
import os
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
_TOKEN_CACHE = _HERE / "gdrive_oauth_token.json"
_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# In-memory auth state shared between HTTP handlers and node execution
_auth_state = {
    "status": "unknown",   # "unknown" | "authorized" | "error"
    "email": "",
    "error": "",
}
_auth_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Lazy Google imports
# ---------------------------------------------------------------------------

def _import_google_libs():
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseUpload
        from google.oauth2 import service_account
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        return (build, MediaIoBaseUpload, service_account,
                Credentials, InstalledAppFlow, Request)
    except ImportError as e:
        raise ImportError(
            f"Google API libraries not found: {e}\n"
            "Run: pip install google-api-python-client "
            "google-auth-httplib2 google-auth-oauthlib"
        ) from e


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _build_service_account(json_key: str):
    build, MediaIoBaseUpload, service_account, *_ = _import_google_libs()
    info = json.loads(json_key)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=_SCOPES
    )
    return build("drive", "v3", credentials=creds), MediaIoBaseUpload


def _build_oauth_from_cache():
    """Build Drive service from cached OAuth token. Raises if no valid cache."""
    build, MediaIoBaseUpload, _, Credentials, _flow, Request = _import_google_libs()

    if not _TOKEN_CACHE.exists():
        raise FileNotFoundError("No cached OAuth token. Please authorize first.")

    creds = Credentials.from_authorized_user_file(str(_TOKEN_CACHE), _SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _TOKEN_CACHE.write_text(creds.to_json())

    if not creds.valid:
        raise ValueError("Cached token is invalid. Please re-authorize.")

    return build("drive", "v3", credentials=creds), MediaIoBaseUpload


def _do_oauth_flow(client_secret_json: str):
    """Run OAuth2 InstalledApp flow, cache token, update _auth_state."""
    build, _, __, ___, InstalledAppFlow, ____ = _import_google_libs()

    client_config = json.loads(client_secret_json)
    flow = InstalledAppFlow.from_client_config(client_config, _SCOPES)
    creds = flow.run_local_server(port=0)
    _TOKEN_CACHE.write_text(creds.to_json())

    try:
        svc = build("oauth2", "v2", credentials=creds)
        info = svc.userinfo().get().execute()
        email = info.get("email", "")
    except Exception:
        email = ""

    with _auth_state_lock:
        _auth_state["status"] = "authorized"
        _auth_state["email"] = email
        _auth_state["error"] = ""


def _check_cached_token_status():
    """Refresh _auth_state from cached token (no browser)."""
    try:
        build, _, __, Credentials, _flow, Request = _import_google_libs()

        if not _TOKEN_CACHE.exists():
            with _auth_state_lock:
                _auth_state["status"] = "unknown"
                _auth_state["email"] = ""
            return

        creds = Credentials.from_authorized_user_file(str(_TOKEN_CACHE), _SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _TOKEN_CACHE.write_text(creds.to_json())

        if creds.valid:
            try:
                svc = build("oauth2", "v2", credentials=creds)
                info = svc.userinfo().get().execute()
                email = info.get("email", "")
            except Exception:
                email = ""
            with _auth_state_lock:
                _auth_state["status"] = "authorized"
                _auth_state["email"] = email
                _auth_state["error"] = ""
        else:
            with _auth_state_lock:
                _auth_state["status"] = "unknown"
                _auth_state["email"] = ""
    except Exception:
        with _auth_state_lock:
            _auth_state["status"] = "unknown"
            _auth_state["email"] = ""


# ---------------------------------------------------------------------------
# Drive utilities
# ---------------------------------------------------------------------------

def _get_or_create_folder(service, folder_name: str, parent_id=None) -> str:
    query = (
        f"name='{folder_name}' and "
        "mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    if parent_id:
        query += f" and '{parent_id}' in parents"

    results = service.files().list(q=query, fields="files(id)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {"name": folder_name,
                "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        metadata["parents"] = [parent_id]

    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def _upload_image(service, MediaIoBaseUpload,
                  image_bytes: bytes, filename: str, folder_id: str) -> str:
    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaIoBaseUpload(
        io.BytesIO(image_bytes), mimetype="image/png", resumable=True
    )
    uploaded = (
        service.files()
        .create(body=file_metadata, media_body=media, fields="id,webViewLink")
        .execute()
    )
    return uploaded.get("webViewLink", uploaded.get("id", "unknown"))


def _tensor_to_pil(tensor) -> Image.Image:
    array = (tensor.cpu().numpy() * 255).clip(0, 255).astype(np.uint8)
    return Image.fromarray(array, "RGB")


# ---------------------------------------------------------------------------
# HTTP API endpoints
# ---------------------------------------------------------------------------

def _register_routes():
    try:
        from aiohttp import web
        from server import PromptServer

        routes = PromptServer.instance.routes

        @routes.get("/gdrive/auth_status")
        async def auth_status(_req):
            _check_cached_token_status()
            with _auth_state_lock:
                state = dict(_auth_state)
            return web.json_response(state)

        @routes.post("/gdrive/authorize")
        async def authorize(req: web.Request):
            try:
                body = await req.json()
                client_secret_json = body.get("credentials_json", "").strip()
                if not client_secret_json:
                    return web.json_response(
                        {"ok": False, "error": "credentials_json is empty"}, status=400
                    )

                def _run():
                    try:
                        _do_oauth_flow(client_secret_json)
                    except Exception as e:
                        with _auth_state_lock:
                            _auth_state["status"] = "error"
                            _auth_state["error"] = str(e)

                threading.Thread(target=_run, daemon=True).start()
                return web.json_response(
                    {"ok": True,
                     "message": "Browser window opened – complete sign-in there."}
                )
            except Exception as e:
                return web.json_response({"ok": False, "error": str(e)}, status=500)

        @routes.post("/gdrive/revoke")
        async def revoke(_req):
            try:
                if _TOKEN_CACHE.exists():
                    _TOKEN_CACHE.unlink()
                with _auth_state_lock:
                    _auth_state["status"] = "unknown"
                    _auth_state["email"] = ""
                    _auth_state["error"] = ""
                return web.json_response({"ok": True})
            except Exception as e:
                return web.json_response({"ok": False, "error": str(e)}, status=500)

    except Exception as e:
        print(f"[GDriveUpload] Could not register routes: {e}")


_register_routes()
_check_cached_token_status()


# ---------------------------------------------------------------------------
# ComfyUI Node
# ---------------------------------------------------------------------------

class GoogleDriveUploadNode:
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
                    {"default": "ComfyUI_Results", "multiline": False},
                ),
                "auth_mode": (
                    ["oauth2", "service_account"],
                    {"default": "oauth2"},
                ),
                "credentials_json": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": True,
                        "password": True,
                        "tooltip": (
                            "oauth2: paste client_secret_*.json, then click Authorize.\n"
                            "service_account: paste Service Account JSON key."
                        ),
                    },
                ),
            },
            "optional": {
                "filename_prefix": (
                    "STRING",
                    {"default": "image", "multiline": False},
                ),
                "parent_folder_id": (
                    "STRING",
                    {"default": "", "multiline": False},
                ),
            },
        }

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

        try:
            if auth_mode == "service_account":
                if not credentials_json:
                    return ("❌ Paste your Service Account JSON key into credentials_json.",)
                service, MediaIoBaseUpload = _build_service_account(credentials_json)
            else:
                try:
                    service, MediaIoBaseUpload = _build_oauth_from_cache()
                except Exception as e:
                    return (
                        f"❌ OAuth2: {e}\n"
                        "Paste client_secret JSON and click Authorize in the node.",
                    )
        except ImportError as e:
            return (f"❌ Import error: {e}",)
        except Exception as e:
            return (f"❌ Auth failed: {e}",)

        try:
            pid = parent_folder_id.strip() or None
            folder_id = _get_or_create_folder(service, folder_name, pid)
        except Exception as e:
            return (f"❌ Folder error: {e}",)

        import torch
        if isinstance(images, torch.Tensor):
            if images.ndim == 3:
                images = images.unsqueeze(0)
        else:
            images = list(images)

        timestamp = int(time.time())
        log_lines = []

        for idx, img_tensor in enumerate(images):
            pil_img = _tensor_to_pil(img_tensor)
            buf = io.BytesIO()
            pil_img.save(buf, format="PNG")

            filename = f"{filename_prefix}_{timestamp}_{idx + 1:03d}.png"
            try:
                url = _upload_image(
                    service, MediaIoBaseUpload, buf.getvalue(), filename, folder_id
                )
                line = f"✅ {filename} → {url}"
            except Exception as e:
                line = f"❌ {filename}: {e}"

            print(f"[GDriveUpload] {line}")
            log_lines.append(line)

        return ("\n".join(log_lines),)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

NODE_CLASS_MAPPINGS = {"GoogleDriveUpload": GoogleDriveUploadNode}
NODE_DISPLAY_NAME_MAPPINGS = {"GoogleDriveUpload": "📤 Google Drive Upload"}
