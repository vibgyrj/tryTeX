import os
import re
import sys
import glob
import time
import shutil
import json
import subprocess
import threading
import queue
from collections import Counter
from flask import Flask, render_template_string, request, jsonify, send_from_directory, Response, abort
import webview

# ==========================================
# TOP TABS (shared by all pages)
# ==========================================
TABS = [('compile', 'Initial Compilation', '/'),
        ('castoff', 'Cast Off', '/step2'),
        ('msprep', 'MS Prep', '/step3')]

TABS_JS = """<script>
document.addEventListener('keydown', function (e) {
    if (!e.ctrlKey || e.key !== 'Tab') return;
    if (window.isProcessing || (typeof runLock !== 'undefined' && runLock) || (typeof running !== 'undefined' && running)) {
        if (!confirm("Files are currently loaded, loading, or processing. Do you want to continue switching tabs or cancel?")) {
            e.preventDefault();
            return;
        }
    }
    e.preventDefault();
    var tabs = Array.prototype.slice.call(document.querySelectorAll('.tabs a.tab'));
    var cur = tabs.findIndex(function (t) { return t.classList.contains('active'); });
    if (cur < 0) return;
    var next = (cur + (e.shiftKey ? -1 : 1) + tabs.length) % tabs.length;
    window.location.href = tabs[next].getAttribute('href');
});
document.addEventListener('DOMContentLoaded', function() {
    document.querySelectorAll('.tabs a.tab').forEach(function(tab) {
        tab.addEventListener('click', function(e) {
            if (window.isProcessing || (typeof runLock !== 'undefined' && runLock) || (typeof running !== 'undefined' && running)) {
                if (!confirm("Files are currently loaded, loading, or processing. Do you want to continue switching tabs or cancel?")) {
                    e.preventDefault();
                }
            }
        });
    });
});
</script>"""

TABS_CSS = """
.tabs { display: flex; justify-content: center; gap: 16px; padding: 40px 40px 0; background: var(--tabs-bg, #cfd5dc); }
.tabs.bleed { margin: -40px -40px 30px; }
.tabs a.tab { position: relative; padding: 10px 24px; border-radius: 14px 14px 0 0; background: var(--tab-bg, #e4e8ed); color: var(--tab-color, #4b5563); font-family: "Segoe UI", Arial, sans-serif; font-size: 13px; font-weight: 600; text-decoration: none; }
.tabs a.tab:hover { background: var(--tab-hover-bg, #eef1f4); }
.tabs a.tab.active { background: var(--tab-active-bg, #f4f6f8); color: var(--tab-active-color, #1f2937); }
.tabs a.tab.active::before, .tabs a.tab.active::after { content: ''; position: absolute; bottom: 0; width: 14px; height: 14px; }
.tabs a.tab.active::before { left: -14px; background: radial-gradient(circle at top left, transparent 14px, var(--tab-active-bg, #f4f6f8) 14.5px); }
.tabs a.tab.active::after { right: -14px; background: radial-gradient(circle at top right, transparent 14px, var(--tab-active-bg, #f4f6f8) 14.5px); }
"""

def tabs_html(active, bleed=False):
    items = ''.join(f'<a class="tab{" active" if key == active else ""}" href="{href}">{label}</a>' for key, label, href in TABS)
    return f'<nav class="tabs{" bleed" if bleed else ""}">{items}{TABS_JS}</nav>'

app = Flask(__name__)
_RUNNING = set()
_RUNNING_LOCK = threading.Lock()

# ==========================================
# STREAMING VALIDATOR ENGINE WITH LIVE STAGES
# ==========================================
class TexValidatorStreamer:
    DIFF_SECS_PER_PAGE = 2.0

    def __init__(self, file_path, engine='auto', ms_pdf=''):
        self.engine = engine
        self.ms_pdf = ms_pdf
        self.file_path = os.path.abspath(file_path)
        self.dir_name = os.path.dirname(self.file_path)
        self.raw_base = os.path.splitext(os.path.basename(self.file_path))[0]
        self.errors = []
        self.missing_items = []
        self.diff_report = None
        self.log_path = os.path.join(self.dir_name, f"{self.raw_base}.log")
        self.pages_diff_count = 0
        self.elapsed_time = 0.0
        self.stage_rows = {}
        self.diff_error = None

    @staticmethod
    def _fmt_secs(s):
        s = int(round(s))
        return f"{s // 60:02d}:{s % 60:02d}"

    def _count_pages(self, *pdfs):
        import fitz
        counts = []
        for p in pdfs:
            try:
                d = fitz.open(p)
                counts.append(d.page_count)
                d.close()
            except Exception as e:
                self.diff_error = f"cannot open {os.path.basename(p)} ({os.path.getsize(p):,} bytes): {e} - file is incomplete/corrupt"
                return 0
        return max(counts)

    def _detect_engine(self):
        uses_biber, eng = False, 'pdflatex'
        try:
            with open(self.file_path, 'r', encoding='utf-8-sig', errors='ignore') as f:
                src = re.sub(r'(?m)(?<!\\)%.*$', '', f.read())
        except Exception:
            return (self.engine if self.engine != 'auto' else eng), uses_biber
        bl = re.search(r'\\usepackage\s*(?:\[([^\]]*)\])?\s*\{biblatex\}', src)
        uses_biber = bool(bl) and not re.search(r'backend\s*=\s*bibtex', bl.group(1) or '')
        if self.engine in ('pdflatex', 'lualatex', 'xelatex'):
            return self.engine, uses_biber
        if (re.search(r'\\usepackage\s*(?:\[[^\]]*\])?\s*\{[^}]*\b(?:fontspec|unicode-math|polyglossia)\b[^}]*\}', src)
                or re.search(r'\\directlua|\\luacode|\\begin\{luacode\*?\}', src)):
            eng = 'lualatex'
        return eng, uses_biber

    def run_pipeline(self):
        key = os.path.normcase(self.file_path)
        with _RUNNING_LOCK:
            busy = key in _RUNNING
            if not busy: _RUNNING.add(key)
        if busy:
            yield f"data: {json.dumps({'status': 'error', 'message': 'A validation of this file is already running. Wait for it to finish.'})}\n\n"
            return
        try:
            for chunk in self._run_pipeline_core():
                try:
                    d = json.loads(chunk[6:].strip())
                    if 'stage_index' in d:
                        r = self.stage_rows.setdefault(d['stage_index'], {'started': time.strftime('%H:%M:%S')})
                        r.update(step=d['step'], status=d['status'], time=d['time'])
                except Exception: pass
                yield chunk
        finally:
            with _RUNNING_LOCK: _RUNNING.discard(key)

    def _run_pipeline_core(self):
        start_time = time.time()

        if not os.path.exists(self.file_path):
            self.errors.append({"domain": "System", "message": f"Source file '{self.file_path}' not found."})
            self.elapsed_time = time.time() - start_time
            yield f"data: {json.dumps({'status': 'error', 'message': 'Source file not found.'})}\n\n"
            return
        
        existing_pdf = os.path.join(self.dir_name, f"{self.raw_base}.pdf")
        backup_pdf = os.path.join(self.dir_name, f"{self.raw_base}_MS.pdf")

        if self.ms_pdf and self.ms_pdf != '__skip__':
            chosen = os.path.join(self.dir_name, os.path.basename(self.ms_pdf))
            try:
                if os.path.exists(chosen) and os.path.abspath(chosen) != os.path.abspath(backup_pdf):
                    shutil.copy2(chosen, backup_pdf)
            except Exception: pass
        elif os.path.exists(existing_pdf):
            try:
                if os.path.exists(backup_pdf): os.remove(backup_pdf)
                os.rename(existing_pdf, backup_pdf)
            except Exception: pass
        elif not os.path.exists(backup_pdf):
            pdfs = [p for p in glob.glob(os.path.join(self.dir_name, "*.pdf")) if not p.endswith("_MS.pdf") and not os.path.basename(p).startswith("diff_report_")]
            if len(pdfs) == 1:
                try: os.rename(pdfs[0], backup_pdf)
                except Exception: pass

        self.engine_name, uses_biber_pkg = self._detect_engine()
        yield f"data: {json.dumps({'status': 'engine', 'engine': self.engine_name})}\n\n"
        pdflatex_exe = r"\\integrafs3\TeXLiveCentral\texlive\2026\bin\windows" + "\\" + self.engine_name + ".exe"
        bibtex_exe = r"\\integrafs3\TeXLiveCentral\texlive\2026\bin\windows\bibtex.exe"
        biber_exe = r"\\integrafs3\TeXLiveCentral\texlive\2026\bin\windows\biber.exe"

        if not os.path.exists(pdflatex_exe): pdflatex_exe = shutil.which(self.engine_name) or pdflatex_exe
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
            # Stage 1
            yield f"data: {json.dumps({'stage_index': 0, 'step': f'1. {self.engine_name} Compiling (Draft Mode)', 'status': 'Running...', 'time': '...'})}\n\n"
            t0 = time.time()
            subprocess.run([pdflatex_exe, "-interaction=nonstopmode", "-draftmode", self.file_path],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace", cwd=self.dir_name,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            dur1 = self._fmt_secs(time.time() - t0)
            yield f"data: {json.dumps({'stage_index': 0, 'step': f'1. {self.engine_name} Compiling (Draft Mode)', 'status': 'Completed', 'time': dur1})}\n\n"

            # Stage 2
            bcf_path = os.path.join(self.dir_name, f"{self.raw_base}.bcf")
            use_biber = (os.path.exists(bcf_path) or uses_biber_pkg) and os.path.exists(biber_exe)
            bib_tool_name = "biber" if use_biber else "bibtex"
            bib_tool = biber_exe if use_biber else bibtex_exe

            yield f"data: {json.dumps({'stage_index': 1, 'step': f'2. Bibliography Processing ({bib_tool_name})', 'status': 'Running...', 'time': '...'})}\n\n"
            t0 = time.time()
            bib_res = subprocess.run([bib_tool, self.raw_base],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace", cwd=self.dir_name,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            bib_status = "Processed (Present)" if bib_res.returncode == 0 or os.path.exists(os.path.join(self.dir_name, f"{self.raw_base}.bbl")) else "Skipped / Not Required"
            dur2 = self._fmt_secs(time.time() - t0)
            yield f"data: {json.dumps({'stage_index': 1, 'step': f'2. Bibliography Processing ({bib_tool_name})', 'status': bib_status, 'time': dur2})}\n\n"

            # Stages 3 & 4
            total_full_passes = 3 if has_toc else 1
            s_idx = 2
            for pass_idx in range(total_full_passes):
                pass_label = f"3. Full Compilation Pass {pass_idx + 1}" if pass_idx == 0 else f"4. Subsequent Compilation Pass {pass_idx + 1}"
                current_s_idx = s_idx + pass_idx
                
                yield f"data: {json.dumps({'stage_index': current_s_idx, 'step': pass_label, 'status': 'Running...', 'time': '...'})}\n\n"
                t0 = time.time()
                subprocess.run([pdflatex_exe, "-interaction=nonstopmode", self.file_path],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace", cwd=self.dir_name,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                dur_pass = self._fmt_secs(time.time() - t0)
                yield f"data: {json.dumps({'stage_index': current_s_idx, 'step': pass_label, 'status': 'Completed', 'time': dur_pass})}\n\n"

                need_rerun = False
                if os.path.exists(self.log_path):
                    with open(self.log_path, 'r', encoding='utf-8', errors='ignore') as lf:
                        log_content = lf.read()
                        if "Rerun to get cross-references right" in log_content or "Label(s) may have changed" in log_content:
                            need_rerun = True
                
                if not need_rerun and pass_idx >= 1:
                    break

            blg_path = os.path.join(self.dir_name, f"{self.raw_base}.blg")
            if os.path.exists(blg_path): self._parse_bib_log_file(blg_path)
        except (FileNotFoundError, TypeError):
            self.errors.append({"domain": "System Engine", "message": "Critical Error: 'pdflatex' or 'bibtex' tools are missing."})
            yield f"data: {json.dumps({'status': 'error', 'message': 'TeX tools missing.'})}\n\n"
            self.elapsed_time = time.time() - start_time
            return

        if os.path.exists(self.log_path): self._parse_log_file(self.log_path)
        self._check_missing_materials()
        self._check_missing_from_log()

        new_pdf = os.path.join(self.dir_name, f"{self.raw_base}.pdf")
        ms_pdf = os.path.join(self.dir_name, f"{self.raw_base}_MS.pdf")

        if os.path.exists(new_pdf) and os.path.exists(ms_pdf):
            diff_step = '5. Diff PDF Preparation'
            n_pages = self._count_pages(new_pdf, ms_pdf)
            est = n_pages * self.DIFF_SECS_PER_PAGE
            first_status = f"Running... {n_pages} pages, est. ~{int(est)}s - please wait" if n_pages else "Running..."
            yield f"data: {json.dumps({'stage_index': 5, 'step': diff_step, 'status': first_status, 'time': '...'})}\n\n"
            t0 = time.time()

            q = queue.Queue()
            def _worker():
                try:
                    if not n_pages and self.diff_error: raise RuntimeError(self.diff_error)
                    self.diff_report = self._compare_pdfs_isolated(new_pdf, ms_pdf, progress=lambda d, t: q.put((d, t)))
                except Exception as e:
                    self.diff_error = f"{type(e).__name__}: {e}"
                finally:
                    q.put(None)
            threading.Thread(target=_worker, daemon=True).start()

            while True:
                try:
                    item = q.get(timeout=5)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                if item is None:
                    break
                done, total = item
                left = ((time.time() - t0) / done * (total - done)) if done > 0 else est
                prog_status = f"Running... page {done}/{total}, ~{int(left)}s left"
                yield f"data: {json.dumps({'stage_index': 5, 'step': diff_step, 'status': prog_status, 'time': '...'})}\n\n"

            dur5 = self._fmt_secs(time.time() - t0)
            if self.diff_report:
                diff_status = 'Completed'
            else:
                diff_status = f"Failed: {self.diff_error or 'unknown error'}"
            yield f"data: {json.dumps({'stage_index': 5, 'step': diff_step, 'status': diff_status, 'time': dur5})}\n\n"
        else:
            yield f"data: {json.dumps({'stage_index': 5, 'step': '5. Diff PDF Preparation', 'status': 'Skipped / PDF not available', 'time': '-'})}\n\n"

        self.elapsed_time = time.time() - start_time
        report_url = self.generate_html_report()
        
        yield f"data: {json.dumps({'status': 'finished', 'report_url': f'/get_report_file?path={report_url}'})}\n\n"

    def _parse_log_file(self, log_path):
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            log_content = f.read()
        error_blocks = re.findall(r'^!(.*?)(?=^!|^Text broken|^\s*$)', log_content, re.MULTILINE | re.DOTALL)
        for block in error_blocks:
            lines = block.strip().split('\n')
            if lines: self.errors.append({"domain": "TeX Engine", "message": lines[0].strip(), "detail": "\n".join(l.rstrip() for l in lines[1:3])})

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

    def _check_missing_from_log(self):
        log = ""
        for p in (self.log_path, os.path.join(self.dir_name, f"{self.raw_base}.blg")):
            if os.path.exists(p):
                with open(p, 'r', encoding='utf-8', errors='ignore') as f:
                    log += f.read() + "\n"
        if not log.strip(): return
        known = {os.path.splitext(os.path.basename(n))[0].lower() for item in self.missing_items for n in re.findall(r'<strong>(.*?)</strong>', item)}
        patterns = [r"File\s+[`']([^'`]{1,200})'\s+not\s+found",
                    r"Unable to load picture or PDF file\s*[`'\"]([^'`\"]{1,200})[`'\"]",
                    r"I can't find file\s+[`']([^'`]{1,200})'",
                    r"Cannot find\s+(?:control file\s+)?[`'\"]([^'`\"]{1,200})[`'\"]",
                    r"I couldn't open (?:database|style) file\s+(\S{1,200})"]
        for pat in patterns:
            for m in re.finditer(pat, log):
                name = m.group(1).replace('\n', '').strip()
                key = os.path.splitext(os.path.basename(name))[0].lower()
                if not name or key in known: continue
                known.add(key)
                ext = os.path.splitext(name)[1].lower()
                if ext in ('.png', '.jpg', '.jpeg', '.pdf', '.eps', '.svg', '.tif', '.tiff', '.gif', '.bmp'):
                    label = "Graphic asset/Image file not found"
                elif ext in ('.bib', '.bst', '.bcf', '.bbl'):
                    label = "Bibliography file not found"
                elif ext in ('', '.tex'):
                    label = "TeX input file not found"
                else:
                    label = "Support file (style/class/other) not found"
                self.missing_items.append(f"{label}: <strong>{name}</strong>")

    def _highlight_diff(self, pg, page_ms, page_new, pm, pn, header_h, dpi=150):
        import difflib
        import fitz
        scale = dpi / 72.0
        words_ms = page_ms.get_text("words")
        words_new = page_new.get_text("words")
        matcher = difflib.SequenceMatcher(None, [w[4] for w in words_ms], [w[4] for w in words_new], autojunk=False)
        text_changed = False
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == 'equal':
                continue
            text_changed = True
            if tag in ('replace', 'delete'):
                for w in words_ms[i1:i2]:
                    r = fitz.Rect(w[0] * scale, w[1] * scale + header_h, w[2] * scale, w[3] * scale + header_h)
                    pg.draw_rect(r, color=None, fill=(1, 0, 0), fill_opacity=0.35)
            if tag in ('replace', 'insert'):
                for w in words_new[j1:j2]:
                    r = fitz.Rect(pm.width + w[0] * scale, w[1] * scale + header_h, pm.width + w[2] * scale, w[3] * scale + header_h)
                    pg.draw_rect(r, color=None, fill=(0, 0.7, 0.2), fill_opacity=0.35)
        return text_changed

    def _compare_pdfs(self, new_pdf, ms_pdf, progress=None):
        try:
            import fitz
            import os
            import difflib
            dn = fitz.open(new_pdf)
            dm = fitz.open(ms_pdf)
            diff_doc = fitz.open()
            pages_diff = 0
            max_pages = max(dn.page_count, dm.page_count)
            
            for i in range(max_pages):
                if progress: progress(i, max_pages)
                if i >= dn.page_count or i >= dm.page_count:
                    pages_diff += 1
                    continue
                
                words_ms = dm[i].get_text("words")
                words_new = dn[i].get_text("words")
                matcher = difflib.SequenceMatcher(None, [w[4] for w in words_ms], [w[4] for w in words_new], autojunk=False)
                has_text_diff = any(tag != 'equal' for tag, _, _, _, _ in matcher.get_opcodes())
                
                if not has_text_diff:
                    continue  # Skip pages with zero text differences completely
                    
                pages_diff += 1
                pn = dn[i].get_pixmap(dpi=150, colorspace=fitz.csRGB)
                pm = dm[i].get_pixmap(dpi=150, colorspace=fitz.csRGB)
                
                header_h = 40
                w = pm.width + pn.width
                h = max(pm.height, pm.height) + header_h
                
                pg = diff_doc.new_page(width=w, height=h)
                pg.draw_rect(fitz.Rect(0, 0, w, header_h), color=None, fill=(0.95, 0.95, 0.95))
                pg.insert_text(fitz.Point(20, 26), f"Manuscript (MS) - Page {i + 1}", fontsize=13, color=(0.8, 0.1, 0.1))
                pg.insert_text(fitz.Point(300, 26), "red = removed/changed text", fontsize=10, color=(0.4, 0.4, 0.4))
                pg.insert_text(fitz.Point(pm.width + 310, 26), "green = added/changed text", fontsize=10, color=(0.4, 0.4, 0.4))
                
                pg.insert_image(fitz.Rect(0, header_h, pm.width, pm.height + header_h), stream=pm.tobytes("png"))
                pg.insert_image(fitz.Rect(pm.width, header_h, pm.width + pn.width, pn.height + header_h), stream=pn.tobytes("png"))
                
                if hasattr(self, '_highlight_diff'):
                    self._highlight_diff(pg, dm[i], dn[i], pm, pn, header_h)
                        
            self.pages_diff_count = pages_diff
            
            if diff_doc.page_count == 0:
                pg = diff_doc.new_page(width=600, height=200)
                pg.draw_rect(fitz.Rect(0, 0, 600, 200), color=None, fill=(0.92, 0.97, 0.94))
                pg.insert_text(fitz.Point(40, 95), "✔ No Visual or Text Layout Differences Found!", fontsize=18, color=(0.02, 0.45, 0.24))
                pg.insert_text(fitz.Point(40, 125), "Both PDF documents match exactly at 150 DPI.", fontsize=12, color=(0.3, 0.3, 0.3))
                pg.insert_text(fitz.Point(40, 150), f"Manuscript (MS) PDF : {os.path.basename(ms_pdf)}", fontsize=10, color=(0.3, 0.3, 0.3))
                pg.insert_text(fitz.Point(40, 168), f"Newly Generated PDF : {os.path.basename(new_pdf)}", fontsize=10, color=(0.3, 0.3, 0.3))

            report_path = os.path.join(self.dir_name, f"diff_report_{self.raw_base}.pdf")
            diff_doc.save(report_path, deflate=True, garbage=3)
            dn.close(); dm.close(); diff_doc.close()
            return report_path
            
        except Exception as e:
            import traceback; traceback.print_exc()
            self.diff_error = f"{type(e).__name__}: {e}"
            return None

    def _compare_pdfs_isolated(self, new_pdf, ms_pdf, progress=None):
        cmd = [sys.executable] + ([] if getattr(sys, 'frozen', False) else [os.path.abspath(__file__)]) + ['--diff', self.file_path, new_pdf, ms_pdf]
        result, err, tail = None, None, []
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf-8', errors='replace',
                                 cwd=self.dir_name, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            for line in p.stdout:
                line = line.rstrip()
                if line.startswith('PROGRESS '):
                    try:
                        d, t = line.split()[1:3]
                        if progress: progress(int(d), int(t))
                    except Exception: pass
                elif line.startswith('RESULT '): result = line[7:]
                elif line.startswith('ERROR '): err = line[6:]
                elif line:
                    tail.append(line); tail = tail[-8:]
            rc = p.wait()
        except Exception as e:
            self.diff_error = f"could not start diff process: {type(e).__name__}: {e}"
            return None
        if result and rc == 0:
            return result
        if err:
            self.diff_error = err
        else:
            self.diff_error = f"diff process crashed (exit code {rc & 0xFFFFFFFF:#x})" + (": " + " | ".join(tail[-3:]) if tail else "") + " - likely out of memory or a PyMuPDF crash"
        return None

    def generate_html_report(self):
        error_counts = Counter((err['domain'], err['message']) for err in self.errors)
        total_errors = sum(error_counts.values())
        total_missing = len(self.missing_items)

        mins = int(self.elapsed_time // 60)
        secs = int(self.elapsed_time % 60)
        formatted_exec_time = f"{mins:02d}:{secs:02d}"

        cards_html = f'''
        <div class="card"><div class="num">{total_errors}</div><div class="lbl">Compilation Errors</div></div>
        <div class="card"><div class="num">{total_missing}</div><div class="lbl">Missing Materials</div></div>
        <div class="card"><div class="num" style="color:#2563eb; font-size:22px;">{formatted_exec_time}</div><div class="lbl">Execution Time (MM:SS)</div></div>
        '''

        files_html = ""
        if self.missing_items:
            missing_rows = "".join(f"<tr><td>{item}</td></tr>" for item in self.missing_items)
            files_html = f'<h2 style="margin-top:28px;">Missing Structural Materials</h2><table class="files"><thead><tr><th>Missing Asset / Dependency</th></tr></thead><tbody>{missing_rows}</tbody></table>'

        groups_html = ""
        if self.errors:
            import html
            def _pattern(domain, msg):
                p = re.sub(r"`[^'`\n]*'", "`…'", msg)
                p = re.sub(r'"[^"\n]*"', '"…"', p)
                p = re.sub(r'\bEnvironment\s+\S+\s+undefined', 'Environment … undefined', p)
                p = re.sub(r'[\w\-./\\]+\.(?:sty|cls|tex|bst|bib|def|cfg|fd|png|jpe?g|pdf|eps|svg|tiff?)\b', '<file>', p, flags=re.I)
                p = re.sub(r'\d+(?:\.\d+)?', '#', p)
                if domain == 'Bibliography':
                    p = re.sub(r'\b(in|for)\s+\S+\s*$', r'\1 …', p)
                return p.strip()
            groups = {}
            for err in self.errors:
                groups.setdefault((err['domain'], _pattern(err['domain'], err['message'])), []).append(err)
            for (domain, pat), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
                distinct = {i['message'] for i in items}
                title = html.escape(next(iter(distinct)) if len(distinct) == 1 else pat)
                entries = []
                for i in items:
                    entry = html.escape(i['message'])
                    if i.get('detail'): entry += '\n' + html.escape(i['detail'])
                    entries.append(entry)
                body = "\n\n".join(entries)
                groups_html += f'''<details class="group"><summary><span class="badge">{len(items)}</span><span class="msg">[{domain}] {title}</span></summary><div class="body"><pre class="extra">{body}</pre></div></details>'''
        else:
            groups_html = '<div class="ok">No text compilation or bibliography schema errors detected!</div>'

        safe_diff = self.diff_report.replace('\\', '/') if self.diff_report else ''
        safe_log = self.log_path.replace('\\', '/') if self.log_path else ''

        import html as _html
        if self.diff_report and os.path.exists(self.diff_report):
            diff_btn_html = f'''<button onclick="showFrame('/view_file?path={safe_diff}')" class="action-btn" style="background:#2563eb;">PDF Diff Report</button>'''
        elif self.diff_error:
            diff_btn_html = f'<span style="display:inline-block;background:#fef2f2;color:#b91c1c;padding:10px 14px;border-radius:8px;font-weight:600;font-size:13px;">PDF diff failed: {_html.escape(self.diff_error)}</span>'
        else:
            diff_btn_html = ''
        log_btn_html = f'''<button onclick="showFrame('/view_file?path={safe_log}')" class="action-btn" style="background:#4b5563;">Log File</button>''' if os.path.exists(self.log_path) else ''

        TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><title>LaTeX Error Report</title>
<script>
    (function() {
        const savedTheme = localStorage.getItem('latex_theme') || 'dark';
        document.documentElement.setAttribute('data-theme', savedTheme);
    })();
</script>
<style>
.nav-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
.theme-switch-btn { background: var(--tab-bg); border: 1px solid var(--border-color); border-radius: 30px; width: 68px; height: 34px; display: flex; align-items: center; justify-content: space-between; padding: 0 4px; cursor: pointer; position: relative; transition: background 0.3s; }
.theme-switch-btn .icon { width: 26px; height: 26px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 14px; z-index: 2; transition: transform 0.3s, background 0.3s; }
.theme-switch-btn .sun { color: #f59e0b; }
.theme-switch-btn .moon { color: #94a3b8; }
:root[data-theme="dark"] .theme-switch-btn .sun { color: #64748b; }
:root[data-theme="dark"] .theme-switch-btn .moon { color: #38bdf8; }
.theme-switch-btn .knob { position: absolute; left: 4px; width: 26px; height: 26px; border-radius: 50%; background: #ffffff; box-shadow: 0 2px 5px rgba(0,0,0,0.2); transition: transform 0.3s; z-index: 1; }
:root[data-theme="dark"] .theme-switch-btn .knob { transform: translateX(34px); background: #262626; }
:root {
    --bg-color: #f4f6f8;
    --text-color: #1f2937;
    --card-bg: #fff;
    --border-color: #e5e7eb;
    --meta-color: #6b7280;
    --pre-bg: #f9fafb;
    --tabs-bg: #cfd5dc;
    --tab-bg: #e4e8ed;
    --tab-color: #4b5563;
    --tab-hover-bg: #eef1f4;
    --tab-active-bg: #f4f6f8;
    --tab-active-color: #1f2937;
}
:root[data-theme="dark"] {
    --bg-color: #000000;
    --text-color: #f3f4f6;
    --card-bg: #121212;
    --border-color: #262626;
    --meta-color: #94a3b8;
    --pre-bg: #0a0a0a;
    --tabs-bg: #000000;
    --tab-bg: #121212;
    --tab-color: #94a3b8;
    --tab-hover-bg: #222222;
    --tab-active-bg: #000000;
    --tab-active-color: #f3f4f6;
}
@@TABS_CSS@@
.nav-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
.theme-switch-btn { background: var(--tab-bg); border: 1px solid var(--border-color); border-radius: 30px; width: 68px; height: 34px; display: flex; align-items: center; justify-content: space-between; padding: 0 4px; cursor: pointer; position: relative; transition: background 0.3s; }
.theme-switch-btn .icon { width: 26px; height: 26px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 14px; z-index: 2; transition: transform 0.3s, background 0.3s; }
.theme-switch-btn .sun { color: #f59e0b; }
.theme-switch-btn .moon { color: #94a3b8; }
:root[data-theme="dark"] .theme-switch-btn .sun { color: #64748b; }
:root[data-theme="dark"] .theme-switch-btn .moon { color: #38bdf8; }
.theme-switch-btn .knob { position: absolute; left: 4px; width: 26px; height: 26px; border-radius: 50%; background: #ffffff; box-shadow: 0 2px 5px rgba(0,0,0,0.2); transition: transform 0.3s; z-index: 1; }
:root[data-theme="dark"] .theme-switch-btn .knob { transform: translateX(34px); background: #262626; }
body { margin: 0; background: var(--bg-color); color: var(--text-color); font-family: "Segoe UI", Arial, sans-serif; font-size: 14px; transition: background 0.3s, color 0.3s; }
.wrap { max-width: 1100px; margin: 30px auto; padding: 0 16px; }
h1 { margin: 0 0 4px; font-size: 26px; }
.meta { color: var(--meta-color); margin-bottom: 20px; font-family: Consolas, monospace; word-break: break-all; }
.cards { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
.card { flex: 1; min-width: 150px; background: var(--card-bg); border-radius: 12px; padding: 16px; box-shadow: 0 2px 10px rgba(0,0,0,.06); border: 1px solid var(--border-color); }
.num { font-size: 28px; font-weight: 700; color: #b91c1c; }
.lbl { color: var(--meta-color); margin-top: 2px; }
.actions { display: flex; gap: 15px; flex-wrap: wrap; margin-bottom: 20px; }
.action-btn { padding: 10px 18px; color: white; border: none; border-radius: 8px; font-weight: 600; font-size: 13px; cursor: pointer; display: inline-block; }
.action-btn:hover { opacity: 0.9; }
.viewer-container { margin-bottom: 30px; background: var(--card-bg); padding: 12px; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,.06); display: none; border: 1px solid var(--border-color); }
.viewer-header { display: flex; justify-content: flex-end; margin-bottom: 8px; }
.new-tab-icon { color: #2563eb; text-decoration: none; font-weight: 600; font-size: 13px; display: flex; align-items: center; gap: 4px; }
.new-tab-icon:hover { text-decoration: underline; }
iframe { width: 100%; height: 550px; border: 1px solid var(--border-color); border-radius: 8px; background: #fff; }
table { width: 100%; border-collapse: collapse; background: var(--card-bg); border-radius: 8px; overflow: hidden; box-shadow: 0 2px 10px rgba(0,0,0,.04); border: 1px solid var(--border-color); }
th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--border-color); color: var(--text-color); }
details.group, details.collapsible-section { background: var(--card-bg); border-radius: 10px; margin-bottom: 8px; box-shadow: 0 2px 10px rgba(0,0,0,.05); overflow: hidden; border: 1px solid var(--border-color); }
details > summary { cursor: pointer; padding: 11px 14px; display: flex; align-items: center; gap: 10px; font-weight: 600; list-style: none; }
details > summary::-webkit-details-marker { display: none; }
details > summary::before { content: '▶'; display: inline-block; font-size: 11px; transition: transform 0.2s; color: var(--meta-color); margin-right: 4px; }
details[open] > summary::before { transform: rotate(90deg); }
.badge { min-width: 42px; text-align: center; padding: 2px 8px; border-radius: 999px; background: #fee2e2; color: #b91c1c; font-size: 12px; }
.ok { background: #ecfdf5; color: #047857; padding: 18px; border-radius: 10px; font-weight: 600; margin-top: 20px; }
pre.extra { margin: 0; padding: 10px 14px; background: var(--pre-bg); color: var(--text-color); font-size: 12.5px; white-space: pre-wrap; }
details.collapsible-section { margin-top: 28px; }
details.collapsible-section > summary { padding: 14px 18px; font-size: 16px; }
details.collapsible-section[open] > summary { border-bottom: 1px solid var(--border-color); }
</style>
</head>
<body>
@@TABS@@
<div class="wrap">
<div class="nav-header">
    <h1>LaTeX Error Report</h1>
    <div class="theme-switch-btn" onclick="toggleTheme()" title="Toggle Dark/Light Mode">
        <div class="knob"></div>
        <div class="icon sun">☀️</div>
        <div class="icon moon">🌙</div>
    </div>
</div>
<div class="meta"><b>@@TITLE@@</b></div>
<div class="cards">@@CARDS@@</div>
<div class="actions">@@DIFF_BTN@@ @@LOG_BTN@@</div>
<div id="viewerBox" class="viewer-container">
    <div class="viewer-header">
        <a id="newTabLink" href="#" target="_blank" class="new-tab-icon">Open in new tab ↗</a>
    </div>
    <iframe id="fileFrame"></iframe>
</div>
@@TIME@@
@@FILES@@
@@GROUPS@@
</div>
<script>
let currentUrl = null;
function showFrame(url) {
    const box = document.getElementById('viewerBox');
    const frame = document.getElementById('fileFrame');
    const newTabLink = document.getElementById('newTabLink');
    if (box.style.display === 'block' && currentUrl === url) {
        box.style.display = 'none';
        frame.src = 'about:blank';
        currentUrl = null;
        return;
    }
    frame.src = url;
    newTabLink.href = url;
    currentUrl = url;
    box.style.display = 'block';
    box.scrollIntoView({ behavior: 'smooth' });
}
function toggleTheme() {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    const nextTheme = isDark ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', nextTheme);
    localStorage.setItem('latex_theme', nextTheme);
}
</script>
</body>
</html>
"""
        time_rows = "".join(f"<tr><td>{r['step']}</td><td>{r['status']}</td><td>{r['started']}</td><td>{r['time']}</td></tr>" for _, r in sorted(self.stage_rows.items()))
        time_html = f'''<details class="collapsible-section"><summary>Execution Time Report (MM:SS)</summary><table class="files" style="margin:0; border:none; box-shadow:none;"><thead><tr><th>Pipeline Stage</th><th>Status</th><th>Started In</th><th>Duration</th></tr></thead><tbody>{time_rows}</tbody></table></details>''' if time_rows else ''
        
        report_html = (TEMPLATE
            .replace("@@TABS_CSS@@", TABS_CSS)
            .replace("@@TABS@@", tabs_html('compile'))
            .replace("@@TITLE@@", self.file_path)
            .replace("@@CARDS@@", cards_html)
            .replace("@@DIFF_BTN@@", diff_btn_html)
            .replace("@@LOG_BTN@@", log_btn_html)
            .replace("@@TIME@@", time_html)
            .replace("@@FILES@@", files_html)
            .replace("@@GROUPS@@", groups_html)
        )
        export_html = report_html.replace(groups_html, '')
        for pat in (r'<nav class="tabs.*?</nav>', r'<div class="actions">.*?</div>',
                    r'<div id="viewerBox".*?</iframe>\s*</div>', r'<script>\s*let currentUrl.*?</script>'):
            export_html = re.sub(pat, '', export_html, flags=re.S)
        report_name = os.path.join(self.dir_name, f"{self.raw_base}_report.html")
        with open(report_name, "w", encoding="utf-8") as f: f.write(export_html)
        import tempfile
        app_name = os.path.join(tempfile.gettempdir(), f"{self.raw_base}_report_app.html")
        with open(app_name, "w", encoding="utf-8") as f: f.write(report_html)
        return app_name


# ==========================================
# FLASK ROUTES
# ==========================================

@app.route('/', methods=['GET'])
def index():
    return render_template_string(r'''
    <!DOCTYPE html>
    <html lang="en">
    <head>
    <meta charset="UTF-8"><title>Step 1: LaTeX Project Validator</title>
    <script>
        (function() {
            const savedTheme = localStorage.getItem('latex_theme') || 'dark';
            document.documentElement.setAttribute('data-theme', savedTheme);
        })();
    </script>
    <style>
        :root {
            --bg-color: #000000;
            --text-color: #f3f4f6;
            --card-bg: #121212;
            --border-color: #262626;
            --meta-color: #94a3b8;
            --tabs-bg: #000000;
            --tab-bg: #121212;
            --tab-color: #94a3b8;
            --tab-hover-bg: #222222;
            --tab-active-bg: #000000;
            --tab-active-color: #f3f4f6;
        }
        :root[data-theme="light"] {
            --bg-color: #f4f6f8;
            --text-color: #1f2937;
            --card-bg: #ffffff;
            --border-color: #d1d5db;
            --meta-color: #4b5563;
            --tabs-bg: #cfd5dc;
            --tab-bg: #e4e8ed;
            --tab-color: #4b5563;
            --tab-hover-bg: #eef1f4;
            --tab-active-bg: #f4f6f8;
            --tab-active-color: #1f2937;
        }
        {{ tabs_css|safe }}
        body { font-family: "Segoe UI", Arial, sans-serif; background: var(--bg-color); margin: 0; padding: 40px; color: var(--text-color); transition: background 0.3s, color 0.3s; }
        .container { max-width: 650px; margin: auto; background: var(--card-bg); padding: 35px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,.15); border: 1px solid var(--border-color); }
        .nav-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .theme-switch-btn { background: var(--tab-bg); border: 1px solid var(--border-color); border-radius: 30px; width: 68px; height: 34px; display: flex; align-items: center; justify-content: space-between; padding: 0 4px; cursor: pointer; position: relative; transition: background 0.3s; }
        .theme-switch-btn .icon { width: 26px; height: 26px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 14px; z-index: 2; transition: transform 0.3s, background 0.3s; }
        .theme-switch-btn .sun { color: #f59e0b; }
        .theme-switch-btn .moon { color: #94a3b8; }
        :root[data-theme="dark"] .theme-switch-btn .sun { color: #64748b; }
        :root[data-theme="dark"] .theme-switch-btn .moon { color: #38bdf8; }
        .theme-switch-btn .knob { position: absolute; left: 4px; width: 26px; height: 26px; border-radius: 50%; background: #ffffff; box-shadow: 0 2px 5px rgba(0,0,0,0.2); transition: transform 0.3s; z-index: 1; }
        :root[data-theme="dark"] .theme-switch-btn .knob { transform: translateX(34px); background: #262626; }
        h2 { margin-top: 0; color: var(--text-color); }
        input[type="text"], select { width: 100%; padding: 12px; border: 1px solid var(--border-color); border-radius: 8px; font-size: 14px; box-sizing: border-box; background: var(--card-bg); color: var(--text-color); }
        #tex-list { 
            margin-top: 20px; 
            list-style: none; 
            padding: 8px; 
            max-height: 220px; 
            overflow-y: auto; 
            border: 1px solid var(--border-color); 
            border-radius: 8px; 
            background: var(--card-bg);
        }
        #tex-list li { 
            padding: 10px; 
            background: var(--bg-color); 
            margin-bottom: 6px; 
            border-radius: 6px; 
            border: 1px solid var(--border-color); 
            display: flex; 
            align-items: center; 
            gap: 10px; 
        }
        #tex-list li:last-child { margin-bottom: 0; }
        button { margin-top: 20px; padding: 10px 22px; background: #2563eb; color: white; border: none; border-radius: 8px; cursor: pointer; font-weight: 600; display: none; }
        #loading-container { display: none; text-align: center; padding: 20px 0; }
        table.steps-table { width: 100%; border-collapse: collapse; background: var(--card-bg); border-radius: 8px; overflow: hidden; margin-top: 15px; box-shadow: 0 2px 8px rgba(0,0,0,.04); text-align: left; border: 1px solid var(--border-color); }
        table.steps-table th, table.steps-table td { padding: 8px 12px; border-bottom: 1px solid var(--border-color); font-size: 13px; color: var(--text-color); }
        #pdf-list { margin-top: 8px; list-style: none; padding: 8px; max-height: 180px; overflow-y: auto; border: 1px solid var(--border-color); border-radius: 8px; background: var(--card-bg); }
        #pdf-list li { padding: 10px; background: var(--bg-color); margin-bottom: 6px; border-radius: 6px; border: 1px solid var(--border-color); display: flex; align-items: center; gap: 10px; }
        #pdf-list li:last-child { margin-bottom: 0; }
        p { color: var(--text-color); }
    </style>
    </head>
    <body>
        {{ tabs|safe }}
        <div class="container">
            <div class="nav-header">
                <h2>Step 1: LaTeX Project Validator</h2>
                <div class="theme-switch-btn" onclick="toggleTheme()" title="Toggle Dark/Light Mode">
                    <div class="knob"></div>
                    <div class="icon sun">☀️</div>
                    <div class="icon moon">🌙</div>
                </div>
            </div>
            <div style="margin-bottom:12px; width: max-content;">
                <label for="engine_select" style="font-size:13px; font-weight:600; color:var(--meta-color);">Compilation Engine:</label>
                <select id="engine_select" style="margin-top:4px; padding:6px 10px;">
                    <option value="pdflatex">pdflatex</option>
                    <option value="lualatex">lualatex</option>
                    <option value="xelatex">xelatex</option>
                </select>
            </div>
            <p>Paste your project folder path below:</p>
            <input type="text" id="folder_path" placeholder="e.g., C:\itools\WMS\WorkingFolder" autocomplete="off">
            <button type="button" id="submit-btn">Run Validation & Generate Report</button>
            <div id="loading-container"></div>

            <form id="valForm">
                <input type="hidden" id="base_dir" name="base_dir">
                <input type="hidden" id="main_tex_val" name="main_tex">
                <ul id="tex-list"></ul>
                <div id="pdf-section" style="display:none; margin-top:16px;">
                    <div style="font-size:13px; font-weight:600; color:var(--meta-color);">Manuscript (MS) PDF not found. Select the PDF to compare against:</div>
                    <ul id="pdf-list"></ul>
                </div>
            </form>
        </div>
        <script>
            let runLock = false;
            let autoSelectedCancelled = false;
            const pathInput = document.getElementById('folder_path');
            const baseDirField = document.getElementById('base_dir');
            const mainTexField = document.getElementById('main_tex_val');
            const texList = document.getElementById('tex-list');
            const submitBtn = document.getElementById('submit-btn');
            const loadingContainer = document.getElementById('loading-container');
            const valForm = document.getElementById('valForm');
            const engineSelect = document.getElementById('engine_select');

            window.addEventListener('DOMContentLoaded', () => {
                pathInput.focus();
            });

            function toggleTheme() {
                const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
                const nextTheme = isDark ? 'light' : 'dark';
                document.documentElement.setAttribute('data-theme', nextTheme);
                localStorage.setItem('latex_theme', nextTheme);
            }

            function formatTime(sec) {
                const m = Math.floor(sec / 60);
                const s = sec % 60;
                return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
            }

            async function getMsCheck(folderPath, texFile) {
                try {
                    const r = await fetch('/check_ms_pdf', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ base_dir: folderPath, main_tex: texFile }) });
                    return await r.json();
                } catch (e) { return { needs_choice: false, pdfs: [] }; }
            }
            function hidePdfSection() {
                document.getElementById('pdf-section').style.display = 'none';
                document.getElementById('pdf-list').innerHTML = '';
            }
            function showPdfList(pdfs) {
                const list = document.getElementById('pdf-list');
                list.innerHTML = '';
                pdfs.forEach(name => {
                    const li = document.createElement('li');
                    li.innerHTML = `<label style="cursor:pointer; width:100%; display:flex; align-items:center; gap:10px;"><input type="radio" name="ms_pdf" value="${name}"> <code>${name}</code></label>`;
                    list.appendChild(li);
                });
                const skip = document.createElement('li');
                skip.innerHTML = `<label style="cursor:pointer; width:100%; display:flex; align-items:center; gap:10px;"><input type="radio" name="ms_pdf" value="__skip__"> <span style="color:#6b7280;">Skip PDF comparison</span></label>`;
                list.appendChild(skip);
                document.getElementById('pdf-section').style.display = 'block';
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
                        let msChk = { needs_choice: false, pdfs: [] };
                        if (docMatches.length === 1) msChk = await getMsCheck(folderPath, docMatches[0]);
                        hidePdfSection();

                        if (!autoSelectedCancelled && docMatches.length === 1 && !msChk.needs_choice) {
                            const detectedFile = docMatches[0];
                            if (runLock) return;
                            runLock = true;
                            mainTexField.value = detectedFile;
                            
                            valForm.style.display = 'none';
                            pathInput.style.display = 'none';
                            document.querySelector('p').style.display = 'none';
                            
                            loadingContainer.innerHTML = `
                                <div style="font-size:15px; font-weight:600; color:var(--text-color); margin-bottom:8px;">
                                    Detected main TeX file: <code style="color:#2563eb;">${detectedFile}</code>
                                </div>
                                <div id="countdown-text-container" style="font-size:13px; color:var(--meta-color); margin-bottom:14px;">Running validation, please wait... (<span id="elapsed-sec">00:00</span>)</div>
                                <div style="width:100%; background:var(--border-color); height:6px; border-radius:3px; overflow:hidden; margin-bottom:12px;">
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
                                
                                const eventSource = new EventSource(`/stream_validate?base_dir=${encodeURIComponent(folderPath)}&main_tex=${encodeURIComponent(detectedFile)}&engine=${encodeURIComponent(engineSelect.value)}`);
                                const stepsDiv = document.getElementById('live-steps-container');
                                let stepsTable = document.createElement('table');
                                stepsTable.className = 'steps-table';
                                stepsTable.innerHTML = `<thead><tr><th>Pipeline Stage</th><th>Status</th><th>Started In</th><th>Duration (MM:SS)</th></tr></thead><tbody id="steps-tbody"></tbody>`;
                                stepsDiv.appendChild(stepsTable);
                                const tbody = document.getElementById('steps-tbody');

                                eventSource.onmessage = function(event) {
                                    const data = JSON.parse(event.data);
                                    if (data.status === 'finished') {
                                        isFinished = true;
                                        clearInterval(timerInterval);
                                        eventSource.close();
                                        runLock = false;
                                        window.location.href = data.report_url;
                                    } else if (data.status === 'engine') {
                                        engineSelect.value = data.engine;
                                    } else if (data.status === 'error') {
                                        isFinished = true;
                                        clearInterval(timerInterval);
                                        eventSource.close();
                                        runLock = false;
                                        alert(data.message);
                                        window.location.reload();
                                    } else {
                                        let rowId = 'stage-row-' + data.stage_index;
                                        let row = document.getElementById(rowId);
                                        let statusHtml = data.status.startsWith('Failed') ?
                                            `<span style="color:#dc2626; font-weight:600;">✖ ${data.status}</span>` :
                                            (data.status === 'Completed' || data.status.includes('Processed') ?
                                            `<span style="color:#059669; font-weight:600;">✔ ${data.status}</span>` :
                                            `<span style="color:#d97706; font-weight:600;">⏳ ${data.status}</span>`);
                                        
                                        if (!row) {
                                            row = document.createElement('tr');
                                            row.id = rowId;
                                            const now = new Date();
                                            const startedAt = [now.getHours(), now.getMinutes(), now.getSeconds()].map(n => String(n).padStart(2, '0')).join(':');
                                            row.innerHTML = `<td>${data.step}</td><td id="status-${data.stage_index}">${statusHtml}</td><td id="started-${data.stage_index}">${startedAt}</td><td id="time-${data.stage_index}">${data.time}</td>`;
                                            tbody.appendChild(row);
                                        } else {
                                            document.getElementById('status-' + data.stage_index).innerHTML = statusHtml;
                                            document.getElementById('time-' + data.stage_index).innerText = data.time;
                                        }
                                    }
                                };
                                eventSource.onerror = function() {
                                    if (isFinished) return;
                                    isFinished = true;
                                    clearInterval(timerInterval);
                                    eventSource.close();
                                    runLock = false;
                                    alert('Connection to the validator was lost before the run finished. Check the last stage shown in the table.');
                                };
                            }, 3000);

                            document.getElementById('cancel-auto-btn').onclick = () => {
                                runLock = false;
                                autoSelectedCancelled = true;
                                clearTimeout(timeoutId);
                                clearInterval(timerInterval);
                                loadingContainer.style.display = 'none';
                                valForm.style.display = 'block';
                                pathInput.style.display = 'block';
                                document.querySelector('p').style.display = 'block';
                                fetchTexFiles();
                                pathInput.focus();
                            };
                            return;
                        }

                        loadingContainer.style.display = 'none';
                        valForm.style.display = 'block';
                        data.files.forEach((file, index) => {
                            const isChecked = file === data.suggested || (data.suggested === null && index === 0);
                            const li = document.createElement('li');
                            li.innerHTML = `<label style="cursor:pointer; width:100%; display:flex; align-items:center; gap:10px;"><input type="radio" name="main_tex" value="${file}" ${isChecked ? 'checked' : ''} required> <code>${file}</code> ${data.documentclass_matches.includes(file) ? '<span style="color:#059669; font-size:12px; font-weight:600;">(documentclass detected)</span>' : ''}</label>`;
                            texList.appendChild(li);
                        });
                        if (msChk.needs_choice) showPdfList(msChk.pdfs);
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
            function runStreamed(folderPath, texFile, msPdf = '') {
                if (runLock) return;
                runLock = true;
                valForm.style.display = 'none';
                pathInput.style.display = 'none';
                document.querySelector('p').style.display = 'none';

                loadingContainer.innerHTML = `
                    <div style="font-size:15px; font-weight:600; color:var(--text-color); margin-bottom:8px;">
                        Selected main TeX file: <code style="color:#2563eb;">${texFile}</code>
                    </div>
                    <div style="font-size:13px; color:var(--meta-color); margin-bottom:14px;">Running validation, please wait... (<span id="elapsed-sec">00:00</span>)</div>
                    <div style="width:100%; background:var(--border-color); height:6px; border-radius:3px; overflow:hidden; margin-bottom:12px;">
                        <div style="width:100%; background:#2563eb; height:100%;"></div>
                    </div>
                    <div id="live-steps-container" style="text-align:left;"></div>
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

                const eventSource = new EventSource(`/stream_validate?base_dir=${encodeURIComponent(folderPath)}&main_tex=${encodeURIComponent(texFile)}&engine=${encodeURIComponent(engineSelect.value)}&ms_pdf=${encodeURIComponent(msPdf)}`);
                const stepsDiv = document.getElementById('live-steps-container');
                const stepsTable = document.createElement('table');
                stepsTable.className = 'steps-table';
                stepsTable.innerHTML = `<thead><tr><th>Pipeline Stage</th><th>Status</th><th>Started In</th><th>Duration (MM:SS)</th></tr></thead><tbody id="steps-tbody"></tbody>`;
                stepsDiv.appendChild(stepsTable);
                const tbody = document.getElementById('steps-tbody');

                eventSource.onmessage = function(event) {
                    const data = JSON.parse(event.data);
                    if (data.status === 'finished') {
                        isFinished = true;
                        clearInterval(timerInterval);
                        eventSource.close();
                        runLock = false;
                        window.location.href = data.report_url;
                    } else if (data.status === 'engine') {
                        engineSelect.value = data.engine;
                    } else if (data.status === 'error') {
                        isFinished = true;
                        clearInterval(timerInterval);
                        eventSource.close();
                        runLock = false;
                        alert(data.message);
                        window.location.reload();
                    } else {
                        let rowId = 'stage-row-' + data.stage_index;
                        let row = document.getElementById(rowId);
                        let statusHtml = data.status.startsWith('Failed') ?
                            `<span style="color:#dc2626; font-weight:600;">✖ ${data.status}</span>` :
                            (data.status === 'Completed' || data.status.includes('Processed') ?
                            `<span style="color:#059669; font-weight:600;">✔ ${data.status}</span>` :
                            `<span style="color:#d97706; font-weight:600;">⏳ ${data.status}</span>`);
                        if (!row) {
                            row = document.createElement('tr');
                            row.id = rowId;
                            const now = new Date();
                            const startedAt = [now.getHours(), now.getMinutes(), now.getSeconds()].map(n => String(n).padStart(2, '0')).join(':');
                            row.innerHTML = `<td>${data.step}</td><td id="status-${data.stage_index}">${statusHtml}</td><td id="started-${data.stage_index}">${startedAt}</td><td id="time-${data.stage_index}">${data.time}</td>`;
                            tbody.appendChild(row);
                        } else {
                            document.getElementById('status-' + data.stage_index).innerHTML = statusHtml;
                            document.getElementById('time-' + data.stage_index).innerText = data.time;
                        }
                    }
                };
                eventSource.onerror = function() {
                    if (isFinished) return;
                    isFinished = true;
                    clearInterval(timerInterval);
                    eventSource.close();
                    runLock = false;
                    alert('Connection to the validator was lost before the run finished. Check the last stage shown in the table.');
                };
            }
            pathInput.addEventListener('input', () => { autoSelectedCancelled = false; fetchTexFiles(); });
            pathInput.addEventListener('paste', () => setTimeout(() => { autoSelectedCancelled = false; fetchTexFiles(); }, 50));
            texList.addEventListener('change', hidePdfSection);
            submitBtn.addEventListener('click', async () => {
                const selected = document.querySelector('input[name="main_tex"]:checked');
                if (!selected) return;
                mainTexField.value = selected.value;
                const folderPath = pathInput.value.trim();
                let msPdf = '';
                if (document.getElementById('pdf-section').style.display === 'block') {
                    const ms = document.querySelector('input[name="ms_pdf"]:checked');
                    if (!ms) { alert('Please select the MS PDF to compare with (or choose Skip).'); return; }
                    msPdf = ms.value;
                } else {
                    const chk = await getMsCheck(folderPath, selected.value);
                    if (chk.needs_choice) { showPdfList(chk.pdfs); return; }
                }
                runStreamed(folderPath, selected.value, msPdf);
            });
        </script>
    </body>
    </html>
    ''', tabs_css=TABS_CSS, tabs=tabs_html('compile', bleed=True))

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
                        if re.search(r'^[ \t]*\\documentclass', content, re.MULTILINE):
                            documentclass_matches.append(rel)
                except Exception: pass

    suggested = documentclass_matches[0] if documentclass_matches else (tex_files[0] if tex_files else None)
    return jsonify({'files': tex_files, 'suggested': suggested, 'documentclass_matches': documentclass_matches})

@app.route('/check_ms_pdf', methods=['POST'])
def check_ms_pdf():
    data = request.get_json(silent=True) or {}
    tex_path = os.path.abspath(os.path.join(str(data.get('base_dir', '')).strip(), str(data.get('main_tex', '')).strip()))
    d = os.path.dirname(tex_path)
    base = os.path.splitext(os.path.basename(tex_path))[0]
    if os.path.exists(os.path.join(d, base + '.pdf')) or os.path.exists(os.path.join(d, base + '_MS.pdf')):
        return jsonify({'needs_choice': False, 'pdfs': []})
    pdfs = sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, '*.pdf')) if not os.path.basename(p).startswith('diff_report_'))
    auto = [p for p in pdfs if not p.endswith('_MS.pdf')]
    return jsonify({'needs_choice': bool(pdfs) and len(auto) != 1, 'pdfs': pdfs})

@app.route('/stream_validate', methods=['GET'])
def stream_validate():
    base_dir = request.args.get('base_dir', '').strip()
    main_tex_rel = request.args.get('main_tex', '').strip()
    engine = request.args.get('engine', 'auto').strip()
    ms_pdf = request.args.get('ms_pdf', '').strip()
    validator = TexValidatorStreamer(os.path.join(base_dir, main_tex_rel), engine, ms_pdf)
    return Response(validator.run_pipeline(), mimetype='text/event-stream')

@app.route('/validate_manual', methods=['POST'])
def validate_manual():
    base_dir = request.form.get('base_dir', '').strip()
    main_tex_rel = request.form.get('main_tex', '').strip()
    validator = TexValidatorStreamer(os.path.join(base_dir, main_tex_rel))
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
            if filename.lower().endswith('.log'):
                with open(abs_path, 'r', encoding='utf-8', errors='replace') as lf:
                    return Response(lf.read(), mimetype='text/plain')
            return send_from_directory(directory, filename)
    return abort(404)


# ==========================================
# STEP 2: DOCUMENTCLASS SWAP + COMMENT PACKAGES + XELATEX x2
# ==========================================
_STEP2_RUNNING = set()
_STEP2_LOCK = threading.Lock()

LATEX_REGISTRY = {
    "amsmath": {"commands": ["\\text", "\\numberwithin", "\\DeclareMathOperator", "\\eqref", "\\dots", "\\cfrac", "\\substack", "\\binom"], "environments": ["equation*", "align", "align*", "gather", "gather*", "multline", "multline*", "pmatrix", "bmatrix", "vmatrix", "cases", "split", "subarray"], "global": False},
    "amssymb": {"commands": ["\\mathbb", "\\mathcal", "\\mathfrak", "\\varnothing", "\\square", "\\checkmark", "\\angle", "\\nexists", "\\nsubseteq", "\\approxeq", "\\boxdot", "\\boxplus", "\\boxtimes", "\\complement", "\\doteqdot"], "environments": [], "global": False},
    "amsthm": {"commands": ["\\newtheorem", "\\theoremstyle", "\\theorembodyfont", "\\theoremheaderfont", "\\qedhere"], "environments": ["proof"], "global": False},
    "mathtools": {"commands": ["\\coloneqq", "\\eqqcolon", "\\DeclarePairedDelimiter", "\\mathclap", "\\mathllap", "\\mathrlap", "\\cramped", "\\smashoperator", "\\adjustlimits"], "environments": ["matrix*", "pmatrix*", "bmatrix*", "vmatrix*", "spreadlines", "dcases", "rcases"], "global": False},
    "bm": {"commands": ["\\bm", "\\hm"], "environments": [], "global": False},
    "siunitx": {"commands": ["\\num", "\\unit", "\\qty", "\\sisetup", "\\SI", "\\si", "\\ang", "\\numlist", "\\qtylist", "\\tablenum"], "environments": [], "global": False},
    "cancel": {"commands": ["\\cancel", "\\bcancel", "\\xcancel", "\\cancelto"], "environments": [], "global": False},
    "diffcoeff": {"commands": ["\\diff", "\\diffd", "\\negthinspace"], "environments": [], "global": False},
    "extarrows": {"commands": ["\\xlongequal", "\\xleftrightarrow", "\\xLongleftarrow", "\\xLonglongrightarrow"], "environments": [], "global": False},
    "mleftright": {"commands": ["\\mleft", "\\mright"], "environments": [], "global": False},
    "yhmath": {"commands": ["\\wideparen", "\\wideamalg", "\\widering"], "environments": [], "global": False},
    "mhchem": {"commands": ["\\ce", "\\cf", "\\cee", "\\sfrac"], "environments": [], "global": False},
    "chemfig": {"commands": ["\\chemfig", "\\definesubmol", "\\polymerhtml", "\\charge", "\\Lewis"], "environments": [], "global": False},
    "physics": {"commands": ["\\dv", "\\pdv", "\\grad", "\\div", "\\rot", "\\bra", "\\ket", "\\braket", "\\innerprod", "\\matrixel", "\\comm", "\\acomm", "\\abs", "\\norm", "\\eval", "\\tr", "\\rank"], "environments": [], "global": False},
    "tikz-feynman": {"commands": ["\\feynman", "\\diagram"], "environments": ["feynman"], "global": False},
    "chemmacros": {"commands": ["\\ch", "\\chemsetup", "\\iupac", "\\state"], "environments": [], "global": False},
    "modiagram": {"commands": ["\\MOdiagram", "\\energy", "\\molecule"], "environments": ["AO"], "global": False},
    "graphicx": {"commands": ["\\includegraphics", "\\graphicspath", "\\rotatebox", "\\resizebox", "\\scalebox", "\\DeclareGraphicsExtensions"], "environments": [], "global": False},
    "tikz": {"commands": ["\\tikz", "\\usetikzlibrary", "\\node", "\\draw", "\\path", "\\fill", "\\coordinates", "\\clip", "\\shadedraw"], "environments": ["tikzpicture", "scope"], "global": False},
    "pgfplots": {"commands": ["\\addplot", "\\pgfplotsset", "\\addlegendentry", "\\addlegendimage"], "environments": ["axis", "groupplot", "pgfplotsstabletypeset"], "global": False},
    "wrapfig": {"commands": ["\\wrapfigure", "\\wraptable"], "environments": ["wrapfigure", "wraptable"], "global": False},
    "subcaption": {"commands": ["\\subcaption", "\\subcaptionbox", "\\nextsubfigure"], "environments": ["subfigure", "subtable"], "global": False},
    "float": {"commands": ["\\restylefloat", "\\floatstyle", "\\newfloat", "\\floatname"], "environments": [], "global": False},
    "caption": {"commands": ["\\captionof", "\\captionsetup", "\\DeclareCaptionLabelFormat"], "environments": [], "global": False},
    "pdfpages": {"commands": ["\\includepdf", "\\includepdfset"], "environments": [], "global": False},
    "tcolorbox": {"commands": ["\\tcbox", "\\newtcolorbox", "\\tcbset"], "environments": ["tcolorbox"], "global": False},
    "eso-pic": {"commands": ["\\AddToShipoutPicture", "\\AddToShipoutPictureBG", "\\ClearShipoutPicture"], "environments": [], "global": True},
    "transparent": {"commands": ["\\transparent"], "environments": [], "global": False},
    "adjustbox": {"commands": ["\\adjustbox", "\\newadjustbox"], "environments": ["adjustbox"], "global": False},
    "booktabs": {"commands": ["\\toprule", "\\midrule", "\\bottomrule", "\\cmidrule", "\\addlinespace", "\\morecmidrule"], "environments": [], "global": False},
    "tabularx": {"commands": ["\\tabularx"], "environments": ["tabularx"], "global": False},
    "array": {"commands": ["\\newcolumntype", "\\firsthline", "\\lasthline"], "environments": [], "global": False},
    "longtable": {"commands": ["\\endfirsthead", "\\endhead", "\\endfoot", "\\endlastfoot"], "environments": ["longtable"], "global": False},
    "multirow": {"commands": ["\\multirow", "\\multicolumn", "\\bigstrut"], "environments": [], "global": False},
    "enumitem": {"commands": ["\\setlist", "\\newlist", "\\setlistdepth"], "environments": [], "global": False},
    "colortbl": {"commands": ["\\rowcolor", "\\columncolor", "\\cellcolor"], "environments": [], "global": False},
    "tabu": {"commands": ["\\tabu", "\\indextabu"], "environments": ["tabu", "longtabu"], "global": False},
    "datatool": {"commands": ["\\dtlloadrawdb", "\\DTLforeach", "\\dtlcolumncount"], "environments": [], "global": False},
    "ltablex": {"commands": [], "environments": ["tabularx"], "global": True},
    "diagbox": {"commands": ["\\diagbox", "\\line"], "environments": [], "global": False},
    "listings": {"commands": ["\\lstset", "\\lstinline", "\\lstinputlisting", "\\lstdefinestyle"], "environments": ["lstlisting"], "global": False},
    "minted": {"commands": ["\\mint", "\\mintinline", "\\usemintedstyle", "\\newminted", "\\inputminted"], "environments": ["minted"], "global": False},
    "algorithm2e": {"commands": ["\\KwIn", "\\KwOut", "\\If", "\\While", "\\SetAlgoLined", "\\DontPrintSemicolon", "\\Titleofalgo"], "environments": ["algorithm", "procedure", "function"], "global": False},
    "algorithmicx": {"commands": ["\\State", "\\For", "\\If", "\\While", "\\ElsIf", "\\EndFor"], "environments": ["algorithmic"], "global": False},
    "algpseudocode": {"commands": ["\\Procedure", "\\Method", "\\While", "\\For", "\\If"], "environments": ["algorithmic"], "global": False},
    "fancyvrb": {"commands": ["\\DefineShortVerb", "\\UndefineShortVerb", "\\fvset"], "environments": ["Verbatim"], "global": False},
    "geometry": {"commands": ["\\geometry", "\\newgeometry", "\\restoregeometry"], "environments": [], "global": True},
    "microtype": {"commands": ["\\microtypesetup", "\\DeclareMicrotypeSet"], "environments": [], "global": True},
    "babel": {"commands": ["\\selectlanguage", "\\iflanguage", "\\babeltags"], "environments": [], "global": True},
    "polyglossia": {"commands": ["\\setdefaultlanguage", "\\setotherlanguage", "\\setlanguage"], "environments": [], "global": True},
    "csquotes": {"commands": ["\\enquote", "\\textquote", "\\foreignquote"], "environments": ["displayquote"], "global": False},
    "setspace": {"commands": ["\\doublespacing", "\\onehalfspacing", "\\singlespacing"], "environments": ["spacing"], "global": False},
    "fancyhdr": {"commands": ["\\fancyhead", "\\fancyfoot", "\\pagestyle", "\\fancypagestyle", "\\fancyhf"], "environments": [], "global": True},
    "titlesec": {"commands": ["\\titleformat", "\\titlespacing", "\\titleclass"], "environments": [], "global": True},
    "parskip": {"commands": [], "environments": [], "global": True},
    "indentfirst": {"commands": [], "environments": [], "global": True},
    "footmisc": {"commands": ["\\footnote", "\\footnotemark", "\\footnotetext"], "environments": [], "global": True},
    "chngcntr": {"commands": ["\\counterwithin", "\\counterwithout"], "environments": [], "global": True},
    "natbib": {"commands": ["\\citep", "\\citet", "\\citealp", "\\citealt", "\\citeauthor", "\\citeyear", "\\bibliographystyle"], "environments": [], "global": False},
    "biblatex": {"commands": ["\\addbibresource", "\\printbibliography", "\\autocite", "\\citefield"], "environments": [], "global": False},
    "cite": {"commands": ["\\cite", "\\nocite"], "environments": [], "global": False},
    "qtree": {"commands": ["\\Tree"], "environments": [], "global": False},
    "forest": {"commands": ["\\forestset"], "environments": ["forest"], "global": False},
    "blindtext": {"commands": ["\\blindtext", "\\Blindtext", "\\blinddocument"], "environments": [], "global": False},
    "lipsum": {"commands": ["\\lipsum"], "environments": [], "global": False}
}

PKG_RE = re.compile(r'\\(usepackage|RequirePackage)(?:\s*\[(.*?)\])?\s*\{(.*?)\}', re.S)

def _in_comment(text, pos):
    ls = text.rfind('\n', 0, pos) + 1
    return re.search(r'(?<!\\)%', text[ls:pos]) is not None

def resolve_tex_content(file_path, base_dir, visited=None):
    if visited is None:
        visited = set()
    path = os.path.normpath(os.path.abspath(file_path))
    if not os.path.isfile(path) and os.path.isfile(path + '.tex'):
        path += '.tex'
    if path in visited or not os.path.isfile(path):
        return ""
    visited.add(path)
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except Exception:
        return ""
    active = "\n".join(re.sub(r'(?<!\\)%.*$', '', ln) for ln in content.splitlines())
    parts = [content]
    for m in re.finditer(r'\\(?:input|include)\s*\{([^}]+)\}', active):
        parts.append(resolve_tex_content(os.path.join(base_dir, m.group(1).strip()), base_dir, visited))
    return "\n".join(parts)

def find_unused_packages(main_text, analysis_text):
    declared = []
    for m in PKG_RE.finditer(main_text):
        if _in_comment(main_text, m.start()):
            continue
        declared.extend(p.strip() for p in m.group(3).split(',') if p.strip())
    declared = list(dict.fromkeys(declared))

    active = "\n".join(re.sub(r'(?<!\\)%.*$', '', ln) for ln in analysis_text.splitlines())
    found_commands = set(re.findall(r'\\[a-zA-Z]+', active))
    found_environments = set(re.findall(r'\\begin\s*\{(.*?)\}', active))

    remove = []
    for pkg in declared:
        rules = LATEX_REGISTRY.get(pkg)
        if not rules or rules.get("global", False):
            continue
        cmds = rules.get("commands") or []
        envs = rules.get("environments") or []
        if not any(c in found_commands for c in cmds) and not any(e in found_environments for e in envs):
            remove.append(pkg)
    return remove

def remove_unused_packages(text, remove_packages):
    remove_set = set(remove_packages)
    eol = '\r\n' if '\r\n' in text else '\n'
    removed = []

    def repl(m):
        if _in_comment(text, m.start()):
            return m.group(0)
        pkgs = [p.strip() for p in m.group(3).split(',') if p.strip()]
        keep = [p for p in pkgs if p not in remove_set]
        drop = [p for p in pkgs if p in remove_set]
        if not drop:
            return m.group(0)
        removed.extend(drop)
        if keep:
            opts = "[" + m.group(2) + "]" if m.group(2) else ""
            return "\\" + m.group(1) + opts + "{" + ", ".join(keep) + "}"
        ls = text.rfind('\n', 0, m.start()) + 1
        le = text.find('\n', m.end())
        le = len(text) if le == -1 else le
        before, after = text[ls:m.start()], text[m.end():le]
        commented = '\n'.join('%' + ln for ln in m.group(0).split('\n'))
        return (eol if before.strip() else '') + commented + (eol if after.strip() else '')

    new_text = PKG_RE.sub(repl, text)
    return new_text, list(dict.fromkeys(removed))

def get_all_linked_tex_files(file_path, base_dir, found_paths=None):
    if found_paths is None:
        found_paths = set()
    path = os.path.normpath(os.path.abspath(file_path))
    if not os.path.isfile(path) and os.path.isfile(path + '.tex'):
        path += '.tex'
    if path in found_paths or not os.path.isfile(path):
        return found_paths
    found_paths.add(path)
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except Exception:
        return found_paths
        
    active = "\n".join(re.sub(r'(?<!\\)%.*$', '', ln) for ln in content.splitlines())
    for m in re.finditer(r'\\(?:input|include)\s*\{([^}]+)\}', active):
        target_name = m.group(1).strip()
        target_path = os.path.join(base_dir, target_name)
        get_all_linked_tex_files(target_path, base_dir, found_paths)
    return found_paths

def comment_specific_packages_and_fonts(text):
    blacklist_pkgs = {"amsmath", "amssymb", "amsthm", "amsfonts", "stmaryrd", "latexsym", "mathabx", "txfonts"}
    
    def pkg_replacer(m):
        if _in_comment(text, m.start()):
            return m.group(0)
        pkgs = [p.strip() for p in m.group(3).split(',') if p.strip()]
        keep = [p for p in pkgs if p not in blacklist_pkgs]
        if not keep:
            return '\n'.join('%' + ln for ln in m.group(0).split('\n'))
        else:
            opts = "[" + m.group(2) + "]" if m.group(2) else ""
            return "\\" + m.group(1) + opts + "{" + ", ".join(keep) + "}"

    processed_text = PKG_RE.sub(pkg_replacer, text)

    font_commands = ["setmainfont", "setromanfont", "setsansfont", "setmonofont"]
    for cmd in font_commands:
        pattern = r'(?<!\\)\\' + cmd + r'\b(?:\s*\[.*?\])?\s*\{.*?\}'
        def font_replacer(m):
            if _in_comment(processed_text, m.start()):
                return m.group(0)
            return '\n'.join('%' + ln for ln in m.group(0).split('\n'))
        processed_text = re.sub(pattern, font_replacer, processed_text, flags=re.S)

    return processed_text

def insert_make_chapter_title(text):
    eol = '\r\n' if '\r\n' in text else '\n'
    pattern = r'(?<!\\)\\chapter\s*(?:\[[^\]]*\])?\s*\{'
    lines = text.splitlines(keepends=True)
    modified_lines = []

    for line in lines:
        modified_lines.append(line)
        clean_code = re.sub(r'(?<!\\)%.*$', '', line)
        if re.search(pattern, clean_code):
            indentation = re.match(r'^\s*', line).group(0)
            modified_lines.append(f"{indentation}\\makechaptertitle{eol}")

    return "".join(modified_lines)

def step2_transform(text, new_class_line):
    out, class_done, pending_drop = [], False, 0
    for line in re.split(r'(?<=\n)', text):
        body = line.rstrip('\r\n')
        eol = line[len(body):]
        code = re.sub(r'(?<!\\)%.*$', '', body)
        depth = code.count('[') + code.count('{') - code.count(']') - code.count('}')
        if pending_drop > 0:
            pending_drop = max(0, pending_drop + depth)
            continue
        if not class_done and re.match(r'\s*\\documentclass\b', code):
            out.append(new_class_line + (eol or '\n'))
            class_done = True
            pending_drop = max(0, depth)
            continue
        out.append(line)
    return ''.join(out), class_done

def step2_pipeline(file_path, new_class_line):
    def ev(idx, step, status, tm):
        return f"data: {json.dumps({'stage_index': idx, 'step': step, 'status': status, 'time': tm})}\n\n"
    def err(msg):
        return f"data: {json.dumps({'status': 'error', 'message': msg})}\n\n"

    t_start = time.time()
    file_path = os.path.abspath(file_path)
    dir_name = os.path.dirname(file_path)
    base = os.path.splitext(os.path.basename(file_path))[0]
    log_path = os.path.join(dir_name, base + '.log')
    pdf_path = os.path.join(dir_name, base + '.pdf')

    if not os.path.exists(file_path):
        yield err('Source file not found.')
        return

    step1 = '1. Process layout configurations across project files'
    yield ev(0, step1, 'Running...', '...')
    t0 = time.time()

    try:
        all_project_files = get_all_linked_tex_files(file_path, dir_name)

        for current_file in all_project_files:
            bak = current_file + '.bak'
            if not os.path.exists(bak):
                shutil.copy2(current_file, bak)

            with open(bak, 'rb') as f:
                raw = f.read()
            try:
                text, enc = raw.decode('utf-8'), 'utf-8'
            except UnicodeDecodeError:
                text, enc = raw.decode('latin-1'), 'latin-1'

            if os.path.normpath(current_file) == os.path.normpath(file_path):
                text, class_done = step2_transform(text, new_class_line)
                if not class_done:
                    yield err('No uncommented \\documentclass line found in root file.')
                    return
                unused = find_unused_packages(text, resolve_tex_content(bak, dir_name))
                text, removed = remove_unused_packages(text, unused)

            text = comment_specific_packages_and_fonts(text)
            text = insert_make_chapter_title(text)

            with open(current_file, 'wb') as f:
                f.write(text.encode(enc))

    except Exception as e:
        yield err(f'Could not modify the project files: {type(e).__name__}: {e}')
        return

    def fmt_secs(s):
        s = int(round(s))
        return f"{s // 60:02d}:{s % 60:02d}"

    yield ev(0, step1, 'Completed - Package restrictions, Font filters and Chapter title setups applied.', fmt_secs(time.time() - t0))

    xelatex_exe = r"\\integra-net\TeXLive\texlive\2019\bin\win32\xelatex.exe"
    if not os.path.exists(xelatex_exe):
        xelatex_exe = shutil.which("xelatex") or xelatex_exe
    pdf_before = os.path.getmtime(pdf_path) if os.path.exists(pdf_path) else None

    for n in (1, 2):
        step = f'{n + 1}. xelatex Compilation Pass {n}'
        yield ev(n, step, 'Running...', '...')
        t0 = time.time()
        try:
            p = subprocess.Popen([xelatex_exe, '-interaction=nonstopmode', file_path],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 cwd=dir_name, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except Exception as e:
            yield ev(n, step, f'Failed: cannot start xelatex ({type(e).__name__}: {e})', '-')
            yield err('xelatex could not be started.')
            return
        while p.poll() is None:
            time.sleep(2)
            yield ": keepalive\n\n"
        yield ev(n, step, 'Completed', fmt_secs(time.time() - t0))

    errs = []
    if os.path.exists(log_path):
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            errs = [m.strip() for m in re.findall(r'^!(.*)$', f.read(), re.M)]
    pdf_ok = os.path.exists(pdf_path) and (pdf_before is None or os.path.getmtime(pdf_path) != pdf_before)
    yield f"data: {json.dumps({'status': 'finished', 'pdf_ok': pdf_ok, 'pdf_name': base + '.pdf', 'pdf_path': pdf_path, 'log_path': log_path, 'error_count': len(errs), 'errors': errs[:10], 'elapsed': round(time.time() - t_start, 1)})}\n\n"

def step2_guarded(file_path, new_class_line):
    key = os.path.normcase(os.path.abspath(file_path))
    with _STEP2_LOCK:
        busy = key in _STEP2_RUNNING
        if not busy:
            _STEP2_RUNNING.add(key)
    if busy:
        yield f"data: {json.dumps({'status': 'error', 'message': 'This file is already being processed. Wait for it to finish.'})}\n\n"
        return
    try:
        yield from step2_pipeline(file_path, new_class_line)
    finally:
        with _STEP2_LOCK:
            _STEP2_RUNNING.discard(key)

@app.route('/stream_step2', methods=['GET'])
def stream_step2():
    base_dir = request.args.get('base_dir', '').strip()
    main_tex = request.args.get('main_tex', '').strip()
    class_line = request.args.get('class_line', '').strip()
    
    if not re.match(r'\\documentclass\b', class_line) or '\n' in class_line or '\r' in class_line:
        error_data = json.dumps({'status': 'error', 'message': 'The replacement must be a single line starting with \\\\documentclass.'})
        return Response(f"data: {error_data}\n\n", mimetype='text/event-stream')
        
    return Response(step2_guarded(os.path.join(base_dir, main_tex), class_line), mimetype='text/event-stream')

STEP2_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><title>Step 2: Documentclass Swap & XeLaTeX</title>
<script>
    (function() {
        const savedTheme = localStorage.getItem('latex_theme') || 'dark';
        document.documentElement.setAttribute('data-theme', savedTheme);
    })();
</script>
<style>
    :root {
        --bg-color: #000000;
        --text-color: #f3f4f6;
        --card-bg: #121212;
        --border-color: #262626;
        --meta-color: #94a3b8;
        --tabs-bg: #000000;
        --tab-bg: #121212;
        --tab-color: #94a3b8;
        --tab-hover-bg: #222222;
        --tab-active-bg: #000000;
        --tab-active-color: #f3f4f6;
    }
    :root[data-theme="light"] {
        --bg-color: #f4f6f8;
        --text-color: #1f2937;
        --card-bg: #ffffff;
        --border-color: #d1d5db;
        --meta-color: #4b5563;
        --tabs-bg: #cfd5dc;
        --tab-bg: #e4e8ed;
        --tab-color: #4b5563;
        --tab-hover-bg: #eef1f4;
        --tab-active-bg: #f4f6f8;
        --tab-active-color: #1f2937;
    }
    {{ tabs_css|safe }}
    body { font-family: "Segoe UI", Arial, sans-serif; background: var(--bg-color); margin: 0; padding: 40px; color: var(--text-color); transition: background 0.3s, color 0.3s; }
    .container { max-width: 650px; margin: auto; background: var(--card-bg); padding: 35px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,.15); border: 1px solid var(--border-color); }
    .nav-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
    .theme-switch-btn { background: var(--tab-bg); border: 1px solid var(--border-color); border-radius: 30px; width: 68px; height: 34px; display: flex; align-items: center; justify-content: space-between; padding: 0 4px; cursor: pointer; position: relative; transition: background 0.3s; }
    .theme-switch-btn .icon { width: 26px; height: 26px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 14px; z-index: 2; transition: transform 0.3s, background 0.3s; }
    .theme-switch-btn .sun { color: #f59e0b; }
    .theme-switch-btn .moon { color: #94a3b8; }
    :root[data-theme="dark"] .theme-switch-btn .sun { color: #64748b; }
    :root[data-theme="dark"] .theme-switch-btn .moon { color: #38bdf8; }
    .theme-switch-btn .knob { position: absolute; left: 4px; width: 26px; height: 26px; border-radius: 50%; background: #ffffff; box-shadow: 0 2px 5px rgba(0,0,0,0.2); transition: transform 0.3s; z-index: 1; }
    :root[data-theme="dark"] .theme-switch-btn .knob { transform: translateX(34px); background: #262626; }
    h2 { margin-top: 0; color: var(--text-color); }
    label.lbl { display: block; font-size: 13px; font-weight: 600; color: var(--meta-color); margin: 16px 0 6px; }
    input[type="text"] { width: 100%; padding: 12px; border: 1px solid var(--border-color); border-radius: 8px; font-size: 14px; box-sizing: border-box; background: var(--card-bg); color: var(--text-color); }
    input[type="text"].mono { font-family: Consolas, monospace; }
    #tex-list { margin-top: 12px; list-style: none; padding: 8px; max-height: 200px; overflow-y: auto; border: 1px solid var(--border-color); border-radius: 8px; background: var(--card-bg); }
    #tex-list:empty { display: none; }
    #tex-list li { padding: 10px; background: var(--bg-color); margin-bottom: 6px; border-radius: 6px; border: 1px solid var(--border-color); display: flex; align-items: center; gap: 10px; }
    #tex-list li:last-child { margin-bottom: 0; }
    #run-btn { margin-top: 20px; padding: 10px 22px; background: #2563eb; color: white; border: none; border-radius: 8px; cursor: pointer; font-weight: 600; }
    #run-btn:disabled { opacity: .5; cursor: not-allowed; }
    #progress { display: none; margin-top: 20px; }
    table.steps-table { width: 100%; border-collapse: collapse; background: var(--card-bg); border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,.04); text-align: left; border: 1px solid var(--border-color); }
    table.steps-table th, table.steps-table td { padding: 8px 12px; border-bottom: 1px solid var(--border-color); font-size: 13px; color: var(--text-color); }
    #result { display: none; margin-top: 20px; }
    .ok { background: #ecfdf5; color: #047857; padding: 14px; border-radius: 10px; font-weight: 600; }
    .bad { background: #fef2f2; color: #b91c1c; padding: 14px; border-radius: 10px; font-weight: 600; }
    pre.errs { background: var(--bg-color); color: var(--text-color); font-size: 12.5px; padding: 10px 14px; border-radius: 8px; white-space: pre-wrap; margin: 10px 0 0; border: 1px solid var(--border-color); }
    .links { margin-top: 12px; display: flex; gap: 14px; }
    .links a { color: #2563eb; font-weight: 600; font-size: 13px; text-decoration: none; }
    .links a:hover { text-decoration: underline; }
</style>
</head>
<body>
{{ tabs|safe }}
<div class="container">
    <div class="nav-header">
        <h2>Step 2: Documentclass Swap & XeLaTeX</h2>
        <div class="theme-switch-btn" onclick="toggleTheme()" title="Toggle Dark/Light Mode">
            <div class="knob"></div>
            <div class="icon sun">☀️</div>
            <div class="icon moon">🌙</div>
        </div>
    </div>
    <label class="lbl" for="folder_path">Project folder path:</label>
    <input type="text" id="folder_path" placeholder="e.g., C:\itools\WMS\WorkingFolder" autocomplete="off">
    <ul id="tex-list"></ul>
    <label class="lbl" for="class_line">Replacement for the entire \documentclass line:</label>
    <input type="text" id="class_line" class="mono" placeholder="e.g., \documentclass[a4paper,12pt]{book}" autocomplete="off">
    <button type="button" id="run-btn" disabled>Replace &amp; Compile (xelatex x2)</button>
    <div id="progress"></div>
    <div id="result"></div>
</div>
<script>
    const pathInput = document.getElementById('folder_path');
    const classInput = document.getElementById('class_line');
    const texList = document.getElementById('tex-list');
    const runBtn = document.getElementById('run-btn');
    const progress = document.getElementById('progress');
    const resultBox = document.getElementById('result');
    let running = false;
    let fetchSeq = 0;

    window.addEventListener('DOMContentLoaded', () => {
        pathInput.focus();
    });

    function toggleTheme() {
        const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
        const nextTheme = isDark ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', nextTheme);
        localStorage.setItem('latex_theme', nextTheme);
    }

    function esc(s) {
        return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }
    function updateRunBtn() {
        const sel = document.querySelector('input[name="main_tex"]:checked');
        runBtn.disabled = running || !sel || !classInput.value.trim();
    }

    async function fetchTexFiles() {
        const folder = pathInput.value.trim();
        const seq = ++fetchSeq;
        if (!folder) { texList.innerHTML = ''; updateRunBtn(); return; }
        try {
            const r = await fetch('/get_tex_files', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ folder_path: folder }) });
            const data = await r.json();
            if (seq !== fetchSeq) return;
            texList.innerHTML = '';
            if (!data.files || data.files.length === 0) {
                texList.innerHTML = '<li style="color:#b91c1c;">No .tex files found.</li>';
            } else {
                data.files.forEach((file, index) => {
                    const isChecked = file === data.suggested || (data.suggested === null && index === 0);
                    const li = document.createElement('li');
                    li.innerHTML = `<label style="cursor:pointer; width:100%; display:flex; align-items:center; gap:10px;"><input type="radio" name="main_tex" value="${esc(file)}" ${isChecked ? 'checked' : ''}> <code>${esc(file)}</code> ${data.documentclass_matches.includes(file) ? '<span style="color:#059669; font-size:12px; font-weight:600;">(documentclass detected)</span>' : ''}</label>`;
                    texList.appendChild(li);
                });
            }
        } catch (e) {
            if (seq !== fetchSeq) return;
            texList.innerHTML = '<li style="color:#b91c1c;">Error scanning path.</li>';
        }
        updateRunBtn();
    }

    function statusHtml(status) {
        if (status.startsWith('Failed')) return `<span style="color:#dc2626; font-weight:600;">✖ ${esc(status)}</span>`;
        if (status.startsWith('Completed')) return `<span style="color:#059669; font-weight:600;">✔ ${esc(status)}</span>`;
        return `<span style="color:#d97706; font-weight:600;">⏳ ${esc(status)}</span>`;
    }
    function updateRow(data) {
        const tbody = document.getElementById('steps-tbody');
        const rowId = 'stage-row-' + data.stage_index;
        let row = document.getElementById(rowId);
        if (!row) {
            row = document.createElement('tr');
            row.id = rowId;
            const now = new Date();
            const startedAt = [now.getHours(), now.getMinutes(), now.getSeconds()].map(n => String(n).padStart(2, '0')).join(':');
            row.innerHTML = `<td>${esc(data.step)}</td><td id="status-${data.stage_index}">${statusHtml(data.status)}</td><td>${startedAt}</td><td id="time-${data.stage_index}">${esc(data.time)}</td>`;
            tbody.appendChild(row);
        } else {
            document.getElementById('status-' + data.stage_index).innerHTML = statusHtml(data.status);
            document.getElementById('time-' + data.stage_index).innerText = data.time;
        }
    }
    function showError(msg) {
        resultBox.innerHTML = `<div class="bad">✖ ${esc(msg)}</div>`;
        resultBox.style.display = 'block';
    }
    function showResult(d) {
        let html = d.pdf_ok
            ? `<div class="ok">✔ PDF produced: <code>${esc(d.pdf_name)}</code> (${d.elapsed}s)${d.error_count ? ' - the log reports ' + d.error_count + ' error(s)' : ''}</div>`
            : `<div class="bad">✖ No new PDF was produced. See the log for details.</div>`;
        if (d.error_count > 0) html += `<pre class="errs">${esc(d.errors.join('\n'))}${d.error_count > d.errors.length ? '\n...' : ''}</pre>`;
        html += `<div class="links">${d.pdf_ok ? `<a href="/view_file?path=${encodeURIComponent(d.pdf_path)}" target="_blank">Open PDF ↗</a>` : ''}<a href="/view_file?path=${encodeURIComponent(d.log_path)}" target="_blank">Open log ↗</a></div>`;
        resultBox.innerHTML = html;
        resultBox.style.display = 'block';
    }

    function startRun(folder, file, line) {
        running = true;
        updateRunBtn();
        resultBox.style.display = 'none';
        resultBox.innerHTML = '';
        progress.innerHTML = `<table class="steps-table"><thead><tr><th>Pipeline Stage</th><th>Status</th><th>Started In</th><th>Duration (MM:SS)</th></tr></thead><tbody id="steps-tbody"></tbody></table>`;
        progress.style.display = 'block';

        const es = new EventSource(`/stream_step2?base_dir=${encodeURIComponent(folder)}&main_tex=${encodeURIComponent(file)}&class_line=${encodeURIComponent(line)}`);
        let done = false;
        es.onmessage = function(event) {
            const data = JSON.parse(event.data);
            if (data.status === 'finished') {
                done = true; es.close(); running = false; updateRunBtn(); showResult(data);
            } else if (data.status === 'error') {
                done = true; es.close(); running = false; updateRunBtn(); showError(data.message);
            } else {
                updateRow(data);
            }
        };
        es.onerror = function() {
            if (done) return;
            done = true;
            es.close();
            running = false;
            updateRunBtn();
            showError('Connection to the app was lost before the run finished. Check the last stage shown above.');
        };
    }

    pathInput.addEventListener('input', fetchTexFiles);
    classInput.addEventListener('input', updateRunBtn);
    texList.addEventListener('change', updateRunBtn);
    runBtn.addEventListener('click', () => {
        if (running) return;
        const sel = document.querySelector('input[name="main_tex"]:checked');
        const line = classInput.value.trim();
        if (!sel || !line) return;
        if (!/^\\documentclass/.test(line)) { alert('The replacement must start with \\documentclass'); return; }
        if (!confirm('This will modify ' + sel.value + ' (the original is kept as ' + sel.value + '.bak) and compile it with xelatex twice.\n\nContinue?')) return;
        startRun(pathInput.value.trim(), sel.value, line);
    });
</script>
</body>
</html>
"""

@app.route('/step2', methods=['GET'])
def step2_page():
    return render_template_string(STEP2_HTML, tabs_css=TABS_CSS, tabs=tabs_html('castoff', bleed=True))

# ==========================================
# STEP 3: FILE SEQUENCE MANAGER
# ==========================================
STEP3_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Step 3: TeX File Sequence Manager</title>
<script>
    (function() {
        const savedTheme = localStorage.getItem('latex_theme') || 'dark';
        document.documentElement.setAttribute('data-theme', savedTheme);
    })();
</script>
<style>
    :root {
        --bg-color: #000000;
        --text-color: #f3f4f6;
        --card-bg: #121212;
        --border-color: #262626;
        --meta-color: #94a3b8;
        --tabs-bg: #000000;
        --tab-bg: #121212;
        --tab-color: #94a3b8;
        --tab-hover-bg: #222222;
        --tab-active-bg: #000000;
        --tab-active-color: #f3f4f6;
    }
    :root[data-theme="light"] {
        --bg-color: #f4f6f8;
        --text-color: #1f2937;
        --card-bg: #ffffff;
        --border-color: #d1d5db;
        --meta-color: #4b5563;
        --tabs-bg: #cfd5dc;
        --tab-bg: #e4e8ed;
        --tab-color: #4b5563;
        --tab-hover-bg: #eef1f4;
        --tab-active-bg: #f4f6f8;
        --tab-active-color: #1f2937;
    }
    {{ tabs_css|safe }}
    body { font-family: "Segoe UI", Arial, sans-serif; background: var(--bg-color); margin: 0; padding: 40px; color: var(--text-color); transition: background 0.3s, color 0.3s; }
    .container { max-width: 650px; margin: auto; background: var(--card-bg); padding: 35px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,.15); border: 1px solid var(--border-color); }
    .nav-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
    .theme-switch-btn { background: var(--tab-bg); border: 1px solid var(--border-color); border-radius: 30px; width: 68px; height: 34px; display: flex; align-items: center; justify-content: space-between; padding: 0 4px; cursor: pointer; position: relative; transition: background 0.3s; }
    .theme-switch-btn .icon { width: 26px; height: 26px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 14px; z-index: 2; transition: transform 0.3s, background 0.3s; }
    .theme-switch-btn .sun { color: #f59e0b; }
    .theme-switch-btn .moon { color: #94a3b8; }
    :root[data-theme="dark"] .theme-switch-btn .sun { color: #64748b; }
    :root[data-theme="dark"] .theme-switch-btn .moon { color: #38bdf8; }
    .theme-switch-btn .knob { position: absolute; left: 4px; width: 26px; height: 26px; border-radius: 50%; background: #ffffff; box-shadow: 0 2px 5px rgba(0,0,0,0.2); transition: transform 0.3s; z-index: 1; }
    :root[data-theme="dark"] .theme-switch-btn .knob { transform: translateX(34px); background: #262626; }
    h2 { margin-top: 0; color: var(--text-color); }
    label.lbl { display: block; font-size: 13px; font-weight: 600; color: var(--meta-color); margin: 16px 0 6px; }
    input[type="text"] { width: 100%; padding: 12px; border: 1px solid var(--border-color); border-radius: 8px; font-size: 14px; box-sizing: border-box; background: var(--card-bg); color: var(--text-color); }
    #fileList { margin-top: 20px; list-style: none; padding: 8px; max-height: 220px; overflow-y: auto; border: 1px solid var(--border-color); border-radius: 8px; background: var(--card-bg); }
    #fileList.drag-active { border-color: #2563eb; background: var(--bg-color); }
    .drop-placeholder { padding: 20px; color: var(--meta-color); font-size: 13px; text-align: center; }
    .file-item { padding: 10px; background: var(--bg-color); margin-bottom: 6px; border-radius: 6px; border: 1px solid var(--border-color); display: flex; align-items: center; gap: 10px; cursor: grab; }
    .file-item:last-child { margin-bottom: 0; }
    .file-item.dragging { opacity: .4; }
    .drag-handle { width: 20px; color: var(--meta-color); font-size: 16px; user-select: none; }
    .number { width: 30px; color: var(--meta-color); font-family: Consolas, monospace; font-size: 13px; }
    .filename { flex: 1; font-family: Consolas, monospace; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-color); }
    .expected { width: 180px; text-align: left; color: #2563eb; font-size: 12px; font-family: Consolas, monospace; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .ignore-box { width: 70px; margin: 0; display: flex; align-items: center; gap: 6px; color: var(--meta-color); font-size: 12px; cursor: pointer; user-select: none; }
    .ignore-box input { width: 15px; height: 15px; cursor: pointer; }
    .file-item.ignored { opacity: 0.5; background: var(--border-color); }
    .file-item.ignored .filename { text-decoration: line-through; color: var(--meta-color); }
    .file-item.ignored .expected { color: var(--meta-color); }
    .info-panel { margin-top: 20px; background: var(--bg-color); border: 1px solid var(--border-color); border-radius: 8px; padding: 12px; font-size: 12.5px; color: var(--meta-color); line-height: 1.6; }
    .info-panel b { color: var(--text-color); }
    #renameButton { margin-top: 20px; padding: 10px 22px; background: #2563eb; color: white; border: none; border-radius: 8px; cursor: pointer; font-weight: 600; width: 100%; text-align: center; }
    #renameButton:disabled { opacity: .5; cursor: not-allowed; }
    .message { margin-top: 18px; padding: 14px; border-radius: 10px; font-weight: 600; display: none; font-size: 13px; }
    .message.success { display: block; background: #ecfdf5; color: #047857; }
    .message.error { display: block; background: #fef2f2; color: #b91c1c; }
    p { color: var(--text-color); }
</style>
</head>
<body>
    {{ tabs|safe }}
    <div class="container">
        <div class="nav-header">
            <h2>Step 3: TeX File Sequence Manager</h2>
            <div class="theme-switch-btn" onclick="toggleTheme()" title="Toggle Dark/Light Mode">
                <div class="knob"></div>
                <div class="icon sun">☀️</div>
                <div class="icon moon">🌙</div>
            </div>
        </div>
        <p>Paste your project folder path below:</p>
        <input type="text" id="folder" placeholder="e.g., C:\itools\WMS\WorkingFolder" autocomplete="off">
        <label class="lbl" for="prefix">Expected Name / Prefix:</label>
        <input type="text" id="prefix" placeholder="e.g., Saari" autocomplete="off" oninput="updateExpectedNames()">
        <label class="lbl"><span id="count">0 files</span> found inside project directory:</label>
        <div id="fileList" ondragover="allowDrop(event)" ondrop="dropFile(event)" ondragleave="dragLeave(event)">
            <div class="drop-placeholder">Paste your project folder path above to inspect files.</div>
        </div>
        <div class="info-panel">
            First active file maps to: <b>00-Prefix-Prelims.tex</b><br>
            Succeeding active files compile to: <b>01-Prefix-Ch01.tex</b>, <b>02-Prefix-Ch02.tex</b>...
        </div>
        <button type="button" id="renameButton" onclick="renameFiles()" disabled>Rename Files</button>
        <div id="message" class="message"></div>
    </div>
<script>
let files = [];
let draggedIndex = null;
let isProcessing = false;
const $ = id => document.getElementById(id);

function toggleTheme() {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    const nextTheme = isDark ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', nextTheme);
    localStorage.setItem('latex_theme', nextTheme);
}

$("folder").addEventListener("paste", () => setTimeout(() => { isProcessing = true; loadFiles().finally(() => { isProcessing = false; }); }, 50));
$("folder").addEventListener("change", () => { isProcessing = true; loadFiles().finally(() => { isProcessing = false; }); });

function buildName(s, p) { return s === 0 ? "00-"+p+"-Prelims.tex" : String(s).padStart(2,"0")+"-"+p+"-Ch"+String(s).padStart(2,"0")+".tex"; }
async function loadFiles(silent = false) {
    const folder = $("folder").value.trim();
    if (!folder) return;
    try {
        const response = await fetch("/rename_files_list", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ folder: folder }) });
        const data = await response.json();
        if (!data.success) { showMessage(data.message, true); return; }
        files = data.files.map(name => ({ name: name, ignored: false }));
        renderFiles();
        if (!silent) showMessage(files.length + (files.length === 1 ? " TeX file loaded." : " TeX files loaded."), false);
    } catch (error) { showMessage("Unable to connect to application.", true); }
}
function renderFiles() {
    const list = $("fileList"); list.innerHTML = "";
    if (files.length === 0) { list.innerHTML = '<div class="drop-placeholder">No .tex files found.</div>'; $("count").innerText = "0 files"; $("renameButton").disabled = true; return; }
    files.forEach((file, index) => {
        const item = document.createElement("div");
        item.className = "file-item" + (file.ignored ? " ignored" : "");
        item.draggable = true; item.dataset.index = index;
        item.innerHTML = `<div class="drag-handle">☰</div><div class="number">${String(index).padStart(2, "0")}</div><div class="filename">${escapeHtml(file.name)}</div><div class="expected" id="expected-${index}"></div><label class="ignore-box"><input type="checkbox" ${file.ignored ? "checked" : ""}>Ignore</label>`;
        item.querySelector("input").addEventListener("change", e => toggleIgnore(index, e.target.checked));
        item.addEventListener("dragstart", dragStart); item.addEventListener("dragend", dragEnd);
        list.appendChild(item);
    });
    $("count").innerText = files.length + (files.length === 1 ? " file" : " files"); $("renameButton").disabled = false; updateExpectedNames();
}
function toggleIgnore(index, ignored) { files[index].ignored = ignored; const items = document.querySelectorAll(".file-item"); if (items[index]) items[index].classList.toggle("ignored", ignored); updateExpectedNames(); }
function dragStart(e) { draggedIndex = Number(e.currentTarget.dataset.index); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", String(draggedIndex)); e.currentTarget.classList.add("dragging"); }
function dragEnd(e) { e.currentTarget.classList.remove("dragging"); $("fileList").classList.remove("drag-active"); if (draggedIndex !== null) { const items = [...$("fileList").querySelectorAll(".file-item")]; files = items.map(item => files[Number(item.dataset.index)]); } draggedIndex = null; renderFiles(); }
function allowDrop(e) { e.preventDefault(); if (draggedIndex === null) return; const list = $("fileList"); list.classList.add("drag-active"); const dragged = document.querySelector(".dragging"); if (!dragged) return; const after = getDragAfterElement(list, e.clientY); if (after == null) list.appendChild(dragged); else list.insertBefore(dragged, after); }
function dragLeave(e) { const list = $("fileList"); if (!list.contains(e.relatedTarget)) list.classList.remove("drag-active"); }
function dropFile(e) { e.preventDefault(); $("fileList").classList.remove("drag-active"); }
function getDragAfterElement(container, y) { const elements = [...container.querySelectorAll(".file-item:not(.dragging)")]; let closest = { offset: Number.NEGATIVE_INFINITY, element: null }; elements.forEach(element => { const box = element.getBoundingClientRect(); const offset = y - box.top - box.height / 2; if (offset < 0 && offset > closest.offset) closest = { offset: offset, element: element }; }); return closest.element; }
function updateExpectedNames() { const prefix = $("prefix").value.trim(); let seq = 0; files.forEach((file, index) => { const el = $("expected-" + index); if (!el) return; if (file.ignored) { el.innerText = "IGNORED"; return; } el.innerText = prefix ? buildName(seq, prefix) : ""; seq++; }); }
async function renameFiles() {
    const folder = $("folder").value.trim(); const prefix = $("prefix").value.trim();
    if (!folder || !prefix || files.length === 0) { showMessage("Required fields are missing.", true); return; }
    let preview = ""; let seq = 0; files.forEach(file => { preview += file.name + "  ->  " + (file.ignored ? "IGNORED" : buildName(seq, prefix)) + "\n"; if (!file.ignored) seq++; });
    if (!confirm("Confirm sequential updates:\n\n" + preview)) return;
    const button = $("renameButton"); button.disabled = true; button.innerText = "Renaming...";
    isProcessing = true;
    try {
        const response = await fetch("/execute_rename", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ folder: folder, prefix: prefix, files: files.map(f => ({ name: f.name, ignored: f.ignored })) }) });
        const data = await response.json(); if (!data.success) { showMessage(data.message, true); return; }
        await loadFiles(true); showMessage(data.message, false);
    } catch (error) { showMessage("Rename operation failed.", true); } finally { button.disabled = files.length === 0; button.innerText = "Rename Files"; isProcessing = false; }
}

function showMessage(text, error) { 
    const el = $("message"); 
    el.innerText = text; 
    el.className = "message " + (error ? "error" : "success"); 
}
function escapeHtml(text) { 
    return text.replace(/&/g, '&amp;')
               .replace(/</g, '&lt;')
               .replace(/>/g, '&gt;')
               .replace(/"/g, '&quot;')
               .replace(/'/g, '&#039;'); 
}
</script>
</body>
</html>
"""

@app.route('/step3', methods=['GET'])
def step3_page():
    return render_template_string(STEP3_HTML, tabs_css=TABS_CSS, tabs=tabs_html('msprep', bleed=True))

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
    if len(sys.argv) >= 5 and sys.argv[1] == '--diff':
        import faulthandler; faulthandler.enable()
        v = TexValidatorStreamer(sys.argv[2])
        res = v._compare_pdfs(sys.argv[3], sys.argv[4], progress=lambda d, t: print(f"PROGRESS {d} {t}", flush=True))
        if res:
            print(f"RESULT {res}", flush=True); sys.exit(0)
        print(f"ERROR {v.diff_error or 'unknown error'}", flush=True); sys.exit(1)

    import faulthandler, tempfile
    _crash_log = open(os.path.join(tempfile.gettempdir(), 'latex_validator_crash.log'), 'a')
    faulthandler.enable(_crash_log)
    webview.create_window('TeXi', app, maximized=True)
    webview.start()
