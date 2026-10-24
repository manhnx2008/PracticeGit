# PDF Cleaner

Công cụ gỡ **logo chìm (watermark)**, **header** và **footer** khỏi file PDF — giữ nguyên định dạng PDF, không cần chuyển sang Word.

Hỗ trợ cả **PDF scan** (mỗi trang là một ảnh): bật tùy chọn "Xóa cả vùng ảnh" rồi cắt theo % trang.

![Python](https://img.shields.io/badge/Python-3.8%2B-blue)
![License](https://img.shields.io/badge/License-MIT-green)

---

## ✨ Tính năng

| Tính năng | Mô tả |
|---|---|
| **Gỡ logo chìm** | Tự động phát hiện ảnh lớn lặp lại ở ≥ 60% số trang và xóa |
| **Xóa header/footer tự động** | Nhận diện chữ lặp lại ở vùng 12% trên/dưới trang |
| **Xóa header/footer thủ công** | Cắt theo % tùy chỉnh (0–15%) từ mép trên/dưới |
| **Hỗ trợ PDF scan** | Tô trắng pixel ảnh trong vùng bị xóa |
| **Xem trước** | Preview kết quả trước khi xử lý, khung đỏ đánh dấu vùng sẽ bị xóa |
| **Xử lý hàng loạt** | Chọn nhiều file hoặc cả thư mục, chạy một lần |

## 📦 Cài đặt

### Yêu cầu hệ thống

- **Python** ≥ 3.8
- **Tkinter** (có sẵn trên Windows/macOS)
  - Linux cần cài thêm:

  | Distro | Lệnh cài |
  |---|---|
  | Ubuntu / Debian | `sudo apt install python3-tk` |
  | Fedora / RHEL | `sudo dnf install python3-tkinter` |
  | Arch Linux | `sudo pacman -S tk` |
  | openSUSE | `sudo zypper install python3-tk` |

### Cài thư viện

```bash
pip install -r requirements.txt
```

Hoặc cài trực tiếp:

```bash
pip install "PyMuPDF>=1.24"
```

## 🚀 Sử dụng

```bash
python pdf_cleaner-2.py
```

### Hướng dẫn nhanh

1. **Chọn file** — Nhấn "Thêm PDF..." hoặc "Thêm thư mục..." để thêm file cần xử lý.
2. **Tùy chỉnh** — Chọn các tùy chọn:
   - ☑ Gỡ logo chìm (ảnh lớn lặp ở nhiều trang)
   - ☑ Xóa cả vùng ảnh (dành cho PDF scan)
   - ◉ Header/footer: tự nhận diện chữ lặp lại **hoặc** cắt theo % trang
   - Thanh trượt điều chỉnh % cắt header/footer (chế độ thủ công)
3. **Xem trước** — Chọn file trong danh sách để preview. Khung đỏ = vùng sẽ bị xóa.
4. **Xuất** — Chọn thư mục đầu ra (hoặc để trống → lưu cạnh file gốc với hậu tố `_clean`). Nhấn **"▶ Làm sạch tất cả"**.

## ⚙️ Cấu hình mặc định

| Tham số | Giá trị | Ý nghĩa |
|---|---|---|
| `WM_MIN_AREA` | 0.15 | Ảnh chiếm ≥ 15% diện tích trang → ứng viên logo chìm |
| `WM_MIN_PAGES` | 0.6 | Xuất hiện ở ≥ 60% số trang → xác nhận là logo chìm |
| `HF_ZONE` | 0.12 | Vùng quét header/footer: 12% trên + 12% dưới |
| `HF_MIN_PAGES` | 0.6 | Chữ lặp ở ≥ 60% số trang = header/footer |
| `FILL_COLOR` | `(1, 1, 1)` | Màu tô vùng bị xóa (trắng) |

Có thể chỉnh trực tiếp trong đầu file `pdf_cleaner-2.py`.

## 📁 Cấu trúc dự án

```
Tool/
├── pdf_cleaner-2.py   # Script chính (GUI + logic xử lý)
├── requirements.txt   # Thư viện cần thiết
└── README.md          # Tài liệu này
```

## 📝 Ghi chú

- File đầu ra giữ nguyên định dạng PDF, **không** chuyển sang ảnh hay Word.
- Với PDF chỉ có 1 trang, chế độ tự nhận diện header/footer sẽ không xóa gì (vì không có gì để so sánh lặp lại).
- Chế độ "Xóa cả vùng ảnh" dành cho PDF scan — tô trắng pixel ảnh bên trong vùng redact.
