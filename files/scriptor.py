# scriptor.py
# Scriptor — single-file Qt editor (polished UI) with window icon support.
# Requirements: pip install pyside6
# Put `scriptor.ico` in the same folder (or bundle it with PyInstaller).

import sys
import os
import zipfile
import tempfile
import shutil
import importlib.util
import traceback
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QFileDialog, QTabWidget,
    QPlainTextEdit, QStatusBar, QWidget, QHBoxLayout, QVBoxLayout,
    QToolButton, QMessageBox, QLabel, QSizePolicy, QFrame, QInputDialog
)
from PySide6.QtGui import (
    QColor, QFont, QTextFormat, QSyntaxHighlighter, QKeySequence,
    QPainter, QAction, QIcon
)
from PySide6.QtCore import Qt, QRect, QSize

# --------------------
# Helper: resource_path (works with PyInstaller)
# --------------------
def resource_path(relative_path: str) -> str:
    """
    Return absolute path to resource, works for dev and for PyInstaller bundles.
    Place your scriptor.ico next to this script, or include it via --add-data.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative_path)

# --------------------
# (Short) Plugin manager scaffold (keeps plugin folder)
# --------------------
PLUGINS_DIR = Path("plugins")

class Plugin:
    def __init__(self, name, path):
        self.name = name
        self.path = Path(path)
        self.module = None
        self.hooks = {}

class PluginAPI:
    def __init__(self, plugin_obj, manager):
        self._plugin = plugin_obj
        self._manager = manager
    def register_hook(self, name, func):
        if name not in self._plugin.hooks:
            self._plugin.hooks[name] = []
        self._plugin.hooks[name].append(func)
    @property
    def app_name(self):
        return "Scriptor"

class PluginManager:
    def __init__(self, app):
        self.app = app
        PLUGINS_DIR.mkdir(exist_ok=True)
        self.plugins = {}
        # we don't auto-load here to keep things simple; call load_all_plugins() as needed
        self.load_all_plugins()

    def load_all_plugins(self):
        self.plugins.clear()
        for p in PLUGINS_DIR.iterdir():
            if p.is_dir():
                try:
                    self.load_plugin_from_dir(p)
                except Exception as e:
                    print("Failed loading plugin", p, e)

    def load_plugin_from_dir(self, path: Path):
        name = path.name
        plugin = Plugin(name, path)
        main = path / "plugin-main.py"
        if not main.exists():
            raise FileNotFoundError(f"plugin-main.py missing in {path}")
        spec = importlib.util.spec_from_file_location(f"scriptor_plugin_{name}", str(main))
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            raise RuntimeError(f"Error executing plugin-main.py: {e}\n{traceback.format_exc()}")
        if not hasattr(module, "register"):
            raise RuntimeError("plugin-main.py must provide register(api) function")
        api = PluginAPI(plugin, self)
        try:
            module.register(api)
        except Exception as e:
            raise RuntimeError(f"Plugin register() failed: {e}\n{traceback.format_exc()}")
        plugin.module = module
        self.plugins[name] = plugin
        print("Loaded plugin:", name)

    def install_plugin(self, scpl_path: str):
        scpl = Path(scpl_path)
        if not scpl.exists():
            raise FileNotFoundError(scpl_path)
        with zipfile.ZipFile(scpl, "r") as z:
            if "plugin-main.py" not in z.namelist():
                raise RuntimeError(".scpl must contain plugin-main.py at root")
            base = scpl.stem
            dest = PLUGINS_DIR / base
            idx = 1
            while dest.exists():
                dest = PLUGINS_DIR / f"{base}_{idx}"
                idx += 1
            z.extractall(dest)
        self.load_plugin_from_dir(dest)
        return dest

    def uninstall_plugin(self, name: str):
        if name not in self.plugins:
            raise KeyError(name)
        path = self.plugins[name].path
        self.plugins.pop(name, None)
        shutil.rmtree(path)
        return True

    def call_hook(self, hook_name, *args, **kwargs):
        for plugin in list(self.plugins.values()):
            funcs = plugin.hooks.get(hook_name)
            if not funcs:
                continue
            for f in funcs:
                try:
                    f(*args, **kwargs)
                except Exception:
                    print(f"Plugin {plugin.name} hook {hook_name} error:\n", traceback.format_exc())

# --------------------
# Hooks adapter
# --------------------
class Hooks:
    def __init__(self, pm: PluginManager):
        self.pm = pm
    def on_open(self, path): self.pm.call_hook("on_open", path)
    def on_save(self, path): self.pm.call_hook("on_save", path)
    def on_event(self, name, **kwargs): self.pm.call_hook("on_event", name=name, **kwargs)

# --------------------
# LineNumberArea and CodeEditor (with minimal highlighting)
# --------------------
class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.setAttribute(Qt.WA_OpaquePaintEvent)
    def sizeHint(self): return QSize(self.editor.line_number_width(), 0)
    def paintEvent(self, event): self.editor.paint_line_numbers(event)

class MultiLangHighlighter(QSyntaxHighlighter):
    LANG_RULES = {
        "python": [
            (r"\b(def|class|return|if|else|elif|for|while|import|from|as|pass|break|continue|with|try|except|finally|lambda|yield|async|await|assert|raise|global|nonlocal|del)\b", "#569cd6"),
            (r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'', "#ce9178"),
            (r"\".*?\"|'.*?'", "#ce9178"),
            (r"#.*", "#6a9955"),
            (r"\b(True|False|None)\b", "#569cd6"),
        ],
        "javascript": [
            (r"\b(function|return|var|let|const|if|else|for|while|break|continue|import|from|export|class|new|this|throw|try|catch)\b", "#569cd6"),
            (r"//.*", "#6a9955"),
            (r"\".*?\"|'.*?'", "#ce9178"),
        ],
        "html": [
            (r"<!--[\s\S]*?-->", "#6a9955"),
            (r"(<[^>]+>)", "#569cd6"),
            (r"\".*?\"|'.*?'", "#ce9178"),
        ],
    }
    EXT_MAP = { ".py": "python", ".js": "javascript", ".mjs": "javascript", ".html": "html", ".htm": "html" }

    def __init__(self, doc, language="python"):
        super().__init__(doc)
        self.language = language
        self.rules = []
        for pattern, color in self.LANG_RULES.get(language, []):
            fmt = QTextFormat()
            fmt.setForeground(QColor(color))
            self.rules.append((pattern, fmt))

    @classmethod
    def language_for_filename(cls, fname):
        ext = Path(fname).suffix.lower() if fname else ""
        return cls.EXT_MAP.get(ext, "python")

    def highlightBlock(self, text):
        import re
        for pattern, fmt in self.rules:
            for m in re.finditer(pattern, text):
                self.setFormat(m.start(), m.end()-m.start(), fmt)

class CodeEditor(QPlainTextEdit):
    def __init__(self, filename=None):
        super().__init__()
        self.file_path = Path(filename) if filename else None
        self.setFont(QFont("Consolas", 12))
        self.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(' '))
        self.cursorPositionChanged.connect(self._cursor_changed)
        self.blockCountChanged.connect(self.update_margins)
        self.updateRequest.connect(self.update_line_numbers)
        self.textChanged.connect(self._on_text_changed)
        self.line_area = LineNumberArea(self)
        self.update_margins(0)
        self.setViewportMargins(self.line_number_width(), 0, 6, 0)
        lang = MultiLangHighlighter.language_for_filename(str(self.file_path) if self.file_path else "")
        self.highlighter = MultiLangHighlighter(self.document(), language=lang)

    def set_language(self, lang):
        self.highlighter = MultiLangHighlighter(self.document(), language=lang)
        self.rehighlight()

    def rehighlight(self):
        self.highlighter.rehighlight()

    def line_number_width(self):
        digits = len(str(max(1, self.blockCount())))
        return 14 + self.fontMetrics().horizontalAdvance('9') * digits

    def update_margins(self, _=0):
        self.setViewportMargins(self.line_number_width(), 0, 6, 0)

    def update_line_numbers(self, rect, dy):
        if dy:
            self.line_area.scroll(0, dy)
        else:
            self.line_area.update(0, rect.y(), self.line_area.width(), rect.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.line_area.setGeometry(QRect(cr.left(), cr.top(), self.line_number_width(), cr.height()))

    def paint_line_numbers(self, event):
        painter = QPainter(self.line_area)
        try:
            bg = QColor("#1f1f1f") if self.window()._dark else QColor("#f0f0f0")
            painter.fillRect(event.rect(), bg)
            block = self.firstVisibleBlock()
            top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
            bottom = top + int(self.blockBoundingRect(block).height())
            gutter_right = self.line_area.width() - 1
            painter.setPen(QColor("#3c3c3c") if self.window()._dark else QColor("#d0d0d0"))
            painter.drawLine(gutter_right, event.rect().top(), gutter_right, event.rect().bottom())
            num_color = QColor("#9ea7b1") if self.window()._dark else QColor("#444444")
            painter.setPen(num_color)
            while block.isValid() and top <= event.rect().bottom():
                if block.isVisible() and bottom >= event.rect().top():
                    number = str(block.blockNumber() + 1)
                    painter.drawText(0, top, self.line_area.width() - 8, self.fontMetrics().height(), Qt.AlignRight | Qt.AlignVCenter, number)
                block = block.next()
                top = bottom
                bottom = top + int(self.blockBoundingRect(block).height())
        finally:
            painter.end()

    def _on_text_changed(self):
        parent = self.parent()
        if parent and hasattr(parent.window(), "refresh_tab_title_for_editor"):
            parent.window().refresh_tab_title_for_editor(self)

    def is_modified_since_save(self):
        return self.document().isModified()

    def set_saved_state(self):
        self.document().setModified(False)

    def _cursor_changed(self):
        parent = self.parent()
        if parent and hasattr(parent.window(), "update_status"):
            parent.window().update_status()

# --------------------
# Ribbon / UI
# --------------------
class Ribbon(QWidget):
    def __init__(self, parent_window):
        super().__init__(parent_window)
        self.parent_window = parent_window
        self._build_ui()
        self.setObjectName("ribbon")

    def _tool_button(self, text, handler, tooltip=None):
        btn = QToolButton(self)
        btn.setText(text)
        btn.setAutoRaise(True)
        btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        btn.setFixedSize(110, 44)
        btn.clicked.connect(handler)
        btn.setToolTip(tooltip or text)
        return btn

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(10)

        new_btn = self._tool_button("New", self.parent_window.on_new, "New (Ctrl+N)")
        open_btn = self._tool_button("Open...", self.parent_window.open_file, "Open (Ctrl+O)")
        save_btn = self._tool_button("Save", self.parent_window.save_file, "Save (Ctrl+S)")
        saveas_btn = self._tool_button("Save As", self.parent_window.save_file_as, "Save As")

        undo_btn = self._tool_button("Undo", self.parent_window.on_undo, "Undo (Ctrl+Z)")
        redo_btn = self._tool_button("Redo", self.parent_window.on_redo, "Redo (Ctrl+Y)")

        plugin_btn = self._tool_button("Install Plugin", self.parent_window.install_plugin, "Install plugin (.scpl)")
        reload_btn = self._tool_button("Reload Plugins", self.parent_window.reload_plugins, "Reload installed plugins")
        theme_btn = self._tool_button("Toggle Theme", self.parent_window.toggle_theme, "Toggle theme (Ctrl+T)")

        for w in (new_btn, open_btn, save_btn, saveas_btn):
            layout.addWidget(w)

        sep = QFrame(self)
        sep.setFrameShape(QFrame.VLine)
        sep.setFixedHeight(28)
        layout.addWidget(sep)

        for w in (undo_btn, redo_btn):
            layout.addWidget(w)

        sep2 = QFrame(self)
        sep2.setFrameShape(QFrame.VLine)
        sep2.setFixedHeight(28)
        layout.addWidget(sep2)

        for w in (plugin_btn, reload_btn, theme_btn):
            layout.addWidget(w)

        layout.addStretch(1)
        self._controls = {
            "buttons": [new_btn, open_btn, save_btn, saveas_btn, undo_btn, redo_btn, plugin_btn, reload_btn, theme_btn],
            "seps": [sep, sep2]
        }

    def set_theme(self, dark: bool):
        if dark:
            self.setStyleSheet("QWidget#ribbon { background: #2b2b2d; border-bottom: 1px solid #3c3c3c; } QToolButton { color: #e6eef6; } QToolButton:hover { background: #333539; }")
            sep_color = "#3c3c3c"; btn_text = "#e6eef6"
        else:
            self.setStyleSheet("QWidget#ribbon { background: #f4f4f4; border-bottom: 1px solid #d0d0d0; } QToolButton { color: #222222; } QToolButton:hover { background: #e8e8e8; }")
            sep_color = "#d0d0d0"; btn_text = "#222222"
        for s in self._controls["seps"]:
            s.setStyleSheet(f"color: {sep_color};")
        for b in self._controls["buttons"]:
            b.setStyleSheet(f"color: {btn_text}; background: transparent;")

# --------------------
# Main Window
# --------------------
class Scriptor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Scriptor")
        self.resize(1000, 700)
        self._dark = True

        # Attempt to set window icon (if available)
        ico = resource_path("scriptor.ico")
        if os.path.exists(ico):
            self.setWindowIcon(QIcon(ico))

        # plugin manager + hooks
        self.plugin_manager = PluginManager(self)
        self.hooks = Hooks(self.plugin_manager)

        # layout
        central = QWidget()
        vbox = QVBoxLayout(central)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        self.ribbon = Ribbon(self)
        vbox.addWidget(self.ribbon)

        hr = QFrame(self)
        hr.setFrameShape(QFrame.HLine)
        hr.setFixedHeight(1)
        vbox.addWidget(hr)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        vbox.addWidget(self.tabs)

        self.setCentralWidget(central)

        # status bar
        self.status = QStatusBar()
        self.status_left = QLabel("")
        self.status_right = QLabel("")
        self.status_right.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.status.addWidget(self.status_left, 1)
        self.status.addPermanentWidget(self.status_right, 0)
        self.setStatusBar(self.status)

        self._create_actions_and_menus()
        self._apply_dark_theme()
        self.new_tab()

    def _create_actions_and_menus(self):
        new_act = QAction("New", self); new_act.setShortcut(QKeySequence("Ctrl+N")); new_act.triggered.connect(self.on_new)
        open_act = QAction("Open...", self); open_act.setShortcut(QKeySequence("Ctrl+O")); open_act.triggered.connect(self.open_file)
        save_act = QAction("Save", self); save_act.setShortcut(QKeySequence("Ctrl+S")); save_act.triggered.connect(self.save_file)
        save_as_act = QAction("Save As...", self); save_as_act.setShortcut(QKeySequence("Ctrl+Shift+S")); save_as_act.triggered.connect(self.save_file_as)
        close_act = QAction("Close Tab", self); close_act.setShortcut(QKeySequence("Ctrl+W")); close_act.triggered.connect(self.on_close_current_tab)
        exit_act = QAction("Exit", self); exit_act.setShortcut(QKeySequence("Ctrl+Q")); exit_act.triggered.connect(self.close)
        undo_act = QAction("Undo", self); undo_act.setShortcut(QKeySequence("Ctrl+Z")); undo_act.triggered.connect(self.on_undo)
        redo_act = QAction("Redo", self); redo_act.setShortcut(QKeySequence("Ctrl+Y")); redo_act.triggered.connect(self.on_redo)
        theme_act = QAction("Toggle Theme", self); theme_act.setShortcut(QKeySequence("Ctrl+T")); theme_act.triggered.connect(self.toggle_theme)
        plugin_install_act = QAction("Install Plugin...", self); plugin_install_act.triggered.connect(self.install_plugin)
        plugin_reload_act = QAction("Reload Plugins", self); plugin_reload_act.triggered.connect(self.reload_plugins)
        plugin_uninstall_act = QAction("Uninstall Plugin...", self); plugin_uninstall_act.triggered.connect(self.uninstall_plugin)

        menubar = self.menuBar(); menubar.setNativeMenuBar(False)
        file_menu = menubar.addMenu("File"); file_menu.addAction(new_act); file_menu.addAction(open_act); file_menu.addAction(save_act); file_menu.addAction(save_as_act); file_menu.addSeparator(); file_menu.addAction(close_act); file_menu.addAction(exit_act)
        edit_menu = menubar.addMenu("Edit"); edit_menu.addAction(undo_act); edit_menu.addAction(redo_act)
        view_menu = menubar.addMenu("View"); view_menu.addAction(theme_act)
        plugin_menu = menubar.addMenu("Plugins"); plugin_menu.addAction(plugin_install_act); plugin_menu.addAction(plugin_reload_act); plugin_menu.addAction(plugin_uninstall_act)

        self._actions = {"new":new_act,"open":open_act,"save":save_act,"save_as":save_as_act,"close":close_act,"exit":exit_act,"undo":undo_act,"redo":redo_act,"theme":theme_act}

    def new_tab(self, path=None, content=""):
        editor = CodeEditor(path)
        editor.textChanged.connect(self.update_status)
        if path and content:
            editor.setPlainText(content)
            editor.set_saved_state()
        title = editor.file_path.name if editor.file_path else "Untitled"
        idx = self.tabs.addTab(editor, title)
        self.tabs.setCurrentIndex(idx)
        if editor.file_path:
            self.tabs.setTabToolTip(idx, str(editor.file_path))
        self.tabs.tabBar().show()

    def current_editor(self):
        w = self.tabs.currentWidget(); return w if isinstance(w, CodeEditor) else None

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open File", "", "All Files (*);;Python Files (*.py);;JS Files (*.js);;HTML Files (*.html *.htm)")
        if not path: return
        try:
            content = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            QMessageBox.warning(self, "Open failed", f"Could not open file:\n{e}"); return
        self.new_tab(path, content); self.hooks.on_open(path)

    def save_file(self):
        editor = self.current_editor(); 
        if not editor: return
        if not editor.file_path: return self.save_file_as()
        try:
            editor.file_path.write_text(editor.toPlainText(), encoding="utf-8")
            editor.set_saved_state(); self.refresh_tab_title_for_editor(editor); self.hooks.on_save(str(editor.file_path))
        except Exception as e:
            QMessageBox.warning(self, "Save failed", f"Could not save file:\n{e}")

    def save_file_as(self):
        editor = self.current_editor(); 
        if not editor: return
        path, _ = QFileDialog.getSaveFileName(self, "Save File As", "", "All Files (*);;Python Files (*.py)")
        if not path: return
        try:
            Path(path).write_text(editor.toPlainText(), encoding="utf-8"); editor.file_path = Path(path); editor.set_saved_state()
            idx = self.tabs.currentIndex(); self.tabs.setTabText(idx, editor.file_path.name); self.tabs.setTabToolTip(idx, str(editor.file_path)); self.hooks.on_save(str(editor.file_path))
        except Exception as e:
            QMessageBox.warning(self, "Save As failed", f"Could not save file:\n{e}")

    def install_plugin(self):
        path, _ = QFileDialog.getOpenFileName(self, "Install Plugin (.scpl zip)", "", "Plugin packages (*.scpl *.zip)")
        if not path: return
        try:
            dest = self.plugin_manager.install_plugin(path)
        except Exception as e:
            QMessageBox.warning(self, "Install failed", f"{e}"); return
        QMessageBox.information(self, "Plugin installed", f"Installed to: {dest}")

    def reload_plugins(self):
        try:
            self.plugin_manager.load_all_plugins()
            QMessageBox.information(self, "Plugins reloaded", f"Loaded plugins: {', '.join(self.plugin_manager.plugins.keys()) or 'none'}")
        except Exception as e:
            QMessageBox.warning(self, "Reload failed", f"{e}")

    def uninstall_plugin(self):
        names = list(self.plugin_manager.plugins.keys())
        if not names:
            QMessageBox.information(self, "No plugins", "No installed plugins to remove."); return
        name, ok = QInputDialog.getItem(self, "Uninstall Plugin", "Select plugin to uninstall:", names, 0, False)
        if not ok or not name: return
        try:
            self.plugin_manager.uninstall_plugin(name); QMessageBox.information(self, "Uninstalled", f"{name} removed.")
        except Exception as e:
            QMessageBox.warning(self, "Uninstall failed", f"{e}")

    def close_tab(self, index):
        editor = self.tabs.widget(index)
        if isinstance(editor, CodeEditor) and editor.is_modified_since_save():
            resp = QMessageBox.question(self, "Unsaved changes", "This tab has unsaved changes. Save before closing?", QMessageBox.StandardButtons(QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel))
            if resp == QMessageBox.Cancel: return
            if resp == QMessageBox.Yes:
                self.tabs.setCurrentIndex(index); self.save_file()
                if editor.is_modified_since_save(): return
        self.tabs.removeTab(index)
        if self.tabs.count() == 0: self.new_tab()

    def on_close_current_tab(self):
        idx = self.tabs.currentIndex()
        if idx >= 0: self.close_tab(idx)

    def on_undo(self):
        e = self.current_editor(); 
        if e: e.undo()

    def on_redo(self):
        e = self.current_editor(); 
        if e: e.redo()

    def on_new(self):
        self.new_tab()

    def update_status(self):
        editor = self.current_editor()
        if not editor:
            self.status_left.setText(""); self.status_right.setText(""); return
        cursor = editor.textCursor()
        modified = "*" if editor.is_modified_since_save() else ""
        path = editor.file_path.name if editor.file_path else "Untitled"
        chars = len(editor.toPlainText()); lines = editor.blockCount()
        self.status_left.setText(f"{modified}{path}"); self.status_right.setText(f"Line {cursor.blockNumber()+1}, Col {cursor.columnNumber()+1} — {lines}L • {chars}ch")

    def refresh_tab_title_for_editor(self, editor):
        for i in range(self.tabs.count()):
            if self.tabs.widget(i) is editor:
                title = editor.file_path.name if editor.file_path else "Untitled"
                if editor.is_modified_since_save(): title = "*" + title
                self.tabs.setTabText(i, title); break

    def refresh_tab_title(self):
        for i in range(self.tabs.count()):
            editor = self.tabs.widget(i)
            if isinstance(editor, CodeEditor):
                title = editor.file_path.name if editor.file_path else "Untitled"
                if editor.is_modified_since_save(): title = "*" + title
                self.tabs.setTabText(i, title)

    def toggle_theme(self):
        self._dark = not self._dark
        if self._dark: self._apply_dark_theme()
        else: self._apply_light_theme()

    def _apply_dark_theme(self):
        self.setStyleSheet("""QMainWindow{background:#1e1e1e;color:#d4d4d4;} QPlainTextEdit{background:#1b1b1b;color:#d4d4d4; selection-background-color:#264f78; padding:6px;} QTabBar::tab{background:#2d2d2d;padding:8px 12px;margin-right:2px;color:#d4d4d4;} QTabBar::tab:selected{background:#1f1f1f;border-bottom:2px solid #007acc;} QStatusBar{background:#007acc;color:white;} QMenuBar{background:#2d2d2d;color:#d4d4d4;}""")
        self.ribbon.set_theme(dark=True)
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, CodeEditor): w.viewport().update()

    def _apply_light_theme(self):
        self.setStyleSheet("""QMainWindow{background:#f0f0f0;color:#2b2b2b;} QPlainTextEdit{background:#ffffff;color:#2b2b2b; selection-background-color:#cce0ff; padding:6px;} QTabBar::tab{background:#e8e8e8;padding:8px 12px;margin-right:2px;color:#2b2b2b;} QTabBar::tab:selected{background:#ffffff;border-bottom:2px solid #007acc;} QStatusBar{background:#e0e0e0;color:#2b2b2b;} QMenuBar{background:#e8e8e8;color:#2b2b2b;}""")
        self.ribbon.set_theme(dark=False)
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, CodeEditor): w.viewport().update()

    def closeEvent(self, event):
        for i in range(self.tabs.count()-1, -1, -1):
            editor = self.tabs.widget(i)
            if isinstance(editor, CodeEditor) and editor.is_modified_since_save():
                self.tabs.setCurrentIndex(i)
                resp = QMessageBox.question(self, "Unsaved changes", f"Tab '{self.tabs.tabText(i)}' has unsaved changes. Save before exit?", QMessageBox.StandardButtons(QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel))
                if resp == QMessageBox.Cancel:
                    event.ignore(); return
                if resp == QMessageBox.Yes:
                    self.save_file()
                    if editor.is_modified_since_save():
                        event.ignore(); return
        event.accept()

# --------------------
# Entrypoint
# --------------------
if __name__ == "__main__":
    app = QApplication(sys.argv)

    # apply window icon (works in dev and when bundled with PyInstaller)
    icon_path = resource_path("scriptor.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    win = Scriptor()
    # set again on the window (redundant but ensures window-level icon)
    if os.path.exists(icon_path):
        win.setWindowIcon(QIcon(icon_path))

    win.show()
    sys.exit(app.exec())
