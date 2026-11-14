# Telegram Video Compressor Bot

A free Telegram bot that compresses forwarded videos with ffmpeg and sends
them back. It runs in 5-hour shifts on GitHub Actions: no server, no database,
no credit card.

```text
Telegram Bot API  <->  GitHub Actions runner (ffmpeg, 5-hour shifts)
```

## What it does

- Send one video (single mode) or queue many (batch mode).
- Downloads video messages, round video notes, animations, and video documents.
- Multi-codec compression: **H.264** (universal), **H.265/HEVC** (-35% size, iOS `-tag:v hvc1`), **AV1** (-50% size via SVT-AV1), and **VP9**.
- Dual rate control: **CRF (Quality)** and **2-Pass Target Size** (10 MB, 20 MB, 50 MB, 50%, 25%).
- Fine-grained controls: resolution downscaling (2160p to 240p), framerate capping (30fps / 24fps), smart audio pass-through/mono/mute, and HDR-to-SDR tonemapping.
- Sends the result back as the same kind of message (Video, Document, or Round
  video) or as an explicit output type.
- If compression saves less than 2%, sends the original by `file_id` instead.
- Persistent bottom reply keyboard (`[🎬 Single] [📚 Batch] [⚙️ Settings] [ℹ️ Status]`) and smooth in-place card editing with breadcrumbs.
- Settings, mode, and the batch queue survive shift rollovers.

## Quick start

1. Message [@BotFather](https://t.me/BotFather), run `/newbot`, copy the token.
2. Create a **public** GitHub repository and push this folder to the default
   branch.
3. **Settings → Secrets and variables → Actions → New repository secret**:
   - `BOT_TOKEN` = your bot token.
   - `BOT_STATE_ENCRYPTION_KEY` = a private Fernet key used to encrypt both bot
     state and the MTProto session before they enter the Actions cache. Generate
     one locally and paste only the printed value into the secret:

     ```bash
     python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
     ```

   Keep a safe backup; losing the key makes existing encrypted caches unrestorable.
4. Strongly recommended repository **variables**:
   - `ALLOWED_USER_IDS` = comma-separated numeric Telegram IDs.
   - `ALLOWED_USERNAMES` = comma-separated usernames (`@` optional).
   - Leave both empty only for a first test; then anyone can use the bot and
     burn your runner time. Send `/id` to the bot to learn your ID.
5. **Actions → telegram-video-compressor → Run workflow.**
6. Open the bot, send `/start`, and pick 🎬 Single or 📚 Multiple.

Public repositories get free standard runners. Private repositories include
about $12/month of compute (~33 Linux hours), which is not enough for 24/7.

## Using the bot

- **Single video** — send one video; progress and the result appear on the card.
- **Multiple videos** — enable 📚 Batch, send videos (each becomes a row), then
  tap ▶️ Start or send `done`.
- **Settings** — ⚙️ Codec, Mode, Quality/Size, Resolution, FPS, Audio, Output, and Tuning. Current values are marked ✅.
- **Cancel** — ✖️ immediately terminates that conversation's active FFmpeg process; unfinished videos return to the queue without killing other chats.
- **Retry** — failed rows stay queued; ▶️ Start retries them.
- **Clear** — 🗑 prompts confirmation before removing queued videos and cached files.

| Setting | Values | Notes |
|---|---|---|
| Codec | H.264, H.265 (HEVC), AV1 (SVT), VP9 | HEVC uses `-tag:v hvc1`; AV1 uses SVT-AV1 preset 8 |
| Mode | Quality (CRF), Target File Size | CRF targets perceptual quality; Target Size computes 2-pass bitrate budget |
| Quality / Size | CRF (18, 22, 26, 30, 34) or Target Size (10MB, 20MB, 50MB, 50%, 25%) | 10MB fits Discord/WhatsApp; 20MB fits Bot API download cap; 50MB fits cloud upload |
| Resolution | Original, 2160p, 1440p, 1080p, 720p, 480p, 360p, 240p | Long-side bounding box, bicubic scaling |
| Framerate | Original, 30 fps, 24 fps | Capping 60fps/120fps video cuts 30–45% file size |
| Audio | Auto, Pass-through, AAC 96k, Opus 64k, Mono, Mute | Auto stream-copies AAC $\le 128$k; Mute drops audio (`-an`) |
| Output | Auto (same as input), Video, Document, Round video | Round video notes cropped 1:1 square, capped at 640 px |
| Advanced | HDR Tonemap Auto/Off, Tune (None, Film, Animation) | HDR Auto converts detected 10-bit BT.2020; Off preserves the source. Film applies to H.264; Animation applies to H.264/H.265. Unsupported combinations are omitted and reported |

Round video notes are cropped square and capped at 640 px. Clips longer than
60 seconds fall back to a regular video. Queued videos use a JIT double-buffering
pipeline (video $N$ encodes while $N+1$ downloads and $N-1$ uploads). The batch
worker accounts for actual input and output bytes against `DISK_BUDGET_MB`,
preserves 1 GiB of free disk space, and joins transfer workers before cleanup.

## Limits

| Limit | Default | Notes |
|---|---|---|
| Input | 20 MB | ~1900 MB with MTProto credentials |
| Output | 50 MB cloud API (49 MiB routing headroom) | ~1900 MB with MTProto credentials; explicit size rejection falls back to MTProto |
| Duration | 60 min | Guards a shift against overruns |
| Batch cache | 2 GB | `DISK_BUDGET_MB` |
| Shift | 5 h 20 min | Workflow timeout is 350 min |
| Update retention | 24 h | Telegram keeps unconfirmed updates |

## Big files

The 20 MB download / 50 MB upload caps belong to Telegram's cloud HTTP Bot API,
not to Telegram itself. Uploads are selected by actual output bytes, with 49 MiB
of routing headroom. The log records `size_bytes`, route, API mode and limits.
An explicit `Request Entity Too Large` response retries the same file once via
MTProto when credentials are available. Without MTProto or a local Bot API
server, the bot returns an actionable limit error instead of silently reducing
quality or repeatedly throwing the same oversized request.
Two supported ways around them:

### MTProto with the bot token (recommended for Actions)

Set two repository secrets from [my.telegram.org](https://my.telegram.org):

- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`

The bot then downloads files over 20 MB and uploads results over 50 MB through
MTProto, signed in as the bot itself. Cap is about 1900 MB each way. No user
account is involved. The session file is encrypted before being cached between
shifts; the runner temp directory is wiped at shift end. `cryptg` makes AES
decryption faster.

Trade-off: `api_id`/`api_hash` belong to your Telegram account. Using them for
your own bot is the documented Telethon path, but keep volume sane.

### Local Bot API server (always-on machine)

The official [`telegram-bot-api`](https://github.com/tdlib/telegram-bot-api)
server in `--local` mode removes the download cap and allows uploads up to
2000 MB. The bot already speaks to it through `BOT_API_URL`.

```bash
git clone --recursive https://github.com/tdlib/telegram-bot-api.git
cd telegram-bot-api && mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release .. && cmake --build . --target install

# Move the bot off the cloud API once
curl "https://api.telegram.org/bot<TOKEN>/logOut"

mkdir -p "$HOME/.telegram-bot-api"
telegram-bot-api --local --api-id=<API_ID> --api-hash=<API_HASH> \
  --http-port=8081 --dir="$HOME/.telegram-bot-api"

BOT_TOKEN=<TOKEN> BOT_API_URL=http://127.0.0.1:8081 \
  MAX_INPUT_MB=2000 MAX_OUTPUT_MB=2000 MAX_DURATION=7200 python bot.py
```

To return to the cloud API, call `logOut` on the local server before stopping
it. Keep the local server's `--dir` clean.

## How it works

`bot.py` is only the entry point. The application lives in `tgcompress/`,
layered so the Telegram transport, ffmpeg pipeline, and UI can be tested
separately.

| Module | Responsibility |
|---|---|
| `settings.py` | Shared defaults, UI option IDs, and saved-setting migration |
| `config.py` | Environment parsing, limits, allow-list, paths |
| `errors.py` | `BotError` (user-facing) and `Defer` (shift too short) |
| `telegram_api.py` | Bot API HTTP client, streaming download, upload metering |
| `mtproto.py` | Telethon download/upload above the cloud API caps |
| `media.py` | ffprobe, presets, filters, ffmpeg encode |
| `transfer.py` | Chooses Bot API or MTProto for each download/upload |
| `queue.py` | Eager batch downloads and disk-budget eviction |
| `files.py` | `safe_unlink`: deletes only inside the work directory |
| `state.py` | Per-conversation state, receipts, pending updates, and JSON persistence |
| `ui.py` | Card text, keyboards, throttled repaints |
| `pipeline.py` | One video: download → probe → compress → send |
| `batch.py` | Batch worker, disk accounting, cancel/defer, summary |
| `handlers.py` | Message/callback routing, duplicate suppression, deferral |
| `app.py` | Wiring, polling, startup recovery, and shift shutdown |

A video flows through: handler → `Pipeline.compress_one` → `Transfer` (download)
→ `media.compress` (ffmpeg) → `Transfer.send_result` (Bot API or MTProto).
Only one video encodes at a time; a batch owns a worker thread. Cancellation and
delivery receipts are scoped to `(chat_id, user_id)`.

### Shift handoff

Each job runs for 5 h 20 min, leaving a buffer under the workflow's 350-minute
timeout. Before it exits, the shift dispatches the next run through the GitHub
API using its own `GITHUB_TOKEN`, so the gap is one runner startup rather than
hours. Self-dispatch is the primary scheduler. A separate `7 */3 * * *`
watchdog workflow checks for `in_progress`, `queued`, `waiting`, `requested`, or
`pending` bot runs and dispatches one only when none exists. It only calls the
GitHub API; it does not install ffmpeg or run the bot.

If a video arrives too close to the end of a shift, the bot records it as a
resumable/pending job and acknowledges the update only after that record is
durable. A new shift resumes started work automatically. Queues collected but
not submitted remain idle until the user starts them. Successful delivery is
also recorded, capped to the latest 100 message IDs per conversation, so a
repeated update does not create a second send. Telegram's update retention is
still only 24 hours.

Settings and queues are saved to `bot-state.json` and carried between shifts
through the Actions cache. On Actions, plaintext state and the MTProto session
live only in `/tmp/tg-private`; `scripts/secure_state.py restore` and `save`
decrypt and encrypt them with authenticated Fernet encryption. Only
`/tmp/tg-cache/bot-state.json.enc` and
`/tmp/tg-cache/bot-mtproto.session.enc` are cached. A wrong key or damaged
ciphertext stops restoration instead of replacing existing state with an empty
file. Telegram `file_id`s expire after 24 hours; expired items fail per-row and
can be resent.

## Development

```bash
pip install -r requirements.txt
python -m unittest -v          # unit tests + real ffmpeg smoke tests
BOT_TOKEN=<token> BOT_RUN_SECONDS=120 BOT_RESERVE_SECONDS=10 python bot.py
```

`BOT_RUN_SECONDS` makes a local run exit after two minutes. `DISK_BUDGET_MB`,
`MAX_INPUT_MB`, `MAX_OUTPUT_MB`, and `MAX_DURATION` are available for local or
VPS tuning. `scripts/mt_check.py` verifies the MTProto credentials in one shot.

The included `Dockerfile` runs the bot side on a VPS:

```bash
docker build -t tg-compress .
docker run --rm -e BOT_TOKEN=<token> -v /tmp/tg-state:/state \
  -e BOT_STATE_FILE=/state/bot-state.json tg-compress
```

### Security

- Set `ALLOWED_USER_IDS`; an open bot is an open compute faucet.
- IDs are permanent; usernames can be renamed, so IDs are the stronger control.
  Username-only allow-lists still work, but IDs are preferred.
- The token lives in a repository secret and is redacted from errors.
- GitHub warns that [cache contents may be exposed to anyone with read access
  to a public repository through a pull
  request](https://docs.github.com/en/actions/using-workflows/caching-dependencies-to-speed-up-workflows).
  Keep `BOT_STATE_ENCRYPTION_KEY` secret; only authenticated ciphertext belongs
  in the bot-state cache.
- **Migration:** the `bot-state-encrypted-v1-` prefix deliberately avoids old
  plaintext `bot-state-` caches. Review and delete those old cache entries
  yourself under **Actions → Caches**; they are not deleted automatically. The
  first encrypted run starts without the previously cached state/session.
- The runner is ephemeral. Cloud downloads are deleted after processing or when
  the shift ends.
- ffmpeg parses untrusted media, so keep the runner image current and keep the
  size and duration caps.
- This is hobby-scale infrastructure. GitHub's terms restrict Actions to the
  project's own build/test/deploy work and prohibit disproportionate load; a
  public high-volume bot belongs on a machine you control, not on Actions.

### Troubleshooting

- **Bot does not answer** — check the Actions run, confirm the workflow is
  enabled and `BOT_TOKEN` exists, then run it manually once.
- **`Conflict: terminated by other getUpdates request`** — two shifts overlap;
  the concurrency group prevents this, wait for the old run to finish.
- **"Request Entity Too Large"** — the bot logs the selected route and retries an explicit cloud rejection through MTProto. For cloud-only installations, add MTProto credentials, use a local Bot API server (with `MAX_OUTPUT_MB` raised appropriately), or choose a smaller target/resolution.
- **"file is too big"** — the input exceeds 20 MB. Add MTProto credentials, use
  a shorter clip, or switch to local Bot API mode.
- **Batch disappeared** — settings/queue persist, but `file_id`s expire after
  24 hours. Resend expired videos.
- **Round video arrived as a regular video** — the clip is over 60 seconds.
- **Private repo runs out of minutes** — make it public or use a local server.
- **Scheduled workflow stopped after 60 days** — GitHub disables schedules in
  inactive public repositories. Re-enable it and push a commit.

## Design notes

- **H.264 by default.** Every Telegram client and phone decodes H.264.
  HEVC/AV1/VP9 remain available as optional codecs and can save 20–50%, but
  playback support and CPU encoding speed vary by device and runner.
- **Send as Document for the exact file.** Telegram may re-encode uploaded
  videos. Documents bypass that, and the bot preserves the input type by default.
- **Why not Telegram Serverless?** It runs JavaScript only, has no filesystem,
  caps `getFileContent` at 20 MB, and documents no WebAssembly/ffmpeg support.
  It is a bot backend, not a video encoder.
- **Alternatives considered.** A webhook in front of Actions removes shift gaps
  but still hits the 20 MB cap and pays a runner cold start per video. Splitting
  a source into keyframe parts is fully private but requires a manual join step.
  User-account (userbot) MTProto is a ToS gray area and not included.

## Sources

- Bot API file limits: <https://core.telegram.org/bots/api#sending-files>
- `getFile`: <https://core.telegram.org/bots/api/methods/getfile>
- Video notes: <https://core.telegram.org/bots/api/methods/sendvideonote>
- Local Bot API server: <https://github.com/tdlib/telegram-bot-api>
- Telethon: <https://docs.telethon.dev/>
- GitHub Actions limits: <https://docs.github.com/en/actions/reference/limits>
- GitHub Actions terms: <https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions>
