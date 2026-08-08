from __future__ import annotations

import copy
import json
import re
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable, Iterable

from macro_core import (
    IfBlock,
    RepeatBlock,
    ScriptCommand,
    ScriptError,
    ScriptNode,
    parse_script,
    script_nodes_to_text,
)


BUTTONS = ("left", "right", "middle")


@dataclass(frozen=True, slots=True)
class FieldSpec:
    key: str
    label: str
    default: str
    kind: str = "text"
    choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ActionSpec:
    command: str
    title: str
    fields: tuple[FieldSpec, ...]


def _field(
    key: str,
    label: str,
    default: str,
    kind: str = "text",
    choices: tuple[str, ...] = (),
) -> FieldSpec:
    return FieldSpec(key, label, default, kind, choices)


ACTION_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec("WAIT", "Пауза", (_field("seconds", "Секунды", "0.5", "number"),)),
    ActionSpec(
        "MOVE",
        "Переместить мышь",
        (
            _field("x", "X", "500", "number"),
            _field("y", "Y", "350", "number"),
            _field("duration", "Длительность, с", "0", "number"),
        ),
    ),
    ActionSpec(
        "MOVE_BY",
        "Сдвинуть мышь",
        (
            _field("x", "По X", "50", "number"),
            _field("y", "По Y", "0", "number"),
            _field("duration", "Длительность, с", "0", "number"),
        ),
    ),
    ActionSpec(
        "CLICK",
        "Клик мыши",
        (
            _field("button", "Кнопка", "left", "choice", BUTTONS),
            _field("count", "Количество", "1", "number"),
            _field("interval", "Интервал, с", "0.1", "number"),
        ),
    ),
    ActionSpec(
        "CLICK_AT",
        "Клик по координате",
        (
            _field("x", "X", "500", "number"),
            _field("y", "Y", "350", "number"),
            _field("button", "Кнопка", "left", "choice", BUTTONS),
            _field("count", "Количество", "1", "number"),
            _field("interval", "Интервал, с", "0.1", "number"),
        ),
    ),
    ActionSpec(
        "WAIT_IMAGE",
        "Ждать изображение",
        (
            _field("path", "Файл изображения", "image.png", "path"),
            _field("timeout", "Тайм-аут, с (0 — бесконечно)", "30", "number"),
            _field("confidence", "Сходство (0.5–1)", "0.9", "number"),
        ),
    ),
    ActionSpec(
        "CLICK_IMAGE",
        "Найти и кликнуть изображение",
        (
            _field("path", "Файл изображения", "image.png", "path"),
            _field("button", "Кнопка", "left", "choice", BUTTONS),
            _field("timeout", "Тайм-аут, с (0 — бесконечно)", "30", "number"),
            _field("confidence", "Сходство (0.5–1)", "0.9", "number"),
        ),
    ),
    ActionSpec(
        "OCR_TEXT",
        "Распознать текст",
        (
            _field("variable", "Переменная", "text"),
            _field("x", "X", "0", "number"),
            _field("y", "Y", "0", "number"),
            _field("width", "Ширина", "300", "number"),
            _field("height", "Высота", "80", "number"),
            _field("language", "Язык", "auto"),
        ),
    ),
    ActionSpec(
        "OCR_NUMBER",
        "Распознать число",
        (
            _field("variable", "Переменная", "number"),
            _field("x", "X", "0", "number"),
            _field("y", "Y", "0", "number"),
            _field("width", "Ширина", "180", "number"),
            _field("height", "Высота", "50", "number"),
            _field("language", "Язык", "auto"),
        ),
    ),
    ActionSpec("DOWN", "Зажать кнопку мыши", (_field("button", "Кнопка", "left", "choice", BUTTONS),)),
    ActionSpec("UP", "Отпустить кнопку мыши", (_field("button", "Кнопка", "left", "choice", BUTTONS),)),
    ActionSpec(
        "SCROLL",
        "Прокрутка",
        (
            _field("x", "По горизонтали", "0", "number"),
            _field("y", "По вертикали", "-1", "number"),
        ),
    ),
    ActionSpec("PRESS", "Нажать клавишу", (_field("key", "Клавиша", "enter"),)),
    ActionSpec("KEY_DOWN", "Зажать клавишу", (_field("key", "Клавиша", "w"),)),
    ActionSpec("KEY_UP", "Отпустить клавишу", (_field("key", "Клавиша", "w"),)),
    ActionSpec(
        "HOTKEY",
        "Сочетание клавиш",
        (_field("keys", "Клавиши через +", "ctrl+c"),),
    ),
    ActionSpec(
        "TYPE",
        "Напечатать текст",
        (
            _field("text", "Текст", "Привет из MacroPilot!"),
            _field("interval", "Интервал, с", "0.03", "number"),
        ),
    ),
)

ACTION_BY_COMMAND = {spec.command: spec for spec in ACTION_SPECS}
ACTION_BY_TITLE = {spec.title: spec for spec in ACTION_SPECS}


def _number_text(value: Any) -> str:
    number = float(value)
    if number == 0:
        return "0"
    if number.is_integer():
        return str(int(number))
    return format(number, ".12g")


def action_values(command: ScriptCommand) -> dict[str, str]:
    args = command.args
    name = command.name
    if name == "WAIT":
        values = (args[0],)
    elif name in {"MOVE", "MOVE_BY", "CLICK", "CLICK_AT", "WAIT_IMAGE", "CLICK_IMAGE", "OCR_TEXT", "OCR_NUMBER", "SCROLL", "TYPE"}:
        values = args
    elif name in {"DOWN", "UP", "PRESS", "KEY_DOWN", "KEY_UP"}:
        values = (args[0],)
    elif name == "HOTKEY":
        return {"keys": "+".join(str(value) for value in args)}
    else:
        raise ValueError(f"Команда {name} не поддерживается визуальным редактором")
    spec = ACTION_BY_COMMAND[name]
    result: dict[str, str] = {}
    for field, value in zip(spec.fields, values):
        result[field.key] = _number_text(value) if isinstance(value, (int, float)) else str(value)
    return result


def build_action(command: str, values: dict[str, str]) -> ScriptCommand:
    spec = ACTION_BY_COMMAND[command]
    tokens: list[str] = []
    for field in spec.fields:
        value = values.get(field.key, "").strip()
        if field.key == "keys":
            keys = [item for item in re.split(r"[+,\s]+", value) if item]
            if not keys:
                raise ScriptError(1, "HOTKEY: укажите хотя бы одну клавишу")
            tokens.extend(json.dumps(item, ensure_ascii=False) for item in keys)
        elif field.kind in {"text", "path"} or field.key in {"key", "language"}:
            tokens.append(json.dumps(value, ensure_ascii=False))
        else:
            tokens.append(value)
    program = parse_script(f"{command} {' '.join(tokens)}")
    node = program.nodes[0]
    if not isinstance(node, ScriptCommand):  # pragma: no cover
        raise ValueError("Ожидалась команда")
    return node


def build_condition(
    value_kind: str,
    variable: str,
    operator: str,
    expected: str,
) -> IfBlock:
    name = "IF_TEXT" if value_kind == "text" else "IF_NUMBER"
    expected_token = json.dumps(expected, ensure_ascii=False) if value_kind == "text" else expected
    program = parse_script(f"{name} {variable} {operator} {expected_token}\nWAIT 0\nEND")
    node = program.nodes[0]
    if not isinstance(node, IfBlock):  # pragma: no cover
        raise ValueError("Ожидалось условие")
    return node


def describe_node(node: ScriptNode) -> tuple[str, str]:
    if isinstance(node, RepeatBlock):
        return "↻  Повтор", f"{node.count} раз"
    if isinstance(node, IfBlock):
        kind = "текст" if node.value_kind == "text" else "число"
        expected = repr(node.expected) if node.value_kind == "text" else _number_text(node.expected)
        return "◇  Условие", f"{kind}: {node.variable} {node.operator} {expected}"
    spec = ACTION_BY_COMMAND.get(node.name)
    title = spec.title if spec is not None else node.name
    source = script_nodes_to_text((node,)).strip()
    details = source[len(node.name) :].strip()
    return f"•  {title}", details


@dataclass(slots=True)
class _TreeRef:
    kind: str
    container: list[ScriptNode]
    index: int | None = None


class _ActionDialog:
    def __init__(
        self,
        parent: tk.Misc,
        command: ScriptCommand | None = None,
    ) -> None:
        self.parent = parent
        self.result: ScriptCommand | None = None
        self.initial = command
        self.values: dict[str, tk.StringVar] = {}
        self.window = tk.Toplevel(parent)
        self.window.title("Действие")
        self.window.resizable(False, False)
        self.window.transient(parent.winfo_toplevel())
        self.window.grab_set()

        body = ttk.Frame(self.window, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Действие:").grid(row=0, column=0, sticky="w", pady=(0, 9))
        selected = ACTION_BY_COMMAND[command.name] if command is not None else ACTION_SPECS[0]
        self.command_var = tk.StringVar(value=selected.title)
        self.command_combo = ttk.Combobox(
            body,
            state="readonly",
            width=34,
            values=tuple(spec.title for spec in ACTION_SPECS),
            textvariable=self.command_var,
        )
        self.command_combo.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(0, 9))
        self.command_combo.bind("<<ComboboxSelected>>", self._rebuild_fields)
        self.fields_frame = ttk.Frame(body)
        self.fields_frame.grid(row=1, column=0, columnspan=3, sticky="nsew")
        buttons = ttk.Frame(body)
        buttons.grid(row=2, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Отмена", command=self.window.destroy).pack(side="right")
        ttk.Button(buttons, text="Сохранить", command=self._accept).pack(side="right", padx=(0, 7))
        body.columnconfigure(1, weight=1)
        self._rebuild_fields()
        self.window.bind("<Escape>", lambda _event: self.window.destroy())
        self.window.bind("<Return>", lambda _event: self._accept())
        self.window.update_idletasks()
        x = parent.winfo_toplevel().winfo_rootx() + 110
        y = parent.winfo_toplevel().winfo_rooty() + 90
        self.window.geometry(f"+{x}+{y}")

    def _rebuild_fields(self, _event: Any = None) -> None:
        for child in self.fields_frame.winfo_children():
            child.destroy()
        spec = ACTION_BY_TITLE[self.command_var.get()]
        initial_values = (
            action_values(self.initial)
            if self.initial is not None and self.initial.name == spec.command
            else {}
        )
        self.values = {}
        for row, field in enumerate(spec.fields):
            ttk.Label(self.fields_frame, text=f"{field.label}:").grid(
                row=row, column=0, sticky="w", pady=4, padx=(0, 10)
            )
            variable = tk.StringVar(value=initial_values.get(field.key, field.default))
            self.values[field.key] = variable
            if field.kind == "choice":
                widget: ttk.Widget = ttk.Combobox(
                    self.fields_frame,
                    state="readonly",
                    values=field.choices,
                    textvariable=variable,
                    width=29,
                )
            else:
                widget = ttk.Entry(self.fields_frame, textvariable=variable, width=32)
            widget.grid(row=row, column=1, sticky="ew", pady=4)
            if field.kind == "path":
                ttk.Button(
                    self.fields_frame,
                    text="…",
                    width=3,
                    command=lambda target=variable: self._browse(target),
                ).grid(row=row, column=2, padx=(6, 0))
        self.fields_frame.columnconfigure(1, weight=1)

    def _browse(self, target: tk.StringVar) -> None:
        path = filedialog.askopenfilename(
            parent=self.window,
            title="Выберите изображение",
            filetypes=(
                ("Изображения", "*.png *.jpg *.jpeg *.bmp"),
                ("Все файлы", "*.*"),
            ),
        )
        if path:
            target.set(path)

    def _accept(self) -> None:
        spec = ACTION_BY_TITLE[self.command_var.get()]
        try:
            self.result = build_action(
                spec.command,
                {key: value.get() for key, value in self.values.items()},
            )
        except (ScriptError, ValueError) as exc:
            messagebox.showerror("Неверные параметры", str(exc), parent=self.window)
            return
        self.window.destroy()

    def show(self) -> ScriptCommand | None:
        self.window.wait_window()
        return self.result


class VisualScriptEditor(ttk.Frame):
    """Tree-based editor synchronized with MacroPilot's text language."""

    def __init__(
        self,
        master: tk.Misc,
        on_source_changed: Callable[[str], None],
        on_status: Callable[[str], None],
    ) -> None:
        super().__init__(master)
        self.on_source_changed = on_source_changed
        self.on_status = on_status
        self.nodes: list[ScriptNode] = []
        self.refs: dict[str, _TreeRef] = {}
        self.source_was_canonical = True
        self.format_warning_accepted = False
        self.enabled = True
        self._build()

    def _build(self) -> None:
        toolbar = ttk.Frame(self, padding=(8, 8))
        toolbar.pack(fill="x")
        self.action_button = ttk.Button(toolbar, text="＋ Действие", command=self.add_action)
        self.action_button.pack(side="left")
        self.condition_button = ttk.Button(toolbar, text="◇ Условие", command=self.add_condition)
        self.condition_button.pack(side="left", padx=(6, 0))
        self.repeat_button = ttk.Button(toolbar, text="↻ Повтор", command=self.add_repeat)
        self.repeat_button.pack(side="left", padx=(6, 0))
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=10)
        self.edit_button = ttk.Button(toolbar, text="Изменить", command=self.edit_selected)
        self.edit_button.pack(side="left")
        self.copy_button = ttk.Button(toolbar, text="Дублировать", command=self.duplicate_selected)
        self.copy_button.pack(side="left", padx=(6, 0))
        self.delete_button = ttk.Button(toolbar, text="Удалить", command=self.delete_selected)
        self.delete_button.pack(side="left", padx=(6, 0))
        self.up_button = ttk.Button(toolbar, text="↑", width=3, command=lambda: self.move_selected(-1))
        self.up_button.pack(side="right")
        self.down_button = ttk.Button(toolbar, text="↓", width=3, command=lambda: self.move_selected(1))
        self.down_button.pack(side="right", padx=(0, 5))

        frame = ttk.Frame(self, padding=1)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            frame,
            columns=("details",),
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0", text="Блок")
        self.tree.heading("details", text="Параметры")
        self.tree.column("#0", width=280, minwidth=190)
        self.tree.column("details", width=620, minwidth=260)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", lambda _event: self.edit_selected())
        self.tree.bind("<Delete>", lambda _event: self.delete_selected())
        self.tree.tag_configure("branch", foreground="#93a4bd")
        self.tree.tag_configure("repeat", foreground="#8fb3ff")
        self.tree.tag_configure("condition", foreground="#e7b778")
        ttk.Label(
            self,
            text=(
                "Выберите «Действия цикла», «Тогда» или «Иначе», чтобы добавить "
                "блок внутрь ветки. Двойной клик изменяет выбранный блок."
            ),
            padding=(10, 7),
        ).pack(fill="x")

    def load_source(self, source: str) -> None:
        program = parse_script(source)
        self.nodes = copy.deepcopy(list(program.nodes))
        canonical = script_nodes_to_text(self.nodes)
        self.source_was_canonical = source.strip() == canonical.strip()
        self.format_warning_accepted = False
        self.render()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        for widget in (
            self.action_button,
            self.condition_button,
            self.repeat_button,
            self.edit_button,
            self.copy_button,
            self.delete_button,
            self.up_button,
            self.down_button,
        ):
            widget.state(["!disabled"] if enabled else ["disabled"])

    def render(self) -> None:
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        self.refs.clear()
        self._render_container("", self.nodes)

    def _render_container(self, parent: str, container: list[ScriptNode]) -> None:
        for index, node in enumerate(container):
            title, details = describe_node(node)
            tag = "repeat" if isinstance(node, RepeatBlock) else "condition" if isinstance(node, IfBlock) else ""
            iid = self.tree.insert(parent, "end", text=title, values=(details,), open=True, tags=(tag,) if tag else ())
            self.refs[iid] = _TreeRef("node", container, index)
            if isinstance(node, RepeatBlock):
                branch = self.tree.insert(iid, "end", text="Действия цикла", values=(f"{len(node.body)} блок(а)",), open=True, tags=("branch",))
                self.refs[branch] = _TreeRef("branch", node.body)
                self._render_container(branch, node.body)
            elif isinstance(node, IfBlock):
                true_branch = self.tree.insert(iid, "end", text="Тогда", values=(f"{len(node.true_body)} блок(а)",), open=True, tags=("branch",))
                self.refs[true_branch] = _TreeRef("branch", node.true_body)
                self._render_container(true_branch, node.true_body)
                false_branch = self.tree.insert(iid, "end", text="Иначе", values=(f"{len(node.false_body)} блок(а)",), open=True, tags=("branch",))
                self.refs[false_branch] = _TreeRef("branch", node.false_body)
                self._render_container(false_branch, node.false_body)

    def _selected_ref(self) -> _TreeRef | None:
        selected = self.tree.selection()
        return self.refs.get(selected[0]) if selected else None

    def _insertion_target(self) -> tuple[list[ScriptNode], int]:
        ref = self._selected_ref()
        if ref is None:
            return self.nodes, len(self.nodes)
        if ref.kind == "branch":
            return ref.container, len(ref.container)
        assert ref.index is not None
        return ref.container, ref.index + 1

    def _confirm_edit(self) -> bool:
        if not self.enabled:
            return False
        if self.source_was_canonical or self.format_warning_accepted:
            return True
        accepted = messagebox.askokcancel(
            "Перейти к блокам",
            "При первом изменении блоков комментарии и ручное форматирование кода "
            "будут заменены аккуратным каноническим кодом. Сами команды сохранятся.",
            parent=self.winfo_toplevel(),
        )
        if accepted:
            self.format_warning_accepted = True
        return accepted

    def _commit(self, status: str) -> None:
        source = script_nodes_to_text(self.nodes)
        parse_script(source)
        self.source_was_canonical = True
        self.render()
        self.on_source_changed(source)
        self.on_status(status)

    def add_action(self) -> None:
        if not self._confirm_edit():
            return
        node = _ActionDialog(self).show()
        if node is None:
            return
        container, index = self._insertion_target()
        container.insert(index, node)
        self._commit("Действие добавлено")

    def insert_action(
        self,
        command: str,
        values: dict[str, str],
        status: str,
    ) -> bool:
        if not self._confirm_edit():
            return False
        node = build_action(command, values)
        container, index = self._insertion_target()
        container.insert(index, node)
        self._commit(status)
        return True

    def add_repeat(self) -> None:
        if not self._confirm_edit():
            return
        count = simpledialog.askinteger(
            "Повтор",
            "Сколько раз повторить вложенные блоки?",
            initialvalue=2,
            minvalue=1,
            maxvalue=10_000,
            parent=self.winfo_toplevel(),
        )
        if count is None:
            return
        placeholder = build_action("WAIT", {"seconds": "0"})
        node = RepeatBlock(count=count, line_no=1, body=[placeholder])
        container, index = self._insertion_target()
        container.insert(index, node)
        self._commit("Цикл добавлен · замените паузу внутри нужными действиями")

    def _ask_condition(self, current: IfBlock | None = None) -> IfBlock | None:
        window = tk.Toplevel(self)
        window.title("Условие")
        window.resizable(False, False)
        window.transient(self.winfo_toplevel())
        window.grab_set()
        body = ttk.Frame(window, padding=16)
        body.pack(fill="both", expand=True)
        kind_var = tk.StringVar(value="Текст" if current is None or current.value_kind == "text" else "Число")
        variable_var = tk.StringVar(value=current.variable if current is not None else "value")
        operator_var = tk.StringVar(value=current.operator if current is not None else "CONTAINS")
        expected_var = tk.StringVar(value=str(current.expected) if current is not None else "ready")
        ttk.Label(body, text="Тип:").grid(row=0, column=0, sticky="w", pady=4, padx=(0, 10))
        kind_combo = ttk.Combobox(body, state="readonly", values=("Текст", "Число"), textvariable=kind_var, width=24)
        kind_combo.grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(body, text="Переменная:").grid(row=1, column=0, sticky="w", pady=4, padx=(0, 10))
        ttk.Entry(body, textvariable=variable_var, width=27).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(body, text="Сравнение:").grid(row=2, column=0, sticky="w", pady=4, padx=(0, 10))
        operator_combo = ttk.Combobox(body, state="readonly", textvariable=operator_var, width=24)
        operator_combo.grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Label(body, text="Значение:").grid(row=3, column=0, sticky="w", pady=4, padx=(0, 10))
        ttk.Entry(body, textvariable=expected_var, width=27).grid(row=3, column=1, sticky="ew", pady=4)
        result: list[IfBlock] = []

        def update_operators(_event: Any = None) -> None:
            text_mode = kind_var.get() == "Текст"
            choices = ("==", "!=", "CONTAINS", "NOT_CONTAINS") if text_mode else ("==", "!=", "<", "<=", ">", ">=")
            operator_combo.configure(values=choices)
            if operator_var.get() not in choices:
                operator_var.set("CONTAINS" if text_mode else "<")

        def accept() -> None:
            try:
                result.append(
                    build_condition(
                        "text" if kind_var.get() == "Текст" else "number",
                        variable_var.get().strip(),
                        operator_var.get(),
                        expected_var.get().strip(),
                    )
                )
            except (ScriptError, ValueError) as exc:
                messagebox.showerror("Неверное условие", str(exc), parent=window)
                return
            window.destroy()

        kind_combo.bind("<<ComboboxSelected>>", update_operators)
        update_operators()
        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Отмена", command=window.destroy).pack(side="right")
        ttk.Button(buttons, text="Сохранить", command=accept).pack(side="right", padx=(0, 7))
        window.bind("<Escape>", lambda _event: window.destroy())
        window.bind("<Return>", lambda _event: accept())
        window.wait_window()
        return result[0] if result else None

    def add_condition(self) -> None:
        if not self._confirm_edit():
            return
        node = self._ask_condition()
        if node is None:
            return
        container, index = self._insertion_target()
        container.insert(index, node)
        self._commit("Условие добавлено")

    def edit_selected(self) -> None:
        ref = self._selected_ref()
        if ref is None or ref.kind != "node" or ref.index is None or not self._confirm_edit():
            return
        current = ref.container[ref.index]
        if isinstance(current, ScriptCommand):
            replacement: ScriptNode | None = _ActionDialog(self, current).show()
        elif isinstance(current, RepeatBlock):
            count = simpledialog.askinteger(
                "Повтор",
                "Сколько раз повторить вложенные блоки?",
                initialvalue=current.count,
                minvalue=1,
                maxvalue=10_000,
                parent=self.winfo_toplevel(),
            )
            replacement = None if count is None else RepeatBlock(count, current.line_no, current.body)
        else:
            changed = self._ask_condition(current)
            if changed is None:
                replacement = None
            else:
                changed.true_body = current.true_body
                changed.false_body = current.false_body
                replacement = changed
        if replacement is None:
            return
        ref.container[ref.index] = replacement
        self._commit("Блок изменён")

    def duplicate_selected(self) -> None:
        ref = self._selected_ref()
        if ref is None or ref.kind != "node" or ref.index is None or not self._confirm_edit():
            return
        ref.container.insert(ref.index + 1, copy.deepcopy(ref.container[ref.index]))
        self._commit("Блок продублирован")

    def _container_requires_item(self, target: list[ScriptNode], nodes: Iterable[ScriptNode] | None = None) -> bool:
        for node in self.nodes if nodes is None else nodes:
            if isinstance(node, RepeatBlock):
                if node.body is target:
                    return True
                if self._container_requires_item(target, node.body):
                    return True
            elif isinstance(node, IfBlock):
                if node.true_body is target:
                    return True
                if self._container_requires_item(target, node.true_body) or self._container_requires_item(target, node.false_body):
                    return True
        return False

    def delete_selected(self) -> None:
        ref = self._selected_ref()
        if ref is None or ref.kind != "node" or ref.index is None or not self._confirm_edit():
            return
        del ref.container[ref.index]
        if not ref.container and self._container_requires_item(ref.container):
            ref.container.append(build_action("WAIT", {"seconds": "0"}))
            status = "Последний обязательный блок заменён паузой 0"
        else:
            status = "Блок удалён"
        self._commit(status)

    def move_selected(self, direction: int) -> None:
        ref = self._selected_ref()
        if ref is None or ref.kind != "node" or ref.index is None or not self._confirm_edit():
            return
        destination = ref.index + direction
        if not 0 <= destination < len(ref.container):
            return
        ref.container[ref.index], ref.container[destination] = (
            ref.container[destination],
            ref.container[ref.index],
        )
        self._commit("Блок перемещён")
