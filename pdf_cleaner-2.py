#!/usr/bin/env python3
"""
PDF Cleaner - gỡ logo chìm, header, footer khỏi PDF (giữ nguyên PDF, không đổi sang Word).
Hỗ trợ cả PDF scan (mỗi trang là một ảnh): bật "Xóa cả vùng ảnh" rồi cắt theo % trang.

Cài đặt:   pip install pymupdf        (khuyên dùng bản >= 1.24)
Chạy:      python pdf_cleaner.py
Tkinter có sẵn trong Python trên Windows/macOS. Linux: sudo apt install python3-tk
"""
import queue
import re
import threading
import tkinter as tk
from collections import Counter, defaultdict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import fitz  # PyMuPDF

# ----------------------------------------------------------------------------
# LÕI XỬ LÝ
# ----------------------------------------------------------------------------

WM_MIN_AREA = 0.15      # ảnh chiếm >= 15% diện tích trang mới coi là logo chìm
WM_MIN_PAGES = 0.6      # ... và xuất hiện ở >= 60% số trang
HF_ZONE = 0.12          # vùng quét header/footer tự động: 12% trên / 12% dưới
HF_MIN_PAGES = 0.6      # chữ lặp lại ở >= 60% số trang = header/footer
FILL_COLOR = (1, 1, 1)  # màu tô vùng bị xóa (trắng). Đổi nếu giấy scan ngả màu.


def detect_watermarks(doc):
    """Trả về tập xref của ảnh lớn lặp lại ở nhiều trang (logo chìm)."""
    n = len(doc)
    seen = defaultdict(set)
    for pno, page in enumerate(doc):
        pa = page.rect.width * page.rect.height
        for info in page.get_image_info(xrefs=True):
            xref = info.get("xref", 0)
            if not xref:
                continue
            b = fitz.Rect(info["bbox"])
            if b.width * b.height >= WM_MIN_AREA * pa:
                seen[xref].add(pno)
    need = 1 if n == 1 else max(2, int(n * WM_MIN_PAGES + 0.5))
    return {x for x, pages in seen.items() if len(pages) >= need}


def detect_header_footer(pages_blocks, heights):
    """
    pages_blocks: list (mỗi trang) các tuple (x0, y0, x1, y1, text)
    heights:      list chiều cao từng trang
    Trả về list (mỗi trang) các tuple (x0, y0, x1, y1) cần xóa.
    Khối chữ nằm ở 12% trên/dưới, có cùng nội dung (số được chuẩn hóa) và cùng
    vị trí ở >= 60% số trang => header/footer.
    """
    n = len(pages_blocks)
    keyed = []
    count = Counter()
    for blocks, h in zip(pages_blocks, heights):
        page_keys = []
        for (x0, y0, x1, y1, text) in blocks:
            t = text.strip()
            if not t:
                continue
            if y1 < h * HF_ZONE or y0 > h * (1 - HF_ZONE):
                key = (re.sub(r"\d+", "#", t), round(y0 / 6))
                page_keys.append((key, (x0, y0, x1, y1)))
                count[key] += 1
        keyed.append(page_keys)
    need = 1 if n == 1 else max(2, int(n * HF_MIN_PAGES + 0.5))
    result = []
    for page_keys in keyed:
        result.append([r for key, r in page_keys if count[key] >= need and n > 1])
    return result


def analyze(doc, opts):
    """Phân tích cả tài liệu một lần. Trả về dict dùng cho clean_page."""
    info = {"wm": set(), "hf": None}
    if opts["watermark"]:
        info["wm"] = detect_watermarks(doc)
    if opts["mode"] == "auto":
        blocks, heights = [], []
        for page in doc:
            bl = [(b[0], b[1], b[2], b[3], b[4]) for b in page.get_text("blocks") if b[6] == 0]
            blocks.append(bl)
            heights.append(page.rect.height)
        info["hf"] = detect_header_footer(blocks, heights)
    return info


def _apply_redactions(page, clean_images=False):
    """clean_images=True: tô trắng cả pixel của ảnh nằm trong vùng redact (PDF scan)."""
    img_mode = fitz.PDF_REDACT_IMAGE_PIXELS if clean_images else fitz.PDF_REDACT_IMAGE_NONE
    try:
        page.apply_redactions(
            images=img_mode,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
        )
    except TypeError:  # PyMuPDF cũ không có tham số graphics
        page.apply_redactions(images=img_mode)


def hf_rects(page, pno, info, opts):
    """Danh sách vùng header/footer cần xóa trên trang pno."""
    w, h = page.rect.width, page.rect.height
    if opts["mode"] == "auto" and info["hf"] is not None:
        pad = 1.5
        return [fitz.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad) for (x0, y0, x1, y1) in info["hf"][pno]]
    rects = []
    if opts["header"] > 0:
        rects.append(fitz.Rect(0, 0, w, h * opts["header"] / 100))
    if opts["footer"] > 0:
        rects.append(fitz.Rect(0, h * (1 - opts["footer"] / 100), w, h))
    return rects


def clean_page(doc, pno, info, opts):
    page = doc[pno]
    for xref in info["wm"]:
        try:
            page.delete_image(xref)
        except Exception:
            pass
    rects = hf_rects(page, pno, info, opts)
    for r in rects:
        page.add_redact_annot(r, fill=FILL_COLOR)
    if rects:
        _apply_redactions(page, opts.get("img", False))


def clean_file(src, dst, opts):
    doc = fitz.open(src)
    info = analyze(doc, opts)
    for pno in range(len(doc)):
        clean_page(doc, pno, info, opts)
    doc.save(dst, garbage=4, deflate=True)
    result = (len(doc), len(info["wm"]))
    doc.close()
    return result


def render_preview(src, pno, opts, dpi=100):
    """Trả về (png_bytes, danh sách vùng bị xóa đã quy đổi theo dpi)."""
    doc = fitz.open(src)
    info = analyze(doc, opts)
    page = doc[pno]
    rects = hf_rects(page, pno, info, opts)
    clean_page(doc, pno, info, opts)
    pix = doc[pno].get_pixmap(dpi=dpi)
    png = pix.tobytes("png")
    k = dpi / 72
    zones = [(r.x0 * k, r.y0 * k, r.x1 * k, r.y1 * k) for r in rects]
    n = len(doc)
    doc.close()
    return png, zones, n, len(info["wm"])


# ----------------------------------------------------------------------------
# GIAO DIỆN
# ----------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PDF Cleaner - gỡ logo chìm, header, footer")
        self.geometry("1100x760")
        self.files = []
        self.cur_page = 0
        self.page_count = 1
        self.photo = None
        self.q = queue.Queue()
        self._build()
        self.after(100, self._poll)

    # --- dựng UI
    def _build(self):
        left = ttk.Frame(self, padding=8)
        left.pack(side="left", fill="y")
        right = ttk.Frame(self, padding=8)
        right.pack(side="right", fill="both", expand=True)

        ttk.Label(left, text="1. Chọn file", font=("", 10, "bold")).pack(anchor="w")
        bf = ttk.Frame(left)
        bf.pack(fill="x")
        ttk.Button(bf, text="Thêm PDF...", command=self.add_files).pack(side="left", padx=(0, 4))
        ttk.Button(bf, text="Thêm thư mục...", command=self.add_folder).pack(side="left", padx=(0, 4))
        ttk.Button(bf, text="Xóa hết", command=self.clear).pack(side="left")
        lf = ttk.Frame(left)
        lf.pack(fill="x", pady=4)
        self.listbox = tk.Listbox(lf, width=44, height=9, exportselection=False)
        sb = ttk.Scrollbar(lf, command=self.listbox.yview)
        self.listbox.config(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="x")
        sb.pack(side="left", fill="y")
        self.listbox.bind("<<ListboxSelect>>", lambda e: self.on_select())

        ttk.Separator(left).pack(fill="x", pady=6)
        ttk.Label(left, text="2. Tùy chọn", font=("", 10, "bold")).pack(anchor="w")
        self.var_wm = tk.BooleanVar(value=True)
        ttk.Checkbutton(left, text="Gỡ logo chìm (ảnh lớn lặp ở nhiều trang)",
                        variable=self.var_wm, command=self.refresh).pack(anchor="w")

        self.var_img = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="Xóa cả vùng ảnh (PDF scan)",
                        variable=self.var_img, command=self.refresh).pack(anchor="w")

        self.var_mode = tk.StringVar(value="auto")
        ttk.Radiobutton(left, text="Header/footer: tự nhận diện chữ lặp lại",
                        variable=self.var_mode, value="auto", command=self.refresh).pack(anchor="w", pady=(6, 0))
        ttk.Radiobutton(left, text="Header/footer: cắt theo % trang",
                        variable=self.var_mode, value="manual", command=self.refresh).pack(anchor="w")

        self.var_h = tk.DoubleVar(value=4.5)
        self.var_f = tk.DoubleVar(value=4.0)
        self.lbl_h = ttk.Label(left, text="")
        self.lbl_h.pack(anchor="w", pady=(6, 0))
        ttk.Scale(left, from_=0, to=15, variable=self.var_h, command=self._slide).pack(fill="x")
        self.lbl_f = ttk.Label(left, text="")
        self.lbl_f.pack(anchor="w")
        ttk.Scale(left, from_=0, to=15, variable=self.var_f, command=self._slide).pack(fill="x")
        self._update_labels()

        ttk.Separator(left).pack(fill="x", pady=6)
        ttk.Label(left, text="3. Xuất", font=("", 10, "bold")).pack(anchor="w")
        of = ttk.Frame(left)
        of.pack(fill="x")
        self.var_out = tk.StringVar(value="")
        ttk.Entry(of, textvariable=self.var_out, width=32).pack(side="left", padx=(0, 4))
        ttk.Button(of, text="Chọn...", command=self.pick_out).pack(side="left")
        ttk.Label(left, text="(để trống = lưu cạnh file gốc, thêm đuôi _clean)",
                  foreground="gray").pack(anchor="w")
        ttk.Button(left, text="▶ Làm sạch tất cả", command=self.run).pack(fill="x", pady=8)
        self.progress = ttk.Progressbar(left, mode="determinate")
        self.progress.pack(fill="x")
        self.log = tk.Text(left, width=44, height=10, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, pady=(6, 0))

        # preview
        nav = ttk.Frame(right)
        nav.pack(fill="x")
        ttk.Button(nav, text="◀", width=3, command=lambda: self.goto(self.cur_page - 1)).pack(side="left")
        self.lbl_page = ttk.Label(nav, text="Chưa có file")
        self.lbl_page.pack(side="left", padx=8)
        ttk.Button(nav, text="▶", width=3, command=lambda: self.goto(self.cur_page + 1)).pack(side="left")
        ttk.Label(nav, text="   Khung đỏ = vùng sẽ bị xóa. Trang hiển thị là kết quả sau khi dọn.",
                  foreground="gray").pack(side="left")
        cf = ttk.Frame(right)
        cf.pack(fill="both", expand=True, pady=4)
        self.canvas = tk.Canvas(cf, background="#888")
        vs = ttk.Scrollbar(cf, orient="vertical", command=self.canvas.yview)
        hs = ttk.Scrollbar(cf, orient="horizontal", command=self.canvas.xview)
        self.canvas.config(yscrollcommand=vs.set, xscrollcommand=hs.set)
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)

    # --- thao tác file
    def add_files(self):
        for f in filedialog.askopenfilenames(filetypes=[("PDF", "*.pdf")]):
            self._add(f)

    def add_folder(self):
        d = filedialog.askdirectory()
        if d:
            for f in sorted(Path(d).glob("*.pdf")):
                self._add(str(f))

    def _add(self, f):
        if f not in self.files:
            self.files.append(f)
            self.listbox.insert("end", Path(f).name)
        if self.listbox.size() and not self.listbox.curselection():
            self.listbox.selection_set(0)
            self.on_select()

    def clear(self):
        self.files.clear()
        self.listbox.delete(0, "end")
        self.canvas.delete("all")
        self.lbl_page.config(text="Chưa có file")

    def pick_out(self):
        d = filedialog.askdirectory()
        if d:
            self.var_out.set(d)

    # --- preview
    def opts(self):
        return {"watermark": self.var_wm.get(), "mode": self.var_mode.get(),
                "header": self.var_h.get(), "footer": self.var_f.get(),
                "img": self.var_img.get()}

    def _update_labels(self):
        self.lbl_h.config(text=f"Header (thủ công): {self.var_h.get():.1f}% từ mép trên")
        self.lbl_f.config(text=f"Footer (thủ công): {self.var_f.get():.1f}% từ mép dưới")

    def _slide(self, _=None):
        self._update_labels()
        if self.var_mode.get() == "manual":
            self.refresh()

    def on_select(self):
        self.cur_page = 0
        self.refresh()

    def goto(self, p):
        if 0 <= p < self.page_count:
            self.cur_page = p
            self.refresh()

    def refresh(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        src = self.files[sel[0]]
        try:
            png, zones, n, nwm = render_preview(src, min(self.cur_page, 9999), self.opts())
        except Exception as e:
            self.lbl_page.config(text=f"Lỗi xem trước: {e}")
            return
        self.page_count = n
        self.cur_page = min(self.cur_page, n - 1)
        self.photo = tk.PhotoImage(data=png)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        for (x0, y0, x1, y1) in zones:
            self.canvas.create_rectangle(x0, y0, x1, y1, outline="red", dash=(4, 3), width=2)
        self.canvas.config(scrollregion=(0, 0, self.photo.width(), self.photo.height()))
        self.lbl_page.config(text=f"Trang {self.cur_page + 1}/{n}  |  logo chìm tìm thấy: {nwm}")

    # --- chạy hàng loạt
    def run(self):
        if not self.files:
            messagebox.showinfo("Thông báo", "Chưa có file nào.")
            return
        opts = self.opts()
        out = self.var_out.get().strip()
        files = list(self.files)
        self.progress.config(maximum=len(files), value=0)
        threading.Thread(target=self._worker, args=(files, out, opts), daemon=True).start()

    def _worker(self, files, out, opts):
        for i, f in enumerate(files, 1):
            p = Path(f)
            dst = (Path(out) if out else p.parent) / f"{p.stem}_clean.pdf"
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                pages, nwm = clean_file(str(p), str(dst), opts)
                self.q.put(("log", f"OK  {p.name}  ({pages} trang, {nwm} logo) -> {dst.name}"))
            except Exception as e:
                self.q.put(("log", f"LỖI {p.name}: {e}"))
            self.q.put(("prog", i))
        self.q.put(("log", "Xong."))

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.log.config(state="normal")
                    self.log.insert("end", val + "\n")
                    self.log.see("end")
                    self.log.config(state="disabled")
                else:
                    self.progress.config(value=val)
        except queue.Empty:
            pass
        self.after(100, self._poll)


if __name__ == "__main__":
    App().mainloop()
