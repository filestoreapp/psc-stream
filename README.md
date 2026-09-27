# PSC Stream

A Windows desktop app that gives you a Netflix-style streaming page for **your own**
study videos stored on Telegram. Videos stream progressively straight from
Telegram's servers — **nothing is saved to disk** (video bytes live in memory only).

## How it works

1. You upload your study videos to your own **private Telegram channel**.
2. PSC Stream logs in to *your* Telegram account via MTProto (Telethon).
3. It scans the channel for videos and shows them as a browsable library.
4. Pressing play streams the video with HTTP Range requests (206 partial content),
   exactly like YouTube/Netflix buffering — no full download.

## Setup on Windows (do these in order)

### 1. Install Python 3.11+
Download from https://www.python.org/downloads/ and during setup **tick
"Add python.exe to PATH"**. Then copy this whole `psc-stream` folder to your laptop.

### 2. Get your Telegram API credentials
- Go to https://my.telegram.org and log in with your phone number.
- Open **API development tools** → create an app (any name, e.g. "PSC Stream").
- Copy the **api_id** (number) and **api_hash** (string). You'll paste these into
  the app's login wizard on first run. (Telegram may take a few minutes to
  activate a brand-new API id.)

### 3. Upload your study videos to a private Telegram channel
- In Telegram, create a **new private channel** (just you as the member).
- Upload your study videos there. Give each a caption — the **first line of the
  caption becomes the video title** in the app.
- If the channel is private, copy its **invite link** (`t.me/+…`): channel →
  three dots → Manage channel → Invite links. The app accepts invite links,
  `t.me/…` links, and `@username`.

### 4. Double-click `run.bat`
First run creates `.venv`, installs dependencies, and opens the PSC Stream window.
(Windows 10/11 already include WebView2, so the window works out of the box.)

### 5. First-run login wizard
Paste your **api_id / api_hash / phone number** → enter the **login code** Telegram
sends you → enter your **2FA password** if asked. Login persists (session file),
so you only do this once.

### 6. Set the channel in Settings
Settings → paste the channel username/link/invite link → **Save channel** →
**Rescan now**. Your videos appear in the Library.

### 7. (Optional) Build a real `.exe`
Run `build_exe.bat` **on the Windows machine** — PyInstaller cannot cross-compile
from Linux/macOS, so the exe must be built where it will run. The result is
`dist\PSC Stream.exe`: copy it anywhere and double-click.

## Where things are stored

All local data lives in `%LOCALAPPDATA%\PSCStream\` on Windows
(`~/.psc-stream/` on Linux):

| File | Purpose |
|---|---|
| `psc_stream_session.session` | Telegram login session (persists across restarts) |
| `settings.json` | api_id/api_hash + channel |
| `video_index.json` | cached library index (instant reopen; Rescan refreshes) |
| `thumbs/` | tiny video thumbnails only |

Video bytes are **never** written to disk — they stream through a 128 MB
in-memory LRU cache.

## Mock mode (for testing without Telegram)

Set the environment variable `PSC_STREAM_MOCK=1` before launching. The app then
serves 12 fake videos with byte-accurate fake streams and generated thumbnails —
the whole UI and Range-request plumbing can be tested with no credentials:

```bat
set PSC_STREAM_MOCK=1
".venv\Scripts\python.exe" app.py
```

In mock mode any login code with 4+ characters works; code `00000` triggers the
2FA step so you can test that screen too.

## Troubleshooting

- **"Flood wait"** — Telegram rate-limited the login attempts. Wait the shown
  number of seconds and retry.
- **Channel not found** — the account must be a *member* of the channel. For
  private channels, join via the invite link in Telegram first, or paste the
  invite link into Settings.
- **Video won't play / keeps buffering** — Telegram throttles some networks;
  pause a few seconds to let the buffer fill.
- **Blank window** — if pywebview can't start, the console prints a
  `http://127.0.0.1:PORT/` URL; open it in Chrome/Edge instead.
- **Antivirus flags the exe** — expected for PyInstaller onefile builds; it's
  your own build from this source, so allow it / add an exclusion.

## Tech notes

- Python 3.11+, stdlib `http.server` only (no FastAPI) to keep packaging simple.
- Telethon runs on a dedicated thread with its own asyncio loop; HTTP handlers
  bridge via `asyncio.run_coroutine_threadsafe`.
- Tested against telethon 1.45.0 (`connect()` is a coroutine there; byte ranges
  go through `client._iter_download(..., offset=bytes, limit=chunk_count)`).
