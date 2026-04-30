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
import logging
import time
import warnings
from pathlib import Path

import numpy as np
from PIL import Image

# подавить предупреждение file_cache от googleapiclient.discovery
warnings.filterwarnings("ignore", message="file_cache is only supported with oauth2client")
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

# отключить файловый кеш discovery — именно он вызывает предупреждение
try:
    import googleapiclient.discovery
    googleapiclient.discovery.DISCOVERY_URI  # просто проверка импорта
    import googleapiclient.http
    # патчим _cache чтобы discovery не пытался писать файл
    from googleapiclient import discovery as _disc
    if hasattr(_disc, "_DISCOVERY_CACHE"):
        _disc._DISCOVERY_CACHE = None
except Exception:
    pass

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
        print("[GDrive] Открываю браузер для авторизации...")
        flow  = InstalledAppFlow.from_client_config(json.loads(cs), _SCOPES)
        creds = flow.run_local_server(port=0)
        print("[GDrive] Авторизация успешна")

    print(f"[GDrive] Токен валиден: {creds.valid}, истёк: {creds.expired}")
    return build("drive", "v3", credentials=creds, cache_discovery=False), creds.to_json()


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
    print(f"[GDrive] Загружаю {filename} ({len(data)} байт) в folder_id={folder_id}")
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype="image/png", resumable=True)
    f = svc.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=media,
        fields="id,webViewLink",
    ).execute()
    print(f"[GDrive] Ответ Drive: {f}")
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
                    "multiline": True,
                    "forceInput": True,
                    "tooltip": (
                        "Принимает:\n"
                        "  • одиночный путь: /data/output/img.jpg\n"
                        "  • JSON-список: [\"/data/a.jpg\", \"/data/b.jpg\"]\n"
                        "  • путь к папке: все файлы внутри загружаются по очереди"
                    ),
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
            L(f"📁 Ищу/создаю папку '{folder_name}' parent_id={parent_id!r}")
            folder_id   = _get_or_create_folder(svc, folder_name, parent_id)
            L(f"📁 Папка готова (id={folder_id})")
        except Exception as e:
            import traceback
            err = traceback.format_exc()
            print(f"[GDrive] ПАПКА ERROR:\n{err}")
            return {"ui": {"text": [f"❌ Папка: {e}\n{err}"]}, "result": (new_token,)}

        ts = int(time.time())

        # ══ images tensor ══════════════════════════════════════════════
        try:
            if images is not None:
                import torch
                L(f"🖼 images type={type(images)}, ndim={getattr(images, 'ndim', '?')} shape={getattr(images, 'shape', '?')} dtype={getattr(images, 'dtype', '?')} ")
                imgs = images
                if isinstance(imgs, torch.Tensor) and imgs.ndim == 3:
                    imgs = imgs.unsqueeze(0)
                L(f"🖼 Батч: {len(imgs)} изображений")
                for i, img in enumerate(imgs):
                    fname = f"{filename_prefix}_{ts}_{i+1:03d}.png"
                    try:
                        png = _tensor_to_png(img)
                        L(f"🖼 {fname} конвертирован в PNG ({len(png)} байт)")
                        url = _upload_bytes(svc, png, fname, folder_id)
                        L(f"✅ {fname} → {url}")
                    except Exception as e:
                        import traceback
                        err = traceback.format_exc()
                        print(f"[GDrive] UPLOAD ERROR {fname}:\n{err}")
                        L(f"❌ {fname}: {e}\n{err}")

            # ══ source_path ════════════════════════════════════════════════
            elif source_path.strip():
                # разобрать вход: JSON-список, одиночный путь или папка
                raw = source_path.strip()
                paths_to_upload = []

                # попытка распарсить как JSON
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        paths_to_upload = [Path(p) for p in parsed]
                    elif isinstance(parsed, str):
                        paths_to_upload = [Path(parsed)]
                    else:
                        paths_to_upload = [Path(raw)]
                except (json.JSONDecodeError, ValueError):
                    # не JSON — одиночный путь или папка
                    p = Path(raw)
                    if p.is_dir():
                        paths_to_upload = sorted(f for f in p.iterdir() if f.is_file())
                    else:
                        paths_to_upload = [p]

                L(f"📋 Файлов для загрузки: {len(paths_to_upload)}")

                for fp in paths_to_upload:
                    fp = Path(fp)
                    if not fp.exists():
                        L(f"❌ Файл не найден: {fp}")
                        continue
                    if not fp.is_file():
                        L(f"⚠ Пропуск (не файл): {fp}")
                        continue
                    try:
                        url = _upload_file(svc, fp, folder_id)
                        L(f"✅ {fp.name} → {url}")
                    except Exception as e:
                        import traceback
                        L(f"❌ {fp.name}: {e}\n{traceback.format_exc()}")
            else:
                L("⚠ Нет входных данных: подключи images или укажи source_path")

        except Exception as e:
            import traceback
            err = traceback.format_exc()
            print(f"[GDrive] НЕОБРАБОТАННАЯ ОШИБКА:\n{err}")
            L(f"❌ Необработанная ошибка: {e}\n{err}")

        return {"ui": {"text": ["\n".join(log)]}, "result": (new_token,)}


# ── регистрация ────────────────────────────────────────────────────────────
NODE_CLASS_MAPPINGS        = {"GoogleDriveUpload": GoogleDriveUploadNode}
NODE_DISPLAY_NAME_MAPPINGS = {"GoogleDriveUpload": "📤 Google Drive Upload"}