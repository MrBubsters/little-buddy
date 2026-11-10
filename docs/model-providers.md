# Model providers: current support and adding providers

## Short answer

little-buddy has first-class backends for **Ollama** and the **OpenAI Chat Completions API format**. It does not have `grok` or `gemini` provider names or provider-specific adapters. However, Grok and Gemini can be configured through the existing OpenAI-compatible backend, so a separate adapter should not be needed for basic chat when the selected model supports the API features little-buddy uses. Tool calling is essential to the assistant, so verify the exact model and endpoint with the checks below before relying on it.

OpenAI compatibility is an API protocol choice, not a requirement to use OpenAI models. The HTTP client is `httpx`; no OpenAI Python SDK is used.

## Current model capabilities

`core/llm.py` defines a provider interface with two operations: `chat(...)` and `embed(...)`. `LLM_PROVIDER` selects an entry in `PROVIDERS`; currently the only values are `ollama` and `openai`.

Both backends are expected to provide:

- Text chat using system, user, assistant and tool messages.
- Function/tool calling with JSON-schema tool definitions, returned arguments and a continuation turn after little-buddy runs the tool.
- A normal text response, temperature and an optional output-token limit.
- Optional token usage in the response. Only Ollama reports prompt-read and generation timings here.
- Optional embeddings. If `EMBED_MODEL` is set, memory search calls the same provider's embeddings operation; without it, keyword/full-text search remains available.
- A startup/status check. Ollama checks downloaded/loaded models; the OpenAI-compatible backend requests `/models` and reports whether the configured model is listed (or a connection/status error if the endpoint is unavailable).

The provider sits under a shared agent loop (`core/agent.py`): it builds a context-budgeted prompt and tool schemas, allows up to `LLM_MAX_STEPS` model/tool rounds, executes tools locally, and stores the exchange. Planner, reminders, memory, web, check-ins and other skills are app-side features; they do not depend on a particular vendor, but they do depend on reliable tool calls.

Current boundaries to keep in mind:

- Direct vendor-specific APIs are not implemented. The OpenAI backend posts to `/chat/completions`, `/embeddings` and `/models` at `LLM_BASE_URL`.
- Tool-call request/response conversion follows the OpenAI Chat Completions function format. An API that only offers a different protocol (for example, an endpoint requiring a Responses-style request/continuation flow) needs an adapter.
- Incoming chat content is text-only today. The provider accepts string message content; Discord attachments/images and multimodal message conversion are not wired into the agent.
- Streaming, audio, image generation, native search/grounding, provider-specific safety settings and reasoning controls are not exposed by the shared interface. The `think` option is used by Ollama; the OpenAI-compatible backend does not send it.
- `LLM_FAST_MODEL` is configured and displayed, but current call sites do not use it for side jobs. Adding a provider does not make a separate fast model active.
- Cloud providers receive the prompt, conversation history, memories and tool results sent by the agent. The local-only privacy description applies only when configured for a local endpoint.

## Configure Grok or Gemini now

No code change is needed to try either provider via its OpenAI-compatible Chat Completions endpoint. Set `LLM_PROVIDER=openai` in `.env`; use `LLM_MODEL` (not `OLLAMA_MODEL`) for the vendor's model ID and set its API key in `LLM_API_KEY`.

### Grok (xAI)

```dotenv
LLM_PROVIDER=openai
LLM_BASE_URL=https://api.x.ai/v1
LLM_API_KEY=your-xai-api-key
LLM_MODEL=your-tool-capable-grok-model
EMBED_MODEL=
```

xAI documents its OpenAI client-compatible Chat Completions endpoint at `https://api.x.ai/v1`. The xAI API key can be supplied as the bearer key used by the existing backend. Check the xAI model documentation for the chosen model's Chat Completions and function-calling support; if it only supports a different tool protocol, it will need an adapter. Keep embeddings disabled unless the selected xAI endpoint/model supports the `/embeddings` request used here.

### Gemini (Google AI Studio Gemini API)

```dotenv
LLM_PROVIDER=openai
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_API_KEY=your-gemini-api-key
LLM_MODEL=your-tool-capable-gemini-model
EMBED_MODEL=
```

Google documents this OpenAI-compatible endpoint for chat completions, function calling, model listing and embeddings. Compatibility is currently documented as beta and may not cover every OpenAI option. Embeddings can be enabled by setting `EMBED_MODEL` to a supported Gemini embedding model after verifying that endpoint in the deployed account.

In either case, keep `LLM_TIMEOUT` appropriate for the chosen model, and choose a `LLM_CONTEXT` that fits the provider's context and account limits. The default timeout is five minutes. The `.env.example` and README currently describe OpenAI-compatible URLs generically rather than documenting these vendor-specific values.

## Integration steps

### Use the existing backend (recommended first step)

1. Create an API key and check account access, model availability, context limits, and tool/function-calling support in the vendor's current API docs.
2. Configure `LLM_PROVIDER=openai`, `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` as above. Do not put a real key in the repository; `.env` is git-ignored.
3. Restart and run `!status`. Confirm the endpoint is reachable and, if it lists models, that the chosen model ID is present.
4. Smoke-test a plain reply, then a multi-step request that must call a tool (for example, ask Buddy to save a memory or add a reminder). Confirm it executes and then responds based on the tool result; chat-only success is not enough.
5. If enabling `EMBED_MODEL`, test memory add/search too. Check provider-specific quotas, pricing and data-retention settings before routine use.

If those checks pass, support is achieved through configuration: add provider-specific setup examples to `.env.example`/README if desired, but no runtime adapter is necessary.

### Add a first-class provider or native API integration

Implement this only if the compatibility endpoint is missing a required feature, requires special authentication/request fields, or does not implement the chat/function-call contract expected here:

1. Add a `Provider` subclass in `core/llm.py` (or a separate provider module), and register its name in `PROVIDERS`.
2. Convert neutral messages and tool schemas into the vendor's API format, then normalize its text, tool-call IDs/names/JSON arguments and usage into `Reply`. Ensure tool-result messages can be sent back in the next turn.
3. Implement `embed()` if the provider offers supported embeddings; otherwise make its lack of embedding support explicit so semantic memory can stay disabled instead of failing at runtime.
4. Implement a useful `status()` using the provider's health/model-list endpoint and provide actionable authentication/network errors.
5. Add any vendor settings as explicit config, without changing existing Ollama/OpenAI defaults. Avoid forwarding Ollama-only options such as `think` to APIs that do not support them.
6. Add isolated tests for message/tool conversion, tool-call response parsing, malformed arguments, usage handling, auth/status errors and embedding response parsing. Add a fake HTTP server or mocked `httpx` transport; do not require live API keys in the test suite.
7. Run the full test suite, then perform a credentialed smoke test with an actual model to verify repeated tool-call turns, token/context limits and latency. Document setup, limitations and cloud-data handling.

## References

- [xAI Chat Completions](https://docs.x.ai/docs/guides/chat-completions)
- [xAI function calling](https://docs.x.ai/docs/guides/function-calling)
- [Gemini API OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai)
