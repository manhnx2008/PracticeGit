# PDF Cleaner

Công cụ gỡ **logo chìm (watermark)**, **header**, **footer** và **vùng tùy chỉnh** khỏi file PDF — giữ nguyên định dạng PDF, không cần chuyển sang Word hay ảnh.

Hỗ trợ cả **PDF scan** (mỗi trang là một ảnh): bật "Xóa cả pixel ảnh" rồi cắt theo % trang.

![Python](https://img.shields.io/badge/Python-3.8%2B-blue)
![License](https://img.shields.io/badge/License-MIT-green)

---

## ✨ Tính năng

| Tính năng | Mô tả |
|---|---|
| **Gỡ logo chìm** | Tự phát hiện ảnh lớn lặp lại ở ≥ 60% số trang và xóa mà không ảnh hưởng trang khác |
| **Xóa header/footer tự động** | Nhận diện chữ lặp lại ở vùng 12% trên/dưới trang |
| **Xóa header/footer thủ công** | Cắt theo % tùy chỉnh (0–30%) từ mép trên/dưới |
| **Vùng xóa tùy chỉnh** | Kéo chuột vẽ vùng trên trang xem trước, áp dụng cho 1 trang / trang lẻ / trang chẵn / khoảng trang / mọi trang |
| **Áp dụng đa file** | Vùng xóa có thể áp dụng cho 1 file, nhiều file chọn tay, hoặc mọi file cùng lúc |
| **Hỗ trợ PDF scan** | Tô trắng (hoặc tô theo màu nền giấy) pixel ảnh trong vùng bị xóa |
| **Xem trước liên tục** | Cuộn hết các trang, thu phóng, xem kết quả sau khi dọn hoặc bản gốc (vùng xóa nhuộm đỏ) |
| **Kéo thả file** | Kéo thả PDF hoặc thư mục vào cửa sổ (cần `tkinterdnd2`) |
| **Hộp chọn thư mục** | Có nút tạo thư mục mới, hiện vị trí quen dùng, hỗ trợ thư mục ẩn |
| **Xử lý hàng loạt** | Chọn nhiều file hoặc cả thư mục, thanh tiến trình, có nút dừng giữa chừng |

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
pip install "PyMuPDF>=1.24" customtkinter Pillow tkinterdnd2
```

> **Ghi chú:** `tkinterdnd2` là tùy chọn — thiếu thì ứng dụng vẫn chạy, chỉ không kéo thả file được.

## 🚀 Sử dụng

```bash
python pdf_cleaner.py
```

### Hướng dẫn nhanh

#### 1. Thêm file
- Bấm vùng tải lên hoặc kéo thả PDF / thư mục vào cửa sổ.
- Chọn file trong danh sách bên trái để xem trước.

#### 2. Tùy chỉnh dọn dẹp
- **Gỡ logo chìm** — bật/tắt tự phát hiện ảnh lớn lặp nhiều trang.
- **Xóa cả pixel ảnh** — dành cho PDF scan.
- **Header/footer** — 3 chế độ:
  - *Tự động:* tìm chữ lặp lại ở 12% trên/dưới.
  - *Theo %:* cắt theo thanh trượt (0–30%).
  - *Tắt:* không xóa header/footer.
- **Màu tô** — trắng hoặc tự lấy theo nền giấy (giấy scan ngả vàng).

#### 3. Vẽ vùng xóa tùy chỉnh
- Bấm **"✂ Bật chế độ vẽ vùng"**, rồi kéo chuột trên trang xem trước.
- Chọn phạm vi áp dụng: chỉ trang này / mọi trang / trang lẻ / trang chẵn / khoảng trang.
- Chọn áp dụng cho file nào: chỉ file này / mọi file / chọn file.
- Hoàn tác bằng **Ctrl+Z**, xóa vùng bằng phím **Delete**.

#### 4. Xuất
- **Cạnh file gốc** — lưu cùng thư mục, thêm hậu tố `_clean`.
- **Thư mục khác** — chọn hoặc tạo thư mục lưu riêng.
- Bấm **"▶ Làm sạch tất cả"**. Có thể bấm lần nữa để dừng giữa chừng.
- Sau khi xong, bấm **"Mở thư mục kết quả"** để mở nhanh.

### Phím tắt

| Phím | Chức năng |
|---|---|
| Ctrl+Z | Hoàn tác vùng xóa cuối |
| Delete | Xóa vùng đang chọn |
| Esc | Tắt chế độ vẽ vùng |
| Ctrl+Cuộn chuột | Thu phóng trang xem trước |
| Shift+Cuộn chuột | Cuộn ngang |

## ⚙️ Cấu hình mặc định

| Tham số | Giá trị | Ý nghĩa |
|---|---|---|
| `WM_MIN_AREA` | 0.15 | Ảnh chiếm ≥ 15% diện tích trang → ứng viên logo chìm |
| `WM_MIN_PAGES` | 0.6 | Xuất hiện ở ≥ 60% số trang → xác nhận là logo chìm |
| `HF_ZONE` | 0.12 | Vùng quét header/footer: 12% trên + 12% dưới |
| `HF_MIN_PAGES` | 0.6 | Chữ lặp ở ≥ 60% số trang = header/footer |
| `HF_MAX_PCT` | 30.0 | Giới hạn % tối đa khi cắt theo % |

Có thể chỉnh trực tiếp trong đầu file `pdf_cleaner.py`.

## 📁 Cấu trúc dự án

```
Tool/
├── pdf_cleaner.py     # Script chính (GUI + logic xử lý)
├── requirements.txt   # Thư viện cần thiết
└── README.md          # Tài liệu này
```

## 📝 Ghi chú

- File đầu ra giữ nguyên định dạng PDF, **không** chuyển sang ảnh hay Word.
- Với PDF chỉ có 1 trang, chế độ tự nhận diện header/footer sẽ không xóa gì (vì cần so sánh lặp lại giữa các trang).
- PDF có mật khẩu sẽ bị bỏ qua (không xem trước hoặc xử lý được).
- Nếu hai file trùng tên xuất cùng thư mục, file sau sẽ tự thêm số thứ tự `(2)`, `(3)`...
- Giao diện sử dụng [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) với theme cam nhẹ.
