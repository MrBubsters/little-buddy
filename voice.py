"""Voice channels: hear everyone, act on "<wake word>, ...", reply in text and out loud.

Pipeline:  Discord voice ─► (DAVE decrypt) ─► per-speaker buffer ─► Whisper (speech→text)
           ─► wake word? ─► agent.chat() ─► text reply + Kokoro (text→speech) back into the call.
Everything runs locally: Whisper on the GPU (CPU fallback), Kokoro on the CPU (~4x realtime).
"""
import asyncio
import glob
import logging
import os
import random
import re
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

import config
import db

# faster-whisper needs NVIDIA's cuBLAS/cuDNN DLLs; on Windows they come from the nvidia-* pip
# packages and must be on the DLL search path before ctranslate2 loads.
if sys.platform == "win32":
    for _d in glob.glob(os.path.join(sys.prefix, "Lib", "site-packages", "nvidia", "*", "bin")):
        os.add_dll_directory(_d)
        os.environ["PATH"] = _d + os.pathsep + os.environ["PATH"]

import davey  # noqa: E402
import discord  # noqa: E402
from discord.ext import voice_recv  # noqa: E402
from discord.ext.voice_recv import reader as _reader  # noqa: E402
from discord.ext.voice_recv.rtp import OPUS_SILENCE  # noqa: E402
from discord.voice_state import VoiceConnectionState  # noqa: E402

FRAME_SECONDS = 0.02         # Discord sends 20 ms frames of 48 kHz stereo 16-bit PCM
FRAME_BYTES = 3840           # one such frame
SILENCE_RMS = 200            # frames quieter than this count as silence
END_OF_SPEECH = 1.0          # seconds of quiet that end an utterance
MAX_UTTERANCE = 20.0         # force-cut very long utterances
MIN_SPEECH = 0.3             # ignore blips shorter than this
FOLLOW_UP_WINDOW = 8.0       # after a bare "Bub?", the next sentence counts as a command
WAKE_FUZZY = 0.75            # how close a misheard word must be to a wake word (1.0 = exact)
FILLER_AFTER = 2.5           # say "one sec" if the model takes longer than this to answer
SENTENCE_GAP = 0.12          # pause between spoken sentences, seconds
CALL_MEMORY = 10             # how many recent messages from this call the model sees
VOICES_DIR = Path(config.DB_PATH).parent / "voices"
KOKORO_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")

ACKS = ["Yeah?", "What's up?", "I'm here.", "Yes?"]
# Whisper "hears" these in background noise, and bare fillers aren't worth a reply.
NOISE = {"", "you", "thank you", "thanks", "thank you very much", "thanks for watching", "thank you for watching",
         "bye", "uh", "um", "hmm", "mm", "mhm", "huh", "ah", "oh", "okay", "ok", "so", "yeah"}
FILLERS = ["One sec.", "Hmm, let me check.", "Okay, one moment."]

# --------------------------------------------------------------------------- logging
# voice_recv logs every RTCP sender report at INFO, which floods the console.
logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.WARNING)

STATS: dict[str, int] = {}
LAST_ERROR: dict[str, str] = {}
DAVE_DEBUG: dict = {}


def _count(key: str, error: Exception | None = None):
    STATS[key] = STATS.get(key, 0) + 1
    if error is not None:
        LAST_ERROR[key] = f"{error.__class__.__name__}: {error}"


class _DaveLogFilter(logging.Filter):
    """Let through only discord.py's DAVE/MLS handshake debug lines (it logs them at DEBUG)."""

    def filter(self, record):
        return record.levelno > logging.DEBUG or bool(
            re.search(r"DAVE|MLS|transition|epoch|E2EE|binary frame", record.getMessage(), re.IGNORECASE))


if config.VOICE_DEBUG:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[dave] %(name)s: %(message)s"))
    _h.addFilter(_DaveLogFilter())
    for _name in ("discord.gateway", "discord.voice_state"):
        _lg = logging.getLogger(_name)
        _lg.setLevel(logging.DEBUG)
        _lg.addHandler(_h)
        _lg.propagate = False

# discord.py swallows errors from processing MLS commits/welcomes and silently resets the
# session, which leaves the bot unable to decrypt anyone. Print the error before that happens.
_original_recover = VoiceConnectionState._recover_from_invalid_commit


async def _recover_logged(self, transition_id):
    print(f"[dave] invalid MLS commit/welcome (transition {transition_id}): {sys.exc_info()[1]!r}")
    await _original_recover(self, transition_id)


VoiceConnectionState._recover_from_invalid_commit = _recover_logged


# --------------------------------------------------------------------------- DAVE patch
# Since March 2026 Discord end-to-end encrypts all voice (DAVE). discord.py manages the DAVE
# session but only *sends* audio; discord-ext-voice-recv receives audio but doesn't know about
# DAVE. This adds the missing step: after the transport layer is decrypted, decrypt the
# DAVE layer with the session discord.py already keeps. (Needs davey<0.2, see requirements.txt.)
_original_reader_init = _reader.AudioReader.__init__


def _reader_init_with_dave(self, sink, voice_client, *, after=None):
    _original_reader_init(self, sink, voice_client, after=after)
    transport_decrypt = self.decryptor.decrypt_rtp

    def decrypt_rtp(packet):
        data = transport_decrypt(packet)
        _count("rtp")
        state = voice_client._connection
        session = getattr(state, "dave_session", None)
        if data == OPUS_SILENCE:
            return data  # silence frames are never DAVE-encrypted
        if not session or not getattr(state, "dave_protocol_version", 0):
            _count("no_dave")
            return data
        user_id = voice_client._ssrc_to_id.get(packet.ssrc)
        if user_id is None:
            _count("unknown_ssrc")
            return OPUS_SILENCE  # don't know who this is yet (arrives with their SPEAKING event)
        try:
            out = session.decrypt(user_id, davey.MediaType.audio, bytes(data))
            _count("dave_ok")
            return out
        except Exception as e:
            _count("dave_fail", e)
            DAVE_DEBUG["failed_user"] = user_id
            DAVE_DEBUG["session"] = session
            return data if session.can_passthrough(user_id) else OPUS_SILENCE

    self.decryptor.decrypt_rtp = decrypt_rtp


_reader.AudioReader.__init__ = _reader_init_with_dave


# --------------------------------------------------------------------------- speech-to-text
_whisper = None


def _load_whisper():
    global _whisper
    if _whisper:
        return _whisper
    from faster_whisper import WhisperModel
    device = config.WHISPER_DEVICE
    if device in ("auto", "cuda"):
        try:
            _whisper = WhisperModel(config.WHISPER_MODEL, device="cuda", compute_type="int8_float16")
            _whisper.transcribe(np.zeros(16000, dtype=np.float32))  # fail now, not mid-call
            print(f"[voice] Whisper '{config.WHISPER_MODEL}' on GPU")
            return _whisper
        except Exception as e:
            if device == "cuda":
                raise
            print(f"[voice] GPU unavailable for Whisper ({e}); using CPU")
    _whisper = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
    print(f"[voice] Whisper '{config.WHISPER_MODEL}' on CPU")
    return _whisper


def _transcribe(audio16k: np.ndarray) -> str:
    # No `hotwords` hint: tested, it made Whisper hallucinate ("bubster bubs bubs bubs…") on short
    # clips. Misheard names are handled by fuzzy wake-word matching instead.
    segments, _ = _load_whisper().transcribe(
        audio16k, language=config.WHISPER_LANGUAGE or None, vad_filter=True,
        condition_on_previous_text=False)
    text = " ".join(s.text.strip() for s in segments).strip()
    return re.sub(r"\b(\w+)(?:[\s,]+\1\b){2,}", r"\1", text, flags=re.I)  # "bubs bubs bubs" → "bubs"


def _to_whisper_audio(pcm: bytes) -> np.ndarray:
    """48 kHz stereo int16 → 16 kHz mono float32."""
    import soxr
    a = np.frombuffer(pcm, dtype=np.int16).reshape(-1, 2).mean(axis=1).astype(np.float32) / 32768.0
    return soxr.resample(a, 48000, 16000).astype(np.float32)


# --------------------------------------------------------------------------- text-to-speech
_kokoro = None
_tts_lock = threading.Lock()


def _load_tts():
    global _kokoro
    if _kokoro:
        return _kokoro
    from kokoro_onnx import Kokoro
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    for name in KOKORO_FILES:
        path = VOICES_DIR / name
        if not path.exists():
            print(f"[voice] downloading {name} (one time)…")
            urllib.request.urlretrieve(KOKORO_URL + name, path.with_suffix(".part"))
            path.with_suffix(".part").rename(path)
    _kokoro = Kokoro(str(VOICES_DIR / KOKORO_FILES[0]), str(VOICES_DIR / KOKORO_FILES[1]))
    if config.TTS_VOICE not in _kokoro.get_voices():
        raise ValueError(f"Unknown TTS_VOICE '{config.TTS_VOICE}'. Options: {', '.join(_kokoro.get_voices())}")
    _synthesize("Ready.")  # warm up so the first real reply isn't slow
    print(f"[voice] Kokoro voice '{config.TTS_VOICE}' loaded")
    return _kokoro


def _synthesize(text: str) -> bytes:
    """One sentence → 48 kHz stereo 16-bit PCM, the format Discord plays."""
    import soxr
    lang = "en-gb" if config.TTS_VOICE.startswith("b") else "en-us"
    with _tts_lock:
        samples, rate = _kokoro.create(text, voice=config.TTS_VOICE, speed=config.TTS_SPEED, lang=lang)
    audio = soxr.resample(np.asarray(samples, dtype=np.float32), rate, 48000)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    return np.repeat(pcm, 2).tobytes()  # mono → interleaved stereo


_DAYS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday",
         "sat": "Saturday", "sun": "Sunday"}
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December"]


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _say_date(d: datetime) -> str:
    today = db.now().date()
    if d.date() == today:
        return "today"
    if d.date() == today + timedelta(days=1):
        return "tomorrow"
    if 0 < (d.date() - today).days < 7:
        return d.strftime("%A")
    return f"{d.strftime('%A')}, {_MONTHS[d.month - 1]} {_ordinal(d.day)}"


def _say_time(h: int, m: int) -> str:
    if (h, m) == (12, 0):
        return "noon"
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12} {suffix}" if m == 0 else f"{h12}:{m:02d} {suffix}"


def _speakable(text: str) -> str:
    """Turn chat-formatted text into something that sounds natural when spoken.

    The model is asked to talk naturally, but this is a safety net: receipts, markdown, emoji,
    item ids, lists and machine-style dates/times are rewritten or removed.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("-#")]
    # Bullet/numbered lists → "a, b, and c."
    out, items = [], []
    for ln in lines + [""]:
        m = re.match(r"^(?:[-*•]|\d+[.)])\s+(.*)", ln)
        if m:
            item = re.sub(r"\s*[—–-]\s*no date\b", "", m.group(1), flags=re.I)
            items.append(re.sub(r"\s+[—–]\s+", " ", item).rstrip(".;"))  # "Standup — Fri…" → "Standup Fri…"
            continue
        if items:
            out.append((", ".join(items[:-1]) + ", and " + items[-1] if len(items) > 1 else items[0]) + ".")
            items = []
        if ln:
            out.append(ln)
    text = " ".join(out)

    text = text.translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "…": "..."}))
    text = re.sub(r"<@!?\d+>", "", text)
    # "Friday, November 6, 2026" / "November 6th, 2026" → "tomorrow"
    def _long_date(m):
        try:
            return _say_date(datetime(int(m[3]), _MONTHS.index(m[1].capitalize()) + 1, int(m[2])))
        except ValueError:
            return m[0]
    text = re.sub(r"\b(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
                  rf"({'|'.join(_MONTHS)})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", _long_date, text, flags=re.I)
    text = re.sub(r"#\d+\b|\[(?:event|deadline|task|reminder)/\w+\]|\(id:? ?\d+\)", "", text, flags=re.I)
    # 2026-11-06 15:00 / 2026-11-06
    text = re.sub(r"\b(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}))?",
                  lambda m: _say_date(datetime(int(m[1]), int(m[2]), int(m[3])))
                  + (f" at {_say_time(int(m[4]), int(m[5]))}" if m[4] else ""), text)
    # "Tue Nov 10 15:00" (how items are formatted)
    def _short_date(m):
        try:
            d = datetime(db.now().year, _MONTHS.index(next(x for x in _MONTHS if x.lower().startswith(m[2].lower()))) + 1, int(m[3]))
        except (StopIteration, ValueError):
            return m[0]
        return _say_date(d) + (f" at {_say_time(int(m[4]), int(m[5]))}" if m[4] else "")
    text = re.sub(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w* (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w* (\d{1,2})"
                  r"(?: (\d{1,2}):(\d{2}))?\b", _short_date, text)
    # Remaining 24h times (not already followed by am/pm)
    text = re.sub(r"\b([01]?\d|2[0-3]):([0-5]\d)\b(?!\s*[ap]\.?m)", lambda m: _say_time(int(m[1]), int(m[2])),
                  text, flags=re.I)
    text = re.sub(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b", lambda m: _DAYS[m[1].lower()], text)
    text = text.replace("&", " and ").replace(" w/ ", " with ").replace("—", ", ").replace("–", ", ")
    text = re.sub(r"[*_`~>#|]", "", text)
    text = re.sub(r"[^\w\s.,!?'\":;()%$/+-]", "", text)  # emoji and other symbols
    text = re.sub(r"\s+([.,!?])", r"\1", re.sub(r"\s+", " ", text)).strip(" ,")
    if len(text) > 600:  # keep it short; cut at a sentence boundary
        cut = max(text.rfind(p, 0, 600) for p in ".!?")
        text = text[: cut + 1] if cut > 0 else text[:600]
    return text


def _synth_estimate(sentence: str) -> float:
    """Rough seconds Kokoro needs for a sentence (measured: ~15 chars of speech/s, ~4x realtime)."""
    return len(sentence) / 15 / 4 + 0.15


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = []
    for p in parts:  # merge tiny fragments ("Okay.") into the next sentence for smoother prosody
        if out and len(out[-1]) < 12:
            out[-1] += " " + p
        else:
            out.append(p)
    return [p for p in out if p.strip()]


class _StreamSource(discord.AudioSource):
    """Audio that can start playing before all of it is synthesized.

    Sentences are fed in as they're ready; while waiting for the next one it plays silence,
    and it ends once finish() is called and the buffer is empty.
    """

    def __init__(self):
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.done = False

    def feed(self, pcm: bytes):
        with self.lock:
            self.buf += pcm

    def finish(self):
        self.done = True

    def read(self) -> bytes:
        with self.lock:
            if len(self.buf) >= FRAME_BYTES:
                out = bytes(self.buf[:FRAME_BYTES])
                del self.buf[:FRAME_BYTES]
                return out
            if self.done:
                if self.buf:
                    out = bytes(self.buf) + bytes(FRAME_BYTES - len(self.buf))
                    self.buf.clear()
                    return out
                return b""
        return bytes(FRAME_BYTES)

    def is_opus(self) -> bool:
        return False


# --------------------------------------------------------------------------- wake word
def _match_wake(text: str) -> str | None:
    """Return what was said after the wake word ("" if only the wake word), or None."""
    # Only the first few words count ("Hey Bub, ..."), so mentioning the name mid-sentence
    # in normal conversation doesn't trigger the bot.
    first = list(re.finditer(r"[\w']+", text))[:3]
    if not first:
        return None
    words = "|".join(re.escape(w) for w in config.WAKE_WORDS)
    m = re.search(rf"\b(?:{words})\b[\s,.!?:;-]*(.*)", text, re.IGNORECASE | re.DOTALL)
    if m and m.start() < first[-1].end():
        return m.group(1).strip()
    # Whisper often mishears short names ("Bub" → "bulb"); accept a close match.
    for tok in first:
        word = tok.group().lower()
        if len(word) >= 3 and any(SequenceMatcher(None, word, w).ratio() >= WAKE_FUZZY for w in config.WAKE_WORDS):
            return text[tok.end():].lstrip(" ,.!?:;-").strip()
    return None


# --------------------------------------------------------------------------- audio capture
class _Speaker:
    def __init__(self):
        self.frames: list[bytes] = []
        self.voiced = 0
        self.started = self.last_voice = 0.0


class _Sink(voice_recv.AudioSink):
    """Receives decoded PCM on the reader thread and hands it to the session."""

    def __init__(self, session: "VoiceSession"):
        super().__init__()
        self.session = session

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data):
        if user is None:
            return _count("pcm_no_user")
        if user.bot or not data.pcm:
            return
        _count("pcm")
        self.session.on_audio(user, data.pcm)

    def cleanup(self):
        pass


class VoiceSession:
    def __init__(self, vc, text_dest, chat, llm_lock: asyncio.Lock):
        self.vc = vc
        self.text_dest = text_dest  # where transcripts and replies are posted
        self.chat = chat
        self.llm_lock = llm_lock
        self.lock = threading.Lock()
        self.speakers: dict[int, _Speaker] = {}
        self.names: dict[int, str] = {}
        self.armed: dict[int, float] = {}  # user id → until when a follow-up counts as a command
        self.queue: asyncio.Queue = asyncio.Queue()
        self.cache: dict[str, bytes] = {}  # pre-synthesized short phrases (acks, fillers)
        self.turns: list[dict] = []  # this call's conversation, so follow-ups ("and tomorrow?") make sense
        self.tasks = [asyncio.create_task(t) for t in (self._flusher(), self._worker(), self._stats(),
                                                        self._precache())]

    async def _precache(self):
        if config.VOICE_REPLIES:
            for phrase in ACKS + FILLERS:
                self.cache[phrase] = await asyncio.to_thread(_synthesize, phrase)

    async def _stats(self):
        while True:
            await asyncio.sleep(5)
            problem = any(k in STATS for k in ("dave_fail", "unknown_ssrc", "pcm_no_user"))
            if STATS and (config.VOICE_DEBUG or problem):
                print("[voice] last 5s: " + " ".join(f"{k}={v}" for k, v in STATS.items()))
                for k, err in LAST_ERROR.items():
                    print(f"[voice]   {k} error: {err}")
                s = DAVE_DEBUG.pop("session", None)
                if s is not None:
                    try:
                        print(f"[voice]   DAVE status={s.status} ready={s.ready} epoch={s.epoch} "
                              f"version={s.protocol_version} me={s.user_id} group={s.get_user_ids()} "
                              f"failed_for={DAVE_DEBUG.get('failed_user')}")
                    except Exception as e:
                        print(f"[voice]   DAVE status unavailable: {e!r}")
            STATS.clear()
            LAST_ERROR.clear()

    # Reader thread: buffer frames per speaker, starting at the first loud frame.
    def on_audio(self, user, pcm: bytes):
        loud = np.sqrt(np.mean(np.frombuffer(pcm, dtype=np.int16).astype(np.float32) ** 2)) > SILENCE_RMS
        now = time.monotonic()
        with self.lock:
            sp = self.speakers.get(user.id)
            if sp is None:
                if not loud:
                    return
                sp = self.speakers[user.id] = _Speaker()
                sp.started = now
                self.names[user.id] = user.display_name
            sp.frames.append(pcm)
            if loud:
                sp.voiced += 1
                sp.last_voice = now

    # Event loop: close utterances after a pause and queue them for transcription.
    async def _flusher(self):
        while True:
            await asyncio.sleep(0.1)
            now = time.monotonic()
            done = []
            with self.lock:
                for uid, sp in list(self.speakers.items()):
                    if now - sp.last_voice > END_OF_SPEECH or now - sp.started > MAX_UTTERANCE:
                        del self.speakers[uid]
                        if sp.voiced * FRAME_SECONDS >= MIN_SPEECH:
                            done.append((uid, b"".join(sp.frames)))
            for item in done:
                self.queue.put_nowait(item)

    # One utterance at a time: transcribe → wake word → LLM → reply.
    async def _worker(self):
        while True:
            uid, pcm = await self.queue.get()
            # If they kept talking after a pause while we were busy, treat it as one message.
            later = []
            while not self.queue.empty():
                other_uid, other_pcm = self.queue.get_nowait()
                if other_uid == uid:
                    pcm += bytes(int(48000 * 0.3) * 4) + other_pcm
                else:
                    later.append((other_uid, other_pcm))
            for item in later:
                self.queue.put_nowait(item)
            try:
                await self._handle(uid, pcm)
            except Exception as e:
                print(f"[voice] error handling speech: {e!r}")

    async def _handle(self, uid: int, pcm: bytes):
        text = await asyncio.to_thread(_transcribe, _to_whisper_audio(pcm))
        name = self.names.get(uid, "someone")
        if not text:
            return
        print(f"[voice] {name}: {text}")
        command = _match_wake(text)
        if config.WAKE_WORD_REQUIRED:
            if command is None:
                if self.armed.pop(uid, 0) < time.monotonic():
                    return  # ordinary conversation, not for us
                command = text
        else:
            if re.sub(r"[^\w\s']", "", text).strip().lower() in NOISE:
                return
            if command is None:
                command = text  # no wake word needed; if one was said, it's already stripped
        if self.vc.is_playing():
            self.vc.stop()  # they're talking to us again: stop the current reply
        if not command:
            self.armed[uid] = time.monotonic() + FOLLOW_UP_WINDOW
            return await self.say(random.choice(ACKS))

        await self.text_dest.send(f"🎙️ **{name}:** {command}")
        owner = uid == config.OWNER_ID
        who = "the user themself" if owner else f"{name}, a friend of the user in the call (not the user)"
        async with self.llm_lock:
            said = command if owner else f"({name} says) {command}"
            reply_task = asyncio.create_task(
                self.chat(said, voice=who, guest=not owner, history=self.turns[-CALL_MEMORY:]))
            done, _ = await asyncio.wait({reply_task}, timeout=FILLER_AFTER)
            if not done:
                await self.say(random.choice(FILLERS))  # slow model: let them know we heard
            try:
                reply = await reply_task
            except Exception as e:
                reply = f"Sorry, I couldn't reach my brain just now. ({e.__class__.__name__})"
        spoken = "\n".join(ln for ln in reply.splitlines() if not ln.lstrip().startswith("-#")).strip()
        self.turns += [{"role": "user", "content": said}, {"role": "assistant", "content": spoken}]
        await self.text_dest.send(reply[:1900])
        await self.say(reply)

    async def say(self, text: str):
        """Speak text in the call, starting as soon as the first sentence is synthesized."""
        if not config.VOICE_REPLIES or not self.vc.is_connected():
            return
        while self.vc.is_playing():
            await asyncio.sleep(0.05)
        if text in self.cache:
            src = _StreamSource()
            src.feed(self.cache[text])
            src.finish()
            return self.vc.play(src)
        sentences = _sentences(_speakable(text))
        if not sentences:
            return
        src = _StreamSource()
        gap = bytes(int(48000 * SENTENCE_GAP) * 4)
        buffered, started = 0.0, False
        for i, sentence in enumerate(sentences):
            pcm = await asyncio.to_thread(_synthesize, sentence)
            src.feed(pcm + gap)
            buffered += len(pcm) / 192000 + SENTENCE_GAP
            if started and not self.vc.is_playing():
                return  # interrupted (vc.stop()) — don't keep synthesizing
            # Start once there's enough audio queued to cover synthesizing the next sentence,
            # so playback never runs dry mid-reply.
            last = i == len(sentences) - 1
            if not started and (last or buffered >= _synth_estimate(sentences[i + 1]) + 0.3):
                if self.vc.is_connected():
                    self.vc.play(src)
                started = True
        src.finish()

    async def close(self):
        for t in self.tasks:
            t.cancel()
        if self.vc.is_listening():
            self.vc.stop_listening()
        await self.vc.disconnect(force=True)


# --------------------------------------------------------------------------- public API
session: VoiceSession | None = None


def owner_voice_channel(bot: discord.Client):
    """The voice channel the owner is currently in, in any server the bot shares with them."""
    for guild in bot.guilds:
        for ch in [*guild.voice_channels, *guild.stage_channels]:
            if config.OWNER_ID in ch.voice_states:
                return ch
    return None


async def join(channel, text_dest, chat, llm_lock) -> str:
    global session
    if session:
        await leave()
    await asyncio.to_thread(_load_whisper)  # first time: downloads the model (~250–500 MB)
    if config.VOICE_REPLIES:
        await asyncio.to_thread(_load_tts)  # first time: downloads Kokoro (~350 MB)
    vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
    session = VoiceSession(vc, text_dest, chat, llm_lock)
    vc.listen(_Sink(session))
    if config.VOICE_DEBUG:
        state = vc._connection
        print(f"[voice] joined {channel.name}: mode={vc.mode} dave_version={getattr(state, 'dave_protocol_version', '?')} "
              f"wake={config.WAKE_WORDS}")
    if not config.WAKE_WORD_REQUIRED:
        return f"🎧 I'm in **{channel.name}**. Just talk to me. `!leave` when you're done."
    wake = config.WAKE_WORDS[0].capitalize()
    return (f"🎧 Listening in **{channel.name}**. Say \"{wake}, …\" to talk to me, "
            f"e.g. \"{wake}, remind me to call mom at 6\". `!leave` when done.")


async def leave() -> bool:
    global session
    if not session:
        return False
    s, session = session, None
    await s.close()
    return True


def humans_left(channel) -> int:
    return sum(1 for m in channel.members if not m.bot)
