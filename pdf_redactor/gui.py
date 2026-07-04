"""Tkinter user interface for the local redaction workflow."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Dict, List, Optional, Tuple

import fitz
from PIL import Image, ImageTk

from .models import ProcessingMode, RedactionRequest, RedactionResult, parse_color
from .processor import PDFRedactor, RedactionError, ReviewSession, normalize_keywords, output_path_for


class RedactionApp(ttk.Frame):
    """Interactive single-PDF workflow with page preview and save-as support."""

    PREVIEW_SIZE = (480, 620)

    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=16)
        self.master = master
        self._events: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._is_processing = False
        self._preview_page = 0
        self._preview_total = 0
        self._preview_photo: Optional[ImageTk.PhotoImage] = None
        self._preview_page_rect: Optional[fitz.Rect] = None
        self._preview_image_box: Optional[Tuple[float, float, float, float]] = None
        self._drag_start: Optional[Tuple[float, float]] = None
        self._drag_item: Optional[int] = None
        self._manual_rects: Dict[int, List[Tuple[float, float, float, float]]] = {}
        self._processor = PDFRedactor()
        self._review_session: Optional[ReviewSession] = None
        self._review_mode = False
        self._review_task_counter = 0
        self._active_review_task: Optional[int] = None
        self._selected_overlay_id: Optional[str] = None
        self._log_link_counter = 0
        self.preview_jump_page = tk.StringVar()

        self.input_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.password = tk.StringVar()
        self.color = tk.StringVar(value="#4F81BD")
        self.processing_mode = tk.StringVar(value=ProcessingMode.COMPREHENSIVE.value)
        self.mode_description = tk.StringVar()
        self.color.trace_add("write", self._update_color_swatch)
        self.processing_mode.trace_add("write", self._update_mode_description)
        self._build_widgets()
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        master.bind_all("<KeyPress>", self._handle_preview_shortcuts, add="+")
        master.bind_all("<Button-1>", self._clear_text_focus_on_outside_click, add="+")
        self.after(100, self._poll_events)

    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)

        ttk.Label(self, text="PDF 自动打码工具", font=("Microsoft YaHei UI", 18, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )

        form = ttk.Frame(self)
        form.grid(row=1, column=0, sticky="nsew", padx=(0, 14))
        form.columnconfigure(0, minsize=150)
        form.columnconfigure(1, weight=1)
        form.columnconfigure(2, minsize=100)
        form.rowconfigure(8, weight=1)

        ttk.Label(form, text="PDF 文件：").grid(row=0, column=0, sticky="w", pady=4)
        self.path_entry = ttk.Entry(form, textvariable=self.input_path, width=40)
        self.path_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=4)
        self.browse_button = ttk.Button(form, text="选择 PDF…", command=self._select_file)
        self.browse_button.grid(row=0, column=2, sticky="e", pady=4)

        ttk.Label(form, text="打开密码（非加密pdf可不管）：").grid(row=1, column=0, sticky="w", pady=4)
        self.password_entry = ttk.Entry(form, textvariable=self.password, show="●", width=40)
        self.password_entry.grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=4)
        self.preview_refresh_button = ttk.Button(form, text="刷新预览", command=self._refresh_preview)
        self.preview_refresh_button.grid(row=1, column=2, sticky="e", pady=4)

        ttk.Label(form, text="输出文件：").grid(row=2, column=0, sticky="w", pady=4)
        self.output_entry = ttk.Entry(form, textvariable=self.output_path, width=40)
        self.output_entry.grid(row=2, column=1, sticky="ew", padx=(0, 8), pady=4)
        self.output_browse_button = ttk.Button(form, text="另存为…", command=self._select_output)
        self.output_browse_button.grid(row=2, column=2, sticky="e", pady=4)

        ttk.Label(form, text="关键词（一行一个）：").grid(row=3, column=0, sticky="nw", pady=(10, 4))
        self.keyword_box = ScrolledText(form, height=9, width=46, wrap="none", font=("Microsoft YaHei UI", 10))
        self.keyword_box.grid(row=3, column=1, columnspan=2, sticky="nsew", pady=(10, 4))

        ttk.Label(form, text="处理模式：").grid(row=4, column=0, sticky="nw", pady=(10, 4))
        mode_frame = ttk.Frame(form)
        mode_frame.grid(row=4, column=1, columnspan=2, sticky="w", pady=(10, 4))
        self.comprehensive_mode_button = ttk.Radiobutton(
            mode_frame,
            text="全面模式（推荐）",
            variable=self.processing_mode,
            value=ProcessingMode.COMPREHENSIVE.value,
        )
        self.comprehensive_mode_button.grid(row=0, column=0, sticky="w")
        self.fast_mode_button = ttk.Radiobutton(
            mode_frame,
            text="快速模式",
            variable=self.processing_mode,
            value=ProcessingMode.FAST.value,
        )
        self.fast_mode_button.grid(row=0, column=1, sticky="w", padx=(16, 0))
        ttk.Label(mode_frame, textvariable=self.mode_description, foreground="#52616B", wraplength=480).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )

        ttk.Label(form, text="打码颜色：").grid(row=5, column=0, sticky="w", pady=(10, 4))
        color_frame = ttk.Frame(form)
        color_frame.grid(row=5, column=1, columnspan=2, sticky="w", pady=(10, 4))
        self.color_swatch = tk.Label(color_frame, width=4, height=1, relief="solid", bd=1)
        self.color_swatch.grid(row=0, column=0, padx=(0, 7))
        self.color_entry = ttk.Entry(color_frame, textvariable=self.color, width=12)
        self.color_entry.grid(row=0, column=1)
        self.color_button = ttk.Button(color_frame, text="选择颜色…", command=self._choose_color)
        self.color_button.grid(row=0, column=2, padx=(8, 0))
        ttk.Button(color_frame, text="默认蓝", command=lambda: self.color.set("#4F81BD")).grid(row=0, column=3, padx=(8, 0))
        ttk.Label(color_frame, text="#RRGGBB").grid(row=0, column=4, padx=(8, 0))
        ttk.Label(
            color_frame,
            text="注意：OCR 识别印章文字的能力较弱，请人工重点检查。",
            foreground="#B54708",
            wraplength=480,
        ).grid(row=1, column=0, columnspan=5, sticky="w", pady=(5, 0))

        action_bar = tk.Frame(form, background="#EEF4FB", highlightbackground="#D6E3F1", highlightthickness=1)
        action_bar.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(14, 12))
        action_bar.columnconfigure(0, weight=1)
        self.action_hint = tk.Label(
            action_bar,
            text="第 1 步：生成遮盖预览并核对结果",
            background="#EEF4FB",
            foreground="#1565C0",
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.action_hint.grid(row=0, column=0, sticky="w", padx=12, pady=10)
        action_buttons = tk.Frame(action_bar, background="#EEF4FB")
        action_buttons.grid(row=0, column=1, sticky="e", padx=10, pady=8)
        button_options = {
            "width": 13,
            "pady": 8,
            "relief": "flat",
            "cursor": "hand2",
            "font": ("Microsoft YaHei UI", 10, "bold"),
        }
        self.reset_button = tk.Button(
            action_buttons,
            text="重置遮盖",
            command=self._reset_redactions,
            background="#607D8B",
            activebackground="#455A64",
            foreground="white",
            activeforeground="white",
            disabledforeground="#F8FAFC",
            state="disabled",
            **button_options,
        )
        self.reset_button.pack(side="left", padx=(0, 8))
        self.start_button = tk.Button(
            action_buttons,
            text="生成遮盖预览",
            command=self._start,
            background="#1565C0",
            activebackground="#0D47A1",
            foreground="white",
            activeforeground="white",
            **button_options,
        )
        self.start_button.pack(side="left")

        ttk.Separator(form).grid(row=7, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        ttk.Label(form, text="处理日志：").grid(row=8, column=0, sticky="nw")
        self.log_box = ScrolledText(form, height=11, width=52, state="disabled", wrap="word", font=("Microsoft YaHei UI", 9))
        self.log_box.grid(row=8, column=1, columnspan=2, sticky="nsew")
        form.rowconfigure(8, weight=1)

        preview = ttk.Labelframe(self, text="PDF 预览(双击可全屏查看)", padding=8)
        preview.grid(row=1, column=1, sticky="nsew")
        preview.columnconfigure(0, weight=1)
        preview.rowconfigure(0, weight=1)
        self.preview_canvas = tk.Canvas(
            preview,
            background="#F4F6F8",
            highlightthickness=0,
            width=self.PREVIEW_SIZE[0],
            height=self.PREVIEW_SIZE[1],
        )
        self.preview_canvas.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        self.preview_canvas.bind("<ButtonPress-1>", self._start_manual_selection)
        self.preview_canvas.bind("<B1-Motion>", self._drag_manual_selection)
        self.preview_canvas.bind("<ButtonRelease-1>", self._finish_manual_selection)
        self.preview_canvas.bind("<Double-Button-1>", self._show_fullscreen_preview)
        controls = ttk.Frame(preview)
        controls.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 8))
        controls.columnconfigure(1, weight=1)
        self.preview_previous_button = ttk.Button(controls, text="‹ 上一页 (A)", command=lambda: self._change_preview_page(-1))
        self.preview_previous_button.grid(row=0, column=0, sticky="w")
        self.preview_page_label = ttk.Label(controls, text="未加载", anchor="center")
        self.preview_page_label.grid(row=0, column=1, sticky="ew")
        ttk.Label(controls, text="跳至：").grid(row=0, column=2, padx=(8, 0))
        self.preview_jump_entry = ttk.Entry(controls, textvariable=self.preview_jump_page, width=5, justify="center")
        self.preview_jump_entry.grid(row=0, column=3)
        self.preview_jump_entry.bind("<Return>", lambda _event: self._jump_from_entry())
        self.preview_jump_button = ttk.Button(controls, text="跳转", command=self._jump_from_entry)
        self.preview_jump_button.grid(row=0, column=4, padx=(4, 8))
        self.preview_next_button = ttk.Button(controls, text="下一页 (D) ›", command=lambda: self._change_preview_page(1))
        self.preview_next_button.grid(row=0, column=5, sticky="e")
        self.clear_manual_button = ttk.Button(controls, text="清除本页手动框选的部分", command=self._clear_manual_selections)
        self.clear_manual_button.grid(row=1, column=0, columnspan=6, pady=(6, 0))
        self.delete_overlay_button = ttk.Button(controls, text="删除选中遮盖", command=self._delete_selected_overlay)
        self.delete_overlay_button.grid(row=3, column=0, columnspan=6, pady=(6, 0))
        ttk.Label(controls, text="提示：在预览中拖动鼠标，可手动遮盖目标文字。", foreground="#52616B").grid(
            row=2, column=0, columnspan=6, pady=(4, 0)
        )
        self._update_color_swatch()
        self._update_mode_description()
        self._update_preview_controls()

    def _reset_redactions(self) -> None:
        """Discard the current review plan and return to the untouched PDF preview."""

        can_cancel_review = self._is_processing and self._active_review_task is not None
        if self._is_processing and not can_cancel_review:
            return
        if not can_cancel_review and not (self._review_session or self._manual_rects):
            return
        if not messagebox.askyesno(
            "重置遮盖",
            (
                "当前正在生成遮盖预览。将取消本次预览并回到原始 PDF 预览。\n"
                if can_cancel_review
                else "将删除当前遮盖预览和所有手动框选，回到原始 PDF 预览。\n"
            )
            +
            "原始 PDF 与已经生成的输出文件都不会被修改。是否继续？",
            parent=self.master,
        ):
            return
        if can_cancel_review:
            # The OCR call currently running in its worker thread cannot be
            # forcibly stopped safely. Marking its task inactive makes every
            # later log/result from it harmless and ignorable.
            self._active_review_task = None
            self._set_processing(False)
        self._review_session = None
        self._review_mode = False
        self._manual_rects.clear()
        self._selected_overlay_id = None
        self._preview_page = 0
        self._set_primary_action(False)
        self._clear_log()
        self._append_log("已重置遮盖：当前显示的是未处理的原始 PDF，可重新调整关键词或颜色后生成预览。")
        self._refresh_preview()

    def _select_file(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择需要打码的 PDF",
            filetypes=[("PDF 文件", "*.pdf *.PDF"), ("所有文件", "*.*")],
        )
        if selected:
            input_file = Path(selected)
            self.input_path.set(str(input_file))
            self.output_path.set(str(output_path_for(input_file)))
            self._preview_page = 0
            self._manual_rects.clear()
            self._review_session = None
            self._review_mode = False
            self._selected_overlay_id = None
            self._set_primary_action(False)
            self._refresh_preview()

    def _select_output(self) -> None:
        input_file = Path(self.input_path.get().strip()) if self.input_path.get().strip() else None
        current = Path(self.output_path.get().strip()) if self.output_path.get().strip() else None
        initial_directory = (current or input_file).parent if (current or input_file) else Path.cwd()
        initial_name = (current or input_file).name if (current or input_file) else "redacted.pdf"
        if input_file and not current:
            initial_name = f"{input_file.stem}_redacted.pdf"
        selected = filedialog.asksaveasfilename(
            title="选择打码文件的保存位置和名称",
            initialdir=str(initial_directory),
            initialfile=initial_name,
            defaultextension=".pdf",
            filetypes=[("PDF 文件", "*.pdf")],
        )
        if selected:
            self.output_path.set(selected)

    def _choose_color(self) -> None:
        try:
            parse_color(self.color.get())
            initial = self.color.get()
        except ValueError:
            initial = "#4F81BD"
        _rgb, selected = colorchooser.askcolor(color=initial, title="选择打码颜色", parent=self.master)
        if selected:
            self.color.set(selected.upper())

    def _update_color_swatch(self, *_args: object) -> None:
        try:
            red, green, blue = parse_color(self.color.get())
            self.color_swatch.configure(background=f"#{red:02X}{green:02X}{blue:02X}", text="")
        except ValueError:
            self.color_swatch.configure(background="#FFFFFF", text="!")

    def _update_mode_description(self, *_args: object) -> None:
        if self.processing_mode.get() == ProcessingMode.FAST.value:
            self.mode_description.set("快速：含图片或没有文字层的页面进行 OCR；纯文字页跳过，兼顾速度与图片文字识别。")
        else:
            self.mode_description.set("全面：每一页均进行 OCR，处理时间较长。")

    def _refresh_preview(self) -> None:
        path_text = self.input_path.get().strip()
        if not path_text or not Path(path_text).is_file():
            self._set_preview_placeholder("选择 PDF 后显示预览")
            return
        document: Optional[fitz.Document] = None
        try:
            document = fitz.open(path_text)
            if document.needs_pass:
                password = self.password.get()
                if not password:
                    self._set_preview_placeholder("此 PDF 受密码保护\n请输入密码后点击“刷新预览”")
                    return
                if not document.authenticate(password):
                    self._set_preview_placeholder("PDF 密码不正确，无法预览")
                    return
            self._preview_total = document.page_count
            self._preview_page = min(max(0, self._preview_page), max(0, self._preview_total - 1))
            page = document[self._preview_page]
            pixmap = page.get_pixmap(matrix=fitz.Matrix(180 / 72.0, 180 / 72.0), alpha=False)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            image.thumbnail(self.PREVIEW_SIZE, Image.Resampling.LANCZOS)
            self._preview_photo = ImageTk.PhotoImage(image)
            canvas_width = max(self.preview_canvas.winfo_width(), self.PREVIEW_SIZE[0])
            canvas_height = max(self.preview_canvas.winfo_height(), self.PREVIEW_SIZE[1])
            offset_x = (canvas_width - image.width) / 2
            offset_y = (canvas_height - image.height) / 2
            self._preview_page_rect = page.rect
            self._preview_image_box = (offset_x, offset_y, offset_x + image.width, offset_y + image.height)
            self.preview_canvas.delete("all")
            self.preview_canvas.create_image(offset_x, offset_y, anchor="nw", image=self._preview_photo, tags="preview")
            self._draw_review_overlays()
            self.preview_page_label.configure(text=f"第 {self._preview_page + 1} / {self._preview_total} 页")
            self.preview_jump_page.set(str(self._preview_page + 1))
        except Exception as exc:
            self._set_preview_placeholder(f"无法预览 PDF\n{exc}")
        finally:
            if document:
                document.close()
        self._update_preview_controls()

    def _set_preview_placeholder(self, text: str) -> None:
        self._preview_photo = None
        self._preview_total = 0
        self._preview_page_rect = None
        self._preview_image_box = None
        self.preview_canvas.delete("all")
        self.preview_canvas.create_text(
            self.PREVIEW_SIZE[0] / 2,
            self.PREVIEW_SIZE[1] / 2,
            text=text,
            justify="center",
            fill="#52616B",
            font=("Microsoft YaHei UI", 11),
        )
        self.preview_page_label.configure(text="未加载")
        self.preview_jump_page.set("")
        self._update_preview_controls()

    def _change_preview_page(self, offset: int) -> None:
        if not self._preview_total:
            return
        self._preview_page = min(max(0, self._preview_page + offset), self._preview_total - 1)
        self._refresh_preview()

    @staticmethod
    def _is_text_input(widget: tk.Misc) -> bool:
        """Do not treat letters typed into an input control as page shortcuts."""

        return isinstance(widget, (tk.Entry, ttk.Entry, tk.Text))

    def _handle_preview_shortcuts(self, event: tk.Event) -> Optional[str]:
        """Handle A/D navigation in the normal preview, outside text inputs."""

        if self._is_processing or not self._preview_total or self._is_text_input(event.widget):
            return None
        if event.widget.winfo_toplevel() != self.master.winfo_toplevel():
            return None
        key = event.keysym.lower()
        if key == "a":
            self._change_preview_page(-1)
            return "break"
        if key == "d":
            self._change_preview_page(1)
            return "break"
        return None

    def _clear_text_focus_on_outside_click(self, event: tk.Event) -> None:
        """Return focus to the preview when a main-window input is left."""

        if event.widget.winfo_toplevel() != self.master.winfo_toplevel():
            return
        if self._is_text_input(event.widget):
            return
        self.preview_canvas.focus_set()

    def _jump_from_entry(self) -> None:
        try:
            target = int(self.preview_jump_page.get().strip())
        except ValueError:
            messagebox.showwarning("页码无效", "请输入有效页码。", parent=self.master)
            return
        if not 1 <= target <= self._preview_total:
            messagebox.showwarning("页码超出范围", f"请输入 1 到 {self._preview_total} 之间的页码。", parent=self.master)
            return
        self._preview_page = target - 1
        self._refresh_preview()

    def _show_fullscreen_preview(self, _event: Optional[tk.Event] = None) -> None:
        if not self._preview_total:
            return
        document: Optional[fitz.Document] = None
        try:
            document = fitz.open(self.input_path.get())
            if document.needs_pass and not document.authenticate(self.password.get()):
                messagebox.showwarning("无法预览", "请输入正确 PDF 密码后再打开全屏预览。", parent=self.master)
                return
            page = document[self._preview_page]
            page_rect = fitz.Rect(page.rect)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(200 / 72.0, 200 / 72.0), alpha=False)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        finally:
            if document:
                document.close()
        window = tk.Toplevel(self.master)
        window.attributes("-fullscreen", True)
        window.configure(background="#1E293B")
        max_width, max_height = window.winfo_screenwidth() - 50, window.winfo_screenheight() - 90
        image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image)
        canvas = tk.Canvas(window, background="#1E293B", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        left, top = (window.winfo_screenwidth() - image.width) / 2, (window.winfo_screenheight() - image.height) / 2 - 20
        canvas.create_image(left, top, anchor="nw", image=photo)
        canvas.image = photo
        selected: Optional[str] = None
        drag: Optional[Tuple[float, float]] = None
        drag_item: Optional[int] = None

        def overlays() -> list:
            if self._review_session:
                return self._review_session.overlays(self._preview_page, PDFRedactor.SCALE)
            return []

        def draw() -> None:
            canvas.delete("overlay")
            for item in overlays():
                points = []
                for x, y in item.quad:
                    points.extend((left + (x - page_rect.x0) * image.width / page_rect.width, top + (y - page_rect.y0) * image.height / page_rect.height))
                canvas.create_polygon(points, fill=self.color.get(), stipple="gray50", outline="#FF5252" if item.id == selected else "#90CAF9", width=3 if item.id == selected else 2, tags="overlay")

        def point_to_pdf(x: float, y: float) -> Optional[Tuple[float, float]]:
            if not left <= x <= left + image.width or not top <= y <= top + image.height:
                return None
            return (page_rect.x0 + (x - left) * page_rect.width / image.width, page_rect.y0 + (y - top) * page_rect.height / image.height)

        def press(event: tk.Event) -> None:
            nonlocal selected, drag, drag_item
            canvas.focus_set()
            point = point_to_pdf(event.x, event.y)
            if not point:
                return
            for item in reversed(overlays()):
                bounds = fitz.Rect(min(x for x, _ in item.quad), min(y for _, y in item.quad), max(x for x, _ in item.quad), max(y for _, y in item.quad))
                if bounds.contains(fitz.Point(*point)):
                    selected = item.id
                    draw()
                    return
            selected = None
            drag = (event.x, event.y)
            drag_item = canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#FF5252", width=3)

        def motion(event: tk.Event) -> None:
            if drag and drag_item:
                canvas.coords(drag_item, drag[0], drag[1], event.x, event.y)

        def release(event: tk.Event) -> None:
            nonlocal drag, drag_item
            if not drag or not drag_item:
                return
            canvas.delete(drag_item)
            start = point_to_pdf(*drag)
            end = point_to_pdf(event.x, event.y)
            drag, drag_item = None, None
            if not start or not end or not self._review_session:
                return
            self._review_session.add_manual_rect(self._preview_page, (min(start[0], end[0]), min(start[1], end[1]), max(start[0], end[0]), max(start[1], end[1])))
            draw()

        def delete(_event: Optional[tk.Event] = None) -> None:
            nonlocal selected
            if selected and self._review_session:
                self._review_session.remove_overlay(selected, self._preview_page)
                selected = None
                draw()

        def close(_event: Optional[tk.Event] = None) -> None:
            window.destroy()
            self._refresh_preview()

        def change_page(target: int) -> None:
            if not 0 <= target < self._preview_total:
                return
            self._preview_page = target
            window.destroy()
            self.master.after(10, self._show_fullscreen_preview)

        jump_page = tk.StringVar(value=str(self._preview_page + 1))

        def jump() -> None:
            try:
                target = int(jump_page.get()) - 1
            except ValueError:
                messagebox.showwarning("页码无效", "请输入有效页码。", parent=window)
                return
            if not 0 <= target < self._preview_total:
                messagebox.showwarning("页码超出范围", f"请输入 1 到 {self._preview_total} 之间的页码。", parent=window)
                return
            change_page(target)

        canvas.bind("<ButtonPress-1>", press)
        canvas.bind("<B1-Motion>", motion)
        canvas.bind("<ButtonRelease-1>", release)
        def keyboard_navigation(event: tk.Event) -> Optional[str]:
            key = event.keysym.lower()
            if key == "escape":
                close()
                return "break"
            if self._is_text_input(event.widget):
                return None
            if key == "a":
                change_page(self._preview_page - 1)
                return "break"
            if key == "d":
                change_page(self._preview_page + 1)
                return "break"
            return None

        window.bind("<Delete>", delete)
        window.bind("<KeyPress>", keyboard_navigation)
        controls = tk.Frame(window, background="#1E293B")
        controls.place(relx=1.0, x=-18, y=18, anchor="ne")
        tk.Button(controls, text="‹ 上一页 (A)", command=lambda: change_page(self._preview_page - 1), state="normal" if self._preview_page > 0 else "disabled", background="#455A64", foreground="white").pack(side="left", padx=4)
        tk.Button(controls, text="下一页 (D) ›", command=lambda: change_page(self._preview_page + 1), state="normal" if self._preview_page + 1 < self._preview_total else "disabled", background="#455A64", foreground="white").pack(side="left", padx=4)
        tk.Label(controls, text="跳至", background="#1E293B", foreground="white").pack(side="left", padx=(10, 2))
        jump_entry = tk.Entry(controls, textvariable=jump_page, width=5, justify="center")
        jump_entry.pack(side="left")
        jump_entry.bind("<Return>", lambda _event: jump())
        tk.Button(controls, text="跳转", command=jump, background="#455A64", foreground="white").pack(side="left", padx=4)
        tk.Button(controls, text="删除选中遮盖", command=delete, background="#C62828", foreground="white").pack(side="left", padx=4)
        tk.Button(controls, text="退出全屏 (Esc)", command=close, background="#455A64", foreground="white").pack(side="left", padx=4)
        draw()
        window.after_idle(canvas.focus_set)

    def _update_preview_controls(self) -> None:
        previous = "normal" if self._preview_page > 0 and not self._is_processing else "disabled"
        following = "normal" if self._preview_page + 1 < self._preview_total and not self._is_processing else "disabled"
        self.preview_previous_button.configure(state=previous)
        self.preview_next_button.configure(state=following)
        has_manual = self._preview_page in self._manual_rects
        if self._review_session:
            has_manual = any(item.source == "手动" for item in self._review_session.overlays(self._preview_page, PDFRedactor.SCALE))
        clear_state = "normal" if has_manual and not self._is_processing else "disabled"
        self.clear_manual_button.configure(state=clear_state)
        jump_state = "normal" if self._preview_total and not self._is_processing else "disabled"
        self.preview_jump_entry.configure(state=jump_state)
        self.preview_jump_button.configure(state=jump_state)
        self.delete_overlay_button.configure(state="normal" if self._review_session and self._selected_overlay_id and not self._is_processing else "disabled")
        self._update_reset_button()

    def _update_reset_button(self) -> None:
        has_redactions = bool(self._review_session or self._manual_rects)
        can_cancel_review = self._is_processing and self._active_review_task is not None
        self.reset_button.configure(
            state="normal" if can_cancel_review or (has_redactions and not self._is_processing) else "disabled"
        )

    def _selection_is_inside_preview(self, x: float, y: float) -> bool:
        if not self._preview_image_box:
            return False
        left, top, right, bottom = self._preview_image_box
        return left <= x <= right and top <= y <= bottom

    def _start_manual_selection(self, event: tk.Event) -> None:
        if self._is_processing or not self._selection_is_inside_preview(event.x, event.y):
            return
        if self._review_session:
            point = self._canvas_rect_to_pdf(event.x, event.y, event.x + 1, event.y + 1)
            if point:
                for item in reversed(self._review_session.overlays(self._preview_page, PDFRedactor.SCALE)):
                    bounds = fitz.Rect(min(x for x, _ in item.quad), min(y for _, y in item.quad), max(x for x, _ in item.quad), max(y for _, y in item.quad))
                    if bounds.contains(fitz.Point(point[0], point[1])):
                        self._selected_overlay_id = item.id
                        self._draw_review_overlays()
                        self._update_preview_controls()
                        return
        self._drag_start = (event.x, event.y)
        self._drag_item = self.preview_canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#E53935", width=2)

    def _drag_manual_selection(self, event: tk.Event) -> None:
        if self._drag_start is None or self._drag_item is None:
            return
        self.preview_canvas.coords(self._drag_item, self._drag_start[0], self._drag_start[1], event.x, event.y)

    def _finish_manual_selection(self, event: tk.Event) -> None:
        if self._drag_start is None or self._drag_item is None:
            return
        start_x, start_y = self._drag_start
        self.preview_canvas.delete(self._drag_item)
        self._drag_start = None
        self._drag_item = None
        if not self._selection_is_inside_preview(event.x, event.y):
            return
        if abs(event.x - start_x) < 4 or abs(event.y - start_y) < 4:
            return
        pdf_rect = self._canvas_rect_to_pdf(start_x, start_y, event.x, event.y)
        if pdf_rect is None:
            return
        if self._review_session:
            self._review_session.add_manual_rect(self._preview_page, pdf_rect)
        else:
            self._manual_rects.setdefault(self._preview_page, []).append(pdf_rect)
        self._draw_review_overlays()
        self._update_preview_controls()

    def _canvas_rect_to_pdf(
        self, start_x: float, start_y: float, end_x: float, end_y: float
    ) -> Optional[Tuple[float, float, float, float]]:
        if not self._preview_image_box or self._preview_page_rect is None:
            return None
        left, top, right, bottom = self._preview_image_box
        x0, x1 = sorted((max(left, start_x), min(right, end_x)))
        y0, y1 = sorted((max(top, start_y), min(bottom, end_y)))
        if x1 <= x0 or y1 <= y0:
            return None
        page = self._preview_page_rect
        width = right - left
        height = bottom - top
        return (
            page.x0 + (x0 - left) * page.width / width,
            page.y0 + (y0 - top) * page.height / height,
            page.x0 + (x1 - left) * page.width / width,
            page.y0 + (y1 - top) * page.height / height,
        )

    def _draw_review_overlays(self) -> None:
        if not self._preview_image_box or self._preview_page_rect is None:
            return
        left, top, right, bottom = self._preview_image_box
        page = self._preview_page_rect
        if self._review_session:
            overlays = self._review_session.overlays(self._preview_page, PDFRedactor.SCALE)
        else:
            overlays = []
            for index, values in enumerate(self._manual_rects.get(self._preview_page, [])):
                rect = fitz.Rect(values)
                overlays.append(type("Overlay", (), {"id": f"manual:{index}", "quad": ((rect.x0, rect.y0), (rect.x1, rect.y0), (rect.x1, rect.y1), (rect.x0, rect.y1))})())
        for item in overlays:
            points = []
            for x, y in item.quad:
                points.extend((left + (x - page.x0) * (right - left) / page.width, top + (y - page.y0) * (bottom - top) / page.height))
            selected = item.id == self._selected_overlay_id
            options = {"outline": "#E53935" if selected else "#1565C0", "width": 3 if selected else 2, "tags": "selection"}
            if self._review_mode:
                options.update({"fill": self.color.get(), "stipple": "gray50"})
            self.preview_canvas.create_polygon(points, **options)

    def _clear_manual_selections(self) -> None:
        if self._review_session:
            for item in reversed(self._review_session.overlays(self._preview_page, PDFRedactor.SCALE)):
                if item.source == "手动":
                    self._review_session.remove_overlay(item.id, self._preview_page)
        else:
            self._manual_rects.pop(self._preview_page, None)
        self._selected_overlay_id = None
        self._refresh_preview()

    def _delete_selected_overlay(self) -> None:
        if self._review_session and self._selected_overlay_id:
            self._review_session.remove_overlay(self._selected_overlay_id, self._preview_page)
            self._selected_overlay_id = None
            self._refresh_preview()

    def _start(self) -> None:
        if self._is_processing:
            return
        if self._review_mode and self._review_session:
            self._set_processing(True)
            threading.Thread(target=self._run_finalize_worker, args=(self._review_session,), daemon=True).start()
            return
        path_text = self.input_path.get().strip()
        output_text = self.output_path.get().strip()
        keywords = normalize_keywords(self.keyword_box.get("1.0", "end-1c"))
        if not path_text:
            messagebox.showwarning("缺少文件", "请选择需要处理的 PDF 文件。", parent=self.master)
            return
        if not keywords:
            messagebox.showwarning("缺少关键词", "请至少输入一个关键词（一行一个）。", parent=self.master)
            return
        try:
            parse_color(self.color.get())
        except ValueError as exc:
            messagebox.showwarning("颜色格式错误", str(exc), parent=self.master)
            return

        self._clear_log()
        self._append_log("正在生成遮盖预览。原始文件不会被修改。")
        request = RedactionRequest(
            input_path=Path(path_text),
            keywords=keywords,
            color=self.color.get().upper(),
            password=self.password.get() or None,
            output_path=Path(output_text) if output_text else None,
            manual_rects={page: list(rects) for page, rects in self._manual_rects.items()},
            processing_mode=ProcessingMode(self.processing_mode.get()),
        )
        self._review_task_counter += 1
        task_id = self._review_task_counter
        self._active_review_task = task_id
        self._set_processing(True)
        worker = threading.Thread(target=self._run_review_worker, args=(request, task_id), daemon=True)
        worker.start()

    def _run_review_worker(self, request: RedactionRequest, task_id: int) -> None:
        try:
            session = self._processor.prepare_review(
                request,
                status=lambda message: self._events.put(("review_log", (task_id, message))),
            )
            self._events.put(("review", (task_id, session)))
        except Exception as exc:
            self._events.put(("review_error", (task_id, exc)))

    def _run_finalize_worker(self, session: ReviewSession) -> None:
        try:
            result = self._processor.finalize_review(session, status=lambda message: self._events.put(("log", message)))
            self._events.put(("done", result))
        except Exception as exc:
            self._events.put(("error", exc))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, value = self._events.get_nowait()
                if kind == "log":
                    self._append_log(str(value))
                elif kind == "review_log":
                    task_id, message = value  # type: ignore[misc]
                    if task_id == self._active_review_task:
                        self._append_log(str(message))
                elif kind == "review":
                    task_id, session = value  # type: ignore[misc]
                    if task_id == self._active_review_task:
                        self._active_review_task = None
                        self._review_ready(session if isinstance(session, ReviewSession) else None)
                elif kind == "review_error":
                    task_id, error = value  # type: ignore[misc]
                    if task_id == self._active_review_task:
                        self._active_review_task = None
                        self._fail(error if isinstance(error, Exception) else RuntimeError(str(error)))
                elif kind == "done":
                    self._finish(value if isinstance(value, RedactionResult) else None)
                elif kind == "error":
                    self._fail(value if isinstance(value, Exception) else RuntimeError(str(value)))
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _review_ready(self, session: Optional[ReviewSession]) -> None:
        self._set_processing(False)
        if not session:
            self._fail(RuntimeError("遮盖预览无效。"))
            return
        self._review_session = session
        self._review_mode = True
        self._selected_overlay_id = None
        self._set_primary_action(True)
        self._append_log("遮盖预览已生成：点击遮盖框后可删除；在空白处拖动可新增遮盖；确认后点击“确认生成 PDF”。")
        self._append_page_match_logs(session)
        self._refresh_preview()

    def _append_page_match_logs(self, session: ReviewSession) -> None:
        for page_index in sorted(session.page_matches):
            cross = {keyword: next_page for keyword, next_page in session.cross_page_continuations.get(page_index, [])}
            fields = "、".join(f"“{keyword}”（跨页，续至第 {cross[keyword]} 页）" if keyword in cross else f"“{keyword}”" for keyword in session.page_matches[page_index])
            self._append_page_link_log(page_index, fields)

    def _append_page_link_log(self, page_index: int, fields: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", "命中页：")
        start = self.log_box.index("end-1c")
        label = f"第 {page_index + 1} 页"
        self.log_box.insert("end", label)
        end = self.log_box.index("end-1c")
        self.log_box.insert("end", f" — 字段：{fields}\n")
        tag = f"page_link_{self._log_link_counter}"
        self._log_link_counter += 1
        self.log_box.tag_add(tag, start, end)
        self.log_box.tag_config(tag, foreground="#1565C0", underline=True)
        self.log_box.tag_bind(tag, "<Button-1>", lambda _event, target=page_index: self._jump_to_preview_page(target))
        self.log_box.configure(state="disabled")

    def _jump_to_preview_page(self, page_index: int) -> None:
        self._preview_page = page_index
        self._refresh_preview()

    def _finish(self, result: Optional[RedactionResult]) -> None:
        self._set_processing(False)
        self.password.set("")
        self._review_mode = False
        self._review_session = None
        self._selected_overlay_id = None
        self._set_primary_action(False)
        if result is None:
            self._fail(RuntimeError("处理结果无效。"))
            return
        self.output_path.set(str(result.output_path))
        if result.succeeded:
            messagebox.showinfo("处理完成", f"已生成：\n{result.output_path}\n\n安全校验通过。\n注意：请仔细检查印章部分（OCR识别该部分能力较弱）", parent=self.master)
        else:
            messagebox.showwarning(
                "安全校验失败",
                "文件已生成，但其中仍可搜索到部分关键词。请勿将该文件视为安全成品，并查看处理日志。",
                parent=self.master,
            )

    def _fail(self, error: Exception) -> None:
        self._set_processing(False)
        self.password.set("")
        message = str(error) if isinstance(error, (RedactionError, RuntimeError)) else f"处理失败：{error}"
        self._append_log(message)
        messagebox.showerror("处理失败", message, parent=self.master)

    def _set_processing(self, processing: bool) -> None:
        self._is_processing = processing
        state = "disabled" if processing else "normal"
        for widget in (
            self.start_button,
            self.browse_button,
            self.path_entry,
            self.password_entry,
            self.output_entry,
            self.output_browse_button,
            self.color_entry,
            self.color_button,
            self.comprehensive_mode_button,
            self.fast_mode_button,
            self.preview_refresh_button,
            self.preview_jump_entry,
            self.preview_jump_button,
            self.delete_overlay_button,
        ):
            widget.configure(state=state)
        self._update_preview_controls()

    def _set_primary_action(self, review: bool) -> None:
        if review:
            self.start_button.configure(text="确认生成 PDF", background="#2E7D32", activebackground="#1B5E20")
            self.action_hint.configure(text="第 2 步：确认遮盖内容并生成最终 PDF", foreground="#2E7D32")
        else:
            self.start_button.configure(text="生成遮盖预览", background="#1565C0", activebackground="#0D47A1")
            self.action_hint.configure(text="第 1 步：生成遮盖预览并核对结果", foreground="#1565C0")

    def _append_log(self, message: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self._log_link_counter = 0


def run() -> None:
    root = tk.Tk()
    root.title("PDF 自动打码工具")
    root.minsize(1080, 680)
    RedactionApp(root)
    root.mainloop()
