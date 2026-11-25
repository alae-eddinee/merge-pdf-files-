import re
import streamlit as st
from pypdf import PdfReader, PdfWriter
import io
import zipfile
from collections import defaultdict

import label_sheets

# ── Config ──────────────────────────────────────────────────────────────────
VALID_FOLDERS = [
    "COGEX PD 2",
    "COGEX PD 1",
    "COGEX INNER 1",
    "COGEX MASTER 1",
    "COGEX MASTER 2",
]

st.set_page_config(page_title="COGEX PDF Tool", page_icon="📄", layout="centered")

# ── Styles ───────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .main { background-color: #f8f9fb; }
    h1 { color: #1a1a2e; }
    .folder-tag {
        display: inline-block;
        background: #e8eaf6;
        color: #3949ab;
        border-radius: 6px;
        padding: 3px 10px;
        margin: 3px;
        font-size: 0.85rem;
        font-weight: 600;
    }
    .warn-box {
        background: #fff8e1;
        border-left: 4px solid #ffc107;
        padding: 10px 14px;
        border-radius: 4px;
        margin: 6px 0;
        font-size: 0.9rem;
    }
    .success-box {
        background: #2e7d32;
        color: #ffffff;
        border-left: 4px solid #1b5e20;
        padding: 10px 14px;
        border-radius: 4px;
        margin: 6px 0;
        font-size: 0.9rem;
    }
    .info-box {
        background: #1565c0;
        color: #ffffff;
        border-left: 4px solid #0d47a1;
        padding: 10px 14px;
        border-radius: 4px;
        margin: 6px 0;
        font-size: 0.9rem;
    }
    .info-box code {
        background: rgba(255,255,255,0.2);
        color: #ffffff;
        padding: 2px 4px;
        border-radius: 3px;
        font-family: monospace;
    }
</style>
""", unsafe_allow_html=True)

# ── Header ───────────────────────────────────────────────────────────────────
st.title("📄 COGEX PDF Tool")

tab_split, tab_merge, tab_labels = st.tabs(["✂️ Split Pages", "🔀 Merge PDFs", "🏷️ Label Sheets (R1/R2)"])


# ════════════════════════════════════════════════════════════════════════════
# HELPERS — 5-digit code detection
# ════════════════════════════════════════════════════════════════════════════

def find_five_digit_code(text: str, filename: str = "", page_num: int = 0) -> tuple[str | None, str]:
    """
    Return the COGEX product code (a standalone 5-digit number starting with 37)
    found in *text*, or fall back to any standalone 5-digit number.

    Priority:
      1. Standalone 5-digit number beginning with '37'  (e.g. 37081, 37172)
      2. Any other standalone 5-digit number (fallback)

    'Standalone' means not adjacent to other digits, so a 6-digit run like
    370828 or the order-number fragment 00122 (inside CO26AC00122) is skipped
    at the priority-1 level.

    Also returns a debug string explaining the detection result.
    """
    if not text or not text.strip():
        return None, "No text extracted (possibly scanned/image PDF)"

    # Priority 1 – standalone 5-digit code starting with 37 (COGEX product codes)
    cogex_matches = re.findall(r"(?<!\d)(37\d{3})(?!\d)", text)
    if cogex_matches:
        return cogex_matches[0], f"Found {len(cogex_matches)} COGEX 37xxx code(s), using first: {cogex_matches[0]}"

    # Priority 2 – any standalone 5-digit number (generic fallback)
    matches = re.findall(r"(?<!\d)(\d{5})(?!\d)", text)
    if matches:
        return matches[0], f"No 37xxx code found. Fell back to first 5-digit match: {matches[0]}"

    # Nothing useful found
    all_digits = re.findall(r"\d+", text)
    if all_digits:
        return None, f"No standalone 5-digit codes. Found digit groups: {all_digits[:5]}"
    return None, "No digits found in extracted text"


def extract_text_from_page(reader: PdfReader, page_index: int) -> str:
    """Extract text from a single page using pypdf."""
    try:
        page = reader.pages[page_index]
        text = page.extract_text() or ""
        # Try to get more text from various sources if main extraction is empty
        if not text.strip():
            # Some PDFs have text in annotations or other objects
            try:
                if "/Annots" in page:
                    annots = page["/Annots"]
                    if annots:
                        for annot in annots:
                            annot_obj = annot.get_object()
                            if annot_obj and "/Contents" in annot_obj:
                                text += " " + str(annot_obj["/Contents"])
            except Exception:
                pass
        return text
    except Exception as e:
        return ""


def split_pdf_bytes(pdf_bytes: bytes, filename: str) -> tuple[dict[str, bytes], list[str]]:
    """
    Split a PDF into individual pages, naming each by its 5-digit product code.

    Returns:
        pages  : dict mapping  "CODE.pdf" → page bytes
        errors : list of error strings (one per failed page)
    """
    pages: dict[str, bytes] = {}
    errors: list[str] = []

    reader = PdfReader(io.BytesIO(pdf_bytes))

    for page_index, page in enumerate(reader.pages):
        page_num = page_index + 1
        raw_text = extract_text_from_page(reader, page_index)
        code, debug_info = find_five_digit_code(raw_text, filename, page_num)

        if code is None:
            # Show first 200 chars of text to help diagnose
            text_preview = raw_text[:200].replace('\n', ' ')
            errors.append(
                f"Page {page_num} of '{filename}': {debug_info}. "
                f"Text preview: {text_preview!r}"
            )
            continue  # collect all errors before reporting

        writer = PdfWriter()
        writer.add_page(page)
        page_buf = io.BytesIO()
        writer.write(page_buf)
        pages[f"{code}.pdf"] = page_buf.getvalue()

    return pages, errors


# ════════════════════════════════════════════════════════════════════════════
# TAB 1 — SPLIT
# ════════════════════════════════════════════════════════════════════════════
with tab_split:
    st.markdown("### ✂️ Split PDF Pages into Named Files")
    st.markdown(
        "Upload a **ZIP file** containing your COGEX folders, or **one or more PDF files** directly. "
        "Each page will be saved as **`<5-digit-code>.pdf`** inside a folder named after its source file."
    )

    st.markdown("**Expected folder order:**")
    tags = "".join(f'<span class="folder-tag">{i+1}. {f}</span>' for i, f in enumerate(VALID_FOLDERS))
    st.markdown(tags, unsafe_allow_html=True)
    st.markdown("---")

    st.markdown("""
    <div class="info-box">
        💡 <b>ZIP Upload:</b> Create a ZIP file containing your 5 COGEX folders
        (<code>COGEX PD 2</code>, <code>COGEX PD 1</code>, <code>COGEX INNER 1</code>,
        <code>COGEX MASTER 1</code>, <code>COGEX MASTER 2</code>) with all PDF files inside.
        Each page will be extracted as <code>37081.pdf</code>, <code>37082.pdf</code>, etc.,
        placed in a folder named after the source PDF file (without extension).<br><br>
        💡 <b>PDF Upload:</b> Upload one or more PDF files directly. Each page will be extracted
        as its 5-digit product code, e.g. <code>37081.pdf</code>.
    </div>
    """, unsafe_allow_html=True)

    split_files = st.file_uploader(
        "Upload ZIP file or PDF file(s) for splitting",
        type=["zip", "pdf"],
        key="split_uploader",
        help="ZIP with COGEX folders, or one or more PDF files",
        accept_multiple_files=True,
    )

    split_file_map = {}  # { key: { folder, filename, data } }

    if split_files:
        for split_file in split_files:
            file_type = split_file.name.lower().rsplit('.', 1)[-1]

            if file_type == 'zip':
                zip_bytes = split_file.read()
                zip_buf = io.BytesIO(zip_bytes)

                with zipfile.ZipFile(zip_buf, 'r') as zf:
                    for file_path in zf.namelist():
                        if file_path.endswith('/') or not file_path.lower().endswith('.pdf'):
                            continue
                        path_parts = file_path.replace('\\', '/').split('/')
                        detected_folder = None
                        for part in path_parts[:-1]:
                            if part in VALID_FOLDERS:
                                detected_folder = part
                                break
                        if detected_folder:
                            filename = path_parts[-1]
                            file_key = f"{detected_folder}/{filename}"
                            split_file_map[file_key] = {
                                'filename': filename,
                                'folder': detected_folder,
                                'data': zf.read(file_path),
                            }

            elif file_type == 'pdf':
                pdf_bytes = split_file.read()
                filename = split_file.name
                split_file_map[filename] = {
                    'filename': filename,
                    'folder': None,
                    'data': pdf_bytes,
                }

        if split_file_map:
            st.success(f"✅ Found {len(split_file_map)} PDF(s)")
            st.markdown("#### 📂 Detected Files:")

            folders_found = set(v['folder'] for v in split_file_map.values() if v['folder'])
            if folders_found:
                for folder in VALID_FOLDERS:
                    folder_files = [k for k, v in split_file_map.items() if v['folder'] == folder]
                    if folder_files:
                        with st.expander(f"📁 {folder} ({len(folder_files)} files)"):
                            for fk in folder_files:
                                st.markdown(f"- `{split_file_map[fk]['filename']}`")
            else:
                for fk in split_file_map:
                    st.markdown(f"- `{split_file_map[fk]['filename']}`")
        else:
            st.warning("No PDF files found. Make sure folders are named correctly or upload a valid PDF file.")

    if split_file_map and st.button("✂️ Split PDFs into Pages", type="primary", width="stretch"):
        out_zip_buf = io.BytesIO()
        total_pages = 0
        all_errors: list[str] = []

        with zipfile.ZipFile(out_zip_buf, "w", zipfile.ZIP_DEFLATED) as out_zf:
            for file_key, file_info in split_file_map.items():
                folder = file_info['folder']
                filename = file_info['filename']
                # Folder inside the ZIP = source filename without extension
                base_name = filename[:-4] if filename.lower().endswith('.pdf') else filename

                pages, errors = split_pdf_bytes(file_info['data'], filename)
                all_errors.extend(errors)

                # Show warnings for pages without codes, but continue processing valid pages
                for err in errors:
                    st.markdown(
                        f'<div class="warn-box">⚠️ {err}</div>',
                        unsafe_allow_html=True,
                    )

                if not pages:
                    st.markdown(
                        f'<div class="warn-box">⚠️ <b>{filename}</b> — no pages could be processed '
                        f'(no 5-digit codes found in any page)</div>',
                        unsafe_allow_html=True,
                    )
                    continue

                for page_filename, page_data in pages.items():
                    # ZIP structure: FolderName/BaseName/37081.pdf
                    if folder:
                        out_path = f"{folder}/{base_name}/{page_filename}"
                    else:
                        out_path = f"{base_name}/{page_filename}"
                    out_zf.writestr(out_path, page_data)
                    total_pages += 1

                st.markdown(
                    f'<div class="success-box">✅ <b>{filename}</b> → '
                    f'{len(pages)} page(s) named by product code'
                    f'{" → folder <i>" + folder + "</i>" if folder else ""}</div>',
                    unsafe_allow_html=True,
                )

        if all_errors:
            st.error(
                f"⛔ {len(all_errors)} page(s) could not be processed because no 5-digit "
                f"product code was detected. See warnings above."
            )
        else:
            out_zip_buf.seek(0)
            st.markdown("---")
            st.success(f"✅ {total_pages} page file(s) created, each named by its 5-digit product code.")
            st.download_button(
                label="📦 Download Split Pages as ZIP",
                data=out_zip_buf.getvalue(),
                file_name="COGEX_split_pages.zip",
                mime="application/zip",
                width="stretch",
            )


# ════════════════════════════════════════════════════════════════════════════
# TAB 2 — MERGE
# ════════════════════════════════════════════════════════════════════════════
with tab_merge:
    st.markdown("### 🔀 Merge PDFs Across Folders")
    st.markdown(
        "Upload a ZIP file containing your COGEX folders. "
        "Files with the **same name** across folders will be merged in folder order."
    )

    st.markdown("**Merge order:**")
    tags2 = "".join(f'<span class="folder-tag">{i+1}. {f}</span>' for i, f in enumerate(VALID_FOLDERS))
    st.markdown(tags2, unsafe_allow_html=True)
    st.markdown("---")

    st.markdown("""
    <div class="info-box">
        💡 <b>ZIP Upload:</b> Create a ZIP file containing your 5 COGEX folders
        (<code>COGEX PD 2</code>, <code>COGEX PD 1</code>, <code>COGEX INNER 1</code>,
        <code>COGEX MASTER 1</code>, <code>COGEX MASTER 2</code>) with all PDF files inside.
        Files sharing the same filename across folders will be merged into one PDF.
    </div>
    """, unsafe_allow_html=True)

    merge_zip = st.file_uploader(
        "Upload ZIP file for merging",
        type=["zip"],
        key="merge_uploader",
        help="ZIP with folders named exactly as listed above",
    )

    merge_file_map = {}

    if merge_zip:
        zip_bytes = merge_zip.read()
        zip_buf = io.BytesIO(zip_bytes)

        with zipfile.ZipFile(zip_buf, 'r') as zf:
            for file_path in zf.namelist():
                if file_path.endswith('/') or not file_path.lower().endswith('.pdf'):
                    continue
                path_parts = file_path.replace('\\', '/').split('/')
                detected_folder = None
                for part in path_parts[:-1]:
                    if part in VALID_FOLDERS:
                        detected_folder = part
                        break
                if detected_folder:
                    filename = path_parts[-1]
                    file_key = f"{detected_folder}/{filename}"
                    merge_file_map[file_key] = {
                        'filename': filename,
                        'folder': detected_folder,
                        'data': zf.read(file_path),
                    }

        if merge_file_map:
            st.success(f"✅ Found {len(merge_file_map)} PDF(s) in ZIP file")
            st.markdown("#### 📂 Detected Files:")
            for folder in VALID_FOLDERS:
                folder_files = [k for k, v in merge_file_map.items() if v['folder'] == folder]
                if folder_files:
                    with st.expander(f"📁 {folder} ({len(folder_files)} files)"):
                        for fk in folder_files:
                            st.markdown(f"- `{merge_file_map[fk]['filename']}`")
        else:
            st.warning("No PDF files found in ZIP. Make sure folders are named correctly.")

    if merge_file_map and st.button("🔀 Merge PDFs", type="primary", width="stretch"):
        warnings = []
        grouped = defaultdict(dict)

        for file_key, file_info in merge_file_map.items():
            folder = file_info['folder']
            filename = file_info['filename']
            if folder not in VALID_FOLDERS:
                warnings.append(f"⚠️ **{filename}** assigned to unknown folder '{folder}' — ignored.")
                continue
            grouped[filename][folder] = file_info['data']

        for w in warnings:
            st.markdown(f'<div class="warn-box">{w}</div>', unsafe_allow_html=True)

        if not grouped:
            st.error("No valid files to process.")
            st.stop()

        merged_results = {}

        for pdf_name, folder_bytes in grouped.items():
            writer = PdfWriter()
            used_folders = []

            for folder in VALID_FOLDERS:
                if folder in folder_bytes:
                    try:
                        reader = PdfReader(io.BytesIO(folder_bytes[folder]))
                        for page in reader.pages:
                            writer.add_page(page)
                        used_folders.append(folder)
                    except Exception as e:
                        warnings.append(f"⚠️ Could not read **{pdf_name}** from '{folder}': {e}")

            if used_folders:
                buf = io.BytesIO()
                writer.write(buf)
                merged_results[pdf_name] = buf.getvalue()
                missing = [f for f in VALID_FOLDERS if f not in folder_bytes]
                msg = f"✅ <b>{pdf_name}</b> — merged from {len(used_folders)} folder(s)"
                if missing:
                    msg += f" | <i>Missing from: {', '.join(missing)}</i>"
                st.markdown(f'<div class="success-box">{msg}</div>', unsafe_allow_html=True)

        if not merged_results:
            st.error("No PDFs could be merged.")
            st.stop()

        st.markdown("---")
        st.success(f"✅ {len(merged_results)} PDF(s) merged successfully!")

        st.markdown("### ⬇️ Download Merged PDFs")
        for pdf_name, data in merged_results.items():
            st.download_button(
                label=f"📥 Download  {pdf_name}",
                data=data,
                file_name=pdf_name,
                mime="application/pdf",
                width="stretch",
            )

        st.markdown("#### Or download all as ZIP")
        zip_out = io.BytesIO()
        with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as zf:
            for pdf_name, data in merged_results.items():
                zf.writestr(pdf_name, data)
        zip_out.seek(0)

        st.download_button(
            label="📦 Download ALL as ZIP",
            data=zip_out.getvalue(),
            file_name="COGEX_merged.zip",
            mime="application/zip",
            width="stretch",
        )

# ════════════════════════════════════════════════════════════════════════════
# TAB 3 — LABEL SHEETS (one PDF per label type, several labels per page)
# ════════════════════════════════════════════════════════════════════════════
with tab_labels:
    st.markdown("### 🏷️ Label Sheets → one PDF per product")
    st.markdown(
        "For batches delivered as **one PDF per label type** with several products on each page "
        "(e.g. `R2 146 COGEX PDT 1.pdf`). Each label is cut out and, per product code, "
        "the labels of every type are merged into one PDF."
    )

    st.markdown("**Merge order:**")
    tags3 = "".join(
        f'<span class="folder-tag">{i+1}. {t}</span>' for i, t in enumerate(label_sheets.TYPE_ORDER)
    )
    st.markdown(tags3, unsafe_allow_html=True)
    st.markdown("---")

    st.markdown("""
    <div class="info-box">
        💡 <b>Upload</b> the <b>RAR</b> or <b>ZIP</b> you received (e.g. <code>PDF R2-146.rar</code>),
        or the PDF files directly. Several batches can be uploaded together.<br><br>
        File names must contain <code>COGEX</code> followed by the label type:
        <code>PDT 1</code>, <code>PDT 2</code>, <code>INNER …</code>, <code>MASTER 1</code>, <code>MASTER 2 …</code>.
        The text before <code>COGEX</code> (e.g. <code>R2 146</code>) is the batch name.<br><br>
        📦 <b>Result:</b> <code>R2 146/37156.pdf</code> (all label types of product 37156, in the order above),
        plus every single label under <code>R2 146/split/</code>.
    </div>
    """, unsafe_allow_html=True)

    label_uploads = st.file_uploader(
        "Upload RAR, ZIP or PDF files",
        type=["rar", "zip", "pdf"],
        key="labels_uploader",
        accept_multiple_files=True,
    )

    # Results are kept across reruns (download / preview clicks rerun the script).
    upload_sig = tuple((f.name, f.size) for f in label_uploads) if label_uploads else None
    if st.session_state.get("ls_sig") != upload_sig:
        st.session_state["ls_sig"] = upload_sig
        st.session_state.pop("ls_result", None)

    label_pdfs: dict[str, bytes] = {}
    if label_uploads:
        for up in label_uploads:
            try:
                label_pdfs.update(label_sheets.extract_pdfs(up.name, up.getvalue()))
            except Exception as e:
                st.markdown(f'<div class="warn-box">⚠️ <b>{up.name}</b>: {e}</div>', unsafe_allow_html=True)

        recognised = {n: label_sheets.classify_filename(n) for n in label_pdfs}
        ok = {n: c for n, c in recognised.items() if c}
        if ok:
            st.success(f"✅ Found {len(ok)} label PDF(s)")
            by_batch: dict[str, list[tuple[str, str]]] = defaultdict(list)
            for n, (batch, typ) in ok.items():
                by_batch[batch].append((typ, n.split("/")[-1]))
            for batch, items in by_batch.items():
                types = {t for t, _ in items}
                missing = [t for t in label_sheets.TYPE_ORDER if t not in types]
                title = f"📁 {batch} ({len(items)} files)" + (f" — missing: {', '.join(missing)}" if missing else "")
                with st.expander(title):
                    for typ, name in sorted(items, key=lambda x: label_sheets.TYPE_ORDER.index(x[0])):
                        st.markdown(f"- **{typ}** — `{name}`")
        ignored = [n for n, c in recognised.items() if not c]
        if ignored:
            st.markdown(
                '<div class="warn-box">⚠️ Ignored (name not recognised as a COGEX label file): '
                + ", ".join(f"<code>{n}</code>" for n in ignored) + "</div>",
                unsafe_allow_html=True,
            )
        if not label_pdfs:
            st.warning("No PDF files found in the upload.")

    if label_pdfs and any(label_sheets.classify_filename(n) for n in label_pdfs):
        if st.button("🏷️ Cut & Merge Labels", type="primary", width="stretch"):
            with st.spinner("Detecting labels and building PDFs…"):
                files, _skipped = label_sheets.process(label_pdfs)
                zip_bytes, coverage = label_sheets.build_outputs(files)
            st.session_state["ls_result"] = {"files": files, "zip": zip_bytes, "coverage": coverage}

    result = st.session_state.get("ls_result")
    if result:
        files = result["files"]
        coverage = result["coverage"]

        for f in sorted(files, key=lambda f: (f.batch, label_sheets.TYPE_ORDER.index(f.type))):
            by_pos = sum(1 for l in f.labels if l.code_source == "position")
            note = f" (codes matched by position: {by_pos})" if by_pos else ""
            if f.labels:
                st.markdown(
                    f'<div class="success-box">✅ <b>{f.name}</b> → {len(f.labels)} label(s){note}</div>',
                    unsafe_allow_html=True,
                )
            for w in f.warnings:
                st.markdown(f'<div class="warn-box">⚠️ <b>{f.name}</b>: {w}</div>', unsafe_allow_html=True)

        st.markdown("---")
        st.markdown("#### 📋 Products found")
        incomplete = 0
        for batch, codes in coverage.items():
            rows = []
            for code, present in codes.items():
                row = {"Code": code}
                for t in label_sheets.TYPE_ORDER:
                    row[t] = "✅" if t in present else "❌"
                incomplete += len(present) < len(label_sheets.TYPE_ORDER)
                rows.append(row)
            st.markdown(f"**{batch}** — {len(codes)} product(s)")
            st.dataframe(rows, hide_index=True, width="stretch")
        if incomplete:
            st.warning(f"⚠️ {incomplete} product(s) are missing at least one label type (❌ above).")

        st.markdown("#### 🔍 Preview")
        choices = {f"{b} — {c}": (b, c) for b, codes in coverage.items() for c in codes}
        if choices:
            pick = choices[st.selectbox("Product", list(choices), key="ls_preview")]
            previews = label_sheets.render_preview(files, pick[0], pick[1])
            cols = st.columns(len(previews) or 1)
            for col, (typ, png) in zip(cols, previews):
                col.image(png, caption=typ, width="stretch")

        st.markdown("---")
        total = sum(len(c) for c in coverage.values())
        st.success(f"✅ {total} product PDF(s) ready.")
        batches = list(coverage)
        st.download_button(
            label="📦 Download Labels as ZIP",
            data=result["zip"],
            file_name=f"COGEX_labels_{'_'.join(b.replace(' ', '-') for b in batches)}.zip",
            mime="application/zip",
            width="stretch",
        )
