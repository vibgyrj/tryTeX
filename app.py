import os
import re
import sys
import glob
import time
import shutil
import json
import subprocess
from collections import Counter
from flask import Flask, render_template_string, request, jsonify, send_from_directory, Response, abort
import webview

app = Flask(__name__)

# ==========================================
# STREAMING VALIDATOR ENGINE WITH LIVE STAGES
# ==========================================
class TexValidatorStreamer:
    def __init__(self, file_path):
        self.file_path = os.path.abspath(file_path)
        self.dir_name = os.path.dirname(self.file_path)
        self.raw_base = os.path.splitext(os.path.basename(self.file_path))[0]
        self.errors = []
        self.missing_items = []
        self.diff_report = None
        self.log_path = os.path.join(self.dir_name, f"{self.raw_base}.log")
        self.pages_diff_count = 0
        self.elapsed_time = 0.0

    def run_pipeline(self):
        start_time = time.time()

        if not os.path.exists(self.file_path):
            self.errors.append({"domain": "System", "message": f"Source file '{self.file_path}' not found."})
            self.elapsed_time = time.time() - start_time
            yield f"data: {json.dumps({'status': 'error', 'message': 'Source file not found.'})}\n\n"
            return
        
        existing_pdf = os.path.join(self.dir_name, f"{self.raw_base}.pdf")
        backup_pdf = os.path.join(self.dir_name, f"{self.raw_base}_MS.pdf")

        if os.path.exists(existing_pdf):
            try:
                if os.path.exists(backup_pdf): os.remove(backup_pdf)
                os.rename(existing_pdf, backup_pdf)
            except Exception: pass
        elif not os.path.exists(backup_pdf):
            pdfs = [p for p in glob.glob(os.path.join(self.dir_name, "*.pdf")) if not p.endswith("_MS.pdf") and not os.path.basename(p).startswith("diff_report_")]
            if len(pdfs) == 1:
                try: os.rename(pdfs[0], backup_pdf)
                except Exception: pass

        pdflatex_exe = r"\\integrafs3\TeXLiveCentral\texlive\2026\bin\windows\xelatex.exe"
        bibtex_exe = r"\\integrafs3\TeXLiveCentral\texlive\2026\bin\windows\bibtex.exe"
        biber_exe = r"\\integrafs3\TeXLiveCentral\texlive\2026\bin\windows\biber.exe"

        if not os.path.exists(pdflatex_exe): pdflatex_exe = shutil.which("xelatex") or pdflatex_exe
        if not os.path.exists(bibtex_exe): bibtex_exe = shutil.which("bibtex") or bibtex_exe
        if not os.path.exists(biber_exe): biber_exe = shutil.which("biber") or biber_exe

        has_toc = False
        try:
            with open(self.file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                if any(cmd in content for cmd in [r"\tableofcontents", r"\listoffigures", r"\listoftables"]):
                    has_toc = True
        except Exception: pass

        try:
            # Stage 1: Initial xelatex compilation (draftmode)
            yield f"data: {json.dumps({'stage_index': 0, 'step': '1. XeLaTeX Compiling (Draft Mode)', 'status': 'Running...', 'time': '...'})}\n\n"
            t0 = time.time()
            subprocess.run([pdflatex_exe, "-interaction=nonstopmode", "-draftmode", self.file_path],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace", cwd=self.dir_name)
            dur1 = f"{time.time() - t0:.2f}s"
            yield f"data: {json.dumps({'stage_index': 0, 'step': '1. XeLaTeX Compiling (Draft Mode)', 'status': 'Completed', 'time': dur1})}\n\n"

            # Stage 2: Bibliography Processing
            bcf_path = os.path.join(self.dir_name, f"{self.raw_base}.bcf")
            use_biber = os.path.exists(bcf_path) and os.path.exists(biber_exe)
            bib_tool_name = "biber" if use_biber else "bibtex"
            bib_tool = biber_exe if use_biber else bibtex_exe

            yield f"data: {json.dumps({'stage_index': 1, 'step': f'2. Bibliography Processing ({bib_tool_name})', 'status': 'Running...', 'time': '...'})}\n\n"
            t0 = time.time()
            bib_res = subprocess.run([bib_tool, self.raw_base],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace", cwd=self.dir_name)
            bib_status = "Processed (Present)" if bib_res.returncode == 0 or os.path.exists(os.path.join(self.dir_name, f"{self.raw_base}.bbl")) else "Skipped / Not Required"
            dur2 = f"{time.time() - t0:.2f}s"
            yield f"data: {json.dumps({'stage_index': 1, 'step': f'2. Bibliography Processing ({bib_tool_name})', 'status': bib_status, 'time': dur2})}\n\n"

            # Stage 3 & 4: Full Compilation passes
            total_full_passes = 3 if has_toc else 1
            for pass_idx in range(total_full_passes):
                pass_label = f"3. Full Compilation Pass {pass_idx + 1}" if pass_idx == 0 else f"4. Subsequent Compilation Pass {pass_idx + 1}"
                s_idx = 2 + pass_idx
                yield f"data: {json.dumps({'stage_index': s_idx, 'step': pass_label, 'status': 'Running...', 'time': '...'})}\n\n"
                t0 = time.time()
                subprocess.run([pdflatex_exe, "-interaction=nonstopmode", self.file_path],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace", cwd=self.dir_name)
                dur_pass = f"{time.time() - t0:.2f}s"
                yield f"data: {json.dumps({'stage_index': s_idx, 'step': pass_label, 'status': 'Completed', 'time': dur_pass})}\n\n"

            blg_path = os.path.join(self.dir_name, f"{self.raw_base}.blg")
            if os.path.exists(blg_path): self._parse_bib_log_file(blg_path)
        except (FileNotFoundError, TypeError):
            self.errors.append({"domain": "System Engine", "message": "Critical Error: 'xelatex' or 'bibtex' tools are missing."})
            yield f"data: {json.dumps({'status': 'error', 'message': 'TeX tools missing.'})}\n\n"
            self.elapsed_time = time.time() - start_time
            return

        if os.path.exists(self.log_path): self._parse_log_file(self.log_path)
        self._check_missing_materials()

        new_pdf = os.path.join(self.dir_name, f"{self.raw_base}.pdf")
        ms_pdf = os.path.join(self.dir_name, f"{self.raw_base}_MS.pdf")

        if os.path.exists(new_pdf) and os.path.exists(ms_pdf):
            self.diff_report = self._compare_pdfs(new_pdf, ms_pdf)

        self.elapsed_time = time.time() - start_time
        report_url = self.generate_html_report()
        
        # Final finish signal containing report redirect URL
        yield f"data: {json.dumps({'status': 'finished', 'report_url': f'/get_report_file?path={report_url}'})}\n\n"

    def _parse_log_file(self, log_path):
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            log_content = f.read()
        error_blocks = re.findall(r'^!(.*?)(?=^!|^Text broken|^\s*$)', log_content, re.MULTILINE | re.DOTALL)
        for block in error_blocks:
            lines = block.strip().split('\n')
            if lines: self.errors.append({"domain": "TeX Engine", "message": lines[0].strip()})

    def _parse_bib_log_file(self, blg_path):
        with open(blg_path, 'r', encoding='utf-8', errors='ignore') as f:
            blg_content = f.read()
        for warn in re.findall(r'Warning--(.*)', blg_content):
            self.errors.append({"domain": "Bibliography", "message": f"BibTeX Flag: {warn.strip()}"})

    def _check_missing_materials(self):
        with open(self.file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        for img in re.findall(r'\\includegraphics(?:\[.*?\])?\{(.*?)\}', content):
            found = any(os.path.exists(os.path.join(self.dir_name, img + ext if not img.endswith(ext) else img)) for ext in ['', '.png', '.jpg', '.jpeg', '.pdf'])
            if not found: self.missing_items.append(f"Graphic asset/Image file not found: <strong>{img}</strong>")

    def _compare_pdfs(self, new_pdf, ms_pdf):
        try:
            import fitz
            dn = fitz.open(new_pdf)
            dm = fitz.open(ms_pdf)
            diff_doc = fitz.open()
            pages_diff = 0
            max_pages = max(dn.page_count, dm.page_count)
            for i in range(max_pages):
                if i < dn.page_count and i < dm.page_count:
                    pn = dn[i].get_pixmap(dpi=150)
                    pm = dm[i].get_pixmap(dpi=150)
                    if pn.tobytes() != pm.tobytes():
                        pages_diff += 1
                        header_h = 40
                        w = pm.width + pn.width
                        h = max(pm.height, pm.height) + header_h
                        pg = diff_doc.new_page(width=w, height=h)
                        pg.draw_rect(fitz.Rect(0, 0, w, header_h), color=None, fill=(0.95, 0.95, 0.95))
                        pg.insert_text(fitz.Point(20, 26), f"Manuscript (MS) - Page {i + 1}", fontsize=13, color=(0.8, 0.1, 0.1))
                        pg.insert_text(fitz.Point(pm.width + 20, 26), f"Newly Generated - Page {i + 1}", fontsize=13, color=(0.1, 0.2, 0.8))
                        pg.insert_image(fitz.Rect(0, header_h, pm.width, pm.height + header_h), pixmap=pm)
                        pg.insert_image(fitz.Rect(pm.width, header_h, pm.width + pn.width, pn.height + header_h), pixmap=pn)
            self.pages_diff_count = pages_diff
            report_path = os.path.join(self.dir_name, f"diff_report_{self.raw_base}.pdf")
            diff_doc.save(report_path, deflate=True, garbage=3)
            dn.close(); dm.close(); diff_doc.close()
            return report_path
        except Exception as e:
            return None

    def generate_html_report(self):
        error_counts = Counter((err['domain'], err['message']) for err in self.errors)
        total_errors = sum(error_counts.values())
        total_missing = len(self.missing_items)

        mins = int(self.elapsed_time // 60)
        secs = int(self.elapsed_time % 60)
        formatted_exec_time = f"{mins}mins {secs}sec"

        cards_html = f'''
        <div class="card"><div class="num">{total_errors}</div><div class="lbl">Compilation Errors</div></div>
        <div class="card"><div class="num">{total_missing}</div><div class="lbl">Missing Materials</div></div>
        <div class="card"><div class="num" style="color:#2563eb; font-size:22px;">{formatted_exec_time}</div><div class="lbl">Execution Time</div></div>
        '''

        files_html = ""
        if self.missing_items:
            missing_rows = "".join(f"<tr><td>{item}</td></tr>" for item in self.missing_items)
            files_html = f'<h2 style="margin-top:28px;">Missing Structural Materials</h2><table class="files"><thead><tr><th>Missing Asset / Dependency</th></tr></thead><tbody>{missing_rows}</tbody></table>'

        groups_html = ""
        if error_counts:
            for (domain, message), count in error_counts.items():
                groups_html += f'''<details class="group"><summary><span class="badge">{count}</span><span class="msg">[{domain}] {message}</span></summary><div class="body"><pre class="extra">Domain: {domain}\nMessage: {message}</pre></div></details>'''
        else:
            groups_html = '<div class="ok">No text compilation or bibliography schema errors detected!</div>'

        safe_diff = self.diff_report.replace('\\', '/') if self.diff_report else ''
        safe_log = self.log_path.replace('\\', '/') if self.log_path else ''

        diff_btn_html = f'''<button onclick="showFrame('/view_file?path={safe_diff}')" class="action-btn" style="background:#2563eb;">Open Diff PDF Report</button>''' if self.diff_report and os.path.exists(self.diff_report) else ''
        log_btn_html = f'''<button onclick="showFrame('/view_file?path={safe_log}')" class="action-btn" style="background:#4b5563;">Open Log File</button>''' if os.path.exists(self.log_path) else ''

        TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><title>LaTeX Error Report</title>
<style>
body { margin: 0; background: #f4f6f8; color: #1f2937; font-family: "Segoe UI", Arial, sans-serif; font-size: 14px; }
.wrap { max-width: 1100px; margin: 30px auto; padding: 0 16px; }
h1 { margin: 0 0 4px; font-size: 26px; }
.meta { color: #6b7280; margin-bottom: 20px; }
.cards { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
.card { flex: 1; min-width: 150px; background: #fff; border-radius: 12px; padding: 16px; box-shadow: 0 2px 10px rgba(0,0,0,.06); }
.num { font-size: 28px; font-weight: 700; color: #b91c1c; }
.lbl { color: #6b7280; margin-top: 2px; }
.actions { display: flex; gap: 15px; flex-wrap: wrap; margin-bottom: 20px; }
.action-btn { padding: 10px 18px; color: white; border: none; border-radius: 8px; font-weight: 600; font-size: 13px; cursor: pointer; display: inline-block; }
.action-btn:hover { opacity: 0.9; }
.viewer-container { margin-bottom: 30px; background: white; padding: 12px; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,.06); display: none; }
.viewer-header { display: flex; justify-content: flex-end; margin-bottom: 8px; }
.new-tab-icon { color: #2563eb; text-decoration: none; font-weight: 600; font-size: 13px; display: flex; align-items: center; gap: 4px; }
.new-tab-icon:hover { text-decoration: underline; }
iframe { width: 100%; height: 550px; border: 1px solid #e5e7eb; border-radius: 8px; }
table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 10px rgba(0,0,0,.04); }
th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid #eef0f3; }
details.group { background: #fff; border-radius: 10px; margin-bottom: 8px; box-shadow: 0 2px 10px rgba(0,0,0,.05); overflow: hidden; }
details > summary { cursor: pointer; padding: 11px 14px; display: flex; align-items: center; gap: 10px; font-weight: 600; }
.badge { min-width: 42px; text-align: center; padding: 2px 8px; border-radius: 999px; background: #fee2e2; color: #b91c1c; font-size: 12px; }
.ok { background: #ecfdf5; color: #047857; padding: 18px; border-radius: 10px; font-weight: 600; margin-top: 20px; }
pre.extra { margin: 0; padding: 10px 14px; background: #f9fafb; color: #4b5563; font-size: 12.5px; white-space: pre-wrap; }
</style>
</head>
<body>
<div class="wrap">
<h1>LaTeX Error Report</h1>
<div class="meta"><b>@@TITLE@@</b></div>
<div class="cards">@@CARDS@@</div>
<div class="actions">@@DIFF_BTN@@ @@LOG_BTN@@</div>
<div id="viewerBox" class="viewer-container">
    <div class="viewer-header">
        <a id="newTabLink" href="#" target="_blank" class="new-tab-icon">Open in new tab ↗</a>
    </div>
    <iframe id="fileFrame"></iframe>
</div>
@@FILES@@
@@GROUPS@@
</div>
<script>
function showFrame(url) {
    const box = document.getElementById('viewerBox');
    const frame = document.getElementById('fileFrame');
    const newTabLink = document.getElementById('newTabLink');
    frame.src = url;
    newTabLink.href = url;
    box.style.display = 'block';
    box.scrollIntoView({ behavior: 'smooth' });
}
</script>
</body>
</html>
"""
        report_html = (TEMPLATE
            .replace("@@TITLE@@", f"{self.raw_base}.tex")
            .replace("@@CARDS@@", cards_html)
            .replace("@@DIFF_BTN@@", diff_btn_html)
            .replace("@@LOG_BTN@@", log_btn_html)
            .replace("@@FILES@@", files_html)
            .replace("@@GROUPS@@", groups_html)
        )
        report_name = os.path.join(self.dir_name, f"{self.raw_base}_report.html")
        with open(report_name, "w", encoding="utf-8") as f: f.write(report_html)
        return report_name


# ==========================================
# FLASK ROUTES
# ==========================================

@app.route('/', methods=['GET'])
def index():
    return render_template_string(r'''
    <!DOCTYPE html>
    <html lang="en">
    <head><title>Step 1: LaTeX Project Validator</title>
    <style>
        body { font-family: "Segoe UI", Arial, sans-serif; background: #f4f6f8; margin: 0; padding: 40px; color: #1f2937; }
        .container { max-width: 650px; margin: auto; background: white; padding: 35px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,.08); }
        .nav-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .next-step-btn { background: #059669; color: white; padding: 8px 16px; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 13px; }
        .next-step-btn:hover { background: #047857; }
        h2 { margin-top: 0; }
        input[type="text"] { width: 100%; padding: 12px; border: 1px solid #d1d5db; border-radius: 8px; font-size: 14px; box-sizing: border-box; }
        #tex-list { margin-top: 20px; list-style: none; padding: 0; }
        #tex-list li { padding: 10px; background: #f9fafb; margin-bottom: 6px; border-radius: 6px; border: 1px solid #eef0f3; display: flex; align-items: center; gap: 10px; }
        button { margin-top: 20px; padding: 10px 22px; background: #2563eb; color: white; border: none; border-radius: 8px; cursor: pointer; font-weight: 600; display: none; }
        #loading-container { display: none; text-align: center; padding: 20px 0; }
        table.steps-table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; margin-top: 15px; box-shadow: 0 2px 8px rgba(0,0,0,.04); text-align: left; }
        table.steps-table th, table.steps-table td { padding: 8px 12px; border-bottom: 1px solid #eef0f3; font-size: 13px; }
    </style>
    </head>
    <body>
        <div class="container">
            <div class="nav-header">
                <h2>Step 1: LaTeX Project Validator</h2>
                <a href="/step2" class="next-step-btn">Go to Step 2 &rarr;</a>
            </div>
            <p>Paste your project folder path below:</p>
            <input type="text" id="folder_path" placeholder="e.g., C:\itools\WMS\WorkingFolder" autocomplete="off">
            
            <div id="loading-container"></div>

            <form id="valForm">
                <input type="hidden" id="base_dir" name="base_dir">
                <input type="hidden" id="main_tex_val" name="main_tex">
                <ul id="tex-list"></ul>
                <button type="button" id="submit-btn">Run Validation & Generate Report</button>
            </form>
        </div>
        <script>
            const pathInput = document.getElementById('folder_path');
            const baseDirField = document.getElementById('base_dir');
            const mainTexField = document.getElementById('main_tex_val');
            const texList = document.getElementById('tex-list');
            const submitBtn = document.getElementById('submit-btn');
            const loadingContainer = document.getElementById('loading-container');
            const valForm = document.getElementById('valForm');

            // Auto-focus the input field instantly on load
            window.addEventListener('DOMContentLoaded', () => {
                pathInput.focus();
            });

            function formatTime(sec) {
                const m = Math.floor(sec / 60);
                const s = sec % 60;
                return `${m}mins ${s}sec`;
            }

            async function fetchTexFiles() {
                const folderPath = pathInput.value.trim();
                baseDirField.value = folderPath;
                if (!folderPath) { 
                    texList.innerHTML = ''; 
                    submitBtn.style.display = 'none'; 
                    loadingContainer.style.display = 'none'; 
                    return; 
                }
                try {
                    const response = await fetch('/get_tex_files', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ folder_path: folderPath })
                    });
                    const data = await response.json();
                    texList.innerHTML = '';
                    
                    if (data.files && data.files.length > 0) {
                        const docMatches = data.documentclass_matches || [];
                        
                        if (docMatches.length === 1) {
                            const detectedFile = docMatches[0];
                            mainTexField.value = detectedFile;
                            
                            valForm.style.display = 'none';
                            pathInput.style.display = 'none';
                            document.querySelector('p').style.display = 'none';
                            
                            loadingContainer.innerHTML = `
                                <div style="font-size:15px; font-weight:600; color:#1f2937; margin-bottom:8px;">
                                    Detected main TeX file: <code style="color:#2563eb;">${detectedFile}</code>
                                </div>
                                <div id="countdown-text-container" style="font-size:13px; color:#6b7280; margin-bottom:14px;">Running validation, please wait... (<span id="elapsed-sec">0mins 0sec</span>)</div>
                                <div style="width:100%; background:#e5e7eb; height:6px; border-radius:3px; overflow:hidden; margin-bottom:12px;">
                                    <div id="prog-bar" style="width:100%; background:#2563eb; height:100%;"></div>
                                </div>
                                <div id="live-steps-container" style="text-align:left;"></div>
                                <button type="button" id="cancel-auto-btn" style="background:#dc2626; color:white; padding:7px 16px; border:none; border-radius:6px; cursor:pointer; font-weight:600; font-size:13px; display:inline-block; margin-top:15px;">Cancel</button>
                            `;
                            loadingContainer.style.display = 'block';

                            let elapsed = 0;
                            let isFinished = false;
                            const timerInterval = setInterval(() => {
                                if (!isFinished) {
                                    elapsed++;
                                    const secEl = document.getElementById('elapsed-sec');
                                    if (secEl) secEl.innerText = formatTime(elapsed);
                                }
                            }, 1000);

                            let timeoutId = setTimeout(() => {
                                const cancelBtn = document.getElementById('cancel-auto-btn');
                                if (cancelBtn) cancelBtn.style.display = 'none';
                                
                                const cdDiv = document.getElementById('countdown-text-container');
                                if (cdDiv) cdDiv.innerHTML = `<span style="color:#2563eb; font-weight:600;">Executing compilation stages in real time...</span>`;
                                
                                const eventSource = new EventSource(`/stream_validate?base_dir=${encodeURIComponent(folderPath)}&main_tex=${encodeURIComponent(detectedFile)}`);
                                const stepsDiv = document.getElementById('live-steps-container');
                                let stepsTable = document.createElement('table');
                                stepsTable.className = 'steps-table';
                                stepsTable.innerHTML = `<thead><tr><th>Pipeline Stage</th><th>Status</th><th>Duration</th></tr></thead><tbody id="steps-tbody"></tbody>`;
                                stepsDiv.appendChild(stepsTable);
                                const tbody = document.getElementById('steps-tbody');

                                eventSource.onmessage = function(event) {
                                    const data = JSON.parse(event.data);
                                    if (data.status === 'finished') {
                                        isFinished = true;
                                        clearInterval(timerInterval);
                                        eventSource.close();
                                        window.location.href = data.report_url;
                                    } else if (data.status === 'error') {
                                        isFinished = true;
                                        clearInterval(timerInterval);
                                        eventSource.close();
                                        alert(data.message);
                                        window.location.reload();
                                    } else {
                                        let rowId = 'stage-row-' + data.stage_index;
                                        let row = document.getElementById(rowId);
                                        let statusHtml = data.status === 'Completed' || data.status.includes('Processed') ? 
                                            `<span style="color:#059669; font-weight:600;">✔ ${data.status}</span>` : 
                                            `<span style="color:#d97706; font-weight:600;">⏳ ${data.status}</span>`;
                                        
                                        if (!row) {
                                            row = document.createElement('tr');
                                            row.id = rowId;
                                            row.innerHTML = `<td>${data.step}</td><td id="status-${data.stage_index}">${statusHtml}</td><td id="time-${data.stage_index}">${data.time}</td>`;
                                            tbody.appendChild(row);
                                        } else {
                                            document.getElementById('status-' + data.stage_index).innerHTML = statusHtml;
                                            document.getElementById('time-' + data.stage_index).innerText = data.time;
                                        }
                                    }
                                };
                            }, 3000);

                            document.getElementById('cancel-auto-btn').onclick = () => {
                                clearTimeout(timeoutId);
                                clearInterval(timerInterval);
                                loadingContainer.style.display = 'none';
                                valForm.style.display = 'block';
                                pathInput.style.display = 'block';
                                document.querySelector('p').style.display = 'block';
                                fetchTexFiles();
                                pathInput.focus(); // Refocus on cancel
                            };
                            return;
                        }

                        loadingContainer.style.display = 'none';
                        valForm.style.display = 'block';
                        data.files.forEach((file, index) => {
                            const isChecked = file === data.suggested || (data.suggested === null && index === 0);
                            const li = document.createElement('li');
                            li.innerHTML = `<label style="cursor:pointer; width:100%; display:flex; align-items:center; gap:10px;"><input type="radio" name="main_tex" value="${file}" ${isChecked ? 'checked' : ''} required> <code>${file}</code> ${isChecked ? '<span style="color:#059669; font-size:12px; font-weight:600;">(Auto-Detected Main)</span>' : ''}</label>`;
                            texList.appendChild(li);
                        });
                        submitBtn.style.display = 'block';
                    } else {
                        loadingContainer.style.display = 'none';
                        valForm.style.display = 'block';
                        texList.innerHTML = '<li style="color:#b91c1c;">No .tex files found.</li>';
                        submitBtn.style.display = 'none';
                    }
                } catch (err) {
                    loadingContainer.style.display = 'none';
                    valForm.style.display = 'block';
                    texList.innerHTML = '<li style="color:#b91c1c;">Error scanning path.</li>';
                    submitBtn.style.display = 'none';
                }
            }
            pathInput.addEventListener('input', fetchTexFiles);
            pathInput.addEventListener('paste', () => setTimeout(fetchTexFiles, 50));
            submitBtn.addEventListener('click', () => {
                const selected = document.querySelector('input[name="main_tex"]:checked');
                if (selected) {
                    mainTexField.value = selected.value;
                    valForm.action = '/validate_manual';
                    valForm.method = 'post';
                    valForm.submit();
                }
            });
        </script>
    </body>
    </html>
    ''')

@app.route('/get_tex_files', methods=['POST'])
def get_tex_files():
    data = request.get_json()
    folder_path = data.get('folder_path', '').strip()
    if not folder_path or not os.path.exists(folder_path): return jsonify({'files': [], 'suggested': None, 'documentclass_matches': []})
    
    tex_files = []
    documentclass_matches = []
    
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            if file.endswith('.tex'):
                full_p = os.path.join(root, file)
                rel = os.path.relpath(full_p, folder_path)
                tex_files.append(rel)
                try:
                    with open(full_p, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                        if re.search(r'(?<!%)\s*\\documentclass', content):
                            documentclass_matches.append(rel)
                except Exception: pass

    suggested = documentclass_matches[0] if len(documentclass_matches) == 1 else (tex_files[0] if tex_files else None)
    return jsonify({'files': tex_files, 'suggested': suggested, 'documentclass_matches': documentclass_matches})

@app.route('/stream_validate', methods=['GET'])
def stream_validate():
    base_dir = request.args.get('base_dir', '').strip()
    main_tex_rel = request.args.get('main_tex', '').strip()
    validator = TexValidatorStreamer(os.path.join(base_dir, main_tex_rel))
    return Response(validator.run_pipeline(), mimetype='text/event-stream')

@app.route('/validate_manual', methods=['POST'])
def validate_manual():
    base_dir = request.form.get('base_dir', '').strip()
    main_tex_rel = request.form.get('main_tex', '').strip()
    validator = TexValidatorStreamer(os.path.join(base_dir, main_tex_rel))
    # Run synchronously for manual click submissions
    for _ in validator.run_pipeline(): pass
    return send_from_directory(validator.dir_name, f"{validator.raw_base}_report.html")

@app.route('/get_report_file')
def get_report_file():
    path = request.args.get('path')
    if path:
        abs_path = os.path.abspath(path)
        if os.path.exists(abs_path):
            directory, filename = os.path.split(abs_path)
            return send_from_directory(directory, filename)
    return abort(404)

@app.route('/view_file')
def view_file():
    path = request.args.get('path')
    if path:
        abs_path = os.path.abspath(path)
        if os.path.exists(abs_path):
            directory, filename = os.path.split(abs_path)
            return send_from_directory(directory, filename)
    return abort(404)


# ==========================================
# STEP 2: DUMMY PLACEHOLDER PAGE
# ==========================================
@app.route('/step2', methods=['GET'])
def step2_dummy():
    return render_template_string(r"""
    <!DOCTYPE html>
    <html lang="en">
    <head><title>Step 2: Placeholder Process</title>
    <style>
        body { font-family: "Segoe UI", Arial, sans-serif; background: #f4f6f8; margin: 0; padding: 40px; color: #1f2937; }
        .container { max-width: 650px; margin: auto; background: white; padding: 35px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,.08); }
        .nav-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .nav-btn { background: #2563eb; color: white; padding: 8px 16px; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 13px; }
        .nav-btn:hover { background: #1d4ed8; }
        h2 { margin-top: 0; }
        .box { background: #f9fafb; border: 2px dashed #d1d5db; border-radius: 10px; padding: 30px; text-align: center; color: #6b7280; font-size: 15px; margin-top: 20px; }
        .footer-nav { display: flex; justify-content: space-between; margin-top: 30px; }
    </style>
    </head>
    <body>
        <div class="container">
            <div class="nav-header">
                <h2>Step 2: Custom Process Placeholder</h2>
            </div>
            <p>This is a placeholder page where you can add your custom workflow steps later.</p>
            <div class="box">
                [ Insert your custom Step 2 process or tool here ]
            </div>
            <div class="footer-nav">
                <a href="/" class="nav-btn">&larr; Back to Step 1</a>
                <a href="/step3" class="nav-btn" style="background:#059669;">Proceed to Step 3 &rarr;</a>
            </div>
        </div>
    </body>
    </html>
    """)


# ==========================================
# STEP 3: FILE SEQUENCE MANAGER
# ==========================================
STEP3_HTML = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Step 3: TeX File Sequence Manager</title>
<style>
* { box-sizing: border-box; }
body { margin: 0; background: #f4f6f8; font-family: "Segoe UI", Arial, sans-serif; color: #202124; }
.container { width: 1000px; max-width: 94%; margin: 35px auto; }
.header { margin-bottom: 24px; display: flex; justify-content: space-between; align-items: flex-start; }
.header h1 { margin: 0; font-size: 28px; font-weight: 600; color: #111827; }
.header p { margin-top: 7px; color: #6b7280; font-size: 14px; }
.nav-link { background: #2563eb; color: white; padding: 8px 16px; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 13px; }
.nav-link:hover { background: #1d4ed8; }
.card { background: white; border-radius: 15px; padding: 26px; box-shadow: 0 5px 20px rgba(0, 0, 0, 0.07); }
.row { display: flex; gap: 12px; margin-bottom: 20px; }
.field { flex: 1; }
label.field-label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 7px; color: #4b5563; }
input[type="text"] { width: 100%; height: 44px; border: 1px solid #d1d5db; border-radius: 8px; padding: 0 13px; font-size: 14px; outline: none; transition: 0.2s; }
input[type="text"]:focus { border-color: #2563eb; box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.10); }
button { border: 0; border-radius: 8px; height: 44px; padding: 0 20px; cursor: pointer; font-size: 14px; font-weight: 600; }
button:disabled { cursor: not-allowed; opacity: 0.55; }
.rename-btn { background: #2563eb; color: white; min-width: 165px; }
.rename-btn:hover { background: #1d4ed8; }
.list-header { display: flex; justify-content: space-between; align-items: center; margin-top: 10px; margin-bottom: 10px; }
.list-header strong { font-size: 15px; }
.count { color: #6b7280; font-size: 13px; }
.file-list { min-height: 180px; border: 2px dashed #cbd5e1; border-radius: 12px; padding: 10px; background: #f8fafc; transition: border-color 0.2s, background 0.2s; }
.file-list.drag-active { border-color: #2563eb; background: #eff6ff; }
.drop-placeholder { min-height: 160px; display: flex; align-items: center; justify-content: center; color: #94a3b8; font-size: 14px; text-align: center; line-height: 1.7; }
.file-item { display: flex; align-items: center; min-height: 58px; background: white; border: 1px solid #e2e8f0; border-radius: 9px; margin-bottom: 7px; padding: 0 12px; cursor: grab; transition: box-shadow .15s, transform .15s, opacity .15s; }
.file-item:last-child { margin-bottom: 0; }
.file-item:hover { box-shadow: 0 3px 10px rgba(0, 0, 0, .08); }
.file-item:active { cursor: grabbing; }
.file-item.dragging { opacity: .40; }
.drag-handle { width: 32px; color: #94a3b8; font-size: 19px; letter-spacing: -3px; user-select: none; }
.number { width: 48px; color: #64748b; font-family: Consolas, monospace; font-size: 13px; }
.filename { flex: 1; font-family: Consolas, monospace; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.expected { width: 240px; text-align: left; color: #2563eb; font-size: 12px; font-family: Consolas, monospace; padding-right: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ignore-box { width: 85px; margin: 0; display: flex; align-items: center; gap: 6px; color: #64748b; font-size: 12px; cursor: pointer; user-select: none; }
.ignore-box input { width: 16px; height: 16px; cursor: pointer; }
.file-item.ignored { opacity: 0.48; background: #f1f5f9; border-color: #cbd5e1; }
.file-item.ignored .filename { text-decoration: line-through; color: #64748b; }
.file-item.ignored .expected { color: #94a3b8; font-weight: 600; }
.bottom { display: flex; align-items: center; justify-content: space-between; margin-top: 22px; gap: 20px; }
.info { color: #64748b; font-size: 13px; line-height: 1.7; }
.actions-group { display: flex; gap: 10px; align-items: center; }
.message { margin-top: 18px; padding: 12px 15px; border-radius: 8px; display: none; font-size: 14px; white-space: pre-line; }
.message.success { display: block; background: #ecfdf5; color: #047857; }
.message.error { display: block; background: #fef2f2; color: #b91c1c; }
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <h1>Step 3: TeX File Sequence Manager</h1>
            <p>Arrange your TeX files and rename them automatically in sequence.</p>
        </div>
        <a href="/step2" class="nav-link">&larr; Back to Step 2</a>
    </div>
    <div class="card">
        <div class="row">
            <div class="field">
                <label class="field-label">TeX Folder</label>
                <input type="text" id="folder" placeholder="Paste the folder path here (e.g., C:\itools\WMS\WorkingFolder)">
            </div>
        </div>
        <div class="field" style="margin-bottom:22px;">
            <label class="field-label">Expected Name / Prefix</label>
            <input type="text" id="prefix" placeholder="Example: Saari --> 00-Saari-Prelims" oninput="updateExpectedNames()">
        </div>
        <div class="list-header">
            <strong>TeX Files</strong>
            <span class="count" id="count">0 files</span>
        </div>
        <div class="file-list" id="fileList" ondragover="allowDrop(event)" ondrop="dropFile(event)" ondragleave="dragLeave(event)">
            <div class="drop-placeholder">Load a folder first.<br>Then drag files up or down to change their order.</div>
        </div>
        <div class="bottom">
            <div class="info">
                First active file &rarr; <b>00-Prefix-Prelims.tex</b><br>
                Remaining active files &rarr; <b>01-Prefix-Ch01.tex</b>, <b>02-Prefix-Ch02.tex</b>...
            </div>
            <div class="actions-group">
                <button class="rename-btn" id="renameButton" onclick="renameFiles()" disabled>Rename Files</button>
            </div>
        </div>
        <div id="message" class="message"></div>
    </div>
</div>

<script>
let files = [];
let draggedIndex = null;
const $ = id => document.getElementById(id);

$("folder").addEventListener("paste", () => setTimeout(loadFiles, 0));
$("folder").addEventListener("change", () => loadFiles());

function buildName(sequence, prefix) {
    if (sequence === 0) return "00-" + prefix + "-Prelims.tex";
    const n = String(sequence).padStart(2, "0");
    return n + "-" + prefix + "-Ch" + n + ".tex";
}

async function loadFiles(silent = false) {
    const folder = $("folder").value.trim();
    if (!folder) { showMessage("Please enter a folder path.", true); return; }
    try {
        const response = await fetch("/rename_files_list", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ folder: folder }) });
        const data = await response.json();
        if (!data.success) { showMessage(data.message, true); return; }
        files = data.files.map(name => ({ name: name, ignored: false }));
        renderFiles();
        if (!silent) showMessage(files.length + (files.length === 1 ? " TeX file loaded." : " TeX files loaded."), false);
    } catch (error) {
        showMessage("Unable to connect to the local application.", true);
    }
}

function renderFiles() {
    const list = $("fileList");
    list.innerHTML = "";
    if (files.length === 0) {
        list.innerHTML = '<div class="drop-placeholder">No .tex files found.</div>';
        $("count").innerText = "0 files";
        $("renameButton").disabled = true;
        return;
    }
    files.forEach((file, index) => {
        const item = document.createElement("div");
        item.className = "file-item" + (file.ignored ? " ignored" : "");
        item.draggable = true;
        item.dataset.index = index;
        item.innerHTML = `
            <div class="drag-handle">&vellip;&vellip;</div>
            <div class="number">${String(index).padStart(2, "0")}</div>
            <div class="filename">${escapeHtml(file.name)}</div>
            <div class="expected" id="expected-${index}"></div>
            <label class="ignore-box"><input type="checkbox" ${file.ignored ? "checked" : ""}>Ignore</label>`;
        item.querySelector("input").addEventListener("change", e => toggleIgnore(index, e.target.checked));
        item.addEventListener("dragstart", dragStart);
        item.addEventListener("dragend", dragEnd);
        list.appendChild(item);
    });
    $("count").innerText = files.length + (files.length === 1 ? " file" : " files");
    $("renameButton").disabled = false;
    updateExpectedNames();
}

function toggleIgnore(index, ignored) {
    files[index].ignored = ignored;
    const items = document.querySelectorAll(".file-item");
    if (items[index]) items[index].classList.toggle("ignored", ignored);
    updateExpectedNames();
}

function dragStart(event) {
    draggedIndex = Number(event.currentTarget.dataset.index);
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", String(draggedIndex));
    event.currentTarget.classList.add("dragging");
}

function dragEnd(event) {
    event.currentTarget.classList.remove("dragging");
    $("fileList").classList.remove("drag-active");
    if (draggedIndex !== null) {
        const items = [...$("fileList").querySelectorAll(".file-item")];
        files = items.map(item => files[Number(item.dataset.index)]);
    }
    draggedIndex = null;
    renderFiles();
}

function allowDrop(event) {
    event.preventDefault();
    if (draggedIndex === null) return;
    const list = $("fileList");
    list.classList.add("drag-active");
    const dragged = document.querySelector(".dragging");
    if (!dragged) return;
    const after = getDragAfterElement(list, event.clientY);
    if (after == null) list.appendChild(dragged); else list.insertBefore(dragged, after);
}

function dragLeave(event) {
    const list = $("fileList");
    if (!list.contains(event.relatedTarget)) list.classList.remove("drag-active");
}

function dropFile(event) {
    event.preventDefault();
    $("fileList").classList.remove("drag-active");
}

function getDragAfterElement(container, y) {
    const elements = [...container.querySelectorAll(".file-item:not(.dragging)")];
    let closest = { offset: Number.NEGATIVE_INFINITY, element: null };
    elements.forEach(element => {
        const box = element.getBoundingClientRect();
        const offset = y - box.top - box.height / 2;
        if (offset < 0 && offset > closest.offset) closest = { offset: offset, element: element };
    });
    return closest.element;
}

function updateExpectedNames() {
    const prefix = $("prefix").value.trim();
    let sequence = 0;
    files.forEach((file, index) => {
        const element = $("expected-" + index);
        if (!element) return;
        if (file.ignored) { element.innerText = "IGNORED"; return; }
        element.innerText = prefix ? buildName(sequence, prefix) : "";
        sequence++;
    });
}

async function renameFiles() {
    const folder = $("folder").value.trim();
    const prefix = $("prefix").value.trim();
    if (!folder) { showMessage("Please enter a folder path.", true); return; }
    if (!prefix) { showMessage("Please enter the Expected Name / Prefix.", true); return; }
    if (files.length === 0) { showMessage("There are no files to rename.", true); return; }

    let preview = "";
    let sequence = 0;
    files.forEach(file => {
        if (file.ignored) { preview += file.name + "  ->  IGNORED\n"; return; }
        preview += file.name + "  ->  " + buildName(sequence, prefix) + "\n";
        sequence++;
    });
    if (sequence === 0) { showMessage("All files marked as Ignore.", true); return; }

    if (!confirm("Confirm changes:\n\n" + preview + "\nContinue?")) return;

    const button = $("renameButton");
    button.disabled = true;
    button.innerText = "Renaming...";
    try {
        const response = await fetch("/execute_rename", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ folder: folder, prefix: prefix, files: files.map(f => ({ name: f.name, ignored: f.ignored })) }) });
        const data = await response.json();
        if (!data.success) { showMessage(data.message, true); return; }
        await loadFiles(true);
        showMessage(data.message, false);
    } catch (error) {
        showMessage("Rename operation failed.", true);
    } finally {
        button.disabled = files.length === 0;
        button.innerText = "Rename Files";
    }
}

function showMessage(text, error) {
    const element = $("message");
    element.innerText = text;
    element.className = "message " + (error ? "error" : "success");
}

function escapeHtml(text) {
    return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}
</script>
</body>
</html>
"""

@app.route('/step3', methods=['GET'])
def step3_page():
    return render_template_string(STEP3_HTML)

def fail_json(message):
    return jsonify({"success": False, "message": message})

def check_folder_path(folder):
    if not folder: return None, "Folder path is empty."
    from pathlib import Path
    folder_path = Path(folder)
    if not folder_path.exists(): return None, "Folder does not exist."
    if not folder_path.is_dir(): return None, "Path is not a folder."
    return folder_path, None

@app.route("/rename_files_list", methods=["POST"])
def rename_files_list():
    from pathlib import Path
    data = request.get_json(silent=True) or {}
    folder_path, error = check_folder_path(str(data.get("folder", "")).strip())
    if error: return fail_json(error)
    files = sorted([p.name for p in folder_path.iterdir() if p.is_file() and p.suffix.lower() == ".tex"], key=lambda x: x.lower())
    return jsonify({"success": True, "files": files})

@app.route("/execute_rename", methods=["POST"])
def execute_rename():
    from pathlib import Path
    import uuid
    data = request.get_json(silent=True) or {}
    folder = str(data.get("folder", "")).strip()
    prefix = str(data.get("prefix", "")).strip()
    files = data.get("files", [])

    folder_path, error = check_folder_path(folder)
    if error: return fail_json(error)
    if not prefix: return fail_json("Prefix is missing.")

    active_files = [f for f in files if not f.get("ignored", False)]
    if not active_files: return fail_json("All files are ignored.")

    sources = []
    for file_info in active_files:
        filename = file_info.get("name", "")
        source = folder_path / filename
        if not source.exists(): return fail_json("File not found: " + filename)
        sources.append(source)

    targets = []
    for sequence in range(len(sources)):
        number = str(sequence).zfill(2)
        new_name = "00-" + prefix + "-Prelims.tex" if sequence == 0 else number + "-" + prefix + "-Ch" + number + ".tex"
        targets.append(folder_path / new_name)

    active_names = {s.name.lower() for s in sources}
    existing_names = {p.name.lower() for p in folder_path.iterdir()}
    for target in targets:
        if target.name.lower() in existing_names and target.name.lower() not in active_names:
            return fail_json("Target already exists: " + target.name)

    states = [{"original": s, "temp": folder_path / (".tmp_" + uuid.uuid4().hex), "final": t, "stage": "original"} for s, t in zip(sources, targets)]
    try:
        for state in states:
            state["original"].rename(state["temp"])
            state["stage"] = "temp"
        for state in states:
            state["temp"].rename(state["final"])
            state["stage"] = "final"
    except Exception as exc:
        for state in reversed(states):
            if state["stage"] == "final" and state["final"].exists():
                state["final"].rename(state["temp"])
        for state in reversed(states):
            if state["stage"] == "temp" and state["temp"].exists():
                state["temp"].rename(state["original"])
        return fail_json("Rename failed: " + str(exc))

    return jsonify({"success": True, "message": f"Successfully renamed {len(active_files)} file(s)."})


if __name__ == '__main__':
    webview.create_window('LaTeX Project Validator & Manager', app, width=1150, height=750)
    webview.start()
