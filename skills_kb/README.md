# BotSleep v2 Dynamic Micro-Skills Knowledge Base (`skills_kb/`)

Each `*.md` card in this directory is retrieved on-demand (Top-3 per LLM turn) by `v2.planner.skill_retriever.BM25SkillRetriever` using Blackboard tags + zero-dependency BM25 scoring.

## Authoring Rules (Aligned with `AGENTS.md`)

1. **Line 1 Tag Header**: `#tag1 #tag2 #tag3` matching Blackboard state tags (e.g. `#nether_portal #speedrun #lava #water_bucket`).
2. **Length Budget**: `300–500` Chinese characters per card (`<= 500` bytes UTF-8 target, hard cap `600` bytes) so Top-3 cards stay strictly `<= 1,500` bytes.
3. **Structure**:
   - **Goal & Preconditions** (1–2 lines)
   - **Bound Tool / Task Call Signature** (exact registered names & units only; never advertise unbound tools)
   - **<= 5-Step Playbook & Abort/Pitfall Conditions**
4. **No Formulas / Verbose Derivations**: Keep math, raycast geometry, sigma fitting, and coordinate transforms inside `v2/gateway/` or `v2/tasks/`, never inside model context cards.
