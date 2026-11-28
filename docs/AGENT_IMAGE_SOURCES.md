# Agent image provenance

Images in `app/static/agent-icons.json` are bundled locally; the UI does not contact an image CDN or account/avatar service.

- Orca agent PNG assets: https://github.com/stablyai/orca/tree/main/src/shared/agent-icons — MIT, copyright Lovecast Inc. The license is preserved in ORCA_ASSET_LICENSE.txt.
- LobeHub brand SVG assets (Claude, OpenAI, Cursor, Gemini, Grok, OpenCode, GitHub Copilot, Pi, Ollama, DeepSeek, Qwen, Mistral, MiniMax and Meta): https://github.com/lobehub/lobe-icons/tree/master/packages/static-svg/icons — MIT, copyright LobeHub. The license is preserved in LOBE_ASSET_LICENSE.txt.
- Brand marks identify third-party tools and do not imply endorsement. Unrecognized runtime IDs use a visibly generic initial avatar; no unofficial mark is presented as their logo.
- Catalogue IDs/names are a snapshot of Orca's built-in TUI catalogue on 2026-11-27, plus Gemini CLI from the official supported-agent documentation. Every additional locally reported agent is merged at runtime. This is a complete directory within that scope, not a claim to list every AI product or model in existence.

- Aider SVG wordmark: https://github.com/Aider-AI/aider/blob/main/aider/website/assets/logo.svg — Apache-2.0, license preserved in AIDER_ASSET_LICENSE.txt.
