"""
ComfyUI – Google Drive Upload
==============================
oauth_token_in  ← STRING нода (буфер токена)
oauth_token_out → STRING нода (буфер токена)

Первый запуск: вставить client_secret_json → браузер → токен появится на выходе
               → сохранить в String ноду → подать обратно на вход.
Следующие запуски: токен читается со входа, обновляется если истёк,
                   актуальная версия выходит снова.
"""

import io
import json
import time
from pathlib import Path

import numpy as np
from PIL import Image

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


# ── lazy imports ───────────────────────────────────────────────────────────
def _google():
    try:
        from googleapiclient.discovery import build
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        return build, Credentials, InstalledAppFlow, Request
    except ImportError as e:
        raise ImportError(
            f"Отсутствуют библиотеки Google: {e}\n"
            "pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        ) from e


# ── авторизация ────────────────────────────────────────────────────────────
def _get_service(client_secret_json: str, cached_token: str):
    """Возвращает (service, актуальный_token_json)."""
    build, Credentials, InstalledAppFlow, Request = _google()

    creds = None

    if cached_token.strip():
        try:
            creds = Credentials.from_authorized_user_info(
                json.loads(cached_token), _SCOPES
            )
        except Exception:
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None

    if not creds or not creds.valid:
        cs = client_secret_json.strip()
        if not cs:
            raise ValueError(
                "Токен не найден.\n"
                "Вставь client_secret_*.json в поле 'client_secret_json' и запусти граф.\n"
                "Откроется браузер — войди в Google (один раз).\n"
                "Токен появится на выходе oauth_token_out — подключи его к String ноде."
            )
        flow  = InstalledAppFlow.from_client_config(json.loads(cs), _SCOPES)
        creds = flow.run_local_server(port=0)

    return build("drive", "v3", credentials=creds), creds.to_json()


# ── Drive утилиты ──────────────────────────────────────────────────────────
def _get_or_create_folder(svc, name: str, parent_id: str = None) -> str:
    safe = name.replace("'", "\\'")
    q    = f"name='{safe}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    res = svc.files().list(q=q, fields="files(id)").execute().get("files", [])
    if res:
        return res[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    return svc.files().create(body=meta, fields="id").execute()["id"]


def _upload_bytes(svc, data: bytes, filename: str, folder_id: str) -> str:
    from googleapiclient.http import MediaIoBaseUpload
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype="image/png", resumable=True)
    f = svc.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=media,
        fields="id,webViewLink",
    ).execute()
    return f.get("webViewLink") or f.get("id", "?")


def _upload_file(svc, filepath: Path, folder_id: str) -> str:
    import mimetypes
    from googleapiclient.http import MediaFileUpload
    mime, _ = mimetypes.guess_type(str(filepath))
    mime     = mime or "application/octet-stream"
    media    = MediaFileUpload(str(filepath), mimetype=mime, resumable=True)
    f = svc.files().create(
        body={"name": filepath.name, "parents": [folder_id]},
        media_body=media,
        fields="id,webViewLink",
    ).execute()
    return f.get("webViewLink") or f.get("id", "?")


def _tensor_to_png(tensor) -> bytes:
    arr = (tensor.cpu().numpy() * 255).clip(0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, format="PNG")
    return buf.getvalue()


# ── нода ──────────────────────────────────────────────────────────────────
class GoogleDriveUploadNode:
    CATEGORY     = "image/upload"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("oauth_token_out",)
    FUNCTION     = "run"
    OUTPUT_NODE  = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "client_secret_json": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": (
                        "Только для первой авторизации: содержимое client_secret_*.json.\n"
                        "После первого запуска токен выйдет из oauth_token_out —\n"
                        "подключи его к String ноде и обратно на oauth_token_in.\n"
                        "Затем это поле можно очистить."
                    ),
                }),
                "drive_folder_name": ("STRING", {
                    "default": "ComfyUI_Uploads",
                    "multiline": False,
                }),
                "drive_parent_folder_id": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "ID папки на Google Drive. Из URL: drive.google.com/drive/folders/<ВОТ_ЭТО>",
                }),
            },
            "optional": {
                "oauth_token_in": ("STRING", {
                    "forceInput": True,
                    "tooltip": (
                        "Подключи String ноду с сохранённым токеном.\n"
                        "Если не подключено — нужен client_secret_json для авторизации."
                    ),
                }),
                "images": ("IMAGE", {}),
                "source_path": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "Путь к файлу или папке. Папка — все файлы по очереди.",
                }),
                "filename_prefix": ("STRING", {
                    "default": "image",
                    "multiline": False,
                }),
            },
        }

    def run(
        self,
        client_secret_json: str,
        drive_folder_name: str,
        drive_parent_folder_id: str,
        oauth_token_in: str = "",
        images=None,
        source_path: str = "",
        filename_prefix: str = "image",
    ):
        log = []
        def L(msg):
            print(f"[GDrive] {msg}")
            log.append(msg)

        # ── авторизация ────────────────────────────────────────────────
        try:
            svc, new_token = _get_service(client_secret_json, oauth_token_in or "")
            L("🔑 Авторизован")
        except Exception as e:
            return {"ui": {"text": [f"❌ Авторизация: {e}"]}, "result": ("",)}

        # ── папка ──────────────────────────────────────────────────────
        try:
            parent_id   = drive_parent_folder_id.strip() or None
            folder_name = drive_folder_name.strip() or "ComfyUI_Uploads"
            folder_id   = _get_or_create_folder(svc, folder_name, parent_id)
            L(f"📁 '{folder_name}' (id={folder_id})")
        except Exception as e:
            return {"ui": {"text": [f"❌ Папка: {e}"]}, "result": (new_token,)}

        ts = int(time.time())

        # ══ images tensor ══════════════════════════════════════════════
        if images is not None:
            import torch
            imgs = images
            if isinstance(imgs, torch.Tensor) and imgs.ndim == 3:
                imgs = imgs.unsqueeze(0)
            for i, img in enumerate(imgs):
                fname = f"{filename_prefix}_{ts}_{i+1:03d}.png"
                try:
                    url = _upload_bytes(svc, _tensor_to_png(img), fname, folder_id)
                    L(f"✅ {fname} → {url}")
                except Exception as e:
                    L(f"❌ {fname}: {e}")

        # ══ source_path ════════════════════════════════════════════════
        elif source_path.strip():
            p = Path(source_path.strip())
            if not p.exists():
                L(f"❌ Путь не найден: {p}")
            elif p.is_file():
                try:
                    L(f"✅ {p.name} → {_upload_file(svc, p, folder_id)}")
                except Exception as e:
                    L(f"❌ {p.name}: {e}")
            elif p.is_dir():
                files = sorted(f for f in p.iterdir() if f.is_file())
                if not files:
                    L("⚠ Папка пустая")
                for fp in files:
                    try:
                        L(f"✅ {fp.name} → {_upload_file(svc, fp, folder_id)}")
                    except Exception as e:
                        L(f"❌ {fp.name}: {e}")
        else:
            L("⚠ Нет входных данных: подключи images или укажи source_path")

        return {"ui": {"text": ["\n".join(log)]}, "result": (new_token,)}


# ── регистрация ────────────────────────────────────────────────────────────
NODE_CLASS_MAPPINGS        = {"GoogleDriveUpload": GoogleDriveUploadNode}
NODE_DISPLAY_NAME_MAPPINGS = {"GoogleDriveUpload": "📤 Google Drive Upload"}