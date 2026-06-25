from __future__ import annotations

import json
import queue
import shutil
import threading
import tkinter as tk
from threading import Event
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from app_icon import apply_window_icon
from deps_installer import (
    SOURCE_URLS,
    ensure_dependencies,
    missing_components,
    optional_components,
)
from clip_engine import ClipLocalJob, ClipRunner, ClipUrlJob
from clip_page import ClipPage
from engine import (
    DownloadCancelled,
    Downloader,
    extract_urls,
    format_label,
    inspect_environment,
)
from i18n import (
    FORMAT_KEYS,
    LANG_LABELS,
    LANGUAGES,
    format_option_labels,
    get_language,
    init_language,
    language_code_from_label,
    resolve_format_key,
    set_language,
    t,
    ui_font_family,
)
from ui_styles import (
    C,
    accent_button,
    apply_theme,
    card_frame,
    compact_button,
    danger_ghost_button,
    folder_picker_button,
    ghost_button,
    primary_button,
    rounded_entry,
    styled_text,
)
from paths_config import (
    APP_DIR,
    CONFIG_PATH,
    InstallPaths,
    detected_install_paths,
    find_ffmpeg_exe,
    find_node_exe,
    normalize_install_targets,
    save_install_paths,
    ytdlp_is_ready,
)
from updater import (
    RELEASE_PAGE,
    ReleaseInfo,
    apply_update_and_restart,
    can_self_update,
    check_for_update,
    current_version_label,
    default_update_download_path,
    download_release,
)
from version import __version__

URL_TEXT_LINES = 3
PROGRESS_HEIGHT = 10


def _default_url_pane_height(master: tk.Misc) -> int:
    """Pane height that fits the link card with three text lines."""
    font = tkfont.Font(master=master, font=(ui_font_family(), 10))
    text_h = font.metrics("linespace") * URL_TEXT_LINES + 28
    return int(36 + text_h + 108)


class YouTubeDownloaderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.minsize(820, 700)
        self.geometry("900x860")

        apply_theme(self)
        apply_window_icon(self)
        self._config = self._load_config()
        saved_lang = self._config.get("language", "")
        init_language(saved_lang if saved_lang in LANGUAGES else None)
        self.title(t("app.title"))
        self._dir_is_placeholder = False
        self._clip_dir_is_placeholder = False
        self._clip_cache_dir_is_placeholder = False
        self._install_paths = InstallPaths.from_config(self._config)
        normalized = normalize_install_targets(self._install_paths)
        if normalized != self._install_paths:
            self._install_paths = normalized
            save_install_paths(normalized)
        self._env = inspect_environment(
            Path(self._config.get("download_dir", "")) or None,
            self._install_paths,
        )
        self._worker: threading.Thread | None = None
        self._cancel_flag = False
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._pending_status: Optional[tuple[float, str]] = None
        self._status_lock = threading.Lock()
        self._show_install_paths = tk.BooleanVar(
            value=bool(self._config.get("show_install_paths", False))
        )

        self._build_ui()
        self._refresh_environment()
        self.after(50, self._poll_ui_queues)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(300, self._maybe_prompt_install)
        self.after(1200, self._maybe_prompt_update)

    def _load_config(self) -> dict:
        if not CONFIG_PATH.is_file():
            return {}
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_config(self) -> None:
        paths = self._current_install_paths()
        saved_dir = self.dir_var.get().strip()
        if self._dir_is_placeholder:
            saved_dir = ""
        clip_dir = self.clip_dir_var.get().strip()
        if self._clip_dir_is_placeholder:
            clip_dir = ""
        clip_cache_dir = self.clip_cache_dir_var.get().strip()
        if self._clip_cache_dir_is_placeholder:
            clip_cache_dir = ""
        data = {
            "download_dir": saved_dir or str(self._env.download_dir),
            "clip_dir": clip_dir or str(self._default_clip_dir()),
            "clip_cache_dir": clip_cache_dir or str(self._default_clip_cache_dir()),
            "format": self._format_key(),
            "subtitles": self.subtitles_var.get(),
            "cookie_file": self.cookie_var.get().strip(),
            "install_paths": paths.to_config_dict(),
            "show_install_paths": self._show_install_paths.get(),
            "language": get_language(),
        }
        pane_height = self._url_pane_height()
        if pane_height is not None:
            data["url_pane_height"] = pane_height
        CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._install_paths = paths

    def _current_install_paths(self) -> InstallPaths:
        """Install targets used by the installer (not only UI display paths)."""
        return self._install_paths

    def _sync_detected_path_display(self) -> None:
        display = detected_install_paths(self._install_paths)
        self.ytdlp_dir_var.set(str(display.ytdlp_dir))
        self.ffmpeg_dir_var.set(str(display.ffmpeg_dir))
        self.nodejs_dir_var.set(str(display.nodejs_dir))

    def _browse_install_dir(
        self, var: tk.StringVar, title: str, component: str
    ) -> None:
        initial = var.get().strip() or str(APP_DIR)
        chosen = self._pick_folder(title, initial)
        if not chosen:
            return
        chosen_path = Path(chosen)
        var.set(chosen)
        if component == "ytdlp":
            self._install_paths = InstallPaths(
                chosen_path,
                self._install_paths.ffmpeg_dir,
                self._install_paths.nodejs_dir,
            )
        elif component == "ffmpeg":
            self._install_paths = InstallPaths(
                self._install_paths.ytdlp_dir,
                chosen_path,
                self._install_paths.nodejs_dir,
            )
        else:
            self._install_paths = InstallPaths(
                self._install_paths.ytdlp_dir,
                self._install_paths.ffmpeg_dir,
                chosen_path,
            )
        save_install_paths(self._install_paths)
        self._refresh_environment()

    def _toggle_install_paths(self) -> None:
        self._show_install_paths.set(not self._show_install_paths.get())
        self._apply_install_paths_visibility()
        self._save_config()

    def _select_log_tab(self) -> None:
        if hasattr(self, "_notebook"):
            self._notebook.select(2)

    def _on_language_changed(self, _event: Optional[object] = None) -> None:
        code = language_code_from_label(self.lang_var.get())
        if not code or code == get_language():
            return
        set_language(code)
        self._apply_language()
        self._save_config()

    def _apply_language(self) -> None:
        self.title(t("app.title"))
        self._header_title.configure(text=t("app.title"))
        self._header_subtitle.configure(text=t("app.subtitle"))
        self._lang_label.configure(text=t("lang.label"))
        self._update_btn.configure(text=t("update.check"))
        self._version_lbl.configure(text=t("update.current", version=__version__))
        self._url_card.set_title(t("url.card"))
        self._paste_btn.configure(text=t("url.paste"))
        self._clear_btn.configure(text=t("url.clear"))
        self._url_resize_lbl.configure(text=t("url.resize_hint"))
        self._options_card.set_title(t("options.card"))
        self._quality_lbl.configure(text=t("options.quality"))
        self._subtitles_chk.configure(text=t("options.subtitles"))
        self._save_lbl.configure(text=t("options.save"))
        self._pick_folder_btn.configure(text=t("options.pick_folder"))
        self._open_folder_btn.configure(text=t("options.open_folder"))
        self._cookies_lbl.configure(text=t("options.cookies"))
        self._cookies_pick_btn.configure(text=t("options.cookies_pick"))
        self._cookies_hint.configure(text=t("options.cookies_hint"))
        self.download_btn.configure(text=t("action.download"))
        self.cancel_btn.configure(text=t("action.cancel"))
        if self.status_var.get() in (
            t("status.ready"),
            "就绪",
            "Ready",
            "Готово",
        ):
            self.status_var.set(t("status.ready"))
        self._paths_title_lbl.configure(text=t("paths.title"))
        for btn, title_key in self._path_browse_btns:
            btn.configure(text=t("paths.browse"))
        self._sources_lbl.configure(
            text=t(
                "sources.footer",
                ytdlp=SOURCE_URLS["yt-dlp"],
                ffmpeg=SOURCE_URLS["ffmpeg"],
                nodejs=SOURCE_URLS["node.js"],
            )
        )
        self.log_frame.set_title(t("log.card"))
        self.install_all_btn.configure(text=t("msg.install.all"))
        self._apply_install_paths_visibility()
        key = self._format_key()
        labels = format_option_labels()
        self.format_combo.configure(values=labels)
        idx = FORMAT_KEYS.index(key) if key in FORMAT_KEYS else 0
        self.format_var.set(labels[idx])
        self.format_combo.current(idx)
        if self._dir_is_placeholder:
            self.dir_var.set(t("dir.unset"))
        self.dir_label.configure(font=(ui_font_family(), 9))
        self._env_summary.configure(font=(ui_font_family(), 9))
        self._refresh_environment()
        if hasattr(self, "_notebook"):
            self._notebook.tab(0, text=t("tab.download"))
            self._notebook.tab(1, text=t("tab.clip"))
            self._notebook.tab(2, text=t("tab.log"))
        if hasattr(self, "_clip_page"):
            self._clip_page.apply_language()
        if hasattr(self, "_clip_save_lbl"):
            self._clip_save_lbl.configure(text=t("clip.save"))
            self._clip_pick_folder_btn.configure(text=t("clip.pick_folder"))
            self._clip_open_folder_btn.configure(text=t("clip.open_folder"))
        if hasattr(self, "_clip_cache_save_lbl"):
            self._clip_cache_save_lbl.configure(text=t("clip.cache"))
            self._clip_cache_pick_btn.configure(text=t("clip.pick_cache"))
            self._clip_cache_open_btn.configure(text=t("clip.open_cache"))
            self._clip_cache_clear_btn.configure(text=t("clip.clear_cache"))
        if hasattr(self, "_clip_paths_card"):
            self._clip_paths_card.set_title(t("clip.paths_card"))
        if hasattr(self, "_clip_cache_hint_lbl"):
            self._clip_cache_hint_lbl.configure(text=t("clip.cache_hint"))

    def _apply_install_paths_visibility(self) -> None:
        if self._show_install_paths.get():
            self.paths_content.pack(
                fill="x",
                padx=12,
                pady=(0, 2),
                before=self.env_btns,
            )
            self._paths_toggle_btn.configure(text=t("paths.hide"))
        else:
            self.paths_content.pack_forget()
            self._paths_toggle_btn.configure(text=t("paths.show"))

    def _build_ui(self) -> None:
        pad_x = {"padx": 12}
        section = {"padx": 12, "pady": 4}

        outer = ttk.Frame(self, padding=(12, 8))
        outer.pack(fill="both", expand=True)

        self._header_title = ttk.Label(
            outer, text=t("app.title"), style="Header.TLabel"
        )
        self._header_title.pack(anchor="w", **pad_x)
        self._header_subtitle = ttk.Label(
            outer,
            text=t("app.subtitle"),
            style="Muted.TLabel",
        )
        self._header_subtitle.pack(anchor="w", padx=12, pady=(0, 4))

        lang_row = ttk.Frame(outer)
        lang_row.pack(fill="x", padx=12, pady=(0, 6))
        self._lang_label = ttk.Label(lang_row, text=t("lang.label"), style="Card.TLabel")
        self._lang_label.pack(side="left")
        lang_names = [LANG_LABELS[code] for code in LANGUAGES]
        self.lang_var = tk.StringVar(value=LANG_LABELS[get_language()])
        self.lang_combo = ttk.Combobox(
            lang_row,
            textvariable=self.lang_var,
            values=lang_names,
            state="readonly",
            width=14,
            style="Flat.TCombobox",
        )
        self.lang_combo.pack(side="left", padx=8)
        self.lang_combo.bind("<<ComboboxSelected>>", self._on_language_changed)
        ttk.Frame(lang_row).pack(side="left", fill="x", expand=True)
        self._update_btn = ghost_button(
            lang_row, t("update.check"), self._check_for_updates
        )
        self._update_btn.pack(side="right")
        self._version_lbl = ttk.Label(
            lang_row,
            text=t("update.current", version=__version__),
            style="Muted.TLabel",
        )
        self._version_lbl.pack(side="right", padx=(0, 8))

        self._notebook = ttk.Notebook(outer)
        self._notebook.pack(fill=tk.BOTH, expand=True, pady=(0, 4))

        download_tab = ttk.Frame(self._notebook)
        clip_tab = ttk.Frame(self._notebook)
        log_tab = ttk.Frame(self._notebook)
        self._notebook.add(download_tab, text=t("tab.download"))
        self._notebook.add(clip_tab, text=t("tab.clip"))
        self._notebook.add(log_tab, text=t("tab.log"))

        self._url_pane = tk.PanedWindow(
            download_tab,
            orient=tk.VERTICAL,
            sashrelief=tk.RAISED,
            sashwidth=7,
            sashpad=1,
            bg=C["border"],
            bd=0,
            opaqueresize=True,
        )
        self._url_pane.pack(fill=tk.BOTH, expand=True)

        url_section = tk.Frame(self._url_pane, bg=C["bg"])
        bottom_section = tk.Frame(self._url_pane, bg=C["bg"])
        default_url_h = _default_url_pane_height(self)
        self._url_pane.add(url_section, minsize=default_url_h - 24)
        self._url_pane.add(bottom_section, minsize=300)

        self._url_card = card_frame(
            url_section,
            text=t("url.card"),
            padding=6,
            expand_vertical=True,
        )
        self._url_card.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)

        url_btns = ttk.Frame(self._url_card.content, style="Card.TFrame")
        url_btns.pack(fill="x", padx=4, pady=(0, 4))
        self._paste_btn = ghost_button(url_btns, t("url.paste"), self._paste_clipboard)
        self._paste_btn.pack(side="left")
        self._clear_btn = ghost_button(
            url_btns,
            t("url.clear"),
            lambda: self.url_text.delete("1.0", "end"),
        )
        self._clear_btn.pack(side="left", padx=8)

        url_body = tk.Frame(self._url_card.content, bg=C["surface"])
        url_body.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 2))
        self.url_text = styled_text(url_body, height=URL_TEXT_LINES, expand=True)

        self._url_resize_lbl = ttk.Label(
            url_section,
            text=t("url.resize_hint"),
            style="Muted.TLabel",
        )
        self._url_resize_lbl.pack(anchor="w", padx=14, pady=(0, 2))

        self._options_card = card_frame(bottom_section, text=t("options.card"), padding=8)
        self._options_card.pack(fill="x", **section)

        row1 = ttk.Frame(self._options_card.content, style="Card.TFrame")
        row1.pack(fill="x", pady=2)
        self._quality_lbl = ttk.Label(row1, text=t("options.quality"), style="Card.TLabel")
        self._quality_lbl.pack(side="left")
        saved_key = resolve_format_key(self._config.get("format", "best"))
        labels = format_option_labels()
        self.format_var = tk.StringVar(value=labels[FORMAT_KEYS.index(saved_key)])
        self.format_combo = ttk.Combobox(
            row1,
            textvariable=self.format_var,
            state="readonly",
            width=18,
            values=labels,
            style="Flat.TCombobox",
        )
        self.format_combo.pack(side="left", padx=8)
        self.format_combo.current(FORMAT_KEYS.index(saved_key))

        self.subtitles_var = tk.BooleanVar(value=bool(self._config.get("subtitles", False)))
        self._subtitles_chk = ttk.Checkbutton(
            row1,
            text=t("options.subtitles"),
            variable=self.subtitles_var,
            style="Card.TCheckbutton",
        )
        self._subtitles_chk.pack(side="left", padx=10)

        row2 = ttk.Frame(self._options_card.content, style="Card.TFrame")
        row2.pack(fill="x", pady=2)
        self._save_lbl = ttk.Label(row2, text=t("options.save"), style="Card.TLabel")
        self._save_lbl.pack(side="left")
        saved_dir = self._config.get("download_dir", "").strip()
        self.dir_var = tk.StringVar(value=saved_dir or str(self._env.download_dir))
        self._pick_folder_btn = folder_picker_button(
            row2, t("options.pick_folder"), self._choose_dir
        )
        self._pick_folder_btn.pack(side="left", padx=(4, 6))
        self._open_folder_btn = ghost_button(
            row2, t("options.open_folder"), self._open_download_dir
        )
        self._open_folder_btn.pack(side="left")

        row2_path = ttk.Frame(self._options_card.content, style="Card.TFrame")
        row2_path.pack(fill="x", pady=(0, 2))
        self.dir_label = tk.Label(
            row2_path,
            textvariable=self.dir_var,
            anchor="w",
            justify="left",
            bg=C["surface"],
            fg=C["text_secondary"],
            font=(ui_font_family(), 9),
            wraplength=820,
        )
        self.dir_label.pack(fill="x", padx=4)
        if not self.dir_var.get().strip():
            self._dir_is_placeholder = True
            self.dir_var.set(t("dir.unset"))

        row3 = ttk.Frame(self._options_card.content, style="Card.TFrame")
        row3.pack(fill="x", pady=2)
        self._cookies_lbl = ttk.Label(row3, text=t("options.cookies"), style="Card.TLabel")
        self._cookies_lbl.pack(side="left")
        self.cookie_var = tk.StringVar(value=self._config.get("cookie_file", ""))
        cookie_field = rounded_entry(row3, self.cookie_var)
        cookie_field.pack(side="left", fill="x", expand=True, padx=8)
        self._cookies_pick_btn = ghost_button(row3, t("options.cookies_pick"), self._choose_cookie)
        self._cookies_pick_btn.pack(side="left")

        self._cookies_hint = tk.Label(
            self._options_card.content,
            text=t("options.cookies_hint"),
            anchor="w",
            justify="left",
            bg=C["surface"],
            fg=C["text_secondary"],
            font=(ui_font_family(), 9),
            wraplength=820,
        )
        self._cookies_hint.pack(fill="x", padx=4, pady=(0, 2))

        action = ttk.Frame(bottom_section)
        action.pack(fill="x", padx=12, pady=(6, 2))

        self.download_btn = primary_button(action, t("action.download"), self._start_download)
        self.download_btn.pack(side="left")

        self.cancel_btn = danger_ghost_button(action, t("action.cancel"), self._cancel_download)
        self.cancel_btn.configure(state="disabled")
        self.cancel_btn.pack(side="left", padx=10)

        saved_clip = self._config.get("clip_dir", "").strip()
        if saved_clip:
            self.clip_dir_var = tk.StringVar(value=saved_clip)
        else:
            default_clip = (self._download_dir_from_ui() or self._env.download_dir) / "Clips"
            self.clip_dir_var = tk.StringVar(value=str(default_clip))

        saved_cache = self._config.get("clip_cache_dir", "").strip()
        if saved_cache:
            self.clip_cache_dir_var = tk.StringVar(value=saved_cache)
        else:
            self.clip_cache_dir_var = tk.StringVar(
                value=str(self._default_clip_cache_dir())
            )

        self._clip_page = ClipPage(clip_tab, self)
        self._clip_page.pack(fill=tk.BOTH, expand=True)

        self.log_frame = card_frame(
            log_tab, text=t("log.card"), padding=8, expand_vertical=True
        )
        self.log_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        log_body = tk.Frame(self.log_frame.content, bg=C["surface"])
        log_body.pack(fill=tk.BOTH, expand=True)
        log_scroll = ttk.Scrollbar(log_body)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text = tk.Text(
            log_body,
            height=24,
            wrap="word",
            font=(ui_font_family(), 10),
            state="disabled",
            bg=C["surface_alt"],
            fg=C["text"],
            insertbackground=C["text"],
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=8,
            pady=6,
            yscrollcommand=log_scroll.set,
        )
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.config(command=self.log_text.yview)

        progress_row = ttk.Frame(outer)
        progress_row.pack(fill="x", padx=12, pady=(4, 2))
        self._progress_pct = 0.0
        self._progress_track = tk.Frame(
            progress_row,
            bg=C["border"],
            height=PROGRESS_HEIGHT,
            highlightthickness=0,
        )
        self._progress_track.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self._progress_track.pack_propagate(False)
        self._progress_fill = tk.Frame(
            self._progress_track,
            bg=C["accent"],
            height=PROGRESS_HEIGHT,
            highlightthickness=0,
        )
        self._progress_track.bind(
            "<Configure>",
            lambda _event: self._set_progress_value(self._progress_pct),
        )
        self.status_var = tk.StringVar(value=t("status.ready"))
        ttk.Label(progress_row, textvariable=self.status_var).pack(side="right")

        shared_section = ttk.Frame(outer)
        shared_section.pack(fill=tk.BOTH, expand=True)

        env_row = ttk.Frame(shared_section)
        env_row.pack(fill="x", padx=12, pady=(0, 2))

        self._env_summary = tk.Label(
            env_row,
            text=t("env.checking"),
            anchor="w",
            bg=C["bg"],
            fg=C["text_secondary"],
            font=(ui_font_family(), 9),
        )
        self._env_summary.pack(fill="x")

        paths_header = ttk.Frame(shared_section)
        paths_header.pack(fill="x", padx=12, pady=(2, 0))
        self._paths_toggle_btn = ghost_button(
            paths_header,
            t("paths.show"),
            self._toggle_install_paths,
        )
        self._paths_toggle_btn.pack(anchor="w")

        self.paths_content = ttk.Frame(shared_section)
        self._paths_title_lbl = ttk.Label(
            self.paths_content,
            text=t("paths.title"),
            style="Muted.TLabel",
        )
        self._paths_title_lbl.pack(anchor="w")

        display_paths = detected_install_paths(self._install_paths)
        self.ytdlp_dir_var = tk.StringVar(value=str(display_paths.ytdlp_dir))
        self.ffmpeg_dir_var = tk.StringVar(value=str(display_paths.ffmpeg_dir))
        self.nodejs_dir_var = tk.StringVar(value=str(display_paths.nodejs_dir))

        self._path_browse_btns: list[tuple[object, str]] = []
        for label, var, title_key, component in (
            ("yt-dlp", self.ytdlp_dir_var, "paths.browse_ytdlp", "ytdlp"),
            ("ffmpeg", self.ffmpeg_dir_var, "paths.browse_ffmpeg", "ffmpeg"),
            ("Node.js", self.nodejs_dir_var, "paths.browse_node", "nodejs"),
        ):
            row = ttk.Frame(self.paths_content)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=f"{label}:", width=8).pack(side="left")
            field = rounded_entry(row, var)
            field.pack(side="left", fill="x", expand=True, padx=(0, 6))
            browse_btn = ghost_button(
                row,
                t("paths.browse"),
                lambda v=var, k=title_key, c=component: self._browse_install_dir(
                    v, t(k), c
                ),
            )
            browse_btn.pack(side="left")
            self._path_browse_btns.append((browse_btn, title_key))

        env_btns = ttk.Frame(shared_section)
        env_btns.pack(fill="x", padx=12, pady=(2, 0))
        self.env_btns = env_btns

        self.ytdlp_btn = accent_button(
            env_btns, "↓ yt-dlp", lambda: self._install_component("yt-dlp")
        )
        self.ytdlp_btn.pack(side="left", padx=(0, 4))
        self.ffmpeg_btn = accent_button(
            env_btns, "↓ ffmpeg", lambda: self._install_component("ffmpeg")
        )
        self.ffmpeg_btn.pack(side="left", padx=(0, 4))
        self.install_node_btn = accent_button(
            env_btns, "↓ Node.js", lambda: self._install_component("node.js")
        )
        self.install_node_btn.pack(side="left", padx=(0, 4))
        self.install_all_btn = accent_button(
            env_btns, t("msg.install.all"), self._install_all
        )
        self.install_all_btn.pack(side="left", padx=(0, 4))
        compact_button(env_btns, "↻", self._refresh_environment).pack(side="left")

        self._sources_lbl = tk.Label(
            shared_section,
            text=t(
                "sources.footer",
                ytdlp=SOURCE_URLS["yt-dlp"],
                ffmpeg=SOURCE_URLS["ffmpeg"],
                nodejs=SOURCE_URLS["node.js"],
            ),
            anchor="w",
            bg=C["bg"],
            fg=C["text_secondary"],
            font=(ui_font_family(), 8),
            wraplength=860,
            justify="left",
        )
        self._sources_lbl.pack(fill="x", padx=12, pady=(2, 4))

        self._apply_install_paths_visibility()
        self.after_idle(self._restore_url_pane_height)

    def _restore_url_pane_height(self) -> None:
        saved = self._config.get("url_pane_height")
        default_h = _default_url_pane_height(self)
        if saved:
            try:
                height = int(saved)
            except (TypeError, ValueError):
                height = default_h
        else:
            height = default_h
        if height < default_h - 20:
            height = default_h
        try:
            self.update_idletasks()
            self._url_pane.sash_place(0, 0, height)
        except (tk.TclError, ValueError, TypeError):
            pass

    def _url_pane_height(self) -> Optional[int]:
        try:
            return int(self._url_pane.sash_coord(0)[1])
        except (tk.TclError, ValueError, AttributeError):
            return None

    def _compact_env_summary(self) -> str:
        y = "✓" if self._env.yt_dlp_ready else "✗"
        f = "✓" if self._env.ffmpeg_available else "✗"
        n = "✓" if self._env.js_runtime_ready else "✗"
        not_inst = t("env.not_installed")
        installed = t("env.installed")
        y_ver = self._env.yt_dlp_version if self._env.yt_dlp_ready else not_inst
        f_hint = self._env.ffmpeg_source if self._env.ffmpeg_available else not_inst
        n_hint = installed if self._env.js_runtime_ready else not_inst
        return t(
            "env.summary",
            y=y,
            y_ver=y_ver,
            f=f,
            f_hint=f_hint,
            n=n,
            n_hint=n_hint,
        )

    def _format_key(self) -> str:
        label = self.format_var.get()
        for key in FORMAT_KEYS:
            if label == t(f"format.{key}"):
                return key
        return "best"

    def _default_clip_dir(self) -> Path:
        download = self._download_dir_from_ui() or self._env.download_dir
        return download / "Clips"

    def _default_clip_cache_dir(self) -> Path:
        import tempfile

        return Path(tempfile.gettempdir()) / "YouTubeDownloader_clip_cache"

    def _download_dir_from_ui(self) -> Optional[Path]:
        if self._dir_is_placeholder:
            return None
        raw = self.dir_var.get().strip()
        if not raw:
            return None
        try:
            return Path(raw)
        except Exception:
            return None

    def _clip_dir_from_ui(self) -> Optional[Path]:
        if self._clip_dir_is_placeholder:
            return None
        raw = self.clip_dir_var.get().strip()
        if not raw:
            return None
        try:
            return Path(raw)
        except Exception:
            return None

    def _clip_cache_dir_from_ui(self) -> Optional[Path]:
        if self._clip_cache_dir_is_placeholder:
            return None
        raw = self.clip_cache_dir_var.get().strip()
        if not raw:
            return None
        try:
            return Path(raw)
        except Exception:
            return None

    def _pick_folder(self, title: str, current: str = "") -> Optional[str]:
        self.update_idletasks()
        self.lift()
        self.focus_force()
        self.attributes("-topmost", True)
        self.update()
        try:
            chosen = filedialog.askdirectory(
                initialdir=self._dialog_initial_dir(
                    current or self.dir_var.get().strip()
                ),
                title=title,
                parent=self,
            )
            return chosen or None
        finally:
            self.attributes("-topmost", False)
            self.lift()
            self.focus_force()

    def _set_download_dir(self, folder: str) -> None:
        self._dir_is_placeholder = False
        self.dir_var.set(folder)
        self._save_config()

    def _refresh_environment(self) -> None:
        download_dir = self._download_dir_from_ui()
        self._env = inspect_environment(download_dir, self._install_paths)
        self._sync_detected_path_display()
        if self._dir_is_placeholder or not self.dir_var.get().strip():
            self._dir_is_placeholder = False
            self.dir_var.set(str(self._env.download_dir))

        self._env_summary.configure(text=self._compact_env_summary())
        if self._env.missing or self._env.optional:
            self._env_summary.configure(fg=C["accent"])
        else:
            self._env_summary.configure(fg=C["success"])

        self.ytdlp_btn.configure(
            state="normal" if not self._env.yt_dlp_ready else "disabled"
        )
        self.ffmpeg_btn.configure(
            state="normal" if not self._env.ffmpeg_available else "disabled"
        )
        self.install_node_btn.configure(
            state="normal" if not self._env.js_runtime_ready else "disabled"
        )
        need_any = bool(self._env.missing or self._env.optional)
        self.install_all_btn.configure(
            text=t("msg.install.all"),
            state="normal" if need_any else "disabled",
        )

    def _set_progress_value(self, percent: float) -> None:
        pct = max(0.0, min(100.0, float(percent)))
        self._progress_pct = pct
        try:
            self._progress_track.update_idletasks()
            width = self._progress_track.winfo_width()
            if width <= 1:
                return
            fill_w = int(width * pct / 100.0)
            if pct > 0 and fill_w < 2:
                fill_w = 2
            self._progress_fill.place(x=0, y=0, width=fill_w, relheight=1.0)
        except tk.TclError:
            pass

    def _poll_ui_queues(self) -> None:
        """Main-thread timer: flush log/status from worker threads."""
        try:
            lines: list[str] = []
            while len(lines) < 300:
                try:
                    lines.append(self._log_queue.get_nowait())
                except queue.Empty:
                    break
            if lines:
                self.log_text.configure(state="normal")
                self.log_text.insert("end", "\n".join(lines) + "\n")
                try:
                    line_count = int(float(self.log_text.index("end-1c").split(".")[0]))
                except (tk.TclError, ValueError):
                    line_count = 0
                if line_count > 6000:
                    self.log_text.delete("1.0", "2500.0")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except tk.TclError:
            pass

        try:
            with self._status_lock:
                pending = self._pending_status
                self._pending_status = None
            if pending is not None:
                pct, msg = pending
                self._set_progress_value(pct)
                self.status_var.set(msg)
        except tk.TclError:
            pass

        delay = 20 if not self._log_queue.empty() else 60
        self.after(delay, self._poll_ui_queues)

    def _flush_ui_queues(self) -> None:
        """Drain pending UI updates immediately (call on main thread)."""
        try:
            lines: list[str] = []
            while True:
                try:
                    lines.append(self._log_queue.get_nowait())
                except queue.Empty:
                    break
            if lines:
                self.log_text.configure(state="normal")
                self.log_text.insert("end", "\n".join(lines) + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
            with self._status_lock:
                pending = self._pending_status
                self._pending_status = None
            if pending is not None:
                pct, msg = pending
                self._set_progress_value(pct)
                self.status_var.set(msg)
        except tk.TclError:
            pass

    def _clear_log(self) -> None:
        try:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass
        while True:
            try:
                self._log_queue.get_nowait()
            except queue.Empty:
                break

    def _append_log(self, message: str) -> None:
        text = message.rstrip()
        if text:
            self._log_queue.put(text)

    def _set_status(self, percent: float, message: str) -> None:
        with self._status_lock:
            self._pending_status = (percent, message)

    def _paste_clipboard(self) -> None:
        try:
            text = self.clipboard_get()
        except tk.TclError:
            messagebox.showwarning(t("msg.clipboard.title"), t("msg.clipboard.empty"))
            return
        self.url_text.insert("end", text.strip() + "\n")

    def _dialog_initial_dir(self, current: str) -> str:
        if self._dir_is_placeholder:
            current = ""
        try:
            path = Path(current) if current else self._env.download_dir
        except Exception:
            path = self._env.download_dir
        if path.is_dir():
            return str(path)
        if path.parent.is_dir():
            return str(path.parent)
        fallback = Path.home() / "Downloads"
        return str(fallback if fallback.is_dir() else Path.home())

    def _choose_dir(self) -> None:
        chosen = self._pick_folder(t("msg.dir.pick_title"))
        if not chosen:
            return
        try:
            Path(chosen).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showwarning(
                t("msg.dir.bad_title"),
                t("msg.dir.bad", err=exc),
                parent=self,
            )
            return
        self._set_download_dir(chosen)
        messagebox.showinfo(
            t("msg.dir.ok_title"),
            t("msg.dir.ok", path=chosen),
            parent=self,
        )

    def _set_clip_dir(self, folder: str) -> None:
        self._clip_dir_is_placeholder = False
        self.clip_dir_var.set(folder)
        if hasattr(self, "_clip_dir_label"):
            self._clip_dir_label.configure(fg=C["text_secondary"])
        self._save_config()

    def _choose_clip_dir(self) -> None:
        initial = self.clip_dir_var.get().strip()
        if self._clip_dir_is_placeholder:
            initial = str(self._default_clip_dir())
        chosen = self._pick_folder(t("clip.pick_folder_title"), initial)
        if not chosen:
            return
        try:
            Path(chosen).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showwarning(
                t("msg.dir.bad_title"),
                t("msg.dir.bad", err=exc),
                parent=self,
            )
            return
        self._set_clip_dir(chosen)
        messagebox.showinfo(
            t("msg.dir.ok_title"),
            t("clip.dir.ok", path=chosen),
            parent=self,
        )

    def _set_clip_cache_dir(self, folder: str) -> None:
        self._clip_cache_dir_is_placeholder = False
        self.clip_cache_dir_var.set(folder)
        if hasattr(self, "_clip_cache_dir_label"):
            self._clip_cache_dir_label.configure(fg=C["text_secondary"])

    def _choose_clip_cache_dir(self) -> None:
        initial = self.clip_cache_dir_var.get().strip()
        if self._clip_cache_dir_is_placeholder:
            initial = str(self._default_clip_cache_dir())
        chosen = self._pick_folder(t("clip.pick_cache_title"), initial)
        if not chosen:
            return
        try:
            Path(chosen).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                t("msg.dir.bad_title"),
                t("msg.dir.mkdir_fail", path=chosen, err=exc),
                parent=self,
            )
            return
        self._set_clip_cache_dir(chosen)
        self._save_config()
        messagebox.showinfo(
            t("clip.cache.ok_title"),
            t("clip.cache.ok", path=chosen),
            parent=self,
        )

    def _clear_clip_cache(self) -> None:
        if self._worker and self._worker.is_alive():
            messagebox.showinfo(
                t("msg.wait.title"),
                t("msg.wait.busy"),
                parent=self,
            )
            return

        path = self._clip_cache_dir_from_ui() or self._default_clip_cache_dir()
        if not path.is_dir():
            messagebox.showinfo(
                t("clip.cache.clear_title"),
                t("clip.cache.clear_empty"),
                parent=self,
            )
            return

        entries = list(path.iterdir())
        if not entries:
            messagebox.showinfo(
                t("clip.cache.clear_title"),
                t("clip.cache.clear_empty"),
                parent=self,
            )
            return

        if not messagebox.askyesno(
            t("clip.cache.clear_title"),
            t("clip.cache.clear_confirm", path=path, count=len(entries)),
            parent=self,
        ):
            return

        removed = 0
        errors: list[str] = []
        for item in entries:
            try:
                if item.is_file() or item.is_symlink():
                    item.unlink()
                    removed += 1
                elif item.is_dir():
                    shutil.rmtree(item)
                    removed += 1
            except OSError as exc:
                errors.append(f"{item.name}: {exc}")

        if errors:
            messagebox.showwarning(
                t("clip.cache.clear_partial_title"),
                t("clip.cache.clear_partial", path=path, n=removed, err="\n".join(errors)),
                parent=self,
            )
        else:
            messagebox.showinfo(
                t("clip.cache.clear_title"),
                t("clip.cache.clear_done", path=path, n=removed),
                parent=self,
            )
        self._append_log(t("log.clip.cache_cleared", path=path, n=removed))

    def _open_clip_cache_dir(self) -> None:
        path = self._clip_cache_dir_from_ui() or self._default_clip_cache_dir()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                t("msg.dir.bad_title"),
                t("msg.dir.mkdir_fail", path=path, err=exc),
                parent=self,
            )
            return
        try:
            import os

            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror(
                t("msg.dir.bad_title"),
                t("msg.dir.open_fail", path=path, err=exc),
                parent=self,
            )

    def _open_clip_dir(self) -> None:
        path = self._clip_dir_from_ui() or self._default_clip_dir()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showwarning(
                t("msg.dir.bad_title"),
                t("msg.dir.open_fail", path=path, err=exc),
                parent=self,
            )
            return
        import os

        os.startfile(path)

    def _open_download_dir(self) -> None:
        path = self._download_dir_from_ui() or self._env.download_dir
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showwarning(
                t("msg.dir.bad_title"),
                t("msg.dir.open_fail", path=path, err=exc),
                parent=self,
            )
            return
        import os

        os.startfile(path)

    def _choose_cookie(self) -> None:
        chosen = filedialog.askopenfilename(
            title=t("msg.cookie.title"),
            filetypes=[("Netscape cookies", "*.txt"), ("All files", "*.*")],
            parent=self,
        )
        if chosen:
            self.cookie_var.set(chosen)

    def _set_busy(self, busy: bool) -> None:
        self.download_btn.configure(state="disabled" if busy else "normal")
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        self.format_combo.configure(state="disabled" if busy else "readonly")
        if hasattr(self, "_clip_page"):
            self._clip_page._clip_btn.configure(state="disabled" if busy else "normal")

    def _set_install_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for btn in (
            self.ytdlp_btn,
            self.ffmpeg_btn,
            self.install_node_btn,
            self.install_all_btn,
        ):
            btn.configure(state=state)
        if not busy:
            self._refresh_environment()

    def _set_update_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self._update_btn.configure(state=state)
        self.download_btn.configure(state=state)
        if hasattr(self, "_clip_page"):
            self._clip_page._clip_btn.configure(state=state)
        self.lang_combo.configure(state="disabled" if busy else "readonly")

    def _ask_update_confirm(self, release: ReleaseInfo, *, startup: bool) -> bool:
        title_key = "update.startup_title" if startup else "update.confirm_title"
        body_key = "update.startup_prompt" if startup else "update.confirm"
        done = Event()
        choice = {"ok": False}

        def ask() -> None:
            self._append_log(t("log.update.found", tag=release.tag))
            choice["ok"] = messagebox.askyesno(
                t(title_key),
                t(
                    body_key,
                    latest=release.tag,
                    current=f"v{current_version_label()}",
                ),
                parent=self,
            )
            done.set()

        self.after(0, ask)
        done.wait(timeout=300)
        return choice["ok"]

    def _download_and_apply_update(self, release: ReleaseInfo) -> None:
        dest = default_update_download_path()

        def on_progress(pct: float, _tag: str) -> None:
            self.after(
                0,
                lambda p=pct: self._set_status(p, t("update.downloading")),
            )

        download_release(release, dest, on_progress)

        restart_done = Event()

        def on_ready() -> None:
            self._append_log(t("log.update.done"))
            messagebox.showinfo(
                t("update.confirm_title"),
                t("update.restart"),
                parent=self,
            )
            restart_done.set()

        self.after(0, on_ready)
        restart_done.wait(timeout=120)
        apply_update_and_restart(dest)
        self.after(0, self.destroy)

    def _run_update_worker(self, *, interactive: bool) -> None:
        def worker() -> None:
            try:
                has_update, release, _current = check_for_update()
                if not has_update:
                    if interactive:
                        done = Event()

                        def on_latest() -> None:
                            messagebox.showinfo(
                                t("update.latest_title"),
                                t(
                                    "update.latest",
                                    version=release.tag.lstrip("vV"),
                                ),
                                parent=self,
                            )
                            self._append_log(t("log.update.none"))
                            done.set()

                        self.after(0, on_latest)
                        done.wait(timeout=120)
                    return

                if not self._ask_update_confirm(release, startup=not interactive):
                    return

                self._download_and_apply_update(release)
            except Exception as exc:
                err = str(exc)

                def on_error() -> None:
                    self._append_log(t("log.install.fail", err=err))
                    messagebox.showerror(
                        t("update.fail_title"),
                        t("update.download_fail", err=err),
                        parent=self,
                    )

                self.after(0, on_error)
            finally:

                def on_finish() -> None:
                    self._flush_ui_queues()
                    self._set_update_busy(False)
                    self.status_var.set(t("status.ready"))

                self.after(0, on_finish)

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def _check_for_updates(self) -> None:
        if self._worker and self._worker.is_alive():
            messagebox.showinfo(t("msg.wait.title"), t("msg.wait.busy"), parent=self)
            return
        if not can_self_update():
            messagebox.showinfo(
                t("update.latest_title"),
                t("update.dev_only", url=RELEASE_PAGE),
                parent=self,
            )
            return

        self._set_update_busy(True)
        self._append_log(t("log.update.check"))
        self.status_var.set(t("update.checking"))
        self._run_update_worker(interactive=True)

    def _start_confirmed_update(self, release: ReleaseInfo) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._set_update_busy(True)
        self.status_var.set(t("update.downloading"))

        def worker() -> None:
            try:
                self._download_and_apply_update(release)
            except Exception as exc:
                err = str(exc)

                def on_error() -> None:
                    self._append_log(t("log.install.fail", err=err))
                    messagebox.showerror(
                        t("update.fail_title"),
                        t("update.download_fail", err=err),
                        parent=self,
                    )

                self.after(0, on_error)
            finally:

                def on_finish() -> None:
                    self._set_update_busy(False)
                    self.status_var.set(t("status.ready"))

                self.after(0, on_finish)

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def _maybe_prompt_update(self) -> None:
        if not can_self_update():
            return
        if self._worker and self._worker.is_alive():
            self.after(2000, self._maybe_prompt_update)
            return

        def worker() -> None:
            try:
                has_update, release, _current = check_for_update()
                if not has_update:
                    return
                if not self._ask_update_confirm(release, startup=True):
                    return
                self.after(0, lambda: self._start_confirmed_update(release))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _maybe_prompt_install(self) -> None:
        paths = self._current_install_paths()
        missing = missing_components(paths)
        optional = optional_components(paths)
        if not missing and not optional:
            return
        if missing:
            items = t("sep.list").join(missing)
            if messagebox.askyesno(
                t("msg.missing.title"),
                t("msg.missing.body", items=items),
            ):
                self._install_all()
            return
        if optional and messagebox.askyesno(
            t("msg.node.title"),
            t("msg.node.body", url=SOURCE_URLS["node.js"]),
        ):
            self._install_component("node.js")

    def _run_install_worker(
        self,
        *,
        install_ytdlp: bool,
        install_ffmpeg_tool: bool,
        install_node: bool,
        title: str,
    ) -> None:
        if self._worker and self._worker.is_alive():
            messagebox.showinfo(t("msg.wait.title"), t("msg.wait.busy"))
            return

        self._set_install_busy(True)
        self._append_log(t("log.install.start", title=title))
        self.status_var.set(t("status.installing", title=title))
        self._set_progress_value(0)

        def worker() -> None:
            try:
                paths = self._current_install_paths()
                save_install_paths(paths)
                ensure_dependencies(
                    install_ytdlp=install_ytdlp,
                    install_ffmpeg_tool=install_ffmpeg_tool,
                    install_node=install_node,
                    log=self._append_log,
                    percent=self._set_status,
                    paths=paths,
                )
                self.after(0, self._refresh_environment)
                self.after(
                    0,
                    lambda: messagebox.showinfo(
                        t("msg.install.done_title"),
                        t("msg.install.done", title=title),
                    ),
                )
            except Exception as exc:
                self._append_log(t("log.install.fail", err=exc))
                self.after(
                    0,
                    lambda: messagebox.showerror(
                        t("msg.install.fail_title"),
                        t("msg.install.fail", err=exc),
                    ),
                )
            finally:

                def _finish_install() -> None:
                    self._flush_ui_queues()
                    self._set_install_busy(False)
                    self.status_var.set(t("status.ready"))

                self.after(0, _finish_install)

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def _install_component(self, name: str) -> None:
        paths = self._current_install_paths()
        save_install_paths(paths)
        self._install_paths = paths

        if name == "yt-dlp":
            if ytdlp_is_ready(paths):
                self._refresh_environment()
                messagebox.showinfo(
                    "yt-dlp",
                    t("msg.ytdlp.ok", ver=self._env.yt_dlp_version),
                )
                return
            self._run_install_worker(
                install_ytdlp=True,
                install_ffmpeg_tool=False,
                install_node=False,
                title=t("msg.install.ytdlp", url=SOURCE_URLS["yt-dlp"]),
            )
        elif name == "ffmpeg":
            if find_ffmpeg_exe(paths):
                self._refresh_environment()
                messagebox.showinfo(
                    "ffmpeg",
                    t("msg.ffmpeg.ok", hint=self._env.ffmpeg_source),
                )
                return
            self._run_install_worker(
                install_ytdlp=False,
                install_ffmpeg_tool=True,
                install_node=False,
                title=t("msg.install.ffmpeg", url=SOURCE_URLS["ffmpeg"]),
            )
        elif name == "node.js":
            if find_node_exe(paths):
                self._refresh_environment()
                messagebox.showinfo("Node.js", t("msg.node.ok"))
                return
            self._run_install_worker(
                install_ytdlp=False,
                install_ffmpeg_tool=False,
                install_node=True,
                title=t("msg.install.node", url=SOURCE_URLS["node.js"]),
            )

    def _install_all(self) -> None:
        paths = self._current_install_paths()
        missing = missing_components(paths)
        optional = optional_components(paths)
        if not missing and not optional:
            messagebox.showinfo(t("msg.deps.ok_title"), t("msg.deps.ok"))
            return
        self._run_install_worker(
            install_ytdlp="yt-dlp" in missing,
            install_ffmpeg_tool="ffmpeg" in missing,
            install_node=bool(optional),
            title=t("msg.install.all"),
        )

    def _install_dependencies(self, include_node: bool = True) -> None:
        self._install_all()

    def start_local_clip_jobs(self, jobs: list[ClipLocalJob]) -> None:
        if self._worker and self._worker.is_alive():
            return

        if not self._env.ffmpeg_available:
            if messagebox.askyesno(
                t("msg.missing.title"),
                t("msg.missing.body", items="ffmpeg"),
            ):
                self._install_all()
            return

        output_dir = self._clip_dir_from_ui()
        if output_dir is None:
            messagebox.showwarning(
                t("msg.dir.bad_title"),
                t("clip.dir.need_pick"),
                parent=self,
            )
            return
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                t("msg.dir.bad_title"),
                t("msg.dir.mkdir_fail", path=output_dir, err=exc),
                parent=self,
            )
            return

        self._save_config()
        self._cancel_flag = False
        self._set_busy(True)
        self._set_progress_value(0)
        self.status_var.set(t("status.clipping"))
        self._select_log_tab()
        self._clear_log()
        self._append_log(t("log.clip.local.start"))
        self._append_log(t("log.clip.dir", dir=output_dir))
        self._flush_ui_queues()

        def worker() -> None:
            runner = ClipRunner(
                log=self._append_log,
                status=self._set_status,
                cancel_check=lambda: self._cancel_flag,
            )
            try:
                ok, failed = runner.run_local(jobs, output_dir)
                summary = t("log.clip.done", summary=f"{ok} ok, {failed} failed")
                self._append_log(summary)
                self.after(
                    0,
                    lambda s=summary, folder=output_dir: messagebox.showinfo(
                        t("msg.clip.done_title"),
                        f"{s}\n{folder}",
                    ),
                )
            except DownloadCancelled:
                self._append_log(t("log.clip.cancelled"))
                self.after(0, lambda: self.status_var.set(t("status.cancelled")))
            except Exception as exc:
                self._append_log(t("log.install.fail", err=str(exc)))
                self.after(
                    0,
                    lambda: messagebox.showerror(
                        t("update.fail_title"),
                        str(exc),
                        parent=self,
                    ),
                )
            finally:

                def _finish_clip() -> None:
                    self._flush_ui_queues()
                    self._set_busy(False)

                self.after(0, _finish_clip)

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def start_clip_jobs(self, jobs: list[ClipUrlJob]) -> None:
        if self._worker and self._worker.is_alive():
            return

        if missing_components(self._current_install_paths()):
            if messagebox.askyesno(t("msg.missing.title"), t("msg.missing.dl")):
                self._install_all()
            return

        if not self._env.ffmpeg_available:
            messagebox.showwarning(
                t("msg.missing.title"),
                t("msg.missing.body", items="ffmpeg"),
                parent=self,
            )
            return

        output_dir = self._clip_dir_from_ui()
        if output_dir is None:
            messagebox.showwarning(
                t("msg.dir.bad_title"),
                t("clip.dir.need_pick"),
                parent=self,
            )
            return
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                t("msg.dir.bad_title"),
                t("msg.dir.mkdir_fail", path=output_dir, err=exc),
                parent=self,
            )
            return

        cache_dir = self._clip_cache_dir_from_ui()
        if cache_dir is None:
            messagebox.showwarning(
                t("msg.dir.bad_title"),
                t("clip.cache.need_pick"),
                parent=self,
            )
            return
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                t("msg.dir.bad_title"),
                t("msg.dir.mkdir_fail", path=cache_dir, err=exc),
                parent=self,
            )
            return

        cookie_path = (
            Path(self.cookie_var.get().strip()) if self.cookie_var.get().strip() else None
        )
        if cookie_path and not cookie_path.is_file():
            messagebox.showwarning("Cookies", t("msg.cookie.missing"))
            return

        self._save_config()
        self._cancel_flag = False
        self._set_busy(True)
        self._set_progress_value(0)
        self.status_var.set(t("status.clipping"))
        self._select_log_tab()
        self._clear_log()
        self._append_log(t("log.clip.start"))
        self._append_log(t("log.clip.dir", dir=output_dir))
        self._append_log(t("log.clip.cache", dir=cache_dir))
        self._flush_ui_queues()

        def worker() -> None:
            runner = ClipRunner(
                log=self._append_log,
                status=self._set_status,
                cancel_check=lambda: self._cancel_flag,
            )
            try:
                ok, failed = runner.run(
                    jobs,
                    output_dir,
                    self._format_key(),
                    cache_dir=cache_dir,
                    cookie_file=cookie_path,
                )
                summary = t("log.clip.done", summary=f"{ok} ok, {failed} failed")
                self._append_log(summary)
                self.after(
                    0,
                    lambda s=summary, folder=output_dir: messagebox.showinfo(
                        t("msg.clip.done_title"),
                        f"{s}\n{folder}",
                    ),
                )
            except DownloadCancelled:
                self._append_log(t("log.clip.cancelled"))
                self.after(0, lambda: self.status_var.set(t("status.cancelled")))
            except Exception as exc:
                self._append_log(t("log.install.fail", err=str(exc)))
                self.after(
                    0,
                    lambda: messagebox.showerror(
                        t("update.fail_title"),
                        str(exc),
                        parent=self,
                    ),
                )
            finally:

                def _finish_clip() -> None:
                    self._flush_ui_queues()
                    self._set_busy(False)

                self.after(0, _finish_clip)

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def _start_download(self) -> None:
        if self._worker and self._worker.is_alive():
            return

        urls = extract_urls(self.url_text.get("1.0", "end"))
        if not urls:
            messagebox.showwarning(t("msg.url.title"), t("msg.url.empty"))
            return

        if missing_components(self._current_install_paths()):
            if messagebox.askyesno(t("msg.missing.title"), t("msg.missing.dl")):
                self._install_all()
            return

        if not self._env.js_runtime_ready and messagebox.askyesno(
            t("msg.node.title"),
            t("msg.node.download", url=SOURCE_URLS["node.js"]),
        ):
            self._install_component("node.js")
            return

        output_dir = self._download_dir_from_ui()
        if output_dir is None:
            messagebox.showwarning(
                t("msg.dir.bad_title"),
                t("msg.dir.need_pick"),
                parent=self,
            )
            return
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                t("msg.dir.bad_title"),
                t("msg.dir.mkdir_fail", path=output_dir, err=exc),
                parent=self,
            )
            return

        cookie_path = (
            Path(self.cookie_var.get().strip()) if self.cookie_var.get().strip() else None
        )
        if cookie_path and not cookie_path.is_file():
            messagebox.showwarning("Cookies", t("msg.cookie.missing"))
            return

        self._save_config()
        self._cancel_flag = False
        self._set_busy(True)
        self._set_progress_value(0)
        self.status_var.set(t("status.preparing"))
        self._select_log_tab()
        self._append_log(t("log.dl.start"))
        self._append_log(t("log.dl.format", fmt=format_label(self._format_key())))
        self._append_log(t("log.dl.dir", dir=output_dir))
        self._flush_ui_queues()

        def worker() -> None:
            downloader = Downloader(
                log=self._append_log,
                status=self._set_status,
                cancel_check=lambda: self._cancel_flag,
            )
            try:
                success, failed, skipped = downloader.download_urls(
                    urls,
                    output_dir,
                    self._format_key(),
                    subtitles=self.subtitles_var.get(),
                    cookie_file=cookie_path,
                )
                parts = [t("log.dl.summary_ok", n=success)]
                if failed:
                    parts.append(t("log.dl.summary_fail", n=failed))
                if skipped:
                    parts.append(t("log.dl.summary_skip", n=skipped))
                summary = t("sep.list").join(parts)
                self._append_log(t("log.dl.done", summary=summary))
                self.after(
                    0,
                    lambda: messagebox.showinfo(
                        t("msg.done.title"),
                        summary + f"\n{output_dir}",
                    ),
                )
            except DownloadCancelled:
                self._append_log(t("log.dl.cancelled"))
                self.after(0, lambda: self.status_var.set(t("status.cancelled")))
            except Exception as exc:
                self._append_log(t("log.dl.error", err=exc))
                self.after(0, lambda: messagebox.showerror(t("msg.fail.title"), str(exc)))
            finally:

                def _finish_download() -> None:
                    self._flush_ui_queues()
                    self._set_busy(False)

                self.after(0, _finish_download)

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def _cancel_download(self) -> None:
        self._cancel_flag = True
        self.status_var.set(t("status.cancelling"))
        self._append_log(t("log.cancel"))

    def _on_close(self) -> None:
        self._save_config()
        if self._worker and self._worker.is_alive():
            if messagebox.askyesno(t("msg.quit.title"), t("msg.quit.body")):
                self._cancel_flag = True
                self.destroy()
            return
        self.destroy()


def main() -> None:
    app = YouTubeDownloaderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
