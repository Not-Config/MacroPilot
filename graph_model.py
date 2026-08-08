from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from macro_core import (
    IfBlock,
    RepeatBlock,
    ScriptCommand,
    ScriptError,
    ScriptNode,
    parse_script,
    script_nodes_to_text,
)


GRAPH_FORMAT = "MacroPilot node graph"
GRAPH_VERSION = 1
MAX_GRAPH_BYTES = 16 * 1024 * 1024
MAX_GRAPH_NODES = 10_000
MAX_GRAPH_LINKS = 20_000


class GraphError(ValueError):
    """Raised when a node graph cannot be loaded, validated, or compiled."""


@dataclass(frozen=True, slots=True)
class PortSpec:
    name: str
    label: str
    data_type: str
    direction: str
    required: bool = False


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    label: str
    default: Any
    value_type: str = "text"
    choices: tuple[str, ...] = ()
    linkable: bool = True


@dataclass(frozen=True, slots=True)
class NodeSpec:
    type_name: str
    title: str
    category: str
    color: str
    fields: tuple[FieldSpec, ...] = ()
    flow_inputs: tuple[PortSpec, ...] = ()
    flow_outputs: tuple[PortSpec, ...] = ()
    data_outputs: tuple[PortSpec, ...] = ()
    command: str | None = None

    @property
    def inputs(self) -> tuple[PortSpec, ...]:
        data = tuple(
            PortSpec(item.name, item.label, item.value_type, "input")
            for item in self.fields
            if item.linkable
        )
        return self.flow_inputs + data

    @property
    def outputs(self) -> tuple[PortSpec, ...]:
        return self.flow_outputs + self.data_outputs

    def input(self, name: str) -> PortSpec | None:
        return next((port for port in self.inputs if port.name == name), None)

    def output(self, name: str) -> PortSpec | None:
        return next((port for port in self.outputs if port.name == name), None)


EXEC_IN = (PortSpec("in", "Вход", "exec", "input", True),)
EXEC_OUT = (PortSpec("out", "Дальше", "exec", "output"),)
BUTTON_CHOICES = ("left", "right", "middle")


def _f(
    name: str,
    label: str,
    default: Any,
    value_type: str = "text",
    choices: tuple[str, ...] = (),
    linkable: bool = True,
) -> FieldSpec:
    return FieldSpec(name, label, default, value_type, choices, linkable)


NODE_SPECS: tuple[NodeSpec, ...] = (
    NodeSpec(
        "start",
        "Старт",
        "Управление",
        "#3f7dd9",
        flow_outputs=(PortSpec("out", "Выполнить", "exec", "output", True),),
    ),
    NodeSpec(
        "repeat",
        "Повтор",
        "Управление",
        "#3f7dd9",
        fields=(_f("count", "Количество", 2, "integer"),),
        flow_inputs=EXEC_IN,
        flow_outputs=(
            PortSpec("body", "Цикл", "exec", "output", True),
            PortSpec("out", "После", "exec", "output"),
        ),
    ),
    NodeSpec(
        "branch_text",
        "Условие: текст",
        "Управление",
        "#d0953e",
        fields=(
            _f("value", "Текст", "", "text"),
            _f("variable", "Переменная", "text", "variable", linkable=False),
            _f(
                "operator",
                "Сравнение",
                "CONTAINS",
                "choice",
                ("==", "!=", "CONTAINS", "NOT_CONTAINS"),
                False,
            ),
            _f("expected", "Образец", "ready", "text"),
        ),
        flow_inputs=EXEC_IN,
        flow_outputs=(
            PortSpec("true", "Да", "exec", "output", True),
            PortSpec("false", "Нет", "exec", "output"),
            PortSpec("out", "После", "exec", "output"),
        ),
    ),
    NodeSpec(
        "branch_number",
        "Условие: число",
        "Управление",
        "#d0953e",
        fields=(
            _f("value", "Число", 0, "number"),
            _f("variable", "Переменная", "number", "variable", linkable=False),
            _f(
                "operator",
                "Сравнение",
                "<",
                "choice",
                ("==", "!=", "<", "<=", ">", ">="),
                False,
            ),
            _f("expected", "Порог", 30, "number"),
        ),
        flow_inputs=EXEC_IN,
        flow_outputs=(
            PortSpec("true", "Да", "exec", "output", True),
            PortSpec("false", "Нет", "exec", "output"),
            PortSpec("out", "После", "exec", "output"),
        ),
    ),
    NodeSpec(
        "wait",
        "Пауза",
        "Действия",
        "#53657d",
        fields=(_f("seconds", "Секунды", 0.5, "number"),),
        flow_inputs=EXEC_IN,
        flow_outputs=EXEC_OUT,
        command="WAIT",
    ),
    NodeSpec(
        "move",
        "Переместить мышь",
        "Мышь",
        "#cc7045",
        fields=(
            _f("x", "X", 500, "integer"),
            _f("y", "Y", 350, "integer"),
            _f("duration", "Длительность", 0, "number"),
        ),
        flow_inputs=EXEC_IN,
        flow_outputs=EXEC_OUT,
        command="MOVE",
    ),
    NodeSpec(
        "move_by",
        "Сдвинуть мышь",
        "Мышь",
        "#cc7045",
        fields=(
            _f("x", "По X", 50, "integer"),
            _f("y", "По Y", 0, "integer"),
            _f("duration", "Длительность", 0, "number"),
        ),
        flow_inputs=EXEC_IN,
        flow_outputs=EXEC_OUT,
        command="MOVE_BY",
    ),
    NodeSpec(
        "click",
        "Клик мыши",
        "Мышь",
        "#cc7045",
        fields=(
            _f("button", "Кнопка", "left", "choice", BUTTON_CHOICES, False),
            _f("count", "Количество", 1, "integer"),
            _f("interval", "Интервал", 0.1, "number"),
        ),
        flow_inputs=EXEC_IN,
        flow_outputs=EXEC_OUT,
        command="CLICK",
    ),
    NodeSpec(
        "click_at",
        "Клик по координате",
        "Мышь",
        "#cc7045",
        fields=(
            _f("x", "X", 500, "integer"),
            _f("y", "Y", 350, "integer"),
            _f("button", "Кнопка", "left", "choice", BUTTON_CHOICES, False),
            _f("count", "Количество", 1, "integer"),
            _f("interval", "Интервал", 0.1, "number"),
        ),
        flow_inputs=EXEC_IN,
        flow_outputs=EXEC_OUT,
        command="CLICK_AT",
    ),
    NodeSpec(
        "mouse_down",
        "Зажать мышь",
        "Мышь",
        "#cc7045",
        fields=(_f("button", "Кнопка", "left", "choice", BUTTON_CHOICES, False),),
        flow_inputs=EXEC_IN,
        flow_outputs=EXEC_OUT,
        command="DOWN",
    ),
    NodeSpec(
        "mouse_up",
        "Отпустить мышь",
        "Мышь",
        "#cc7045",
        fields=(_f("button", "Кнопка", "left", "choice", BUTTON_CHOICES, False),),
        flow_inputs=EXEC_IN,
        flow_outputs=EXEC_OUT,
        command="UP",
    ),
    NodeSpec(
        "scroll",
        "Прокрутка",
        "Мышь",
        "#cc7045",
        fields=(
            _f("x", "По X", 0, "integer"),
            _f("y", "По Y", -1, "integer"),
        ),
        flow_inputs=EXEC_IN,
        flow_outputs=EXEC_OUT,
        command="SCROLL",
    ),
    NodeSpec(
        "press",
        "Нажать клавишу",
        "Клавиатура",
        "#8c61c2",
        fields=(_f("key", "Клавиша", "enter", "key", linkable=False),),
        flow_inputs=EXEC_IN,
        flow_outputs=EXEC_OUT,
        command="PRESS",
    ),
    NodeSpec(
        "key_down",
        "Зажать клавишу",
        "Клавиатура",
        "#8c61c2",
        fields=(_f("key", "Клавиша", "w", "key", linkable=False),),
        flow_inputs=EXEC_IN,
        flow_outputs=EXEC_OUT,
        command="KEY_DOWN",
    ),
    NodeSpec(
        "key_up",
        "Отпустить клавишу",
        "Клавиатура",
        "#8c61c2",
        fields=(_f("key", "Клавиша", "w", "key", linkable=False),),
        flow_inputs=EXEC_IN,
        flow_outputs=EXEC_OUT,
        command="KEY_UP",
    ),
    NodeSpec(
        "hotkey",
        "Сочетание клавиш",
        "Клавиатура",
        "#8c61c2",
        fields=(_f("keys", "Клавиши через +", "ctrl+c", "keys", linkable=False),),
        flow_inputs=EXEC_IN,
        flow_outputs=EXEC_OUT,
        command="HOTKEY",
    ),
    NodeSpec(
        "type_text",
        "Напечатать текст",
        "Клавиатура",
        "#8c61c2",
        fields=(
            _f("text", "Текст", "Привет из MacroPilot!", "text"),
            _f("interval", "Интервал", 0.03, "number"),
        ),
        flow_inputs=EXEC_IN,
        flow_outputs=EXEC_OUT,
        command="TYPE",
    ),
    NodeSpec(
        "wait_image",
        "Ждать изображение",
        "Экран",
        "#2f9a84",
        fields=(
            _f("path", "Файл", "image.png", "path", linkable=False),
            _f("timeout", "Тайм-аут", 30, "number"),
            _f("confidence", "Сходство", 0.9, "number"),
        ),
        flow_inputs=EXEC_IN,
        flow_outputs=EXEC_OUT,
        command="WAIT_IMAGE",
    ),
    NodeSpec(
        "click_image",
        "Кликнуть изображение",
        "Экран",
        "#2f9a84",
        fields=(
            _f("path", "Файл", "image.png", "path", linkable=False),
            _f("button", "Кнопка", "left", "choice", BUTTON_CHOICES, False),
            _f("timeout", "Тайм-аут", 30, "number"),
            _f("confidence", "Сходство", 0.9, "number"),
        ),
        flow_inputs=EXEC_IN,
        flow_outputs=EXEC_OUT,
        command="CLICK_IMAGE",
    ),
    NodeSpec(
        "ocr_text",
        "Распознать текст",
        "Экран",
        "#2f9a84",
        fields=(
            _f("variable", "Переменная", "text", "variable", linkable=False),
            _f("x", "X", 0, "integer"),
            _f("y", "Y", 0, "integer"),
            _f("width", "Ширина", 300, "integer"),
            _f("height", "Высота", 80, "integer"),
            _f("language", "Язык", "auto", "language", linkable=False),
        ),
        flow_inputs=EXEC_IN,
        flow_outputs=EXEC_OUT,
        data_outputs=(PortSpec("value", "Текст", "text", "output"),),
        command="OCR_TEXT",
    ),
    NodeSpec(
        "ocr_number",
        "Распознать число",
        "Экран",
        "#2f9a84",
        fields=(
            _f("variable", "Переменная", "number", "variable", linkable=False),
            _f("x", "X", 0, "integer"),
            _f("y", "Y", 0, "integer"),
            _f("width", "Ширина", 180, "integer"),
            _f("height", "Высота", 50, "integer"),
            _f("language", "Язык", "auto", "language", linkable=False),
        ),
        flow_inputs=EXEC_IN,
        flow_outputs=EXEC_OUT,
        data_outputs=(PortSpec("value", "Число", "number", "output"),),
        command="OCR_NUMBER",
    ),
    NodeSpec(
        "number_value",
        "Число",
        "Значения",
        "#607588",
        fields=(_f("value", "Значение", 0, "number", linkable=False),),
        data_outputs=(PortSpec("value", "Число", "number", "output"),),
    ),
    NodeSpec(
        "text_value",
        "Текст",
        "Значения",
        "#607588",
        fields=(_f("value", "Значение", "text", "text", linkable=False),),
        data_outputs=(PortSpec("value", "Текст", "text", "output"),),
    ),
)

NODE_SPEC_BY_TYPE = {spec.type_name: spec for spec in NODE_SPECS}
COMMAND_TO_NODE_TYPE = {
    spec.command: spec.type_name for spec in NODE_SPECS if spec.command is not None
}


@dataclass(slots=True)
class GraphNode:
    id: str
    type: str
    x: float = 0.0
    y: float = 0.0
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def spec(self) -> NodeSpec:
        try:
            return NODE_SPEC_BY_TYPE[self.type]
        except KeyError as exc:
            raise GraphError(f"Узел {self.id}: неизвестный тип {self.type!r}") from exc

    def value(self, name: str) -> Any:
        field_spec = next((item for item in self.spec.fields if item.name == name), None)
        if field_spec is None:
            raise GraphError(f"Узел {self.spec.title}: неизвестный параметр {name!r}")
        return self.params.get(name, field_spec.default)


@dataclass(frozen=True, slots=True)
class GraphLink:
    from_node: str
    from_port: str
    to_node: str
    to_port: str


@dataclass(slots=True)
class GraphDocument:
    nodes: list[GraphNode] = field(default_factory=list)
    links: list[GraphLink] = field(default_factory=list)
    viewport_x: float = 0.0
    viewport_y: float = 0.0
    viewport_zoom: float = 1.0

    def node_map(self) -> dict[str, GraphNode]:
        return {node.id: node for node in self.nodes}

    def add_node(
        self,
        type_name: str,
        x: float = 0.0,
        y: float = 0.0,
        params: dict[str, Any] | None = None,
        node_id: str | None = None,
    ) -> GraphNode:
        if type_name not in NODE_SPEC_BY_TYPE:
            raise GraphError(f"Неизвестный тип узла: {type_name}")
        node = GraphNode(
            id=node_id or uuid.uuid4().hex,
            type=type_name,
            x=float(x),
            y=float(y),
            params=dict(params or {}),
        )
        self.nodes.append(node)
        return node

    def add_link(
        self,
        from_node: str,
        from_port: str,
        to_node: str,
        to_port: str,
    ) -> GraphLink:
        link = GraphLink(from_node, from_port, to_node, to_port)
        self.links.append(link)
        return link

    def outgoing(self, node_id: str, port: str | None = None) -> list[GraphLink]:
        return [
            link
            for link in self.links
            if link.from_node == node_id and (port is None or link.from_port == port)
        ]

    def incoming(self, node_id: str, port: str | None = None) -> list[GraphLink]:
        return [
            link
            for link in self.links
            if link.to_node == node_id and (port is None or link.to_port == port)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": GRAPH_FORMAT,
            "version": GRAPH_VERSION,
            "viewport": {
                "x": self.viewport_x,
                "y": self.viewport_y,
                "zoom": self.viewport_zoom,
            },
            "nodes": [
                {
                    "id": node.id,
                    "type": node.type,
                    "x": node.x,
                    "y": node.y,
                    "params": node.params,
                }
                for node in self.nodes
            ],
            "links": [
                {
                    "from": {"node": link.from_node, "port": link.from_port},
                    "to": {"node": link.to_node, "port": link.to_port},
                }
                for link in self.links
            ],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> GraphDocument:
        if not isinstance(raw, dict):
            raise GraphError("Файл графа должен содержать JSON-объект")
        if raw.get("format") != GRAPH_FORMAT:
            raise GraphError("Это не файл графа MacroPilot")
        if raw.get("version") != GRAPH_VERSION:
            raise GraphError(f"Неподдерживаемая версия графа: {raw.get('version')!r}")
        raw_nodes = raw.get("nodes")
        raw_links = raw.get("links")
        if not isinstance(raw_nodes, list) or not isinstance(raw_links, list):
            raise GraphError("В файле отсутствуют списки nodes или links")
        if len(raw_nodes) > MAX_GRAPH_NODES or len(raw_links) > MAX_GRAPH_LINKS:
            raise GraphError("Граф превышает безопасный лимит размера")
        document = cls()
        for index, item in enumerate(raw_nodes, start=1):
            if not isinstance(item, dict):
                raise GraphError(f"Узел {index}: ожидался объект")
            params = item.get("params", {})
            if not isinstance(params, dict):
                raise GraphError(f"Узел {index}: params должен быть объектом")
            try:
                document.add_node(
                    str(item["type"]),
                    float(item.get("x", 0.0)),
                    float(item.get("y", 0.0)),
                    params,
                    str(item["id"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise GraphError(f"Узел {index}: неверные поля") from exc
        for index, item in enumerate(raw_links, start=1):
            try:
                source = item["from"]
                target = item["to"]
                document.add_link(
                    str(source["node"]),
                    str(source["port"]),
                    str(target["node"]),
                    str(target["port"]),
                )
            except (KeyError, TypeError) as exc:
                raise GraphError(f"Связь {index}: неверные поля") from exc
        viewport = raw.get("viewport", {})
        if isinstance(viewport, dict):
            try:
                document.viewport_x = float(viewport.get("x", 0.0))
                document.viewport_y = float(viewport.get("y", 0.0))
                document.viewport_zoom = float(viewport.get("zoom", 1.0))
            except (TypeError, ValueError) as exc:
                raise GraphError("Неверные параметры viewport") from exc
        document.validate()
        return document

    def validate(self) -> None:
        if len(self.nodes) > MAX_GRAPH_NODES or len(self.links) > MAX_GRAPH_LINKS:
            raise GraphError("Граф превышает безопасный лимит размера")
        node_map = self.node_map()
        if any(not node.id for node in self.nodes):
            raise GraphError("Идентификатор узла не может быть пустым")
        if any(len(node.id) > 256 for node in self.nodes):
            raise GraphError("Идентификатор узла слишком длинный")
        if any(not math.isfinite(node.x) or not math.isfinite(node.y) for node in self.nodes):
            raise GraphError("Координаты узлов должны быть конечными числами")
        if len(node_map) != len(self.nodes):
            raise GraphError("Идентификаторы узлов должны быть уникальными")
        starts = [node for node in self.nodes if node.type == "start"]
        if len(starts) != 1:
            raise GraphError("В графе должен быть ровно один узел «Старт»")
        seen_links: set[GraphLink] = set()
        incoming_ports: set[tuple[str, str]] = set()
        outgoing_exec: set[tuple[str, str]] = set()
        for link in self.links:
            if link in seen_links:
                raise GraphError("Граф содержит повторяющуюся связь")
            seen_links.add(link)
            source = node_map.get(link.from_node)
            target = node_map.get(link.to_node)
            if source is None or target is None:
                raise GraphError("Связь ссылается на отсутствующий узел")
            source_port = source.spec.output(link.from_port)
            target_port = target.spec.input(link.to_port)
            if source_port is None:
                raise GraphError(
                    f"Узел {source.spec.title}: нет выхода {link.from_port!r}"
                )
            if target_port is None:
                raise GraphError(
                    f"Узел {target.spec.title}: нет входа {link.to_port!r}"
                )
            if not _types_compatible(source_port.data_type, target_port.data_type):
                raise GraphError(
                    f"Нельзя соединить {source_port.data_type} с {target_port.data_type}"
                )
            incoming_key = (link.to_node, link.to_port)
            if incoming_key in incoming_ports:
                raise GraphError("Один вход не может иметь несколько связей")
            incoming_ports.add(incoming_key)
            if source_port.data_type == "exec":
                outgoing_key = (link.from_node, link.from_port)
                if outgoing_key in outgoing_exec:
                    raise GraphError("Один выход выполнения ведёт только к одному блоку")
                outgoing_exec.add(outgoing_key)

        start = starts[0]
        if not self.outgoing(start.id, "out"):
            raise GraphError("Подключите действие к выходу узла «Старт»")
        for node in self.nodes:
            for port in node.spec.flow_outputs:
                if port.required and not self.outgoing(node.id, port.name):
                    raise GraphError(
                        f"Узел {node.spec.title}: подключите выход «{port.label}»"
                    )

        reachable: set[str] = set()
        visiting: set[str] = set()

        def walk(node_id: str) -> None:
            if node_id in visiting:
                raise GraphError("Обычные циклические связи запрещены; используйте «Повтор»")
            if node_id in reachable:
                return
            visiting.add(node_id)
            reachable.add(node_id)
            for link in self.outgoing(node_id):
                source_port = node_map[node_id].spec.output(link.from_port)
                if source_port is not None and source_port.data_type == "exec":
                    walk(link.to_node)
            visiting.remove(node_id)

        walk(start.id)
        orphan_flow = [
            node.spec.title
            for node in self.nodes
            if node.spec.flow_inputs and node.id not in reachable
        ]
        if orphan_flow:
            raise GraphError(
                "Не подключены к потоку выполнения: " + ", ".join(orphan_flow[:5])
            )
        if not math.isfinite(self.viewport_x) or not math.isfinite(self.viewport_y):
            raise GraphError("Координаты камеры должны быть конечными числами")
        if not math.isfinite(self.viewport_zoom) or not 0.1 <= self.viewport_zoom <= 4.0:
            raise GraphError("Масштаб графа должен быть от 0.1 до 4")


def _types_compatible(source: str, target: str) -> bool:
    if source == target:
        return True
    return source == "integer" and target == "number"


def new_graph() -> GraphDocument:
    graph = GraphDocument()
    start = graph.add_node("start", 0, 0)
    wait = graph.add_node("wait", 300, 0, {"seconds": 0})
    graph.add_link(start.id, "out", wait.id, "in")
    return graph


def save_graph(path: str | os.PathLike[str], graph: GraphDocument) -> None:
    graph.validate()
    destination = Path(path)
    try:
        payload = json.dumps(
            graph.to_dict(),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError) as exc:
        raise GraphError(f"Граф содержит несохраняемое значение: {exc}") from exc
    if len(payload.encode("utf-8")) > MAX_GRAPH_BYTES:
        raise GraphError("Файл графа превышает безопасный лимит 16 МБ")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def load_graph(path: str | os.PathLike[str]) -> GraphDocument:
    source = Path(path)
    try:
        if source.stat().st_size > MAX_GRAPH_BYTES:
            raise GraphError("Файл графа превышает безопасный лимит 16 МБ")
        raw = json.loads(source.read_text(encoding="utf-8"))
    except GraphError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GraphError(f"Не удалось прочитать граф: {exc}") from exc
    return GraphDocument.from_dict(raw)


@dataclass(frozen=True, slots=True)
class _VariableRef:
    name: str
    value_type: str
    source_node: str


class _GraphCompiler:
    def __init__(self, graph: GraphDocument) -> None:
        graph.validate()
        self.graph = graph
        self.nodes = graph.node_map()
        self.compiled_flow: set[str] = set()
        self.node_for_script_node: dict[int, str] = {}

    def compile(self) -> str:
        source, _line_map = self.compile_with_line_map()
        return source

    def compile_with_line_map(self) -> tuple[str, dict[int, str]]:
        start = next(node for node in self.graph.nodes if node.type == "start")
        first = self._flow_target(start.id, "out")
        nodes, _available = self._compile_chain(first, set())
        source = script_nodes_to_text(nodes)
        parsed = parse_script(source)
        line_map: dict[int, str] = {}

        def collect(
            original_items: Iterable[ScriptNode],
            parsed_items: Iterable[ScriptNode],
        ) -> None:
            for original, rebuilt in zip(original_items, parsed_items):
                node_id = self.node_for_script_node.get(id(original))
                if node_id is not None:
                    line_map[rebuilt.line_no] = node_id
                if isinstance(original, RepeatBlock) and isinstance(rebuilt, RepeatBlock):
                    collect(original.body, rebuilt.body)
                elif isinstance(original, IfBlock) and isinstance(rebuilt, IfBlock):
                    collect(original.true_body, rebuilt.true_body)
                    collect(original.false_body, rebuilt.false_body)

        collect(nodes, parsed.nodes)
        return source, line_map

    def _flow_target(self, node_id: str, port: str) -> str | None:
        links = self.graph.outgoing(node_id, port)
        return links[0].to_node if links else None

    def _compile_chain(
        self,
        node_id: str | None,
        available: set[str],
    ) -> tuple[list[ScriptNode], set[str]]:
        result: list[ScriptNode] = []
        current = node_id
        local_available = set(available)
        while current is not None:
            if current in self.compiled_flow:
                raise GraphError(
                    "Один блок выполнения нельзя использовать в нескольких ветках"
                )
            self.compiled_flow.add(current)
            node = self.nodes[current]
            if node.type == "repeat":
                count = int(self._resolve_static(node, "count"))
                body_start = self._flow_target(node.id, "body")
                body, body_available = self._compile_chain(
                    body_start,
                    set(local_available),
                )
                block = RepeatBlock(count=count, line_no=1, body=body)
                self.node_for_script_node[id(block)] = node.id
                result.append(block)
                local_available.update(body_available)
                current = self._flow_target(node.id, "out")
                continue
            if node.type in {"branch_text", "branch_number"}:
                value_kind = "text" if node.type == "branch_text" else "number"
                variable = str(node.value("variable"))
                incoming = self.graph.incoming(node.id, "value")
                if incoming:
                    resolved = self._resolve_link(incoming[0], local_available)
                    if isinstance(resolved, _VariableRef):
                        variable = resolved.name
                    else:
                        raise GraphError(
                            f"{node.spec.title}: вход значения должен идти от OCR"
                        )
                expected = self._resolve_static(node, "expected")
                if value_kind == "number":
                    expected = float(expected)
                true_nodes, true_available = self._compile_chain(
                    self._flow_target(node.id, "true"),
                    set(local_available),
                )
                false_nodes, false_available = self._compile_chain(
                    self._flow_target(node.id, "false"),
                    set(local_available),
                )
                block = IfBlock(
                    value_kind=value_kind,
                    variable=variable,
                    operator=str(node.value("operator")),
                    expected=expected,
                    line_no=1,
                    true_body=true_nodes,
                    false_body=false_nodes,
                )
                self.node_for_script_node[id(block)] = node.id
                result.append(block)
                local_available.update(true_available & false_available)
                current = self._flow_target(node.id, "out")
                continue
            if node.spec.command is None:
                raise GraphError(f"Узел {node.spec.title} нельзя выполнить в потоке")
            command = self._command(node, local_available)
            self.node_for_script_node[id(command)] = node.id
            result.append(command)
            if node.type in {"ocr_text", "ocr_number"}:
                local_available.add(node.id)
            current = self._flow_target(node.id, "out")
        return result, local_available

    def _resolve_link(
        self,
        link: GraphLink,
        available: set[str],
    ) -> Any:
        source = self.nodes[link.from_node]
        if source.type in {"number_value", "text_value"}:
            return source.value("value")
        if source.type in {"ocr_text", "ocr_number"} and link.from_port == "value":
            if source.id not in available:
                raise GraphError(
                    f"Сначала выполните узел {source.spec.title}, затем используйте его результат"
                )
            return _VariableRef(
                str(source.value("variable")),
                "text" if source.type == "ocr_text" else "number",
                source.id,
            )
        raise GraphError(f"Узел {source.spec.title} не предоставляет постоянное значение")

    def _resolve_static(self, node: GraphNode, field_name: str) -> Any:
        incoming = self.graph.incoming(node.id, field_name)
        if not incoming:
            return node.value(field_name)
        value = self._resolve_link(incoming[0], set())
        if isinstance(value, _VariableRef):
            raise GraphError(
                f"{node.spec.title}: динамическое значение нельзя использовать в этом поле"
            )
        return value

    def _command(self, node: GraphNode, available: set[str]) -> ScriptCommand:
        values: dict[str, Any] = {}
        for field_spec in node.spec.fields:
            incoming = self.graph.incoming(node.id, field_spec.name)
            if incoming:
                resolved = self._resolve_link(incoming[0], available)
                if isinstance(resolved, _VariableRef):
                    if node.type == "type_text" and field_spec.name == "text":
                        resolved = "${" + resolved.name + "}"
                    else:
                        raise GraphError(
                            f"{node.spec.title}: динамический вход «{field_spec.label}» пока не поддерживается"
                        )
                values[field_spec.name] = resolved
            else:
                values[field_spec.name] = node.value(field_spec.name)
        line = _command_source(node.spec.command or "", values)
        try:
            parsed = parse_script(line).nodes[0]
        except ScriptError as exc:
            raise GraphError(f"{node.spec.title}: {exc.message}") from exc
        if not isinstance(parsed, ScriptCommand):
            raise GraphError(f"{node.spec.title}: ожидалась команда")
        return parsed


def _quoted(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _command_source(command: str, values: dict[str, Any]) -> str:
    if command == "WAIT":
        args = [values["seconds"]]
    elif command in {"MOVE", "MOVE_BY"}:
        args = [values["x"], values["y"], values["duration"]]
    elif command == "CLICK":
        args = [values["button"], values["count"], values["interval"]]
    elif command == "CLICK_AT":
        args = [
            values["x"],
            values["y"],
            values["button"],
            values["count"],
            values["interval"],
        ]
    elif command in {"DOWN", "UP"}:
        args = [values["button"]]
    elif command == "SCROLL":
        args = [values["x"], values["y"]]
    elif command in {"PRESS", "KEY_DOWN", "KEY_UP"}:
        return f"{command} {_quoted(values['key'])}"
    elif command == "HOTKEY":
        keys = [item for item in str(values["keys"]).replace("+", " ").split() if item]
        return command + " " + " ".join(_quoted(item) for item in keys)
    elif command == "TYPE":
        return f"TYPE {_quoted(values['text'])} {values['interval']}"
    elif command == "WAIT_IMAGE":
        return (
            f"WAIT_IMAGE {_quoted(values['path'])} "
            f"{values['timeout']} {values['confidence']}"
        )
    elif command == "CLICK_IMAGE":
        return (
            f"CLICK_IMAGE {_quoted(values['path'])} {values['button']} "
            f"{values['timeout']} {values['confidence']}"
        )
    elif command in {"OCR_TEXT", "OCR_NUMBER"}:
        return (
            f"{command} {values['variable']} {values['x']} {values['y']} "
            f"{values['width']} {values['height']} {_quoted(values['language'])}"
        )
    else:
        raise GraphError(f"Неизвестная команда узла: {command}")
    return command + " " + " ".join(str(value) for value in args)


def graph_to_script(graph: GraphDocument) -> str:
    return _GraphCompiler(graph).compile()


def graph_to_script_with_line_map(
    graph: GraphDocument,
) -> tuple[str, dict[int, str]]:
    """Compile a graph and map generated source lines back to graph nodes."""

    return _GraphCompiler(graph).compile_with_line_map()


def _command_params(command: ScriptCommand) -> dict[str, Any]:
    names: dict[str, tuple[str, ...]] = {
        "WAIT": ("seconds",),
        "MOVE": ("x", "y", "duration"),
        "MOVE_BY": ("x", "y", "duration"),
        "CLICK": ("button", "count", "interval"),
        "CLICK_AT": ("x", "y", "button", "count", "interval"),
        "WAIT_IMAGE": ("path", "timeout", "confidence"),
        "CLICK_IMAGE": ("path", "button", "timeout", "confidence"),
        "OCR_TEXT": ("variable", "x", "y", "width", "height", "language"),
        "OCR_NUMBER": ("variable", "x", "y", "width", "height", "language"),
        "DOWN": ("button",),
        "UP": ("button",),
        "SCROLL": ("x", "y"),
        "PRESS": ("key",),
        "KEY_DOWN": ("key",),
        "KEY_UP": ("key",),
        "TYPE": ("text", "interval"),
    }
    if command.name == "HOTKEY":
        return {"keys": "+".join(str(item) for item in command.args)}
    keys = names.get(command.name)
    if keys is None:
        raise GraphError(f"Команда {command.name} не поддерживается графом")
    return dict(zip(keys, command.args))


def script_to_graph(source: str) -> GraphDocument:
    program = parse_script(source)
    graph = GraphDocument()
    start = graph.add_node("start", 0, 0)
    variable_sources: dict[str, GraphNode] = {}

    def append_nodes(
        items: Iterable[ScriptNode],
        source_node: GraphNode,
        source_port: str,
        x: float,
        y: float,
        depth: int = 0,
    ) -> tuple[GraphNode, str, float]:
        previous = source_node
        previous_port = source_port
        cursor_x = x
        for item in items:
            if isinstance(item, RepeatBlock):
                node = graph.add_node("repeat", cursor_x, y, {"count": item.count})
                graph.add_link(previous.id, previous_port, node.id, "in")
                append_nodes(
                    item.body,
                    node,
                    "body",
                    cursor_x + 300,
                    y + 220 + depth * 30,
                    depth + 1,
                )
                previous, previous_port = node, "out"
            elif isinstance(item, IfBlock):
                node_type = "branch_text" if item.value_kind == "text" else "branch_number"
                node = graph.add_node(
                    node_type,
                    cursor_x,
                    y,
                    {
                        "variable": item.variable,
                        "operator": item.operator,
                        "expected": item.expected,
                    },
                )
                graph.add_link(previous.id, previous_port, node.id, "in")
                producer = variable_sources.get(item.variable)
                if producer is not None:
                    graph.add_link(producer.id, "value", node.id, "value")
                append_nodes(
                    item.true_body,
                    node,
                    "true",
                    cursor_x + 300,
                    y + 190,
                    depth + 1,
                )
                if item.false_body:
                    append_nodes(
                        item.false_body,
                        node,
                        "false",
                        cursor_x + 300,
                        y - 190,
                        depth + 1,
                    )
                previous, previous_port = node, "out"
            else:
                type_name = COMMAND_TO_NODE_TYPE.get(item.name)
                if type_name is None:
                    raise GraphError(f"Команда {item.name} не поддерживается графом")
                node = graph.add_node(
                    type_name,
                    cursor_x,
                    y,
                    _command_params(item),
                )
                graph.add_link(previous.id, previous_port, node.id, "in")
                if type_name in {"ocr_text", "ocr_number"}:
                    variable_sources[str(node.value("variable"))] = node
                previous, previous_port = node, "out"
            cursor_x += 300
        return previous, previous_port, cursor_x

    if program.nodes:
        append_nodes(program.nodes, start, "out", 300, 0)
    else:
        wait = graph.add_node("wait", 300, 0, {"seconds": 0})
        graph.add_link(start.id, "out", wait.id, "in")
    graph.validate()
    return graph
