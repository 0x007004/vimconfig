# RTA Integration Agent — Top-Level Guide

> This file is the **primary Guide** the agent reads at the start of every task.
> It tells the agent *what we are*, *how we work*, and *what red lines exist*.
> Mapping: Anthropic Harness Engineering → "Guides (feedforward)".

## Mission

We are an ad-tech team. Our job is to integrate with media-side **RTA (Real-Time API)**
endpoints. Each new media platform (FoxAds, OwlAds, BearAds, ...) ships a slightly
different protocol but the integration shape is the same:

1. Read the media's protocol doc in `protocols/<media>.md`
2. Implement an adapter in `adapters/<media>.py` that conforms to `adapters/base.py`
3. Verify against the sandbox in `sandbox/server.py`
4. Sink any new learning to `skills/` and `pitfalls.md`

## How you (the agent) should work

When asked to integrate a new media:

1. **Read first, code later.** Always start with `protocols/<media>.md` and skim
   `adapters/` for similar reference implementations. Use `tools/retrieve_skill.py`
   to find any existing skills that match keywords from the protocol.
2. **Reuse before invent.** If a skill matches, apply it. If a reference adapter is
   90% similar, copy and modify rather than starting from scratch.
3. **Ask, don't guess.** If you encounter an unfamiliar field, encryption scheme,
   or business rule, call `tools/ask_expert.py` BEFORE writing speculative code.
4. **Sensors are not optional.** Every integration must pass `tools/verify.py`
   before being declared done. A green run = schema check + 100-request replay
   + p99 latency budget + reconciliation parity.
5. **Sink everything.** Every Q&A you have with an expert, every pitfall you hit,
   every non-obvious decision — write a skill via `tools/write_skill.py`.

## Red lines (hard constraints — do not cross)

These are enforced in `policy.yaml` but stated here in plain English:

- Never push to `master` / `main`. Only feature branches.
- Never modify files under `protocols/` (those are the source of truth from media).
- Never modify reference adapters that are already verified in production
  (currently `foxads.py`, `owlads.py`).
- Never bypass `tools/verify.py`. If verify fails, fix the code, do not silence
  the test.
- Never invent media-side credentials. If a credential is missing, ask the human.

## Tools available

- `tools/retrieve_skill.py <keywords>` — search the skill library
- `tools/ask_expert.py --topic <topic> --question <q>` — ask a domain expert
- `tools/write_skill.py --id <id> --content <md>` — sink a new skill
- `tools/verify.py <media>` — run the full sensor suite
- `tools/generate_adapter.py <media>` — generate adapter (delegates to LLM
  if `ANTHROPIC_API_KEY` is set, else uses a deterministic template fallback)

## Reference reading order for a new media

1. `protocols/<media>.md`              — what the media wants
2. `adapters/base.py`                   — what shape we produce
3. `adapters/foxads.py`                 — simplest reference
4. `adapters/owlads.py`                 — reference with a quirk
5. `pitfalls.md`                        — things known to bite
6. `skills/`                            — sunk knowledge from past integrations

That's it. Start with the protocol, end with a passing verify run, leave a
trail of skills behind you.
