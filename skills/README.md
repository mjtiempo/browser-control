# Agent skills

These are [Agent Skills](https://agentskills.io/specification) (`SKILL.md`
directories) that teach a coding agent how to drive `browser-control-cli`
correctly — the verify-or-refuse stance, the canonical lifecycle, working
recipes, capabilities, and the refusal codes. They are instructions only; the
tool itself is this repository.

| skill | use it when |
| --- | --- |
| `browser-control` | driving a page: open, read, click, type, wait, screenshot, recover |
| `browser-control-extract` | scraping repeated items/records, pagination loops, frames |
| `browser-control-sessions` | logins, seeded profiles, multiple accounts, attach/detach |
| `browser-control-plugins` | the shipped `google`/`x` search actions; writing a site adapter |

The CLI must be on `PATH` (`python3 -m pip install .`, or run
`./browser-control-cli` from a checkout and adapt the command).

## Install

Point an Agent Skills host at these directories. For pi, either symlink each
(nothing to keep in sync) or copy them:

```bash
# option A: symlink
for s in browser-control browser-control-extract browser-control-sessions \
         browser-control-plugins; do
  ln -sfn "$PWD/skills/$s" ~/.pi/agent/skills/$s
done

# option B: copy
cp -r skills/browser-control skills/browser-control-extract \
      skills/browser-control-sessions skills/browser-control-plugins \
      ~/.pi/agent/skills/
```

`~/.agents/skills/` is the portable Agent Skills location and works the same
way; project-level `.agents/skills/` is discovered from the working directory.
Restart the agent (or `/reload` in pi) after installing, then confirm the
skills are listed and check the CLI itself with
`browser-control-cli selftest | jq '{ok, browsers, plugins, plugin_errors}'`.
