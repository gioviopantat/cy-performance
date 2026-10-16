# Ride PDF does not render

The per-ride PDF (`cyp.reports.ride_pdf`, docs/specs/ride-report.md) is HTML -> PDF with
WeasyPrint, which needs system libraries (pango, harfbuzz, fontconfig) and a zh-TW font.

## `WeasyPrint could not import some external libraries` / `OSError: cannot load library`
pango is not installed, or not where the Python process looks.

```bash
brew list pango || brew install pango    # macOS (the always-on Mac)
uv run python -c "from cyp.reports.ride_pdf import _write_pdf; from pathlib import Path; _write_pdf('<p>ok</p>', Path('/tmp/probe.pdf'))"
```

uv's Python does not search Homebrew's lib dir; `ride_pdf` adds `/opt/homebrew/lib` (and
`/usr/local/lib`) to `DYLD_FALLBACK_LIBRARY_PATH` before importing WeasyPrint. If Homebrew lives
elsewhere, export `DYLD_FALLBACK_LIBRARY_PATH=$(brew --prefix)/lib` in the launch agent's
environment. Linux: `apt-get install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0`.

## Text is garbled (wrong characters), but copy-paste from the PDF reads correctly
WeasyPrint subsets a font that is not the first face of a `.ttc` collection with the wrong
glyphs. PingFang TC is such a face, so `templates/ride.css` lists Arial + Heiti TC and never
PingFang (`tests/test_ride_pdf.py` checks that PingFang is not embedded). Do not reorder the
`font-family` list to put a later `.ttc` face first.

## Boxes (tofu) instead of zh-TW characters
No zh-TW font was found. macOS ships Heiti TC; on Linux install `fonts-noto-cjk`. Charts use
matplotlib with the same list (`charts.CJK_FONTS`); `charts.cjk_font()` returns `None` when
none is installed.

## Check a PDF by hand
```bash
uv run python -c "import pypdf,sys; r=pypdf.PdfReader(sys.argv[1]); print(len(r.pages)); print(r.pages[0].extract_text()[:300])" <path>.pdf
```
