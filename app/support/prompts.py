"""
Промпты для OpenAI (формулировка ответов).
Используется только для фразы — не для принятия решений.
FAQ загружается из docs/ai-support/faq.md и передаётся в prompt при unclear intent.
"""
from pathlib import Path
from typing import Optional

SYSTEM_PROMPT = """Ты — помощник первой линии поддержки бота MaxNet VPN в Telegram.
Твои правила:
- Отвечай кратко и по делу.
- Не выдумывай факты о подписке, оплате или статусе VPN.
- Если данных недостаточно — скажи об этом и предложи обратиться в поддержку.
- Не обещай того, что не проверено (например, статус платежа).
- Пиши на русском, дружелюбно, без лишних слов.
- Если вопрос не по VPN — вежливо направь к оператору."""

USER_TEMPLATE = """Пользователь написал: "{user_message}"

Контекст (только для справки, не меняй факты):
{context_summary}

Дай короткий ответ (1–3 предложения) для поддержки. Если не уверен — предложи обратиться в поддержку."""

USER_TEMPLATE_WITH_FAQ = """Используй базу знаний ниже только как справку. Не выдумывай факты.

--- База знаний (FAQ) ---
{faq_text}
--- Конец FAQ ---

Пользователь написал: "{user_message}"

Контекст пользователя (только для справки, не меняй факты):
{context_summary}

Дай короткий ответ (1–3 предложения) для поддержки, опираясь на FAQ где уместно. Если не уверен — предложи обратиться в поддержку."""

_FAQ_CACHE: Optional[str] = None


def get_faq_text() -> str:
    """
    Загружает текст FAQ из docs/ai-support/faq.md.
    Результат кэшируется в памяти. При отсутствии файла или ошибке возвращает пустую строку.
    """
    global _FAQ_CACHE
    if _FAQ_CACHE is not None:
        return _FAQ_CACHE
    try:
        # app/support/prompts.py -> project root = parent.parent
        base = Path(__file__).resolve().parent.parent.parent
        faq_path = base / "docs" / "ai-support" / "faq.md"
        if not faq_path.is_file():
            _FAQ_CACHE = ""
            return ""
        raw = faq_path.read_text(encoding="utf-8")
        _FAQ_CACHE = (raw or "").strip()
        return _FAQ_CACHE
    except Exception:
        _FAQ_CACHE = ""
        return ""


def build_user_prompt(
    user_message: str,
    context_summary: str,
    faq_text: Optional[str] = None,
) -> str:
    msg = user_message[:500]
    ctx = context_summary[:800] or "Нет данных"
    if faq_text and faq_text.strip():
        faq_trimmed = faq_text[:4000].strip()
        return USER_TEMPLATE_WITH_FAQ.format(
            faq_text=faq_trimmed,
            user_message=msg,
            context_summary=ctx,
        )
    return USER_TEMPLATE.format(
        user_message=msg,
        context_summary=ctx,
    )
