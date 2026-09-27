"""The playtest report (app.game.report): built from the event log of a
game played on the persistence hooks, checked against what the live
runtime recorded; the dev route and the script that serve it.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import settings
from app.game import report as rep
from app.game.engine import Phase
from app.models import GameSession
from tests.test_persistence import Table, _own_registry, table  # noqa: F401 - fixture
from tests.test_runtime import World, live, settled  # noqa: F401 - fixture

# Every correct answer earns a token, so attack windows open from round 1.
RULES = {"streak_for_token": 1, "question_count": 3}


async def _report(t: Table) -> rep.GameReport:
    await t.hooks.flush(t.rt)
    async with t.live.factory() as s:
        row = await s.get(GameSession, t.session_id)
        assert row is not None
        return await rep.load_report(s, row)


@pytest.mark.asyncio
async def test_report_matches_what_the_live_game_recorded(live: World):
    t = await table(live, seed=3, question_seconds=7, attack_window_seconds=4, grace_min_ms=250, grace_max_ms=900, **RULES)
    await t.start()
    await t.play_to_end()
    r = await _report(t)
    state = t.rt.state

    # The timers come from resolved_config, the overrides from the request.
    row = await t.row()
    assert row.resolved_config is not None
    for f in rep.TIMER_FIELDS:
        assert getattr(r.config, f) == row.resolved_config[f]
    assert (r.config.question_seconds, r.config.attack_window_seconds) == (7, 4)
    assert (r.config.grace_min_ms, r.config.grace_max_ms) == (250, 900)
    assert r.config_overrides["question_seconds"] == 7 and r.config_overrides["streak_for_token"] == 1
    assert r.phase is Phase.END and r.end_reason == "finished" and r.round == 3
    assert r.results is not None and len(r.results) == 3
    assert r.event_count == t.rt.seq

    # Every serve the engine recorded, on the question it belongs to.
    rounds = [q for q in r.questions if q.kind == "question"]
    blocks = [q for q in r.questions if q.kind == "block"]
    assert [q.round for q in rounds] == [1, 2, 3]
    reported = [(q.question_id, s.player_id, s.outcome, s.response_ms, q.round, q.kind) for q in r.questions for s in q.serves]
    recorded = [(s.question_id, s.player_id, s.outcome, s.response_ms, s.round, s.kind) for s in state.serves]
    assert sorted(reported) == sorted(recorded)
    for q in rounds:
        assert q.stem and q.stem_length == len(q.stem) > 0
        assert q.category_id in {str(c) for c in row.category_ids}
        assert q.timer_seconds == 7 and (q.grace_min_ms, q.grace_max_ms) == (250, 900)
        assert q.phase_end_ms == q.deadline_ms + 900
        assert q.deadline_ms == q.shown_ms + 7_000 and q.phase_end_ms == q.deadline_ms + 900
        assert q.resolved_ms is not None and 0 <= q.open_ms <= 7_250
        assert len(q.serves) == 3 and {s.display_name for s in q.serves} == {"host", "beth", "carl"}
        for s in q.serves:
            assert (s.response_ms is None) == (s.outcome in ("timeout", "absent"))
            assert s.points is not None
    for q in blocks:
        assert q.timer_seconds == r.config.block_seconds and q.target_id and q.attacker_ids
        assert q.blocked is not None and len(q.serves) == 1 and q.serves[0].player_id == q.target_id
        assert q.stem_length == len(q.stem or "")

    # Overall: timeouts over serves to present players, percentiles over
    # the answered ones.
    answered = sorted(s.response_ms for q in rounds for s in q.serves if s.response_ms is not None)
    assert r.overall.serves == sum(len(q.serves) for q in rounds) - r.overall.absent == 9
    assert r.overall.timeout_rate == r.overall.timeout / 9
    assert r.overall.answered_ms == [s.response_ms for q in rounds for s in q.serves if s.response_ms is not None]
    assert r.overall.p50_ms == answered[-(-len(answered) // 2) - 1]
    assert r.overall.p90_ms == rep.percentile(answered, 0.9)
    assert sum(p.questions.serves for p in r.players) == 9

    # The table's players always attack: every window closes with nobody
    # expired, and every delay is measured from the window opening.
    assert r.windows, "no token holder at any reveal — the seed must change"
    for w in r.windows:
        assert w.timer_seconds == 4 and (w.grace_min_ms, w.grace_max_ms) == (250, 900)
        assert w.deadline_ms == w.opened_ms + 4_000 and w.phase_end_ms == w.deadline_ms + 900
        assert w.closed_ms is not None and w.holders and w.expired_count == 0
        for h in w.holders:
            assert h.action == "attack" and h.target_id and h.tokens_at_open and h.tokens_at_open >= 1
            assert h.delay_ms is not None and 0 <= h.delay_ms <= w.open_ms
    assert r.holders_total == sum(p.attacks for p in r.players) > 0 and r.windows_expired == 0

    # The renderers carry the headline numbers.
    text = rep.render_text(r)
    assert f"Game {t.code}" in text and "question_seconds 7" in text and "grace_min_ms 250" in text
    assert "7 s + 250–900 ms grace" in text
    assert f"p50 {r.overall.p50_ms} ms, p90 {r.overall.p90_ms} ms" in text
    html = rep.render_html(r)
    assert f"<title>Game report {t.code}</title>" in html and "attack_window_seconds" in html
    data = json.loads(json.dumps(rep.to_dict(r)))
    assert data["config"]["question_seconds"] == 7 and data["overall"]["p90_ms"] == r.overall.p90_ms
    assert data["windows"][0]["expired_count"] == 0 and data["questions"][0]["stem_length"] == rounds[0].stem_length


@pytest.mark.asyncio
async def test_attack_window_delays_passes_and_expiry(live: World):
    t = await table(live, **RULES)
    await t.start()
    st = t.rt.state  # the runtime steps on a copy: re-read after every settle
    assert st.picker_id is not None
    t.send(st.picker_id, {"type": "pick", "category_id": st.board[0]})
    await settled(t.rt)
    st = t.rt.state
    q = st.question
    assert q is not None and st.question_sent_ms is not None
    host, beth, carl = t.socks
    # Everyone answers correctly, a little apart: three token holders.
    for i, pid in enumerate((host, beth, carl), start=1):
        t.clock.set(st.question_sent_ms + 300 * i)
        t.send(pid, {"type": "answer", "question_id": q.id, "option": q.correct_option})
    await settled(t.rt)
    assert t.rt.state.phase is Phase.REVEAL
    wake = t.rt._next_wake_ms()
    assert wake is not None
    t.clock.set(wake)
    await settled(t.rt)
    st = t.rt.state
    assert st.phase is Phase.ATTACK and all(st.awaiting_attack(p) for p in (host, beth, carl))
    opened = t.clock.t

    t.clock.set(opened + 700)
    t.send(host, {"type": "pass"})
    await settled(t.rt)
    t.clock.set(opened + 1_200)
    t.send(beth, {"type": "attack", "target_player_id": carl})
    await settled(t.rt)
    st = t.rt.state
    assert st.phase is Phase.ATTACK  # carl still holds the window open
    wake = t.rt._next_wake_ms()
    assert wake is not None and wake == st.phase_end_ms
    t.clock.set(wake)
    await settled(t.rt)
    st = t.rt.state
    assert st.phase is Phase.BLOCK
    block = st.blocks[carl]
    assert block.question is not None
    t.clock.set(t.clock.t + 900)
    t.send(carl, {"type": "answer", "question_id": block.question.id, "option": (block.question.correct_option + 1) % 4})
    await t.play_to_end()

    r = await _report(t)
    first = [q for q in r.questions if q.kind == "question"][0]
    assert [(x.display_name, x.outcome, x.response_ms) for x in first.serves] == [
        ("host", "correct", 300),
        ("beth", "correct", 600),
        ("carl", "correct", 900),
    ]
    assert first.open_ms == 900  # revealed as soon as the last answer landed

    w = r.windows[0]
    assert w.round == 1 and w.opened_ms == opened and w.closed_ms == w.phase_end_ms
    assert [(h.display_name, h.tokens_at_open, h.action, h.delay_ms) for h in w.holders] == [
        ("host", 1, "pass", 700),
        ("beth", 1, "attack", 1_200),
        ("carl", 1, "expired", None),
    ]
    assert w.holders[1].target_id == carl and not w.holders[2].absent_at_close
    assert w.expired_count == 1

    b = next(q for q in r.questions if q.kind == "block" and q.round == 1)
    assert b.target_id == carl and b.attacker_ids == (beth,) and b.blocked is False
    assert b.timer_seconds == r.config.block_seconds
    assert [(x.display_name, x.outcome, x.response_ms) for x in b.serves] == [("carl", "incorrect", 900)]
    assert r.blocks.serves >= 1 and r.blocks.timeout_rate is not None

    by_name = {p.display_name: p for p in r.players}
    assert by_name["host"].passes >= 1 and by_name["beth"].attacks >= 1 and by_name["carl"].expired >= 1

    text = rep.render_text(r)
    assert "host             (1 token)  passed after 700 ms" in text
    assert "beth             (1 token)  attacked carl after 1200 ms" in text
    assert "carl             (1 token)  let the window expire" in text
    assert "1 of 3 holders let it expire" in text


@pytest.mark.asyncio
async def test_report_mid_game_leaves_the_open_window_pending(live: World):
    t = await table(live, **RULES)
    await t.start()
    await t.play_until(lambda rt: rt.state.phase is Phase.ATTACK)
    r = await _report(t)
    assert r.phase is Phase.ATTACK and r.results is None
    w = r.windows[-1]
    assert w.closed_ms is None and w.expired_count == 0
    assert all(h.action == "pending" for h in w.holders)
    assert "window still open" in rep.render_text(r)


@pytest.mark.asyncio
async def test_report_route(live: World):
    t = await table(live, **RULES)
    await t.start()
    await t.play_to_end()
    await t.hooks.flush(t.rt)

    r = await live.http.get(f"/dev/sessions/{t.code.lower()}/report")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert f"<title>Game report {t.code}</title>" in r.text
    assert "question_seconds" in r.text and "Attack windows" in r.text

    r = await live.http.get(f"/dev/sessions/{t.code}/report", params={"format": "json"})
    assert r.status_code == 200
    body = r.json()
    assert body["join_code"] == t.code and body["config"]["question_count"] == 3
    assert body["phase"] == "end" and len(body["questions"]) >= 3 and "timeout_rate" in body["overall"]

    r = await live.http.get(f"/dev/sessions/{t.code}/report", params={"format": "text"})
    assert r.status_code == 200 and r.text.startswith(f"Game {t.code}")

    assert (await live.http.get("/dev/sessions/ZZZZZZ/report")).status_code == 404

    host = await live.user("lobby-host")
    code = await live.lobby(host, await live.bank())
    r = await live.http.get(f"/dev/sessions/{code}/report")
    assert r.status_code == 409 and "has not started" in r.json()["detail"]


@pytest.mark.asyncio
async def test_report_route_is_404_outside_dev(live: World, monkeypatch: pytest.MonkeyPatch):
    t = await table(live, **RULES)
    await t.start()
    await t.play_to_end()
    monkeypatch.setattr(settings, "env", "prod")
    assert (await live.http.get(f"/dev/sessions/{t.code}/report")).status_code == 404


@pytest.mark.asyncio
async def test_script_prints_the_report(live: World, migrated_db: str):
    t = await table(live, **RULES)
    await t.start()
    await t.play_to_end()
    await t.hooks.flush(t.rt)
    script = Path(__file__).resolve().parent.parent / "scripts" / "game_report.py"

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), *args, "--database-url", migrated_db],
            capture_output=True,
            text=True,
            timeout=60,
        )

    p = run(t.code.lower())
    assert p.returncode == 0, p.stderr
    assert p.stdout.startswith(f"Game {t.code}") and "== Attack windows ==" in p.stdout
    p = run(str(t.session_id), "--json")
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["join_code"] == t.code
    p = run("ZZZZZZ")
    assert p.returncode == 1 and "no session" in p.stderr
