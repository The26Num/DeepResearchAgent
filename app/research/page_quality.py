"""Conservative detection of empty shells and explicit browser challenge pages."""

import re


MIN_BODY_CHARACTERS = 16
_SHELL = re.compile(
    r"^(?:just a moment[.!…\s]|checking your browser|verify (?:you are|that you are) human|"
    r"access denied[.!:\s]|please (?:enable|turn on) javascript|enable javascript to|"
    r"you need to enable javascript|请(?:开启|启用)\s*javascript|正在加载[.。…\s]|"
    r"加载中[.。…\s]|请完成(?:安全验证|验证码))", re.I,
)


def page_body_error(text: str) -> str | None:
    if not text.strip():
        return "empty HTML body"
    count = len(re.sub(r"[\W_]", "", text, flags=re.UNICODE))
    if count < MIN_BODY_CHARACTERS:
        return f"insufficient HTML body: meaningful_chars={count}, minimum={MIN_BODY_CHARACTERS}"
    if len(text) < 600 and _SHELL.search(text.strip()):
        return "HTML body is a browser challenge or JavaScript shell"
    return None
