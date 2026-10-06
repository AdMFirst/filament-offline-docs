# Filament Docs, Offline Edition

## Disclaimer

**This is an unofficial community project. It is not affiliated with, endorsed by, or supported by the Filament team.**

The official, up-to-date documentation lives at <https://filamentphp.com/docs>. Anything you find here may be outdated or converted imperfectly, so when something looks wrong, trust the official site. Please do not report problems with this repo to the Filament maintainers.

## What this is for

Filament's documentation is only published online. That is fine most of the time, but some of us develop on air-gapped or heavily restricted machines. In that situation the docs have to be read on a separate device, which means constantly switching screens and copying code by hand. Some people also have unreliable or blocked internet access, and documentation that depends on a live connection stops being useful.

This project exists so you can carry the Filament docs to such a machine on a USB stick and browse them locally, in a real docs site with navigation and screenshots, instead of a pile of Markdown files.

It is inspired by two things:

- [richeklein/filament3-books](https://github.com/richeklein/filament3-books), which turned the Filament 3 docs into a single-page HTML file and an ePub for offline reading. This project does the same job for the current version, but as a browsable docs site.
- [Iran Mirrors / Docs Hub](https://docs.iranisoft.ir/iran-mirror), a service that hosts documentation for developers when internet access is unavailable.

## How it is made

The conversion script (`scripts/to_mintlify.py`) was written with the help of Claude and then tested against real pages from the docs.

It is not really scraping. It never crawls `filamentphp.com`. Filament's documentation is written in Markdown and published in the open-source [filamentphp/filament](https://github.com/filamentphp/filament) repository under the **MIT license**, which allows copying, modifying and redistributing it as long as the license notice is kept. The script works on a normal clone of that repository:

1. It reads the Markdown files in `docs/` and the screenshots in `docs-assets/`.
2. It converts the Astro-specific parts (callouts, collapsible sections, screenshot tags, the installation selector) into their [Mintlify](https://mintlify.com) equivalents, since the live site is built with Mintlify.
3. It generates a `docs.json` navigation from the folder structure, so the result can be previewed with the Mintlify CLI or exported as a static site.

Each release includes Filament's `LICENSE.md` as `FILAMENT-LICENSE.md`. If you redistribute a copy, keep it with the files.

Because the script is AI-assisted and was written against a sample of the docs, treat the output as a convenience copy. It prints a report of anything it could not convert, and some content (see below) is lost.

## How to use it

### Download a release

Go to the **Releases** page and pick the version you need. Each release contains one or both of the following files.

**`filament-docs-<version>-site.zip`** is the easiest, if the release includes it. It is a self-contained static site and needs only Node.js 20.17 or newer on the offline machine:

```bash
unzip filament-docs-*-site.zip -d filament-docs
cd filament-docs
node serve.js
```

Then open the address it prints in your browser.

**`filament-docs-<version>-project.zip`** is the converted Mintlify project. It needs the Mintlify CLI (Node.js 20.17 or newer). On a machine with internet:

```bash
unzip filament-docs-*-project.zip -d filament-docs
cd filament-docs
npm i mint
npx mint dev        # run once online so the CLI can cache what it needs
```

Then copy the folder (including `node_modules`) and your `~/.mintlify` folder to the offline machine. Use the same operating system, CPU architecture and Node.js version, and run `npx mint dev` there.

### Build it yourself

You only need Python 3 and Git for the conversion:

```bash
git clone --depth 1 --branch 5.x https://github.com/filamentphp/filament.git
python3 scripts/to_mintlify.py filament/docs mint-out filament/docs-assets
cd mint-out
npx mint dev
```

Check that `filament/docs` and `filament/docs-assets` exist first, since the script assumes that layout. Only the light-theme screenshots are used (`docs-assets/screenshots/images/light/`).

To produce the self-contained static site, run `npx mint export --output filament-docs-site.zip` inside `mint-out`. Mintlify's documentation says offline export may require a paid plan, so this step may not work for everyone.

Maintainers can also run the **build-offline-docs** workflow from the Actions tab, enter a Filament branch such as `5.x`, and it will publish a release automatically.

## Known limitations

- Screenshots missing from `docs-assets` are dropped. The script prints how many.
- The installation page's interactive selector becomes tabs, and the `#components` URL hash no longer preselects one.
- The lists of injectable utilities that the live site generates are not reproduced.
- Icons and fonts that Mintlify loads from a CDN may be missing offline. This is cosmetic.
- Any custom component the script does not know about is reported at the end of the run and needs a new rule.

## License

The scripts in this repository are released under the license in `LICENSE`. The documentation content belongs to the Filament project and is redistributed under Filament's MIT license.
