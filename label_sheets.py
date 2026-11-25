"""
Label-sheet support for COGEX batch PDFs (e.g. "R1 145 COGEX PDT 1.pdf").

In this format each label type is one PDF holding a grid of labels — one label
per product, several labels per page.  This module:

  1. finds every label on every page (grid detection from text anchors),
  2. works out which product code (37xxx) each label belongs to,
  3. crops each label onto its own page,
  4. merges, per product, the labels of every type in the standard order.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import statistics
import tempfile
import zipfile
from dataclasses import dataclass, field

import numpy as np
import pymupdf

# Merge order, matching the old folder order (PD 2, PD 1, INNER, MASTER 1, MASTER 2).
TYPE_ORDER = ["PDT 2", "PDT 1", "INNER", "MASTER 1", "MASTER 2"]

_FILE_RE = re.compile(
    r"^(?P<batch>.*?)\s*COGEX\s+(?P<type>PDT?\s*[12]|INNER(?:\s*\d)?|MASTER\s*[12])\b",
    re.IGNORECASE,
)
_CODE_RE = re.compile(r"^37\d{3}$")                  # product code as its own text
_ORDER_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z]{2}\d{4,}$")  # order number, e.g. CO26AC01146
_EAN_RE = re.compile(r"^\d{12,13}$")                 # barcode digits (with/without leading 3)

MARGIN = 3.0       # pt of white space kept around content when a label has no grid neighbour
CLUSTER_TOL = 6.0  # pt: anchors closer than this share a row/column


# ════════════════════════════════════════════════════════════════════════════
# File classification
# ════════════════════════════════════════════════════════════════════════════

def classify_filename(filename: str) -> tuple[str, str] | None:
    """'R1 145 COGEX MASTER 2 N.pdf' -> ('R1 145', 'MASTER 2'); None if not a label file."""
    stem = os.path.splitext(os.path.basename(filename))[0].strip()
    m = _FILE_RE.match(stem)
    if not m:
        return None
    raw = re.sub(r"\s+", "", m.group("type").upper())
    if raw.startswith("PD"):
        typ = f"PDT {raw[-1]}"
    elif raw.startswith("INNER"):
        typ = "INNER"
    else:
        typ = f"MASTER {raw[-1]}"
    batch = m.group("batch").strip(" -_") or "Batch"
    return batch, typ


# ════════════════════════════════════════════════════════════════════════════
# Archive extraction (ZIP / RAR)
# ════════════════════════════════════════════════════════════════════════════

def _rar_tools() -> list[list[str]]:
    """Command prefixes able to extract a RAR into a directory (first found wins)."""
    tools = []
    for exe in ("unrar", r"C:\Program Files\WinRAR\UnRAR.exe"):
        path = shutil.which(exe) or (exe if os.path.isfile(exe) else None)
        if path:
            tools.append([path, "x", "-o+", "-idq", "{archive}", "{dest}" + os.sep])
    for exe in ("bsdtar", r"C:\Windows\System32\tar.exe"):
        path = shutil.which(exe) or (exe if os.path.isfile(exe) else None)
        if path:
            tools.append([path, "-xf", "{archive}", "-C", "{dest}"])
    for exe in ("7z", "7zz"):
        path = shutil.which(exe)
        if path:
            tools.append([path, "x", "-y", "-o{dest}", "{archive}"])
    return tools


def _extract_rar(data: bytes) -> dict[str, bytes]:
    import subprocess

    tools = _rar_tools()
    if not tools:
        raise RuntimeError("No RAR extractor available on this server. Upload a ZIP or the PDFs instead.")
    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, "upload.rar")
        dest = os.path.join(tmp, "out")
        os.makedirs(dest)
        with open(archive, "wb") as fh:
            fh.write(data)
        last_err = ""
        for tool in tools:
            cmd = [a.replace("{archive}", archive).replace("{dest}", dest) for a in tool]
            res = subprocess.run(cmd, capture_output=True, text=True)
            files = _read_pdfs_in(dest)
            if files:
                return files
            last_err = (res.stderr or res.stdout or "").strip()[:300]
        raise RuntimeError(f"Could not extract the RAR file. {last_err}")


def _read_pdfs_in(folder: str) -> dict[str, bytes]:
    out = {}
    for root, _dirs, names in os.walk(folder):
        for name in names:
            if name.lower().endswith(".pdf"):
                rel = os.path.relpath(os.path.join(root, name), folder).replace("\\", "/")
                with open(os.path.join(root, name), "rb") as fh:
                    out[rel] = fh.read()
    return out


def extract_pdfs(filename: str, data: bytes) -> dict[str, bytes]:
    """Return {path: pdf bytes} for an uploaded PDF, ZIP or RAR."""
    low = filename.lower()
    if low.endswith(".pdf"):
        return {filename: data}
    if low.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return {
                n: zf.read(n) for n in zf.namelist()
                if n.lower().endswith(".pdf") and not n.endswith("/") and "__MACOSX" not in n
            }
    if low.endswith(".rar"):
        return _extract_rar(data)
    return {}


# ════════════════════════════════════════════════════════════════════════════
# Label detection
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Label:
    page: int                      # 0-based page index in the source PDF
    rect: pymupdf.Rect
    code: str | None
    code_source: str               # "text", "barcode" or "position"


@dataclass
class LabelFile:
    name: str
    batch: str
    type: str
    data: bytes
    labels: list[Label] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _spans(page: pymupdf.Page) -> list[tuple[str, pymupdf.Rect]]:
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                text = span["text"].strip()
                if text:
                    out.append((text, pymupdf.Rect(span["bbox"])))
    return out


def _ink_box(page: pymupdf.Page, dpi: int = 150) -> pymupdf.Rect | None:
    """
    Bounding box of what is actually printed on the page (non-white pixels).
    Object bounding boxes are not reliable: label artwork images carry white
    margins that overlap the neighbouring label.
    """
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, : pix.width]
    ink = img < 200
    rows, cols = np.flatnonzero(ink.any(axis=1)), np.flatnonzero(ink.any(axis=0))
    if not len(rows):
        return None
    s = 72 / dpi
    return pymupdf.Rect(cols[0] * s, rows[0] * s, (cols[-1] + 1) * s, (rows[-1] + 1) * s)


def _cluster(values: list[float]) -> list[float]:
    """Group nearly-equal coordinates; return the sorted group centres."""
    groups: list[list[float]] = []
    for v in sorted(values):
        if groups and v - groups[-1][-1] <= CLUSTER_TOL:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


def _anchors(spans: list[tuple[str, pymupdf.Rect]]) -> list[pymupdf.Point]:
    """One reference point per label: the order number, else the code, else the barcode digits."""
    for kind in ("order", "code", "ean"):
        pts, seen = [], set()
        for text, r in spans:
            t = text.replace(" ", "")
            if kind == "order" and _ORDER_RE.match(t):
                pts.append((r.tl + r.br) / 2)
            elif kind == "code" and _CODE_RE.match(t) and t not in seen:
                seen.add(t)  # MASTER labels print the code twice
                pts.append((r.tl + r.br) / 2)
            elif kind == "ean" and _EAN_RE.match(t):
                pts.append((r.tl + r.br) / 2)
        if pts:
            return pts
    return []


def _pitch(centres_per_page: list[list[float]]) -> float | None:
    diffs = [b - a for cs in centres_per_page for a, b in zip(cs, cs[1:])]
    return statistics.median(diffs) if diffs else None


def _grid_edges(lo: float, hi: float, n_anchor: int, pitch: float | None) -> list[tuple[float, float]]:
    """
    Split the printed extent [lo, hi] along one axis into label cells.

    Labels are identical and repeat every *pitch*, so the grid is tiled from the
    first printed point.  If the labels have white space between them, that gap
    is split evenly so each cut falls in the middle of it.  If they touch (PDT 2)
    the cuts land exactly on the joins.
    """
    if pitch is None:  # one row/column in the whole file: use the printed extent
        return [(lo - MARGIN, hi + MARGIN)]
    n = max(n_anchor, round((hi - lo) / pitch))
    gap = max(0.0, n * pitch - (hi - lo))
    start = lo - gap / 2
    return [(start + k * pitch, start + (k + 1) * pitch) for k in range(n)]


def _code_in(spans, rect: pymupdf.Rect) -> tuple[str | None, str]:
    inside = [(t.replace(" ", ""), r) for t, r in spans if rect.contains(r.tl + (r.br - r.tl) / 2)]
    for t, _r in inside:
        if _CODE_RE.match(t):
            return t, "text"
    for t, _r in inside:
        if _EAN_RE.match(t):
            # GS1 barcode 3 235463 CCCCC K — the product code follows the company prefix
            body = t[1:] if len(t) == 13 else t
            item = body[6:11]
            if _CODE_RE.match(item):
                return item, "barcode"
    return None, "position"


def detect_labels(lf: LabelFile) -> None:
    doc = pymupdf.open(stream=lf.data, filetype="pdf")
    pages = []
    for pno, page in enumerate(doc):
        spans = _spans(page)
        anchors = _anchors(spans)
        pages.append((pno, page, spans, anchors,
                      _cluster([p.x for p in anchors]), _cluster([p.y for p in anchors])))

    px = _pitch([p[4] for p in pages])
    py = _pitch([p[5] for p in pages])

    for pno, page, spans, anchors, cols, rows in pages:
        if not anchors:
            lf.warnings.append(f"Page {pno + 1}: no labels found (no order number, code or barcode text).")
            continue
        ink = _ink_box(page)
        if ink is None:
            lf.warnings.append(f"Page {pno + 1}: page is blank.")
            continue
        col_edges = _grid_edges(ink.x0, ink.x1, len(cols), px)
        row_edges = _grid_edges(ink.y0, ink.y1, len(rows), py)

        for y0, y1 in row_edges:
            for x0, x1 in col_edges:
                cell = pymupdf.Rect(x0, y0, x1, y1) & page.rect
                if not any(cell.contains(a) for a in anchors):
                    continue  # empty slot (e.g. last page is not full)
                code, src = _code_in(spans, cell)
                lf.labels.append(Label(pno, cell, code, src))
    doc.close()


def fill_codes_by_position(files: list[LabelFile]) -> None:
    """
    Labels whose code is not readable (e.g. PDT 1, where the code is drawn as an
    image) take the code of the label at the same position in a sibling file of
    the same batch.  Every file lists the products in the same order.
    """
    by_batch: dict[str, list[LabelFile]] = {}
    for f in files:
        by_batch.setdefault(f.batch, []).append(f)

    for batch_files in by_batch.values():
        refs = [f for f in batch_files if f.labels and all(l.code for l in f.labels)]
        for f in batch_files:
            if all(l.code for l in f.labels):
                continue
            ref = next((r for r in refs if len(r.labels) == len(f.labels)), None)
            if ref is None:
                f.warnings.append(
                    f"{sum(1 for l in f.labels if not l.code)} label(s) have no readable code and no "
                    f"other file in batch '{f.batch}' has the same number of labels to match them by position."
                )
                continue
            # where both have a readable code they must agree, otherwise positions are not comparable
            mismatch = [i for i, (a, b) in enumerate(zip(f.labels, ref.labels)) if a.code and a.code != b.code]
            if mismatch:
                f.warnings.append(
                    f"Label order differs from '{ref.name}' (label {mismatch[0] + 1}); "
                    "codes could not be matched by position."
                )
                continue
            for lab, r in zip(f.labels, ref.labels):
                if not lab.code:
                    lab.code, lab.code_source = r.code, "position"


# ════════════════════════════════════════════════════════════════════════════
# Output
# ════════════════════════════════════════════════════════════════════════════

def label_pdf(f: LabelFile, lab: Label) -> bytes:
    """
    One-page PDF holding just this label.  Everything outside the label is
    really removed (redacted), not only hidden, so the file stays small and
    does not carry the other products' labels.
    """
    doc = pymupdf.open(stream=f.data, filetype="pdf")
    doc.select([lab.page])
    page = doc[0]
    full, r = page.rect, lab.rect
    for out in (
        pymupdf.Rect(full.x0, full.y0, full.x1, r.y0),   # above
        pymupdf.Rect(full.x0, r.y1, full.x1, full.y1),   # below
        pymupdf.Rect(full.x0, r.y0, r.x0, r.y1),         # left
        pymupdf.Rect(r.x1, r.y0, full.x1, r.y1),         # right
    ):
        if out.width > 0.1 and out.height > 0.1:
            page.add_redact_annot(out, fill=False)
    page.apply_redactions(
        # PIXELS, not REMOVE: on PDT 2 sheets neighbouring labels' artwork
        # images overlap by a few points, so only blank what lies outside.
        images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
        graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
    )
    # Make the label the physical page size.  set_mediabox expects PDF
    # coordinates (origin bottom-left), while label rects are top-left based.
    u = r * page.derotation_matrix  # undo any /Rotate
    mb = page.mediabox
    page.set_mediabox(pymupdf.Rect(mb.x0 + u.x0, mb.y1 - u.y1, mb.x0 + u.x1, mb.y1 - u.y0))
    try:
        doc.subset_fonts()  # source PDFs embed full fonts (~1.5 MB per label otherwise)
    except Exception:
        pass
    data = doc.tobytes(garbage=4, deflate=True, clean=True)
    doc.close()
    return data


def build_outputs(files: list[LabelFile]) -> tuple[bytes, dict[str, dict[str, list[str]]]]:
    """
    Build the output ZIP:
        <batch>/<code>.pdf                    labels of every type merged, in TYPE_ORDER
        <batch>/split/<file name>/<code>.pdf  each label on its own

    Also returns a coverage table {batch: {code: [types present, in order]}}.
    """
    cache: dict[tuple[int, int], bytes] = {}

    def single(f: LabelFile, i: int) -> pymupdf.Document:
        if (id(f), i) not in cache:
            cache[(id(f), i)] = label_pdf(f, f.labels[i])
        return pymupdf.open(stream=cache[(id(f), i)], filetype="pdf")

    def assemble(parts: list[tuple[LabelFile, int]]) -> bytes:
        out = pymupdf.open()
        for f, i in parts:
            one = single(f, i)
            out.insert_pdf(one)
            one.close()
        data = out.tobytes(garbage=4, deflate=True)
        out.close()
        return data

    coverage: dict[str, dict[str, list[str]]] = {}
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # split files
        for f in files:
            stem = os.path.splitext(os.path.basename(f.name))[0]
            per_code: dict[str, list[int]] = {}
            for i, lab in enumerate(f.labels):
                if lab.code:
                    per_code.setdefault(lab.code, []).append(i)
            for code, idxs in per_code.items():
                zf.writestr(f"{f.batch}/split/{stem}/{code}.pdf", assemble([(f, i) for i in idxs]))

        # merged per product
        batches: dict[str, list[LabelFile]] = {}
        for f in files:
            batches.setdefault(f.batch, []).append(f)
        for batch, bfiles in batches.items():
            bfiles.sort(key=lambda f: TYPE_ORDER.index(f.type))
            codes: list[str] = []
            for f in bfiles:
                for lab in f.labels:
                    if lab.code and lab.code not in codes:
                        codes.append(lab.code)
            coverage[batch] = {}
            for code in codes:
                parts, present = [], []
                for f in bfiles:
                    idxs = [i for i, l in enumerate(f.labels) if l.code == code]
                    parts += [(f, i) for i in idxs]
                    if idxs:
                        present.append(f.type)
                coverage[batch][code] = present
                zf.writestr(f"{batch}/{code}.pdf", assemble(parts))

    return buf.getvalue(), coverage


def render_preview(files: list[LabelFile], batch: str, code: str, dpi: int = 70) -> list[tuple[str, bytes]]:
    """PNG previews of each label of *code* in *batch*, in merge order."""
    out = []
    for f in sorted((f for f in files if f.batch == batch), key=lambda f: TYPE_ORDER.index(f.type)):
        doc = pymupdf.open(stream=f.data, filetype="pdf")
        for lab in f.labels:
            if lab.code == code:
                pix = doc[lab.page].get_pixmap(dpi=dpi, clip=lab.rect)
                out.append((f.type, pix.tobytes("png")))
        doc.close()
    return out


def process(pdfs: dict[str, bytes]) -> tuple[list[LabelFile], list[str]]:
    """Classify and analyse uploaded PDFs. Returns (label files, names that were skipped)."""
    files, skipped = [], []
    for name, data in pdfs.items():
        cls = classify_filename(name)
        if cls is None:
            skipped.append(name)
            continue
        lf = LabelFile(name=os.path.basename(name), batch=cls[0], type=cls[1], data=data)
        try:
            detect_labels(lf)
        except Exception as e:  # damaged PDF etc.
            lf.warnings.append(f"Could not read this PDF: {e}")
        files.append(lf)
    fill_codes_by_position(files)
    return files, skipped
