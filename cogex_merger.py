import streamlit as st
from pypdf import PdfReader, PdfWriter
import io
import zipfile
from collections import defaultdict
import os

# ── Config ──────────────────────────────────────────────────────────────────
VALID_FOLDERS = [
    "COGEX PD 2",
    "COGEX PD 1",
    "COGEX INNER 1",
    "COGEX MASTER 1",
    "COGEX MASTER 2",
]

st.set_page_config(page_title="COGEX PDF Merger", page_icon="📄", layout="centered")

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
        background: #e8f5e9;
        border-left: 4px solid #43a047;
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
st.title("📄 COGEX PDF Merger")
st.markdown("Upload your COGEX folders as a ZIP file. Files with the same name across folders will be merged in the correct folder order.")

st.markdown("**Expected folder order:**")
tags = "".join(f'<span class="folder-tag">{i+1}. {f}</span>' for i, f in enumerate(VALID_FOLDERS))
st.markdown(tags, unsafe_allow_html=True)

st.markdown("---")

st.markdown("""
<div class="info-box">
    💡 <b>ZIP Upload:</b> Create a ZIP file containing your 5 COGEX folders
    (<code>COGEX PD 2</code>, <code>COGEX PD 1</code>, <code>COGEX INNER 1</code>,
    <code>COGEX MASTER 1</code>, <code>COGEX MASTER 2</code>) with all PDF files inside.
    The folder names in the ZIP must match exactly.
</div>
""", unsafe_allow_html=True)

uploaded_zip = st.file_uploader(
    "Upload ZIP file containing COGEX folders",
    type=["zip"],
    help="ZIP file with folders named exactly: COGEX PD 2, COGEX PD 1, COGEX INNER 1, COGEX MASTER 1, COGEX MASTER 2",
)

# Initialize file_folder_map
file_folder_map = {}

if uploaded_zip:
    zip_bytes = uploaded_zip.read()
    zip_buf = io.BytesIO(zip_bytes)

    with zipfile.ZipFile(zip_buf, 'r') as zf:
        all_files = zf.namelist()

        # Extract files and auto-assign to folders based on path
        for file_path in all_files:
            # Skip directories and non-PDF files
            if file_path.endswith('/'):
                continue
            if not file_path.lower().endswith('.pdf'):
                continue

            # Determine folder from path (e.g., "COGEX PD 2/report.pdf")
            path_parts = file_path.replace('\\', '/').split('/')

            # Check if any part of the path matches a valid folder name
            detected_folder = None
            for part in path_parts[:-1]:  # Exclude filename
                if part in VALID_FOLDERS:
                    detected_folder = part
                    break

            if detected_folder:
                filename = path_parts[-1]
                # Create a unique key for each file
                file_key = f"{detected_folder}/{filename}"
                file_folder_map[file_key] = {
                    'filename': filename,
                    'folder': detected_folder,
                    'data': zf.read(file_path)
                }

    if file_folder_map:
        st.success(f"✅ Found {len(file_folder_map)} PDF(s) in ZIP file")

        # Show detected files by folder
        st.markdown("#### 📂 Detected Files:")
        for folder in VALID_FOLDERS:
            folder_files = [k for k, v in file_folder_map.items() if v['folder'] == folder]
            if folder_files:
                with st.expander(f"📁 {folder} ({len(folder_files)} files)"):
                    for fk in folder_files:
                        st.markdown(f"- `{file_folder_map[fk]['filename']}`")
    else:
        st.warning("No PDF files found in ZIP. Make sure folders are named correctly.")

# ── Process button ─────────────────────────────────────────────────────────────
if file_folder_map and st.button("🔀 Merge PDFs", type="primary", use_container_width=True):
    warnings = []
    # Group files: { pdf_filename: { folder_name: file_bytes } }
    grouped = defaultdict(dict)

    for file_key, file_info in file_folder_map.items():
        folder = file_info['folder']
        filename = file_info['filename']

        if folder == "-- Select folder --":
            warnings.append(f"⚠️ **{filename}** has no folder assigned — skipped.")
            continue
        if folder not in VALID_FOLDERS:
            warnings.append(f"⚠️ **{filename}** assigned to unknown folder '{folder}' — ignored.")
            continue
        grouped[filename][folder] = file_info['data']

    # Show warnings
    for w in warnings:
        st.markdown(f'<div class="warn-box">{w}</div>', unsafe_allow_html=True)

    if not grouped:
        st.error("No valid files to process. Please assign folders and try again.")
        st.stop()

    # ── Merge each PDF group in folder order ───────────────────────────────────
    merged_results = {}  # filename → bytes

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

    # ── Download: individual ───────────────────────────────────────────────────
    st.markdown("### ⬇️ Download Merged PDFs")

    for pdf_name, data in merged_results.items():
        st.download_button(
            label=f"📥 Download  {pdf_name}",
            data=data,
            file_name=pdf_name,
            mime="application/pdf",
            use_container_width=True,
        )

    # ── Download: ZIP ──────────────────────────────────────────────────────────
    st.markdown("#### Or download all as ZIP")
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for pdf_name, data in merged_results.items():
            zf.writestr(pdf_name, data)
    zip_buf.seek(0)

    st.download_button(
        label="📦 Download ALL as ZIP",
        data=zip_buf.getvalue(),
        file_name="COGEX_merged.zip",
        mime="application/zip",
        use_container_width=True,
    )

elif not file_folder_map:
    st.markdown("---")
    st.info("👆 Upload your folders or files above to get started.")