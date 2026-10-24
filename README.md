# PDF Sạch

Ứng dụng desktop một file dùng PyMuPDF + PySide6 để loại bỏ header, footer, watermark dạng ảnh và vùng tùy chọn trong PDF.

## Chạy

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python pdf_cleaner.py
```

Hoặc nếu hệ thống đã cài `python3-venv`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python pdf_cleaner.py
```

## Tính năng

- Xem trước từng trang và chọn vùng bằng kéo chuột.
- Redact header/footer bằng dải lề tùy chỉnh (3-25%).
- Dò và redact ảnh watermark có alpha hoặc ảnh nhỏ trên trang.
- Xử lý hàng loạt; đầu ra nằm trong `pdf_sach_output/`.
- Không ghi đè tệp gốc.

## Giao diện

- Màu cam làm tone chủ đạo, thiết kế gọn nhẹ với sidebar và canvas preview.
- Nút bấm bo tròn, font Segoe UI, tương phản cao.
- Điều chỉnh cửa sổ tự động render lại preview.

## Lưu ý

Watermark dạng ảnh được phát hiện theo heuristic bảo thủ: ảnh có alpha hoặc chiếm dưới 38% diện tích trang. Các watermark không có alpha hoặc dùng nội dung vector có thể cần dùng Chọn vùng.

## Xuất desktop app

Dùng PyInstaller hoặc Nuitka để đóng gói thành `.exe` (Windows) hoặc `.app` (macOS) cho việc dùng hàng ngày. Tệp đơn có thể di chuyển, không cần cài Python riêng.
