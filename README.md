# Telegram leech bot

Lightweight, single-user bot for servers with under 1 GB of RAM. Send it a link; it uploads the file
to Telegram. Direct files, YouTube and the ~1,800 other sites yt-dlp supports.

## Install

One command, on a fresh Debian/Ubuntu server (as root):

    bash <(curl -Ls https://raw.githubusercontent.com/YOUR-USER/leechbot/main/install.sh)

It downloads the bot, installs everything (Python, ffmpeg, Deno, swap/zram, earlyoom), asks for your
Telegram credentials, starts the service, and adds a `leechbot` management command.
`curl -Ls ... | sudo bash` works too.

You'll be asked for three things, and can skip the fourth:

| Value      | Where to get it                                                         |
|------------|-------------------------------------------------------------------------|
| API ID     | https://my.telegram.org -> API development tools                        |
| API hash   | same page                                                               |
| Bot token  | @BotFather (it's checked against Telegram the moment you paste it)      |
| Your user ID | **press Enter to skip**: the installer prints a link, you tap START in Telegram, and your ID is detected (a one-time code makes sure only you can claim the bot) |

There is no way around the first three: Telegram only issues them through a web login and a chat.

Unattended (nothing to answer), e.g. from a script or a VPS "user data" box (`deploy/cloud-init.yaml`):

    API_ID=123456 API_HASH=... BOT_TOKEN=... OWNER_ID=123456789 \
      bash <(curl -Ls https://raw.githubusercontent.com/YOUR-USER/leechbot/main/install.sh) -y

`install.sh --help` lists every option (`--no-zram`, `--no-earlyoom`, `--no-swap`, `--dir`, ...).

### Publishing it (do this once, so the one-liner works for you)

1. Create a **public** GitHub repository, e.g. `you/leechbot`, and put this project in it.
2. Before the first commit, point the installer at your repository:

       sed -i 's#YOUR-USER/leechbot#you/leechbot#g' install.sh README.md deploy/cloud-init.yaml

3. Commit and push to `main`. Your install command is then:

       bash <(curl -Ls https://raw.githubusercontent.com/you/leechbot/main/install.sh)

For a tagged version use `LEECHBOT_REF=v1.0`; to pin the exact download, add `LEECHBOT_SHA256=<hash>`.
A private repository needs `LEECHBOT_URL` pointing at a tarball you can fetch with a token.

## Managing it: `leechbot`

    leechbot              menu (start/stop/restart, status, logs, settings, update, uninstall)
    leechbot status       service state, restarts, memory vs its cap, yt-dlp version
    leechbot log          live logs (Ctrl-C to leave)
    leechbot update       fetch the newest version; your settings are kept
    leechbot settings     change bot token, API credentials, owner, limits (token/API changes reset the
                          stored Telegram session so the new ones really take effect)
    leechbot ytdlp        update yt-dlp now (it also auto-updates weekly)
    leechbot uninstall

## Using it: just send a link

No commands needed.

- **Direct file link** (`.mp4`, `.zip`, `.pdf`, ..., or any URL the server says is a file): downloaded and
  uploaded straight away. Add `| new_name.ext` after the link to rename it. Files over ~2 GB are split
  into `.001`, `.002`... parts (rejoin with `cat name.* > name`).
- **YouTube / video-site link**: the bot asks yt-dlp what the link *really* offers and shows buttons for
  exactly those resolutions, with estimated sizes, plus **MP3**. Audio-only links (podcasts, SoundCloud)
  skip the buttons and go straight to MP3.
- A web page that embeds a video is detected too. Several links in one message are handled one by one.

Video is chosen at the resolution you pick, preferring H.264 + AAC so it plays inline in Telegram.
**MP3** takes the highest-bitrate source stream and encodes at 320 kbit/s (the MP3 maximum), keeping the
source's sample rate and embedding title/artist tags. MP3 is lossy: a 128-160 kbit/s YouTube source can't
gain quality, it just stops getting worse. Change it with `MP3_QUALITY` in `.env` (e.g. `192K`, or `0` for VBR V0).

Commands, optional: `/leech <url>` (force direct), `/ytdl <url> [720|audio]` (force yt-dlp),
`/cookies`, `/status`, `/cancel`.

## YouTube says "Sign in to confirm you're not a bot"

Server IPs are often flagged. Give the bot cookies from a logged-in session:

1. Open a **private/incognito window** and log in to YouTube (a throwaway account is safest; yt-dlp warns
   that accounts used with it can be banned).
2. In that same tab open `https://www.youtube.com/robots.txt`.
3. Export `youtube.com` cookies with **Get cookies.txt LOCALLY** (Chrome) or **cookies.txt** (Firefox),
   then close the private window.
4. Send the file to the bot with the caption `/cookies`.

It validates the file, stores it privately (mode 600) and deletes your upload from the chat.
`/cookies clear` removes it. If the error returns later, export a fresh file.

## Memory use and crash protection

Measured on the bot (Python 3.12):

| | RAM |
|---|---|
| Bot idle | ~67 MB |
| Bot during a 300 MB download + split + upload | ~72 MB (files are streamed to disk) |
| yt-dlp subprocess, one download | ~58 MB (freed when it exits) |
| ffmpeg (thumbnail / merge), transient | ~55 MB |

**Saving RAM**

- downloads use only the standard library (no aiohttp: -13 MB)
- yt-dlp runs as a subprocess with lazy extractors (-17 MB per download); the weekly updater regenerates them
- Pyrogram's in-RAM caches cut from 1000 to 50, one transfer at a time, `MALLOC_ARENA_MAX=2`
- **page-cache hygiene**: downloads and splits flush and drop cached data every 16 MB. A 300 MB download
  otherwise leaves ~300 MB of page cache behind (measured: ~22 MB with this), which on a small server
  makes the service's memory cap throttle everything
- compressed swap in RAM (**zram**, installer) and a disk swap file as a last resort

**Not crashing**

- yt-dlp/ffmpeg children are marked first-to-be-killed (`oom_score_adj=500`) and the bot is protected
  (`-100`, `OOMPolicy=continue`): if memory runs out, a download fails with a clear message and the bot lives
- only one heavy process at a time on servers under 1.5 GB, and a job waits until `MIN_FREE_RAM_MB`
  (default 150) is actually free before starting one
- **earlyoom** (installer) kills the right process *before* the server starts thrashing; skip with `--no-earlyoom`
- the worker is supervised and restarted if it ever dies, the queue is bounded, `Restart=always`
- if the bot is killed or restarted mid-job it tells you which link was interrupted

`/status` shows live numbers: system RAM and swap, the bot (with peak), running yt-dlp/ffmpeg, and the
service against its memory cap.

Don't run the bot with `python -OO`: it saves ~6 MB but makes every Telegram API error (including
FloodWait) crash, because Pyrogram builds error messages from docstrings.

## Operate

Everything above is in the `leechbot` menu. Under the hood it's a normal systemd service:
`journalctl -u leechbot -f` for logs, and the settings live in `/opt/leechbot/.env`.
