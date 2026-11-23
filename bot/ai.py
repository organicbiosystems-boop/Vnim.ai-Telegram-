"""Оценка новости и написание поста с помощью Claude."""
import logging
from typing import Literal

import anthropic
from pydantic import BaseModel

log = logging.getLogger(__name__)


# Модели, у которых есть серверный резерв на случай отказа (у Haiku его нет).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1"}


class Evaluation(BaseModel):
    is_duplicate: bool
    tier: Literal["breaking", "regular", "skip"]
    approve_probability: int
    reason: str
    post_title: str
    post_body: str


INSTRUCTIONS = """\
Ты — редактор русскоязычного Telegram-канала VNIM.AI о новостях искусственного интеллекта.
Тебе приходит одна новость (из RSS или от владельцев канала). Нужно:

1. Определить уровень (tier):
   - "breaking" — по-настоящему крупное событие, которое точно нужно опубликовать сразу:
     выход новой флагманской модели или крупной версии у ведущих лабораторий
     (OpenAI, Anthropic, Google/DeepMind, Meta, xAI, Mistral, DeepSeek, Alibaba/Qwen и т.п.),
     запуск крупного продукта, сделки/поглощения на миллиарды, громкие законы и регулирование.
     Пример: «вышла Fable 3.5». Ставь "breaking" редко — только если сомнений нет.
   - "regular" — интересная, но нишевая новость: инструменты, библиотеки, исследования,
     оптимизации, стартапы, обновления второстепенных продуктов.
     Пример: «Ускорение локальных ИИ-моделей от 30% до 5 раз. Разработчик представил скилл
     для оптимизации инференса».
   - "skip" — не про ИИ, реклама, вакансии, мнения без новости, мелочь, повтор уже вышедшего.
2. is_duplicate = true, если это та же история, что уже есть в списке «Недавние посты».
3. approve_probability — целое число 0..100: насколько вероятно, что владельцы канала одобрят
   эту новость к публикации. Опирайся на их прошлые решения (ниже) — это главный сигнал.
   Если решений пока мало, оценивай по общей интересности для русскоязычной ИИ-аудитории.
4. reason — 1 короткое предложение по-русски, почему такая оценка.
5. post_title и post_body — готовый пост на русском:
   - post_title: цепкий информативный заголовок до 100 символов, без кликбейта, можно один
     эмодзи в начале.
   - post_body: 2–5 предложений, только факты из новости: что произошло, почему важно,
     ключевые цифры. Без воды, без хэштегов, без ссылок, без подписи — их добавим сами.
   - Названия продуктов и компаний оставляй в оригинальном написании.
   Если tier = "skip", поля поста можно оставить пустыми.
"""


def _build_system(approved: list[str], rejected: list[str], recent: list[str]) -> str:
    def block(title: str, rows: list[str]) -> str:
        body = "\n".join(f"- {r}" for r in rows) if rows else "(пока нет)"
        return f"## {title}\n{body}"

    return "\n\n".join([
        INSTRUCTIONS,
        "# Прошлые решения владельцев канала",
        block("Одобрены к публикации", approved),
        block("Отклонены", rejected),
        block("Недавние посты (последние 3 дня, для поиска дублей)", recent),
    ])


class Editor:
    def __init__(self, model: str, effort: str):
        self.client = anthropic.AsyncAnthropic()
        self.model = model
        self.effort = effort

    async def evaluate(
        self,
        *,
        title: str,
        source: str,
        url: str,
        summary: str,
        approved: list[str],
        rejected: list[str],
        recent: list[str],
    ) -> Evaluation | None:
        system = _build_system(approved, rejected, recent)
        user = (
            f"Источник: {source}\n"
            f"Ссылка: {url or '-'}\n"
            f"Заголовок: {title}\n\n"
            f"Текст:\n{summary or '(нет текста)'}"
        )
        params = dict(
            model=self.model,
            max_tokens=8000,
            output_config={"effort": self.effort},
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=Evaluation,
        )
        try:
            if self.model in FALLBACK_MODELS:
                # Если модель откажется отвечать по соображениям безопасности,
                # API сам повторит запрос на резервной модели.
                resp = await self.client.beta.messages.parse(
                    **params,
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            else:
                resp = await self.client.messages.parse(**params)
        except anthropic.RateLimitError as e:
            log.warning("Claude: превышен лимит запросов: %s", e)
            return None
        except anthropic.APIStatusError as e:
            log.error("Claude API ошибка %s: %s", e.status_code, e.message)
            return None
        except anthropic.APIConnectionError as e:
            log.error("Claude: нет соединения: %s", e)
            return None

        if resp.stop_reason == "refusal":
            log.warning("Claude отказался оценивать новость: %s", title)
            return None
        ev = resp.parsed_output
        if ev is None:
            log.warning("Claude вернул неразборчивый ответ для: %s", title)
            return None
        ev.approve_probability = max(0, min(100, int(ev.approve_probability)))
        return ev
