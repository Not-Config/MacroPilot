from __future__ import annotations

import re
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Iterable

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPoint,
    QRect,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QColor, QCloseEvent, QDesktopServices, QFont, QPainter, QPen, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTableView,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app_settings import (
    FUNCTION_HOTKEYS,
    HotkeySettings,
    load_hotkey_settings,
    normalize_hotkey_name,
    save_hotkey_settings,
)
from graph_model import GraphError, graph_to_script_with_line_map
from macro_core import (
    APP_NAME,
    APP_VERSION,
    EXAMPLE_SCRIPT,
    IfBlock,
    MAX_RECORDED_EVENTS,
    MacroFormatError,
    RepeatBlock,
    ScriptError,
    ScriptNode,
    describe_event,
    events_to_script,
    load_macro,
    macro_duration,
    parse_script,
    save_macro,
)
from main import (
    DEFAULT_BLOCK_PHYSICAL_MOUSE,
    DEFAULT_MINIMIZE_ACTION_WINDOW,
    DEFAULT_RECORD_MOUSE_MOVES,
    DEFAULT_RECORDING_PRECISION,
    MAX_TABLE_ROWS,
    PYNPUT_IMPORT_ERROR,
    RECORDING_PRECISION_OPTIONS,
    AutomationRunner,
    EventRecorder,
    WINDOWS_NATIVE_AVAILABLE,
    keyboard,
    resolve_script_key,
)
from project_config import AUTHOR_NAME, PROJECT_URL, SUPPORT_URL
from qt_graph import GRAPH_STYLE_SHEET, GraphEditor
from update_service import (
    ReleaseAsset,
    ReleaseInfo,
    UpdateError,
    choose_release_asset,
    download_release_asset,
    fetch_latest_release,
    inspect_update_archive,
    is_newer_version,
    launch_update_installer,
    temporary_update_path,
)
from project_config import PROJECT_REPOSITORY


SCRIPT_FILE_LIMIT = 2 * 1024 * 1024


class UiBridge(QObject):
    record_stop_requested = Signal(str)
    recorder_error = Signal(str)
    recorder_warning = Signal(str)
    runner_progress = Signal(str)
    runner_finished = Signal(bool, object)
    global_action = Signal(str)
    update_check_finished = Signal(bool, object, object)
    update_progress = Signal(str)
    update_download_finished = Signal(object, object, object)


class EventTableModel(QAbstractTableModel):
    HEADERS = ("№", "Время", "Действие", "Подробности")

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.events: list[dict[str, Any]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else min(len(self.events), MAX_TABLE_ROWS)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self.HEADERS):
            return self.HEADERS[section]
        return section + 1

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < self.rowCount():
            return None
        event = self.events[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            title, details = describe_event(event)
            values = (
                str(index.row() + 1),
                f"{float(event['t']):.3f}",
                title,
                details,
            )
            return values[index.column()]
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in {0, 1}:
            return int(Qt.AlignmentFlag.AlignCenter)
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 1:
            return QColor("#9cadc2")
        if role == Qt.ItemDataRole.BackgroundRole and index.row() % 2:
            return QColor("#171d27")
        return None

    def set_events(self, events: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self.events = events
        self.endResetModel()

    def refresh(self) -> None:
        self.beginResetModel()
        self.endResetModel()


class ScreenRegionOverlay(QWidget):
    region_selected = Signal(int, int, int, int)
    cancelled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.origin: QPoint | None = None
        self.current: QPoint | None = None
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.CrossCursor)
        virtual = QRect()
        for screen in QApplication.screens():
            virtual = virtual.united(screen.geometry())
        self.setGeometry(virtual)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def selection_rect(self) -> QRect:
        if self.origin is None or self.current is None:
            return QRect()
        return QRect(self.origin, self.current).normalized()

    def paintEvent(self, event: Any) -> None:
        del event
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(5, 9, 15, 150))
        selection = self.selection_rect()
        if not selection.isNull():
            painter.fillRect(selection, QColor(75, 135, 220, 55))
            painter.setPen(QPen(QColor("#75a7ed"), 2))
            painter.drawRect(selection)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(
                selection.adjusted(8, 8, -8, -8),
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                f"{selection.width()} × {selection.height()}",
            )
        painter.setPen(QColor("#ffffff"))
        painter.drawText(
            self.rect().adjusted(0, 24, 0, 0),
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter,
            "Выделите область экрана мышью · Esc — отмена",
        )

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.origin = event.position().toPoint()
            self.current = self.origin
            self.update()

    def mouseMoveEvent(self, event: Any) -> None:
        if self.origin is not None:
            self.current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self.origin is None:
            return
        self.current = event.position().toPoint()
        selection = self.selection_rect()
        if selection.width() < 2 or selection.height() < 2:
            self.origin = None
            self.current = None
            self.update()
            return
        top_left = self.mapToGlobal(selection.topLeft())
        center = self.mapToGlobal(selection.center())
        screen = QApplication.screenAt(center) or QApplication.primaryScreen()
        ratio = screen.devicePixelRatio() if screen is not None else 1.0
        screen_origin = screen.geometry().topLeft() if screen is not None else QPoint()
        relative = top_left - screen_origin
        x = round(screen_origin.x() + relative.x() * ratio)
        y = round(screen_origin.y() + relative.y() * ratio)
        width = max(1, round(selection.width() * ratio))
        height = max(1, round(selection.height() * ratio))
        self.hide()
        self.region_selected.emit(x, y, width, height)
        self.deleteLater()

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            self.cancelled.emit()
            self.deleteLater()
            return
        super().keyPressEvent(event)


class CodeEditor(QPlainTextEdit):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        font = QFont("Cascadia Mono")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(10)
        self.setFont(font)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setPlaceholderText("# Введите команды MacroPilot или соберите граф…")

    def highlight_error(self, line_no: int) -> None:
        block = self.document().findBlockByLineNumber(max(0, line_no - 1))
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        cursor.select(QTextCursor.SelectionType.LineUnderCursor)
        selection = QTextEdit.ExtraSelection()
        selection.cursor = cursor
        selection.format.setBackground(QColor("#5b2d35"))
        selection.format.setForeground(QColor("#ffe2e5"))
        self.setExtraSelections([selection])
        focus_cursor = QTextCursor(block)
        self.setTextCursor(focus_cursor)
        self.centerCursor()
        self.setFocus()

    def clear_error(self) -> None:
        self.setExtraSelections([])


class MacroPilotQtWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(1360, 860)
        self.setMinimumSize(1024, 680)

        self.bridge = UiBridge(self)
        self.events: list[dict[str, Any]] = []
        self.recorder: EventRecorder | None = None
        self.runner: AutomationRunner | None = None
        self.worker: threading.Thread | None = None
        self.safety_listener: Any = None
        self.hotkeys_held: set[str] = set()
        self.hotkey_settings = load_hotkey_settings()
        self.mode = "idle"
        self.current_macro_path: Path | None = None
        self.current_script_path: Path | None = None
        self.recording_append_mode = False
        self.recording_base_count = 0
        self.recording_base_duration = 0.0
        self.window_was_minimized = False
        self.available_release: ReleaseInfo | None = None
        self.update_busy = False
        self.region_overlay: ScreenRegionOverlay | None = None
        self._countdown_value = 0
        self._countdown_label = ""
        self._countdown_callback: Any = None
        self._active_graph_line_map: dict[int, str] = {}

        self.recording_timer = QTimer(self)
        self.recording_timer.setInterval(350)
        self.recording_timer.timeout.connect(self._refresh_recording_preview)
        self.countdown_timer = QTimer(self)
        self.countdown_timer.setInterval(1000)
        self.countdown_timer.timeout.connect(self._countdown_tick)

        self._build_ui()
        self._connect_bridge()
        self._start_safety_listener()
        self._refresh_event_table()
        self._refresh_controls()
        QTimer.singleShot(1800, lambda: self.check_for_updates(manual=False))

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 10)
        root.setSpacing(10)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("MacroPilot")
        title.setObjectName("AppTitle")
        subtitle = QLabel("Игровые макросы, сценарии и визуальные графы")
        subtitle.setObjectName("MutedText")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch(1)
        self.hotkey_hint = QLabel()
        self.hotkey_hint.setObjectName("Pill")
        header.addWidget(self.hotkey_hint)
        version = QLabel(f"v{APP_VERSION}")
        version.setObjectName("Pill")
        header.addWidget(version)
        root.addLayout(header)

        self.tabs = QTabWidget()
        self.record_tab = self._build_record_tab()
        self.script_tab = self._build_script_tab()
        self.settings_tab = self._build_settings_tab()
        self.about_tab = self._build_about_tab()
        self.tabs.addTab(self.record_tab, "Запись")
        self.tabs.addTab(self.script_tab, "Сценарий")
        self.tabs.addTab(self.settings_tab, "Настройки")
        self.tabs.addTab(self.about_tab, "О проекте")
        root.addWidget(self.tabs, 1)

        footer = QHBoxLayout()
        self.status_label = QLabel("Готово")
        self.status_label.setObjectName("StatusLabel")
        self.status_label.setWordWrap(True)
        footer.addWidget(self.status_label, 1)
        self.mode_label = QLabel("ГОТОВО")
        self.mode_label.setObjectName("ModePill")
        footer.addWidget(self.mode_label)
        root.addLayout(footer)
        self.setCentralWidget(central)
        self._update_hotkey_labels()

    def _build_record_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 12, 8, 8)
        layout.setSpacing(10)

        actions = QHBoxLayout()
        self.record_button = QPushButton()
        self.record_button.setObjectName("RecordButton")
        self.play_button = QPushButton()
        self.play_button.setObjectName("AccentButton")
        self.stop_button = QPushButton()
        self.stop_button.setObjectName("DangerButton")
        self.load_macro_button = QPushButton("Открыть")
        self.save_macro_button = QPushButton("Сохранить")
        self.to_script_button = QPushButton("В сценарий")
        self.delete_events_button = QPushButton("Удалить выбранное")
        self.clear_events_button = QPushButton("Очистить")
        for button in (
            self.record_button,
            self.play_button,
            self.stop_button,
            self.load_macro_button,
            self.save_macro_button,
            self.to_script_button,
            self.delete_events_button,
            self.clear_events_button,
        ):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        options = QFrame()
        options.setObjectName("Card")
        options_layout = QGridLayout(options)
        options_layout.setContentsMargins(12, 10, 12, 10)
        self.record_moves_check = QCheckBox("Записывать движение мыши")
        self.record_moves_check.setChecked(DEFAULT_RECORD_MOUSE_MOVES)
        self.minimize_check = QCheckBox("Сворачивать при записи и запуске")
        self.minimize_check.setChecked(DEFAULT_MINIMIZE_ACTION_WINDOW)
        self.block_mouse_check = QCheckBox("Блокировать физическую мышь при выполнении")
        self.block_mouse_check.setChecked(DEFAULT_BLOCK_PHYSICAL_MOUSE)
        self.block_mouse_check.setEnabled(WINDOWS_NATIVE_AVAILABLE)
        self.precision_combo = QComboBox()
        self.precision_combo.addItems(RECORDING_PRECISION_OPTIONS)
        self.precision_combo.setCurrentText(DEFAULT_RECORDING_PRECISION)
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.1, 10.0)
        self.speed_spin.setSingleStep(0.1)
        self.speed_spin.setValue(1.0)
        self.repeats_spin = QSpinBox()
        self.repeats_spin.setRange(1, 999)
        self.repeats_spin.setValue(1)
        self.infinite_check = QCheckBox("Бесконечно")
        options_layout.addWidget(QLabel("Точность"), 0, 0)
        options_layout.addWidget(self.precision_combo, 0, 1)
        options_layout.addWidget(QLabel("Скорость"), 0, 2)
        options_layout.addWidget(self.speed_spin, 0, 3)
        options_layout.addWidget(QLabel("Повторы"), 0, 4)
        options_layout.addWidget(self.repeats_spin, 0, 5)
        options_layout.addWidget(self.infinite_check, 0, 6)
        options_layout.addWidget(self.record_moves_check, 1, 0, 1, 2)
        options_layout.addWidget(self.minimize_check, 1, 2, 1, 2)
        options_layout.addWidget(self.block_mouse_check, 1, 4, 1, 3)
        layout.addWidget(options)

        self.event_model = EventTableModel(self)
        self.event_table = QTableView()
        self.event_table.setModel(self.event_model)
        self.event_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.event_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.event_table.setAlternatingRowColors(False)
        self.event_table.setSortingEnabled(False)
        self.event_table.verticalHeader().setVisible(False)
        self.event_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.event_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.event_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.event_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.event_table, 1)
        self.record_summary = QLabel()
        self.record_summary.setObjectName("MutedText")
        layout.addWidget(self.record_summary)

        self.record_button.clicked.connect(self.start_recording)
        self.play_button.clicked.connect(self.play_recording_countdown)
        self.stop_button.clicked.connect(self.stop_current)
        self.load_macro_button.clicked.connect(self.load_macro_file)
        self.save_macro_button.clicked.connect(self.save_macro_file)
        self.to_script_button.clicked.connect(self.convert_to_script)
        self.delete_events_button.clicked.connect(self.delete_selected_events)
        self.clear_events_button.clicked.connect(self.clear_events)
        self.infinite_check.toggled.connect(
            lambda checked: self.repeats_spin.setEnabled(not checked and self.mode == "idle")
        )
        return page

    def _build_script_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 12, 8, 8)
        layout.setSpacing(8)

        actions = QHBoxLayout()
        self.script_run_button = QPushButton()
        self.script_run_button.setObjectName("AccentButton")
        self.script_stop_button = QPushButton()
        self.script_stop_button.setObjectName("DangerButton")
        self.script_validate_button = QPushButton("Проверить")
        self.script_open_button = QPushButton("Открыть код")
        self.script_save_button = QPushButton("Сохранить код")
        self.script_example_button = QPushButton("Пример")
        self.script_to_graph_button = QPushButton("Код → граф")
        self.ocr_region_button = QPushButton("Выбрать OCR-область")
        self.script_speed_spin = QDoubleSpinBox()
        self.script_speed_spin.setRange(0.1, 10.0)
        self.script_speed_spin.setSingleStep(0.1)
        self.script_speed_spin.setValue(1.0)
        for button in (
            self.script_run_button,
            self.script_stop_button,
            self.script_validate_button,
            self.script_open_button,
            self.script_save_button,
            self.script_example_button,
            self.script_to_graph_button,
            self.ocr_region_button,
        ):
            actions.addWidget(button)
        actions.addStretch(1)
        actions.addWidget(QLabel("Скорость"))
        actions.addWidget(self.script_speed_spin)
        layout.addLayout(actions)

        self.script_modes = QTabWidget()
        self.code_editor = CodeEditor()
        self.code_editor.setPlainText(EXAMPLE_SCRIPT)
        self.graph_editor = GraphEditor()
        self.script_modes.addTab(self.code_editor, "Код")
        self.script_modes.addTab(self.graph_editor, "Граф")
        layout.addWidget(self.script_modes, 1)

        self.script_run_button.clicked.connect(self.play_script_countdown)
        self.script_stop_button.clicked.connect(self.stop_current)
        self.script_validate_button.clicked.connect(lambda: self.validate_current_script(True))
        self.script_open_button.clicked.connect(self.load_script_file)
        self.script_save_button.clicked.connect(self.save_script_file)
        self.script_example_button.clicked.connect(self.load_example_script)
        self.script_to_graph_button.clicked.connect(self.code_to_graph)
        self.ocr_region_button.clicked.connect(self.pick_ocr_region)
        self.graph_editor.import_script_requested.connect(self.code_to_graph)
        self.graph_editor.script_generated.connect(self.graph_to_code)
        return page

    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 20, 20, 20)
        title = QLabel("Глобальные горячие клавиши")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)
        description = QLabel(
            "Назначенные клавиши работают поверх игр. Во время записи они не попадают в макрос."
        )
        description.setObjectName("MutedText")
        description.setWordWrap(True)
        layout.addWidget(description)

        card = QFrame()
        card.setObjectName("Card")
        form = QFormLayout(card)
        form.setContentsMargins(18, 18, 18, 18)
        self.play_hotkey_combo = self._hotkey_combo(self.hotkey_settings.play)
        self.record_hotkey_combo = self._hotkey_combo(self.hotkey_settings.record)
        self.finish_hotkey_combo = self._hotkey_combo(self.hotkey_settings.finish_recording)
        self.stop_hotkey_combo = self._hotkey_combo(self.hotkey_settings.stop)
        form.addRow("Запустить текущий макрос", self.play_hotkey_combo)
        form.addRow("Начать / продолжить запись", self.record_hotkey_combo)
        form.addRow("Закончить запись", self.finish_hotkey_combo)
        form.addRow("Аварийная остановка", self.stop_hotkey_combo)
        layout.addWidget(card)
        buttons = QHBoxLayout()
        save_button = QPushButton("Сохранить назначения")
        save_button.setObjectName("AccentButton")
        reset_button = QPushButton("Вернуть стандартные")
        save_button.clicked.connect(self.save_hotkey_preferences)
        reset_button.clicked.connect(self.reset_hotkey_preferences)
        buttons.addWidget(save_button)
        buttons.addWidget(reset_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return page

    def _build_about_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)
        title = QLabel(f"MacroPilot {APP_VERSION}")
        title.setObjectName("AppTitle")
        layout.addWidget(title)
        text = QLabel(
            "Открытый инструмент для записи игровых макросов, автоматизации экрана, "
            "OCR и визуальных сценариев. Лицензия MIT."
        )
        text.setWordWrap(True)
        text.setObjectName("MutedText")
        layout.addWidget(text)
        author = QLabel(f"Автор: {AUTHOR_NAME}")
        layout.addWidget(author)
        links = QHBoxLayout()
        project_button = QPushButton("GitHub проекта")
        support_button = QPushButton("Поддержать развитие")
        support_button.setObjectName("SupportButton")
        project_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(PROJECT_URL)))
        support_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(SUPPORT_URL)))
        links.addWidget(project_button)
        links.addWidget(support_button)
        links.addStretch(1)
        layout.addLayout(links)
        update_card = QFrame()
        update_card.setObjectName("Card")
        update_layout = QHBoxLayout(update_card)
        self.update_state_label = QLabel("Обновления через GitHub Releases")
        self.update_state_label.setWordWrap(True)
        self.update_button = QPushButton("Проверить обновления")
        self.update_button.clicked.connect(self._update_button_clicked)
        update_layout.addWidget(self.update_state_label, 1)
        update_layout.addWidget(self.update_button)
        layout.addWidget(update_card)
        warning = QLabel(
            "Windows Defender иногда ошибочно помечает приложения с глобальным перехватом "
            "клавиатуры и мыши. Загружайте MacroPilot только из официального репозитория."
        )
        warning.setWordWrap(True)
        warning.setObjectName("WarningText")
        layout.addWidget(warning)
        layout.addStretch(1)
        return page

    @staticmethod
    def _hotkey_combo(current: str) -> QComboBox:
        combo = QComboBox()
        combo.addItems(FUNCTION_HOTKEYS)
        combo.setCurrentText(current)
        return combo

    def _connect_bridge(self) -> None:
        self.bridge.record_stop_requested.connect(self.finish_recording)
        self.bridge.recorder_error.connect(self._recorder_error)
        self.bridge.recorder_warning.connect(self._recorder_warning)
        self.bridge.runner_progress.connect(self._set_progress)
        self.bridge.runner_finished.connect(self._runner_finished)
        self.bridge.global_action.connect(self._handle_global_action)
        self.bridge.update_check_finished.connect(self._finish_update_check)
        self.bridge.update_progress.connect(self._set_update_progress)
        self.bridge.update_download_finished.connect(self._finish_update_download)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _set_mode(self, mode: str, status: str) -> None:
        self.mode = mode
        labels = {
            "idle": "ГОТОВО",
            "recording": "ЗАПИСЬ",
            "playing": "ВЫПОЛНЕНИЕ",
            "countdown": "ОТСЧЁТ",
        }
        self.mode_label.setText(labels.get(mode, mode.upper()))
        self.mode_label.setProperty("mode", mode)
        self.mode_label.style().unpolish(self.mode_label)
        self.mode_label.style().polish(self.mode_label)
        self._set_status(status)
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        idle = self.mode == "idle"
        has_events = bool(self.events)
        for widget, enabled in (
            (self.record_button, idle),
            (self.play_button, idle and has_events),
            (self.stop_button, not idle),
            (self.load_macro_button, idle),
            (self.save_macro_button, idle and has_events),
            (self.to_script_button, idle and has_events),
            (self.delete_events_button, idle and has_events),
            (self.clear_events_button, idle and has_events),
            (self.precision_combo, idle),
            (self.record_moves_check, idle),
            (self.minimize_check, idle),
            (self.speed_spin, idle),
            (self.repeats_spin, idle and not self.infinite_check.isChecked()),
            (self.infinite_check, idle),
            (self.block_mouse_check, idle and WINDOWS_NATIVE_AVAILABLE),
            (self.script_run_button, idle),
            (self.script_stop_button, not idle),
            (self.script_validate_button, idle),
            (self.script_open_button, idle),
            (self.script_save_button, idle),
            (self.script_example_button, idle),
            (self.script_to_graph_button, idle),
            (self.ocr_region_button, idle),
            (self.script_speed_spin, idle),
            (self.code_editor, idle),
        ):
            widget.setEnabled(enabled)
        self.graph_editor.set_enabled(idle)
        self.update_button.setEnabled(idle and not self.update_busy)

    def _refresh_event_table(
        self,
        total_event_count: int | None = None,
        duration: float | None = None,
    ) -> None:
        self.event_model.set_events(self.events)
        total = len(self.events) if total_event_count is None else total_event_count
        seconds = macro_duration(self.events) if duration is None else duration
        hidden = max(0, total - MAX_TABLE_ROWS)
        hidden_text = f" · не показано в таблице: {hidden}" if hidden else ""
        capacity = ""
        if self.mode == "recording":
            capacity = f" · заполнено: {min(100.0, total * 100 / MAX_RECORDED_EVENTS):.1f}%"
        self.record_summary.setText(
            f"Событий: {total} · длительность: {seconds:.2f} с{capacity}{hidden_text}"
        )
        self._refresh_controls()

    def start_recording(self) -> None:
        if self.mode != "idle":
            return
        append = False
        if self.events:
            answer = QMessageBox.question(
                self,
                "В макросе уже есть запись",
                f"Текущий макрос содержит {len(self.events)} событий.\n\n"
                "Да — продолжить запись с её конца.\n"
                "Нет — перезаписать макрос с начала.\n"
                "Отмена — ничего не менять.",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                self._set_status("Начало записи отменено — текущий макрос сохранён")
                return
            append = answer == QMessageBox.StandardButton.Yes
            if append and len(self.events) >= MAX_RECORDED_EVENTS:
                QMessageBox.warning(
                    self,
                    "Продолжение невозможно",
                    "Запись уже достигла безопасного предела событий.",
                )
                return
        self._begin_recording(append)

    def _begin_recording(self, append: bool) -> None:
        base_count = len(self.events) if append else 0
        base_duration = macro_duration(self.events) if append else 0.0
        available = MAX_RECORDED_EVENTS - base_count
        if available <= 0:
            return
        precision_name = self.precision_combo.currentText()
        recorder = EventRecorder(
            record_moves=self.record_moves_check.isChecked(),
            request_stop=self.bridge.record_stop_requested.emit,
            report_error=self.bridge.recorder_error.emit,
            report_warning=self.bridge.recorder_warning.emit,
            move_interval=RECORDING_PRECISION_OPTIONS.get(
                precision_name,
                RECORDING_PRECISION_OPTIONS[DEFAULT_RECORDING_PRECISION],
            ),
            hotkeys=self.hotkey_settings,
        )
        recorder.capacity_base_count = base_count
        recorder.max_recorded_events = available
        try:
            recorder.start()
        except Exception as exc:
            try:
                recorder.stop()
            except Exception:
                pass
            QMessageBox.critical(self, APP_NAME, f"Не удалось начать запись:\n{exc}")
            self._set_mode("idle", "Запись не запущена")
            return
        self.recorder = recorder
        self.recording_append_mode = append
        self.recording_base_count = base_count
        self.recording_base_duration = base_duration
        if not append:
            self.events = []
            self.current_macro_path = None
            self._refresh_event_table()
        action = "Продолжается запись" if append else "Идёт запись"
        input_mode = " · игровые scan-коды" if WINDOWS_NATIVE_AVAILABLE else ""
        self._set_mode(
            "recording",
            f"{action}{input_mode} · {precision_name} · "
            f"{self.hotkey_settings.finish_recording}/{self.hotkey_settings.stop} — закончить",
        )
        if self.minimize_check.isChecked():
            self._minimize_for_action()
        self.recording_timer.start()

    def _refresh_recording_preview(self) -> None:
        if self.mode != "recording" or self.recorder is None:
            self.recording_timer.stop()
            return
        new_count, new_duration = self.recorder.recording_stats()
        preview_space = max(0, MAX_TABLE_ROWS - self.recording_base_count)
        desired = min(new_count, preview_space)
        previewed = max(0, len(self.events) - self.recording_base_count)
        if previewed < desired:
            self.events.extend(self.recorder.snapshot_range(previewed, desired))
        total = self.recording_base_count + new_count
        duration = self.recording_base_duration + new_duration if new_count else self.recording_base_duration
        self._refresh_event_table(total, duration)

    def _merge_recorded_segment(self, new_events: list[dict[str, Any]]) -> None:
        if not self.recording_append_mode:
            self.events = new_events
            return
        del self.events[self.recording_base_count :]
        for event in new_events:
            event["t"] = round(self.recording_base_duration + float(event["t"]), 6)
        self.events.extend(new_events)

    def finish_recording(self, reason: str = "Запись остановлена") -> None:
        if self.mode != "recording" or self.recorder is None:
            return
        self.recording_timer.stop()
        recorder = self.recorder
        recording_error = recorder.last_error
        new_events = recorder.stop()
        append_mode = self.recording_append_mode
        self._merge_recorded_segment(new_events)
        self.recorder = None
        self.recording_append_mode = False
        self.recording_base_count = 0
        self.recording_base_duration = 0.0
        self._restore_window()
        self._refresh_event_table()
        if append_mode:
            message = f"{reason}. Добавлено: {len(new_events)} · всего: {len(self.events)}"
        else:
            message = f"{reason}. Событий: {len(self.events)}"
        self._set_mode("idle", message)
        if recording_error:
            QMessageBox.critical(
                self,
                "Ошибка записи",
                f"Перехватчик ввода сообщил ошибку:\n\n{recording_error}",
            )

    def _recorder_error(self, text: str) -> None:
        self._set_status(f"Ошибка записи: {text}")

    def _recorder_warning(self, text: str) -> None:
        if self.mode == "recording":
            self._set_status(text)
            QApplication.beep()

    def _minimize_for_action(self) -> None:
        self.window_was_minimized = True
        self.showMinimized()

    def _restore_window(self) -> None:
        if not self.window_was_minimized:
            return
        self.window_was_minimized = False
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _start_countdown(self, label: str, callback: Any) -> None:
        self._countdown_label = label
        self._countdown_callback = callback
        self._countdown_value = 3
        self._set_mode(
            "countdown",
            f"{label} через 3… {self.hotkey_settings.stop} — отмена",
        )
        self.countdown_timer.start()

    def _countdown_tick(self) -> None:
        if self.mode != "countdown":
            self.countdown_timer.stop()
            return
        self._countdown_value -= 1
        if self._countdown_value <= 0:
            self.countdown_timer.stop()
            callback = self._countdown_callback
            self._countdown_callback = None
            if callback is not None:
                callback()
            return
        self._set_status(
            f"{self._countdown_label} через {self._countdown_value}… "
            f"{self.hotkey_settings.stop} — отмена"
        )

    def play_recording_countdown(self) -> None:
        if self.mode != "idle" or not self.events:
            return
        speed = self.speed_spin.value()
        repeats = None if self.infinite_check.isChecked() else self.repeats_spin.value()
        if repeats is not None:
            estimate = macro_duration(self.events) * repeats / speed
            if (repeats > 20 or estimate > 300) and QMessageBox.question(
                self,
                "Длительное выполнение",
                f"Макрос будет повторён {repeats} раз.\n"
                f"Ориентировочная длительность: {estimate:.0f} с.\n\nПродолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return
        self._start_countdown(
            "Воспроизведение начнётся",
            lambda: self._begin_recording_playback(speed, repeats),
        )

    def _begin_recording_playback(self, speed: float, repeats: int | None) -> None:
        self._active_graph_line_map = {}
        if self.minimize_check.isChecked():
            self._minimize_for_action()
        self._start_runner(speed)
        assert self.runner is not None
        text = "Бесконечное воспроизведение" if repeats is None else "Воспроизведение записи"
        self._set_mode("playing", f"{text} · {self.hotkey_settings.stop} — остановить")
        self.worker = threading.Thread(
            target=self.runner.run_recording,
            args=(self.events.copy(), repeats),
            name="MacroPilotPlayback",
            daemon=True,
        )
        self.worker.start()

    def _current_script_source(self) -> str:
        if self.script_modes.currentWidget() is self.graph_editor:
            return self.graph_editor.to_source()
        return self.code_editor.toPlainText()

    def validate_current_script(self, show_dialog: bool = True) -> Any:
        self.code_editor.clear_error()
        try:
            source = self._current_script_source()
            program = parse_script(source)
            self._validate_script_keys(program.nodes)
        except GraphError as exc:
            self._set_status(str(exc))
            if show_dialog:
                QMessageBox.warning(self, "Граф пока не готов", str(exc))
            return None
        except ScriptError as exc:
            if self.script_modes.currentWidget() is self.code_editor:
                self.code_editor.highlight_error(exc.line_no)
            self._set_status(str(exc))
            if show_dialog:
                QMessageBox.critical(self, "Ошибка сценария", str(exc))
            return None
        except ValueError as exc:
            self._set_status(str(exc))
            if show_dialog:
                QMessageBox.critical(self, "Ошибка сценария", str(exc))
            return None
        self._set_status(
            f"Сценарий корректен · команд с учётом циклов: {program.estimated_steps}"
        )
        if show_dialog:
            QMessageBox.information(
                self,
                APP_NAME,
                f"Сценарий корректен.\nКоманд с учётом циклов: {program.estimated_steps}",
            )
        return program

    def _script_directory(self) -> Path:
        if self.script_modes.currentWidget() is self.graph_editor and self.graph_editor.graph_path:
            return self.graph_editor.graph_path.resolve().parent
        if self.current_script_path is not None:
            return self.current_script_path.resolve().parent
        if bool(getattr(sys, "frozen", False)):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent

    def _validate_script_keys(self, nodes: Iterable[ScriptNode]) -> None:
        directory = self._script_directory()
        for node in nodes:
            if isinstance(node, RepeatBlock):
                self._validate_script_keys(node.body)
            elif isinstance(node, IfBlock):
                self._validate_script_keys(node.true_body)
                self._validate_script_keys(node.false_body)
            elif node.name in {"PRESS", "KEY_DOWN", "KEY_UP", "HOTKEY"}:
                for token in node.args:
                    try:
                        resolve_script_key(str(token))
                    except ValueError as exc:
                        raise ScriptError(node.line_no, str(exc)) from exc
            elif node.name in {"WAIT_IMAGE", "CLICK_IMAGE"}:
                image = Path(str(node.args[0])).expanduser()
                if not image.is_absolute():
                    image = directory / image
                if not image.is_file():
                    raise ScriptError(node.line_no, f"файл изображения не найден: {image}")

    def play_script_countdown(self) -> None:
        if self.mode != "idle":
            return
        program = self.validate_current_script(show_dialog=False)
        if program is None:
            QMessageBox.critical(self, "Ошибка сценария", self.status_label.text())
            return
        if program.estimated_steps > 10_000 and QMessageBox.question(
            self,
            "Большой сценарий",
            f"Сценарий выполнит около {program.estimated_steps:,} команд.\n\nПродолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        speed = self.script_speed_spin.value()
        directory = self._script_directory()
        if self.script_modes.currentWidget() is self.graph_editor:
            _source, self._active_graph_line_map = graph_to_script_with_line_map(
                self.graph_editor.document
            )
        else:
            self._active_graph_line_map = {}
        self._start_countdown(
            "Сценарий начнётся",
            lambda: self._begin_script_playback(speed, program.nodes, directory),
        )

    def _begin_script_playback(
        self,
        speed: float,
        nodes: Iterable[ScriptNode],
        directory: Path,
    ) -> None:
        if self.minimize_check.isChecked():
            self._minimize_for_action()
        self._start_runner(speed, directory)
        assert self.runner is not None
        self._set_mode(
            "playing",
            f"Выполняется сценарий · {self.hotkey_settings.stop} — остановить",
        )
        self.worker = threading.Thread(
            target=self.runner.run_script,
            args=(tuple(nodes),),
            name="MacroPilotScript",
            daemon=True,
        )
        self.worker.start()

    def _start_runner(self, speed: float, directory: Path | None = None) -> None:
        self.runner = AutomationRunner(
            speed=speed,
            on_progress=self.bridge.runner_progress.emit,
            on_finished=self.bridge.runner_finished.emit,
            script_directory=directory,
            block_physical_mouse=self.block_mouse_check.isChecked(),
        )

    def _set_progress(self, text: str) -> None:
        if self.mode == "playing":
            match = re.match(r"Строка (\d+):", text)
            if match is not None and self._active_graph_line_map:
                self.graph_editor.set_active_node(
                    self._active_graph_line_map.get(int(match.group(1)))
                )
            self._set_status(f"{text} · {self.hotkey_settings.stop} — остановить")

    def _runner_finished(self, stopped: bool, error: Any) -> None:
        self.runner = None
        self.worker = None
        self._active_graph_line_map = {}
        self.graph_editor.set_active_node(None)
        self._restore_window()
        self._set_mode("idle", "Остановлено" if stopped else "Выполнение завершено")
        if error:
            self._set_status(f"Ошибка выполнения: {error}")
            QMessageBox.critical(self, APP_NAME, f"Ошибка выполнения:\n{error}")

    def stop_current(self) -> None:
        if self.mode == "countdown":
            self.countdown_timer.stop()
            self._countdown_callback = None
            self._set_mode("idle", "Запуск отменён")
        elif self.mode == "recording":
            self.finish_recording()
        elif self.mode == "playing" and self.runner is not None:
            self._set_status("Останавливаю…")
            self.runner.stop()

    def delete_selected_events(self) -> None:
        rows = sorted(
            {index.row() for index in self.event_table.selectionModel().selectedRows()},
            reverse=True,
        )
        if not rows:
            return
        for row in rows:
            if 0 <= row < len(self.events):
                self.events.pop(row)
        self.current_macro_path = None
        self._refresh_event_table()
        self._set_status(f"Удалено событий: {len(rows)}")

    def clear_events(self) -> None:
        if not self.events:
            return
        if QMessageBox.question(
            self,
            APP_NAME,
            "Очистить текущую запись?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self.events.clear()
        self.current_macro_path = None
        self._refresh_event_table()
        self._set_status("Запись очищена")

    def save_macro_file(self) -> None:
        if not self.events:
            return
        initial = self.current_macro_path or Path.cwd() / "macro.macro.json"
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Сохранить макрос",
            str(initial),
            "Макрос MacroPilot (*.macro.json);;JSON (*.json);;Все файлы (*)",
        )
        if not path:
            return
        destination = Path(path)
        if not destination.name.lower().endswith((".macro.json", ".json")):
            destination = destination.with_name(destination.name + ".macro.json")
        try:
            save_macro(destination, self.events)
        except (OSError, MacroFormatError) as exc:
            QMessageBox.critical(self, APP_NAME, f"Не удалось сохранить макрос:\n{exc}")
            return
        self.current_macro_path = destination
        self._set_status(f"Макрос сохранён: {destination.name}")

    def load_macro_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Открыть макрос",
            str(self.current_macro_path.parent if self.current_macro_path else Path.cwd()),
            "Макрос MacroPilot (*.macro.json);;JSON (*.json);;Все файлы (*)",
        )
        if not path:
            return
        try:
            events = load_macro(path)
        except (OSError, MacroFormatError) as exc:
            QMessageBox.critical(self, APP_NAME, f"Не удалось открыть макрос:\n{exc}")
            return
        self.events = events
        self.current_macro_path = Path(path)
        self._refresh_event_table()
        self._set_status(f"Открыт макрос: {self.current_macro_path.name}")

    def convert_to_script(self) -> None:
        if not self.events:
            return
        self.code_editor.setPlainText(events_to_script(self.events))
        self.code_editor.document().setModified(True)
        self.current_script_path = None
        self.tabs.setCurrentWidget(self.script_tab)
        self.script_modes.setCurrentWidget(self.code_editor)
        self._set_status("Запись преобразована в редактируемый сценарий")

    def load_example_script(self) -> None:
        if self.code_editor.document().isModified() and QMessageBox.question(
            self,
            "Заменить сценарий?",
            "Несохранённые изменения кода будут потеряны. Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self.code_editor.setPlainText(EXAMPLE_SCRIPT)
        self.code_editor.document().setModified(False)
        self.current_script_path = None
        self.script_modes.setCurrentWidget(self.code_editor)
        self._set_status("Загружен пример сценария")

    def save_script_file(self) -> None:
        initial = self.current_script_path or Path.cwd() / "scenario.macro.txt"
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Сохранить сценарий",
            str(initial),
            "Сценарий MacroPilot (*.macro.txt);;Текст (*.txt);;Все файлы (*)",
        )
        if not path:
            return
        destination = Path(path)
        if not destination.name.lower().endswith((".macro.txt", ".txt")):
            destination = destination.with_name(destination.name + ".macro.txt")
        try:
            destination.write_text(
                self.code_editor.toPlainText().rstrip() + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            QMessageBox.critical(self, APP_NAME, f"Не удалось сохранить сценарий:\n{exc}")
            return
        self.current_script_path = destination
        self.code_editor.document().setModified(False)
        self._set_status(f"Сценарий сохранён: {destination.name}")

    def load_script_file(self) -> None:
        if self.code_editor.document().isModified() and QMessageBox.question(
            self,
            "Заменить сценарий?",
            "Несохранённые изменения кода будут потеряны. Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Открыть сценарий",
            str(self.current_script_path.parent if self.current_script_path else Path.cwd()),
            "Сценарий MacroPilot (*.macro.txt);;Текст (*.txt);;Все файлы (*)",
        )
        if not path:
            return
        source = Path(path)
        try:
            if source.stat().st_size > SCRIPT_FILE_LIMIT:
                raise OSError("Файл сценария больше 2 МБ")
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            QMessageBox.critical(self, APP_NAME, f"Не удалось открыть сценарий:\n{exc}")
            return
        self.code_editor.setPlainText(text)
        self.code_editor.document().setModified(False)
        self.current_script_path = source
        self.script_modes.setCurrentWidget(self.code_editor)
        self._set_status(f"Открыт сценарий: {source.name}")

    def code_to_graph(self) -> None:
        try:
            self.graph_editor.load_source(self.code_editor.toPlainText())
        except (GraphError, ScriptError) as exc:
            if isinstance(exc, ScriptError):
                self.code_editor.highlight_error(exc.line_no)
            QMessageBox.critical(self, "Не удалось создать граф", str(exc))
            self._set_status(str(exc))
            return
        self.script_modes.setCurrentWidget(self.graph_editor)
        self._set_status("Код преобразован в связанный граф")

    def graph_to_code(self, source: str) -> None:
        if self.code_editor.document().isModified() and self.code_editor.toPlainText().strip():
            if QMessageBox.question(
                self,
                "Заменить код?",
                "Граф сгенерирует новый код и заменит текущий текст. Продолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return
        self.code_editor.setPlainText(source)
        self.code_editor.document().setModified(True)
        self.current_script_path = None
        self.script_modes.setCurrentWidget(self.code_editor)
        self._set_status("Граф преобразован в код MacroPilot")

    def pick_ocr_region(self) -> None:
        if self.mode != "idle" or self.region_overlay is not None:
            return
        overlay = ScreenRegionOverlay()
        self.region_overlay = overlay
        overlay.region_selected.connect(self._ocr_region_selected)
        overlay.cancelled.connect(self._ocr_region_cancelled)
        overlay.show()
        overlay.raise_()
        overlay.activateWindow()

    def _ocr_region_selected(self, x: int, y: int, width: int, height: int) -> None:
        self.region_overlay = None
        line = f"OCR_TEXT text {x} {y} {width} {height} auto"
        cursor = self.code_editor.textCursor()
        if cursor.positionInBlock() != 0:
            cursor.insertText("\n")
        cursor.insertText(line + "\n")
        self.code_editor.setTextCursor(cursor)
        self.code_editor.document().setModified(True)
        self.tabs.setCurrentWidget(self.script_tab)
        self.script_modes.setCurrentWidget(self.code_editor)
        self._set_status(f"Добавлена OCR-область: {width} × {height}")

    def _ocr_region_cancelled(self) -> None:
        self.region_overlay = None
        self._set_status("Выбор OCR-области отменён")

    def reset_hotkey_preferences(self) -> None:
        defaults = HotkeySettings()
        self.play_hotkey_combo.setCurrentText(defaults.play)
        self.record_hotkey_combo.setCurrentText(defaults.record)
        self.finish_hotkey_combo.setCurrentText(defaults.finish_recording)
        self.stop_hotkey_combo.setCurrentText(defaults.stop)
        self._set_status("Стандартные назначения выбраны · нажмите «Сохранить»")

    def save_hotkey_preferences(self) -> None:
        if self.mode != "idle":
            return
        try:
            settings = HotkeySettings(
                play=self.play_hotkey_combo.currentText(),
                record=self.record_hotkey_combo.currentText(),
                finish_recording=self.finish_hotkey_combo.currentText(),
                stop=self.stop_hotkey_combo.currentText(),
            )
            save_hotkey_settings(settings)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(
                self,
                "Горячие клавиши",
                f"Не удалось сохранить настройки:\n{exc}",
            )
            return
        self.hotkey_settings = settings
        self.hotkeys_held.clear()
        self._restart_safety_listener()
        self._update_hotkey_labels()
        self._set_status("Горячие клавиши сохранены и уже работают")

    def _update_hotkey_labels(self) -> None:
        keys = self.hotkey_settings
        self.record_button.setText(f"● Запись ({keys.record})")
        self.play_button.setText(f"▶ Воспроизвести ({keys.play})")
        self.stop_button.setText(f"■ Остановить ({keys.stop})")
        self.script_run_button.setText(f"▶ Запустить ({keys.play})")
        self.script_stop_button.setText(f"■ Остановить ({keys.stop})")
        self.hotkey_hint.setText(
            f"{keys.play} запуск · {keys.record} запись · {keys.stop} стоп"
        )

    def _restart_safety_listener(self) -> None:
        listener = self.safety_listener
        self.safety_listener = None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass
        self._start_safety_listener()

    def _start_safety_listener(self) -> None:
        if keyboard is None:
            return

        def on_press(key: Any, _injected: bool = False) -> None:
            del _injected
            name = normalize_hotkey_name(key)
            if name is None or name in self.hotkeys_held:
                return
            self.hotkeys_held.add(name)
            if name == self.hotkey_settings.play:
                self.bridge.global_action.emit("play")
            elif name == self.hotkey_settings.record:
                self.bridge.global_action.emit("record")
            elif name == self.hotkey_settings.finish_recording:
                self.bridge.global_action.emit("finish")
            elif name == self.hotkey_settings.stop:
                self.bridge.global_action.emit("stop")

        def on_release(key: Any, _injected: bool = False) -> None:
            del _injected
            name = normalize_hotkey_name(key)
            if name is not None:
                self.hotkeys_held.discard(name)

        try:
            self.safety_listener = keyboard.Listener(
                on_press=on_press,
                on_release=on_release,
            )
            self.safety_listener.start()
        except Exception as exc:
            self._set_status(f"Не удалось включить глобальные клавиши: {exc}")

    def _handle_global_action(self, action: str) -> None:
        if action == "play":
            self.start_current_playback()
        elif action == "record":
            self.start_recording()
        elif action == "finish":
            self.finish_recording()
        elif action == "stop":
            self.stop_current()

    def start_current_playback(self) -> None:
        if self.mode != "idle":
            return
        if self.tabs.currentWidget() is self.record_tab:
            self.play_recording_countdown()
        elif self.tabs.currentWidget() is self.script_tab:
            self.play_script_countdown()
        elif self.events:
            self.play_recording_countdown()
        else:
            self.play_script_countdown()

    def _update_button_clicked(self) -> None:
        if self.available_release is not None:
            self._prompt_update(self.available_release)
        else:
            self.check_for_updates(manual=True)

    def check_for_updates(self, manual: bool = True) -> None:
        if self.update_busy:
            return
        if self.mode != "idle":
            if manual:
                QMessageBox.information(
                    self,
                    APP_NAME,
                    "Сначала остановите запись или воспроизведение.",
                )
            return
        self.update_busy = True
        self.update_state_label.setText("Проверяю актуальную версию…")
        if manual:
            self._set_status("Проверяю обновления на GitHub…")
        self._refresh_controls()

        def worker() -> None:
            try:
                release = fetch_latest_release(PROJECT_REPOSITORY)
            except UpdateError as exc:
                self.bridge.update_check_finished.emit(manual, None, str(exc))
            else:
                self.bridge.update_check_finished.emit(manual, release, None)

        threading.Thread(
            target=worker,
            name="MacroPilotUpdateCheck",
            daemon=True,
        ).start()

    def _finish_update_check(
        self,
        manual: bool,
        release: Any,
        error: Any,
    ) -> None:
        self.update_busy = False
        if error:
            self.update_state_label.setText("Проверка обновлений сейчас недоступна")
            if manual:
                self._set_status(f"Не удалось проверить обновления: {error}")
                QMessageBox.critical(self, "Обновления", str(error))
            self._refresh_controls()
            return
        assert isinstance(release, ReleaseInfo)
        try:
            newer = is_newer_version(release.version, APP_VERSION)
        except UpdateError as exc:
            self.update_state_label.setText("GitHub вернул неизвестную версию")
            if manual:
                QMessageBox.critical(self, "Обновления", str(exc))
            self._refresh_controls()
            return
        if newer:
            self.available_release = release
            self.update_state_label.setText(f"Доступно обновление {release.version}")
            self.update_button.setText("Установить обновление")
            self._set_status(f"Доступна новая версия MacroPilot {release.version}")
            self._refresh_controls()
            if manual:
                self._prompt_update(release)
            return
        self.available_release = None
        self.update_state_label.setText(f"Установлена актуальная версия {APP_VERSION}")
        self.update_button.setText("Проверить обновления")
        if manual:
            self._set_status("Установлена актуальная версия")
            QMessageBox.information(
                self,
                "Обновления",
                f"MacroPilot {APP_VERSION} — актуальная версия.",
            )
        self._refresh_controls()

    def _prompt_update(self, release: ReleaseInfo) -> None:
        if sys.platform != "win32":
            QDesktopServices.openUrl(QUrl(release.page_url))
            return
        try:
            asset = choose_release_asset(
                release,
                frozen=bool(getattr(sys, "frozen", False)),
            )
        except UpdateError as exc:
            if QMessageBox.question(
                self,
                "Обновления",
                f"{exc}\n\nОткрыть страницу релиза в браузере?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            ) == QMessageBox.StandardButton.Yes:
                QDesktopServices.openUrl(QUrl(release.page_url))
            return
        notes = release.notes.strip()
        if len(notes) > 600:
            notes = notes[:597].rstrip() + "…"
        details = f"\n\n{notes}" if notes else ""
        if QMessageBox.question(
            self,
            "Доступно обновление",
            f"Установить MacroPilot {release.version}?{details}\n\n"
            "Приложение скачает архив, проверит его и автоматически перезапустится.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        ) != QMessageBox.StandardButton.Yes:
            return
        self.update_busy = True
        self._set_status(f"Скачиваю MacroPilot {release.version}…")
        self.update_state_label.setText(f"Загрузка версии {release.version}…")
        self._refresh_controls()
        threading.Thread(
            target=self._download_update_worker,
            args=(release, asset),
            name="MacroPilotUpdateDownload",
            daemon=True,
        ).start()

    def _download_update_worker(self, release: ReleaseInfo, asset: ReleaseAsset) -> None:
        archive = temporary_update_path(release.version)
        last_percent = -1

        def progress(received: int, total: int) -> None:
            nonlocal last_percent
            if total > 0:
                percent = min(100, int(received * 100 / total))
                if percent == last_percent:
                    return
                last_percent = percent
                text = f"Скачиваю MacroPilot {release.version}: {percent}%"
            else:
                text = f"Скачано обновления: {received / (1024 * 1024):.1f} МБ"
            self.bridge.update_progress.emit(text)

        try:
            download_release_asset(asset, archive, progress=progress)
            payload_subdir = inspect_update_archive(
                archive,
                frozen=bool(getattr(sys, "frozen", False)),
            )
        except UpdateError as exc:
            try:
                archive.unlink(missing_ok=True)
            except OSError:
                pass
            self.bridge.update_download_finished.emit(None, None, str(exc))
        except Exception as exc:
            try:
                archive.unlink(missing_ok=True)
            except OSError:
                pass
            self.bridge.update_download_finished.emit(
                None,
                None,
                f"Непредвиденная ошибка загрузки: {exc}",
            )
        else:
            self.bridge.update_download_finished.emit(archive, payload_subdir, None)

    def _set_update_progress(self, text: str) -> None:
        self._set_status(text)

    def _finish_update_download(self, archive: Any, payload_subdir: Any, error: Any) -> None:
        self.update_busy = False
        self._refresh_controls()
        if error:
            self.update_state_label.setText("Не удалось загрузить обновление")
            self._set_status(f"Ошибка обновления: {error}")
            QMessageBox.critical(self, "Обновления", str(error))
            return
        assert isinstance(archive, Path) and isinstance(payload_subdir, str)
        try:
            launch_update_installer(archive, payload_subdir)
        except UpdateError as exc:
            try:
                archive.unlink(missing_ok=True)
            except OSError:
                pass
            self.update_state_label.setText("Не удалось запустить установщик")
            self._set_status(f"Ошибка обновления: {exc}")
            QMessageBox.critical(self, "Обновления", str(exc))
            return
        self._set_status("Обновление загружено. Перезапускаю MacroPilot…")
        QTimer.singleShot(250, self.close)

    def show_update_error_log(self) -> None:
        install_directory = (
            Path(sys.executable).resolve().parent
            if bool(getattr(sys, "frozen", False))
            else Path(__file__).resolve().parent
        )
        log = install_directory / "update-error.log"
        if not log.is_file():
            return
        try:
            text = log.read_text(encoding="utf-8-sig").strip()
            log.unlink(missing_ok=True)
        except OSError:
            return
        if text:
            QMessageBox.critical(self, "Обновление", text)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.countdown_timer.stop()
        self.recording_timer.stop()
        if self.recorder is not None:
            try:
                self.recorder.stop()
            except Exception:
                pass
            self.recorder = None
        if self.runner is not None:
            self.runner.stop()
        if self.safety_listener is not None:
            try:
                self.safety_listener.stop()
            except Exception:
                pass
            self.safety_listener = None
        if self.region_overlay is not None:
            self.region_overlay.close()
            self.region_overlay = None
        event.accept()


APP_STYLE_SHEET = GRAPH_STYLE_SHEET + """
QMainWindow, QWidget { background: #0d121b; }
QWidget#qt_scrollarea_viewport { background: #20252c; }
QLabel#AppTitle { font-size: 22pt; font-weight: 700; color: #f2f6fc; }
QLabel#SectionTitle { font-size: 16pt; font-weight: 650; color: #f2f6fc; }
QLabel#Pill, QLabel#ModePill {
    background: #1b2533;
    border: 1px solid #304158;
    border-radius: 10px;
    padding: 5px 10px;
    color: #aebdd1;
}
QLabel#ModePill[mode="recording"] { background: #5a2530; border-color: #d85a6b; color: #ffdce1; }
QLabel#ModePill[mode="playing"] { background: #174a3b; border-color: #43b88c; color: #d6ffef; }
QLabel#ModePill[mode="countdown"] { background: #574119; border-color: #d4a54a; color: #fff0ce; }
QLabel#StatusLabel { color: #acb9ca; padding: 3px; }
QLabel#WarningText { color: #efb082; }
QFrame#Card, QGroupBox {
    background: #141b26;
    border: 1px solid #263448;
    border-radius: 8px;
}
QPushButton { min-height: 22px; }
QPushButton#AccentButton { background: #376fc2; border-color: #568bd8; }
QPushButton#AccentButton:hover { background: #437dce; }
QPushButton#RecordButton { background: #a83d4d; border-color: #d45a6b; }
QPushButton#RecordButton:hover { background: #bd485a; }
QPushButton#DangerButton { background: #563039; border-color: #8c4c58; }
QPushButton#SupportButton { background: #963c70; border-color: #c75d98; }
QTabWidget::pane { border: 1px solid #263448; border-radius: 7px; background: #111722; }
QTabBar::tab {
    background: #161e2a;
    color: #93a3b8;
    padding: 8px 16px;
    margin-right: 2px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}
QTabBar::tab:selected { background: #26354a; color: #eef4fc; }
QTableView {
    background: #111722;
    gridline-color: #253043;
    border: 1px solid #263448;
    border-radius: 6px;
    selection-background-color: #314d72;
}
QHeaderView::section {
    background: #1c2634;
    color: #cbd6e5;
    border: 0;
    border-right: 1px solid #303d50;
    padding: 7px;
}
QPlainTextEdit {
    background: #0b1018;
    color: #dce7f4;
    border: 1px solid #2c3b50;
    selection-background-color: #33527b;
    padding: 8px;
}
QCheckBox { spacing: 7px; }
QCheckBox::indicator { width: 16px; height: 16px; }
"""


def run_app() -> int:
    application = QApplication.instance() or QApplication(sys.argv)
    application.setApplicationName(APP_NAME)
    application.setApplicationVersion(APP_VERSION)
    application.setOrganizationName(AUTHOR_NAME)
    application.setStyle("Fusion")
    application.setStyleSheet(APP_STYLE_SHEET)
    if PYNPUT_IMPORT_ERROR is not None:
        QMessageBox.critical(
            None,
            APP_NAME,
            "Не удалось загрузить pynput.\n\n"
            "Установите зависимости командой:\n"
            "python -m pip install -r requirements.txt\n\n"
            f"Техническая информация: {PYNPUT_IMPORT_ERROR}",
        )
        return 1
    window = MacroPilotQtWindow()
    window.show()
    QTimer.singleShot(100, window.show_update_error_log)
    return application.exec()
