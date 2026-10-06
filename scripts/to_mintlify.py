#!/usr/bin/env python3
"""
Rebuild a Mintlify project from Filament's source docs.

Usage (recommended: point it at a clone of filamentphp/filament):
    python3 to_mintlify.py <filament-repo> <out-dir>

  It reads <repo>/docs (general pages) AND every <repo>/packages/*/docs (each package's own
  docs, e.g. packages/tables/docs/03-filters/...), so nothing is missed. General pages sit at
  the top level of the sidebar; each package becomes a nested group (e.g. "Tables"), with the
  package name as the first URL segment (tables/filters/overview).
  <repo>/docs-assets is used for screenshots automatically.

  Loose .md/.mdx files sitting directly in <repo>/docs/*.md each become their OWN top-level
  section (named after their frontmatter title), rather than being bundled into one "General"
  group. Folder-based sections in <repo>/docs are unaffected. Package docs are unaffected.

Old usage (a single docs folder) still works:
    python3 to_mintlify.py <docs-dir> <out-dir> [<docs-assets-dir>]

What it does (first pass, based on the structure of the source pages):
  - frontmatter (title, description) is kept, other keys are dropped
  - numeric filename prefixes (01-foo.md) are stripped from output paths
  - <Aside variant="x"> -> <Warning>/<Info>/<Tip>/<Check>/<Note>
  - <Disclosure> + <span slot="summary"> -> <Accordion title="...">
  - Astro `import ...` lines are removed
  - folders become nested navigation groups in docs.json
  - <AutoScreenshot name="a/b"> -> <Frame> with two <img>:
      docs-assets/screenshots/images/light/a/b.*    (shown in light mode)
      docs-assets/screenshots/images/dark/a/b.*     (shown in dark mode)
    Dark images use Mintlify's `#dark-only` fragment; light uses `#light-only`.
    For package docs, both <pkg>/<name> and <name> are tried, for each variant.
    Missing files are dropped and counted.
  - RadioGroup + <div x-show="model === 'value'"> blocks (Alpine) -> <Tabs>/<Tab>
  - <UtilityInjection extras="..."> -> its prose, extras turned into a bullet list
  - docs-assets is copied into the project (both light and dark trees)
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
#   <assets>/screenshots/images/light/a/b/c.<ext>   (light variant)
#   <assets>/screenshots/images/dark/a/b/c.<ext>    (dark variant)
ASSETS = None                      # set in main() from argv[3]
SHOT_BASE = "screenshots/images"
SHOT_LIGHT = f"{SHOT_BASE}/light"
SHOT_DARK = f"{SHOT_BASE}/dark"
missing_shots = []                 # (pkg_prefix, name, variant)
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


def _find_shot(name, variant, pkg_prefix):
    """Locate a screenshot file in the light or dark tree, trying <pkg>/ first."""
    if not ASSETS or not name:
        return None
    base = ASSETS / SHOT_BASE / variant
    candidates = []
    if pkg_prefix:
        candidates.append(base / pkg_prefix / name)
    candidates.append(base / name)
    for p in candidates:
        hits = sorted(p.parent.glob(p.name + ".*")) if p.parent.is_dir() else []
        if hits:
            return hits[0]
    return None


def convert_screenshots(body, pkg_prefix=None):
    """Convert <AutoScreenshot> to a <Frame> with light + dark <img> tags.

    Mintlify shows an image only in one theme if its URL ends with
    `#light-only` or `#dark-only`. We emit both, with the same alt/caption.
    If only one variant exists, we fall back to showing just that one
    (still tagged, so it disappears in the other theme only if both exist).
    """
    global converted_shots

    def repl(m):
        global converted_shots
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        name, alt = attrs.get("name", ""), attrs.get("alt", "")
        alt = alt.replace('"', "&quot;")
        light = _find_shot(name, "light", pkg_prefix)
        dark = _find_shot(name, "dark", pkg_prefix)
        if not light and not dark:
            missing_shots.append((pkg_prefix, name, "both"))
            return ""
        if not dark:
            missing_shots.append((pkg_prefix, name, "dark"))
        if not light:
            missing_shots.append((pkg_prefix, name, "light"))
        converted_shots += 1

        imgs = []
        if light:
            rel = light.relative_to(ASSETS).as_posix()
            imgs.append(f'  <img src="/{ASSETS.name}/{rel}#light-only" alt="{alt}" />')
        if dark:
            rel = dark.relative_to(ASSETS).as_posix()
            imgs.append(f'  <img src="/{ASSETS.name}/{rel}#dark-only" alt="{alt}" />')
        caption = f' caption="{alt}"' if alt else ""
        return f'<Frame{caption}>\n' + "\n".join(imgs) + "\n</Frame>"

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


def convert_body(body, pkg_prefix=None):
    body = convert_radio(body)
    body = convert_utility(body)
    body = convert_screenshots(body, pkg_prefix)
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


def convert_file(src, dst, pkg_prefix=None):
    meta, body = parse_frontmatter(src.read_text(encoding="utf-8"))
    title = meta.get("title") or pretty(src.stem)
    fm = ["---", f"title: {json.dumps(title)}"]
    if meta.get("description"):
        fm.append(f"description: {json.dumps(meta['description'])}")
    fm.append("---\n")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(fm) + convert_body(body, pkg_prefix), encoding="utf-8")
    return title


skipped = []          # (path, reason)
written = {}          # output slug -> source path
page_titles = {}      # output slug -> frontmatter title (for sidebar labels)
collisions = []       # (source path, final slug)


def build(src_dir, out_dir, rel, pkg_prefix=None):
    entries = []
    for p in sorted(src_dir.iterdir(), key=lambda p: natkey(p.name)):
        if p.name.startswith((".", "_")):
            skipped.append((p, "hidden/underscore name"))
            continue
        if p.is_dir():
            sub = build(p, out_dir, rel / strip_num(p.name), pkg_prefix)
            if sub:
                entries.append({"group": pretty(p.name), "pages": sub})
            else:
                skipped.append((p, "folder produced no pages"))
        elif p.suffix in (".md", ".mdx"):
            name = strip_num(p.stem)
            if (rel / name).as_posix() in written:      # collision after stripping prefix
                name = p.stem                           # keep the prefix instead
                n = 2
                while (rel / name).as_posix() in written:
                    name, n = f"{p.stem}-{n}", n + 1
                collisions.append((p, (rel / name).as_posix()))
            slug = (rel / name).as_posix()
            written[slug] = p
            page_titles[slug] = convert_file(p, out_dir / f"{slug}.mdx", pkg_prefix=pkg_prefix)
            entries.append(slug)
        else:
            skipped.append((p, f"not markdown ({p.suffix or 'no extension'})"))
    return entries


PKG_ORDER = ["panels", "schemas", "forms", "infolists", "tables", "actions",
             "notifications", "widgets", "support"]
IGNORED_DIRS = {"node_modules", "vendor", ".git", ".github", "bin", "docs-assets",
                ".agents", ".amp", "tests", "art", "bootstrap", ".idea", ".vscode"}
IGNORED_FILES = {"readme", "changelog", "license", "contributing", "security", "upgrade",
                 "agents", "claude", "skill", "code_of_conduct"}


def md_files(root, extra_ignored=()):
    """All .md/.mdx under root, skipping any path that passes through an ignored dir."""
    ignored = IGNORED_DIRS | set(extra_ignored)
    out = []
    for f in root.rglob("*"):
        if not f.is_file() or f.suffix not in (".md", ".mdx"):
            continue
        if any(part in ignored for part in f.parts):
            continue
        out.append(f)
    return out


def discover(repo):
    """(docs_dir, url_prefix, group_title) for the root docs and each package's docs."""
    sources, no_docs = [], []
    if (repo / "docs").is_dir():
        sources.append((repo / "docs", Path("."), "General"))
    pk = repo / "packages"
    if pk.is_dir():
        names = sorted((d.name for d in pk.iterdir() if d.is_dir()),
                       key=lambda n: (PKG_ORDER.index(n) if n in PKG_ORDER else 99, n))
        for n in names:
            if (pk / n / "docs").is_dir():
                sources.append((pk / n / "docs", Path(n), pretty(n)))
            else:
                no_docs.append(n)
    return sources, no_docs


def to_groups(entries, loose_title):
    loose = [e for e in entries if isinstance(e, str)]
    groups = [e for e in entries if isinstance(e, dict)]
    return ([{"group": loose_title, "pages": loose}] if loose else []) + groups


def main():
    global ASSETS
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    first, out = Path(sys.argv[1]), Path(sys.argv[2])
    repo_mode = (first / "packages").is_dir()
    if repo_mode:
        sources, no_docs = discover(first)
        assets = Path(sys.argv[3]) if len(sys.argv) > 3 else first / "docs-assets"
    else:
        sources, no_docs = [(first, Path("."), "General")], []
        assets = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    ASSETS = assets.resolve() if assets and assets.is_dir() else None
    out.mkdir(parents=True, exist_ok=True)

    # One flat list of groups for the whole sidebar:
    #   - general docs come first. Loose pages at the root of /docs each become
    #     their OWN top-level section (named after their frontmatter title).
    #     Folders in /docs become groups as usual.
    #   - then each package as a single top-level group whose pages are that
    #     package's groups (or its loose pages if it has none).
    sidebar_groups = []
    all_src, per_source = [], []
    for src, prefix, title in sources:
        before = len(written)
        pkg_prefix = None if prefix == Path(".") else prefix.name
        entries = build(src, out, prefix, pkg_prefix=pkg_prefix)
        files = md_files(src)
        all_src += files
        per_source.append((src, len(files), len(written) - before))

        if prefix == Path("."):
            loose = [e for e in entries if isinstance(e, str)]
            groups = [e for e in entries if isinstance(e, dict)]
            # Root /docs: give each loose page its own section instead of
            # bundling them all under a single "General" group.
            for slug in loose:
                meta_title = page_titles.get(slug, pretty(Path(slug).stem))
                sidebar_groups.append({"group": meta_title, "pages": [slug]})
            sidebar_groups.extend(groups)
        else:
            inner = to_groups(entries, title)
            if len(inner) == 1 and inner[0].get("group") == title:
                pages = inner[0]["pages"]
            else:
                pages = inner
            sidebar_groups.append({"group": title, "pages": pages})

    navigation = {"tabs": [{"tab": "Docs", "groups": sidebar_groups}]}

    (out / "docs.json").write_text(json.dumps({
        "$schema": "https://mintlify.com/docs.json",
        "theme": "mint",
        "name": "Filament 5.x (offline)",
        "colors": {"primary": "#d97706"},
        "navigation": navigation,
    }, indent=2), encoding="utf-8")

    if ASSETS:
        # Copy docs-assets wholesale, but skip the light/dark variant folders'
        # dark subtree only if you want to save space — by default we keep both
        # so the theme toggle works. Drop the `ignore_patterns` if you want
        # everything (e.g. extra variants) copied.
        shutil.copytree(ASSETS, out / ASSETS.name, dirs_exist_ok=True)
    elif repo_mode:
        print(f"WARNING: no docs-assets folder found at {assets}; screenshots will be dropped")

    print(f"Wrote {out}/docs.json and pages.\n")
    print(f"{'source folder':60} {'md files':>8} {'pages':>6}")
    for src, n_files, n_pages in per_source:
        flag = "" if n_files == n_pages else "   <-- mismatch"
        print(f"{str(src):60} {n_files:8d} {n_pages:6d}{flag}")
    print(f"{'TOTAL':60} {len(all_src):8d} {len(written):6d}")
    if no_docs:
        print(f"\nPackages with NO docs folder (nothing to convert): {', '.join(no_docs)}")

    done = set(written.values())
    missed = [f for f in all_src if f not in done]
    if missed:
        print("\nNOT converted:")
        for f in missed:
            print(f"    {f}")

    if repo_mode:
        covered = set(all_src)
        extra = collections.Counter()
        for f in md_files(first):
            if (f in covered
                    or f.stem.lower() in IGNORED_FILES):
                continue
            extra[str(f.parent.relative_to(first))] += 1
        if extra:
            print("\nOther markdown in the repo NOT included (check none of this is documentation):")
            for d, n in extra.most_common(15):
                print(f"  {n:4d}  {d}")

    if collisions:
        print("\nName collisions (prefix kept to avoid overwriting):")
        for f, slug in collisions:
            print(f"    {f} -> {slug}")
    if skipped:
        print(f"\nSkipped entries ({len(skipped)}):")
        for f, why in skipped[:25]:
            print(f"    {f}  [{why}]")
        if len(skipped) > 25:
            print(f"    ... and {len(skipped) - 25} more")
    print(f"\nScreenshots converted: {converted_shots}, missing variants (dropped): {len(missing_shots)}")
    for pkg, n, variant in missing_shots[:10]:
        tag = f"[{pkg}] " if pkg else ""
        print(f"  missing ({variant}): {tag}{n}")
    if stripped and sum(stripped.values()):
        print("Tags stripped (content kept, review by hand): "
              + ", ".join(f"{k} x{v}" for k, v in stripped.items() if v))
    if alpine_left:
        print(f"\nWARNING: {alpine_left} line(s) still contain Alpine attributes (x-show/x-data...)")
    if unhandled:
        print("\nComponents NOT converted (these will break in Mintlify, need rules):")
        for t, n in unhandled.most_common():
            print(f"  {n:5d}  <{t}>")
    print(f"\nNext: cd {out} && npx mint dev")


if __name__ == "__main__":
    main()
