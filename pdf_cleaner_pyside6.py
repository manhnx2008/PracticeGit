#!/usr/bin/env python3
"""
PDF Cleaner Pro — Công cụ gỡ logo chìm (watermark), header và footer khỏi PDF.
Giao diện PySide6 hiện đại, tối ưu trải nghiệm người dùng (Dark Mode, Zoom, Pan, Drag & Drop).

Chạy:
    python Tool.py
    hoặc .venv/bin/python Tool.py
"""

import os
import re
import sys
import warnings
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# Tắt cảnh báo từ thư viện PyMuPDF
warnings.filterwarnings("ignore", category=FutureWarning)
try:
    import pymupdf as fitz
except ImportError:
    import fitz

from PySide6.QtCore import Qt, QThread, Signal, QRectF, QPointF
from PySide6.QtGui import (
    QPixmap, QImage, QPainter, QColor, QPen, QBrush, QFont,
    QKeySequence, QShortcut, QIcon
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame,
    QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene, QGraphicsView,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QProgressBar, QRadioButton, QScrollArea, QSlider, QSplitter, QTextEdit,
    QVBoxLayout, QWidget, QListWidget, QListWidgetItem, QGroupBox, QSpinBox,
    QButtonGroup, QToolTip
)

# ---------------------------------------------------------------------------
# CẤU HÌNH VÀ THUẬT TOÁN XỬ LÝ PDF (LÕI)
# ---------------------------------------------------------------------------

WM_MIN_AREA = 0.15       # Ảnh chiếm >= 15% diện tích trang
WM_MIN_PAGES = 0.6       # Ảnh xuất hiện ở >= 60% số trang
HF_ZONE = 0.12           # Vùng header/footer: 12% trên/dưới
HF_MIN_PAGES = 0.6       # Chữ lặp lại ở >= 60% số trang


def detect_watermarks(doc):
    """Tìm xref của ảnh lớn (watermark/logo chìm) lặp lại trên nhiều trang."""
    n = len(doc)
    seen = defaultdict(set)

    for pno, page in enumerate(doc):
        page_area = page.rect.width * page.rect.height
        for info in page.get_image_info(xrefs=True):
            xref = info.get("xref", 0)
            if not xref:
                continue
            bbox = fitz.Rect(info["bbox"])
            if bbox.width * bbox.height >= WM_MIN_AREA * page_area:
                seen[xref].add(pno)

    needed = 1 if n == 1 else max(2, int(n * WM_MIN_PAGES + 0.5))
    return {xref for xref, pages in seen.items() if len(pages) >= needed}


def detect_header_footer(pages_blocks, heights):
    """
    Phát hiện khối văn bản ở vùng 12% mép trên/dưới có cùng nội dung và vị trí
    lặp lại trên ít nhất 60% số trang. Chuẩn hóa chữ số để nhận diện số trang.
    """
    n = len(pages_blocks)
    keyed = []
    counts = Counter()

    for blocks, height in zip(pages_blocks, heights):
        page_keys = []
        for x0, y0, x1, y1, text in blocks:
            text = text.strip()
            if not text:
                continue

            if y1 < height * HF_ZONE or y0 > height * (1 - HF_ZONE):
                key = (re.sub(r"\d+", "#", text), round(y0 / 6))
                page_keys.append((key, (x0, y0, x1, y1)))
                counts[key] += 1
        keyed.append(page_keys)

    needed = 1 if n == 1 else max(2, int(n * HF_MIN_PAGES + 0.5))
    result = []
    for page_keys in keyed:
        result.append([
            rect for key, rect in page_keys
            if counts[key] >= needed and n > 1
        ])
    return result


def analyze(doc, options):
    """Phân tích tài liệu tìm logo chìm và vị trí header/footer."""
    info = {"wm": set(), "hf": None}
    if options.get("watermark", True):
        info["wm"] = detect_watermarks(doc)

    if options.get("mode") == "auto":
        blocks, heights = [], []
        for page in doc:
            text_blocks = [
                (b[0], b[1], b[2], b[3], b[4])
                for b in page.get_text("blocks")
                if len(b) > 6 and b[6] == 0
            ]
            blocks.append(text_blocks)
            heights.append(page.rect.height)
        info["hf"] = detect_header_footer(blocks, heights)

    return info


def apply_redactions(page):
    """Xóa text trong vùng redact, bảo toàn ảnh và nét vẽ bên dưới."""
    try:
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
        )
    except (TypeError, AttributeError):
        try:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        except TypeError:
            page.apply_redactions()


def get_header_footer_rects(page, page_number, info, options):
    """Lấy danh sách các hình chữ nhật vùng header/footer cần xóa."""
    width, height = page.rect.width, page.rect.height

    if options.get("mode") == "auto" and info.get("hf") is not None:
        pad = 1.5
        return [
            fitz.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad)
            for x0, y0, x1, y1 in info["hf"][page_number]
        ]

    rects = []
    h_pct = options.get("header", 0)
    f_pct = options.get("footer", 0)
    if h_pct > 0:
        rects.append(fitz.Rect(0, 0, width, height * h_pct / 100))
    if f_pct > 0:
        rects.append(fitz.Rect(0, height * (1 - f_pct / 100), width, height))
    return rects


def clean_page(doc, page_number, info, options):
    """Xử lý làm sạch một trang đơn lẻ."""
    page = doc[page_number]

    # Xóa ảnh watermark
    for xref in info["wm"]:
        try:
            page.delete_image(xref)
        except Exception:
            pass

    # Xóa header/footer
    rects = get_header_footer_rects(page, page_number, info, options)
    for rect in rects:
        page.add_redact_annot(rect, fill=False)
    if rects:
        apply_redactions(page)


def clean_file(source, destination, options):
    """Làm sạch toàn bộ file PDF và lưu ra file mới."""
    doc = fitz.open(source)
    try:
        info = analyze(doc, options)
        for page_number in range(len(doc)):
            clean_page(doc, page_number, info, options)
        doc.save(destination, garbage=4, deflate=True)
        return len(doc), len(info["wm"])
    finally:
        doc.close()


def render_preview(source, page_number, options, preview_mode="clean", dpi=130):
    """
    Render ảnh preview độ nét cao.
    preview_mode:
        - 'clean': Hiển thị kết quả trang sau khi làm sạch.
        - 'markup': Hiển thị trang gốc kèm các hộp chữ nhật đỏ đánh dấu vùng sẽ xóa.
    """
    doc = fitz.open(source)
    try:
        info = analyze(doc, options)
        page_number = max(0, min(page_number, len(doc) - 1))
        page = doc[page_number]
        rects = get_header_footer_rects(page, page_number, info, options)

        scale = dpi / 72.0
        zones = [
            (r.x0 * scale, r.y0 * scale, r.x1 * scale, r.y1 * scale)
            for r in rects
        ]

        if preview_mode == "clean":
            clean_page(doc, page_number, info, options)

        pixmap = doc[page_number].get_pixmap(dpi=dpi, alpha=False)
        image = QImage(
            pixmap.samples, pixmap.width, pixmap.height,
            pixmap.stride, QImage.Format.Format_RGB888
        ).copy()

        return image, zones, len(doc), len(info["wm"])
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# LUỒNG XỬ LÝ CHẠY NỀN (WORKER THREAD)
# ---------------------------------------------------------------------------

class CleanWorker(QThread):
    progress_changed = Signal(int)
    log_message = Signal(str)
    finished_all = Signal()

    def __init__(self, files, output_dir, options):
        super().__init__()
        self.files = files
        self.output_dir = output_dir
        self.options = options

    def run(self):
        total = len(self.files)
        self.log_message.emit(f"ℹ Bắt đầu xử lý {total} tệp PDF...")

        for index, filename in enumerate(self.files, 1):
            source = Path(filename)
            output_folder = Path(self.output_dir) if self.output_dir else source.parent
            destination = output_folder / f"{source.stem}_clean.pdf"

            try:
                output_folder.mkdir(parents=True, exist_ok=True)
                if destination.resolve() == source.resolve():
                    raise ValueError("Tên file xuất trùng với file gốc.")

                pages, wm_count = clean_file(str(source), str(destination), self.options)
                self.log_message.emit(
                    f"OK  {source.name} ({pages} trang, gỡ {wm_count} logo) → {destination.name}"
                )
            except Exception as exc:
                self.log_message.emit(f"LỖI {source.name}: {exc}")

            self.progress_changed.emit(index)

        self.log_message.emit("ℹ Đã hoàn tất toàn bộ quá trình làm sạch!")
        self.finished_all.emit()


# ---------------------------------------------------------------------------
# VIEWPORT PREVIEW TƯƠNG TÁC (ZOOM, PAN, SHADOW)
# ---------------------------------------------------------------------------

class PDFPreviewView(QGraphicsView):
    zoom_changed = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setBackgroundBrush(QBrush(QColor("#0b0f19")))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.zoom_factor = 1.0

    def wheelEvent(self, event):
        """Ctrl + Lăn chuột để Zoom mượt mà vào vị trí con trỏ."""
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            factor = 1.15 if delta > 0 else 0.85
            self.apply_zoom(factor)
            event.accept()
        else:
            super().wheelEvent(event)

    def apply_zoom(self, factor):
        new_zoom = self.zoom_factor * factor
        if 0.15 <= new_zoom <= 4.0:
            self.scale(factor, factor)
            self.zoom_factor = new_zoom
            self.zoom_changed.emit(self.zoom_factor)

    def reset_zoom(self):
        self.resetTransform()
        self.zoom_factor = 1.0
        self.zoom_changed.emit(self.zoom_factor)

    def fit_page(self):
        if self.scene() and not self.scene().sceneRect().isEmpty():
            self.resetTransform()
            margins = 30
            rect = self.scene().sceneRect().adjusted(-margins, -margins, margins, margins)
            self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
            self.zoom_factor = self.transform().m11()
            self.zoom_changed.emit(self.zoom_factor)

    def fit_width(self):
        if self.scene() and not self.scene().sceneRect().isEmpty():
            self.resetTransform()
            rect = self.scene().sceneRect()
            avail_w = max(50, self.viewport().width() - 40)
            scale = avail_w / max(1.0, rect.width())
            self.scale(scale, scale)
            self.zoom_factor = scale
            self.zoom_changed.emit(self.zoom_factor)


# ---------------------------------------------------------------------------
# GIAO DIỆN CHÍNH PYSIDE6 (MODERN DARK THEME)
# ---------------------------------------------------------------------------

class PDFCleanerPro(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PDF Cleaner Pro — Gỡ Logo Chìm & Header/Footer")
        self.resize(1340, 860)
        self.setMinimumSize(1000, 680)

        self.files = []
        self.current_page = 0
        self.page_count = 1
        self.preview_image = None
        self.worker = None

        self.setAcceptDrops(True)
        self._build_ui()
        self._apply_style()

    # -----------------------------------------------------------------------
    # HỖ TRỢ KÉO THẢ TẬP TIN (DRAG & DROP)
    # -----------------------------------------------------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                p = Path(url.toLocalFile())
                if p.suffix.lower() == ".pdf" or p.is_dir():
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event):
        dropped_files = []
        for url in event.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.is_file() and p.suffix.lower() == ".pdf":
                dropped_files.append(str(p))
            elif p.is_dir():
                dropped_files.extend(sorted(str(f) for f in p.glob("*.pdf") if f.is_file()))
        if dropped_files:
            self._add_files(dropped_files)
            event.acceptProposedAction()

    # -----------------------------------------------------------------------
    # XÂY DỰNG GIAO DIỆN
    # -----------------------------------------------------------------------
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # 1. Thanh tiêu đề đầu trang (App Banner)
        header_bar = QWidget()
        header_bar.setObjectName("HeaderBar")
        header_layout = QHBoxLayout(header_bar)
        header_layout.setContentsMargins(20, 14, 20, 14)

        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        app_title = QLabel("⚡ PDF CLEANER PRO")
        app_title.setObjectName("AppTitle")
        app_sub = QLabel("Tự động nhận diện & gỡ bỏ logo chìm (watermark), vùng header/footer tài liệu PDF")
        app_sub.setObjectName("AppSubtitle")
        title_box.addWidget(app_title)
        title_box.addWidget(app_sub)
        header_layout.addLayout(title_box, 1)

        badge_engine = QLabel("PyMuPDF v2.0 • Tốc độ cao")
        badge_engine.setObjectName("BadgeEngine")
        header_layout.addWidget(badge_engine)
        root_layout.addWidget(header_bar)

        # 2. Vùng làm việc chính: Splitter (Trái: Bảng điều khiển, Phải: Studio Preview)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(4)
        root_layout.addWidget(splitter, 1)

        # --- BẢNG ĐIỀU KHIỂN (PANEL TRÁI) ---
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(16, 16, 12, 16)
        left_layout.setSpacing(14)

        # Group 1: Danh sách file
        file_group = QGroupBox("1. DANH SÁCH TẬP TIN PDF")
        file_layout = QVBoxLayout(file_group)
        file_layout.setSpacing(10)
        file_layout.setContentsMargins(14, 18, 14, 14)

        file_toolbar = QHBoxLayout()
        file_toolbar.setSpacing(8)

        self.add_files_btn = QPushButton("📄 Thêm PDF...")
        self.add_files_btn.setToolTip("Chọn một hoặc nhiều tệp PDF từ máy tính")
        self.add_files_btn.clicked.connect(self.add_files)

        self.add_folder_btn = QPushButton("📁 Thư mục...")
        self.add_folder_btn.setToolTip("Quét toàn bộ tệp PDF có trong thư mục")
        self.add_folder_btn.clicked.connect(self.add_folder)

        self.clear_btn = QPushButton("🗑️ Xóa hết")
        self.clear_btn.setToolTip("Xóa sạch danh sách tệp")
        self.clear_btn.clicked.connect(self.clear_files)

        file_toolbar.addWidget(self.add_files_btn)
        file_toolbar.addWidget(self.add_folder_btn)
        file_toolbar.addWidget(self.clear_btn)
        file_layout.addLayout(file_toolbar)

        self.file_list = QListWidget()
        self.file_list.setMinimumHeight(130)
        self.file_list.currentRowChanged.connect(self.on_file_selected)
        file_layout.addWidget(self.file_list)

        self.file_summary_label = QLabel("Chưa chọn tệp PDF nào (Hỗ trợ kéo & thả tệp)")
        self.file_summary_label.setStyleSheet("color: #64748b; font-size: 11px;")
        file_layout.addWidget(self.file_summary_label)

        left_layout.addWidget(file_group)

        # Group 2: Tùy chọn làm sạch
        opt_group = QGroupBox("2. TÙY CHỌN LÀM SẠCH")
        opt_layout = QVBoxLayout(opt_group)
        opt_layout.setSpacing(12)
        opt_layout.setContentsMargins(14, 18, 14, 14)

        # Checkbox Watermark
        self.watermark_check = QCheckBox("Gỡ logo chìm (Watermark lặp lại trên các trang)")
        self.watermark_check.setChecked(True)
        self.watermark_check.setToolTip("Tự động quét và loại bỏ ảnh nền mờ lặp lại trên nhiều trang")
        self.watermark_check.stateChanged.connect(self.refresh_preview)
        opt_layout.addWidget(self.watermark_check)

        # Mode Selector
        mode_label = QLabel("Chế độ nhận diện Header & Footer:")
        mode_label.setStyleSheet("color: #94a3b8; font-weight: 600; font-size: 12px;")
        opt_layout.addWidget(mode_label)

        self.auto_radio = QRadioButton("✨ Tự động nhận diện chữ lặp lại (Khuyên dùng)")
        self.manual_radio = QRadioButton("📐 Cắt thủ công theo tỷ lệ % mép trang")
        self.auto_radio.setChecked(True)

        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.auto_radio)
        self.mode_group.addButton(self.manual_radio)
        self.auto_radio.toggled.connect(self.on_mode_toggled)
        opt_layout.addWidget(self.auto_radio)
        opt_layout.addWidget(self.manual_radio)

        # Thanh trượt tỷ lệ cắt thủ công
        self.sliders_container = QWidget()
        sliders_layout = QVBoxLayout(self.sliders_container)
        sliders_layout.setContentsMargins(8, 4, 8, 4)
        sliders_layout.setSpacing(8)

        # Header slider
        h_box = QHBoxLayout()
        self.header_title = QLabel("Mép trên (Header):")
        self.header_badge = QLabel("5.0%")
        self.header_badge.setStyleSheet("color: #38bdf8; font-weight: 700;")
        h_box.addWidget(self.header_title)
        h_box.addStretch()
        h_box.addWidget(self.header_badge)
        sliders_layout.addLayout(h_box)

        self.header_slider = QSlider(Qt.Orientation.Horizontal)
        self.header_slider.setRange(0, 20)
        self.header_slider.setValue(5)
        self.header_slider.valueChanged.connect(self.update_slider_labels)
        self.header_slider.valueChanged.connect(self.refresh_preview)
        sliders_layout.addWidget(self.header_slider)

        # Footer slider
        f_box = QHBoxLayout()
        self.footer_title = QLabel("Mép dưới (Footer):")
        self.footer_badge = QLabel("4.0%")
        self.footer_badge.setStyleSheet("color: #38bdf8; font-weight: 700;")
        f_box.addWidget(self.footer_title)
        f_box.addStretch()
        f_box.addWidget(self.footer_badge)
        sliders_layout.addLayout(f_box)

        self.footer_slider = QSlider(Qt.Orientation.Horizontal)
        self.footer_slider.setRange(0, 20)
        self.footer_slider.setValue(4)
        self.footer_slider.valueChanged.connect(self.update_slider_labels)
        self.footer_slider.valueChanged.connect(self.refresh_preview)
        sliders_layout.addWidget(self.footer_slider)

        opt_layout.addWidget(self.sliders_container)
        self.on_mode_toggled()
        left_layout.addWidget(opt_group)

        # Group 3: Thư mục xuất & Nút bấm thực thi
        exec_group = QGroupBox("3. XUẤT KẾT QUẢ & TIẾN TRÌNH")
        exec_layout = QVBoxLayout(exec_group)
        exec_layout.setSpacing(10)
        exec_layout.setContentsMargins(14, 18, 14, 14)

        out_box = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Để trống: Lưu cùng thư mục với file gốc (*_clean.pdf)")
        self.output_btn = QPushButton("📂 Chọn...")
        self.output_btn.clicked.connect(self.choose_output_dir)
        out_box.addWidget(self.output_edit, 1)
        out_box.addWidget(self.output_btn)
        exec_layout.addLayout(out_box)

        self.run_button = QPushButton("🚀 BẮT ĐẦU LÀM SẠCH TẤT CẢ")
        self.run_button.setObjectName("RunButton")
        self.run_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.run_button.clicked.connect(self.run_cleaning)
        exec_layout.addWidget(self.run_button)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        exec_layout.addWidget(self.progress)

        left_layout.addWidget(exec_group)

        # Group 4: Nhật ký xử lý
        log_header = QHBoxLayout()
        log_label = QLabel("NHẬT KÝ HOẠT ĐỘNG")
        log_label.setStyleSheet("font-weight: 700; color: #94a3b8; font-size: 11px;")
        self.clear_log_btn = QPushButton("Xóa log")
        self.clear_log_btn.setStyleSheet("padding: 2px 8px; font-size: 11px;")
        self.clear_log_btn.clicked.connect(lambda: self.log.clear())
        log_header.addWidget(log_label)
        log_header.addStretch()
        log_header.addWidget(self.clear_log_btn)
        left_layout.addLayout(log_header)

        self.log = QTextEdit()
        self.log.setObjectName("LogBox")
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(110)
        left_layout.addWidget(self.log, 1)

        # --- STUDIO XEM TRƯỚC (PANEL PHẢI) ---
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(14, 16, 16, 16)
        right_layout.setSpacing(10)

        # Toolbar điều khiển Preview
        preview_bar = QWidget()
        preview_bar.setObjectName("PreviewBar")
        bar_layout = QHBoxLayout(preview_bar)
        bar_layout.setContentsMargins(10, 8, 10, 8)
        bar_layout.setSpacing(8)

        # Nhóm chuyển trang
        self.btn_first = QPushButton("⏮")
        self.btn_first.setToolTip("Trang đầu tiên")
        self.btn_first.clicked.connect(lambda: self.goto_page(0))

        self.btn_prev = QPushButton("◀")
        self.btn_prev.setToolTip("Trang trước (Phím Mũi tên trái)")
        self.btn_prev.clicked.connect(lambda: self.goto_page(self.current_page - 1))

        self.page_spin = QSpinBox()
        self.page_spin.setRange(1, 1)
        self.page_spin.setValue(1)
        self.page_spin.setToolTip("Nhập số trang để nhảy tới nhanh")
        self.page_spin.valueChanged.connect(lambda val: self.goto_page(val - 1))

        self.total_pages_label = QLabel("/ 1")
        self.total_pages_label.setStyleSheet("color: #94a3b8; font-weight: 600;")

        self.btn_next = QPushButton("▶")
        self.btn_next.setToolTip("Trang sau (Phím Mũi tên phải)")
        self.btn_next.clicked.connect(lambda: self.goto_page(self.current_page + 1))

        self.btn_last = QPushButton("⏭")
        self.btn_last.setToolTip("Trang cuối cùng")
        self.btn_last.clicked.connect(lambda: self.goto_page(self.page_count - 1))

        bar_layout.addWidget(self.btn_first)
        bar_layout.addWidget(self.btn_prev)
        bar_layout.addWidget(self.page_spin)
        bar_layout.addWidget(self.total_pages_label)
        bar_layout.addWidget(self.btn_next)
        bar_layout.addWidget(self.btn_last)

        bar_layout.addSpacing(12)

        # Tùy chọn chế độ xem trước
        self.preview_mode_combo = QComboBox()
        self.preview_mode_combo.setMinimumWidth(230)
        self.preview_mode_combo.addItem("👁️ Xem kết quả sau khi xóa", "clean")
        self.preview_mode_combo.addItem("🎯 Xem vùng đánh dấu xóa", "markup")
        self.preview_mode_combo.currentIndexChanged.connect(self.refresh_preview)
        bar_layout.addWidget(self.preview_mode_combo)

        bar_layout.addStretch(1)

        # Zoom Controls
        self.btn_zoom_out = QPushButton("➖")
        self.btn_zoom_out.setToolTip("Thu nhỏ (Ctrl + Lăn chuột xuống)")
        self.btn_zoom_out.clicked.connect(lambda: self.preview_view.apply_zoom(0.85))

        self.zoom_label = QLabel("100%")
        self.zoom_label.setStyleSheet("color: #38bdf8; font-weight: 600; min-width: 45px;")
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.btn_zoom_in = QPushButton("➕")
        self.btn_zoom_in.setToolTip("Phóng to (Ctrl + Lăn chuột lên)")
        self.btn_zoom_in.clicked.connect(lambda: self.preview_view.apply_zoom(1.15))

        self.btn_fit_page = QPushButton("⛶ Vừa trang")
        self.btn_fit_page.setToolTip("Tự căn chỉnh vừa toàn bộ khung trang")
        self.btn_fit_page.clicked.connect(lambda: self.preview_view.fit_page())

        self.btn_fit_width = QPushButton("↔ Vừa rộng")
        self.btn_fit_width.setToolTip("Tự căn chỉnh vừa chiều rộng")
        self.btn_fit_width.clicked.connect(lambda: self.preview_view.fit_width())

        bar_layout.addWidget(self.btn_zoom_out)
        bar_layout.addWidget(self.zoom_label)
        bar_layout.addWidget(self.btn_zoom_in)
        bar_layout.addWidget(self.btn_fit_page)
        bar_layout.addWidget(self.btn_fit_width)

        right_layout.addWidget(preview_bar)

        # Canvas GraphicsView
        self.preview_scene = QGraphicsScene(self)
        self.preview_view = PDFPreviewView(self)
        self.preview_view.setScene(self.preview_scene)
        self.preview_view.zoom_changed.connect(self.on_zoom_changed)
        right_layout.addWidget(self.preview_view, 1)

        # Thanh trạng thái dưới cùng của Preview
        status_bar = QHBoxLayout()
        self.status_file_info = QLabel("Chưa chọn tệp PDF để xem trước")
        self.status_file_info.setStyleSheet("color: #94a3b8; font-size: 12px;")

        self.status_wm_info = QLabel("Watermark: 0")
        self.status_wm_info.setStyleSheet(
            "background-color: #1e293b; color: #a5b4fc; border: 1px solid #334155; "
            "border-radius: 10px; padding: 2px 10px; font-weight: 600; font-size: 11px;"
        )

        status_bar.addWidget(self.status_file_info, 1)
        status_bar.addWidget(self.status_wm_info)
        right_layout.addLayout(status_bar)

        # Gắn vào Splitter
        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setSizes([450, 890])

        # Phím tắt điều hướng nhanh
        QShortcut(QKeySequence(Qt.Key.Key_Left), self, lambda: self.goto_page(self.current_page - 1))
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, lambda: self.goto_page(self.current_page + 1))
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self, self.delete_selected_file)

    # -----------------------------------------------------------------------
    # ĐỊNH KIỂU GIAO DIỆN HIỆN ĐẠI (DARK THEME QSS)
    # -----------------------------------------------------------------------
    def _apply_style(self):
        self.setStyleSheet("""
            /* Tổng thể cửa sổ */
            QMainWindow {
                background-color: #0f172a;
                color: #f1f5f9;
            }
            QWidget {
                color: #e2e8f0;
                font-family: 'Segoe UI', Roboto, 'Inter', -apple-system, sans-serif;
                font-size: 13px;
            }

            /* Thanh Header */
            #HeaderBar {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1e1b4b, stop:0.5 #1e293b, stop:1 #0f172a);
                border-bottom: 1px solid #334155;
            }
            #AppTitle {
                font-size: 17px;
                font-weight: 800;
                color: #ffffff;
                letter-spacing: 0.5px;
            }
            #AppSubtitle {
                font-size: 12px;
                color: #94a3b8;
            }
            #BadgeEngine {
                background-color: #1e293b;
                border: 1px solid #4338ca;
                color: #a5b4fc;
                border-radius: 12px;
                padding: 4px 12px;
                font-size: 11px;
                font-weight: 600;
            }

            /* Hộp nhóm (Cards) */
            QGroupBox {
                font-weight: 700;
                font-size: 12px;
                border: 1px solid #334155;
                border-radius: 10px;
                margin-top: 14px;
                padding-top: 10px;
                background-color: #1e293b;
                color: #f8fafc;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 14px;
                padding: 0 6px;
                color: #38bdf8;
            }

            /* Nút bấm thông thường */
            QPushButton {
                background-color: #334155;
                color: #f8fafc;
                border: 1px solid #475569;
                border-radius: 6px;
                padding: 7px 14px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #475569;
                border-color: #64748b;
            }
            QPushButton:pressed {
                background-color: #1e293b;
            }
            QPushButton:disabled {
                background-color: #1e293b;
                border-color: #334155;
                color: #64748b;
            }

            /* Nút chạy chính (Hero Button) */
            #RunButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4f46e5, stop:1 #2563eb);
                color: #ffffff;
                border: none;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 700;
                padding: 11px 18px;
            }
            #RunButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #6366f1, stop:1 #3b82f6);
            }
            #RunButton:pressed {
                background: #3730a3;
            }
            #RunButton:disabled {
                background: #334155;
                color: #64748b;
            }

            /* Danh sách File */
            QListWidget {
                background-color: #0f172a;
                border: 1px solid #334155;
                border-radius: 8px;
                padding: 4px;
                color: #f8fafc;
            }
            QListWidget::item {
                padding: 6px 10px;
                border-radius: 6px;
                margin-bottom: 3px;
                border: 1px solid transparent;
            }
            QListWidget::item:hover {
                background-color: #1e293b;
                border-color: #475569;
            }
            QListWidget::item:selected {
                background-color: #312e81;
                border-color: #6366f1;
                color: #ffffff;
            }

            /* Ô nhập văn bản */
            QLineEdit, QSpinBox, QComboBox {
                padding: 6px 12px;
                background-color: #0f172a;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 6px 10px;
                color: #f8fafc;
            }
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
                border-color: #6366f1;
            }
            QComboBox::drop-down {
                border: none;
                padding-right: 6px;
            }

            /* Checkbox & Radio */
            QCheckBox, QRadioButton {
                spacing: 8px;
                color: #e2e8f0;
                font-weight: 500;
            }
            QCheckBox::indicator, QRadioButton::indicator {
                width: 17px;
                height: 17px;
                border: 1px solid #475569;
                border-radius: 4px;
                background-color: #0f172a;
            }
            QRadioButton::indicator {
                border-radius: 9px;
            }
            QCheckBox::indicator:hover, QRadioButton::indicator:hover {
                border-color: #818cf8;
            }
            QCheckBox::indicator:checked, QRadioButton::indicator:checked {
                background-color: #4f46e5;
                border-color: #6366f1;
            }

            /* Thanh trượt Slider */
            QSlider::groove:horizontal {
                height: 6px;
                background: #0f172a;
                border-radius: 3px;
                border: 1px solid #334155;
            }
            QSlider::sub-page:horizontal {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4f46e5, stop:1 #38bdf8);
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #ffffff;
                border: 2px solid #6366f1;
                width: 16px;
                margin-top: -6px;
                margin-bottom: -6px;
                border-radius: 9px;
            }
            QSlider::handle:horizontal:hover {
                background: #e0e7ff;
                border-color: #818cf8;
            }

            /* Thanh tiến trình Progress Bar */
            QProgressBar {
                border: 1px solid #334155;
                border-radius: 6px;
                text-align: center;
                background-color: #0f172a;
                color: #f8fafc;
                font-weight: 700;
                font-size: 11px;
                min-height: 16px;
                max-height: 16px;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #10b981, stop:1 #3b82f6);
                border-radius: 5px;
            }

            /* Terminal Log */
            #LogBox {
                background-color: #090d16;
                border: 1px solid #1e293b;
                border-radius: 6px;
                color: #94a3b8;
                font-family: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
                font-size: 11px;
                padding: 8px;
            }

            /* Thanh công cụ xem trước */
            #PreviewBar {
                background-color: #1e293b;
                border: 1px solid #334155;
                border-radius: 8px;
            }

            /* Thanh cuộn Scrollbar */
            QScrollBar:vertical {
                background: #0f172a;
                width: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #334155;
                border-radius: 4px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background: #475569;
            }
            QScrollBar:horizontal {
                background: #0f172a;
                height: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:horizontal {
                background: #334155;
                border-radius: 4px;
                min-width: 20px;
            }
            QScrollBar::add-line, QScrollBar::sub-line {
                width: 0px;
                height: 0px;
            }
        """)

    # -----------------------------------------------------------------------
    # LOGIC VÀ XỬ LÝ SỰ KIỆN
    # -----------------------------------------------------------------------
    def options(self):
        return {
            "watermark": self.watermark_check.isChecked(),
            "mode": "auto" if self.auto_radio.isChecked() else "manual",
            "header": float(self.header_slider.value()),
            "footer": float(self.footer_slider.value()),
        }

    def on_mode_toggled(self):
        is_manual = self.manual_radio.isChecked()
        self.sliders_container.setEnabled(is_manual)
        opacity = "1.0" if is_manual else "0.4"
        self.sliders_container.setStyleSheet(f"opacity: {opacity};")

    def update_slider_labels(self, *_):
        self.header_badge.setText(f"{self.header_slider.value()}.0%")
        self.footer_badge.setText(f"{self.footer_slider.value()}.0%")

    def add_files(self):
        filenames, _ = QFileDialog.getOpenFileNames(
            self, "Chọn một hoặc nhiều tệp PDF", "", "PDF files (*.pdf)"
        )
        if filenames:
            self._add_files(filenames)

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục chứa tệp PDF")
        if folder:
            filenames = sorted(
                str(p) for p in Path(folder).glob("*.pdf") if p.is_file()
            )
            self._add_files(filenames)

    def _add_files(self, filenames):
        existing = set(self.files)
        added_count = 0

        for fn in filenames:
            fn_str = str(Path(fn).resolve())
            if fn_str not in existing:
                self.files.append(fn_str)
                existing.add(fn_str)
                added_count += 1

                p = Path(fn_str)
                try:
                    size_mb = p.stat().st_size / (1024 * 1024)
                    size_text = f"{size_mb:.1f} MB" if size_mb >= 1.0 else f"{p.stat().st_size / 1024:.0f} KB"
                except Exception:
                    size_text = "N/A"

                item = QListWidgetItem(f"📄  {p.name}\n     └─ Dung lượng: {size_text}")
                self.file_list.addItem(item)

        total_size_mb = sum(
            Path(f).stat().st_size / (1024 * 1024) for f in self.files if Path(f).exists()
        )
        self.file_summary_label.setText(
            f"Đã chọn: {len(self.files)} tệp PDF ({total_size_mb:.1f} MB tổng cộng)"
        )

        if self.files and self.file_list.currentRow() < 0:
            self.file_list.setCurrentRow(0)

        if added_count > 0:
            self.append_log(f"ℹ Đã thêm {added_count} tệp vào danh sách.")

    def clear_files(self):
        self.files.clear()
        self.file_list.clear()
        self.preview_scene.clear()
        self.preview_image = None
        self.current_page = 0
        self.page_count = 1
        self.page_spin.setMaximum(1)
        self.page_spin.setValue(1)
        self.total_pages_label.setText("/ 1")
        self.file_summary_label.setText("Chưa chọn tệp PDF nào (Hỗ trợ kéo & thả tệp)")
        self.status_file_info.setText("Chưa chọn tệp PDF để xem trước")
        self.status_wm_info.setText("Watermark: 0")

    def delete_selected_file(self):
        row = self.file_list.currentRow()
        if 0 <= row < len(self.files):
            removed = self.files.pop(row)
            self.file_list.takeItem(row)
            self.append_log(f"ℹ Đã xóa {Path(removed).name} khỏi danh sách.")
            if self.files:
                next_row = min(row, len(self.files) - 1)
                self.file_list.setCurrentRow(next_row)
            else:
                self.clear_files()

    def choose_output_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu kết quả")
        if folder:
            self.output_edit.setText(folder)

    def on_file_selected(self, row):
        if row < 0 or row >= len(self.files):
            return
        self.current_page = 0
        self.refresh_preview(reset_fit=True)

    def goto_page(self, page_number):
        page_number = max(0, min(page_number, self.page_count - 1))
        if page_number != self.current_page:
            self.current_page = page_number
            self.refresh_preview(reset_fit=False)

    def on_zoom_changed(self, zoom):
        self.zoom_label.setText(f"{int(zoom * 100)}%")

    def refresh_preview(self, *_, reset_fit=False):
        row = self.file_list.currentRow()
        if row < 0 or row >= len(self.files):
            return

        source = self.files[row]
        preview_mode = self.preview_mode_combo.currentData()

        try:
            image, zones, page_count, watermark_count = render_preview(
                source, self.current_page, self.options(), preview_mode=preview_mode
            )

            self.page_count = page_count
            self.current_page = min(self.current_page, page_count - 1)

            # Cập nhật số trang trên thanh điều hướng
            self.page_spin.blockSignals(True)
            self.page_spin.setMaximum(page_count)
            self.page_spin.setValue(self.current_page + 1)
            self.page_spin.blockSignals(False)
            self.total_pages_label.setText(f"/ {page_count}")

            self.preview_image = QPixmap.fromImage(image)
            self.preview_scene.clear()

            w = self.preview_image.width()
            h = self.preview_image.height()

            # Hiệu ứng đổ bóng tờ giấy (Paper Drop Shadow)
            shadow_rect = QGraphicsRectItem(6, 6, w, h)
            shadow_rect.setBrush(QBrush(QColor(0, 0, 0, 110)))
            shadow_rect.setPen(Qt.PenStyle.NoPen)
            self.preview_scene.addItem(shadow_rect)

            # Đặt tờ giấy PDF
            pix_item = self.preview_scene.addPixmap(self.preview_image)
            pix_item.setPos(0, 0)

            # Nếu ở chế độ 'markup' hoặc có zones cần hiển thị
            if preview_mode == "markup":
                red_pen = QPen(QColor(239, 68, 68, 230))
                red_pen.setWidth(2)
                red_pen.setStyle(Qt.PenStyle.DashLine)
                red_brush = QBrush(QColor(239, 68, 68, 55))

                for x0, y0, x1, y1 in zones:
                    rect_item = self.preview_scene.addRect(
                        QRectF(x0, y0, x1 - x0, y1 - y0), red_pen, red_brush
                    )
                    rect_item.setZValue(10)

            self.preview_scene.setSceneRect(self.preview_scene.itemsBoundingRect().adjusted(-20, -20, 20, 20))

            if reset_fit:
                self.preview_view.fit_page()

            file_p = Path(source)
            self.status_file_info.setText(
                f"📄 {file_p.name}  (Trang {self.current_page + 1}/{page_count})"
            )
            self.status_wm_info.setText(f"Watermark: {watermark_count} logo")

        except Exception as exc:
            self.status_file_info.setText(f"Lỗi hiển thị xem trước: {exc}")

    def run_cleaning(self):
        if not self.files:
            QMessageBox.warning(
                self, "Chưa chọn tệp", "Vui lòng chọn ít nhất một tệp PDF để bắt đầu làm sạch."
            )
            return

        if self.worker is not None and self.worker.isRunning():
            return

        self.run_button.setEnabled(False)
        self.run_button.setText("⏳ ĐANG LÀM SẠCH...")
        self.progress.setMaximum(len(self.files))
        self.progress.setValue(0)

        out_dir = self.output_edit.text().strip()
        self.worker = CleanWorker(list(self.files), out_dir, self.options())
        self.worker.progress_changed.connect(self.progress.setValue)
        self.worker.log_message.connect(self.append_log)
        self.worker.finished_all.connect(self.cleaning_finished)
        self.worker.start()

    def append_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        if message.startswith("OK"):
            html = (
                f"<span style='color:#64748b;'>[{timestamp}]</span> "
                f"<b style='color:#10b981;'>[THÀNH CÔNG]</b> "
                f"<span style='color:#e2e8f0;'>{message[3:]}</span>"
            )
        elif message.startswith("LỖI"):
            html = (
                f"<span style='color:#64748b;'>[{timestamp}]</span> "
                f"<b style='color:#ef4444;'>[LỖI]</b> "
                f"<span style='color:#fca5a5;'>{message[4:]}</span>"
            )
        elif message.startswith("ℹ"):
            html = (
                f"<span style='color:#64748b;'>[{timestamp}]</span> "
                f"<b style='color:#38bdf8;'>[THÔNG BÁO]</b> "
                f"<span style='color:#94a3b8;'>{message[2:]}</span>"
            )
        else:
            html = f"<span style='color:#64748b;'>[{timestamp}]</span> <span style='color:#94a3b8;'>{message}</span>"

        self.log.append(html)

    def cleaning_finished(self):
        self.run_button.setEnabled(True)
        self.run_button.setText("🚀 BẮT ĐẦU LÀM SẠCH TẤT CẢ")
        self.refresh_preview()
        QMessageBox.information(
            self, "Hoàn tất", "Quá trình làm sạch các tệp PDF đã hoàn thành xuất sắc!"
        )


# ---------------------------------------------------------------------------
# ĐIỂM KHỞI CHẠY CHƯƠNG TRÌNH
# ---------------------------------------------------------------------------

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("PDF Cleaner Pro")
    app.setStyle("Fusion")

    # Font chữ hệ thống chuẩn đẹp
    font = QFont("Segoe UI", 10)
    app.setFont(font)

    window = PDFCleanerPro()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
