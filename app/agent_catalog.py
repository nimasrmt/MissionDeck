"""Built-in Orca agent directory, merged with live local capability evidence."""
from __future__ import annotations
from app.free_models import selected_route

SOURCE = "https://github.com/stablyai/orca/blob/main/src/shared/agent-launch/catalog/tui-agent-display-names.ts"
NAMES = {
    "claude":"Claude", "claude-agent-teams":"Claude Agent Teams", "codebuddy":"CodeBuddy",
    "openclaude":"OpenClaude", "codex":"Codex", "devin":"Devin", "ante":"Ante", "trae":"Trae",
    "muse":"Muse", "dsh":"DeepSeek Harness", "zcode":"ZCode", "autohand":"Autohand Code",
    "opencode":"OpenCode", "opencode2":"OpenCode 2", "mimo-code":"MiMo Code", "pi":"Pi", "omp":"OMP",
    "prime-agent":"Prime Agent", "qoder":"Qoder CLI", "qoder-cn":"Qoder CLI China", "antigravity":"Antigravity",
    "aider":"Aider", "goose":"Goose", "amp":"Amp", "kilo":"Kilocode", "kiro":"Kiro", "crush":"Charm",
    "aug":"Auggie", "cline":"Cline", "codebuff":"Codebuff", "freebuff":"Freebuff", "command-code":"Command Code",
    "continue":"Continue", "cursor":"Cursor", "droid":"Droid", "kimi":"Kimi", "mistral-vibe":"Mistral Vibe",
    "qwen-code":"Qwen Code", "rovo":"Rovo Dev", "hermes":"Hermes", "openclaw":"OpenClaw", "copilot":"GitHub Copilot",
    "grok":"Grok", "jcode":"Jcode", "gemini":"Gemini CLI",
}
ICONS = {"codex":"openai", "claude-agent-teams":"claude", "opencode2":"opencode", "omp":"pi", "gemini":"gemini"}


def catalogue(capabilities: dict, free_models: dict) -> dict:
    live = {a["id"]: a for a in capabilities.get("agents", [])}
    names = {**NAMES, **{key: NAMES.get(key, row.get("name") or key) for key, row in live.items()}}
    agents=[]
    for aid,name in sorted(names.items(), key=lambda item:item[1].casefold()):
        row=live.get(aid, {})
        agents.append({"id":aid,"name":name,"icon_key":ICONS.get(aid,aid),
                       "installed":row.get("installed",False), "eligible":row.get("eligible",False),
                       "billing":row.get("billing","unverified"), "quota":row.get("quota"),
                       "model":row.get("model") or "unknown", "evidence":row.get("evidence",[]),
                       "reason":row.get("reason") or "Not reported installed by the local Orca runtime"})
    models=[{**m,"agent":"opencode","icon_key":"opencode","eligible":any(a["id"]=="opencode" and a["installed"] for a in agents) and bool(selected_route(free_models,m.get("id")))}
            for m in free_models.get("models",[]) if isinstance(m,dict)]
    return {"agents":agents,"models":models,"checked_at":capabilities.get("checked_at"),
            "catalogue_source":SOURCE,"scope":"Orca built-ins plus every agent reported by this runtime; verified local free models only"}
