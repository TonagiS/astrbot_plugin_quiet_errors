"""别把 LLM/agent 的原始报错发进群。

AstrBot 出错时会直接 `await event.send(...)` 把 "Error occurred while processing
agent request: ..." 甩到会话里（internal.py），这条路绕过了结果装饰阶段，
所以只能在这一层拦：包一层 AstrMessageEvent.send。

规则：
- 群聊里命中报错特征 → 不发送，只写日志 + 用桥接接口私聊给管理员。
- 私聊（管理员）→ 照常发，方便排查。
- 兼容正常回复：只有命中特征才拦。
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

PLUGIN_DIR = Path(__file__).resolve().parent
LOG_PATH = PLUGIN_DIR / "data" / "quiet_errors.log"
STATE_FILE = PLUGIN_DIR / "data" / "hooked.flag"
BRIDGE_CFG = PLUGIN_DIR / "bridge.json"

DEFAULT_PATTERNS = [
    r"Error occurred while processing agent",
    r"Traceback \(most recent call last\)",
    r"^\s*(Error|Exception)\s*[:：]",
    r"status:\s*(4\d\d|5\d\d)",
    r"rate limit",
    r"invalid api key",
    r"insufficient_quota",
    r"APIConnectionError",
    r"Timeout",
    r"发生错误，请联系管理员",
    r"错误[:：]\s*",
    r"LLM\s*响应错误",
    r"All chat models failed",
    r"BadRequestError",
    r"inappropriate content",
    r"Error code:\s*\d{3}",
    r"模型调用失败",
    r"服务暂时不可用",
]


def _log(msg: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 2 * 1024 * 1024:
            LOG_PATH.write_text("", encoding="utf-8")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def _plain(message) -> str:
    chain = getattr(message, "chain", None)
    if chain is None:
        return str(message)
    out = []
    for comp in chain:
        text = getattr(comp, "text", None)
        if text:
            out.append(str(text))
    return " ".join(out)


def _notify_admin(text: str) -> None:
    try:
        if not BRIDGE_CFG.exists():
            return
        cfg = json.loads(BRIDGE_CFG.read_text(encoding="utf-8-sig"))
        body = json.dumps(
            {"text": text[:1500]}, ensure_ascii=False
        ).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{cfg.get('port', 8721)}/notify",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Token": str(cfg.get("token", "")),
            },
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"quiet_errors: 通知管理员失败 {exc}")


class QuietErrors(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._patched = False

    def _cfg(self, key: str, default=None):
        try:
            value = self.config.get(key, default)
        except Exception:  # noqa: BLE001
            value = default
        return default if value is None else value

    def _patterns(self) -> list[re.Pattern]:
        raw = self._cfg("patterns", DEFAULT_PATTERNS) or []
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        merged = list(dict.fromkeys(list(DEFAULT_PATTERNS) + [str(p) for p in raw]))
        return [re.compile(p, re.IGNORECASE) for p in merged]

    def _looks_like_error(self, text: str) -> bool:
        if not text:
            return False
        if len(text) > 1500:  # 长文一般不是报错串
            return False
        return any(p.search(text) for p in self._patterns())

    @filter.on_astrbot_loaded()
    async def install_hook(self):
        if self._patched:
            return
        pats = self._patterns()
        only_groups = bool(self._cfg("groups_only", True))
        max_keep = int(self._cfg("notify_admin", True))

        def looks_like(text: str) -> bool:
            return any(p.search(text) for p in pats)

        original = getattr(AstrMessageEvent, "send", None)
        if original is None:
            logger.warning("quiet_errors: 找不到 AstrMessageEvent.send，跳过")
            return

        async def guarded_send(self, message):  # type: ignore[no-untyped-def]
            try:
                text = _plain(message)
                is_group = "GroupMessage" in str(self.unified_msg_origin)
                if text and looks_like(text) and (is_group or not only_groups):
                    umo = str(self.unified_msg_origin)
                    _log(f"SUPPRESS {umo} :: {text[:300]}")
                    if max_keep:
                        _notify_admin(f"[出错·已拦在群里]\n{umo}\n{text[:600]}")
                    return None
            except Exception as exc:  # noqa: BLE001
                _log(f"guard error: {exc}")
            return await original(self, message)

        AstrMessageEvent.send = guarded_send  # type: ignore[assignment]

        # 第二条路：主动发送/管线推送走的是 context.send_message(umo, chain)
        try:
            from astrbot.api.star import Context

            original_send_message = getattr(Context, "send_message", None)
            if original_send_message is not None:

                async def guarded_send_message(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                    session = kwargs.get("session", args[0] if args else None)
                    message_chain = kwargs.get(
                        "message_chain", args[1] if len(args) > 1 else None
                    )
                    try:
                        umo = str(getattr(session, "unified_msg_origin", session))
                        text = _plain(message_chain)
                        if (
                            text
                            and looks_like(text)
                            and ("GroupMessage" in umo or not only_groups)
                        ):
                            _log(f"SUPPRESS(ctx) {umo} :: {text[:300]}")
                            if max_keep:
                                _notify_admin(f"[出错·已拦在群里]\n{umo}\n{text[:600]}")
                            return True
                    except Exception as exc:  # noqa: BLE001
                        _log(f"ctx guard error: {exc}")
                    return await original_send_message(self, *args, **kwargs)

                Context.send_message = guarded_send_message  # type: ignore[assignment]
                _log("hooked Context.send_message")
        except Exception as exc:  # noqa: BLE001
            _log(f"ctx hook failed: {exc}")

        # 第三条路：适配器层直接发（插件绕过 event/context 时）
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )

            original_adapter_send = getattr(AiocqhttpMessageEvent, "send_message", None)
            if original_adapter_send is not None:

                async def guarded_adapter_send(cls, *args, **kwargs):  # type: ignore[no-untyped-def]
                    bot = kwargs.get("bot", args[0] if args else None)
                    message_chain = kwargs.get(
                        "message_chain", args[1] if len(args) > 1 else None
                    )
                    is_group = kwargs.get("is_group", args[3] if len(args) > 3 else False)
                    session_id = kwargs.get(
                        "session_id", args[4] if len(args) > 4 else None
                    )
                    try:
                        text = _plain(message_chain)
                        if (
                            text
                            and looks_like(text)
                            and (is_group or not only_groups)
                        ):
                            who = f"group={session_id}" if is_group else f"sess={session_id}"
                            _log(f"SUPPRESS(adapter) {who} :: {text[:300]}")
                            if max_keep:
                                _notify_admin(f"[出错·已拦在群里]\n{who}\n{text[:600]}")
                            return
                    except Exception as exc:  # noqa: BLE001
                        _log(f"adapter guard error: {exc}")
                    return await original_adapter_send(*args, **kwargs)

                AiocqhttpMessageEvent.send_message = classmethod(guarded_adapter_send)  # type: ignore[assignment]
                _log("hooked AiocqhttpMessageEvent.send_message")
        except Exception as exc:  # noqa: BLE001
            _log(f"adapter hook failed: {exc}")

        self._patched = True
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        logger.info("quiet_errors: 已挂上 send 拦截")
        _log("hooked AstrMessageEvent.send")
