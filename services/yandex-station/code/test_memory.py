"""Memory store + hybrid retrieval tests (tg#27845).

The fake embedder hashes character trigrams into a fixed-width bag vector:
texts sharing trigrams land close in cosine space, so the vector channel is
exercised deterministically without the sidecar.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import adapter
from memory import Memory, parse_facts, trigrams

DIM = 256


def fake_vector(text: str) -> list[float]:
    vec = [0.0] * DIM
    for gram in trigrams(text):
        vec[hash(gram) % DIM] += 1.0
    norm = sum(x * x for x in vec) ** 0.5 or 1.0
    return [x / norm for x in vec]


async def fake_embed(texts: list[str]) -> list[list[float]]:
    return [fake_vector(t) for t in texts]


def make_memory(tmp_path, **kwargs) -> Memory:
    return Memory(str(tmp_path / 'memory.sqlite3'), embed=fake_embed, **kwargs)


def test_facts_retrieved_by_paraphrase(tmp_path):
    mem = make_memory(tmp_path)
    mem.add_fact('st1', 'Владелец любит чай пуэр', 'preference')
    mem.add_fact('st1', 'Кот Барсик спит на подоконнике', 'fact')
    hits = asyncio.run(mem.retrieve('st2', 'что я люблю пить'))
    assert any('пуэр' in h for h in hits), hits
    mem.close()


def test_station_scoped_episodes(tmp_path):
    mem = make_memory(tmp_path)
    mem.record_episode('st1', 'включи музыку', 'Включаю.')
    hits = asyncio.run(mem.retrieve('st2', 'какую музыку я просил'))
    assert not any('включи музыку' in h for h in hits), hits
    hits2 = asyncio.run(mem.retrieve('st1', 'какую музыку я просил'))
    assert any('включи музыку' in h for h in hits2), hits2
    mem.close()


def test_dedup_updates_instead_of_appending(tmp_path):
    mem = make_memory(tmp_path)
    mem.add_fact('st1', 'Владелец любит чай', 'preference')
    mem.add_fact('st1', 'Владелец любит зелёный чай', 'preference')
    rows = mem.db.execute("SELECT text FROM memories WHERE kind='preference'").fetchall()
    assert len(rows) == 1 and 'зелёный' in rows[0][0], rows
    # FTS index follows the update.
    hits = asyncio.run(mem.retrieve('st1', 'зелёный чай'))
    assert any('зелёный' in h for h in hits), hits
    mem.close()


def test_vector_channel_alone_finds_match(tmp_path):
    mem = make_memory(tmp_path)
    # Typos defeat word-level FTS but trigram vectors still connect them.
    mem.add_fact('st1', 'доч(board) зовут Аня', 'fact', fake_vector('дочку зовут Аня'))
    hits = asyncio.run(mem.retrieve('st1', 'как зовут доцьку'))
    assert any('Аня' in h for h in hits), hits
    mem.close()


def test_lexical_only_degrades_gracefully(tmp_path):
    mem = Memory(str(tmp_path / 'memory.sqlite3'), embed=None)
    mem.add_fact('st1', 'Владелец работает инженером', 'fact')
    hits = asyncio.run(mem.retrieve('st1', 'где я работаю'))
    assert any('инженер' in h for h in hits), hits
    mem.close()


def test_episode_pruning(tmp_path):
    mem = make_memory(tmp_path)
    for i in range(205):
        mem.record_episode('st1', f'реплика {i}', f'ответ {i}')
    count = mem.db.execute("SELECT count(*) FROM memories WHERE kind='episode'").fetchone()[0]
    assert count == 200, count
    oldest = mem.db.execute("SELECT min(id) FROM memories WHERE kind='episode'").fetchone()[0]
    assert oldest == 6, oldest
    mem.close()


def test_format_block_caps_and_marks(tmp_path):
    mem = make_memory(tmp_path, max_chars=60)
    block = mem.format_block(['факт один короткий', 'факт два сильно длиннее лимита ' * 6, 'третий'])
    assert block.startswith('[Память ассистента')
    assert 'факт один' in block and 'третий' not in block

    mem.close()


def test_parse_facts_variants():
    assert parse_facts('{"facts":[{"text":"Любит чай","kind":"preference"}]}') == \
        [{'text': 'Любит чай', 'kind': 'preference'}]
    assert parse_facts('Вот JSON: {"facts":[]} конец') == []
    assert parse_facts('мусор без json') == []
    assert parse_facts('{"facts":[{"text":"x","kind":"evil"},{"text":"","kind":"fact"}]}') == []


def test_turn_end_to_end_with_injection_and_writeback(tmp_path, monkeypatch):
    """One Adapter turn: memory injected into the brain payload, durable fact
    written back by the extractor, then found by the next turn's retrieval."""
    captured = {}

    async def fake_run_command(command, payload, timeout):
        captured.update(payload)
        return {'text': 'Запомнил: ты любишь жасминовый чай. ', 'conversation_id': 'https://chatgpt.com/c/x'}

    monkeypatch.setattr(adapter, 'run_command', fake_run_command)

    async def fake_extract(prompt, reply):
        return [{'text': 'Владелец любит жасминовый чай', 'kind': 'preference'}]

    env = {'AISIS_ALLOWED_STATIONS': 'st1',
        'AISIS_STATE_DB': str(tmp_path / 'adapter.sqlite3'),
        'AISIS_MEMORY_DB': str(tmp_path / 'memory.sqlite3'),
        'AISIS_CHATGPT_COMMAND': '["/bin/true"]', 'AISIS_PERSONA': ''}

    async def scenario():
        app = adapter.Adapter(env)
        monkeypatch.setattr(adapter.Adapter, '_extract_facts', fake_extract)
        # Turn 1: no memories yet, write-back triggered by «запомни».
        reply = await app.turn('st1', 'запомни что я люблю жасминовый чай')
        assert reply == 'Запомнил: ты любишь жасминовый чай.'
        assert '[Память ассистента' not in captured['text']
        assert app.remember_task is not None
        await app.remember_task
        # Turn 2: the stored fact rides into the brain payload.
        await app.turn('st1', 'что я люблю пить')
        assert '[Память ассистента' in captured['text'], captured['text']
        assert 'жасминовый чай' in captured['text']
        await app.close(None)

    asyncio.run(scenario())
