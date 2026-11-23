"""VNIM.AI news bot: собирает ИИ-новости, публикует важные сразу,
остальные отправляет вам с партнёром на одобрение и учится на ваших решениях."""
import asyncio
import html
import logging
import time

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .ai import Editor
from .config import Config, load_config
from .db import DB
from .learning import agreement, calibrate_threshold
from .sources import fetch_all, load_feeds

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("vnim")

EXAMPLES_PER_CLASS = 25


class NewsBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = DB(cfg.db_path)
        self.editor = Editor(cfg.claude_model, cfg.claude_effort)
        self._poll_lock = asyncio.Lock()

    # ------------------------------------------------------------------ utils
    def is_admin(self, update: Update) -> bool:
        u = update.effective_user
        return u is not None and u.id in self.cfg.admin_ids

    def threshold(self) -> int | None:
        return calibrate_threshold(
            self.db.labeled_predictions(),
            self.cfg.learning_min_decisions,
            self.cfg.learning_target_precision,
        )

    def channel_html(self, row) -> str:
        parts = [f"<b>{html.escape(row['post_title'] or row['title'] or '')}</b>"]
        if row["post_body"]:
            parts.append(html.escape(row["post_body"]))
        if self.cfg.include_source_link and row["url"]:
            parts.append(f'<a href="{html.escape(row["url"], quote=True)}">Источник</a>')
        parts.append(f"<b>{html.escape(self.cfg.signature)}</b>")
        return "\n\n".join(parts)

    def admin_html(self, row, header: str) -> str:
        thr = self.threshold()
        thr_txt = f"{thr}%" if thr is not None else "ещё учусь"
        prob = row["approve_prob"]
        meta = [
            header,
            f"Источник: {html.escape(row['source'] or '-')}",
        ]
        if prob is not None:
            meta.append(f"Прогноз «одобрите»: <b>{prob}%</b> · порог автопубликации: {thr_txt}")
        if row["reason"]:
            meta.append(f"<i>{html.escape(row['reason'])}</i>")
        return "\n".join(meta) + "\n\n———\n\n" + self.channel_html(row)

    @staticmethod
    def review_kb(news_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Опубликовать", callback_data=f"pub:{news_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"rej:{news_id}"),
            ],
            [InlineKeyboardButton("✏️ Изменить текст", callback_data=f"edit:{news_id}")],
        ])

    @staticmethod
    def published_kb(news_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Удалить из канала", callback_data=f"del:{news_id}")],
        ])

    async def notify_admins(self, bot, news_id: int, text: str, kb=None) -> None:
        for admin_id in self.cfg.admin_ids:
            try:
                msg = await bot.send_message(
                    admin_id, text, parse_mode=ParseMode.HTML, reply_markup=kb,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                self.db.add_admin_msg(news_id, admin_id, msg.message_id)
            except TelegramError as e:
                log.warning("Не удалось написать админу %s: %s (он нажал /start у бота?)", admin_id, e)

    async def broadcast(self, bot, text: str) -> None:
        for admin_id in self.cfg.admin_ids:
            try:
                await bot.send_message(admin_id, text, parse_mode=ParseMode.HTML)
            except TelegramError as e:
                log.warning("Не удалось написать админу %s: %s", admin_id, e)

    async def update_admin_msgs(self, bot, news_id: int, footer: str, kb=None) -> None:
        """Обновляет карточку новости у обоих админов (чтобы не одобрить дважды)."""
        row = self.db.get(news_id)
        text = self.admin_html(row, footer)
        for m in self.db.admin_msgs(news_id):
            try:
                await bot.edit_message_text(
                    text, chat_id=m["chat_id"], message_id=m["message_id"],
                    parse_mode=ParseMode.HTML, reply_markup=kb,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except TelegramError as e:
                if "not modified" not in str(e).lower():
                    log.debug("edit_message_text: %s", e)

    async def publish(self, bot, news_id: int, mode: str) -> bool:
        row = self.db.get(news_id)
        preview = (
            LinkPreviewOptions(url=row["url"], prefer_large_media=True)
            if self.cfg.include_source_link and row["url"]
            else LinkPreviewOptions(is_disabled=True)
        )
        try:
            msg = await bot.send_message(
                self.cfg.channel_id, self.channel_html(row),
                parse_mode=ParseMode.HTML, link_preview_options=preview,
            )
        except TelegramError as e:
            log.error("Не удалось опубликовать в канал: %s", e)
            await self.broadcast(
                bot,
                f"⚠️ Не удалось опубликовать в {html.escape(self.cfg.channel_id)}: "
                f"{html.escape(str(e))}\nПроверьте, что бот — админ канала с правом публикации.",
            )
            return False
        self.db.update(
            news_id, status="published", publish_mode=mode,
            channel_message_id=msg.message_id, published_at=time.time(),
        )
        return True

    # ------------------------------------------------------------- pipeline
    async def evaluate(self, news_id: int):
        row = self.db.get(news_id)
        approved = [r["post_title"] or r["title"] for r in self.db.examples(1, EXAMPLES_PER_CLASS)]
        rejected = [r["post_title"] or r["title"] for r in self.db.examples(0, EXAMPLES_PER_CLASS)]
        recent = [t for t in self.db.recent_titles() if t != (row["post_title"] or row["title"])]
        ev = await self.editor.evaluate(
            title=row["title"], source=row["source"], url=row["url"],
            summary=row["raw_summary"], approved=approved, rejected=rejected, recent=recent,
        )
        if ev is None:
            self.db.update(news_id, status="error")
            return None
        self.db.update(
            news_id, tier=ev.tier, approve_prob=ev.approve_probability, reason=ev.reason,
            post_title=ev.post_title.strip() or None, post_body=ev.post_body.strip() or None,
        )
        return ev

    async def process(self, bot, news_id: int) -> None:
        ev = await self.evaluate(news_id)
        if ev is None:
            return
        if ev.tier == "skip" or ev.is_duplicate or not ev.post_title.strip():
            self.db.update(news_id, status="skipped")
            return

        thr = self.threshold()
        if ev.tier == "breaking" and self.cfg.auto_publish_breaking:
            if await self.publish(bot, news_id, "auto_breaking"):
                await self.notify_admins(
                    bot, news_id,
                    self.admin_html(self.db.get(news_id), "🔴 <b>Срочная — опубликовано автоматически</b>"),
                    self.published_kb(news_id),
                )
            return
        if thr is not None and ev.approve_probability >= thr:
            if await self.publish(bot, news_id, "auto_learned"):
                # Пока вы не удалили пост — считаем, что вы его одобряете.
                self.db.update(news_id, human_label=1)
                await self.notify_admins(
                    bot, news_id,
                    self.admin_html(self.db.get(news_id), "🟢 <b>Опубликовано автоматически — бот уверен</b>"),
                    self.published_kb(news_id),
                )
            return

        self.db.update(news_id, status="pending")
        await self.notify_admins(
            bot, news_id,
            self.admin_html(self.db.get(news_id), "🟡 <b>Нужно ваше решение</b>"),
            self.review_kb(news_id),
        )

    async def poll(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._poll_lock.locked():
            return
        async with self._poll_lock:
            feeds = load_feeds(self.cfg.feeds_file)
            items = await fetch_all(feeds, self.cfg.max_age_hours)

            if self.db.get_kv("initialized") is None:
                # Первый запуск: не заваливаем вас старыми новостями.
                for it in items:
                    self.db.insert_news(it.guid, it.url, it.title, it.source, it.summary, "seen")
                self.db.set_kv("initialized", "1")
                await self.broadcast(
                    context.bot,
                    f"👋 Бот запущен. Подключено лент: {len(feeds)}. "
                    f"{len(items)} уже вышедших новостей пропущено — дальше буду присылать новые.",
                )
                return

            new = [it for it in items if not self.db.has_guid(it.guid)][: self.cfg.max_items_per_run]
            if new:
                log.info("Новых новостей: %d", len(new))
            for it in new:
                news_id = self.db.insert_news(it.guid, it.url, it.title, it.source, it.summary, "new")
                if news_id is not None:
                    try:
                        await self.process(context.bot, news_id)
                    except Exception:
                        log.exception("Ошибка обработки новости %s", it.url)
                        self.db.update(news_id, status="error")

    # ------------------------------------------------------------- handlers
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update):
            await update.message.reply_text(
                f"Это служебный бот канала VNIM.AI. Ваш id: {update.effective_user.id}"
            )
            return
        await update.message.reply_text(
            "Привет! Я присылаю новости для канала.\n\n"
            "🔴 Срочные (новые флагманские модели и т.п.) публикую сам.\n"
            "🟡 Остальные присылаю вам на решение.\n"
            "🟢 Когда научусь угадывать ваши решения, начну публиковать уверенные сам.\n\n"
            "Можно просто прислать мне текст или ссылку на новость — я оформлю пост на проверку.\n\n"
            "/stats — статистика и обучение\n/check — проверить ленты сейчас\n/cancel — отменить редактирование"
        )

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update):
            return
        s = self.db.stats()
        labeled = self.db.labeled_predictions()
        thr = self.threshold()
        agr = agreement(labeled)
        need = max(0, self.cfg.learning_min_decisions - len(labeled))
        if thr is not None:
            learn = f"✅ Обучен: публикую сам обычные новости с прогнозом ≥ {thr}%"
        elif need:
            learn = f"📚 Учусь: нужно ещё {need} ваших решений"
        else:
            learn = (f"📚 Учусь: решений достаточно, но пока не могу угадывать вас с точностью "
                     f"{int(self.cfg.learning_target_precision * 100)}%")
        await update.message.reply_text(
            f"📊 Статистика\n\n"
            f"Просмотрено новостей: {s['seen_total']}\n"
            f"Отсеяно как неинтересные: {s['skipped']}\n"
            f"Ждут решения: {s['pending']}\n"
            f"Опубликовано: {s['published']} "
            f"(срочные: {s['auto_breaking']}, сам по обучению: {s['auto_learned']}, вами: {s['manual']})\n"
            f"Ваших решений: одобрено {s['approved']}, отклонено {s['rejected']}\n"
            f"Совпадение прогноза с вами: {f'{agr:.0%}' if agr is not None else '—'}\n\n"
            f"{learn}"
        )

    async def cmd_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update):
            return
        await update.message.reply_text("🔎 Проверяю ленты…")
        await self.poll(context)
        await update.message.reply_text("Готово.")

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update):
            return
        context.user_data.pop("editing", None)
        await update.message.reply_text("Ок, отменено.")

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update):
            return
        text = update.message.text.strip()
        editing = context.user_data.pop("editing", None)
        if editing is not None:
            row = self.db.get(editing)
            if row is None or row["status"] != "pending":
                await update.message.reply_text("Эта новость уже обработана.")
                return
            title, _, body = text.partition("\n")
            self.db.update(editing, post_title=title.strip(), post_body=body.strip() or None)
            await self.notify_admins(
                context.bot, editing,
                self.admin_html(self.db.get(editing),
                                f"✏️ <b>Текст изменён ({html.escape(update.effective_user.first_name)})</b>"),
                self.review_kb(editing),
            )
            return

        # Новость, присланная вручную
        url = next((w for w in text.split() if w.startswith(("http://", "https://"))), "")
        first_line = text.split("\n", 1)[0][:200]
        news_id = self.db.insert_news(
            f"manual:{update.effective_user.id}:{time.time()}", url, first_line,
            f"прислал(а) {update.effective_user.first_name}", text, "new",
        )
        await update.message.reply_text("✍️ Готовлю пост…")
        ev = await self.evaluate(news_id)
        if ev is None or not ev.post_title.strip():
            await update.message.reply_text("Не получилось оформить пост, попробуйте прислать больше текста.")
            return
        self.db.update(news_id, status="pending")
        await self.notify_admins(
            context.bot, news_id,
            self.admin_html(self.db.get(news_id), "🟡 <b>Прислано вручную — нужно решение</b>"),
            self.review_kb(news_id),
        )

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not self.is_admin(update):
            await q.answer("Нет доступа")
            return
        action, _, raw_id = q.data.partition(":")
        news_id = int(raw_id)
        row = self.db.get(news_id)
        who = html.escape(update.effective_user.first_name or "админ")
        if row is None:
            await q.answer("Новость не найдена")
            return

        if action in ("pub", "rej", "edit") and row["status"] != "pending":
            await q.answer("Уже обработано")
            return

        if action == "pub":
            await q.answer("Публикую…")
            if await self.publish(context.bot, news_id, "manual"):
                self.db.update(news_id, human_label=1, decided_by=update.effective_user.id)
                await self.update_admin_msgs(
                    context.bot, news_id, f"✅ <b>Опубликовано</b> — {who}", self.published_kb(news_id)
                )
        elif action == "rej":
            self.db.update(news_id, status="rejected", human_label=0, decided_by=update.effective_user.id)
            await q.answer("Отклонено — учту")
            await self.update_admin_msgs(context.bot, news_id, f"❌ <b>Отклонено</b> — {who}")
        elif action == "edit":
            context.user_data["editing"] = news_id
            await q.answer()
            await context.bot.send_message(
                update.effective_user.id,
                "Пришлите новый текст поста.\nПервая строка — заголовок, остальное — текст.\n"
                "Подпись и ссылку на источник я добавлю сам. /cancel — отмена.",
            )
        elif action == "del":
            if row["status"] != "published":
                await q.answer("Пост уже не в канале")
                return
            try:
                await context.bot.delete_message(self.cfg.channel_id, row["channel_message_id"])
            except TelegramError as e:
                await q.answer(f"Не удалось удалить: {e}", show_alert=True)
                return
            # Удаление = сигнал «такое не публикуем» (кроме срочных — у них своё правило)
            label = None if row["publish_mode"] == "auto_breaking" else 0
            self.db.update(news_id, status="rejected", human_label=label,
                           decided_by=update.effective_user.id)
            await q.answer("Удалено — учту")
            await self.update_admin_msgs(context.bot, news_id, f"🗑 <b>Удалено из канала</b> — {who}")


def main() -> None:
    cfg = load_config()
    nb = NewsBot(cfg)
    app = Application.builder().token(cfg.bot_token).build()
    app.add_handler(CommandHandler("start", nb.cmd_start))
    app.add_handler(CommandHandler("stats", nb.cmd_stats))
    app.add_handler(CommandHandler("check", nb.cmd_check))
    app.add_handler(CommandHandler("cancel", nb.cmd_cancel))
    app.add_handler(CallbackQueryHandler(nb.on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, nb.on_text))
    if cfg.admin_ids:
        app.job_queue.run_repeating(nb.poll, interval=cfg.poll_interval_min * 60, first=10)
    log.info("Бот запущен, канал %s, админы %s", cfg.channel_id, sorted(cfg.admin_ids))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
