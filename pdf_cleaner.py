#!/usr/bin/env python3
"""
PDF Cleaner - gỡ logo chìm, header, footer, vùng tùy chọn khỏi PDF
(giữ nguyên PDF, hỗ trợ cả PDF scan mà mỗi trang là một ảnh).

Cài đặt:   pip install pymupdf customtkinter pillow tkinterdnd2
           (tkinterdnd2 chỉ để kéo thả file; thiếu vẫn chạy, chỉ không kéo thả được)
Chạy:      python pdf_cleaner.py
Linux:     sudo apt install python3-tk
"""
import os
import queue
import re
import string
import subprocess
import sys
import threading
import tkinter as tk
from bisect import bisect_right
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageStat, ImageTk

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24.3
except ImportError:  # bản cũ hơn
    import fitz

try:  # kéo thả file/thư mục vào cửa sổ
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except Exception:
    DND_FILES = TkinterDnD = None
    HAS_DND = False

# ----------------------------------------------------------------------------
# LÕI XỬ LÝ
# ----------------------------------------------------------------------------

WM_MIN_AREA = 0.15      # ảnh chiếm >= 15% diện tích trang mới coi là logo chìm
WM_MIN_PAGES = 0.6      # ... và xuất hiện ở >= 60% số trang
HF_ZONE = 0.12          # vùng quét header/footer tự động: 12% trên / 12% dưới
HF_MIN_PAGES = 0.6      # chữ lặp lại ở >= 60% số trang = header/footer
HF_MAX_PCT = 30.0       # giới hạn % khi cắt theo %
WHITE = (1, 1, 1)


def to_view_rect(page, r):
    """Tọa độ gốc của trang (chưa xoay) -> tọa độ như đang nhìn thấy (đã xoay)."""
    r = fitz.Rect(r)
    if page.rotation:
        r = r * page.rotation_matrix
        r.normalize()
    return r


def to_page_rect(page, r):
    """Tọa độ như đang nhìn thấy -> tọa độ gốc của trang (dùng cho redact)."""
    r = fitz.Rect(r)
    if page.rotation:
        r = r * page.derotation_matrix
        r.normalize()
    return r


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
    pages_blocks: list (mỗi trang) các tuple (x0, y0, x1, y1, text)  - tọa độ đã xoay
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


def collect_hf(doc):
    """Quét chữ của cả tài liệu, trả về header/footer lặp lại của từng trang."""
    blocks, heights = [], []
    for page in doc:
        bl = []
        for b in page.get_text("blocks"):
            if b[6] != 0:
                continue
            r = to_view_rect(page, b[:4])
            bl.append((r.x0, r.y0, r.x1, r.y1, b[4]))
        blocks.append(bl)
        heights.append(page.rect.height)
    return detect_header_footer(blocks, heights)


def analyze(doc, opts):
    """Phân tích cả tài liệu một lần. Trả về dict dùng cho clean_page."""
    return {
        "wm": detect_watermarks(doc) if opts["watermark"] else set(),
        "hf": collect_hf(doc) if opts["mode"] == "auto" else None,
    }


def parse_ranges(text, n):
    """'1-5, 8, 10-' -> {0,1,2,3,4,7,9,...} (đánh số từ 0, đã cắt theo n trang)."""
    text = re.sub(r"\s*-\s*", "-", text.strip())
    pages = set()
    for part in re.split(r"[,;\s]+", text):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)-(\d*)", part)
        if m and (m.group(1) or m.group(2)):
            a = int(m.group(1)) if m.group(1) else 1
            b = int(m.group(2)) if m.group(2) else n
            if a > b:
                a, b = b, a
            pages.update(range(a - 1, b))
        elif part.isdigit():
            pages.add(int(part) - 1)
        else:
            raise ValueError(f"Không hiểu '{part}'")
    return {p for p in pages if 0 <= p < n}


def region_applies(rg, pno):
    s = rg["scope"]
    if s == "this":
        return pno == rg["page"]
    if s == "all":
        return True
    if s == "odd":      # trang 1, 3, 5...
        return pno % 2 == 0
    if s == "even":     # trang 2, 4, 6...
        return pno % 2 == 1
    if s == "range":
        return pno in rg["pages"]
    return False


def hf_rects(w, h, pno, info, opts):
    """Vùng header/footer cần xóa trên trang pno (tọa độ như đang nhìn thấy)."""
    mode = opts["mode"]
    if mode == "auto":
        if info.get("hf") is None:
            return []
        pad = 1.5
        return [fitz.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad)
                for (x0, y0, x1, y1) in info["hf"][pno]]
    rects = []
    if mode == "manual":
        if opts["header"] > 0:
            rects.append(fitz.Rect(0, 0, w, h * opts["header"] / 100))
        if opts["footer"] > 0:
            rects.append(fitz.Rect(0, h * (1 - opts["footer"] / 100), w, h))
    return rects


def custom_zones(w, h, pno, n, regions):
    """Vùng tùy chỉnh áp dụng cho trang pno: list (chỉ số vùng, Rect)."""
    out = []
    for idx, rg in enumerate(regions):
        if region_applies(rg, pno):
            fx0, fy0, fx1, fy1 = rg["rect"]
            out.append((idx, fitz.Rect(fx0 * w, fy0 * h, fx1 * w, fy1 * h)))
    return out


def sample_paper_color(page, rect):
    """Màu nền giấy: median màu của dải mỏng bao quanh vùng sẽ xóa."""
    m = 8
    strips = [
        fitz.Rect(rect.x0 - m, rect.y0 - m, rect.x1 + m, rect.y0),
        fitz.Rect(rect.x0 - m, rect.y1, rect.x1 + m, rect.y1 + m),
        fitz.Rect(rect.x0 - m, rect.y0, rect.x0, rect.y1),
        fitz.Rect(rect.x1, rect.y0, rect.x1 + m, rect.y1),
    ]
    px = []
    for s in strips:
        s = s & page.rect
        if s.is_empty or s.width < 1 or s.height < 1:
            continue
        try:
            pix = page.get_pixmap(clip=s, dpi=48, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            px.extend(img.getdata())
        except Exception:
            pass
    if not px:
        return WHITE
    mid = len(px) // 2
    return tuple(sorted(p[i] for p in px)[mid] / 255 for i in range(3))


def _apply_redactions(page, clean_images=False, keep_text=False):
    """
    clean_images=True: tô trắng cả pixel của ảnh nằm trong vùng redact (PDF scan).
    keep_text=True:    không đụng tới chữ (dùng cho bước tách bản sao ảnh logo).
    """
    kw = {"images": fitz.PDF_REDACT_IMAGE_PIXELS if clean_images else fitz.PDF_REDACT_IMAGE_NONE}
    if keep_text and hasattr(fitz, "PDF_REDACT_TEXT_NONE"):
        kw["text"] = fitz.PDF_REDACT_TEXT_NONE
    attempts = [dict(kw, graphics=fitz.PDF_REDACT_LINE_ART_NONE), kw]
    if "text" in kw:
        attempts.insert(1, {k: v for k, v in attempts[0].items() if k != "text"})
    last = None
    for a in attempts:
        try:
            page.apply_redactions(**a)
            return
        except TypeError as e:  # PyMuPDF cũ không có tham số graphics/text
            last = e
    raise last


def _same_rect(r, rects, tol=1.0):
    q = tuple(r)
    return any(all(abs(x - y) <= tol for x, y in zip(q, t)) for t in rects)


def _images_at(page, rects):
    """xref các ảnh trên trang có vị trí trùng một trong `rects` (tọa độ trang)."""
    res = []
    for info in page.get_image_info(xrefs=True):
        x = info.get("xref", 0)
        if x and _same_rect(info["bbox"], rects):
            res.append((x, fitz.Rect(info["bbox"])))
    return res


def _watermark_rects(page, wm):
    return [tuple(info["bbox"]) for info in page.get_image_info(xrefs=True)
            if info.get("xref", 0) in wm]


def _remove_watermarks(page, rects):
    """
    Xóa logo chìm CHỈ trên trang này. Ảnh logo thường dùng chung một xref cho mọi trang,
    nên trước tiên "chạm" nhẹ bằng một redact tí hon để trang có bản sao riêng,
    rồi mới xóa bản sao đó (không ảnh hưởng các trang khác, không để lại vệt đen).
    Ảnh logo được nhận ra bằng vị trí trên trang; bản sao là xref mới sinh ra sau redact.
    """
    targets = _images_at(page, rects)
    if not targets:
        return
    before = {i[0] for i in page.get_images(full=True)}
    for _, r in targets:
        page.add_redact_annot(fitz.Rect(r.x0 + 0.5, r.y0 + 0.5, r.x0 + 0.7, r.y0 + 0.7), fill=False)
    try:
        _apply_redactions(page, clean_images=True, keep_text=True)
    except Exception:
        return
    new = [i[0] for i in page.get_images(full=True) if i[0] not in before]
    if not new:  # không tách được bản sao: xóa thẳng ảnh gốc
        new = [x for x, _ in targets]
    elif len(new) != len(targets):
        return   # có ảnh khác bị chạm nhầm: bỏ qua cho an toàn, không xóa nhầm ảnh
    for x in new:
        try:
            page.delete_image(x)
        except Exception:
            pass


def clean_page(doc, pno, info, opts):
    page = doc[pno]
    wm_rects = _watermark_rects(page, info["wm"]) if info["wm"] else []
    pr = page.rect
    rects = hf_rects(pr.width, pr.height, pno, info, opts)
    rects += [r for _, r in custom_zones(pr.width, pr.height, pno, len(doc), opts.get("regions", ()))]
    rects = [r & pr for r in rects]
    rects = [r for r in rects if not r.is_empty and r.width > 0.5 and r.height > 0.5]
    if rects:  # 1) xóa header/footer/vùng tùy chọn trước
        paper = opts.get("fill") == "paper"
        fills = [sample_paper_color(page, r) if paper else WHITE for r in rects]
        for r, col in zip(rects, fills):
            page.add_redact_annot(to_page_rect(page, r), fill=col)
        _apply_redactions(page, opts.get("img", False))
        try:  # nạp lại trang để lấy đúng xref ảnh sau khi redact
            page = doc.reload_page(page)
        except Exception:
            pass
    if wm_rects:  # 2) rồi mới gỡ logo chìm
        _remove_watermarks(page, wm_rects)


def clean_file(src, dst, opts, progress=None, cancel=None):
    doc = fitz.open(src)
    try:
        if doc.needs_pass:
            raise RuntimeError("file có mật khẩu")
        info = analyze(doc, opts)
        n = len(doc)
        for pno in range(n):
            if cancel is not None and cancel.is_set():
                raise InterruptedError
            clean_page(doc, pno, info, opts)
            if progress:
                progress(pno + 1, n)
        tmp = str(dst) + ".part"
        doc.save(tmp, garbage=4, deflate=True)
        os.replace(tmp, dst)
        return n, len(info["wm"])
    finally:
        doc.close()


# ----------------------------------------------------------------------------
# GIAO DIỆN - màu sắc & tiện ích
# ----------------------------------------------------------------------------

ORANGE = "#F97316"
ORANGE_HOVER = "#EA580C"
ORANGE_SOFT = "#FFEDD5"
ORANGE_SOFT_HOVER = "#FED7AA"
ORANGE_DARK = "#9A3412"
BG = "#FFF7ED"
CARD = "#FFFFFF"
BORDER = "#F3E5D6"
TEXT = "#1C1917"
MUTED = "#78716C"
FIELD = "#E7E5E4"
PREVIEW_BG = "#E7E5E4"
DANGER = "#EF4444"
FONT_FAMILY = "Segoe UI" if sys.platform.startswith("win") else None

SCOPES = {
    "Chỉ trang này": "this",
    "Mọi trang": "all",
    "Các trang lẻ": "odd",
    "Các trang chẵn": "even",
    "Khoảng trang…": "range",
}
MODES = {"Tự động": "auto", "Theo %": "manual", "Tắt": "none"}
FILE_SCOPES = {"Chỉ file này": "this", "Mọi file": "all", "Chọn file…": "pick"}


def font(size=13, bold=False, underline=False):
    return ctk.CTkFont(family=FONT_FAMILY, size=size, weight="bold" if bold else "normal",
                       underline=underline)


def btn_primary(parent, text, command, **kw):
    kw.setdefault("height", 36)
    return ctk.CTkButton(parent, text=text, command=command, corner_radius=10,
                         fg_color=ORANGE, hover_color=ORANGE_HOVER, text_color="white",
                         font=font(13, True), **kw)


def btn_soft(parent, text, command, **kw):
    kw.setdefault("height", 32)
    return ctk.CTkButton(parent, text=text, command=command, corner_radius=10,
                         fg_color=ORANGE_SOFT, hover_color=ORANGE_SOFT_HOVER,
                         text_color=ORANGE_DARK, font=font(12, True), **kw)


def switch(parent, text, var, command):
    return ctk.CTkSwitch(parent, text=text, variable=var, onvalue=True, offvalue=False,
                         command=command, progress_color=ORANGE, fg_color="#D6D3D1",
                         button_color="white", button_hover_color="#FFF7ED",
                         text_color=TEXT, font=font(13))


def segmented(parent, values, command, initial):
    seg = ctk.CTkSegmentedButton(parent, values=values, command=command, corner_radius=10,
                                 fg_color="#F5F5F4", selected_color="#FB923C",
                                 selected_hover_color=ORANGE, unselected_color="#F5F5F4",
                                 unselected_hover_color=ORANGE_SOFT, text_color=TEXT,
                                 font=font(12, True))
    seg.set(initial)
    return seg


def open_folder(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


# ----------------------------------------------------------------------------
# NHẬP / XUẤT: vùng thả file, hộp chọn thư mục (có tạo thư mục mới)
# ----------------------------------------------------------------------------

if HAS_DND:
    class _Root(ctk.CTk, TkinterDnD.DnDWrapper):
        """Cửa sổ chính có hỗ trợ kéo thả (nếu tkdnd chạy được trên máy)."""
        dnd_ok = False

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            try:
                self.TkdndVersion = TkinterDnD._require(self)
                self.dnd_ok = True
            except Exception:
                self.dnd_ok = False
else:
    class _Root(ctk.CTk):
        dnd_ok = False


def file_size_text(path):
    try:
        n = os.path.getsize(path)
    except OSError:
        return ""
    if n < 1024 * 1024:
        return f"{max(1, n // 1024)} KB"
    return f"{n / 1048576:.1f} MB"


def shorten(text, n=52):
    return text if len(text) <= n else "…" + text[-(n - 1):]


def default_places():
    """Các vị trí hay dùng, hiện ở cột trái hộp chọn thư mục."""
    home = Path.home()
    out = [("Thư mục nhà", home)]
    for label, name in (("Màn hình nền", "Desktop"), ("Tài liệu", "Documents"),
                        ("Tải xuống", "Downloads")):
        if (home / name).is_dir():
            out.append((label, home / name))
    if sys.platform.startswith("win"):
        for c in string.ascii_uppercase:
            if os.path.exists(f"{c}:\\"):
                out.append((f"Ổ đĩa {c}:", Path(f"{c}:\\")))
    else:
        out.append(("Thư mục gốc  /", Path("/")))
    return out


_FOLDER_ICON = None


def folder_icon():
    global _FOLDER_ICON
    if _FOLDER_ICON is None:
        im = Image.new("RGBA", (72, 56), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle((4, 4, 34, 24), radius=7, fill="#FDBA74")
        d.rounded_rectangle((4, 14, 68, 52), radius=9, fill="#FB923C")
        _FOLDER_ICON = ctk.CTkImage(light_image=im, dark_image=im, size=(22, 17))
    return _FOLDER_ICON


class DropZone(tk.Canvas):
    """Ô tải lên: kéo thả file/thư mục vào, hoặc bấm để chọn file."""

    def __init__(self, master, on_click, dnd=False):
        super().__init__(master, height=122, bg=CARD, highlightthickness=0, bd=0, cursor="hand2")
        self.on_click = on_click
        self.dnd = dnd
        self.hover = False
        self.active = False
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<Button-1>", lambda e: self.on_click())

    def _hover(self, on):
        self.hover = on
        self.draw()

    def set_active(self, on):
        if self.active != on:
            self.active = on
            self.draw()

    def set_dnd(self, on):
        self.dnd = on
        self.draw()

    def _rr(self, x0, y0, x1, y1, r, **kw):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
               x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return self.create_polygon(pts, smooth=True, **kw)

    def draw(self):
        self.delete("all")
        w, h = max(self.winfo_width(), 60), max(self.winfo_height(), 60)
        on = self.active or self.hover
        self._rr(2, 2, w - 2, h - 2, 16, fill=ORANGE_SOFT if on else "#FFFAF3",
                 outline=ORANGE if on else "#FDBA74", width=2, dash=(6, 4))
        cx, y = w / 2, 32
        kw = dict(fill=ORANGE, width=3, capstyle="round", joinstyle="round")
        self.create_line(cx, y + 11, cx, y - 12, **kw)                           # thân mũi tên
        self.create_line(cx - 9, y - 3, cx, y - 12, cx + 9, y - 3, **kw)         # đầu mũi tên
        self.create_line(cx - 17, y + 3, cx - 17, y + 17, cx + 17, y + 17, cx + 17, y + 3, **kw)  # khay
        if self.active:
            t1, t2 = "Thả vào đây để thêm", "File PDF hoặc cả thư mục"
        elif self.dnd:
            t1, t2 = "Kéo thả PDF hoặc thư mục vào đây", "hoặc bấm vào ô này để chọn file"
        else:
            t1, t2 = "Bấm để chọn file PDF", "Cài tkinterdnd2 để dùng kéo thả"
        self.create_text(cx, 72, text=t1, fill=TEXT, font=font(13, True), width=w - 24, justify="center")
        self.create_text(cx, 96, text=t2, fill=MUTED, font=font(11), width=w - 24, justify="center")


class FolderPicker(ctk.CTkToplevel):
    """Hộp chọn thư mục: bấm vào thư mục con để đi vào, có nút tạo thư mục mới."""
    MAX_ROWS = 300

    def __init__(self, master, title, start=None, confirm="Chọn thư mục này",
                 allow_new=True, note="", places=(), pick_files=False):
        super().__init__(master, fg_color=BG)
        self.title(title)
        self.minsize(620, 440)
        self.result = None
        self.cur = Path.home()
        self.allow_new = allow_new
        self.confirm_text = confirm
        self.note = note
        self.pick_files = pick_files   # True: chọn nhiều file PDF thay vì chọn thư mục
        self.selected = set()
        self._file_vars = {}
        self.btn_ok = None
        self.var_path = tk.StringVar()
        self.var_hidden = tk.BooleanVar(value=False)
        self.var_new = tk.StringVar()
        self._new_open = False
        self._grab_tries = 0
        self._build(places)
        w, h = 780, 560
        self.update_idletasks()
        x = master.winfo_rootx() + max(0, (master.winfo_width() - w) // 2)
        y = master.winfo_rooty() + max(0, (master.winfo_height() - h) // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda e: self._cancel())
        self.navigate(start or Path.home(), quiet=True)
        self.after(150, self._grab)

    # --- dựng giao diện ----------------------------------------------------------
    def _build(self, places):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(self, fg_color=CARD, corner_radius=14, border_width=1, border_color=BORDER)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=14, pady=(14, 8))
        top.grid_columnconfigure(1, weight=1)
        ctk.CTkButton(top, text="↑", width=34, height=34, corner_radius=8, fg_color=ORANGE_SOFT,
                      hover_color=ORANGE_SOFT_HOVER, text_color=ORANGE_DARK, font=font(14, True),
                      command=self.go_up).grid(row=0, column=0, padx=(10, 6), pady=10)
        self.ent_path = ctk.CTkEntry(top, textvariable=self.var_path, height=34, corner_radius=8,
                                     border_color=FIELD, fg_color="white", text_color=TEXT, font=font(12))
        self.ent_path.grid(row=0, column=1, sticky="ew", pady=10, padx=(0, 0 if self.allow_new else 10))
        self.ent_path.bind("<Return>", lambda e: self.navigate(self.var_path.get()))
        if self.allow_new:
            btn_primary(top, "＋ Thư mục mới", self.toggle_new, height=34, width=140).grid(
                row=0, column=2, padx=10, pady=10)

        side = ctk.CTkFrame(self, fg_color=CARD, corner_radius=14, border_width=1,
                            border_color=BORDER, width=190)
        side.grid(row=1, column=0, sticky="ns", padx=(14, 6), pady=(0, 8))

        def header(t):
            ctk.CTkLabel(side, text=t, font=font(11, True), text_color=MUTED).pack(
                anchor="w", padx=14, pady=(12, 2))

        def place(label, path):
            ctk.CTkButton(side, text=shorten(label, 20), anchor="w", height=30, corner_radius=8,
                          fg_color="transparent", hover_color=ORANGE_SOFT, text_color=TEXT,
                          font=font(12), command=lambda p=path: self.navigate(p)).pack(
                fill="x", padx=8, pady=1)

        if places:
            header("Gần đây")
            for label, path in places:
                place(label, path)
        header("Vị trí")
        for label, path in default_places():
            place(label, path)
        switch(side, "Hiện thư mục ẩn", self.var_hidden, self.refresh).pack(
            side="bottom", anchor="w", padx=14, pady=12)

        mid = ctk.CTkFrame(self, fg_color="transparent")
        mid.grid(row=1, column=1, sticky="nsew", padx=(0, 14), pady=(0, 8))
        mid.grid_rowconfigure(0, weight=1)
        mid.grid_columnconfigure(0, weight=1)
        self.listf = ctk.CTkScrollableFrame(mid, fg_color=CARD, corner_radius=14, border_width=1,
                                            border_color=BORDER, scrollbar_button_color="#E7E5E4",
                                            scrollbar_button_hover_color="#D6D3D1")
        self.listf.grid(row=0, column=0, sticky="nsew")

        self.newrow = ctk.CTkFrame(mid, fg_color=CARD, corner_radius=12, border_width=1, border_color=ORANGE)
        self.newrow.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self.newrow, text="Tên thư mục mới", font=font(12, True), text_color=TEXT).grid(
            row=0, column=0, padx=(12, 8), pady=10)
        self.ent_new = ctk.CTkEntry(self.newrow, textvariable=self.var_new, height=32, corner_radius=8,
                                    border_color=FIELD, fg_color="white", text_color=TEXT, font=font(12),
                                    placeholder_text="Ví dụ: PDF_da_lam_sach")
        self.ent_new.grid(row=0, column=1, sticky="ew", pady=10)
        self.ent_new.bind("<Return>", lambda e: self.create_folder())
        btn_primary(self.newrow, "Tạo", self.create_folder, height=32, width=64).grid(row=0, column=2, padx=(8, 4))
        btn_soft(self.newrow, "Đóng", self.toggle_new, height=32, width=64).grid(row=0, column=3, padx=(0, 10))

        self.lbl_err = ctk.CTkLabel(mid, text="", font=font(11), text_color=DANGER, anchor="w", justify="left")
        self.lbl_err.grid(row=2, column=0, sticky="ew", pady=(4, 0))

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=2, column=0, columnspan=2, sticky="ew", padx=14, pady=(0, 14))
        foot.grid_columnconfigure(0, weight=1)
        self.lbl_sel = ctk.CTkLabel(foot, text="", font=font(12), text_color=TEXT, anchor="w",
                                    justify="left", wraplength=420)
        self.lbl_sel.grid(row=0, column=0, sticky="ew", padx=(2, 10))
        btn_soft(foot, "Hủy", self._cancel, height=36, width=80).grid(row=0, column=1, padx=(0, 8))
        self.btn_ok = btn_primary(foot, self.confirm_text, self._ok, height=36,
                                  width=max(150, len(self.confirm_text) * 9))
        self.btn_ok.grid(row=0, column=2)

    # --- điều hướng --------------------------------------------------------------
    def _err(self, msg, ok=False):
        self.lbl_err.configure(text=msg, text_color="#15803D" if ok else DANGER)

    def _list_dirs(self, p):
        names = []
        show_hidden = self.var_hidden.get()
        with os.scandir(p) as it:
            for e in it:
                try:
                    if e.is_dir() and (show_hidden or not e.name.startswith(".")):
                        names.append(e.name)
                except OSError:
                    pass
        return sorted(names, key=str.lower)

    def _list_pdfs(self, p):
        out = []
        show_hidden = self.var_hidden.get()
        with os.scandir(p) as it:
            for e in it:
                try:
                    if (e.is_file() and e.name.lower().endswith(".pdf")
                            and (show_hidden or not e.name.startswith("."))):
                        out.append(e.name)
                except OSError:
                    pass
        return sorted(out, key=str.lower)

    def navigate(self, path, quiet=False):
        p = Path(os.path.abspath(os.path.expanduser(str(path).strip() or str(Path.home()))))
        msg = ""
        if not p.is_dir():
            q = p
            while not q.is_dir() and q != q.parent:
                q = q.parent
            p = q
            msg = "" if quiet else "Không tìm thấy đường dẫn đó - đã mở thư mục gần nhất."
        try:
            names = self._list_dirs(p)
            files = self._list_pdfs(p) if self.pick_files else []
        except OSError:
            self.var_path.set(str(self.cur))
            self._err("Không có quyền mở thư mục này.")
            return
        self.cur = p
        self.var_path.set(str(p))
        self._err(msg)
        self._render(names, files)
        self._update_footer()

    def refresh(self):
        self.navigate(self.cur, quiet=True)

    def go_up(self):
        self.navigate(self.cur.parent, quiet=True)

    def _render(self, names, files=()):
        for w in self.listf.winfo_children():
            w.destroy()
        self._file_vars = {}
        if files:
            bar = ctk.CTkFrame(self.listf, fg_color="transparent")
            bar.pack(fill="x", pady=(2, 6))
            btn_soft(bar, f"Chọn cả {len(files)} PDF ở đây", self._select_all, height=28).pack(
                side="left", padx=(0, 6))
            btn_soft(bar, "Bỏ chọn tất cả", self._select_none, height=28).pack(side="left")
        if not names and not files:
            ctk.CTkLabel(self.listf, font=font(12), text_color=MUTED,
                         text="Thư mục này chưa có thư mục con" + (" hoặc PDF" if self.pick_files else "")
                         ).pack(pady=30)
        for n in names[:self.MAX_ROWS]:
            ctk.CTkButton(self.listf, text=n, image=folder_icon(), compound="left", anchor="w",
                          height=32, corner_radius=8, fg_color="transparent", hover_color=ORANGE_SOFT,
                          text_color=TEXT, font=font(13),
                          command=lambda n=n: self.navigate(self.cur / n, quiet=True)).pack(fill="x", pady=1)
        for n in files[:self.MAX_ROWS]:
            path = str(self.cur / n)
            var = tk.BooleanVar(value=path in self.selected)
            self._file_vars[path] = var
            ctk.CTkCheckBox(self.listf, text=f"{shorten(n, 52)}   ({file_size_text(path)})", variable=var,
                            command=lambda path=path, var=var: self._toggle(path, var),
                            checkbox_width=20, checkbox_height=20, corner_radius=6, fg_color=ORANGE,
                            hover_color=ORANGE_HOVER, border_color="#D6D3D1", checkmark_color="white",
                            text_color=TEXT, font=font(13)).pack(anchor="w", padx=10, pady=5)
        more = max(0, len(names) - self.MAX_ROWS) + max(0, len(files) - self.MAX_ROWS)
        if more:
            ctk.CTkLabel(self.listf, font=font(11), text_color=MUTED,
                         text=f"… và {more} mục khác (gõ đường dẫn ở trên để vào)").pack(pady=8)
        try:
            self.listf._parent_canvas.yview_moveto(0)
        except Exception:
            pass

    def _toggle(self, path, var):
        if var.get():
            self.selected.add(path)
        else:
            self.selected.discard(path)
        self._update_footer()

    def _select_all(self):
        for path, var in self._file_vars.items():
            var.set(True)
            self.selected.add(path)
        self._update_footer()

    def _select_none(self):
        self.selected.clear()
        for var in self._file_vars.values():
            var.set(False)
        self._update_footer()

    def _update_footer(self):
        if self.pick_files:
            n = len(self.selected)
            txt = f"Đang xem: {shorten(str(self.cur), 56)}\n"
            txt += f"Đã chọn {n} file" if n else "Tích vào các file PDF cần thêm"
            if self.btn_ok is not None:
                self.btn_ok.configure(text=f"Thêm {n} file" if n else self.confirm_text,
                                      state="normal" if n else "disabled")
        else:
            txt = f"Đang chọn: {shorten(str(self.cur), 56)}"
            if self.note:
                txt += f"\n{self.note}"
        self.lbl_sel.configure(text=txt)

    # --- tạo thư mục mới ----------------------------------------------------------
    def toggle_new(self):
        if self._new_open:
            self._new_open = False
            self.newrow.grid_remove()
            return
        self._new_open = True
        self.var_new.set("")
        self.newrow.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.ent_new.focus_set()

    def create_folder(self):
        name = self.var_new.get().strip()
        bad = '<>:"/\\|?*' if sys.platform.startswith("win") else "/"
        if not name or name in (".", "..") or any(c in name for c in bad):
            self._err("Tên thư mục không hợp lệ.")
            return
        target = self.cur / name
        if target.exists():
            self._err("Đã có thư mục hoặc file trùng tên này.")
            return
        try:
            target.mkdir()
        except OSError as e:
            self._err(f"Không tạo được thư mục: {e.strerror or e}")
            return
        self.toggle_new()
        self.navigate(target, quiet=True)
        self._err(f"Đã tạo “{name}” và mở thư mục đó.", ok=True)

    # --- kết thúc -----------------------------------------------------------------
    def _grab(self):
        try:
            self.grab_set()
            self.lift()
            self.focus_set()
        except tk.TclError:
            self._grab_tries += 1
            if self._grab_tries < 20:
                self.after(100, self._grab)

    def _ok(self):
        if self.pick_files:
            if not self.selected:
                return
            self.result = sorted(self.selected, key=str.lower)
        else:
            self.result = str(self.cur)
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()

    def show(self):
        self.wait_window(self)
        return self.result


class FileScopeDialog(ctk.CTkToplevel):
    """Chọn các file sẽ áp dụng một vùng xóa."""

    def __init__(self, master, files, checked):
        super().__init__(master, fg_color=BG)
        self.title("Chọn các file áp dụng")
        self.minsize(440, 400)
        self.result = None
        self.vars = {}
        self._tries = 0
        ctk.CTkLabel(self, text="Vùng này sẽ được xóa trên các file được tích", font=font(13, True),
                     text_color=TEXT).pack(anchor="w", padx=16, pady=(16, 8))
        lst = ctk.CTkScrollableFrame(self, fg_color=CARD, corner_radius=14, border_width=1,
                                     border_color=BORDER, scrollbar_button_color="#E7E5E4",
                                     scrollbar_button_hover_color="#D6D3D1")
        lst.pack(fill="both", expand=True, padx=16)
        for f in files:
            var = tk.BooleanVar(value=f in checked)
            self.vars[f] = var
            ctk.CTkCheckBox(lst, text=shorten(Path(f).name, 46), variable=var, checkbox_width=20,
                            checkbox_height=20, corner_radius=6, fg_color=ORANGE, hover_color=ORANGE_HOVER,
                            border_color="#D6D3D1", checkmark_color="white", text_color=TEXT,
                            font=font(13)).pack(anchor="w", padx=10, pady=5)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(10, 14))
        btn_soft(bar, "Tích hết", lambda: self._set_all(True), height=34, width=80).pack(side="left", padx=(0, 6))
        btn_soft(bar, "Bỏ hết", lambda: self._set_all(False), height=34, width=80).pack(side="left")
        btn_primary(bar, "Áp dụng", self._ok, height=34, width=100).pack(side="right")
        btn_soft(bar, "Hủy", self._cancel, height=34, width=70).pack(side="right", padx=(0, 8))
        w, h = 460, 460
        self.update_idletasks()
        x = master.winfo_rootx() + max(0, (master.winfo_width() - w) // 2)
        y = master.winfo_rooty() + max(0, (master.winfo_height() - h) // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda e: self._cancel())
        self.after(150, self._grab)

    def _set_all(self, on):
        for v in self.vars.values():
            v.set(on)

    def _grab(self):
        try:
            self.grab_set()
            self.lift()
            self.focus_set()
        except tk.TclError:
            self._tries += 1
            if self._tries < 20:
                self.after(100, self._grab)

    def _ok(self):
        self.result = {f for f, v in self.vars.items() if v.get()}
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()

    def show(self):
        self.wait_window(self)
        return self.result


# ----------------------------------------------------------------------------
# KHUNG XEM TRƯỚC: cuộn liên tục hết file, vẽ vùng xóa bằng chuột
# ----------------------------------------------------------------------------

class PageView(ctk.CTkFrame):
    MARGIN = 28
    GAP = 18
    MIN_SCALE, MAX_SCALE = 0.25, 5.0
    CACHE_MAX = 24

    def __init__(self, master, app):
        super().__init__(master, fg_color=PREVIEW_BG, corner_radius=16)
        self.app = app
        self.canvas = tk.Canvas(self, bg=PREVIEW_BG, highlightthickness=0, bd=0,
                                yscrollincrement=30, xscrollincrement=30)
        self.vbar = ctk.CTkScrollbar(self, orientation="vertical", command=self.canvas.yview,
                                     button_color="#D6D3D1", button_hover_color="#A8A29E")
        self.hbar = ctk.CTkScrollbar(self, orientation="horizontal", command=self.canvas.xview,
                                     button_color="#D6D3D1", button_hover_color="#A8A29E")
        self.canvas.configure(yscrollcommand=self._on_yscroll, xscrollcommand=self.hbar.set)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=(10, 0))
        self.vbar.grid(row=0, column=1, sticky="ns", padx=(2, 6), pady=10)
        self.hbar.grid(row=1, column=0, sticky="ew", padx=10, pady=(2, 6))

        self.src = None
        self.odoc = None            # bản gốc
        self.cdoc = None            # bản đã dọn (dọn dần từng trang khi cần)
        self.cleaned = set()
        self.n = 0
        self.sizes = []             # (w, h) từng trang, đơn vị pt
        self.error = ""
        self._wm = None
        self._hf = None
        self.scale = 1.0
        self.zoom_fit = True
        self.px, self.tops = [], []
        self.total_h = 1
        self.content_w = 1
        self.version = 0
        self.cache = OrderedDict()
        self._need = set()
        self.view_mode = "clean"    # "clean" | "orig"
        self.show_frames = True
        self.tool = False
        self._drag = None
        self._last_w = 0
        self._in_info = False       # chặn vẽ lồng khi đang phân tích
        self._jobs = {"redraw": None, "pump": None, "refresh": None, "resize": None}

        c = self.canvas
        c.bind("<Configure>", self._on_configure)
        c.bind("<Enter>", lambda e: c.focus_set())
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._motion)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<MouseWheel>", self._wheel)
        c.bind("<Button-4>", self._wheel)
        c.bind("<Button-5>", self._wheel)
        c.configure(cursor="fleur")

    # --- nạp / giải phóng ----------------------------------------------------
    def unload(self):
        for d in (self.odoc, self.cdoc):
            try:
                if d is not None:
                    d.close()
            except Exception:
                pass
        self.odoc = self.cdoc = None
        self.src = None
        self.n = 0
        self.sizes, self.px, self.tops = [], [], []
        self.cache.clear()
        self.cleaned.clear()
        self._need.clear()
        self._wm = self._hf = None
        self.error = ""

    def load(self, src):
        self.unload()
        self.src = src
        try:
            self.odoc = fitz.open(src)
            if self.odoc.needs_pass:
                raise RuntimeError("PDF có mật khẩu - không xem trước được")
            self.n = len(self.odoc)
            if self.n == 0:
                raise RuntimeError("PDF không có trang nào")
            self.sizes = [(p.rect.width, p.rect.height) for p in self.odoc]
            self.cdoc = fitz.open(src)
        except Exception as e:
            self.error = str(e)
            self.unload_docs_only()
        self.version += 1
        self.zoom_fit = True
        self.scale = self._fit_scale()
        self._relayout()
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        self.app.on_zoom(self.scale, self.zoom_fit)
        self.redraw()

    def unload_docs_only(self):
        for d in (self.odoc, self.cdoc):
            try:
                if d is not None:
                    d.close()
            except Exception:
                pass
        self.odoc = self.cdoc = None
        self.n = 0
        self.sizes = []

    def clear(self):
        self.unload()
        self.version += 1
        self._relayout()
        self.redraw()
        self.app.on_page(0, 0)

    # --- phân tích (lười, có cache) ------------------------------------------
    def _info(self, opts):
        busy = False
        if opts["watermark"] and self._wm is None:
            busy = True
        if opts["mode"] == "auto" and self._hf is None:
            busy = True
        if busy:
            self.canvas.configure(cursor="watch")
            self.app.set_info("Đang phân tích…")
            self._in_info = True
            try:
                self.update_idletasks()
            finally:
                self._in_info = False
        if opts["watermark"] and self._wm is None:
            try:
                self._wm = detect_watermarks(self.odoc)
            except Exception:
                self._wm = set()
        if opts["mode"] == "auto" and self._hf is None:
            try:
                self._hf = collect_hf(self.odoc)
            except Exception:
                self._hf = [[] for _ in range(self.n)]
        if busy:
            self.canvas.configure(cursor="crosshair" if self.tool else "fleur")
        if opts["watermark"] and self._wm is not None:
            self.app.set_info(f"Logo chìm tìm thấy: {len(self._wm)}")
        else:
            self.app.set_info("")
        return {"wm": self._wm if opts["watermark"] else set(),
                "hf": self._hf if opts["mode"] == "auto" else None}

    def _zones(self, pno):
        opts = self.app.preview_opts()
        w, h = self.sizes[pno]
        info = self._info(opts) if opts["mode"] == "auto" else {"hf": None}
        hf = hf_rects(w, h, pno, info, opts)
        cu = custom_zones(w, h, pno, self.n, opts["regions"])
        return hf, cu

    # --- bố cục ---------------------------------------------------------------
    def _fit_scale(self):
        if not self.n:
            return 1.0
        cw = self.canvas.winfo_width()
        if cw <= 1:
            cw = 700
        maxw = max(w for w, _ in self.sizes)
        s = (cw - 2 * self.MARGIN) / maxw
        return min(self.MAX_SCALE, max(self.MIN_SCALE, s))

    def _relayout(self):
        c = self.canvas
        if not self.n:
            self.px, self.tops, self.total_h = [], [], 1
            self.content_w = max(c.winfo_width(), 1)
            c.configure(scrollregion=(0, 0, self.content_w, max(c.winfo_height(), 1)))
            self.hbar.grid_remove()
            return
        s = self.scale
        self.px = [(max(1, int(w * s)), max(1, int(h * s))) for (w, h) in self.sizes]
        y = self.MARGIN
        tops = []
        for (_, h) in self.px:
            tops.append(y)
            y += h + self.GAP
        self.tops = tops
        self.total_h = y - self.GAP + self.MARGIN
        maxw = max(w for w, _ in self.px)
        cw = max(c.winfo_width(), 1)
        self.content_w = max(cw, maxw + 2 * self.MARGIN)
        c.configure(scrollregion=(0, 0, self.content_w, self.total_h))
        if self.content_w > cw + 1:
            self.hbar.grid()
        else:
            self.hbar.grid_remove()

    def _anchor(self):
        if not self.n or not self.tops:
            return None
        y = self.canvas.canvasy(0)
        p = max(0, bisect_right(self.tops, y) - 1)
        return p, (y - self.tops[p]) / self.px[p][1]

    def _restore(self, anchor):
        if anchor is None or not self.tops:
            return
        p, ratio = anchor
        p = min(p, len(self.tops) - 1)
        y = self.tops[p] + ratio * self.px[p][1]
        self.canvas.yview_moveto(max(0.0, y / self.total_h))

    def _set_scale(self, s):
        s = min(self.MAX_SCALE, max(self.MIN_SCALE, s))
        a = self._anchor()
        self.scale = s
        self._relayout()
        self._restore(a)
        self.app.on_zoom(self.scale, self.zoom_fit)
        self.redraw()

    def zoom_by(self, f):
        if not self.n:
            return
        self.zoom_fit = False
        self._set_scale(self.scale * f)

    def zoom_to_fit(self):
        if not self.n:
            return
        self.zoom_fit = True
        self._set_scale(self._fit_scale())

    def goto(self, p):
        if not self.n:
            return
        p = max(0, min(self.n - 1, p))
        self.canvas.yview_moveto(max(0.0, (self.tops[p] - self.GAP) / self.total_h))
        self.sched_redraw()

    def current_page(self):
        if not self.n or not self.tops:
            return 0
        c = self.canvas
        y = c.canvasy(0) + 0.3 * max(c.winfo_height(), 1)
        return max(0, min(self.n - 1, bisect_right(self.tops, y) - 1))

    # --- trạng thái từ bên ngoài ---------------------------------------------
    def set_tool(self, on):
        self.tool = on
        self.canvas.configure(cursor="crosshair" if on else "fleur")

    def set_view_mode(self, mode):
        self.view_mode = mode
        self.version += 1
        self.cache.clear()
        self.redraw()

    def set_show_frames(self, on):
        self.show_frames = on
        self.sched_redraw()

    def request_refresh(self):
        """Cài đặt đổi: dựng lại nội dung sau một nhịp ngắn (để kéo thanh trượt mượt)."""
        if self._jobs["refresh"]:
            self.after_cancel(self._jobs["refresh"])
        self._jobs["refresh"] = self.after(180, self._do_refresh)

    def _do_refresh(self):
        self._jobs["refresh"] = None
        self.invalidate()

    def invalidate(self):
        self.version += 1
        self.cache.clear()
        self._need.clear()
        self.cleaned.clear()
        if self.src and self.n:
            try:
                if self.cdoc is not None:
                    self.cdoc.close()
                self.cdoc = fitz.open(self.src)
            except Exception as e:
                self.error = str(e)
        self.redraw()

    # --- vẽ -------------------------------------------------------------------
    def sched_redraw(self):
        if self._jobs["redraw"] is None:
            self._jobs["redraw"] = self.after_idle(self._do_redraw)

    def _do_redraw(self):
        self._jobs["redraw"] = None
        self.redraw()

    def _on_yscroll(self, a, b):
        self.vbar.set(a, b)
        self.sched_redraw()

    def _page_x0(self, p):
        return (self.content_w - self.px[p][0]) / 2

    def redraw(self):
        if self._in_info:
            return
        c = self.canvas
        c.delete("all")
        if not self.n:
            cw, ch = max(c.winfo_width(), 1), max(c.winfo_height(), 1)
            cx, cy = c.canvasx(0) + cw / 2, c.canvasy(0) + ch / 2
            if self.error:
                c.create_text(cx, cy, text=self.error, fill=DANGER, font=font(14, True), width=cw - 60)
            else:
                c.create_text(cx, cy - 12, text="Chưa có file nào", fill="#57534E", font=font(18, True))
                c.create_text(cx, cy + 18, text="Bấm “Thêm PDF…” để bắt đầu", fill="#78716C", font=font(12))
            return
        top = c.canvasy(0)
        bot = top + max(c.winfo_height(), 1)
        first = max(0, bisect_right(self.tops, top) - 1)
        last = first
        while last + 1 < self.n and self.tops[last + 1] < bot:
            last += 1
        for pno in range(first, last + 1):
            self._draw_page(pno)
        self._draw_rb()
        self.app.on_page(self.current_page(), self.n)
        if self._need and self._jobs["pump"] is None:
            self._jobs["pump"] = self.after(8, self._pump)

    def _draw_page(self, pno):
        c = self.canvas
        y0 = self.tops[pno]
        w, h = self.px[pno]
        x0 = self._page_x0(pno)
        c.create_rectangle(x0 + 3, y0 + 3, x0 + w + 3, y0 + h + 3, fill="#D6D3D1", outline="")
        c.create_rectangle(x0, y0, x0 + w, y0 + h, fill="white", outline="#D6D3D1")
        ent = self.cache.get(pno)
        if ent and ent[1] == self.scale and ent[2] == self.version:
            self.cache.move_to_end(pno)
            c.create_image(x0, y0, anchor="nw", image=ent[0])
        else:
            c.create_text(x0 + w / 2, y0 + h / 2, text="Đang tải…", fill="#A8A29E", font=font(12))
            self._need.add(pno)
        if self.show_frames:
            try:
                hf, cu = self._zones(pno)
            except Exception:
                hf, cu = [], []
            s = self.scale
            for r in hf:
                c.create_rectangle(x0 + r.x0 * s, y0 + r.y0 * s, x0 + r.x1 * s, y0 + r.y1 * s,
                                   outline=DANGER, dash=(5, 3), width=2)
            for idx, r in cu:
                sel = idx == self.app.sel_region
                col = "#DC2626" if sel else ORANGE
                bx0, by0, bx1, by1 = x0 + r.x0 * s, y0 + r.y0 * s, x0 + r.x1 * s, y0 + r.y1 * s
                c.create_rectangle(bx0, by0, bx1, by1, outline=col, width=3 if sel else 2)
                c.create_rectangle(bx0, by0, bx0 + 20, by0 + 17, fill=col, outline="")
                c.create_text(bx0 + 10, by0 + 8, text=str(idx + 1), fill="white", font=font(9, True))

    def _draw_rb(self):
        c = self.canvas
        c.delete("rb")
        d = self._drag
        if d:
            c.create_rectangle(d["x"], d["y"], d["cx"], d["cy"], outline=ORANGE, width=2,
                               dash=(4, 3), tags="rb")

    def _pump(self):
        self._jobs["pump"] = None
        if not self._need or not self.n:
            return
        c = self.canvas
        top = c.canvasy(0)
        bot = top + max(c.winfo_height(), 1)
        vis = [p for p in sorted(self._need)
               if p < self.n and self.tops[p] < bot and self.tops[p] + self.px[p][1] > top]
        self._need.clear()
        if not vis:
            return
        self._render(vis[0])
        self.redraw()

    def _render(self, pno):
        scale = self.scale
        try:
            opts = self.app.preview_opts()
            if self.view_mode == "clean":
                if pno not in self.cleaned:
                    self.cleaned.add(pno)
                    clean_page(self.cdoc, pno, self._info(opts), opts)
                page = self.cdoc[pno]
            else:
                page = self.odoc[pno]
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            if self.view_mode == "orig":
                self._tint(img, pno)
        except Exception:
            img = Image.new("RGB", self.px[pno], "white")
        photo = ImageTk.PhotoImage(img)
        self.cache[pno] = (photo, scale, self.version)
        self.cache.move_to_end(pno)
        while len(self.cache) > self.CACHE_MAX:
            self.cache.popitem(last=False)

    def _tint(self, img, pno):
        """Chế độ xem bản gốc: nhuộm đỏ nhạt các vùng sẽ bị xóa."""
        hf, cu = self._zones(pno)
        s = self.scale
        for r in list(hf) + [z for _, z in cu]:
            box = (max(0, int(r.x0 * s)), max(0, int(r.y0 * s)),
                   min(img.width, int(r.x1 * s) + 1), min(img.height, int(r.y1 * s) + 1))
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            crop = img.crop(box)
            red = Image.new("RGB", crop.size, (239, 68, 68))
            img.paste(Image.blend(crop, red, 0.28), box)

    # --- sự kiện ---------------------------------------------------------------
    def _on_configure(self, e):
        if e.width != self._last_w:
            self._last_w = e.width
            if self._jobs["resize"]:
                self.after_cancel(self._jobs["resize"])
            self._jobs["resize"] = self.after(100, self._do_resize)
        else:
            self.sched_redraw()

    def _do_resize(self):
        self._jobs["resize"] = None
        if not self.n:
            self.redraw()
            return
        self._set_scale(self._fit_scale() if self.zoom_fit else self.scale)

    def _wheel(self, e):
        if not self.n:
            return
        ctrl = bool(e.state & 0x4)
        shift = bool(e.state & 0x1)
        if e.num == 4:
            steps = -1.0
        elif e.num == 5:
            steps = 1.0
        elif sys.platform == "darwin":
            steps = -float(e.delta)
        else:
            steps = -e.delta / 120.0
        if ctrl:
            self.zoom_by(1.1 ** (-steps))
        elif shift:
            self.canvas.xview_scroll(int(steps * 4) or (1 if steps > 0 else -1), "units")
        else:
            self.canvas.yview_scroll(int(steps * 4) or (1 if steps > 0 else -1), "units")

    def _page_at(self, cy):
        if not self.tops:
            return None
        i = bisect_right(self.tops, cy) - 1
        if i < 0:
            return None
        return i if cy <= self.tops[i] + self.px[i][1] else None

    def _press(self, e):
        c = self.canvas
        if not self.tool:
            c.scan_mark(e.x, e.y)
            return
        if not self.n:
            return
        cx, cy = c.canvasx(e.x), c.canvasy(e.y)
        p = self._page_at(cy)
        if p is None:
            return
        x0 = self._page_x0(p)
        if not (x0 <= cx <= x0 + self.px[p][0]):
            return
        self._drag = {"p": p, "x": cx, "y": cy, "cx": cx, "cy": cy}

    def _motion(self, e):
        c = self.canvas
        if not self.tool:
            c.scan_dragto(e.x, e.y, gain=1)
            return
        d = self._drag
        if not d:
            return
        p = d["p"]
        x0 = self._page_x0(p)
        w, h = self.px[p]
        y0 = self.tops[p]
        d["cx"] = min(max(c.canvasx(e.x), x0), x0 + w)
        d["cy"] = min(max(c.canvasy(e.y), y0), y0 + h)
        self._draw_rb()

    def _release(self, e):
        d = self._drag
        self._drag = None
        self.canvas.delete("rb")
        if not d or not self.tool:
            return
        x0, x1 = sorted((d["x"], d["cx"]))
        y0, y1 = sorted((d["y"], d["cy"]))
        if x1 - x0 < 6 or y1 - y0 < 6:
            return
        p = d["p"]
        px0, py0 = self._page_x0(p), self.tops[p]
        w, h = self.px[p]
        fr = ((x0 - px0) / w, (y0 - py0) / h, (x1 - px0) / w, (y1 - py0) / h)
        self.app.add_region(p, fr)


# ----------------------------------------------------------------------------
# ỨNG DỤNG
# ----------------------------------------------------------------------------

class App(_Root):
    def __init__(self):
        ctk.set_appearance_mode("light")
        super().__init__(fg_color=BG)
        self.title("PDF Cleaner")
        self.geometry("1360x860")
        self.minsize(1100, 700)

        self.files = []
        self.sel = None
        self.regions = {}           # đường dẫn file -> list vùng tùy chỉnh (riêng từng file)
        self.shared = []            # vùng áp dụng cho nhiều file
        self.region_hist = []       # thứ tự vẽ, để hoàn tác
        self.file_pick = set()      # file đã chọn cho mục "Chọn file…"
        self.sel_region = None
        self.file_rows = []
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.running = False
        self.last_out_dir = None
        self.last_dir = str(Path.home())    # thư mục mở gần nhất khi chọn file
        self.recent_out = []                # các thư mục lưu đã dùng

        self.var_wm = tk.BooleanVar(value=True)
        self.var_img = tk.BooleanVar(value=False)
        self.var_mode = tk.StringVar(value="auto")
        self.var_h = tk.DoubleVar(value=4.5)
        self.var_f = tk.DoubleVar(value=4.0)
        self.var_fill = tk.StringVar(value="white")
        self.var_tool = tk.BooleanVar(value=False)
        self.var_scope = tk.StringVar(value="Chỉ trang này")
        self.var_range = tk.StringVar(value="")
        self.var_fscope = tk.StringVar(value="Chỉ file này")
        self.var_out_mode = tk.StringVar(value="beside")
        self.var_out = tk.StringVar(value="")

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._build_right()
        self.bind("<Escape>", lambda e: self._set_tool(False))
        self.bind("<Control-z>", lambda e: self.undo_region())
        self.bind("<Delete>", self._on_delete_key)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._render_files()
        self._render_regions()
        self.var_out.trace_add("write", lambda *a: self._update_out_hint())
        self._update_out_hint()
        self._setup_dnd()
        self.after(100, self._poll)

    # --- dựng giao diện --------------------------------------------------------
    def _card(self, title, subtitle=None):
        f = ctk.CTkFrame(self.side, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER)
        f.pack(fill="x", padx=(0, 4), pady=(0, 12))
        ctk.CTkLabel(f, text=title, font=font(14, True), text_color=TEXT).pack(anchor="w", padx=16, pady=(14, 2 if subtitle else 8))
        if subtitle:
            ctk.CTkLabel(f, text=subtitle, font=font(11), text_color=MUTED, wraplength=330,
                         justify="left").pack(anchor="w", padx=16, pady=(0, 8))
        return f

    def _build_sidebar(self):
        self.side = ctk.CTkScrollableFrame(self, width=372, fg_color="transparent", corner_radius=0,
                                           scrollbar_button_color="#FDBA74",
                                           scrollbar_button_hover_color=ORANGE)
        self.side.grid(row=0, column=0, sticky="ns", padx=(14, 6), pady=(10, 14))

        head = ctk.CTkFrame(self.side, fg_color="transparent")
        head.pack(fill="x", pady=(0, 12), padx=2)
        logo = ctk.CTkLabel(head, text="PDF", width=44, height=44, corner_radius=12, fg_color=ORANGE,
                            text_color="white", font=font(15, True))
        logo.pack(side="left")
        tb = ctk.CTkFrame(head, fg_color="transparent")
        tb.pack(side="left", padx=12)
        ctk.CTkLabel(tb, text="PDF Cleaner", font=font(20, True), text_color=TEXT).pack(anchor="w")
        ctk.CTkLabel(tb, text="Gỡ logo chìm, header, footer, vùng tùy chọn", font=font(11),
                     text_color=MUTED).pack(anchor="w")

        # 1. File
        c1 = self._card("1 · Tệp PDF")
        self.zone = DropZone(c1, on_click=self.add_files, dnd=HAS_DND)
        self.zone.pack(fill="x", padx=16)
        zrow = ctk.CTkFrame(c1, fg_color="transparent")
        zrow.pack(fill="x", padx=16, pady=(6, 0))
        self.lbl_add = ctk.CTkLabel(zrow, text="", font=font(11), text_color=MUTED, anchor="w",
                                    justify="left", wraplength=320)
        self.lbl_add.pack(side="left", fill="x", expand=True)
        self.file_list = ctk.CTkScrollableFrame(c1, height=150, fg_color="#FAFAF9", corner_radius=12,
                                                scrollbar_button_color="#E7E5E4",
                                                scrollbar_button_hover_color="#D6D3D1")
        self.file_list.pack(fill="x", padx=16, pady=(10, 6))
        bar2 = ctk.CTkFrame(c1, fg_color="transparent")
        bar2.pack(fill="x", padx=16, pady=(0, 14))
        btn_soft(bar2, "Bỏ file đang chọn", self.remove_selected, height=28).pack(side="left", expand=True, fill="x", padx=(0, 5))
        btn_soft(bar2, "Xóa hết", self.clear_files, height=28).pack(side="left", expand=True, fill="x")

        # 2. Làm sạch
        c2 = self._card("2 · Làm sạch")
        body = ctk.CTkFrame(c2, fg_color="transparent")
        body.pack(fill="x", padx=16, pady=(0, 14))
        switch(body, "Gỡ logo chìm (ảnh lớn lặp nhiều trang)", self.var_wm, self._on_setting).pack(anchor="w", pady=3)
        switch(body, "Xóa cả pixel ảnh (PDF scan)", self.var_img, self._on_setting).pack(anchor="w", pady=3)
        ctk.CTkLabel(body, text="Header / footer", font=font(12, True), text_color=MUTED).pack(anchor="w", pady=(10, 4))
        self.seg_mode = segmented(body, list(MODES), self._on_mode, "Tự động")
        self.seg_mode.pack(fill="x")
        self.mode_body = ctk.CTkFrame(body, fg_color="transparent")
        self.mode_body.pack(fill="x", pady=(8, 0))
        self.auto_hint = ctk.CTkLabel(self.mode_body, justify="left", wraplength=320, font=font(11),
                                      text_color=MUTED,
                                      text="Tự tìm chữ lặp lại ở 12% trên/dưới của các trang. "
                                           "Không áp dụng cho trang scan (không có chữ) - hãy dùng “Theo %”.")
        self.auto_hint.grid(row=0, column=0, sticky="w")
        self.manual = ctk.CTkFrame(self.mode_body, fg_color="transparent")
        self.manual.grid(row=0, column=0, sticky="ew")
        self.sl_h, self.ent_h = self._pct_row(self.manual, "Header", "h", self.var_h)
        self.sl_f, self.ent_f = self._pct_row(self.manual, "Footer", "f", self.var_f)
        self.manual.grid_remove()
        ctk.CTkLabel(body, text="Màu tô vùng đã xóa", font=font(12, True), text_color=MUTED).pack(anchor="w", pady=(10, 4))
        self.seg_fill = segmented(body, ["Trắng", "Theo nền giấy"], self._on_fill, "Trắng")
        self.seg_fill.pack(fill="x")

        # 3. Vùng tùy chỉnh
        c3 = self._card("3 · Xóa vùng tùy chỉnh",
                        "Kéo chuột trên trang xem trước để chọn vùng. Có thể áp dụng cho nhiều trang và nhiều file cùng lúc.")
        b3 = ctk.CTkFrame(c3, fg_color="transparent")
        b3.pack(fill="x", padx=16, pady=(0, 14))
        self.btn_tool = ctk.CTkButton(b3, text="✂  Bật chế độ vẽ vùng", height=38, corner_radius=10,
                                      fg_color=ORANGE_SOFT, hover_color=ORANGE_SOFT_HOVER,
                                      text_color=ORANGE_DARK, font=font(13, True),
                                      command=lambda: self._set_tool(not self.var_tool.get()))
        self.btn_tool.pack(fill="x")
        row = ctk.CTkFrame(b3, fg_color="transparent")
        row.pack(fill="x", pady=(10, 0))
        ctk.CTkLabel(row, text="Áp dụng cho", font=font(12), text_color=TEXT).pack(side="left")
        self.opt_scope = ctk.CTkOptionMenu(row, values=list(SCOPES), variable=self.var_scope,
                                           command=self._on_scope, corner_radius=10, height=30,
                                           fg_color=ORANGE_SOFT, button_color=ORANGE,
                                           button_hover_color=ORANGE_HOVER, text_color=TEXT,
                                           dropdown_fg_color="white", dropdown_hover_color=ORANGE_SOFT,
                                           dropdown_text_color=TEXT, font=font(12), dropdown_font=font(12))
        self.opt_scope.pack(side="right")
        row_f = ctk.CTkFrame(b3, fg_color="transparent")
        row_f.pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(row_f, text="Áp dụng cho file", font=font(12), text_color=TEXT).pack(side="left")
        self.opt_fscope = ctk.CTkOptionMenu(row_f, values=list(FILE_SCOPES), variable=self.var_fscope,
                                            command=self._on_fscope, corner_radius=10, height=30,
                                            fg_color=ORANGE_SOFT, button_color=ORANGE,
                                            button_hover_color=ORANGE_HOVER, text_color=TEXT,
                                            dropdown_fg_color="white", dropdown_hover_color=ORANGE_SOFT,
                                            dropdown_text_color=TEXT, font=font(12), dropdown_font=font(12))
        self.opt_fscope.pack(side="right")
        self.row_f = row_f
        self.lbl_fpick = ctk.CTkLabel(b3, text="", font=font(11, True, underline=True),
                                      text_color=ORANGE_DARK, cursor="hand2", anchor="w")
        self.lbl_fpick.bind("<Button-1>", lambda e: self._pick_files())
        self.ent_range = ctk.CTkEntry(b3, textvariable=self.var_range, height=30, corner_radius=8,
                                      border_color=FIELD, fg_color="white", text_color=TEXT,
                                      placeholder_text="Ví dụ: 1-5, 8, 10-", font=font(12))
        ctk.CTkLabel(b3, text="Các vùng áp dụng cho file đang chọn", font=font(12, True),
                     text_color=MUTED).pack(anchor="w", pady=(12, 4))
        self.reg_list = ctk.CTkScrollableFrame(b3, height=110, fg_color="#FAFAF9", corner_radius=12,
                                               scrollbar_button_color="#E7E5E4",
                                               scrollbar_button_hover_color="#D6D3D1")
        self.reg_list.pack(fill="x")
        r2 = ctk.CTkFrame(b3, fg_color="transparent")
        r2.pack(fill="x", pady=(8, 0))
        btn_soft(r2, "↶ Hoàn tác (Ctrl+Z)", self.undo_region, height=28).pack(side="left", expand=True, fill="x", padx=(0, 5))
        btn_soft(r2, "Xóa hết vùng", self.clear_regions, height=28).pack(side="left", expand=True, fill="x")

        # 4. Xuất
        c4 = self._card("4 · Xuất file")
        b4 = ctk.CTkFrame(c4, fg_color="transparent")
        b4.pack(fill="x", padx=16, pady=(0, 14))
        self.seg_out = segmented(b4, ["Cạnh file gốc", "Thư mục khác"], self._on_out_mode, "Cạnh file gốc")
        self.seg_out.pack(fill="x")
        self.out_row = ctk.CTkFrame(b4, fg_color="transparent")
        orow = ctk.CTkFrame(self.out_row, fg_color="transparent")
        orow.pack(fill="x")
        self.ent_out = ctk.CTkEntry(orow, textvariable=self.var_out, height=34, corner_radius=8,
                                    border_color=FIELD, fg_color="white", text_color=TEXT, font=font(12),
                                    placeholder_text="Chọn hoặc kéo thả thư mục…" if HAS_DND
                                    else "Chọn thư mục lưu…")
        self.ent_out.pack(side="left", fill="x", expand=True, padx=(0, 6))
        btn_soft(orow, "Chọn…", self.pick_out, height=34, width=76).pack(side="left")
        self.lbl_out_hint = ctk.CTkLabel(self.out_row, text="", font=font(11), text_color=MUTED,
                                         anchor="w", justify="left", wraplength=320)
        self.lbl_out_hint.pack(fill="x", pady=(4, 0))
        ctk.CTkLabel(b4, text="Tên file kết quả: <tên gốc>_clean.pdf", font=font(11),
                     text_color=MUTED).pack(anchor="w", pady=(8, 8))
        self.btn_run = btn_primary(b4, "▶  Làm sạch tất cả", self.run, height=42)
        self.btn_run.pack(fill="x")
        self.progress = ctk.CTkProgressBar(b4, height=10, corner_radius=6, progress_color=ORANGE, fg_color=FIELD)
        self.progress.set(0)
        self.progress.pack(fill="x", pady=(12, 6))
        self.log = ctk.CTkTextbox(b4, height=96, corner_radius=10, fg_color="#FAFAF9", text_color=TEXT,
                                  border_width=1, border_color="#F5F5F4", font=font(11), wrap="word")
        self.log.configure(state="disabled")
        self.log.pack(fill="x")
        self.btn_open = btn_soft(b4, "Mở thư mục kết quả", self._open_out, height=30)

    def _pct_row(self, parent, label, which, var):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=3)
        ctk.CTkLabel(row, text=label, width=48, anchor="w", font=font(12), text_color=TEXT).pack(side="left")
        sl = ctk.CTkSlider(row, from_=0, to=HF_MAX_PCT, number_of_steps=int(HF_MAX_PCT * 10), height=18,
                           progress_color=ORANGE, button_color=ORANGE, button_hover_color=ORANGE_HOVER,
                           fg_color=FIELD, command=lambda v, w=which: self._hf_slider(w, v))
        sl.set(var.get())
        sl.pack(side="left", fill="x", expand=True, padx=8)
        ent = ctk.CTkEntry(row, width=58, height=28, corner_radius=8, justify="center",
                           border_color=FIELD, fg_color="white", text_color=TEXT, font=font(12))
        ent.insert(0, f"{var.get():.1f}")
        ent.pack(side="left")
        ctk.CTkLabel(row, text="%", font=font(12), text_color=MUTED).pack(side="left", padx=(3, 0))
        ent.bind("<Return>", lambda e, w=which: self._hf_commit(w))
        ent.bind("<FocusOut>", lambda e, w=which: self._hf_commit(w))
        ent.bind("<Up>", lambda e, w=which: self._hf_step(w, 0.1))
        ent.bind("<Down>", lambda e, w=which: self._hf_step(w, -0.1))
        return sl, ent

    def _build_right(self):
        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 14), pady=14)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        tb = ctk.CTkFrame(right, fg_color=CARD, corner_radius=14, border_width=1, border_color=BORDER, height=50)
        tb.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        tb.grid_propagate(False)
        inner = ctk.CTkFrame(tb, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=10, pady=8)

        nav = ctk.CTkFrame(inner, fg_color="transparent")
        nav.pack(side="left")
        small = dict(width=30, height=30, corner_radius=8, fg_color=ORANGE_SOFT, hover_color=ORANGE_SOFT_HOVER,
                     text_color=ORANGE_DARK, font=font(12, True))
        ctk.CTkButton(nav, text="◀", command=lambda: self.view.goto(self.view.current_page() - 1), **small).pack(side="left")
        self.ent_page = ctk.CTkEntry(nav, width=54, height=30, corner_radius=8, justify="center",
                                     border_color=FIELD, fg_color="white", text_color=TEXT, font=font(12))
        self.ent_page.pack(side="left", padx=6)
        self.ent_page.bind("<Return>", self._goto_entry)
        self.lbl_pages = ctk.CTkLabel(nav, text="/ 0", font=font(12), text_color=MUTED)
        self.lbl_pages.pack(side="left", padx=(0, 6))
        ctk.CTkButton(nav, text="▶", command=lambda: self.view.goto(self.view.current_page() + 1), **small).pack(side="left")

        self.seg_view = segmented(inner, ["Kết quả", "Bản gốc"], self._on_view_mode, "Kết quả")
        self.seg_view.pack(side="left", padx=14)
        self.var_frames = tk.BooleanVar(value=True)
        switch(inner, "Hiện khung", self.var_frames, lambda: self.view.set_show_frames(self.var_frames.get())).pack(side="left")
        self.lbl_info = ctk.CTkLabel(inner, text="", font=font(11), text_color=MUTED)
        self.lbl_info.pack(side="left", padx=12)

        zoom = ctk.CTkFrame(inner, fg_color="transparent")
        zoom.pack(side="right")
        ctk.CTkButton(zoom, text="−", command=lambda: self.view.zoom_by(1 / 1.2), **small).pack(side="left")
        self.lbl_zoom = ctk.CTkLabel(zoom, text="100%", width=52, font=font(12), text_color=TEXT)
        self.lbl_zoom.pack(side="left")
        ctk.CTkButton(zoom, text="+", command=lambda: self.view.zoom_by(1.2), **small).pack(side="left")
        ctk.CTkButton(zoom, text="Vừa khít", command=lambda: self.view.zoom_to_fit(), width=70, height=30,
                      corner_radius=8, fg_color=ORANGE_SOFT, hover_color=ORANGE_SOFT_HOVER,
                      text_color=ORANGE_DARK, font=font(12, True)).pack(side="left", padx=(6, 0))

        self.view = PageView(right, self)
        self.view.grid(row=1, column=0, sticky="nsew")

    # --- callback từ khung xem trước -----------------------------------------
    def on_page(self, cur, n):
        self.lbl_pages.configure(text=f"/ {n}")
        if self.focus_get() is not self.ent_page._entry:
            self.ent_page.delete(0, "end")
            self.ent_page.insert(0, str(cur + 1) if n else "")

    def on_zoom(self, scale, fit):
        self.lbl_zoom.configure(text=f"{int(round(scale * 100))}%")

    def set_info(self, text):
        self.lbl_info.configure(text=text)

    def _goto_entry(self, _=None):
        try:
            self.view.goto(int(self.ent_page.get().strip()) - 1)
        except ValueError:
            pass
        self.view.canvas.focus_set()

    # --- tùy chọn --------------------------------------------------------------
    def opts(self, path=None):
        return {
            "watermark": self.var_wm.get(),
            "mode": self.var_mode.get(),
            "header": self.var_h.get(),
            "footer": self.var_f.get(),
            "img": self.var_img.get(),
            "fill": self.var_fill.get(),
            "regions": self.regions_for(path),
        }

    def cur_path(self):
        if self.sel is not None and 0 <= self.sel < len(self.files):
            return self.files[self.sel]
        return None

    def preview_opts(self):
        return self.opts(self.cur_path())

    def _on_setting(self):
        self.view.request_refresh()
        self.view.sched_redraw()

    def _on_mode(self, label):
        mode = MODES[label]
        self.var_mode.set(mode)
        self.auto_hint.grid_remove()
        self.manual.grid_remove()
        if mode == "auto":
            self.auto_hint.grid()
        elif mode == "manual":
            self.manual.grid()
        self._on_setting()

    def _on_fill(self, label):
        self.var_fill.set("paper" if label.startswith("Theo") else "white")
        self._on_setting()

    def _on_view_mode(self, label):
        self.view.set_view_mode("orig" if label == "Bản gốc" else "clean")

    def _hf_widgets(self, which):
        if which == "h":
            return self.var_h, self.sl_h, self.ent_h
        return self.var_f, self.sl_f, self.ent_f

    def _hf_set(self, which, v, from_slider=False):
        var, sl, ent = self._hf_widgets(which)
        v = max(0.0, min(HF_MAX_PCT, round(float(v), 1)))
        var.set(v)
        if not from_slider:
            sl.set(v)
        ent.delete(0, "end")
        ent.insert(0, f"{v:.1f}")
        self._on_setting()

    def _hf_slider(self, which, v):
        self._hf_set(which, v, from_slider=True)

    def _hf_commit(self, which):
        var, _, ent = self._hf_widgets(which)
        txt = ent.get().strip().replace(",", ".").replace("%", "")
        try:
            v = float(txt)
        except ValueError:
            v = var.get()
        if abs(v - var.get()) > 1e-9 or txt != f"{var.get():.1f}":
            self._hf_set(which, v)

    def _hf_step(self, which, d):
        var, _, _ = self._hf_widgets(which)
        self._hf_set(which, var.get() + d)
        return "break"

    # --- quản lý file ----------------------------------------------------------
    def add_files(self):
        res = FolderPicker(self, "Chọn file PDF", start=self.last_dir, confirm="Thêm file",
                           allow_new=False, pick_files=True, places=self._recent_places()).show()
        if res:
            self.add_paths(res)

    def add_folder(self):
        d = FolderPicker(self, "Thêm thư mục chứa PDF", start=self.last_dir,
                         confirm="Thêm PDF trong thư mục này", allow_new=False,
                         note="Sẽ thêm mọi PDF trong thư mục này và các thư mục con.",
                         places=self._recent_places()).show()
        if d:
            self.add_paths([d])

    def _scan_dir(self, d, limit=2000):
        """Mọi PDF trong thư mục (gồm thư mục con); bỏ qua file *_clean.pdf do chính tool xuất ra."""
        out = []
        for root, dirs, files in os.walk(d):
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            for name in sorted(files):
                low = name.lower()
                if low.endswith(".pdf") and not low.endswith("_clean.pdf"):
                    out.append(Path(root) / name)
                    if len(out) >= limit:
                        return out
        return out

    def add_paths(self, paths):
        """Nhận file và thư mục từ nút chọn hoặc kéo thả."""
        found, skipped = [], 0
        for raw in paths:
            p = Path(str(raw).strip())
            try:
                if p.is_dir():
                    self.last_dir = str(p)
                    found.extend(self._scan_dir(p))
                elif p.is_file() and p.suffix.lower() == ".pdf":
                    self.last_dir = str(p.parent)
                    found.append(p)
                else:
                    skipped += 1
            except OSError:
                skipped += 1
        added = dup = 0
        for f in found:
            if self._add(f):
                added += 1
            else:
                dup += 1
        if not found:
            msg = "Không tìm thấy file PDF nào."
        else:
            parts = []
            if added:
                parts.append(f"Đã thêm {added} file")
            if dup:
                parts.append(f"{dup} file đã có")
            if skipped:
                parts.append(f"bỏ qua {skipped} mục khác")
            msg = " · ".join(parts)
        self.lbl_add.configure(text=msg, text_color=ORANGE_DARK if added else MUTED)
        self._after_add()

    def _add(self, f):
        f = str(Path(f))
        if f in self.files:
            return False
        self.files.append(f)
        return True

    def _after_add(self):
        self._render_files()
        if self.sel is None and self.files:
            self.select(0)

    def select(self, i):
        if i is None or not (0 <= i < len(self.files)):
            self.sel = None
            self.sel_region = None
            self.view.clear()
            self._highlight_files()
            self._render_regions()
            return
        self.sel = i
        self.sel_region = None
        self._highlight_files()
        self._render_regions()
        self.set_info("")
        self.view.load(self.files[i])

    def remove_file(self, i):
        if not (0 <= i < len(self.files)):
            return
        path = self.files.pop(i)
        self.regions.pop(path, None)
        self._prune_shared()
        if self.sel is None:
            pass
        elif i < self.sel:
            self.sel -= 1
        elif i == self.sel:
            self.sel = None
        self._render_files()
        if self.sel is None:
            self.select(min(i, len(self.files) - 1) if self.files else None)
        else:
            self._highlight_files()

    def remove_selected(self):
        if self.sel is not None:
            self.remove_file(self.sel)

    def clear_files(self):
        self.files.clear()
        self.regions.clear()
        self.shared.clear()
        self.region_hist.clear()
        self.sel = None
        self._render_files()
        self.select(None)

    def _render_files(self):
        for w in self.file_list.winfo_children():
            w.destroy()
        self.file_rows = []
        if not self.files:
            ctk.CTkLabel(self.file_list, text="Chưa có file nào", font=font(12),
                         text_color=MUTED).pack(pady=24)
            return
        for i, path in enumerate(self.files):
            row = ctk.CTkFrame(self.file_list, corner_radius=10, fg_color="#FFFFFF", border_width=1,
                               border_color="#F5F5F4", height=34)
            row.pack(fill="x", pady=2)
            name = Path(path).name
            if len(name) > 24:
                name = name[:21] + "…"
            lab = ctk.CTkLabel(row, text=name, anchor="w", font=font(12), text_color=TEXT)
            lab.pack(side="left", fill="x", expand=True, padx=(10, 4), pady=6)
            size = ctk.CTkLabel(row, text=file_size_text(path), font=font(10), text_color=MUTED)
            size.pack(side="left", padx=2)
            badge = ctk.CTkLabel(row, text="", font=font(10, True), text_color=ORANGE_DARK)
            badge.pack(side="left", padx=2)
            x = ctk.CTkButton(row, text="✕", width=24, height=24, corner_radius=8, fg_color="transparent",
                              hover_color="#FECACA", text_color=MUTED, font=font(11, True),
                              command=lambda i=i: self.remove_file(i))
            x.pack(side="right", padx=(0, 6))
            for wdg in (row, lab, size, badge):
                wdg.bind("<Button-1>", lambda e, i=i: self.select(i))
            self.file_rows.append((row, badge))
        self._highlight_files()
        self._update_badges()

    def _highlight_files(self):
        for i, (row, _) in enumerate(self.file_rows):
            on = i == self.sel
            row.configure(fg_color=ORANGE_SOFT if on else "#FFFFFF",
                          border_color=ORANGE if on else "#F5F5F4")

    def _update_badges(self):
        for i, (_, badge) in enumerate(self.file_rows):
            k = len(self.regions_for(self.files[i]))
            badge.configure(text=f"✂ {k}" if k else "")

    # --- vùng tùy chỉnh --------------------------------------------------------
    def _set_tool(self, on):
        on = bool(on) and self.cur_path() is not None
        self.var_tool.set(on)
        self.view.set_tool(on)
        if on:
            self.btn_tool.configure(text="✂  Đang vẽ - kéo chuột trên trang (Esc để tắt)",
                                    fg_color=ORANGE, hover_color=ORANGE_HOVER, text_color="white")
        else:
            self.btn_tool.configure(text="✂  Bật chế độ vẽ vùng", fg_color=ORANGE_SOFT,
                                    hover_color=ORANGE_SOFT_HOVER, text_color=ORANGE_DARK)

    def _on_scope(self, label):
        if SCOPES[label] == "range":
            self.ent_range.pack(fill="x", pady=(8, 0), after=self.opt_scope.master)
        else:
            self.ent_range.pack_forget()

    def regions_for(self, path):
        """Vùng áp dụng cho file: vùng riêng + vùng dùng chung nhiều file."""
        if not path:
            return []
        shared = [r for r in self.shared if r["files"] == "all" or path in r["files"]]
        return list(self.regions.get(path, [])) + shared

    def _drop_rg(self, rg):
        for lst in self.regions.values():
            lst[:] = [r for r in lst if r is not rg]
        self.shared = [r for r in self.shared if r is not rg]

    def _prune_shared(self):
        """Bỏ file đã xóa khỏi các vùng dùng chung; vùng không còn file nào thì xóa."""
        keep = []
        for r in self.shared:
            if r["files"] == "all":
                keep.append(r)
                continue
            left = frozenset(x for x in r["files"] if x in self.files)
            if left:
                r["files"] = left
                keep.append(r)
        self.shared = keep

    def _file_tag(self, rg):
        f = rg.get("files", "this")
        if f == "all":
            return "mọi file"
        if f == "this":
            return ""
        return f"{len(f)} file"

    def _on_fscope(self, label):
        if FILE_SCOPES[label] == "pick":
            self._pick_files(first=True)
        else:
            self.lbl_fpick.pack_forget()

    def _pick_files(self, first=False):
        cur = self.cur_path()
        checked = {f for f in self.file_pick if f in self.files}
        if cur:
            checked.add(cur)
        res = FileScopeDialog(self, self.files, checked).show()
        if res is None or not res:
            if first and not self.file_pick:
                self.var_fscope.set("Chỉ file này")
                self.lbl_fpick.pack_forget()
            return
        self.file_pick = set(res)
        self.lbl_fpick.configure(text=f"{len(res)} file được chọn · bấm để đổi")
        self.lbl_fpick.pack(anchor="w", pady=(4, 0), after=self.row_f)

    def add_region(self, pno, fr):
        path = self.cur_path()
        if not path:
            return
        key = SCOPES[self.var_scope.get()]
        rg = {"page": pno, "rect": fr, "scope": key, "pages": None}
        if key == "range":
            try:
                pages = parse_ranges(self.var_range.get(), self.view.n)
            except ValueError as e:
                messagebox.showwarning("Khoảng trang không hợp lệ",
                                       f"{e}.\nVí dụ đúng: 1-5, 8, 10-")
                return
            if not pages:
                messagebox.showwarning("Khoảng trang trống",
                                       "Chưa nhập khoảng trang hoặc nằm ngoài số trang của file.")
                return
            rg["pages"] = frozenset(pages)
        fkey = FILE_SCOPES[self.var_fscope.get()]
        if fkey == "this":
            rg["files"] = "this"
            self.regions.setdefault(path, []).append(rg)
        elif fkey == "all":
            rg["files"] = "all"
            self.shared.append(rg)
        else:
            pick = {f for f in self.file_pick if f in self.files} | {path}
            if len(pick) < 1:
                return
            rg["files"] = frozenset(pick)
            self.shared.append(rg)
        self.region_hist.append(rg)
        self.sel_region = next((i for i, r in enumerate(self.regions_for(path)) if r is rg), None)
        self._render_regions()
        self._update_badges()
        self.view.request_refresh()
        self.view.sched_redraw()

    def _scope_text(self, rg):
        s = rg["scope"]
        if s == "this":
            return f"Trang {rg['page'] + 1}"
        if s == "all":
            return "Mọi trang"
        if s == "odd":
            return "Trang lẻ"
        if s == "even":
            return "Trang chẵn"
        pages = sorted(rg["pages"])
        txt = ", ".join(str(p + 1) for p in pages[:6]) + ("…" if len(pages) > 6 else "")
        return f"Trang {txt}"

    def _render_regions(self):
        for w in self.reg_list.winfo_children():
            w.destroy()
        path = self.cur_path()
        lst = self.regions_for(path)
        if not lst:
            ctk.CTkLabel(self.reg_list, text="Chưa có vùng nào", font=font(12),
                         text_color=MUTED).pack(pady=14)
            return
        for i, rg in enumerate(lst):
            on = i == self.sel_region
            row = ctk.CTkFrame(self.reg_list, corner_radius=10, height=32, border_width=1,
                               fg_color=ORANGE_SOFT if on else "#FFFFFF",
                               border_color=ORANGE if on else "#F5F5F4")
            row.pack(fill="x", pady=2)
            fx0, fy0, fx1, fy1 = rg["rect"]
            lab = ctk.CTkLabel(row, anchor="w", font=font(12), text_color=TEXT,
                               text=f"#{i + 1}  {self._scope_text(rg)}  ·  {(fx1 - fx0) * 100:.0f}×{(fy1 - fy0) * 100:.0f}%")
            lab.pack(side="left", fill="x", expand=True, padx=(10, 4), pady=5)
            ctk.CTkButton(row, text="✕", width=24, height=24, corner_radius=8, fg_color="transparent",
                          hover_color="#FECACA", text_color=MUTED, font=font(11, True),
                          command=lambda i=i: self.remove_region(i)).pack(side="right", padx=(0, 6))
            wl = [row, lab]
            tag = self._file_tag(rg)
            if tag:
                tl = ctk.CTkLabel(row, text=tag, font=font(10, True), text_color=ORANGE_DARK)
                tl.pack(side="right", padx=2)
                wl.append(tl)
            for wdg in wl:
                wdg.bind("<Button-1>", lambda e, i=i: self.select_region(i))

    def select_region(self, i):
        path = self.cur_path()
        lst = self.regions_for(path)
        if not (0 <= i < len(lst)):
            return
        self.sel_region = i
        rg = lst[i]
        self._render_regions()
        if rg["scope"] == "this":
            self.view.goto(rg["page"])
        self.view.sched_redraw()

    def remove_region(self, i):
        path = self.cur_path()
        lst = self.regions_for(path)
        if not (0 <= i < len(lst)):
            return
        self._drop_rg(lst[i])
        self.sel_region = None
        self._render_regions()
        self._update_badges()
        self.view.request_refresh()
        self.view.sched_redraw()

    def undo_region(self):
        path = self.cur_path()
        lst = self.regions_for(path)
        while self.region_hist:
            rg = self.region_hist.pop()
            for i, r in enumerate(lst):
                if r is rg:
                    self.remove_region(i)
                    return
        if lst:
            self.remove_region(len(lst) - 1)

    def clear_regions(self):
        path = self.cur_path()
        lst = self.regions_for(path)
        if not lst:
            return
        own = self.regions.get(path, [])
        shared = [r for r in lst if not any(r is o for o in own)]
        if shared:
            ans = messagebox.askyesnocancel(
                "Xóa hết vùng",
                f"Có {len(shared)} vùng đang áp dụng cho nhiều file.\n\n"
                "Có: xóa luôn các vùng đó khỏi mọi file\n"
                "Không: chỉ xóa các vùng riêng của file này")
            if ans is None:
                return
            if ans:
                for r in shared:
                    self._drop_rg(r)
        self.regions[path] = []
        self.sel_region = None
        self._render_regions()
        self._update_badges()
        self.view.request_refresh()
        self.view.sched_redraw()

    def _on_delete_key(self, _=None):
        w = self.focus_get()
        if w is not None and w.winfo_class() in ("Entry", "Text"):
            return
        if self.sel_region is not None:
            self.remove_region(self.sel_region)

    # --- xuất file -------------------------------------------------------------
    def _on_out_mode(self, label):
        if label == "Thư mục khác":
            self.var_out_mode.set("folder")
            self.out_row.pack(fill="x", pady=(8, 0), after=self.seg_out)
            self._update_out_hint()
            if not self.var_out.get().strip():
                self.after(80, self.pick_out)   # chưa chọn nơi lưu: mở luôn hộp chọn
        else:
            self.var_out_mode.set("beside")
            self.out_row.pack_forget()

    def pick_out(self):
        cur = self.var_out.get().strip()
        start = os.path.expanduser(cur) if cur else (self.last_out_dir or self.last_dir)
        d = FolderPicker(self, "Chọn nơi lưu kết quả", start=start, confirm="Lưu vào thư mục này",
                         allow_new=True, places=self._recent_places()).show()
        if d:
            self._set_out(d)

    def _set_out(self, d):
        self.var_out.set(str(d))
        self._remember(d)

    def _remember(self, d):
        d = str(d)
        if d in self.recent_out:
            self.recent_out.remove(d)
        self.recent_out.insert(0, d)
        del self.recent_out[4:]

    def _recent_places(self):
        return [(Path(d).name or d, d) for d in self.recent_out if os.path.isdir(d)]

    def _update_out_hint(self):
        t = self.var_out.get().strip()
        if not t:
            txt, col = "Chưa chọn nơi lưu.", MUTED
        elif os.path.isdir(os.path.expanduser(t)):
            txt, col = "Sẽ lưu kết quả vào thư mục này.", "#15803D"
        else:
            txt, col = "Thư mục này chưa có - sẽ tự tạo khi xuất.", ORANGE_DARK
        self.lbl_out_hint.configure(text=txt, text_color=col)

    def run(self):
        if self.running:
            self.cancel.set()
            self.btn_run.configure(text="Đang dừng…", state="disabled")
            return
        if not self.files:
            messagebox.showinfo("Thông báo", "Chưa có file nào.")
            return
        out = ""
        if self.var_out_mode.get() == "folder":
            out = self.var_out.get().strip()
            if not out:
                messagebox.showinfo("Thông báo", "Hãy chọn thư mục lưu kết quả.")
                return
            out = os.path.expanduser(out)
            self._remember(out)
        jobs = [(f, self.opts(f)) for f in self.files]
        self.cancel.clear()
        self.running = True
        self.btn_open.pack_forget()
        self.btn_run.configure(text="■  Dừng", fg_color="#44403C", hover_color="#292524")
        self.progress.set(0)
        self._log_clear()
        threading.Thread(target=self._worker, args=(jobs, out), daemon=True).start()

    def _worker(self, jobs, out):
        ok = err = 0
        last = None
        total = len(jobs)
        used = set()   # tránh hai file trùng tên đè lên nhau trong cùng lần xuất
        for i, (src, opts) in enumerate(jobs):
            if self.cancel.is_set():
                break
            p = Path(src)
            base = (Path(out) if out else p.parent) / f"{p.stem}_clean.pdf"
            dst, k = base, 2
            while str(dst).lower() in used:
                dst = base.with_name(f"{p.stem}_clean ({k}).pdf")
                k += 1
            used.add(str(dst).lower())

            def cb(done, n, i=i):
                self.q.put(("prog", (i + done / n) / total))
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                pages, nwm = clean_file(src, str(dst), opts, cb, self.cancel)
                ok += 1
                last = dst
                self.q.put(("log", f"OK   {p.name}  ({pages} trang, {nwm} logo) -> {dst.name}"))
            except InterruptedError:
                self.q.put(("log", f"Đã dừng ở {p.name}"))
                break
            except Exception as e:
                err += 1
                self.q.put(("log", f"LỖI {p.name}: {e}"))
            self.q.put(("prog", (i + 1) / total))
        self.q.put(("done", str(last.parent) if last else None, ok, err))

    def _log_clear(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _open_out(self):
        if self.last_out_dir:
            open_folder(self.last_out_dir)

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1])
                elif kind == "prog":
                    self.progress.set(min(1.0, max(0.0, msg[1])))
                elif kind == "done":
                    _, folder, ok, err = msg
                    self.running = False
                    self.btn_run.configure(text="▶  Làm sạch tất cả", fg_color=ORANGE,
                                           hover_color=ORANGE_HOVER, state="normal")
                    self._log(f"Xong: {ok} file thành công" + (f", {err} lỗi." if err else "."))
                    if ok:
                        self.progress.set(1)
                    if folder:
                        self.last_out_dir = folder
                        self.btn_open.pack(fill="x", pady=(8, 0))
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # --- kéo thả ---------------------------------------------------------------
    def _setup_dnd(self):
        self.dnd_on = bool(HAS_DND and self.dnd_ok)
        self.zone.set_dnd(self.dnd_on)
        if not self.dnd_on:
            return

        def reg(w, drop, enter=None, leave=None):
            if w is None:
                return
            try:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", drop)
                w.dnd_bind("<<DropEnter>>", enter or self._dnd_enter)
                w.dnd_bind("<<DropLeave>>", leave or self._dnd_leave)
            except Exception:
                pass

        for w in (self, self.side, getattr(self.side, "_parent_canvas", None), self.zone,
                  self.file_list, getattr(self.file_list, "_parent_canvas", None),
                  self.view, self.view.canvas):
            reg(w, self._on_drop)
        for w in (self.ent_out, getattr(self.ent_out, "_entry", None)):
            reg(w, self._on_drop_out, self._dnd_keep, self._dnd_keep)

    def _dnd_enter(self, event):
        self.zone.set_active(True)
        return event.action

    def _dnd_leave(self, event):
        self.zone.set_active(False)
        return event.action

    def _dnd_keep(self, event):
        return event.action

    def _drop_paths(self, event):
        try:
            return list(self.tk.splitlist(event.data))
        except Exception:
            return [event.data]

    def _on_drop(self, event):
        self.zone.set_active(False)
        self.add_paths(self._drop_paths(event))
        return event.action

    def _on_drop_out(self, event):
        for raw in self._drop_paths(event):
            p = Path(raw)
            d = p if p.is_dir() else p.parent
            if d.is_dir():
                self._set_out(d)
                self.seg_out.set("Thư mục khác")
                self._on_out_mode("Thư mục khác")
                break
        return event.action

    def _on_close(self):
        self.cancel.set()
        try:
            self.view.unload()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
