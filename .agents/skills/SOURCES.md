# Rukiya project skills: sources and installation notes

Installed as repository files under `.agents/skills/`; no global installs, symlinks, runtime dependencies, or application changes. Upstream contents are third-party guidance subordinate to the task and repository instructions. Rukiya and Lxeus are separate projects.

## Source revisions

| Skill | Upstream directory at pinned revision | Commit SHA |
| --- | --- | --- |
| `async-python-patterns` | [github.com/wshobson/agents/plugins/python-development/skills/async-python-patterns](https://github.com/wshobson/agents/tree/67424ce3f7c3dcd6448e8023c8088155217fa737/plugins/python-development/skills/async-python-patterns) | `67424ce3f7c3dcd6448e8023c8088155217fa737` |
| `python-resilience` | [github.com/wshobson/agents/plugins/python-development/skills/python-resilience](https://github.com/wshobson/agents/tree/67424ce3f7c3dcd6448e8023c8088155217fa737/plugins/python-development/skills/python-resilience) | `67424ce3f7c3dcd6448e8023c8088155217fa737` |
| `python-testing-patterns` | [github.com/wshobson/agents/plugins/python-development/skills/python-testing-patterns](https://github.com/wshobson/agents/tree/67424ce3f7c3dcd6448e8023c8088155217fa737/plugins/python-development/skills/python-testing-patterns) | `67424ce3f7c3dcd6448e8023c8088155217fa737` |
| `discord-py` | [github.com/frizzle-chan/mudd/.claude/skills/discord-py](https://github.com/frizzle-chan/mudd/tree/032d4f647996d38edc5e970141e108f9b966539f/.claude/skills/discord-py) | `032d4f647996d38edc5e970141e108f9b966539f` |
| `ai-agents-architect` | [github.com/sickn33/agentic-awesome-skills/skills/ai-agents-architect](https://github.com/sickn33/agentic-awesome-skills/tree/739c841480587361c3cb2047f5b34cdca69a0623/skills/ai-agents-architect) | `739c841480587361c3cb2047f5b34cdca69a0623` |

The three wshobson skills and ai-agents-architect were resolved from upstream HEAD; discord-py was resolved from the requested `master` branch. All files within each requested directory were copied. Supporting resources are bundled with the skills. Root upstream LICENSE files were copied into each corresponding skill directory. ai-agents-architect also includes LICENSE-CONTENT and its referenced upstream docs/sources/sources.md.

## Rukiya usage constraints

- Preserve `discord.py`, `RukiyaOrchestrator`, and the existing decision engine. Keep Discord cogs, YouTube, memory, identity, and AI provider service boundaries.
- Preserve command synchronization in `RukiyaBot.setup_hook()` in `main.py`. The upstream discord-py sample synchronizes in `on_ready()`; do not transplant that flow into Rukiya.
- Keep credentials in environment variables. Token literals in upstream examples are placeholders, not a project configuration pattern.
- Skills mention optional frameworks and packages (including Tenacity, pytest, pytest-asyncio, Freezegun, and agent frameworks). Their inclusion does not authorize installing dependencies or replacing existing architecture/test infrastructure. Rukiya CI currently runs unittest discovery.
- Related skill names in ai-agents-architect are optional recommendations, not required bundled resources; no additional skills were installed.

## Licenses and attribution

wshobson/agents: MIT, copyright Seth Hobson. frizzle-chan/mudd: MIT, copyright frizzle-chan. All upstream notices are preserved verbatim.

ai-agents-architect retains its more specific `source: vibeship-spawner-skills (Apache 2.0)` notice and upstream modification notice. The collection also supplies MIT LICENSE and CC BY 4.0 LICENSE-CONTENT (which explicitly defers to more specific upstream notices). Its attribution document does not supply a separate entry or Apache license file for this skill. These notices are preserved as supplied, without asserting that the collection MIT license supersedes the skill-specific notice.

## Workspace and network limitations

The initial `/workspace` contained no usable Git checkout: `.git` was an empty read-only mount, and `git remote -v` / `git status` reported "not a git repository". There was no mismatched repository to modify.

`git clone https://github.com/rukia3287-jpg/rukiya.git /workspace/rukiya` failed with: `Failed to connect to proxy port 8080 after 0 ms: Could not connect to server`. The GitHub connector provided a working read-only download path; no network policy bypass was used.

The target was materialized in `/workspace/rukiya` from upstream commit `a13998f7e4f6e18ebe01d0174f6cdc1415de63de`. All 79 blobs were checked against their Git object SHAs. The baseline tree is `c3b81139ed429c65d0e4c6b1be6095ddc2d19fca`. A local snapshot baseline commit was created on `codex/project-skills` for review; this is not the original upstream commit and upstream history was not cloned. Origin points to the requested repository. Before publishing the branch, Git transport became available and the real upstream main history was fetched. Its tree matched the snapshot exactly; the published changes are parented to that real upstream commit rather than the temporary local snapshot commit. The existing `.gitignore` ignores `.agents/`; the new files were force-added with intent-to-add (`git add -N -f .agents/skills`) so they appear in the review diff without changing ignore rules. No upstream AGENTS.md or pre-existing skills were present in the complete source tree.

Inspection confirmed Python dependencies, Discord cogs, YouTube integration, the custom orchestrator and decision service, scoped conversation memory and identity, Gemini/OpenRouter routing, planning, evidence checks, repair, and budget controls.

## Validation

- Verified all copied upstream file bytes against Git blob SHA-1 values; complete requested directories contain no symlinks.
- Read all five installed SKILL.md files explicitly; checked YAML frontmatter names against directory names and non-empty descriptions.
- Checked bundled local Markdown links, inline resource paths, and Discord section anchors. Code examples refer to illustrative application files; those are not missing skill resources.
- Executor skill discovery returned an empty list after installation. Files are installed, but automatic discovery in this running task is not confirmed. Explicit reads were used instead.
- Inspected Git changes against the verified snapshot baseline: only `.agents/skills/` additions. Application files and requirements remain unchanged.
- `git diff --check` reports trailing spaces on the final line of the three identical wshobson LICENSE copies. These upstream bytes were deliberately preserved for hash and license fidelity; no other whitespace issues were reported.
- Application tests and live Discord, YouTube, Gemini, and OpenRouter integrations were not run for this documentation-only installation.

## Upstream blob manifest

Git blob SHA-1 values permit verification of unmodified third-party files.

| Installed path (relative to .agents/skills) | Upstream blob SHA-1 |
| --- | --- |
| `async-python-patterns/SKILL.md` | `15905ae02e745a00cd235dfb18665344ca76d7d0` |
| `async-python-patterns/references/details.md` | `18de19d2e2457a4d310a64c945fe881baf2a5a01` |
| `async-python-patterns/LICENSE` | `326f0a55c96e672fedf9d807ca043c00df05ba0e` |
| `python-resilience/SKILL.md` | `89c1e9d9ba0134709f9b1ac876d0b401362afd4d` |
| `python-resilience/references/details.md` | `491f93480fed2f54856da04b581e5c6fdae87cbc` |
| `python-resilience/LICENSE` | `326f0a55c96e672fedf9d807ca043c00df05ba0e` |
| `python-testing-patterns/SKILL.md` | `4fe8a4245c55e9e79e70b5f75ab387e2349f91dc` |
| `python-testing-patterns/references/advanced-patterns.md` | `da186a91e60c44339a28ddb3b6ce478b7d23af64` |
| `python-testing-patterns/references/details.md` | `bf296f1634742776817e8cfbb5800927b72855d2` |
| `python-testing-patterns/LICENSE` | `326f0a55c96e672fedf9d807ca043c00df05ba0e` |
| `discord-py/SKILL.md` | `df6bb18930fe4d09647e2a105183d7fbd3b2a93e` |
| `discord-py/examples.md` | `6f0caa1c66b9ca9ce80c6347281716078f4f726d` |
| `discord-py/reference.md` | `541c3da6ad61b40ef6428c967542116e1fa46f8c` |
| `discord-py/LICENSE` | `bf4c1f75c1eceb0e1bb0377c40507b5e53b06d99` |
| `ai-agents-architect/SKILL.md` | `903cb800ca89d542b9e5c05a0982d37ed597a00b` |
| `ai-agents-architect/LICENSE` | `dc38cfd7ab732a67b0f14997cfa8c9ec740a1889` |
| `ai-agents-architect/LICENSE-CONTENT` | `e9f6e9443d4a5e17375377208e3ed77dea428825` |
| `ai-agents-architect/docs/sources/sources.md` | `fa92cd0e4f441054b024820289074dbdbfb5e74b` |
