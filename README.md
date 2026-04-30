# 📤 ComfyUI — Google Drive Upload

Нода для загрузки изображений и файлов напрямую на Google Drive через OAuth2.

---

## Установка

```bash
# 1. Скопируй папку в custom_nodes
cp -r comfyui_gdrive_upload/ ComfyUI/custom_nodes/

# 2. Установи зависимости
pip install -r ComfyUI/custom_nodes/comfyui_gdrive_upload/requirements.txt

# 3. Перезапусти ComfyUI
```

---

## Настройка Google API (один раз)

1. Открой [console.cloud.google.com](https://console.cloud.google.com)
2. Создай проект → **APIs & Services → Enable APIs** → включи **Google Drive API**
3. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Нажми **Create** → скачай `client_secret_*.json`
4. Если проект в статусе Testing → **OAuth consent screen → Test users** → добавь свой Gmail

---

## Первая авторизация

1. Открой скачанный `client_secret_*.json` в текстовом редакторе
2. Скопируй **всё содержимое** и вставь в поле `client_secret_json` ноды
3. Запусти граф — откроется браузер, войди в Google и разреши доступ
4. Токен появится на выходе `oauth_token_out`
5. Подключи `oauth_token_out` к ноде **String** (Text Multiline или аналог)
6. Выход String ноды подключи обратно на вход `oauth_token_in`
7. Сохрани воркфлоу `Ctrl+S` — токен теперь хранится внутри воркфлоу
8. Очисти поле `client_secret_json` — оно больше не нужно

```
[String / Text node] ◄─── oauth_token_out
        │
        └──────────────► oauth_token_in  [📤 Google Drive Upload]
                                                  │
                          [Load Image] ──images───┘
```

---

## Параметры ноды

| Параметр | Тип | Описание |
|---|---|---|
| `client_secret_json` | STRING | Содержимое `client_secret_*.json`. Только для первой авторизации, потом очистить |
| `oauth_token_in` | STRING (optional) | Вход токена из String ноды. Если не подключён — нужен `client_secret_json` |
| `drive_folder_name` | STRING | Имя папки на Google Drive. Создаётся автоматически если не существует |
| `drive_parent_folder_id` | STRING | ID родительской папки (опционально). Из URL: `drive.google.com/drive/folders/`**`ВОТ_ЭТО`** |
| `images` | IMAGE (optional) | Изображения из ComfyUI. Одно или батч |
| `source_path` | STRING (optional) | Путь к локальному файлу или папке. Папка — все файлы по очереди |
| `filename_prefix` | STRING | Префикс имени файла при загрузке из `images`. Пример: `result` → `result_1700000000_001.png` |
| `oauth_token_out` | STRING (output) | Актуальный токен. Подключить к String ноде для хранения |

> Нужен хотя бы один из источников: `images` или `source_path`

---

## Использование drive_parent_folder_id

Google Drive API не позволяет сервисным аккаунтам загружать в чужое хранилище.  
При использовании OAuth2 это ограничение снято — файлы загружаются на твой личный Drive.

Если хочешь загружать в конкретную папку:
1. Открой нужную папку в Google Drive
2. Скопируй ID из URL: `https://drive.google.com/drive/folders/`**`1AbCdEfGhIjKlMn`**
3. Вставь в поле `drive_parent_folder_id`
4. Папка `drive_folder_name` будет создана внутри неё

---

## Обновление токена

Токен обновляется автоматически при каждом запуске если истёк.  
Актуальная версия выходит из `oauth_token_out` и обновляет String ноду.  
Повторная авторизация через браузер не требуется — `refresh_token` бессрочный.


