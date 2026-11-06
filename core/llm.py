"""Language-model backends behind one small interface.

    reply = await llm.chat(messages, tools=..., model=..., temperature=...)
    reply.content, reply.tool_calls  # [{"id", "name", "arguments": dict}]

Messages use one neutral format and each provider converts it:
    {"role": "system" | "user" | "assistant" | "tool", "content": str}
    assistant turns may carry "tool_calls"; tool turns carry "tool_call_id" and "name".

Add a backend by subclassing Provider and registering it in PROVIDERS.
"""
import asyncio
import json
import re
from dataclasses import dataclass, field

import config


@dataclass
class Reply:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.S)


def _clean(text: str | None) -> str:
    return _THINK_BLOCK.sub("", text or "").strip()


class Provider:
    name = "base"

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, *, model: str,
                   temperature: float, max_tokens: int | None, think: bool | None) -> Reply:
        raise NotImplementedError

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        raise NotImplementedError

    async def status(self) -> str:
        return f"✓ {self.name} provider, model '{config.LLM_MODEL}'"


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self):
        from ollama import AsyncClient
        self.client = AsyncClient(host=config.LLM_BASE_URL, timeout=config.LLM_TIMEOUT)

    @staticmethod
    def _convert(m: dict) -> dict:
        out = {"role": m["role"], "content": m.get("content") or ""}
        if m.get("tool_calls"):
            out["tool_calls"] = [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in m["tool_calls"]]
        if m["role"] == "tool":
            out["tool_name"] = m.get("name", "")
        return out

    async def chat(self, messages, tools=None, *, model, temperature, max_tokens, think):
        options = {"num_ctx": config.LLM_CONTEXT, "temperature": temperature}
        if max_tokens:
            options["num_predict"] = max_tokens
        kwargs = {"model": model, "messages": [self._convert(m) for m in messages],
                  "keep_alive": config.OLLAMA_KEEP_ALIVE, "options": options}
        if tools:
            kwargs["tools"] = tools
        if think is not None:
            kwargs["think"] = think
        r = await self.client.chat(**kwargs)
        calls = [{"id": f"call_{i}", "name": c.function.name, "arguments": dict(c.function.arguments or {})}
                 for i, c in enumerate(r.message.tool_calls or [])]
        return Reply(_clean(r.message.content), calls, r.prompt_eval_count, r.eval_count)

    async def embed(self, texts, model):
        r = await self.client.embed(model=model, input=texts, keep_alive=config.OLLAMA_KEEP_ALIVE)
        return [list(v) for v in r.embeddings]

    async def status(self) -> str:
        """Check Ollama is reachable and the model exists, and load it into memory."""
        model = config.LLM_MODEL
        try:
            models = {m.model for m in (await self.client.list()).models}
        except Exception as e:
            return f"✗ Ollama not reachable at {config.LLM_BASE_URL} ({e.__class__.__name__}). Is it running?"
        missing = [m for m in {model, config.LLM_FAST_MODEL, config.EMBED_MODEL} - {""}
                   if m not in models and f"{m}:latest" not in models]
        if missing:
            return f"✗ Model(s) not downloaded: {', '.join(missing)}. Run: ollama pull <name>"
        # An empty prompt makes Ollama load the model without generating anything.
        await self.client.generate(model=model, prompt="", keep_alive=config.OLLAMA_KEEP_ALIVE,
                                   options={"num_ctx": config.LLM_CONTEXT})
        loaded = {m.model: m for m in (await self.client.ps()).models}
        m = loaded.get(model) or loaded.get(f"{model}:latest")
        if not m:
            return f"✓ Model '{model}' ready"
        vram = (m.size_vram or 0) / 1e9
        where = "100% GPU" if m.size_vram and m.size_vram >= m.size else f"{vram:.1f} GB on GPU, rest on CPU (slow!)"
        return f"✓ Model '{model}' loaded: {m.size / 1e9:.1f} GB, {where}, context {config.LLM_CONTEXT} tokens"


class OpenAIProvider(Provider):
    """Any server speaking the OpenAI chat-completions API."""
    name = "openai"

    def __init__(self):
        import httpx
        headers = {"Authorization": f"Bearer {config.LLM_API_KEY}"} if config.LLM_API_KEY else {}
        self.client = httpx.AsyncClient(base_url=config.LLM_BASE_URL.rstrip("/"), headers=headers,
                                        timeout=config.LLM_TIMEOUT)

    @staticmethod
    def _convert(m: dict) -> dict:
        out = {"role": m["role"], "content": m.get("content") or ""}
        if m.get("tool_calls"):
            out["tool_calls"] = [{"id": c["id"], "type": "function",
                                  "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                 for c in m["tool_calls"]]
        if m["role"] == "tool":
            out["tool_call_id"] = m.get("tool_call_id", "")
        return out

    async def chat(self, messages, tools=None, *, model, temperature, max_tokens, think):
        payload = {"model": model, "messages": [self._convert(m) for m in messages], "temperature": temperature}
        if tools:
            payload["tools"] = tools
        if max_tokens:
            payload["max_tokens"] = max_tokens
        r = await self.client.post("/chat/completions", json=payload)
        r.raise_for_status()
        data = r.json()
        msg = data["choices"][0]["message"]
        calls = []
        for i, c in enumerate(msg.get("tool_calls") or []):
            raw = c["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                args = {"_unparsed": raw}
            calls.append({"id": c.get("id") or f"call_{i}", "name": c["function"]["name"], "arguments": args})
        usage = data.get("usage") or {}
        return Reply(_clean(msg.get("content")), calls, usage.get("prompt_tokens"), usage.get("completion_tokens"))

    async def embed(self, texts, model):
        r = await self.client.post("/embeddings", json={"model": model, "input": texts})
        r.raise_for_status()
        return [d["embedding"] for d in r.json()["data"]]

    async def status(self) -> str:
        try:
            r = await self.client.get("/models")
            r.raise_for_status()
            ids = {m.get("id") for m in r.json().get("data", [])}
        except Exception as e:
            return f"✗ LLM server not reachable at {config.LLM_BASE_URL} ({e.__class__.__name__})"
        note = "" if not ids or config.LLM_MODEL in ids else f" (server lists: {', '.join(sorted(ids)[:5])})"
        return f"✓ OpenAI-compatible server at {config.LLM_BASE_URL}, model '{config.LLM_MODEL}'{note}"


PROVIDERS = {"ollama": OllamaProvider, "openai": OpenAIProvider}

_provider: Provider | None = None
_slots = asyncio.Semaphore(max(1, config.LLM_CONCURRENCY))
STATS = {"requests": 0, "last_prompt_tokens": None}


def tokens(text: str) -> int:
    """Rough token count (deliberately on the high side, so budgets don't overflow)."""
    return len(text) // 3 + 1


def provider() -> Provider:
    global _provider
    if _provider is None:
        if config.LLM_PROVIDER not in PROVIDERS:
            raise ValueError(f"Unknown LLM_PROVIDER '{config.LLM_PROVIDER}'. Options: {', '.join(PROVIDERS)}")
        _provider = PROVIDERS[config.LLM_PROVIDER]()
    return _provider


def set_provider(p: Provider) -> None:
    """Swap the backend (tests use a scripted fake)."""
    global _provider
    _provider = p


async def chat(messages: list[dict], tools: list[dict] | None = None, *, model: str | None = None,
               temperature: float | None = None, max_tokens: int | None = None,
               think: bool | None = None) -> Reply:
    think = config.LLM_THINK if think is None else (think if config.LLM_THINK is not None else None)
    async with _slots:
        reply = await provider().chat(messages, tools, model=model or config.LLM_MODEL,
                                      temperature=config.LLM_TEMPERATURE if temperature is None else temperature,
                                      max_tokens=max_tokens, think=think)
    STATS["requests"] += 1
    if reply.prompt_tokens:
        STATS["last_prompt_tokens"] = reply.prompt_tokens
    return reply


async def embed(texts: list[str]) -> list[list[float]] | None:
    """Embeddings for texts, or None when no EMBED_MODEL is configured."""
    if not config.EMBED_MODEL or not texts:
        return None
    async with _slots:
        return await provider().embed(texts, config.EMBED_MODEL)


async def status() -> str:
    return await provider().status()
