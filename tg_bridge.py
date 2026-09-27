"""
Telegram (MTProto) bridge for PSC Stream.

Runs Telethon on a dedicated thread with its own asyncio event loop. The
synchronous HTTP server talks to it through `run_coroutine_threadsafe`, so the
request handlers never touch asyncio directly.

All Telethon call signatures below were verified against the installed
telethon 1.45.0 (see build notes). Important version facts:

  * `TelegramClient.connect()` is a *coroutine* in 1.45 -> `await client.connect()`
  * `disconnect()` is sync, but returns an awaitable when the loop is running.
  * `download_file()` has NO offset/limit params -> byte ranges are fetched via
    the lower-level `client._iter_download(doc, offset=<bytes>,
    limit=<chunk count>, chunk_size=BLOCK, request_size=BLOCK)` async iterator.
  * `iter_messages()` is a plain (sync) method returning an async iterator.
  * `download_media(msg, file=bytes, thumb=0)` returns the smallest thumb bytes.
  * `sign_in(phone, code)` for the SMS code step, `sign_in(password=...)` for 2FA.
"""

import asyncio
import inspect
import re
import threading

from telethon import TelegramClient, errors
from telethon.tl import types

import mock_data

# Size of one Telegram file chunk used for range fetches. Must be a multiple
# of 4096 and at most 512 KiB (Telegram's MAX_CHUNK_SIZE).
BLOCK_SIZE = 512 * 1024


class BridgeError(Exception):
    """Error with a machine-readable `code` the UI can translate."""

    def __init__(self, code, message, extra=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra or {}


def _map_error(exc):
    """Translate telethon/network exceptions into BridgeError."""
    if isinstance(exc, BridgeError):
        return exc
    if isinstance(exc, errors.FloodWaitError):
        secs = getattr(exc, "seconds", 60)
        return BridgeError(
            "flood_wait",
            "Too many attempts. Please wait %d seconds and try again." % secs,
            {"seconds": secs},
        )
    if isinstance(exc, errors.PhoneNumberInvalidError):
        return BridgeError(
            "phone_invalid",
            "That phone number looks invalid. Use international format, e.g. +919876543210.",
        )
    if isinstance(exc, errors.PhoneCodeInvalidError):
        return BridgeError(
            "code_invalid", "Wrong code. Check the code Telegram sent and try again."
        )
    if isinstance(exc, errors.PhoneCodeExpiredError):
        return BridgeError(
            "code_expired", "That code has expired. Go back and request a new one."
        )
    if isinstance(exc, errors.PasswordHashInvalidError):
        return BridgeError("password_invalid", "Wrong 2FA password. Try again.")
    if isinstance(exc, errors.SessionPasswordNeededError):
        # handled by the caller as a flow step, but map it anyway
        return BridgeError("password_needed", "Two-step verification password required.")
    if isinstance(
        exc,
        (
            errors.ChannelPrivateError,
            errors.UsernameInvalidError,
            errors.UsernameNotOccupiedError,
            errors.InviteHashInvalidError,
            errors.InviteHashExpiredError,
        ),
    ):
        return BridgeError(
            "channel_not_found",
            "Could not find or access that channel. Check the username/link, "
            "or make sure this Telegram account is a member of it.",
        )
    if isinstance(exc, asyncio.TimeoutError):
        return BridgeError("network", "Timed out talking to Telegram. Try again.")
    if isinstance(exc, (ConnectionError, OSError, asyncio.IncompleteReadError)):
        return BridgeError(
            "network", "Network error talking to Telegram. Check your connection."
        )
    return BridgeError("unknown", "%s: %s" % (type(exc).__name__, exc))


class TelegramBridge:
    """Real MTProto backend. All public methods are synchronous (blocking)."""

    def __init__(self, session_path):
        # session_path: full path WITHOUT the .session suffix; telethon appends it,
        # producing e.g. ".../psc_stream_session.session".
        self._session_path = str(session_path)
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._client = None
        self._creds = None  # (api_id, api_hash) the live client was built with
        self._phone = None  # phone used for the pending login-code step
        self._channel_input = None  # raw channel string from settings
        self._entity = None  # resolved channel entity (in-memory cache)
        self._docs = {}  # msg_id -> telethon Document (for range fetches)
        self._meta = {}  # msg_id -> metadata dict
        self._thread = threading.Thread(
            target=self._run_loop, name="telethon-loop", daemon=True
        )
        self._thread.start()

    # -- loop plumbing -----------------------------------------------------
    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def _call(self, coro_func, *args, timeout=90):
        """Run an async function on the telethon thread, block for the result."""
        self._ready.wait(10)

        async def _wrapper():
            return await coro_func(*args)

        try:
            future = asyncio.run_coroutine_threadsafe(_wrapper(), self._loop)
            return future.result(timeout=timeout)
        except BridgeError:
            raise
        except Exception as exc:  # includes concurrent.futures.TimeoutError
            raise _map_error(exc)

    # -- internal async helpers (run on the telethon thread) ---------------
    async def _ensure_client(self, api_id, api_hash):
        if self._client is None or self._creds != (api_id, api_hash):
            if self._client is not None:
                try:
                    disc = self._client.disconnect()
                    if inspect.isawaitable(disc):
                        await disc
                except Exception:
                    pass
            # Created on the loop thread so client.loop is our dedicated loop.
            self._client = TelegramClient(self._session_path, api_id, api_hash)
            self._creds = (api_id, api_hash)
            self._entity = None
        if not self._client.is_connected():
            await self._client.connect()

    async def _ensure_entity(self):
        if self._entity is not None:
            return self._entity
        if not self._channel_input:
            raise BridgeError(
                "channel_not_set", "No channel configured. Set one in Settings first."
            )
        self._entity = await self._resolve_channel(self._channel_input)
        return self._entity

    async def _resolve_channel(self, raw):
        """Accept @username, t.me links, or private invite links."""
        s = raw.strip()
        # Private invite link: t.me/+HASH, t.me/joinchat/HASH, or bare +HASH
        m = re.search(r"(?:t\.me/(?:\+|joinchat/))([A-Za-z0-9_-]+)", s)
        if not m and s.startswith("+"):
            m = re.match(r"\+([A-Za-z0-9_-]+)$", s)
        if m:
            invite_hash = m.group(1)
            try:
                from telethon.tl.functions.messages import ImportChatInviteRequest
            except ImportError:
                raise BridgeError(
                    "channel_not_found", "Invite links are not supported by this build."
                )
            try:
                # Join via the invite (raises if already a member -> handled below).
                result = await self._client(ImportChatInviteRequest(invite_hash))
                for chat in getattr(result, "chats", []):
                    return chat
            except errors.UserAlreadyParticipantError:
                # Already a member: ask Telegram which chat this invite is for.
                from telethon.tl.functions.messages import CheckChatInviteRequest

                invite = await self._client(CheckChatInviteRequest(invite_hash))
                chat = getattr(invite, "chat", None)
                if chat is not None:
                    return chat
            # Join returned nothing usable and we couldn't resolve the chat.
            raise BridgeError(
                "channel_not_found",
                "Could not open that invite link. Open the channel once in "
                "Telegram, then use its @username here if it has one.",
            )
        # Public username or t.me/<username> link: telethon resolves both.
        if s.startswith("@"):
            s = s[1:]
        try:
            return await self._client.get_entity(s)
        except Exception as exc:
            raise _map_error(exc)

    def _meta_from_msg(self, msg):
        media = getattr(msg, "media", None)
        if not isinstance(media, types.MessageMediaDocument):
            return None
        doc = media.document
        if not isinstance(doc, types.Document):
            return None
        self._docs[msg.id] = doc
        # Title: first line of the caption, else the file name, else fallback.
        title = (getattr(msg, "message", None) or "").strip().split("\n")[0].strip()
        if not title:
            for attr in doc.attributes:
                if isinstance(attr, types.DocumentAttributeFilename):
                    title = attr.file_name
                    break
        if not title:
            title = "Video %d" % msg.id
        duration = 0
        for attr in doc.attributes:
            if isinstance(attr, types.DocumentAttributeVideo):
                duration = int(attr.duration or 0)
                break
        meta = {
            "id": msg.id,
            "title": title[:200],
            "duration": duration,
            "size": doc.size,
            "mime_type": doc.mime_type or "video/mp4",
            "has_thumb": bool(getattr(doc, "thumbs", None)),
            "date": msg.date.strftime("%Y-%m-%d") if getattr(msg, "date", None) else "",
        }
        self._meta[msg.id] = meta
        return meta

    # -- auth ---------------------------------------------------------------
    async def _auth_start(self, api_id, api_hash, phone):
        await self._ensure_client(api_id, api_hash)
        if await self._client.is_user_authorized():
            return {"next": "done"}
        try:
            await self._client.send_code_request(phone)
        except Exception as exc:
            raise _map_error(exc)
        self._phone = phone
        return {"next": "code"}

    async def _auth_code(self, code):
        if not self._phone:
            raise BridgeError(
                "no_pending_login", "No login in progress. Start again from step 1."
            )
        try:
            await self._client.sign_in(self._phone, code)
        except errors.SessionPasswordNeededError:
            return {"next": "password"}
        except Exception as exc:
            raise _map_error(exc)
        self._phone = None
        return {"next": "done"}

    async def _auth_password(self, password):
        try:
            await self._client.sign_in(password=password)
        except Exception as exc:
            raise _map_error(exc)
        self._phone = None
        return {"next": "done"}

    async def _is_authorized(self):
        if self._client is None:
            return False
        try:
            await self._ensure_client(*self._creds)
            return await self._client.is_user_authorized()
        except Exception:
            return False

    async def _logout(self):
        if self._client is not None:
            try:
                await self._client.log_out()
            except Exception:
                pass
            try:
                disc = self._client.disconnect()
                if inspect.isawaitable(disc):
                    await disc
            except Exception:
                pass
        self._client = None
        self._creds = None
        self._phone = None
        self._entity = None
        self._docs.clear()
        self._meta.clear()

    # -- library ------------------------------------------------------------
    async def _set_channel(self, channel_input):
        self._channel_input = channel_input.strip()
        self._entity = None
        entity = await self._ensure_entity()  # validates immediately
        title = getattr(entity, "title", None) or self._channel_input
        return {"title": title}

    async def _scan_page(self, offset_id, limit):
        entity = await self._ensure_entity()
        out = []
        # iter_messages is sync and returns an async iterator.
        async for msg in self._client.iter_messages(
            entity,
            filter=types.InputMessagesFilterVideo,
            limit=limit,
            offset_id=offset_id,
        ):
            meta = self._meta_from_msg(msg)
            if meta:
                out.append(meta)
        return out

    async def _get_video(self, msg_id):
        meta = self._meta.get(msg_id)
        if meta is not None and msg_id in self._docs:
            return meta
        entity = await self._ensure_entity()
        msg = await self._client.get_messages(entity, ids=msg_id)
        if isinstance(msg, list):
            msg = msg[0] if msg else None
        meta = self._meta_from_msg(msg) if msg else None
        if meta is None:
            raise BridgeError("not_found", "Video %d not found in the channel." % msg_id)
        return meta

    # -- streaming ----------------------------------------------------------
    async def _fetch_range(self, msg_id, start, end):
        """
        Fetch inclusive byte range [start, end] of a video's file.
        Uses aligned BLOCK_SIZE chunk fetches via _iter_download (the only
        telethon 1.45 API that supports byte offsets) and slices exactly.
        Data stays in memory; nothing is written to disk.
        """
        await self._get_video(msg_id)  # ensures self._docs[msg_id]
        doc = self._docs[msg_id]
        first_block = start // BLOCK_SIZE
        last_block = end // BLOCK_SIZE
        buf = bytearray()
        # offset/limit here: byte offset + number of BLOCK_SIZE chunks.
        async for chunk in self._client._iter_download(
            doc,
            offset=first_block * BLOCK_SIZE,
            limit=last_block - first_block + 1,
            chunk_size=BLOCK_SIZE,
            request_size=BLOCK_SIZE,
            file_size=doc.size,
        ):
            buf.extend(chunk)
        data = bytes(buf)
        lo = start - first_block * BLOCK_SIZE
        hi = end - first_block * BLOCK_SIZE + 1
        return data[lo:hi]

    async def _get_thumb(self, msg_id):
        entity = await self._ensure_entity()
        msg = await self._client.get_messages(entity, ids=msg_id)
        if isinstance(msg, list):
            msg = msg[0] if msg else None
        if not msg or not getattr(msg, "media", None):
            return None
        # thumb=0 -> smallest available thumbnail; file=bytes -> in-memory.
        data = await self._client.download_media(msg, file=bytes, thumb=0)
        return bytes(data) if data else None

    # -- public sync API ----------------------------------------------------
    def restore(self, api_id, api_hash, channel):
        """
        Rebuild the client from saved settings on startup (no code is sent).
        Returns True if the saved session is still authorized.
        """
        return self._call(self._restore, api_id, api_hash, channel, timeout=60)

    async def _restore(self, api_id, api_hash, channel):
        await self._ensure_client(api_id, api_hash)
        if channel:
            self._channel_input = channel.strip()
            self._entity = None  # resolved lazily on first use
        try:
            return await self._client.is_user_authorized()
        except Exception:
            return False

    def auth_start(self, api_id, api_hash, phone):
        return self._call(self._auth_start, api_id, api_hash, phone, timeout=60)

    def auth_code(self, code):
        return self._call(self._auth_code, code, timeout=60)

    def auth_password(self, password):
        return self._call(self._auth_password, password, timeout=60)

    def is_authorized(self):
        return self._call(self._is_authorized, timeout=30)

    def logout(self):
        return self._call(self._logout, timeout=30)

    def set_channel(self, channel_input):
        return self._call(self._set_channel, channel_input, timeout=60)

    def scan_page(self, offset_id=0, limit=60):
        return self._call(self._scan_page, offset_id, limit, timeout=120)

    def get_video(self, msg_id):
        return self._call(self._get_video, msg_id, timeout=60)

    def fetch_range(self, msg_id, start, end):
        return self._call(self._fetch_range, msg_id, start, end, timeout=120)

    def get_thumb(self, msg_id):
        return self._call(self._get_thumb, msg_id, timeout=60)


class MockBridge:
    """
    Fake backend used when PSC_STREAM_MOCK=1. Mirrors TelegramBridge's public
    API so the server and UI exercise the exact same code paths, including
    HTTP Range handling on /api/stream.
    """

    def __init__(self):
        self._authed = False
        self._phone = None
        self._channel = None
        self._videos = mock_data.fake_library()

    def restore(self, api_id, api_hash, channel):
        # Mock has no persistent session; stay logged out until the wizard runs.
        return False

    def auth_start(self, api_id, api_hash, phone):
        phone = (phone or "").strip()
        if not phone:
            raise BridgeError("phone_invalid", "Enter your phone number.")
        self._phone = phone
        return {"next": "code"}

    def auth_code(self, code):
        code = (code or "").strip()
        if code == "00000":
            return {"next": "password"}  # lets the UI's 2FA step be tested
        if len(code) >= 4:
            self._authed = True
            self._phone = None
            return {"next": "done"}
        raise BridgeError("code_invalid", "Wrong code (mock: any 4+ chars works).")

    def auth_password(self, password):
        if (password or "").strip():
            self._authed = True
            return {"next": "done"}
        raise BridgeError("password_invalid", "Wrong 2FA password.")

    def is_authorized(self):
        return self._authed

    def logout(self):
        self._authed = False
        self._channel = None

    def set_channel(self, channel_input):
        channel_input = (channel_input or "").strip()
        if not channel_input:
            raise BridgeError("channel_not_found", "Enter a channel username or link.")
        self._channel = channel_input
        return {"title": "Mock Study Channel"}

    def scan_page(self, offset_id=0, limit=60):
        vids = [v for v in self._videos if v["id"] < offset_id] if offset_id else list(self._videos)
        return vids[:limit]

    def get_video(self, msg_id):
        for v in self._videos:
            if v["id"] == msg_id:
                return v
        raise BridgeError("not_found", "Video %d not found." % msg_id)

    def fetch_range(self, msg_id, start, end):
        size = mock_data.fake_size(msg_id)
        if size is None:
            raise BridgeError("not_found", "Video %d not found." % msg_id)
        end = min(end, size - 1)
        return mock_data.fake_range(msg_id, start, end)

    def get_thumb(self, msg_id):
        if mock_data.fake_size(msg_id) is None:
            return None
        return mock_data.fake_thumb(msg_id)


def make_bridge(mock, session_path):
    if mock:
        return MockBridge()
    return TelegramBridge(session_path)
