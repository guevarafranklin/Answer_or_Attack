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
    starting_xp_choices=None, # explicit override; None = pick a tier by present player count
    starting_xp_tiers=(       # (max_players, choices): first tier covering the count at Start
        (8, (10, 12, 15)),    #   2–8 players (2–3 = test games)
        (20, (10, 11, 13)),   #   9–20 players; larger counts reuse the last tier
    ),                        # equal probability among the choices, secret
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
    attack_steal=2,           # XP each attacker gains on a failed block (bounty, not a transfer)
    max_incoming_attacks=2,   # per target per attack window
    grace_ms=400,             # late-arrival allowance, see §5
    rejoin_seconds=60,
    board_size=4,             # categories offered to the picker each round
)
```

**Balance rationale (2026-09-26, `scripts/simulate_balance.py`, 10,000 games per cell, seed 1; raw numbers in `docs/balance/2026-09-26-grid.json`).** The grid crossed `starting_xp_choices` ∈ {(10,18,30), (10,14,18), (10,12,15)} with `attack_steal` ∈ {0, 2, 3} at 4, 6, 8, 12 and 20 bots of mixed skill, every other field at the values above (the grid ran before the tiers and `attack_steal=2` became defaults, with each cell set as an explicit override). Bots attack per a fixed policy (never / random / greedy-highest-delta), so the attack numbers measure incentives, not human tactics.

- *Starting XP dominated the outcome under the Phase 0 spread.* With (10,18,30) the top bucket won 78% of 4-player games and 97% of 20-player games (2.9× its seat share at 20; the low bucket never won). Tightening to (10,12,15) brought skill↔placement Spearman ρ from ~0.55 to ~0.75 and "best accuracy wins" from 50–69% to 76–89%.
- *The high bucket's edge grows with table size* because more seats put a strong player in the top bucket more often: with (10,12,15) its lift went 1.19× (4 players) → 1.39× (8) → 1.58× (12) → 1.87× (20). Hence the tiers: (10,12,15) up to 8 players and (10,11,13) above, which holds the high-bucket lift at 1.3–1.5× and "winner had the highest start" near 45–55% across the whole range. **(10,11,12) was rejected** for 20 players: it evens the buckets further (1.27×) but a 2-point spread is less than one correct answer, which makes the secret start cosmetic rather than a mechanic; the 3-point spread keeps it felt while staying below the (10,12,15)-at-8 level.
- *`attack_steal` = 2 makes attacking break-even; 0 punishes it, 3 rewards it too much.* At 0, "never attack" bots won 1.1–1.7× their seat share (worse with more players); at 3 they fell to 0.67–0.81×. At 2 every policy stayed within ±15% of fair at every table size, and it moved the starting-XP numbers by only 2–4 points. Steal is a bounty paid in full even when the target is at 0 XP (Amendment A), so it never depends on the target's total.
- Attack volume, block success (40–45% land) and 0-XP frequency (someone hits 0 in ≤6.5% of games) did not react to either knob, so `attack_cost`, `attack_damage` and `max_incoming_attacks` were left alone until the playtest says otherwise.

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
- **"Can pay" means XP > 0.** The attacker pays `attack_cost` floored at 0. A holder at 0 XP is refused privately (`no_xp`); nobody else learns of it.
- **The ATTACK window is skipped when no present player holds a token.** XP is not considered, so skipping never reveals who is broke. A token holder may `pass` instead of attacking; the window ends early once every present token holder has attacked or passed, and one attack *or* pass is allowed per window.
- BLOCK: each attacked player gets one block question (difficulty 2–3), `block_seconds` to answer. Correct = all incoming attacks blocked. Wrong/timeout = target loses `attack_damage` per incoming attack (floored at 0) and each attacker gains `attack_steal` in full, even when the target had nothing left to lose. Block answers do not affect streaks or tokens.
- **Block reserve exhausted:** the block question is drawn from the unused questions of the category pools (difficulty ≥ 2 preferred). Only when every pool is empty too do the attacks count as blocked.
- Attackers see only whether the block succeeded, never the target's resulting total.

### 2.5 Amendments to the Phase 0 rules

**A. Players at 0 XP can be attacked; the attack just does nothing.** The Phase 0 rule "at 0 you can't be attacked" leaks hidden information: if the UI refuses a target, the attacker learns that target is at zero. Instead, any player can be targeted; damage to a player at 0 is 0. The attacker still pays.

**B. Questions are drawn per category, not as one ordered list.** Because the picker chooses a category each round, the session draws a **pool per category** at start (ramped by difficulty within each pool), plus a **block reserve** of difficulty 2–3 questions. This changes the Phase 1 `POST /sessions/generate` shape (see §6).

### 2.6 Visibility
- During the game every player sees: all display names, each player's **nominal delta** (below), who holds the pick, the round number. Nobody sees totals or starting XP, including their own starting XP. Players see their own token count; others see only that *someone* attacked whom.
- **Two deltas.** The XP floor is itself a secret: a delta that stops falling says "this player is at 0", and with the deltas public that gives their start away. So the engine keeps, per player, the **nominal delta** — the sum of every scoring change as nominally applied (+3 / −1 / −1, `attack_cost`, `attack_damage` per incoming attack, `attack_steal`), the floor ignored — next to the real, floored XP. Everything shown before END (`reveal`, the roster, `block_result`, the reconnect `state`) is the nominal delta; the nominal block damage is what the target is told. Scoring, the ranking and the tiebreak use the real XP.
- At END: everyone's starting XP, final total, real delta (final − starting) and nominal delta are revealed; the final screen shows both.

### 2.7 Winner and tiebreak
Highest **final total** wins (the hidden start is the point of the game). Ties: higher delta → lower mean response time on correct answers (round questions only, never blocks; a player with no correct answer loses this step) → shared win. No sudden-death round in MVP.

### 2.8 Presence
- Disconnect marks a player **absent**. They keep their seat and XP for `rejoin_seconds`, rejoining with their `player_token`. After that they're dropped from the turn order but remain in final results.
- **Absence only protects round answers** (§2.3). An attacked player who is absent at the block deadline times out and takes damage like anyone else.
- **Dropped players** are served no more questions and cannot be targeted (`unknown_target`), but keep their XP and appear in the END reveal.
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
- `POST /sessions/{join_code}/join` (HTTP) `{display_name, player_token?}` → `{player_id, player_token}`. The token is opaque, random, stored hashed and scoped to the session. Once the game is running the endpoint only accepts a rejoin — the seat's own token — and refuses everyone else (409).
- WS connect: `/ws/sessions/{join_code}?token=<player_token>`.

### Client → server
| type | fields | when |
|---|---|---|
| `sync` | `client_ms` | anytime — the client's own clock offset (§5) |
| `pong` | `server_ms` | immediately on every server `ping`, echoing its `server_ms` (§5) |
| `start` | | host, in LOBBY |
| `pick` | `category_id` | picker, in PICK |
| `answer` | `question_id`, `option` (0–3) | QUESTION or BLOCK |
| `attack` | `target_player_id` | ATTACK, token holders |
| `pass` | | ATTACK, token holders who decline to attack |
| `report` | `question_id`, `reason`, `note?` | anytime after the question is shown to you; filed through the report service |

### Server → client
| type | notes |
|---|---|
| `sync_reply` | `client_ms`, `server_ms` |
| `ping` | `server_ms` — echo as `pong` at once; a burst of 5 on connect, then one every 15 s (§5) |
| `lobby` | players list, host, config summary |
| `phase` | `{phase, round, deadline_ms, ...}` — every transition |
| `board` | categories offered + picker id (PICK) |
| `question` | `{question_id, stem, options, deadline_ms}` — **no correct_index** |
| `answer_ack` | your answer was received in time / too late |
| `report_ack` | your report was filed, or why not (`unknown_question`, `already_reported`) |
| `reveal` | `correct_option`, your `outcome`, your nominal `delta`, everyone's nominal deltas, tokens you hold |
| `attacks` | who attacked whom this window (no XP values) |
| `block_question` | only to attacked players |
| `block_result` | per target: blocked or not; target also sees nominal damage and own new nominal delta; attackers their own gain |
| `presence` | player joined / absent / returned / dropped |
| `end` | full reveal: starting, final, real delta, nominal delta, winner, tiebreak used |
| `error` | `{code, message}` — e.g. `not_your_pick`, `no_token`, `target_full`, `too_late` |

Reconnecting clients receive a full `state` message built by the same per-recipient serializer.

---

## 5. Timing and latency fairness

This is the highest-risk part of the project. Build and test it before polishing anything else.

Two measurements, two owners. The **server measures each player's round trip** itself and uses it for telemetry and the tiebreak; the **client measures its own clock offset** and uses it only to draw the countdown. Nothing from a client's clock ever reaches scoring or acceptance.

- **Server RTT (`ping`/`pong`).** On connect the runtime sends 5 `ping {server_ms}` messages 250 ms apart, then one every 15 s. The client echoes each as `pong {server_ms}` immediately, adding nothing. The runtime computes `rtt = pong_received_ms − server_ms` with both stamps on its own clock (the ping's stamp is the send time, the pong's stamp is its enqueue time), keeps the last 7 samples per player and uses their **median** as the player's RTT — so one stalled sample does not move it and a sustained change does. A pong is ignored if it echoes a stamp that was never sent, was already echoed, or is older than 30 s; a client can therefore only make its own RTT look *larger*, never smaller, and never negative. A new socket for the same seat starts a fresh measurement. Until the first sample the RTT is 0.
- **Client offset (`sync`/`sync_reply`).** For the countdown only. The client sends `sync {client_ms}`; the server replies `sync_reply {client_ms, server_ms}` with `server_ms` stamped on receipt. The client algorithm, for the web and mobile clients alike:
  1. On connect send 5 `sync` messages, each with `client_ms = now()` — any clock the client reads consistently (`Date.now()` on the web), in ms — ~100 ms apart.
  2. For each reply: `rtt = client_recv − client_ms`; `offset = server_ms − (client_ms + client_recv) / 2`.
  3. Keep the offset from the sample with the **lowest rtt** (not the average: the shortest trip is the least skewed).
  4. Re-sync every 30 s the same way. Adopt the new offset if its best sample's rtt is no worse than the one in use, or if the one in use is older than 60 s (so a drifting clock is still corrected).
  5. Render every countdown as `deadline_ms − (now() + offset)`. Show the countdown reaching 0 at `deadline_ms`; the server's grace is invisible to the player.
  The client's `sync` cadence is its own business; the server answers every `sync` and never acts on it.
- **Acceptance rule.** The server stamps each answer on receipt — the stamp is put on the message when it is taken off the socket and enqueued, before anything else in the queue is applied, so a busy runtime cannot make an answer late. An answer counts if `received_ms ≤ deadline_ms + grace_ms`. No client timestamps are trusted.
- **Why grace, not client time:** trusting client timestamps lets a modified client answer after seeing others react. A fixed grace window is honest and simple. `grace_ms` is tuned in playtests with the latency bots (§8).
- **The phase ends at `deadline_ms + grace_ms`,** not at `deadline_ms`, so late-but-valid answers are never cut off: the runtime's timer ticks at `phase_end_ms = deadline_ms + grace_ms`, and the tick is queued behind any answer that arrived first. If every present player has answered, the phase ends early. REVEAL takes no input and ends at its deadline.
- **Response time** recorded for telemetry and used by the tiebreak = `max(0, received_ms − question_sent_ms − min(rtt / 2, 500))`, with the player's median RTT at the moment the answer is applied. The 500 ms cap bounds what a client that stalls its pongs (inflating its RTT) can gain in the tiebreak. It never affects points.

---

## 6. Changes to Phase 1

- **`POST /sessions/generate` is replaced by the draw at `start`.** The draw produces, per selected category, a pool of `ceil(question_count / categories) + 2` questions ramped easy→hard, plus a block reserve of `max_players` difficulty 2–3 questions from any selected category. Same guarantees: no duplicates across all pools and reserve, per-session option shuffle stored in `session_questions.option_order`, `short_by` when the pool is thin. Add `session_questions.pool` (category id or `'block'`) with a new migration.
- **Session status** transitions `lobby → running → finished | abandoned` are owned by the runtime.
- **Persistence at END:** `session_players.starting_xp/final_xp/delta_xp`, session `ended_at`, and `record_serves` for every question shown (outcomes include `absent`). As built (step 8): `on_end` → `persist_end`, one transaction from the engine's final state — every seat the engine saw (dropped players included; seats that never connected keep NULLs), `ended_at` if not yet set, one serve per engine `ServeRecord` (round and block, `response_ms` RTT-corrected). Idempotent: a session whose seats already carry a `final_xp` is left alone, so a retried, resumed or replayed END writes nothing twice. Abandoned games persist what reached a reveal.

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

**Snapshot, resume, replay (step 7, as built).** After every engine step the runtime writes the full engine state, the rng state and the event counter to `session:{id}:state` (TTL 3 h) and appends the event with its enqueue stamp to `session_events`, both through one background writer per session: events are inserted before the snapshot that has seen them, snapshot writes are coalesced to the latest state, and everything is flushed at END (the snapshot is then deleted) and at shutdown. On startup, every `running` session with a snapshot is restored: state and rng loaded, the log rows past the snapshot's seq applied to it through the pure engine (so every step a player saw acknowledged survives the crash; rows past a gap in the log are dropped), then a `Disconnect` applied (and logged) for each present player so the rejoin window starts at resume, and timers re-armed from the stored deadlines. A game the log had already ended is finished at resume. A `running` session without a snapshot is marked `abandoned`. `replay(session_id)` (`scripts/replay_session.py`) rebuilds the game from `resolved_config`, `rng_seed`, `session_questions` and `session_events` and returns the final state, which equals the live one exactly.

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
- [ ] Leak tests: for seeded games where someone hits 0, the public delta sequence is identical to one computed without the floor
- [ ] Serializer tests prove no client message ever contains `correct_index` before reveal, any `starting_xp` or total before END, or another player's tokens
- [ ] A full game plays end to end in the web client with 3 browser tabs
- [x] Killing and restarting the API mid-round resumes the session from the Redis snapshot
- [x] A disputed game can be replayed from `session_events` to the identical result
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
