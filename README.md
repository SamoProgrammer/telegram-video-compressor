# Telegram Video Compressor Bot

A free Telegram bot that compresses forwarded videos with ffmpeg and sends them back. Runs on GitHub Actions: no server, no credit card.

## Is the Telegram bot-hosting news real?

Yes. Telegram launched **Telegram Serverless** in July 2026. The official docs are at <https://core.telegram.org/bots/serverless>. It runs JavaScript modules in V8 isolates on Telegram's infrastructure, gives every bot a SQLite database, and deploys with `npx tgcloud push`.

It is currently free during the launch period, according to launch coverage. Telegram has **not** published pricing, execution-time limits, memory limits, or database quotas (as of November 2026). Treat "free" as launch-period pricing, not a guarantee.

But it cannot compress video:

| Telegram Serverless | Reality |
|---|---|
| Runtime | JavaScript only, V8 isolate |
| Packages | No npm; only `sdk` and your own modules |
| Filesystem | None |
| WebAssembly / ffmpeg | Not documented or supported |
| Download | `api.getFileContent()` capped at **20 MB** |
| Upload | **50 MB** per file |
| `fetch` response | Capped at **30 MB** |
| CPU / memory / time | No published limits |

So the news is real, but it is a bot backend host, not a video-processing service. A video encoder needs a real process, a filesystem, and more than 20 MB of input.

## What this repo does

One Python bot and one GitHub Actions workflow:

1. Long-polls Telegram for messages and button presses.
2. Shows a menu: **Single video** or **Multiple videos**, plus settings.
3. Downloads a forwarded video, round video message, or video document.
4. Compresses it with ffmpeg:
   - H.264 High profile, `yuv420p`, `+faststart`
   - default 720p max (1280 px long side), 480×480 for round video notes
   - default CRF 28, `veryfast`, AAC 96 kbps
   - metadata and chapters stripped
5. Sends it back as the chosen output type (Auto keeps the input type).
6. If compression does not save at least 2%, sends the original by `file_id` (no re-upload).

In multi mode the bot queues every video, downloads it eagerly, and waits for **✅ Done**. While adding a video, if the batch would exceed the disk budget, the oldest cached file is deleted from disk and re-downloaded when its turn comes. The batch is processed sequentially with progress, cancel, retry, and a final size summary.

Settings (all available as buttons):

| Setting | Values |
|---|---|
| Resolution | Original, 2160p, 1440p, 1080p, 720p, 480p, 360p, 240p |
| Quality | High (CRF 22), Balanced (CRF 28), Small (CRF 33) |
| Output | Auto (same as input), Video, Document, Round video |

Round video output crops to a square and falls back to a regular video when the clip is longer than 60 seconds. Colored buttons use Bot API `style` (`primary`, `success`, `danger`); older clients simply render them unstyled.

Architecture:

```text
Telegram cloud Bot API  <->  GitHub Actions runner (ffmpeg, 5-hour shifts)
```

- GitHub Actions standard runners are free for **public** repositories. No payment method needed.
- Private repositories get $12/month of included compute (~2,000 Linux minutes, ~33 hours), which is not enough for 24/7.
- Telegram Bot API is free.
- No server, database, or persistent storage.

### Terms-of-use reality check

GitHub's Actions terms restrict use to the production, testing, deployment, or publication of the project in the repository, and prohibit activity that puts a disproportionate burden on their servers. A personal, low-volume bot is a low-burden deployment; a public bot serving many users 24/7 is not what Actions is for. GitHub can terminate jobs, disable the repository, or restrict the account. For a public service, run the bot on a machine you control (local Bot API mode below) or a host that permits bots.

## Quick start

1. Message [@BotFather](https://t.me/BotFather), run `/newbot`, and copy the token. Keep privacy mode enabled (the default); the bot only needs direct messages.
2. Create a **public** GitHub repository and push this folder to the default branch (usually `main`).
3. Repository **Settings → Secrets and variables → Actions → New repository secret**:
   - `BOT_TOKEN` = your bot token.
4. Optional but strongly recommended: add repository **variables**:
   - `ALLOWED_USER_IDS` = comma-separated numeric Telegram user IDs.
   - `ALLOWED_USERNAMES` = comma-separated usernames (with or without `@`, case-insensitive).
   - Get your ID from [@userinfobot](https://t.me/userinfobot), or leave both empty for the first run and send `/id` to the bot.
   - If both are empty, anyone can use your bot and burn your runner time. Prefer IDs: usernames can be changed or transferred; IDs are permanent. A denied user is told their own ID so you can allow-list them.
5. Open the **Actions** tab, enable workflows, select `telegram-video-compressor`, and click **Run workflow**.
6. Open the bot and send `/start`; tap **Single video** or **Multiple videos**.

## Using the bot

- **Single video** — send one video and get the compressed result with another-video/settings/menu buttons.
- **Multiple videos** — send all videos first. Each one is downloaded into the batch. Tap **✅ Done** (or send `done`) to start; the bot processes them one by one and posts a summary with total savings.
- **Settings** — resolution, quality, and output menus. The current value is marked ✅.
- **Cancel** — the red cancel button during a batch stops after the current video and keeps the rest in the queue. `Clear` deletes the queue.
- **Retry** — failed videos stay in the queue; tap **✅ Done** again to retry them.
- **Disk budget** — `DISK_BUDGET_MB` (default 2048) caps the batch cache. When a new video does not fit, the oldest cached file is deleted and downloaded again when its turn comes.
- **Stale menus** — old buttons answer with "This menu is outdated" instead of doing the wrong thing.

Batch queues live in the runner's memory, so finish a batch within one shift. If the shift ends mid-batch, the videos already confirmed to the bot must be sent again after the next shift starts. Single videos that arrive too late are left unconfirmed and Telegram redelivers them next shift.

### Shift schedule

The workflow runs every 5 hours (`7 */5 * * *`, minute 7 to avoid the top-of-the-hour scheduling rush). Each job works for 5 h 20 min, leaving a buffer under the workflow's 350-minute timeout. The next run is queued before the current one ends, so the handoff is usually a few seconds.

If a single video arrives too close to the end of a shift, the bot replies "Not enough time left in this shift" and leaves the update unconfirmed. Telegram keeps updates for 24 hours, so the next shift picks it up.

### GitHub Actions limits

- Public-repo standard runner: 4 vCPU, 16 GB RAM, 14 GB SSD, free and unlimited (subject to the terms above).
- Job timeout: 6 hours; this workflow stops the bot at 5 h 20 min.
- Queued run: discarded if GitHub does not pick it up within 45 minutes.
- Scheduled runs are best-effort, can be delayed at busy times, and are disabled after 60 days without a commit.
- Free plan: 20 concurrent jobs; this workflow uses one.
- Private repo: $12/month of included compute (~2,000 Linux minutes), then usage is blocked without a payment method.

## Limits

| Limit | Value | Why |
|---|---|---|
| Input size | 20 MB | Cloud Bot API `getFile` cap |
| Output size | 50 MB (`MAX_OUTPUT_MB`) | Cloud Bot API upload cap |
| Duration | 1 hour | Keeps a shift from overrunning |
| Batch cache | 2 GB (`DISK_BUDGET_MB`) | Oldest cached file is evicted first |
| Work shift | 5 h 20 min | GitHub Actions 6-hour job cap |
| Update retention | 24 hours | Telegram retention |

Round video messages ("video messages") are short and usually well under 20 MB, so they work. A 4K 10-minute video will not.

## Large files: local Bot API mode

The 20 MB cap is a **cloud Bot API** limit, not a Telegram account limit. The official [`telegram-bot-api`](https://github.com/tdlib/telegram-bot-api) server in `--local` mode removes the download cap and allows uploads up to 2000 MB. The bot already supports it through `BOT_API_URL`.

Run it on any always-on machine you already own (home PC, Raspberry Pi, old laptop):

```bash
# Build the official server once
git clone --recursive https://github.com/tdlib/telegram-bot-api.git
cd telegram-bot-api
mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release ..
cmake --build . --target install

# Move the bot from the cloud server to the local one (once)
curl "https://api.telegram.org/bot<YOUR_TOKEN>/logOut"

# Start the local server
mkdir -p "$HOME/.telegram-bot-api"
telegram-bot-api --local --api-id=<API_ID> --api-hash=<API_HASH> \
  --http-port=8081 --dir="$HOME/.telegram-bot-api"

# Start the bot against it
BOT_TOKEN=<YOUR_TOKEN> BOT_API_URL=http://127.0.0.1:8081 \
  MAX_INPUT_MB=2000 MAX_OUTPUT_MB=2000 MAX_DURATION=7200 python bot.py
```

For a quick local test, add `BOT_RUN_SECONDS=120` so the bot exits after two minutes. To move the bot back to the cloud Bot API later, call `curl "http://127.0.0.1:8081/bot<YOUR_TOKEN>/logOut"` before stopping the local server.

Get `api_id` and `api_hash` free at <https://my.telegram.org>. This is the clean upgrade path: it is not serverless, but it is free if you already own the machine. The included `Dockerfile` can run the bot side. The local server stores downloaded files under `--dir`; clean that directory periodically.

Do **not** use a user-account (MTProto/Telethon) session to bypass the bot limit. It works technically, but automating a user account is a ToS gray area and can get the account limited. The local Bot API server is the supported way.

## Why H.264 and not AV1/HEVC?

- H.264 High/`yuv420p` is decoded by every Telegram client and every phone.
- HEVC and AV1 give 20–50% smaller files at the same quality, but Telegram playback support is inconsistent and CPU encoding is much slower on a free runner.
- For a bot that must "just play", H.264 is the correct default. If you only deliver files as documents, AV1/HEVC are worth testing, but you must change the encoder, preset, and profile flags together.
- Telegram may re-encode an uploaded video again on its servers. If you need the exact compressed file, send it as a document (the bot does this when the input was a document).

## Other workarounds considered

| Option | Free without a card? | Verdict |
|---|---|---|
| Telegram Serverless | Yes (launch period) | No ffmpeg/WASM, 20 MB download, no filesystem. Good for a webhook/queue, not encoding. |
| GitHub Actions + ffmpeg | Yes, public repo | **Chosen for the free demo.** Real CPU/RAM, ffmpeg, 6 h per job; hobby-scale only (see terms). |
| Hugging Face Spaces | CPU Basic is free; Docker Space hosting is a PRO feature | Free Spaces sleep/restart and are built for demos, not always-on polling. |
| Cloudflare Workers + ffmpeg.wasm | Yes | 128 MB memory, no threads, 10 ms CPU on free. Not viable for video. |
| Vercel Hobby + ffmpeg-static | Yes | Possible for very short clips, but a short function timeout and ~1 GB RAM; cold starts. |
| Modal free credits | Usually no card, but finite credits | Good alternative if you want a real webhook + containers. |
| Oracle/GCP/AWS/Azure free tier | Card required | Excluded. |
| Kaggle/Colab GPU | No card | 12 h sessions, manual restart; not an always-on bot. |
| User-account downloader | No card | ToS risk; not included. |

### Hybrid option: event-driven instead of polling

The included workflow polls in shifts. If you want an instant response, put a small webhook in front:

```text
Telegram webhook -> Cloudflare Worker (free) -> repository_dispatch -> GitHub Actions
```

The Worker answers the user immediately, then triggers one Actions run per video. Add `repository_dispatch` to the workflow's `on:` block and make that run process a single update, then exit. It needs a fine-grained GitHub token with `actions:write` and the bot token as Worker secrets. This removes the shift gaps, but still hits the 20 MB cloud download cap, and each video pays a ~20–60 s runner cold start. Telegram Serverless can also receive the webhook, but it has no documented secure secret storage, so a Worker is the safer front door.

## Security notes

- The repository must be public for free unlimited Actions minutes (private repos are capped at ~$12/month of compute). A public repo is not a security hole here: the token lives in a secret, and only `ALLOWED_USER_IDS` / `ALLOWED_USERNAMES` users can talk to the bot. IDs are strict; usernames are a convenience.
- Set `ALLOWED_USER_IDS`; otherwise anyone can use your bot.
- The runner is ephemeral. Cloud-downloaded videos are deleted when processed or at the end of the shift. A local Bot API server manages its own `--dir`.
- This is hobby-scale infrastructure. See the terms-of-use reality check above; don't run a public high-volume service on GitHub Actions.
- ffmpeg parses untrusted media. Keep the runner image updated, and keep the 20 MB / 1 hour caps.
- The bot redacts the token from errors. GitHub also masks repository secrets in logs.

## Files

- `bot.py` — the bot, menus, batch queue, and ffmpeg pipeline.
- `test_bot.py` — unit tests plus a real ffmpeg smoke test (`python -m unittest -v`).
- `.github/workflows/bot.yml` — free 5-hour shifts.
- `Dockerfile` — optional local/VPS run.
- `requirements.txt` — `requests`.

## Troubleshooting

- **Bot does not answer:** check the Actions run; make sure the workflow is enabled and `BOT_TOKEN` is set. Run it manually once.
- **`Conflict: terminated by other getUpdates request`:** two runs overlapped. The concurrency group prevents this; wait for the old run to finish.
- **"file is too big":** the video is over 20 MB. Use a round video message, a shorter clip, or local Bot API mode.
- **Buttons look plain:** colored buttons need a current Telegram client. Older clients ignore `style`; the buttons still work.
- **My batch disappeared after a shift:** batch queues are in runner memory. Send the videos again after the next shift starts.
- **Round video output arrived as a regular video:** clips longer than 60 seconds cannot be video notes, so the bot falls back.
- **Scheduled workflow stopped after 60 days:** GitHub disables scheduled workflows in inactive public repositories. Re-enable it in the Actions tab and push a commit; GitHub counts commits, not scheduled runs, as activity.
- **Private repository:** included compute is $12/month (~33 Linux hours); 24/7 shifts will hit the limit. Make the repository public.

## Sources

- Telegram Serverless: <https://core.telegram.org/bots/serverless> (launched July 2026; docs updated 6 October 2026)
- Launch coverage: <https://habr.com/ru/articles/1059556/> (15 July 2026) and <https://www.techtimes.com/articles/320758/20260716/telegram-serverless-ships-one-deploy-command-your-bot-data-never-leaves-telegram.htm> (16 July 2026)
- Bot API file limits: <https://core.telegram.org/bots/api#sending-files> and <https://core.telegram.org/bots/api/methods/getfile>
- Colored inline buttons: <https://core.telegram.org/bots/api/types/inlinekeyboardbutton>
- Video note limits: <https://core.telegram.org/bots/api/methods/sendvideonote>
- Local Bot API server: <https://github.com/tdlib/telegram-bot-api>
- GitHub Actions billing: <https://docs.github.com/en/billing/concepts/product-billing/github-actions>
- GitHub Actions limits: <https://docs.github.com/en/actions/reference/limits>
- GitHub Actions terms (purpose restrictions): <https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions>
