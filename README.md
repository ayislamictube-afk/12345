# AY Telegram Hosting

Render Web Service + Telegram Topic as permanent project storage.

## Supported
- Python `.py`
- Node.js `.js`

ZIP/TAR is intentionally disabled.

## 1. Telegram storage group

1. Open your Telegram supergroup.
2. Make sure Topics are enabled.
3. Open topic/message thread `178`.
4. Add the hosting bot as an administrator.
5. Give it permission to send messages/documents and delete its own messages if desired.

The storage chat ID must be the numeric ID, normally something like `-100xxxxxxxxxx`.

`https://t.me/shjskwmw73jw/178` alone is NOT the numeric chat ID.

## 2. Bot token

Do NOT put the bot token in source code.

Create a new token in BotFather if an old token was exposed.

## 3. Render Environment Variables

Set:

BOT_TOKEN=your_new_bot_token
STORAGE_CHAT_ID=-100xxxxxxxxxx
STORAGE_THREAD_ID=178
ALLOWED_USER_IDS=
MAX_FILE_MB=20
SUPERVISOR_INTERVAL=10
PORT=10000

`ALLOWED_USER_IDS` is optional. If empty, any Telegram user can upload/host. For safety, put trusted Telegram IDs there, comma-separated.

## 4. Deploy

Use Render Web Service:

Render Runtime: Docker

The included Dockerfile installs both Python 3.12 and Node.js 22.

Start command is defined by the Dockerfile.

Health:
`/health`

## 5. How storage works

Upload:
User -> Bot -> temporary Render file -> Telegram Topic -> metadata

Start:
Telegram Topic -> temporary Render runtime file -> Python/Node process

Stop/crash:
Process stops -> temporary runtime directory is deleted

Restart:
Telegram copy is downloaded again

Therefore the permanent source is not kept on a Render Persistent Disk.

## Important limitations

1. Runtime still needs temporary local space. Zero local bytes is impossible while executing a file.
2. This project uses Telegram Bot API file transfer, so practical file-size limits apply. The default is 20 MB.
3. Python dependencies can be installed with `requirements.txt` only if that file exists in the runtime project; because this version accepts single `.py`/`.js` uploads, dependency-heavy scripts should preferably use standard-library modules or packages already installed on the server.
4. Arbitrary user code executes with the same OS privileges as this Render service. Use `ALLOWED_USER_IDS` and only allow trusted users. This is NOT a hardened multi-tenant sandbox.
5. A Web Service's main purpose is HTTP. The included Flask health server keeps Render's Web Service healthy while the Telegram polling and hosting supervisor run in background threads.
6. Render Free instances can sleep/restart according to Render's current free-tier behavior. This project cannot guarantee 24/7 uptime on a sleeping/restarted free instance.

## Files

- `app.py` — complete application
- `requirements.txt` — Python dependencies
- `render.yaml` — Render deployment configuration
- `.gitignore`
