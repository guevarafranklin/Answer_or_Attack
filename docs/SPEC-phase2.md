# Phase 2 Spec — Game Engine & Web Test Client

**Project:** Answer or Attack / ¿Juegas o Restas?
**Phase 2 scope:** the rules engine, the realtime session runtime over WebSockets, clock sync and latency fairness, persistence of results and telemetry, a throwaway web test client, and bot players for load and latency testing.
**Out of scope:** mobile clients (Phase 3), real auth and payments (Phase 4), study-pack upload (later).

**Phase 2 goal:** 20 real people can play a full game in a browser, and it feels fair on a bad mobile connection. Everything in this phase serves tuning the game, not shipping it.

---

## 1. Ground rules for the implementer

1. **The engine is pure.** Rules live in a module with no I/O, no clock, no randomness of its own: it takes state + event + time + RNG and returns new state + outgoing messages. Every rule is unit-testable in milliseconds.
2. **Server is authoritative.** Clients send intentions (`answer`, `pick`, `attack`). The server decides time, correctness, and XP. A client never computes a score.
3. **Secrets never leave the server until reveal.** `correct_index` before the reveal phase, any player's `starting_xp` or current total before game end, and other players' attack tokens are never sent to any client. Enforce it in the outgoing message serializers, and test it.
4. **Every tunable number lives in one `GameConfig`.** No magic numbers in the engine. A session can override config for playtest experiments.
5. **Phase 1 invariants still hold.** House pool = `status='live' AND pack_id IS NULL`; serves go through `record_serves`; player identity through `current_player` (dev-only stub).

---

## 2. Rules (from Phase 0, with two amendments)

### 2.1 GameConfig defaults

```python
GameConfig(
    min_players=2,            # 4 for real games; 2 allowed for testing
    max_players=20,
    question_count=15,
    starting_xp_choices=(10, 18, 30),   # equal probability, secret
    points_correct=3,
    points_wrong=-1,
    points_timeout=-1,
    pick_seconds=8,           # picker chooses a category; auto-pick on timeout
    question_seconds=10,
    reveal_seconds=4,
    attack_window_seconds=6,
    block_seconds=5,
    streak_for_token=2,       # consecutive correct answers to earn a token
    max_tokens=2,
    attack_cost=1,            # XP the attacker pays, win or lose
    attack_damage=3,          # XP the target loses on a failed block
    max_incoming_attacks=2,   # per target per attack window
    grace_ms=400,             # late-arrival allowance, see §5
    rejoin_seconds=60,
    board_size=4,             # categories offered to the picker each round
)
```

### 2.2 Round flow

```
LOBBY → [host starts] →
  PICK      picker chooses 1 of board_size categories (auto-pick random on timeout)
  QUESTION  all present players answer simultaneously
  REVEAL    correct answer + each player's own delta shown
  ATTACK    players holding a token may declare one attack
  BLOCK     each attacked player answers one block question (skipped if no attacks)
  → next round, picker rotates …
→ END       true totals revealed, winner announced, results persisted
```

### 2.3 Scoring
- Correct +3, wrong −1, timeout −1. No answer while **absent** (disconnected) = no change and does not break a streak.
- XP floor is 0. XP never goes negative.
- A wrong answer or timeout resets the correct-answer streak.

### 2.4 Attacks
- A token is earned on every `streak_for_token` consecutive correct answers (2nd, 4th, 6th…). Max held: `max_tokens`.
- During ATTACK, a player with a token may target any other player. The attacker pays `attack_cost` immediately and loses the token. A player with 0 XP cannot attack (can't pay).
- A target may receive at most `max_incoming_attacks` per window. Extra attacks are refused at declaration (first by server receive time wins), and the refused attacker keeps token and XP.
- BLOCK: each attacked player gets one block question (difficulty 2–3), `block_seconds` to answer. Correct = all incoming attacks blocked. Wrong/timeout = target loses `attack_damage` per incoming attack (floored at 0). Block answers do not affect streaks or tokens.
- Attackers see only whether the block succeeded, never the target's resulting total.

### 2.5 Amendments to the Phase 0 rules

**A. Players at 0 XP can be attacked; the attack just does nothing.** The Phase 0 rule "at 0 you can't be attacked" leaks hidden information: if the UI refuses a target, the attacker learns that target is at zero. Instead, any player can be targeted; damage to a player at 0 is 0. The attacker still pays.

**B. Questions are drawn per category, not as one ordered list.** Because the picker chooses a category each round, the session draws a **pool per category** at start (ramped by difficulty within each pool), plus a **block reserve** of difficulty 2–3 questions. This changes the Phase 1 `POST /sessions/generate` shape (see §6).

### 2.6 Visibility
- During the game every player sees: all display names, each player's **delta** (current − starting), who holds the pick, the round number. Nobody sees totals or starting XP, including their own starting XP. Players see their own token count; others see only that *someone* attacked whom.
- At END: everyone's starting XP, final total, and delta are revealed.

### 2.7 Winner and tiebreak
Highest **final total** wins (the hidden start is the point of the game). Ties: higher delta → lower mean response time on correct answers → shared win. No sudden-death round in MVP.

### 2.8 Presence
- Disconnect marks a player **absent**. They keep their seat and XP for `rejoin_seconds`, rejoining with their `player_token`. After that they're dropped from the turn order but remain in final results.
- If the picker is absent at PICK, the pick passes to the next present player.
- If the host disconnects, host passes to the longest-connected present player.
- If fewer than 2 players are present for 60s, the session ends as `abandoned`.

---

## 3. Architecture

```
Client ⇄ WebSocket /ws/sessions/{join_code}
             │
     SessionRuntime (one asyncio task per live session)
             │  applies events → Engine (pure) → new state + messages
             │  persists snapshot to Redis on every transition
             ▼
     Redis: session:{id}:state (snapshot), session:{id}:questions (Phase 1 draw)
     Postgres: sessions, session_players, question_serves (via record_serves)
```

- **Single-instance MVP.** One API process owns all live sessions. The Redis snapshot means a restart can resume a session mid-round. Horizontal scaling (sticky routing by session id) is out of scope; keep the runtime behind an interface so it can be added.
- **Engine** — `app/game/engine.py`: `step(state, event, now_ms, rng) -> (state, [OutboundMessage])`. Events include player messages and `Tick` (phase deadline reached).
- **Runtime** — `app/game/runtime.py`: owns the asyncio task, the timers, connection registry, fan-out, snapshot, and persistence hooks.
- **Protocol** — `app/game/protocol.py`: Pydantic models for every inbound and outbound message, with per-recipient serializers (§1 rule 3).

---

## 4. WebSocket protocol

All messages are JSON `{ "type": ..., ... }`. Server times are epoch milliseconds in **server time**.

### Join / session lifecycle
- `POST /sessions` (HTTP) — host creates a lobby with config overrides and category ids → `{session_id, join_code}`. Draw happens at start, not here.
- `POST /sessions/{join_code}/join` (HTTP) `{display_name}` → `{player_id, player_token}`.
- WS connect: `/ws/sessions/{join_code}?token=<player_token>`.

### Client → server
| type | fields | when |
|---|---|---|
| `sync` | `client_ms` | anytime (clock sync, §5) |
| `start` | | host, in LOBBY |
| `pick` | `category_id` | picker, in PICK |
| `answer` | `question_id`, `option` (0–3) | QUESTION or BLOCK |
| `attack` | `target_player_id` | ATTACK, token holders |
| `report` | `question_id`, `reason` | anytime after the question is shown |

### Server → client
| type | notes |
|---|---|
| `sync_reply` | `client_ms`, `server_ms` |
| `lobby` | players list, host, config summary |
| `phase` | `{phase, round, deadline_ms, ...}` — every transition |
| `board` | categories offered + picker id (PICK) |
| `question` | `{question_id, stem, options, deadline_ms}` — **no correct_index** |
| `answer_ack` | your answer was received in time / too late |
| `reveal` | `correct_option`, your `outcome`, your `delta`, everyone's deltas, tokens you hold |
| `attacks` | who attacked whom this window (no XP values) |
| `block_question` | only to attacked players |
| `block_result` | per target: blocked or not; target also sees own new delta |
| `presence` | player joined / absent / returned / dropped |
| `end` | full reveal: starting, final, delta, winner, tiebreak used |
| `error` | `{code, message}` — e.g. `not_your_pick`, `no_token`, `target_full`, `too_late` |

Reconnecting clients receive a full `state` message built by the same per-recipient serializer.

---

## 5. Timing and latency fairness

This is the highest-risk part of the project. Build and test it before polishing anything else.

- **Clock sync.** On connect the client sends 5 `sync` messages; for each, `offset = server_ms − (client_send + client_recv)/2`. Use the offset from the sample with the lowest round-trip time. Re-sync every 30s. The client renders every countdown against `deadline_ms − offset`.
- **Acceptance rule.** The server stamps each answer on receipt. An answer counts if `received_ms ≤ deadline_ms + grace_ms`. No client timestamps are trusted.
- **Why grace, not client time:** trusting client timestamps lets a modified client answer after seeing others react. A fixed grace window is honest and simple. `grace_ms` is tuned in playtests with the latency bots (§8).
- **The phase ends at `deadline_ms + grace_ms`,** not at `deadline_ms`, so late-but-valid answers are never cut off. If every present player has answered, the phase ends early.
- **Response time** recorded for telemetry = `received_ms − question_sent_ms` minus half the player's measured RTT.

---

## 6. Changes to Phase 1

- **`POST /sessions/generate` is replaced by the draw at `start`.** The draw produces, per selected category, a pool of `ceil(question_count / categories) + 2` questions ramped easy→hard, plus a block reserve of `max_players` difficulty 2–3 questions from any selected category. Same guarantees: no duplicates across all pools and reserve, per-session option shuffle stored in `session_questions.option_order`, `short_by` when the pool is thin. Add `session_questions.pool` (category id or `'block'`) with a new migration.
- **Session status** transitions `lobby → running → finished | abandoned` are owned by the runtime.
- **Persistence at END:** `session_players.starting_xp/final_xp/delta_xp`, session `ended_at`, and `record_serves` for every question shown (outcomes include `absent`).

New tables (new migration):
```sql
CREATE TABLE session_events (      -- append-only log for debugging and replays
  id          BIGSERIAL PRIMARY KEY,
  session_id  UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  seq         INT NOT NULL,
  at_ms       BIGINT NOT NULL,
  kind        TEXT NOT NULL,
  payload     JSONB NOT NULL,
  UNIQUE (session_id, seq)
);
```
The event log lets you replay any disputed game through the pure engine and get the same result. Write it asynchronously; it must never slow a round.

---

## 7. Web test client

Served at `GET /dev/client` **only when `ENV=dev`**. One HTML file, vanilla JS, no build step, no styling beyond readable.

- Join screen (join code + name), lobby, and every phase rendered plainly.
- Shows the countdown from the synced clock, measured RTT and clock offset in a debug strip.
- A **latency simulator**: a dropdown adding artificial delay + jitter to outgoing and incoming messages (0 / 150±50 / 400±150 / 800±300 ms), so one laptop can feel a 3G player's game.
- A **host panel**: start game, config overrides (JSON), and an END screen with the full reveal.

This client is thrown away after Phase 3. Don't polish it.

---

## 8. Bots and load testing

`scripts/bots.py` — N simulated players over real WebSockets:
- configurable accuracy per bot, answer-delay distribution, and network profile (same four as §7),
- attack behaviour: never / random / greedy (always attack the highest-delta player),
- `--sessions` to run several games at once.

Report per run: answers rejected as late per network profile, mean phase-transition lag, server CPU, and a game summary. **Target: at 800±300ms with the default grace, fewer than 2% of on-time human-speed answers rejected**, and 20 bots × 10 concurrent sessions with phase transitions lagging under 100ms.

Also add a balance simulator: `scripts/simulate_balance.py` runs the pure engine for 10,000 games with bots of mixed skill and reports how often the winner is decided by starting XP versus play, attack usage, and how often players hit 0. This is how you tune `starting_xp_choices`, `attack_cost`, and `attack_damage` without 10,000 human games.

---

## 9. Definition of done

- [ ] Engine unit tests cover every rule in §2, including both amendments, floors, token cap, incoming-attack cap, streak reset, absent handling, tiebreaks
- [ ] Serializer tests prove no client message ever contains `correct_index` before reveal, any `starting_xp` or total before END, or another player's tokens
- [ ] A full game plays end to end in the web client with 3 browser tabs
- [ ] Killing and restarting the API mid-round resumes the session from the Redis snapshot
- [ ] A disputed game can be replayed from `session_events` to the identical result
- [ ] Bot run meets the §8 latency and load targets
- [ ] Balance simulator runs and reports
- [ ] One real playtest with at least 8 people

---

## 10. Build order

1. `GameConfig` + pure engine + exhaustive unit tests (no I/O at all)
2. Balance simulator (uses only the engine — gives early feedback on the numbers)
3. Session draw changes (per-category pools, block reserve, migration)
4. Protocol models + per-recipient serializers + leak tests
5. HTTP create/join + WebSocket endpoint + runtime (timers, fan-out, presence)
6. Clock sync + grace acceptance
7. Redis snapshot/resume + `session_events` log + replay test
8. Persistence at END + `record_serves`
9. Web test client with latency simulator
10. Bots + load/latency report
11. Real playtest → tune `GameConfig`
