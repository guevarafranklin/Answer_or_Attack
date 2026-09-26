"""Balance simulator (spec §8, build step 2): runs the pure engine for
thousands of games with bots of mixed skill and reports whether the
winner is decided by the hidden starting XP or by play.

    cd api && python scripts/simulate_balance.py [--games 10000] [--players 8 20]
        [--config '{"starting_xp_choices": [10, 20, 40]}'] [--config '{"attack_damage": 4}']
        [--seed 1] [--workers N] [--json out.json]

No database, no network: only app.game. Each `--config` is a JSON object of
GameConfig overrides and becomes one column of the report next to the
baseline, so variants of `starting_xp_choices`, `attack_cost` and
`attack_damage` can be compared side by side. The same seed gives the same
numbers.

Bots: skill = probability of a correct answer at difficulty 2 (±0.12 per
difficulty step), a log-normal response time whose median falls with
skill (slow enough that the tail times out), and one of three attack
policies split evenly across the table — never (always passes), random
(any other player) and greedy (the highest public delta). Bots act only
on public information: they never read their own total or anyone's
starting XP, and learn they are at 0 XP the way a player would, by having
an attack refused.

Reported per player count and config:
  winner had the highest starting XP / the best accuracy / both / neither
  Spearman rank correlation between skill and final placement (mean per game)
  attacks per game, success rate (block failed), and the share that landed
    on a 0-XP player for no damage
  share of players who touched 0 XP, share of games where anyone did
  tokens earned and passes per game, mean game duration in engine time
  per starting-XP bucket and per attack policy: share of wins next to
    share of seats, and their ratio (1.00x = wins its fair share)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.game.config import GameConfig  # noqa: E402
from app.game.engine import (  # noqa: E402
    Answer,
    Attack,
    AttackDeclared,
    BlockResolved,
    Ended,
    Error,
    GameState,
    Join,
    Pass,
    Passed,
    Phase,
    Pick,
    Question,
    Revealed,
    Start,
    Tick,
    new_game,
    step,
)

POLICIES = ("never", "random", "greedy")
SKILL_RANGE = (0.35, 0.90)
DIFFICULTY_STEP = 0.12  # P(correct) shifts this much per difficulty level away from 2
RT_SIGMA = 0.45  # log-normal spread of response times
RT_MIN_MS = 300
CATEGORIES = 5


# ---------- bots ----------


@dataclass(frozen=True)
class Bot:
    id: str
    skill: float
    policy: str

    def p_correct(self, difficulty: int) -> float:
        return min(0.98, max(0.02, self.skill + DIFFICULTY_STEP * (2 - difficulty)))

    def response_ms(self, rng: random.Random) -> int:
        # Strong players answer in ~2.5s, weak ones in ~4.5s, with a long tail.
        median = 5500 - 3500 * self.skill
        return max(RT_MIN_MS, int(rng.lognormvariate(math.log(median), RT_SIGMA)))


def make_bots(n: int, rng: random.Random) -> list[Bot]:
    return [
        Bot(f"b{i}", round(rng.uniform(*SKILL_RANGE), 3), POLICIES[i % len(POLICIES)])
        for i in range(n)
    ]


# ---------- question draw (shape of §6, no database) ----------


def draw(config: GameConfig, rng: random.Random) -> tuple[dict[str, list[Question]], list[Question]]:
    per_cat = math.ceil(config.question_count / CATEGORIES) + 2
    pools: dict[str, list[Question]] = {}
    for c in range(CATEGORIES):
        cid = f"c{c}"
        pools[cid] = [
            Question(f"{cid}-{i}", cid, 1 + (3 * i) // per_cat, rng.randrange(4))
            for i in range(per_cat)
        ]
    reserve = [
        Question(f"blk-{i}", f"c{i % CATEGORIES}", 2 + i % 2, rng.randrange(4))
        for i in range(config.max_players)
    ]
    return pools, reserve


# ---------- one game ----------


@dataclass
class GameRecord:
    players: int
    duration_s: float
    winner_had_top_start: bool
    winner_had_best_accuracy: bool
    spearman: float | None
    attacks: int
    attacks_landed: int
    attacks_on_zero: int
    passes: int
    tokens_earned: int
    hit_zero: int
    winner_policies: list[str] = field(default_factory=list)
    winner_starts: list[int] = field(default_factory=list)
    seat_starts: list[int] = field(default_factory=list)


def play_game(config: GameConfig, bots: list[Bot], seed: int) -> GameRecord:
    bot_rng = random.Random(seed)
    engine_rng = random.Random(seed ^ 0x5EED)
    pools, reserve = draw(config, bot_rng)
    state = new_game(config, pools, reserve)
    by_id = {b.id: b for b in bots}
    now = 0
    attacks = landed = on_zero = passes = tokens = 0
    hit_zero: set[str] = set()
    ended: Ended | None = None

    def send(event, at: int | None = None) -> list:
        nonlocal now, state, attacks, landed, on_zero, passes, tokens, ended
        if at is not None:
            now = max(now, at)
        state, msgs = step(state, event, now, engine_rng, copy_state=False)
        for m in msgs:
            if isinstance(m, AttackDeclared):
                attacks += 1
            elif isinstance(m, Passed):
                passes += 1
            elif isinstance(m, BlockResolved) and not m.blocked:
                landed += len(m.attacker_ids)
                if m.damage == 0:
                    on_zero += len(m.attacker_ids)
            elif isinstance(m, Revealed):
                tokens += sum(o.token_earned for o in m.outcomes.values())
            elif isinstance(m, Ended):
                ended = m
        if state.phase is not Phase.LOBBY:  # starting XP is only dealt at Start
            for p in state.players.values():
                if p.xp == 0:
                    hit_zero.add(p.id)
        return msgs

    for b in bots:
        send(Join(b.id, b.id))
    send(Start(bots[0].id))
    start_ms = now

    while state.phase is not Phase.END:
        phase = state.phase
        if phase is Phase.PICK:
            send(Pick(state.picker_id, bot_rng.choice(state.board)))
        elif phase is Phase.QUESTION:
            q = state.question
            plan = []
            for p in state.active_players():
                b = by_id[p.id]
                at = state.question_sent_ms + b.response_ms(bot_rng)
                if at <= state.phase_end_ms:
                    right = bot_rng.random() < b.p_correct(q.difficulty)
                    plan.append((at, p.id, q.correct_option if right else (q.correct_option + 1) % 4))
            for at, pid, option in sorted(plan):
                if state.phase is not Phase.QUESTION:
                    break
                send(Answer(pid, q.id, option), at=at)
            if state.phase is Phase.QUESTION:
                send(Tick(), at=state.phase_end_ms)
        elif phase is Phase.ATTACK:
            holders = [p.id for p in state.active_players() if state.awaiting_attack(p.id)]
            bot_rng.shuffle(holders)
            for pid in holders:
                if state.phase is not Phase.ATTACK:
                    break
                _act_in_attack_window(state, by_id[pid], bot_rng, send)
            if state.phase is Phase.ATTACK:
                send(Tick(), at=state.phase_end_ms)
        elif phase is Phase.BLOCK:
            plan = []
            for target_id, block in state.blocks.items():
                b, q = by_id[target_id], block.question
                at = state.question_sent_ms + b.response_ms(bot_rng)
                if q is not None and at <= state.phase_end_ms:
                    right = bot_rng.random() < b.p_correct(q.difficulty)
                    plan.append((at, target_id, q.id, q.correct_option if right else (q.correct_option + 1) % 4))
            for at, pid, qid, option in sorted(plan):
                if state.phase is not Phase.BLOCK:
                    break
                send(Answer(pid, qid, option), at=at)
            if state.phase is Phase.BLOCK:
                send(Tick(), at=state.phase_end_ms)
        else:  # REVEAL
            send(Tick(), at=state.phase_end_ms)

    assert ended is not None and ended.reason == "finished"
    return _record(
        ended, bots, by_id, state, now - start_ms, attacks, landed, on_zero, passes, tokens, hit_zero
    )


def _act_in_attack_window(state: GameState, bot: Bot, rng: random.Random, send) -> None:
    """Attack per policy on public information; pass when nothing works."""
    if bot.policy == "never":
        send(Pass(bot.id))
        return
    others = [p for p in state.active_players() if p.id != bot.id]
    if bot.policy == "greedy":
        others.sort(key=lambda p: -p.delta)
    else:
        rng.shuffle(others)
    for target in others:
        msgs = send(Attack(bot.id, target.id))
        codes = [m.code for m in msgs if isinstance(m, Error)]
        if not codes:
            return
        if codes != ["target_full"]:
            break  # no_xp: learned the hard way, like a player would
    if state.phase is Phase.ATTACK:
        send(Pass(bot.id))


def _record(
    ended: Ended,
    bots: list[Bot],
    by_id: dict[str, Bot],
    state: GameState,
    duration_ms: int,
    attacks: int,
    landed: int,
    on_zero: int,
    passes: int,
    tokens: int,
    hit_zero: set[str],
) -> GameRecord:
    results = ended.results  # ranked by the engine, winners first
    winners = [r for r in results if r.player_id in ended.winner_ids]
    top_start = max(r.starting_xp for r in results)
    accuracy = _accuracies(state)
    best_acc = max(accuracy.values())
    # Placement 1 = best; players the engine could not separate share a rank.
    keys = [(r.final_xp, r.delta, r.mean_correct_ms) for r in results]
    placement = _tie_ranks(keys)
    skills = [by_id[r.player_id].skill for r in results]
    rho = spearman(skills, [-p for p in placement])  # positive = skill helps
    return GameRecord(
        players=len(bots),
        duration_s=duration_ms / 1000,
        winner_had_top_start=any(w.starting_xp == top_start for w in winners),
        winner_had_best_accuracy=any(accuracy[w.player_id] == best_acc for w in winners),
        spearman=rho,
        attacks=attacks,
        attacks_landed=landed,
        attacks_on_zero=on_zero,
        passes=passes,
        tokens_earned=tokens,
        hit_zero=len(hit_zero),
        winner_policies=[by_id[w.player_id].policy for w in winners],
        winner_starts=[w.starting_xp for w in winners],
        seat_starts=[r.starting_xp for r in results],
    )


def _accuracies(state: GameState) -> dict[str, float]:
    served: dict[str, int] = {}
    correct: dict[str, int] = {}
    for s in state.serves:
        if s.kind != "question" or s.outcome == "absent":
            continue
        served[s.player_id] = served.get(s.player_id, 0) + 1
        if s.outcome == "correct":
            correct[s.player_id] = correct.get(s.player_id, 0) + 1
    return {pid: correct.get(pid, 0) / n for pid, n in served.items()} | {
        pid: 0.0 for pid in state.players if pid not in served
    }


def _tie_ranks(keys: list[Any]) -> list[float]:
    """keys are already in ranked order; runs of equal keys share the mean rank."""
    ranks = [0.0] * len(keys)
    i = 0
    while i < len(keys):
        j = i
        while j + 1 < len(keys) and keys[j + 1] == keys[i]:
            j += 1
        mean_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = mean_rank
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rho with tie-averaged ranks; None when a side is constant."""
    rx = _rank_values(xs)
    ry = _rank_values(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def _rank_values(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        mean_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = mean_rank
        i = j + 1
    return ranks


# ---------- batches and aggregation ----------


def run_batch(config_overrides: dict[str, Any], players: int, seeds: list[int]) -> list[dict[str, Any]]:
    """Worker entry point: plain dicts cross the process boundary."""
    config = GameConfig.from_overrides(config_overrides)
    out = []
    for seed in seeds:
        bots = make_bots(players, random.Random(seed * 7919 + 1))
        rec = play_game(config, bots, seed)
        out.append(rec.__dict__)
    return out


@dataclass
class Summary:
    games: int
    players: int
    top_start: float
    best_accuracy: float
    both: float
    neither: float
    spearman: float
    attacks_per_game: float
    attack_success: float | None
    attacks_on_zero: float | None
    players_hit_zero: float
    games_with_zero: float
    tokens_per_game: float
    passes_per_game: float
    duration_s: float
    # per attack policy and per starting-XP bucket: (share of seats, share of wins)
    policy_shares: dict[str, tuple[float, float]]
    start_shares: dict[int, tuple[float, float]]

    def rows(self) -> list[tuple[str, str]]:
        pct = lambda x: "—" if x is None else f"{100 * x:5.1f}%"  # noqa: E731

        def lift(seats: float, wins: float) -> str:
            # wins / seats: 1.00 = wins its fair share of games
            return f"{pct(wins)} of wins / {pct(seats)} of seats = {wins / seats:.2f}x"

        return [
            ("games", str(self.games)),
            ("winner had highest starting XP", pct(self.top_start)),
            ("winner had best accuracy", pct(self.best_accuracy)),
            ("  both", pct(self.both)),
            ("  neither", pct(self.neither)),
            ("skill↔placement Spearman ρ", f"{self.spearman:.3f}"),
            ("attacks per game", f"{self.attacks_per_game:.2f}"),
            ("attack success rate", pct(self.attack_success)),
            ("  landed on 0-XP target", pct(self.attacks_on_zero)),
            ("players who hit 0 XP", pct(self.players_hit_zero)),
            ("games where anyone hit 0", pct(self.games_with_zero)),
            ("tokens earned per game", f"{self.tokens_per_game:.2f}"),
            ("passes per game", f"{self.passes_per_game:.2f}"),
            ("mean game duration", f"{self.duration_s / 60:.1f} min"),
            *[(f"start {xp:>3}", lift(*self.start_shares[xp])) for xp in sorted(self.start_shares)],
            *[(f"policy {p}", lift(*self.policy_shares[p])) for p in POLICIES],
        ]


def summarize(records: list[dict[str, Any]], players: int) -> Summary:
    n = len(records)
    rhos = [r["spearman"] for r in records if r["spearman"] is not None]
    attacks = sum(r["attacks"] for r in records)
    landed = sum(r["attacks_landed"] for r in records)
    on_zero = sum(r["attacks_on_zero"] for r in records)
    policy_seats = {p: sum(1 for i in range(players) if POLICIES[i % 3] == p) / players for p in POLICIES}
    policy_wins = {p: 0.0 for p in POLICIES}
    start_seats: dict[int, int] = {}
    start_wins: dict[int, float] = {}
    for r in records:
        for p in r["winner_policies"]:  # a shared win is split between the winners
            policy_wins[p] += 1 / len(r["winner_policies"])
        for xp in r["seat_starts"]:
            start_seats[xp] = start_seats.get(xp, 0) + 1
        for xp in r["winner_starts"]:
            start_wins[xp] = start_wins.get(xp, 0.0) + 1 / len(r["winner_starts"])
    return Summary(
        games=n,
        players=players,
        top_start=sum(r["winner_had_top_start"] for r in records) / n,
        best_accuracy=sum(r["winner_had_best_accuracy"] for r in records) / n,
        both=sum(r["winner_had_top_start"] and r["winner_had_best_accuracy"] for r in records) / n,
        neither=sum(not r["winner_had_top_start"] and not r["winner_had_best_accuracy"] for r in records) / n,
        spearman=sum(rhos) / len(rhos) if rhos else float("nan"),
        attacks_per_game=attacks / n,
        attack_success=landed / attacks if attacks else None,
        attacks_on_zero=on_zero / attacks if attacks else None,
        players_hit_zero=sum(r["hit_zero"] for r in records) / (n * players),
        games_with_zero=sum(r["hit_zero"] > 0 for r in records) / n,
        tokens_per_game=sum(r["tokens_earned"] for r in records) / n,
        passes_per_game=sum(r["passes"] for r in records) / n,
        duration_s=sum(r["duration_s"] for r in records) / n,
        policy_shares={p: (policy_seats[p], policy_wins[p] / n) for p in POLICIES},
        start_shares={
            xp: (start_seats[xp] / (n * players), start_wins.get(xp, 0.0) / n) for xp in start_seats
        },
    )


def chunks(seq: list[int], size: int) -> Iterable[list[int]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def simulate(
    variants: dict[str, dict[str, Any]], player_counts: list[int], games: int, seed: int, workers: int
) -> dict[int, dict[str, Summary]]:
    report: dict[int, dict[str, Summary]] = {}
    for players in player_counts:
        report[players] = {}
        for label, overrides in variants.items():
            seeds = [seed * 1_000_003 + players * 10_007 + i for i in range(games)]
            t0 = time.perf_counter()
            if workers > 1:
                with ProcessPoolExecutor(workers) as pool:
                    futures = [pool.submit(run_batch, overrides, players, c) for c in chunks(seeds, 200)]
                    records = [r for f in futures for r in f.result()]
            else:
                records = run_batch(overrides, players, seeds)
            print(
                f"  {players:2d} players · {label}: {games} games in {time.perf_counter() - t0:.1f}s",
                file=sys.stderr,
            )
            report[players][label] = summarize(records, players)
    return report


def print_report(report: dict[int, dict[str, Summary]]) -> None:
    for players, by_label in report.items():
        labels = list(by_label)
        rows = [s.rows() for s in by_label.values()]
        name_w = max(len(r[0]) for r in rows[0])
        col_w = max(12, *(len(l) for l in labels))
        print(f"\n== {players} players ==")
        print(" " * name_w + "  " + "  ".join(l.rjust(col_w) for l in labels))
        for i, (name, _) in enumerate(rows[0]):
            print(name.ljust(name_w) + "  " + "  ".join(r[i][1].rjust(col_w) for r in rows))


def parse_config(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"--config must be a JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("--config must be a JSON object")
    GameConfig.from_overrides(value)  # fail early on unknown fields / bad values
    return value


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--games", type=int, default=10_000, help="games per player count and config")
    parser.add_argument("--players", type=int, nargs="+", default=[8, 20], help="table sizes to simulate")
    parser.add_argument(
        "--config",
        type=parse_config,
        action="append",
        default=[],
        metavar="JSON",
        help="GameConfig overrides as a JSON object; repeat for several variants",
    )
    parser.add_argument("--seed", type=int, default=1, help="same seed, same numbers")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1, help="processes")
    parser.add_argument("--json", type=Path, help="also write the summaries to this file")
    args = parser.parse_args(argv)

    variants: dict[str, dict[str, Any]] = {"baseline": {}}
    for overrides in args.config:
        label = json.dumps(overrides, separators=(",", ":"))
        variants[label] = overrides
    report = simulate(variants, args.players, args.games, args.seed, args.workers)
    print_report(report)
    if args.json:
        args.json.write_text(
            json.dumps(
                {str(p): {l: s.__dict__ for l, s in by_label.items()} for p, by_label in report.items()},
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
