# Living — A Model-Agnostic Runtime for Persistent Autonomous Agents

*niaochao* means "nest". This is a home for an AI that doesn't live inside a host application — its own place, its own clock, its own memory.

[中文版 →](README.zh-CN.md)

About 1400 lines of Python. Standard library only. One dependency: an OpenAI-compatible chat API.

---

## What the three files do

**`runner.py`** — the main loop. Give it a task; it assembles your `soul.md` / `user.md` / `memory.md` into the system prompt and works with three tools:

| Tool | What it does |
|---|---|
| `run(command)` | Executes shell on this machine, so it can look at real state |
| `recall(query)` | Searches long-term memory |
| `skill(query/name)` | Looks up the skill library (procedures and pitfalls someone already worked out) |

**`clock.py`** — the clock. Not a timer. It's a chain: every time it wakes, it decides *itself* when to wake next.

- **Light wake** just runs shell and looks around (open loops, due items, failing units). Nothing to do? It goes back to sleep — **zero model calls**.
- **Decide** — when there's a signal, it asks one question: do this or not. One turn, no tools.
- **Work** — only if the answer was yes does it pick up tools.

Budget lives in `state/clock/<who>/config.json`. A light wake costs nothing and ignores the budget; the budget governs *whether to call the model at all*. When it runs out the clock still wakes, it just doesn't call the model — it writes the work down and waits for approval. You can temporarily lift the limit while pushing through something, but it **must expire**: `limit_override_until` falls back on its own. See `CLOCK.md`.

**`hooks/`** — attachment points. The main loop is shared, and **you don't change it to add behaviour**. You write a module and hang it on. Interface in `HOOKS.md`.

---

## Where it stands

**Beta. It runs, but four things aren't solved. Listing them so you don't trip on them.**

1. **Heavy tasks used to stall.** Anything that needs many rounds of exploration — "read 500 conversations, then write a summary" — would run out of turns before producing anything. Now there's no turn ceiling: it lays out a plan first (with a rough turn estimate), then works block by block. Read-only tools and read-only commands don't count against turns, and the only circuit breaker is idling — ten consecutive rounds of looking without touching anything. Measured on a real task: the plan estimated 2 turns, it actually took 13, and it still finished. Light work (packaging, checking state, running one script) was never a problem.
2. **The judgement layer is unstable.** Given the identical signal, the model sometimes says yes and sometimes no. So the rule here is: **a due item is due** — that goes straight through and never gets handed to the model. Only "there's work, but I'm not sure it's real" gets asked.
3. **Memory retrieval is crude.** Character bigrams plus an overlap ratio, not vector search. Usable for short queries; don't expect accuracy.
4. **No peripherals included.** Seeing the screen, moving a mouse, speaking — all platform-specific. Only the verb contract is defined here (below). You write the implementation.

---

## Running it

```bash
# 1. Make one
mkdir -p personas/me
cp personas/example/*.md personas/me/
cp personas/example/.env.example personas/me/.env
# put your API key in, then rewrite soul.md / user.md as your own

# 2. Ask it something
python3 runner.py --who me "how much disk is left"

# 3. Self-check
python3 runner.py --who me --selftest

# 4. Wake it once (don't schedule the next one)
python3 clock.py --who me --tick --no-arm

# 5. Put it in the chain
python3 clock.py --who me --tick
```

To have it wake up on boot, point a systemd user timer or `cron @reboot` at a single `--tick`. The chain keeps itself going from there.

---

## Design decisions that actually matter

**The clock is a chain, not a timer.** A timer has no idea that nothing is happening today — it will wake you up anyway and burn money doing it. A chained clock sweeps for free first, and goes back to sleep if there's nothing.

**Deciding and doing are separate calls.** Asking a model to "judge whether this is worth doing" and "do it" in one call is a contradiction — it will act in order to justify itself, or give up in order not to bother you. Split it into two steps, one job each.

**Personality is files, not code.** Swap `soul.md` and you've swapped the being. The same skeleton runs different individuals, each with its own memory and its own clock, unaware of each other.

**There is exactly one main loop.** Nobody edits it. New behaviour goes in a hook. This isn't fussiness — the moment everyone edits their own copy of the main loop, nobody can tell which version is actually running.

**Don't read the host's private directories.** `runner.py` does not import the host and does not read its database. Moving history over is a separate script's job (see the notes in `migrate/`). The main loop reads only its own home.

---

## The verbs a body has to speak

I can't implement a body for you — screens, mice and microphones differ per platform. But the interface should look like this:

| Verb | Meaning | Failures must be structured |
|---|---|---|
| `see(target)` | Perceive | `screen_off` / `screen_blank` / `blocked` / `no_target` |
| `hear(source)` | Listen | same as above |
| `speak(text)` | Say it | engine unavailable → fall back to text, never fail silently |
| `act(action)` | Move something | `screen_off` / `blocked` / `off_target` / `not_supported` |
| `show(thing)` | Put it in front of a person | a path or a URL |

**"I can't see" and "I see a black screen" are different things.** Every failure has to distinguish them. Don't throw an exception and paper over it — the layer above uses this to decide whether retrying makes sense.

---

## Privacy

⚠️ **In this design, personality and privacy are the same set of files.**

Under `personas/<who>/` live the soul, your user profile, and your long-term notes. Under `state/` are journals and process state. `memory/` is the fact store.

`.gitignore` already blocks `personas/*/` (except example), `state/`, `memory/`, `conversation/` and `medical/`.

**But don't rely on `.gitignore` alone.** Run `git status` before you commit. What ends up in this repository is exactly what you're willing to make public.

---

## License

MIT. Use it, change it, sell it. If it breaks, that's on you.
