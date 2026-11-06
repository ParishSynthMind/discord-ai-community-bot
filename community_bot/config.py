"""Settings: the YAML file for the community, the environment (or the macOS Keychain)
for anything secret. Secrets are only read when they are needed and never logged."""
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent

# secret name -> default Keychain item (account "neo" unless KEYCHAIN_ACCOUNT says otherwise).
# Each can be overridden with <NAME>_KEYCHAIN, or given directly as the env var <NAME>.
SECRET_KEYCHAIN_ITEMS = {
    "DISCORD_BOT_TOKEN": "discord-bot-token",
    "ANTHROPIC_API_KEY": "anthropic-api-key",
    "GEMINI_API_KEY": "gemini-api-key",
    "STRIPE_WEBHOOK_SECRET": "discord-bot-stripe-whsec",
    "LEMONSQUEEZY_SIGNING_SECRET": "discord-bot-lemonsqueezy-secret",
    "PADDLE_WEBHOOK_SECRET": "discord-bot-paddle-secret",
    "CALENDLY_SIGNING_KEY": "discord-bot-calendly-key",
    "HOOK_SIGNING_SECRET": "discord-bot-hook-secret",
    "ADMIN_TOKEN": "discord-bot-admin-token",
}


def keychain_lookup(item: str, account: Optional[str] = None) -> Optional[str]:
    """Read a generic password from the macOS Keychain: the named account first (default
    KEYCHAIN_ACCOUNT or "neo"), then the item under any account. None if missing or not on a Mac."""
    account = account or os.environ.get("KEYCHAIN_ACCOUNT", "neo")
    for args in (["-a", account, "-s", item], ["-s", item]):
        try:
            out = subprocess.run(["security", "find-generic-password", *args, "-w"],
                                 capture_output=True, text=True, timeout=10)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        value = out.stdout.strip()
        if out.returncode == 0 and value:
            return value
    return None


class Secrets:
    """Env var first, then the Keychain. Values are cached in memory only."""

    def __init__(self, env=None, keychain: Optional[Callable[[str], Optional[str]]] = None):
        self.env = os.environ if env is None else env
        self.keychain = keychain_lookup if keychain is None else keychain
        self._cache: Dict[str, Optional[str]] = {}

    def get(self, name: str) -> Optional[str]:
        if name in self._cache:
            return self._cache[name]
        value = self.env.get(name) or None
        if value is None:
            item = self.env.get(f"{name}_KEYCHAIN") or SECRET_KEYCHAIN_ITEMS.get(name)
            if item and self.env.get("NO_KEYCHAIN") != "1":
                value = self.keychain(item)
        self._cache[name] = value
        return value

    def require(self, name: str) -> str:
        value = self.get(name)
        if not value:
            item = self.env.get(f"{name}_KEYCHAIN") or SECRET_KEYCHAIN_ITEMS.get(name, "?")
            raise RuntimeError(f"{name} is not set (env var {name}, or Keychain item '{item}')")
        return value


@dataclass
class AISettings:
    provider: str = "anthropic"
    model: str = ""
    max_tokens: int = 900
    daily_cap_total: int = 200
    daily_cap_per_user: int = 10
    max_sections: int = 5
    handover_when_not_found: bool = True
    public_answers: bool = True


@dataclass
class Settings:
    community_name: str
    guild_id: str
    premium_role_id: str
    call_booked_role_id: str = ""
    mod_channel_id: str = ""
    bookings_channel_id: str = ""
    members_intent: bool = False
    ai: AISettings = field(default_factory=AISettings)
    kb_dir: Path = ROOT / "knowledge_base"
    plan_roles: Dict[str, str] = field(default_factory=dict)
    past_due_grace_days: int = 3
    checkout_url: str = ""
    webhook_tolerance: int = 300
    hook_url: str = ""
    hook_events: List[str] = field(default_factory=list)
    booking_url: str = ""
    calendly_question: str = "Discord username"
    db_path: Path = ROOT / "community.db"
    # Where the Discord API and gateway live. Only changed to point at the fake in tests.
    api_base: str = "https://discord.com/api/v10"
    gateway_url: str = ""

    def role_for_plan(self, plan: Optional[str]) -> str:
        return self.plan_roles.get(plan or "", self.premium_role_id)

    def all_paid_roles(self) -> set:
        return {self.premium_role_id, *self.plan_roles.values()}


def load_settings(path: Optional[Path] = None, env=None) -> Settings:
    env = os.environ if env is None else env
    path = Path(path or env.get("BOT_CONFIG") or ROOT / "config" / "bot.yaml")
    raw = yaml.safe_load(path.read_text()) or {}
    ai_raw = raw.get("ai") or {}
    ai = AISettings(**{k: v for k, v in ai_raw.items() if k in AISettings.__dataclass_fields__})
    if env.get("AI_PROVIDER"):
        ai.provider = env["AI_PROVIDER"]
    if not ai.model or (env.get("AI_PROVIDER") and not env.get("AI_MODEL")):
        ai.model = {"anthropic": "claude-sonnet-5-5", "gemini": "gemini-3.5-flash-lite"}.get(ai.provider, ai.model)
    if env.get("AI_MODEL"):
        ai.model = env["AI_MODEL"]
    roles = raw.get("roles") or {}
    channels = raw.get("channels") or {}
    membership = raw.get("membership") or {}
    hooks = raw.get("hooks") or {}
    calendly = raw.get("calendly") or {}
    kb = Path(raw.get("knowledge_base") or "knowledge_base")
    settings = Settings(
        community_name=raw.get("community_name", "Community"),
        guild_id=str(env.get("DISCORD_GUILD_ID") or raw["guild_id"]),
        premium_role_id=str(env.get("PREMIUM_ROLE_ID") or roles["premium"]),
        call_booked_role_id=str(env.get("CALL_BOOKED_ROLE_ID") or roles.get("call_booked") or ""),
        mod_channel_id=str(env.get("MOD_CHANNEL_ID") or channels.get("mod_queue") or ""),
        bookings_channel_id=str(env.get("BOOKINGS_CHANNEL_ID") or channels.get("bookings") or ""),
        members_intent=bool((raw.get("intents") or {}).get("members", False)),
        ai=ai,
        kb_dir=kb if kb.is_absolute() else (path.parent.parent / kb),
        plan_roles={str(k): str(v) for k, v in (membership.get("plans") or {}).items()},
        past_due_grace_days=int(membership.get("past_due_grace_days", 3)),
        checkout_url=str(membership.get("checkout_url") or ""),
        webhook_tolerance=int(membership.get("webhook_tolerance_seconds", 300)),
        hook_url=str(env.get("HOOK_URL") or hooks.get("url") or ""),
        hook_events=list(hooks.get("events") or []),
        booking_url=str(calendly.get("booking_url") or ""),
        calendly_question=str(calendly.get("discord_question") or "Discord username"),
        db_path=Path(env.get("BOT_DB") or ROOT / "community.db"),
        api_base=env.get("DISCORD_API_BASE") or "https://discord.com/api/v10",
        gateway_url=env.get("DISCORD_GATEWAY_URL") or "",
    )
    if env.get("MEMBERS_INTENT"):
        settings.members_intent = env["MEMBERS_INTENT"] == "1"
    return settings
