#!/usr/bin/env python3
"""
Rebuild a Mintlify project from Filament's source docs.

Usage:
    python3 to_mintlify.py <docs-dir> <out-dir> [<docs-assets-dir>]

What it does (first pass, based on the structure of the source pages):
  - frontmatter (title, description) is kept, other keys are dropped
  - numeric filename prefixes (01-foo.md) are stripped from output paths
  - <Aside variant="x"> -> <Warning>/<Info>/<Tip>/<Check>/<Note>
  - <Disclosure> + <span slot="summary"> -> <Accordion title="...">
  - Astro `import ...` lines are removed
  - folders become nested navigation groups in docs.json
  - <AutoScreenshot name="a/b"> -> <Frame><img> using docs-assets/screenshots/images/light/a/b.*
    (missing files are dropped and counted; the dark folder is not copied)
  - RadioGroup + <div x-show="model === 'value'"> blocks (Alpine) -> <Tabs>/<Tab>
  - <UtilityInjection extras="..."> -> its prose, extras turned into a bullet list
  - docs-assets is copied into the project (without dark variants)
  - prints an inventory of components it did NOT handle (these will need rules)
"""
import collections
import json
import re
import shutil
import sys
from pathlib import Path

ASIDE = {"warning": "Warning", "danger": "Warning", "info": "Info",
         "tip": "Tip", "success": "Check"}
KNOWN = {"Note", "Warning", "Info", "Tip", "Check", "Accordion", "AccordionGroup",
         "Tabs", "Tab", "Steps", "Step", "Card", "CardGroup", "Frame", "CodeGroup"}
unhandled = collections.Counter()
image_refs = collections.Counter()

# Screenshots: <AutoScreenshot name="a/b/c" alt="..." /> ->
#   <assets>/screenshots/images/light/a/b/c.<ext>   (light variant only)
ASSETS = None                      # set in main() from argv[3]
SHOT_BASE = "screenshots/images/light"
missing_shots = []
converted_shots = 0

# Components with no Mintlify equivalent: the tags are removed, inner content kept.
# Check the pages that use them by hand afterwards.
STRIP_TAGS = []
stripped = collections.Counter()


FENCE_RE = re.compile(r"^[ \t]*```.*?^[ \t]*```[ \t]*$", re.S | re.M)
RADIO_OR_DIV_RE = re.compile(r"<RadioGroup\b[^>]*>.*?</RadioGroup>|<div\b[^>]*>|</div>", re.S)
OPT_RE = re.compile(r"<RadioGroupOption\b([^>]*)>(.*?)</RadioGroupOption>", re.S)
DESC_RE = re.compile(r'<span slot="description">(.*?)</span>', re.S)
XSHOW_RE = re.compile(r"x-show=\"(\w+)\s*===\s*'([^']*)'\"")
alpine_left = 0


def convert_radio(body):
    """RadioGroup selector + x-show divs -> Tabs. Code fences are left untouched."""
    if "<RadioGroup" not in body:
        return body
    groups = {}   # model -> {"opts": {value: (label, desc)}, "seen": int}
    stack = []    # per open <div>: None (plain, dropped) or (model, closes_tabs)

    def repl(m):
        tok = m.group(0)
        if tok.startswith("<RadioGroup"):
            model = re.search(r'model="([^"]*)"', tok).group(1)
            opts = {}
            for om in OPT_RE.finditer(tok):
                value = re.search(r'value="([^"]*)"', om.group(1)).group(1)
                dm = DESC_RE.search(om.group(2))
                desc = " ".join(dm.group(1).split()) if dm else ""
                label = " ".join(DESC_RE.sub("", om.group(2)).split())
                opts[value] = (label, desc)
            groups[model] = {"opts": opts, "seen": 0}
            return ""
        if tok == "</div>":
            top = stack.pop() if stack else None
            if not top:
                return ""
            return "\n</Tab>\n" + ("</Tabs>\n" if top[1] else "")
        sm = XSHOW_RE.search(tok)
        if sm and sm.group(1) in groups:
            g = groups[sm.group(1)]
            label, desc = g["opts"].get(sm.group(2), (sm.group(2), ""))
            first = g["seen"] == 0
            g["seen"] += 1
            stack.append((sm.group(1), g["seen"] == len(g["opts"])))
            out = "\n<Tabs>\n" if first else ""
            out += f'\n<Tab title="{label.replace(chr(34), "&quot;")}">\n'
            if desc:
                out += f"\n*{desc}*\n"
            return out
        stack.append(None)
        return ""

    parts, pos = [], 0
    for fm in FENCE_RE.finditer(body):
        parts.append(RADIO_OR_DIV_RE.sub(repl, body[pos:fm.start()]))
        parts.append(fm.group(0))
        pos = fm.end()
    parts.append(RADIO_OR_DIV_RE.sub(repl, body[pos:]))
    return "".join(parts)


def convert_utility(body):
    def repl(m):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        text = m.group(2).strip()
        items = []
        for e in attrs.get("extras", "").split("||"):
            f = e.split(";;")
            if len(f) == 4:
                items.append(f"- `{f[2]}` (`{f[1]}`): {f[3]}")
        if items:
            text += "\n\nAdditional utilities available here:\n\n" + "\n".join(items)
        return text

    body = re.sub(r"<UtilityInjection\b([^>]*)>(.*?)</UtilityInjection>", repl, body, flags=re.S)
    return re.sub(r"<UtilityInjection\b[^>]*/>\n?", "", body)


def convert_screenshots(body):
    global converted_shots

    def repl(m):
        global converted_shots
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        name, alt = attrs.get("name", ""), attrs.get("alt", "")
        found = None
        if ASSETS and name:
            p = ASSETS / SHOT_BASE / name
            hits = sorted(p.parent.glob(p.name + ".*")) if p.parent.is_dir() else []
            found = hits[0] if hits else None
        if not found:
            missing_shots.append(name)
            return ""
        converted_shots += 1
        rel = found.relative_to(ASSETS).as_posix()
        alt = alt.replace('"', "&quot;")
        return (f'<Frame caption="{alt}">\n'
                f'  <img src="/{ASSETS.name}/{rel}" alt="{alt}" />\n</Frame>')

    body = re.sub(r"<AutoScreenshot\b([^>]*?)/>", repl, body, flags=re.S)
    for tag in STRIP_TAGS:
        body, n = re.subn(rf"[ \t]*</?{tag}\b[^>]*>[ \t]*\n?", "", body)
        stripped[tag] += n
    return body


def strip_num(name):
    return re.sub(r"^\d+[-_.]\s*", "", name)


def natkey(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def pretty(name):
    return strip_num(name).replace("-", " ").replace("_", " ").title()


def parse_frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).split("\n"):
        k, sep, v = line.partition(":")
        if sep and re.match(r"^[A-Za-z_][\w-]*$", k.strip()):
            meta[k.strip()] = v.strip().strip("\"'")
    return meta, text[m.end():]


def convert_body(body):
    body = convert_radio(body)
    body = convert_utility(body)
    body = convert_screenshots(body)
    out, stack = [], []
    fence = False
    pending_disc = False
    for line in body.split("\n"):
        s = line.strip()
        if s.startswith("```"):
            fence = not fence
            out.append(line[4:] if stack and line.startswith("    ") else line)
            continue
        if fence:
            out.append(line[4:] if stack and line.startswith("    ") else line)
            continue
        if s.startswith("import ") and " from " in s:
            continue

        # single-line <Aside ...>text</Aside>
        m = re.match(r'^<Aside(?:\s+variant="([^"]*)")?[^>]*>(.*)</Aside>$', s)
        if m:
            tag = ASIDE.get(m.group(1) or "", "Note")
            out.append(f"<{tag}>{m.group(2)}</{tag}>")
            continue
        m = re.match(r'^<Aside(?:\s+variant="([^"]*)")?[^>]*>(.*)$', s)
        if m:
            tag = ASIDE.get(m.group(1) or "", "Note")
            stack.append(tag)
            out.append(f"<{tag}>")
            if m.group(2).strip():
                out.append(m.group(2).strip())
            continue
        if s == "</Aside>":
            out.append(f"</{stack.pop() if stack else 'Note'}>")
            continue

        if re.match(r"^<Disclosure[^>]*>$", s):
            stack.append("Accordion")
            pending_disc = True
            continue
        if s == "</Disclosure>":
            out.append("</Accordion>")
            if stack:
                stack.pop()
            continue
        m = re.match(r'^<span slot="summary">(.*)</span>$', s)
        if m:
            title = m.group(1).replace('"', "&quot;")
            out.append(f'<Accordion title="{title}">')
            pending_disc = False
            continue
        if pending_disc and s:
            out.append('<Accordion title="Details">')
            pending_disc = False

        out.append(line[4:] if stack and line.startswith("    ") else line)

    text = "\n".join(out)
    # inventory of anything component-like left outside code fences
    infence = False
    for l in text.split("\n"):
        if l.strip().startswith("```"):
            infence = not infence
        if infence:
            continue
        l = re.sub(r"`[^`]*`", "", l)          # ignore inline code
        if re.search(r"\bx-(show|data|cloak|on|bind)\b", l):
            global alpine_left
            alpine_left += 1
        for t in re.findall(r"<([A-Z][A-Za-z0-9.]*)", l):
            if t not in KNOWN:
                unhandled[t] += 1
        for t in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", l):
            image_refs[t.split("/")[0] or "/"] += 1
    return text


def convert_file(src, dst):
    meta, body = parse_frontmatter(src.read_text(encoding="utf-8"))
    title = meta.get("title") or pretty(src.stem)
    fm = ["---", f"title: {json.dumps(title)}"]
    if meta.get("description"):
        fm.append(f"description: {json.dumps(meta['description'])}")
    fm.append("---\n")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(fm) + convert_body(body), encoding="utf-8")


def build(src_dir, out_dir, rel):
    entries = []
    for p in sorted(src_dir.iterdir(), key=lambda p: natkey(p.name)):
        if p.name.startswith((".", "_")):
            continue
        if p.is_dir():
            sub = build(p, out_dir, rel / strip_num(p.name))
            if sub:
                entries.append({"group": pretty(p.name), "pages": sub})
        elif p.suffix in (".md", ".mdx"):
            slug = rel / strip_num(p.stem)
            convert_file(p, out_dir / f"{slug.as_posix()}.mdx")
            entries.append(slug.as_posix())
    return entries


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    global ASSETS
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    ASSETS = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else None
    out.mkdir(parents=True, exist_ok=True)
    entries = build(src, out, Path("."))

    loose = [e for e in entries if isinstance(e, str)]
    groups = [e for e in entries if isinstance(e, dict)]
    nav = ([{"group": "General", "pages": loose}] if loose else []) + groups

    (out / "docs.json").write_text(json.dumps({
        "$schema": "https://mintlify.com/docs.json",
        "theme": "mint",
        "name": "Filament 5.x (offline)",
        "colors": {"primary": "#d97706"},
        "navigation": {"groups": nav},
    }, indent=2), encoding="utf-8")

    if ASSETS:
        # copy everything except the dark screenshot variants
        shutil.copytree(ASSETS, out / ASSETS.name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("dark"))

    print(f"Wrote {out}/docs.json and pages.")
    print(f"Screenshots converted: {converted_shots}, missing (dropped): {len(missing_shots)}")
    for n in missing_shots[:10]:
        print(f"  missing: {n}")
    if stripped and sum(stripped.values()):
        print("Tags stripped (content kept, review by hand): "
              + ", ".join(f"{k} x{v}" for k, v in stripped.items() if v))
    if alpine_left:
        print(f"\nWARNING: {alpine_left} line(s) still contain Alpine attributes (x-show/x-data...)")
    if unhandled:
        print("\nComponents NOT converted (these will break in Mintlify, need rules):")
        for t, n in unhandled.most_common():
            print(f"  {n:5d}  <{t}>")
    if image_refs:
        print("\nMarkdown image path prefixes seen:")
        for t, n in image_refs.most_common(10):
            print(f"  {n:5d}  {t}")
    print(f"\nNext: cd {out} && npx mint dev")


if __name__ == "__main__":
    main()
