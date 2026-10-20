# Rukiya conversation update

This update improves the existing conversation pipeline without changing providers, requesting new credentials, adding an AI planning call, or copying architecture from another bot.

## Behavior changes

- **One shared character brief:** `services/persona.py` now supplies both AI engine and legacy prompts. The portrayal emphasizes Rukia's composure, dry humor, judgment and protective warmth, with a few restrained character anchors. Compliments no longer force the same tsundere deflection. She remains honest about being an AI portrayal if asked.
- **The request comes first:** “hi Rukiya, why…?” is a question, rather than a welcome-only instruction. Help requests beat salutations. Words such as “cooldown” and “smartphone” no longer count as compliments, brief acknowledgments are not spam, and length alone does not make a meaningful message spam.
- **Useful short replies:** normal chat permits up to two sentences within the existing 250-character ceiling, so an answer can include its reason or a useful follow-up question. The standalone validator retains its one-sentence default for older Python callers; serious-message guidance and the orchestrator's serious-output validation remain one sentence. Safety scans still cover the entire proposed reply.
- **Explicit replies and DMs:** a resolved Discord reply to Rukiya triggers a conversation without another tag and supplies the exact quoted bot message as bounded, untrusted context. Direct messages also trigger without a tag. Ordinary unaddressed guild messages remain ignored. Bot messages, command invocations, rate limits, per-user cooldowns and mention suppression remain protected. An unavailable/deleted reference does not cause an extra fetch or guess at the quoted message; users can tag the bot to continue.
- **Better follow-ups:** history distinguishes the current viewer, other viewers and the recipient of each bot reply using stable canonical IDs. Database-loaded history now keeps the same identity metadata as in-memory history. Display names alone never link people. Corrections and explicit reply targets receive specific guidance rather than a generic response instruction. History remains scoped as before and bounded to eight turns, with at most 800 characters per turn and quoted reply target.
- **Smarter search decisions:** word-boundary matching avoids accidental triggers such as “hey” inside “they.” Character and personal-memory questions can use conversation context without web search; external current-information questions still use the existing verification paths. No new provider call is added by these changes.

## Validation

The suite contains 271 passing tests (250 existing plus 21 conversation regressions), covering two-sentence replies through the engine/legacy/orchestrator paths, original safety and language checks, question precedence, current-information routing, same-name participants, identity after database reload, escaped context, explicit Discord replies and DMs. Compilation and diff checks also pass.

Provider responses and Discord transports in tests are mocked. These checks establish control flow and safety behavior, not measured quality from a live model. Real personality, reasoning and language quality still depend on the configured model and available context. Deployment must restart the running bot to load the new code.

For a live smoke check after restart: ask “hi Rukiya, why is my timing off?”, reply to her answer with “why that one?” without a tag, correct a detail with “actually, I meant…”, and ask a personal-memory question. Check that she answers the request, follows the explicit reference and correction, and does not claim to see gameplay she has not been given. These are evaluation prompts, not guaranteed or pre-recorded outputs.

Character anchors were checked against the [official BLEACH character page](https://bleach-anime.com/en/character/) and [VIZ's discussion of Rukia's rabbit preference](https://www.viz.com/blog/posts/manga-sj-dream-couples-1461). The dialogue instructions are original, not copied character dialogue. Current-rank claims and unsolicited spoilers are deliberately avoided.
