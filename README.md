# ComfyUI — Google Drive Upload Node

Нода загружает одно или несколько изображений прямо в папку на Google Drive.

---

## Установка

1. Скопируй папку `comfyui_gdrive_upload` в  
   `ComfyUI/custom_nodes/comfyui_gdrive_upload/`

2. Установи зависимости:

```bash
pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
```

3. Перезапусти ComfyUI — нода появится в категории **image → upload** под именем **📤 Google Drive Upload**.

---

## Настройка Google API

Выбери один из двух способов авторизации.

---

### Способ 1 — Service Account (рекомендуется для автоматизации)

Подходит, если нужна работа без браузера/человека.

1. Открой [console.cloud.google.com](https://console.cloud.google.com).
2. Создай проект (или выбери существующий).
3. **APIs & Services → Enable APIs** → найди и включи **Google Drive API**.
4. **APIs & Services → Credentials → Create Credentials → Service account**.
5. Задай имя, нажми **Done**.
6. В списке сервисных аккаунтов нажми на созданный → вкладка **Keys → Add Key → JSON**.
7. Скачается файл вида `my-project-XXXX.json` — это и есть ключ.
8. Открой файл в текстовом редакторе, **скопируй всё содержимое** и вставь в поле `credentials_json` ноды.
9. В поле `auth_mode` выбери **service_account**.

> ⚠️ Сервисный аккаунт работает в своём Drive-пространстве.  
> Чтобы загрузки попадали **в твой** Google Drive — открой нужную папку и выдай сервисному аккаунту доступ:  
> **Поделиться → вставь email сервисного аккаунта** (вида `name@project.iam.gserviceaccount.com`) → роль **Editor**.  
> Затем скопируй ID этой папки из URL (`https://drive.google.com/drive/folders/**ВОТ_ЭТО**`) и вставь в поле `parent_folder_id`.

---

### Способ 2 — OAuth2 (файл client_secret)

Подходит, если хочешь загружать прямо в свой Drive без расшаривания.

1. В Google Cloud Console: **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
2. Application type: **Desktop app** → придумай имя → **Create**.
3. Нажми **Download JSON** — скачается `client_secret_XXXX.json`.
4. Открой файл, **скопируй всё содержимое** и вставь в `credentials_json`.
5. В поле `auth_mode` выбери **oauth2**.
6. При первом запуске ноды откроется браузер — войди в Google и разреши доступ.  
   Токен кешируется в `comfyui_gdrive_upload/gdrive_oauth_token.json`, повторный вход не нужен.

> Если в Google Cloud Console проект ещё в стадии «Testing», добавь свой Google-аккаунт в **Test users** (OAuth consent screen → Test users).

---

## Параметры ноды

| Параметр | Описание |
|---|---|
| **images** | Вход: одно изображение или батч из IMAGE |
| **folder_name** | Имя папки на Drive. Будет создана автоматически, если не существует |
| **auth_mode** | `service_account` или `oauth2` |
| **credentials_json** | Содержимое JSON-ключа (полностью) |
| **filename_prefix** | Префикс имени файла, например `result` → `result_1700000000_001.png` |
| **parent_folder_id** | ID родительской папки на Drive (опционально) |

Нода возвращает строку с логом: ссылка на каждый загруженный файл или сообщение об ошибке.

---

## Безопасность

- **Не коммить** `credentials_json` и `gdrive_oauth_token.json` в репозиторий.
- Добавь в `.gitignore`:

```
comfyui_gdrive_upload/gdrive_oauth_token.json
```

- Service Account ключ храни в безопасном месте — он даёт доступ к Drive без пароля.
