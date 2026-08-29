# 15 — Generation Tools: PDF, PPTX, Image

> **Pre-build draft.** Written ahead of the milestone; revise after it ships to describe what actually shipped.

> **Changed since writing:** `.149` was dropped from the critical path, so **ComfyUI runs on `.226`**
> alongside the fast tier, governed by admission control rather than owning a card
> ([`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md)). The model choice is unchanged —
> FLUX.1-schnell, Apache-2.0. Read every `.149` reference below as `.226`, and note the new rule:
> an image request never preempts a coding session, it queues.

> The three artefact-producing tools behind [`14-mcp-tool-server.md`](./14-mcp-tool-server.md).
> PDF and PPTX ship in **M6** on `.87`; image ships in **M7** on `.149`. Together they satisfy F9.

---

## 1. Concept — one shape for all three

Every one of these tools has the same temptation and the same answer.

**The temptation:** let the model produce the artefact directly — emit LaTeX, emit python-pptx calls,
emit a ComfyUI workflow graph. It feels flexible and it demos well.

**The answer: the model supplies *content*; the template supplies *form*.** The model writes a title,
some prose, a list of bullets, a description of a picture. The template — written once, by a human,
checked into the repo — decides typography, margins, slide layout, sampler settings, and image size.

Four reasons, in descending order of how much they will bite:

1. **Reliability.** A local 14–30B model emits subtly invalid markup routinely. Anything it produces
   that must parse — LaTeX, XML, a node graph with integer ids — is a coin flip, and the failure lands
   at render time, after the user has already waited.
2. **Safety.** Model output is untrusted input. Typst can read files. A workflow graph can name
   arbitrary file paths. Treat every string coming from the model as hostile-by-accident and never let
   it reach a renderer's escape hatches. §2.4 makes this concrete.
3. **Consistency.** Twenty documents that look the same is a feature. Twenty documents that each
   reinvent their heading sizes is a mess nobody wants to hand a client.
4. **Context cost.** A raw-Typst tool needs Typst syntax in its description; a raw-workflow tool needs
   the node schema. Both blow the tool-context budget we defended in
   [`14`](./14-mcp-tool-server.md) §2.

```
   model output                    our code                       renderer
   -----------                     --------                       --------
   title + markdown body   -->  validate, escape, fill  -->  Typst   -->  .pdf
   title + slide outline   -->  validate, truncate, fill -->  pptx   -->  .pptx
   prompt + aspect         -->  validate, patch workflow -->  Comfy  -->  .png
                                       ^
                                template owned by us, in the repo
```

All three write into one **artefact store** on `.87` (§5), and all three return a file id plus a URL
rather than base64 in the tool result. A 3 MB image inlined into a tool response destroys the context
window of the model that asked for it.

---

## 2. PDF — Typst

### 2.1 Why Typst

| | Typst | LaTeX | HTML -> WeasyPrint / wkhtmltopdf | Pandoc -> LaTeX |
|---|---|---|---|---|
| Install | One binary (a few tens of MB) | Multi-gigabyte TeX distribution | Python stack + system libs | Pandoc **plus** TeX |
| Compile speed | Fast; incremental | Slow; multiple passes | Moderate | Slow |
| Error messages | Actionable, with line numbers | Famously opaque | CSS-print debugging | Inherits LaTeX's |
| Model-generated source | Usually valid | Subtly broken routinely | Fine, but CSS paged media is fiddly | Hides the problem, does not remove it |
| Templating | First-class functions | Macros | Any templating engine | Limited |
| Ecosystem | Younger, fewer packages | Enormous | Enormous | Enormous |

The install size alone would decide it — a TeX distribution on a shared workstation is an intrusion
we do not need — but the real reason is the fourth row. Whenever a model touches the source, a format
whose errors are actionable and whose syntax is regular is worth more than a bigger package library.

The cost is honest: fewer ready-made templates, and a smaller pool of people who can debug ours. Since
we intend to write exactly one or two templates and keep them, that is a good trade.

### 2.2 The template

Keep templates in `services/mcp-tools/app/templates/typst/`. Each template is a function taking
metadata and a content body:

```typst
// templates/typst/report.typ
#let report(title: "", author: "", date: none, body) = {
  set document(title: title, author: author)
  set page(paper: "a4", margin: (x: 2.2cm, y: 2.4cm),
           footer: context align(center, counter(page).display("1")))
  set text(font: ("Inter", "DejaVu Sans"), size: 10.5pt, lang: "en")
  set par(justify: true, leading: 0.65em)
  show heading.where(level: 1): it => block(above: 1.4em, below: 0.7em, text(size: 16pt, it))

  block(text(size: 22pt, weight: "bold", title))
  if author != "" { text(size: 10pt, fill: luma(90), author) }
  if date != none { text(size: 10pt, fill: luma(90), " - " + date) }
  line(length: 100%, stroke: 0.4pt + luma(180))
  v(1em)
  body
}
```

Fonts are a deployment concern, not a template concern: install the fonts inside the container image
and list a metric-compatible fallback in the `font` tuple. A template that silently falls back to a
different face on one host and not another produces documents that disagree with each other, and it is
the kind of bug nobody notices until a client does.

### 2.3 How content gets in

The model gives us `title` and `body_markdown`. We do **not** paste that string into the template.
Convert markdown to Typst with a **small, explicit converter** covering a closed subset:

| Supported | Rendered as |
|---|---|
| `#`, `##`, `###` headings | `= `, `== `, `=== ` |
| Paragraphs, bold, italic, inline code | `*...*`, `_..._`, `` `...` `` |
| Bullet and numbered lists | `- `, `+ ` |
| Fenced code blocks | `raw(block: true, lang: ...)` |
| Simple pipe tables | `#table(columns: n, ...)` |
| Everything else | Escaped and emitted as literal text |

The last row is the design. An unsupported construct becomes visible text, not a compile error and not
an injection. This converter is ~150 lines and it is the piece worth writing carefully.

```python
# app/render/pdf.py (sketch)
import subprocess, tempfile, pathlib, uuid
from .markdown_to_typst import convert          # the closed-subset converter above

TEMPLATES = pathlib.Path(__file__).parent.parent / "templates" / "typst"

def render_pdf(title: str, body_markdown: str, template: str = "report") -> pathlib.Path:
    tpl = TEMPLATES / f"{template}.typ"
    if not tpl.exists():
        raise ValueError(f"unknown template: {template}")   # never interpolate into a path

    body = convert(body_markdown)                            # escaped, closed subset
    doc  = f'#import "{template}.typ": report\n' \
           f'#show: report.with(title: {typst_str(title)})\n\n{body}\n'

    with tempfile.TemporaryDirectory() as tmp:
        src = pathlib.Path(tmp) / "main.typ"
        src.write_text(doc, encoding="utf-8")
        out = pathlib.Path(ARTIFACT_DIR) / f"{uuid.uuid4().hex}.pdf"
        subprocess.run(
            ["typst", "compile", "--root", tmp, str(src), str(out)],
            check=True, timeout=30, capture_output=True,
        )
    return out
```

`typst_str()` is a one-liner that quotes and escapes a Typst string literal. Use it for every value
that comes from the model, including the title. Verify the exact CLI flags against your Typst version
— `--root` and font-path flags have shifted between releases.

### 2.4 Sandboxing untrusted input

Typst has file-reading constructs (`read`, `include`, `image`). A model that has been fed a malicious
document could, in principle, produce body text that tries to use them. The converter already prevents
this by escaping everything outside its closed subset, but defend twice:

- **`--root` at a scratch directory.** Typst refuses paths outside the root. Never set the root to the
  repo or to `/`.
- **A fresh temp directory per render**, deleted after. No shared scratch space between requests.
- **A timeout on the subprocess** (30 s is a reasonable start; measure and adjust). A runaway
  compile must not pin a core on the hub host.
- **No shell.** Pass an argument list to `subprocess.run`, never `shell=True`, never an f-string
  command.
- **Run as the unprivileged container user**, writing only to the artefact volume — the rule from
  [`01-architecture.md`](./01-architecture.md) §5.
- **Return stderr to the model on failure, truncated.** Typst's errors are actionable, so a failed
  render can be retried usefully instead of just reported.

---

## 3. PPTX — python-pptx

### 3.1 Why editable output matters

A PDF of slides is not a deck. People need to change a number, re-order two slides, and present it.
python-pptx writes genuine Office Open XML, so the result opens in PowerPoint, Keynote and LibreOffice
and can be edited normally. That single property outranks everything else here.

| | python-pptx | Marp (markdown -> slides) | reveal.js -> PDF | LibreOffice headless conversion |
|---|---|---|---|---|
| Editable `.pptx` | **Yes** | No (HTML/PDF; `.pptx` export is lossy where it exists) | No | Yes, but from another source format |
| Model-friendly input | Structured outline (we impose it) | Markdown — very model-friendly | Markdown/HTML | Depends |
| Layout control | Manual, verbose API | Themes; limited | CSS | Inherits the source |
| Corporate template reuse | **Yes — open the real `.potx`** | No | No | Partial |
| Extra runtime | None (pure Python) | Node | Node + headless browser | A LibreOffice install |

**Marp is the honest alternative and the one to reach for if this proves painful.** It is far more
pleasant to generate — a model emits markdown reliably, and slide breaks are `---`. We are not choosing
it because its output is not an editable deck, and "I need to tweak slide 4 before the meeting" is the
first thing anyone will ask. Worth revisiting if the requirement turns out to be "a good-looking deck
to show", not "a deck to hand over".

### 3.2 Template deck, named layouts

Do not build slides from empty shapes. Author a `template.pptx` in PowerPoint with the master and the
layouts you want, check it into `app/templates/pptx/`, and have code only *fill placeholders*:

```
template.pptx
  layout "Title Slide"      -> placeholders: title, subtitle
  layout "Title and Content"-> placeholders: title, body
  layout "Section Header"   -> placeholders: title
  layout "Two Content"      -> placeholders: title, left, right
```

Address layouts **by name**, not by index — indices differ between templates and shift when someone
edits the master. Same for placeholders: resolve by `placeholder_format.idx` recorded once per layout,
and fail loudly if a template lacks one, rather than writing text into whatever shape happens to be
there.

### 3.3 The structured outline

This is the contract in [`14`](./14-mcp-tool-server.md) §2 made explicit:

```python
# app/render/pptx.py (sketch)
from pydantic import BaseModel, Field

class Slide(BaseModel):
    title: str = Field(max_length=90)
    bullets: list[str] = Field(default_factory=list, max_length=6)   # count cap
    notes: str = ""
    layout: str = "Title and Content"

class Deck(BaseModel):
    title: str = Field(max_length=90)
    subtitle: str = ""
    slides: list[Slide] = Field(max_length=30)
```

The caps are load-bearing. **python-pptx cannot measure text** — that requires a rendering engine —
so it cannot know a bullet has overflowed its box. PowerPoint's autofit is computed by PowerPoint at
open time, and setting the autofit property in the file does not resize anything until then. The only
reliable defence is refusing to put too much text in: cap bullets per slide, cap characters per bullet,
and truncate with an ellipsis rather than producing a slide with text running off the bottom.

```python
from pptx import Presentation
from pptx.util import Pt

def build_deck(deck: Deck, template: str = "default") -> pathlib.Path:
    prs = Presentation(str(TEMPLATES / f"{template}.pptx"))
    layouts = {l.name: l for l in prs.slide_layouts}

    title_slide = prs.slides.add_slide(layouts["Title Slide"])
    title_slide.placeholders[0].text = deck.title
    if deck.subtitle:
        title_slide.placeholders[1].text = deck.subtitle

    for s in deck.slides:
        layout = layouts.get(s.layout) or layouts["Title and Content"]
        slide = prs.slides.add_slide(layout)
        slide.shapes.title.text = s.title
        body = slide.placeholders[1].text_frame
        body.clear()
        for i, bullet in enumerate(s.bullets):
            para = body.paragraphs[0] if i == 0 else body.add_paragraph()
            para.text = truncate(bullet, 140)     # cap, do not hope
            para.level = 0
            para.font.size = Pt(18)
        if s.notes:
            slide.notes_slide.notes_text_frame.text = s.notes

    out = pathlib.Path(ARTIFACT_DIR) / f"{uuid.uuid4().hex}.pptx"
    prs.save(str(out))
    return out
```

Two practical notes. Adding a slide from a layout copies that layout's placeholders — which is why the
template does the design work and this function stays boring. And `add_slide` appends only; if
re-ordering is ever needed it means manipulating the XML element list directly, which is a good moment
to ask whether the user should just re-order it themselves in PowerPoint.

---

## 4. Image — ComfyUI on `.149`

### 4.1 The host

`.149` is 10.0.1.149: RTX 5080, 16 GB, Blackwell (CC 12.0), **native Ubuntu** (chosen precisely to
avoid the WSL2 `sm_120` memory-overhead problem — [`02`](./02-hardware-and-fleet.md) §1), on a
**different subnet** from the hub, with only **32 GB of system RAM**.

Three consequences that shape the build:

- **Verify `sm_120` kernels** in the PyTorch and ComfyUI builds you install (M0 spike 3). Blackwell
  support landed later than Ada's, and a build without those kernels fails at generation time, not at
  install time.
- **32 GB of RAM is the real constraint**, not the 16 GB of VRAM. ComfyUI caches models in system RAM
  between runs and will happily hold several. Keep one model resident, and prefer ComfyUI's low-RAM
  options over letting it swap. Confirm the current flag names against your version — they have been
  renamed more than once.
- **Cross-subnet** means every call from `.87` crosses a router (M0 spike 4). Fine for bursty,
  asynchronous work like this; not fine for anything latency-sensitive, which is why nothing else lives
  here.

### 4.2 The licence decision — schnell, not dev

**Use FLUX.1-schnell (Apache-2.0). Do not use FLUX.1-dev.**

FLUX.1 **[dev]** ships under a **non-commercial** licence. Commercial use requires a paid licence from
Black Forest Labs, and that arrangement involves usage tracking through their API. That fails **two**
of this project's hard requirements at once: N2 (zero recurring cost) and N1 (nothing about our work
leaves the network). It is not a licence we can buy our way out of without abandoning the premise of
the project.

FLUX.1 **[schnell]** is **Apache-2.0** and fine for commercial self-hosting. It is a few-step distilled
model: faster, somewhat lower fidelity than dev, and the correct default here.

| Model | Licence | Approx. VRAM | Role |
|---|---|---|---|
| **FLUX.1-schnell** FP8 | **Apache-2.0** | ~12 GB | Default when `.149` is free |
| **SD3.5-medium / SDXL-Turbo** | Stability community licence / OpenRAIL | ~6 GB | Fallback when `.149` is partly claimed |
| FLUX.1-dev | **Non-commercial — do not deploy** | ~12 GB+ | Excluded |

Check the Stability community licence's revenue threshold before relying on SD3.5 in production; SDXL
(OpenRAIL) is the unambiguous alternative if that threshold is ever a question. This is the same
warning as [`tech-stack.md`](./tech-stack.md) §8 — it is repeated here because this is the document
someone will read while downloading weights.

### 4.3 The ladder, and how the tool sees it

`.149` follows [`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) §3 exactly:

| Free VRAM on `.149` | Loaded | `generate_image` behaviour |
|---|---|---|
| >= 14 GB | FLUX.1-schnell FP8 (~12 GB) | Full quality |
| 7–14 GB | SD3.5-medium / SDXL-Turbo (~6 GB) | Works, visibly lower fidelity — say so in the result |
| < 7 GB | Nothing | `unavailable`: "host in use" |

The tool takes `quality`, never a model name ([`14`](./14-mcp-tool-server.md) §2). When the fallback
rung served the request, put that in the result — `{"model": "sd3.5-medium", "note": "reduced quality:
host partly in use"}` — so the model can tell the user why the picture is worse than last time. An
unexplained quality drop erodes trust faster than an explained one; the same principle as surfacing
the chat ladder rung.

### 4.4 API mode and workflow JSON

ComfyUI is a node-graph UI, but it has a proper HTTP API. Endpoint shapes have been stable for a while
yet remain unversioned — **verify against your build**:

```
POST /prompt          submit {"prompt": <workflow-api-json>, "client_id": ...} -> {"prompt_id": ...}
GET  /history/<id>    completed job's outputs (filenames, subfolder, type)
GET  /view?filename=..&subfolder=..&type=output    fetch the image bytes
GET  /queue           what is running and pending
WS   /ws?clientId=..  progress events
```

The workflow JSON must be the **API format**, not the editor format. In the ComfyUI UI enable dev mode
and use *Save (API Format)*; the editor's own save file will not submit.

The build pattern: author one workflow per rung in the UI, export API JSON, check it into
`app/templates/comfy/`, and **patch it by node id** at request time. Never generate the graph.

```python
# app/clients/comfy.py (sketch)
import json, copy, pathlib, httpx

WORKFLOWS = pathlib.Path(__file__).parent.parent / "templates" / "comfy"
SIZES = {"16:9": (1344, 768), "1:1": (1024, 1024), "4:3": (1152, 896)}

def build_workflow(rung: str, prompt: str, aspect: str, seed: int) -> dict:
    wf = json.loads((WORKFLOWS / f"{rung}.json").read_text())
    wf = copy.deepcopy(wf)
    w, h = SIZES.get(aspect, SIZES["16:9"])            # closed set - never model-supplied numbers
    wf["6"]["inputs"]["text"] = prompt                 # CLIPTextEncode - ids are YOUR graph's
    wf["5"]["inputs"]["width"], wf["5"]["inputs"]["height"] = w, h
    wf["3"]["inputs"]["seed"] = seed
    return wf
```

Record the node-id mapping in a comment beside the workflow file. Re-exporting the workflow after
editing it in the UI can renumber nodes, and a silently mis-patched graph produces a plausible image
with the wrong settings — the worst kind of bug because it does not look like one.

### 4.5 Queueing and timeouts

Image generation is the only tool here that takes tens of seconds and holds a GPU. So:

- **One job at a time from the platform.** ComfyUI has its own queue; add a small semaphore on our side
  so a chatty agent cannot enqueue twenty jobs and wedge the host for its owner.
- **Cap queue depth** (2–3 pending). Beyond that return "temporarily busy" immediately.
- **Check the fleet controller before submitting** — the pattern in [`14`](./14-mcp-tool-server.md)
  §4.4. This is the difference between a clear refusal and a three-minute hang.
- **Poll `/history`, or subscribe to the websocket** for progress. Polling every second or two is
  simpler and quite sufficient; use the websocket only if you want a progress figure in the UI.
- **An overall timeout**, after which we stop waiting and return `unavailable`. Set it from measured
  generation times once M7 is up — do not guess and then treat the guess as a fact.
- **Cancel on timeout** rather than orphaning the job, so the host is not still working for us after
  we have given up on it.

### 4.6 Getting the file back to `.87`

Fetch the bytes over `/view` and write them into the artefact store on `.87` (§5). One place for every
artefact, one retention policy, one URL shape — and images stay available when `.149` is claimed,
rebooted, or unreachable. That is worth the one-time transfer across the subnet boundary.

---

## 5. The artefact store

All three tools write to `/data/artifacts` on `.87` (a Docker volume, on the NVMe that already holds
container volumes — [`02`](./02-hardware-and-fleet.md) §5) and return:

```json
{"ok": true, "file_id": "9f2c...", "filename": "quarterly-report.pdf",
 "url": "https://ai.internal/artifacts/9f2c....pdf", "bytes": 184320}
```

- **Random file ids**, never model-supplied names, on disk. Keep the human-readable name as metadata
  and set it in the `Content-Disposition` header on download.
- **Serve through Caddy**, on the LAN, with the same auth as the rest of the platform.
- **Never return file bytes in the tool result.** A URL costs a few tokens; a base64 PNG costs the
  conversation.
- **Retention: delete after ~30 days**, by a scheduled job, and say so in the tool result if it
  matters. These are regenerable outputs, not records. Revisit if people start treating them as
  records — they will try.

---

## 6. Acceptance

| # | Test | Pass |
|---|---|---|
| 1 | `generate_pdf` with a two-page markdown body including a table and a code block | Opens in a PDF reader; headings, table and code render; fonts as configured |
| 2 | `generate_pdf` with adversarial body text (Typst syntax, `#read("/etc/passwd")`, unbalanced braces) | Renders as literal escaped text; no file access; no compile failure |
| 3 | `generate_pdf` with a nonsense template name | Clear `refused` result, no path traversal attempted |
| 4 | `generate_pptx` from a 10-slide outline | Opens in PowerPoint **and** LibreOffice; layouts match the template; notes present |
| 5 | `generate_pptx` with a 400-character bullet | Truncated cleanly; no text running off the slide |
| 6 | `generate_image` with `.149` free | Image returned; result reports FLUX.1-schnell |
| 7 | `generate_image` with `.149` at ~10 GB free | SD3.5-medium used; result says quality is reduced and why |
| 8 | `generate_image` with `.149` toggled *in use* | `unavailable` — "host in use" — promptly, no hang (M6 test 6) |
| 9 | Three image requests fired at once | Queue cap holds; no more than one job runs; the extra requests get "busy", not a hang |
| 10 | All three tools from all three clients | Same artefacts, same URLs (M6 test 5) |

Tests 2 and 3 are the ones that will actually be skipped, and they are the ones protecting the hub
host from its own renderer. Run them.

---

## Reflect

The through-line is that **we never let the model produce the artefact — only its contents.** That
decision costs flexibility: a user cannot ask for a custom slide layout, and the PDF converter supports
a closed subset of markdown rather than everything. It buys reliability we cannot otherwise get from a
model in this size class, plus a security boundary around three renderers running on the hub host.

Where we expect to feel the constraint: **the PPTX outline will feel too rigid** within a week of real
use. Somebody will want an image on a slide, or a two-column comparison, and the honest answer is to
add a layout to the template rather than to loosen the schema. Adding layouts scales; loosening the
schema does not.

Two residual risks worth stating plainly. **The artefact store grows without bound** unless the
retention job is actually written — deferred to `18-operations.md`, and easy to forget until a disk
fills on the host everything else depends on. And **the licence boundary is one download away from
being crossed**: FLUX.1-dev weights are trivially available and produce visibly better images, so at
some point someone will suggest "just for internal use". Record the reason in the host's `.env`
alongside the pinned model revision, so the next person to touch it sees the constraint before the
temptation.

**Next:** [`16-web-search-and-egress.md`](./16-web-search-and-egress.md).
