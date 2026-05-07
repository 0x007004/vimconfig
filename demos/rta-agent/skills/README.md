# Skills

Each `*.md` file is one piece of crystallized operational knowledge.
Format:

```markdown
---
id: <stable.dotted.id>
keywords: [k1, k2, ...]
created_by: <agent | name>
created_at: YYYY-MM-DD
source: <pr-link | feishu-link | ...>
verified: true | false
hit_count: <int>
---

# Title

## Problem
## Answer
## Counter-example (the gold)
## Notes
```

The agent matches `keywords` first, then falls back to body substring search.
A skill that has been useful many times (high `hit_count`) and verified by
a second human (`verified: true`) is more trusted than a fresh one.
