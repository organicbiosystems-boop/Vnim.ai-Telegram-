"""Офлайн-тесты: логика порога и маршрутизации без Telegram и Claude."""
import asyncio
import os
import tempfile
from types import SimpleNamespace

from bot.ai import Evaluation
from bot.config import Config
from bot.learning import calibrate_threshold
from bot import main as m


def test_threshold_needs_data():
    assert calibrate_threshold([(90, 1)] * 5, 30, 0.95) is None


def test_threshold_found():
    data = [(90, 1)] * 20 + [(60, 1)] * 5 + [(60, 0)] * 10
    assert calibrate_threshold(data, 30, 0.95) == 61


def test_threshold_none_when_model_disagrees():
    data = [(90, 0)] * 20 + [(90, 1)] * 20
    assert calibrate_threshold(data, 30, 0.95) is None


class FakeBot:
    def __init__(self):
        self.sent = []
        self.n = 0

    async def send_message(self, chat_id, text, **kw):
        self.n += 1
        self.sent.append((chat_id, text, kw.get("reply_markup")))
        return SimpleNamespace(message_id=self.n)


class FakeEditor:
    def __init__(self, ev):
        self.ev = ev

    async def evaluate(self, **kw):
        return self.ev


def make_bot(tmp):
    os.environ.setdefault("ANTHROPIC_API_KEY", "test")
    cfg = Config(bot_token="x", channel_id="@VnimAi", admin_ids=frozenset({1, 2}),
                 db_path=os.path.join(tmp, "t.db"), learning_min_decisions=30)
    return m.NewsBot(cfg)


def ev(tier, prob):
    return Evaluation(is_duplicate=False, tier=tier, approve_probability=prob,
                      reason="r", post_title="Вышла Fable 3.5", post_body="Текст <b>")


def run(nb, e):
    nb.editor = FakeEditor(e)
    bot = FakeBot()
    nid = nb.db.insert_news(f"g{id(e)}", "https://ex.com/a?b=1&c=2", "T", "Src", "s", "new")
    asyncio.run(nb.process(bot, nid))
    return bot, nb.db.get(nid)


def test_breaking_autopublished_with_signature():
    with tempfile.TemporaryDirectory() as tmp:
        nb = make_bot(tmp)
        bot, row = run(nb, ev("breaking", 99))
        assert row["status"] == "published" and row["publish_mode"] == "auto_breaking"
        channel = [t for c, t, _ in bot.sent if c == "@VnimAi"][0]
        assert channel.rstrip().endswith("<b>VNIM.AI</b>")
        assert "&lt;b&gt;" in channel and "&amp;c=2" in channel
        assert len([1 for c, *_ in bot.sent if c in (1, 2)]) == 2


def test_regular_goes_to_review_then_learns():
    with tempfile.TemporaryDirectory() as tmp:
        nb = make_bot(tmp)
        bot, row = run(nb, ev("regular", 90))
        assert row["status"] == "pending"
        assert not any(c == "@VnimAi" for c, *_ in bot.sent)
        # Имитируем 30 решений: высокие прогнозы одобрены, низкие отклонены
        for i in range(20):
            n = nb.db.insert_news(f"a{i}", "", "t", "s", "", "published")
            nb.db.update(n, approve_prob=85, human_label=1)
        for i in range(15):
            n = nb.db.insert_news(f"r{i}", "", "t", "s", "", "rejected")
            nb.db.update(n, approve_prob=40, human_label=0)
        assert nb.threshold() is not None
        bot, row = run(nb, ev("regular", 88))
        assert row["publish_mode"] == "auto_learned"


def test_skip_is_silent():
    with tempfile.TemporaryDirectory() as tmp:
        nb = make_bot(tmp)
        bot, row = run(nb, ev("skip", 5))
        assert row["status"] == "skipped" and bot.sent == []
