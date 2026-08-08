from __future__ import annotations

import copy
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QByteArray,
    QMimeData,
    QPoint,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QDrag,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from graph_model import (
    NODE_SPECS,
    FieldSpec,
    GraphDocument,
    GraphError,
    GraphLink,
    GraphNode,
    NodeSpec,
    PortSpec,
    graph_to_script,
    load_graph,
    new_graph,
    save_graph,
    script_to_graph,
)


NODE_MIME_TYPE = "application/x-macropilot-node"
CLIPBOARD_FORMAT = "MacroPilot graph selection"
NODE_WIDTH = 248.0
NODE_HEADER_HEIGHT = 38.0
NODE_ROW_HEIGHT = 25.0
NODE_PADDING = 12.0
PORT_RADIUS = 6.0

TYPE_COLORS = {
    "exec": QColor("#e9edf2"),
    "number": QColor("#8bd66b"),
    "integer": QColor("#8bd66b"),
    "text": QColor("#e5a45b"),
    "choice": QColor("#c481e3"),
    "key": QColor("#c481e3"),
    "keys": QColor("#c481e3"),
    "path": QColor("#61b8e8"),
    "variable": QColor("#e5a45b"),
    "language": QColor("#61b8e8"),
}


def _port_color(data_type: str) -> QColor:
    return QColor(TYPE_COLORS.get(data_type, QColor("#aeb7c4")))


def _types_compatible(source: str, target: str) -> bool:
    return source == target or (source == "integer" and target == "number")


def _short_value(value: Any, limit: int = 20) -> str:
    if isinstance(value, float):
        text = f"{value:.6f}".rstrip("0").rstrip(".")
    else:
        text = str(value)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


class PortItem(QGraphicsItem):
    def __init__(
        self,
        node_item: NodeItem,
        port: PortSpec,
        is_output: bool,
    ) -> None:
        super().__init__(node_item)
        self.node_item = node_item
        self.port = port
        self.is_output = is_output
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip(
            f"{port.label} · {port.data_type} · "
            + ("выход" if is_output else "вход")
        )
        self.setZValue(4)

    def boundingRect(self) -> QRectF:
        radius = PORT_RADIUS + 4.0
        return QRectF(-radius, -radius, radius * 2, radius * 2)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addEllipse(self.boundingRect())
        return path

    def paint(self, painter: QPainter, option: Any, widget: QWidget | None = None) -> None:
        del option, widget
        color = _port_color(self.port.data_type)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#15191f"), 2.0))
        painter.setBrush(color if self.isUnderMouse() else color.darker(112))
        radius = PORT_RADIUS + (1.0 if self.isUnderMouse() else 0.0)
        painter.drawEllipse(QPointF(0, 0), radius, radius)
        if self.port.data_type == "exec":
            painter.setBrush(QColor("#252b34"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(0, 0), 2.4, 2.4)

    def scene_center(self) -> QPointF:
        return self.mapToScene(QPointF(0, 0))

    def hoverEnterEvent(self, event: Any) -> None:
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event: Any) -> None:
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            view = self.scene().views()[0] if self.scene() and self.scene().views() else None
            if isinstance(view, GraphView):
                view.begin_connection(self)
                event.accept()
                return
        super().mousePressEvent(event)


class EdgeItem(QGraphicsPathItem):
    def __init__(
        self,
        link: GraphLink,
        source: PortItem,
        target: PortItem,
    ) -> None:
        super().__init__()
        self.link = link
        self.source = source
        self.target = target
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setZValue(-1)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_path()

    @staticmethod
    def path_between(start: QPointF, end: QPointF) -> QPainterPath:
        distance = abs(end.x() - start.x())
        handle = max(70.0, min(260.0, distance * 0.5))
        direction = 1.0 if end.x() >= start.x() else -1.0
        path = QPainterPath(start)
        path.cubicTo(
            QPointF(start.x() + handle * direction, start.y()),
            QPointF(end.x() - handle * direction, end.y()),
            end,
        )
        return path

    def update_path(self) -> None:
        self.setPath(self.path_between(self.source.scene_center(), self.target.scene_center()))

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(14.0)
        return stroker.createStroke(self.path())

    def paint(self, painter: QPainter, option: Any, widget: QWidget | None = None) -> None:
        del option, widget
        color = _port_color(self.source.port.data_type)
        if self.isSelected():
            color = color.lighter(145)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(color, 4.2 if self.isSelected() else 2.6))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self.path())


class NodeItem(QGraphicsObject):
    moved = Signal(str)
    activated = Signal(str)

    def __init__(self, node: GraphNode) -> None:
        super().__init__()
        self.node = node
        self.spec = node.spec
        self.input_ports: dict[str, PortItem] = {}
        self.output_ports: dict[str, PortItem] = {}
        self.edges: set[EdgeItem] = set()
        self.active = False
        base_rows = max(1, len(self.spec.inputs), len(self.spec.outputs))
        self._standalone_field_rows: dict[str, int] = {}
        row_count = base_rows
        for field_spec in self.spec.fields:
            if not field_spec.linkable:
                self._standalone_field_rows[field_spec.name] = row_count
                row_count += 1
        self.height = NODE_HEADER_HEIGHT + NODE_PADDING * 2 + row_count * NODE_ROW_HEIGHT
        self.setPos(node.x, node.y)
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setZValue(1)
        self._create_ports()

    def _create_ports(self) -> None:
        input_rows = {port.name: index for index, port in enumerate(self.spec.inputs)}
        output_rows = {port.name: index for index, port in enumerate(self.spec.outputs)}
        for port in self.spec.inputs:
            item = PortItem(self, port, False)
            item.setPos(0, self._row_y(input_rows[port.name]))
            self.input_ports[port.name] = item
        for port in self.spec.outputs:
            item = PortItem(self, port, True)
            item.setPos(NODE_WIDTH, self._row_y(output_rows[port.name]))
            self.output_ports[port.name] = item

    @staticmethod
    def _row_y(index: int) -> float:
        return NODE_HEADER_HEIGHT + NODE_PADDING + NODE_ROW_HEIGHT * (index + 0.5)

    def boundingRect(self) -> QRectF:
        return QRectF(-1, -1, NODE_WIDTH + 2, self.height + 2)

    def paint(self, painter: QPainter, option: Any, widget: QWidget | None = None) -> None:
        del option, widget
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QRectF(0, 0, NODE_WIDTH, self.height)
        painter.setPen(QPen(QColor("#f4b860") if self.isSelected() else QColor("#11151a"), 2.4))
        painter.setBrush(QColor("#252b34"))
        painter.drawRoundedRect(body, 8, 8)

        header_path = QPainterPath()
        header_path.addRoundedRect(QRectF(0, 0, NODE_WIDTH, NODE_HEADER_HEIGHT + 7), 8, 8)
        header_path.addRect(QRectF(0, NODE_HEADER_HEIGHT - 7, NODE_WIDTH, 14))
        header = QColor(self.spec.color)
        if self.active:
            header = header.lighter(145)
        painter.fillPath(header_path, header)

        painter.setPen(QColor("#f6f8fa"))
        title_font = painter.font()
        title_font.setBold(True)
        title_font.setPointSizeF(10.0)
        painter.setFont(title_font)
        painter.drawText(
            QRectF(13, 0, NODE_WIDTH - 26, NODE_HEADER_HEIGHT),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self.spec.title,
        )

        body_font = painter.font()
        body_font.setBold(False)
        body_font.setPointSizeF(8.6)
        painter.setFont(body_font)
        painter.setPen(QColor("#d8dee8"))
        field_by_name = {field.name: field for field in self.spec.fields}
        linked_names = {link.to_port for link in self._model_incoming_links()}
        for index, port in enumerate(self.spec.inputs):
            y = self._row_y(index)
            label = port.label
            field_spec = field_by_name.get(port.name)
            if field_spec is not None:
                value = (
                    "связано"
                    if field_spec.name in linked_names
                    else _short_value(self.node.value(field_spec.name), 14)
                )
                label = f"{field_spec.label}: {value}"
            painter.drawText(
                QRectF(13, y - NODE_ROW_HEIGHT / 2, 143, NODE_ROW_HEIGHT),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                label,
            )
        for index, port in enumerate(self.spec.outputs):
            y = self._row_y(index)
            painter.drawText(
                QRectF(NODE_WIDTH - 116, y - NODE_ROW_HEIGHT / 2, 103, NODE_ROW_HEIGHT),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                port.label,
            )

        painter.setPen(QColor("#aeb7c4"))
        for field_spec in self.spec.fields:
            row = self._standalone_field_rows.get(field_spec.name)
            if row is None:
                continue
            value = _short_value(self.node.value(field_spec.name), 28)
            y = self._row_y(row)
            painter.drawText(
                QRectF(13, y - NODE_ROW_HEIGHT / 2, NODE_WIDTH - 26, NODE_ROW_HEIGHT),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                f"{field_spec.label}: {value}",
            )

        if self.active:
            painter.setPen(QPen(QColor("#86e58a"), 3.2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(body.adjusted(3, 3, -3, -3), 7, 7)

    def _model_incoming_links(self) -> list[GraphLink]:
        scene = self.scene()
        if isinstance(scene, GraphScene):
            return scene.document.incoming(self.node.id)
        return []

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value: Any) -> Any:
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            point = self.pos()
            self.node.x = point.x()
            self.node.y = point.y()
            for edge in tuple(self.edges):
                edge.update_path()
            scene = self.scene()
            if isinstance(scene, GraphScene) and not scene.loading:
                scene.mark_changed()
            self.moved.emit(self.node.id)
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.setZValue(2 if bool(value) else 1)
        return super().itemChange(change, value)

    def mousePressEvent(self, event: Any) -> None:
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:
        self.activated.emit(self.node.id)
        super().mouseDoubleClickEvent(event)

    def set_active(self, active: bool) -> None:
        if self.active == active:
            return
        self.active = active
        self.update()


class GraphScene(QGraphicsScene):
    document_changed = Signal()
    connection_rejected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.document = new_graph()
        self.node_items: dict[str, NodeItem] = {}
        self.edge_items: list[EdgeItem] = []
        self.loading = False
        self.setSceneRect(-5_000_000, -5_000_000, 10_000_000, 10_000_000)
        self.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.BspTreeIndex)
        self.set_document(self.document)

    def set_document(self, document: GraphDocument) -> None:
        self.loading = True
        try:
            self.clear()
            self.document = document
            self.node_items = {}
            self.edge_items = []
            for node in document.nodes:
                self._add_node_item(node)
            for link in document.links:
                self._add_edge_item(link)
        finally:
            self.loading = False
        self.document_changed.emit()

    def _add_node_item(self, node: GraphNode) -> NodeItem:
        item = NodeItem(node)
        self.addItem(item)
        self.node_items[node.id] = item
        return item

    def _add_edge_item(self, link: GraphLink) -> EdgeItem:
        source_node = self.node_items[link.from_node]
        target_node = self.node_items[link.to_node]
        source = source_node.output_ports[link.from_port]
        target = target_node.input_ports[link.to_port]
        edge = EdgeItem(link, source, target)
        self.addItem(edge)
        source_node.edges.add(edge)
        target_node.edges.add(edge)
        self.edge_items.append(edge)
        return edge

    def add_node(self, type_name: str, position: QPointF) -> NodeItem:
        node = self.document.add_node(type_name, position.x(), position.y())
        item = self._add_node_item(node)
        self.clearSelection()
        item.setSelected(True)
        self.mark_changed()
        return item

    def port_at(self, scene_position: QPointF) -> PortItem | None:
        for item in self.items(scene_position):
            if isinstance(item, PortItem):
                return item
        return None

    def can_connect(self, first: PortItem, second: PortItem) -> tuple[bool, str]:
        if first is second:
            return False, "Выберите другой порт"
        if first.is_output == second.is_output:
            return False, "Соедините выход со входом"
        source, target = (first, second) if first.is_output else (second, first)
        if source.node_item is target.node_item:
            return False, "Нельзя соединить узел с самим собой"
        if not _types_compatible(source.port.data_type, target.port.data_type):
            return False, f"Типы {source.port.data_type} и {target.port.data_type} несовместимы"
        return True, ""

    def connect_ports(self, first: PortItem, second: PortItem) -> bool:
        allowed, reason = self.can_connect(first, second)
        if not allowed:
            self.connection_rejected.emit(reason)
            return False
        source, target = (first, second) if first.is_output else (second, first)
        candidate = GraphLink(
            source.node_item.node.id,
            source.port.name,
            target.node_item.node.id,
            target.port.name,
        )
        if candidate in self.document.links:
            return True

        existing = list(self.document.incoming(candidate.to_node, candidate.to_port))
        if source.port.data_type == "exec":
            existing.extend(self.document.outgoing(candidate.from_node, candidate.from_port))
        for link in list(dict.fromkeys(existing)):
            self.remove_link(link, notify=False)

        self.document.links.append(candidate)
        self._add_edge_item(candidate)
        source.node_item.update()
        target.node_item.update()
        self.mark_changed()
        return True

    def remove_link(self, link: GraphLink, notify: bool = True) -> None:
        edge = next((item for item in self.edge_items if item.link == link), None)
        if edge is not None:
            edge.source.node_item.edges.discard(edge)
            edge.target.node_item.edges.discard(edge)
            edge.source.node_item.update()
            edge.target.node_item.update()
            self.edge_items.remove(edge)
            self.removeItem(edge)
        if link in self.document.links:
            self.document.links.remove(link)
        if notify:
            self.mark_changed()

    def remove_selected(self) -> None:
        selected_edges = [item for item in self.selectedItems() if isinstance(item, EdgeItem)]
        selected_nodes = [item for item in self.selectedItems() if isinstance(item, NodeItem)]
        changed = False
        for edge in selected_edges:
            self.remove_link(edge.link, notify=False)
            changed = True
        for node_item in selected_nodes:
            if node_item.node.type == "start":
                continue
            for edge in list(node_item.edges):
                self.remove_link(edge.link, notify=False)
            if node_item.node in self.document.nodes:
                self.document.nodes.remove(node_item.node)
            self.node_items.pop(node_item.node.id, None)
            self.removeItem(node_item)
            changed = True
        if changed:
            self.mark_changed()

    def mark_changed(self) -> None:
        if not self.loading:
            self.document_changed.emit()

    def set_active_node(self, node_id: str | None) -> None:
        for item in self.node_items.values():
            item.set_active(item.node.id == node_id)


class GraphView(QGraphicsView):
    node_added = Signal(str)

    def __init__(self, scene: GraphScene, parent: QWidget | None = None) -> None:
        super().__init__(scene, parent)
        self.graph_scene = scene
        self.setAcceptDrops(True)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.BoundingRectViewportUpdate)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setBackgroundBrush(QColor("#171b21"))
        self._panning = False
        self._pan_start = QPoint()
        self._connection_port: PortItem | None = None
        self._connection_preview: QGraphicsPathItem | None = None
        self._paste_offset = 0

    def restore_viewport(self) -> None:
        document = self.graph_scene.document
        self.resetTransform()
        self.scale(document.viewport_zoom, document.viewport_zoom)
        self.centerOn(document.viewport_x, document.viewport_y)

    def remember_viewport(self) -> None:
        document = self.graph_scene.document
        center = self.mapToScene(self.viewport().rect().center())
        document.viewport_x = center.x()
        document.viewport_y = center.y()
        document.viewport_zoom = max(0.1, min(4.0, self.transform().m11()))

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        painter.fillRect(rect, QColor("#171b21"))
        minor = 24
        major = minor * 5
        left = math.floor(rect.left() / minor) * minor
        top = math.floor(rect.top() / minor) * minor
        minor_lines = []
        major_lines = []
        x = left
        while x <= rect.right():
            target = major_lines if int(x) % major == 0 else minor_lines
            target.append((QPointF(x, rect.top()), QPointF(x, rect.bottom())))
            x += minor
        y = top
        while y <= rect.bottom():
            target = major_lines if int(y) % major == 0 else minor_lines
            target.append((QPointF(rect.left(), y), QPointF(rect.right(), y)))
            y += minor
        painter.setPen(QPen(QColor("#20262e"), 1))
        for start, end in minor_lines:
            painter.drawLine(start, end)
        painter.setPen(QPen(QColor("#2a313b"), 1.2))
        for start, end in major_lines:
            painter.drawLine(start, end)

    def begin_connection(self, port: PortItem) -> None:
        self.cancel_connection()
        self._connection_port = port
        preview = QGraphicsPathItem()
        preview.setZValue(10)
        preview.setPen(QPen(_port_color(port.port.data_type).lighter(125), 2.5, Qt.PenStyle.DashLine))
        self.graph_scene.addItem(preview)
        self._connection_preview = preview

    def cancel_connection(self) -> None:
        if self._connection_preview is not None:
            self.graph_scene.removeItem(self._connection_preview)
        self._connection_preview = None
        self._connection_port = None

    def wheelEvent(self, event: Any) -> None:
        factor = 1.18 if event.angleDelta().y() > 0 else 1 / 1.18
        current = self.transform().m11()
        target = current * factor
        if 0.12 <= target <= 3.5:
            self.scale(factor, factor)
            self.remember_viewport()
        event.accept()

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.MiddleButton or (
            event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() & Qt.KeyboardModifier.AltModifier
        ):
            self._panning = True
            self._pan_start = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._panning:
            delta = event.position().toPoint() - self._pan_start
            self._pan_start = event.position().toPoint()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            self.remember_viewport()
            event.accept()
            return
        if self._connection_port is not None and self._connection_preview is not None:
            start = self._connection_port.scene_center()
            end = self.mapToScene(event.position().toPoint())
            if not self._connection_port.is_output:
                start, end = end, start
            self._connection_preview.setPath(EdgeItem.path_between(start, end))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if self._panning and event.button() in {
            Qt.MouseButton.MiddleButton,
            Qt.MouseButton.LeftButton,
        }:
            self._panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.remember_viewport()
            event.accept()
            return
        if self._connection_port is not None and event.button() == Qt.MouseButton.LeftButton:
            source = self._connection_port
            target = self.graph_scene.port_at(self.mapToScene(event.position().toPoint()))
            self.cancel_connection()
            if target is not None:
                self.graph_scene.connect_ports(source, target)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: Any) -> None:
        if event.key() in {Qt.Key.Key_Delete, Qt.Key.Key_Backspace}:
            self.graph_scene.remove_selected()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selection()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Paste):
            self.paste_selection()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.SelectAll):
            for item in self.graph_scene.node_items.values():
                item.setSelected(True)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Home:
            self.fit_graph()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and self._connection_port is not None:
            self.cancel_connection()
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event: Any) -> None:
        scene_position = self.mapToScene(event.pos())
        item = self.itemAt(event.pos())
        menu = QMenu(self)
        add_menu = menu.addMenu("Добавить узел")
        categories: dict[str, QMenu] = {}
        for spec in NODE_SPECS:
            if spec.type_name == "start":
                continue
            submenu = categories.get(spec.category)
            if submenu is None:
                submenu = add_menu.addMenu(spec.category)
                categories[spec.category] = submenu
            action = submenu.addAction(spec.title)
            action.setData(spec.type_name)
        if isinstance(item, (NodeItem, EdgeItem, PortItem)):
            menu.addSeparator()
            delete_action = menu.addAction("Удалить")
            delete_action.setShortcut(QKeySequence.StandardKey.Delete)
        else:
            delete_action = None
        action = menu.exec(event.globalPos())
        if action is None:
            return
        type_name = action.data()
        if isinstance(type_name, str):
            added = self.graph_scene.add_node(type_name, scene_position)
            self.node_added.emit(added.node.id)
        elif action is delete_action:
            if isinstance(item, PortItem):
                item = item.node_item
            if isinstance(item, QGraphicsItem):
                self.graph_scene.clearSelection()
                item.setSelected(True)
            self.graph_scene.remove_selected()

    def dragEnterEvent(self, event: Any) -> None:
        if event.mimeData().hasFormat(NODE_MIME_TYPE):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: Any) -> None:
        if event.mimeData().hasFormat(NODE_MIME_TYPE):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: Any) -> None:
        if event.mimeData().hasFormat(NODE_MIME_TYPE):
            type_name = bytes(event.mimeData().data(NODE_MIME_TYPE)).decode("utf-8")
            if any(spec.type_name == type_name for spec in NODE_SPECS):
                item = self.graph_scene.add_node(
                    type_name,
                    self.mapToScene(event.position().toPoint()),
                )
                self.node_added.emit(item.node.id)
                event.acceptProposedAction()
                return
        super().dropEvent(event)

    def copy_selection(self) -> None:
        selected = [
            item.node
            for item in self.graph_scene.selectedItems()
            if isinstance(item, NodeItem) and item.node.type != "start"
        ]
        if not selected:
            return
        ids = {node.id for node in selected}
        links = [
            link
            for link in self.graph_scene.document.links
            if link.from_node in ids and link.to_node in ids
        ]
        payload = {
            "format": CLIPBOARD_FORMAT,
            "nodes": [
                {
                    "id": node.id,
                    "type": node.type,
                    "x": node.x,
                    "y": node.y,
                    "params": node.params,
                }
                for node in selected
            ],
            "links": [
                {
                    "from_node": link.from_node,
                    "from_port": link.from_port,
                    "to_node": link.to_node,
                    "to_port": link.to_port,
                }
                for link in links
            ],
        }
        mime = QMimeData()
        mime.setData(NODE_MIME_TYPE + "+json", QByteArray(json.dumps(payload).encode("utf-8")))
        QApplication.clipboard().setMimeData(mime)
        self._paste_offset = 0

    def paste_selection(self) -> None:
        mime = QApplication.clipboard().mimeData()
        clipboard_type = NODE_MIME_TYPE + "+json"
        if not mime.hasFormat(clipboard_type):
            return
        try:
            payload = json.loads(bytes(mime.data(clipboard_type)).decode("utf-8"))
            if payload.get("format") != CLIPBOARD_FORMAT:
                return
            raw_nodes = payload["nodes"]
            raw_links = payload["links"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return
        self._paste_offset += 32
        mapping: dict[str, GraphNode] = {}
        self.graph_scene.clearSelection()
        for raw in raw_nodes:
            try:
                node = self.graph_scene.document.add_node(
                    str(raw["type"]),
                    float(raw["x"]) + self._paste_offset,
                    float(raw["y"]) + self._paste_offset,
                    dict(raw.get("params", {})),
                )
            except (KeyError, TypeError, ValueError, GraphError):
                continue
            mapping[str(raw["id"])] = node
            item = self.graph_scene._add_node_item(node)
            item.setSelected(True)
        for raw in raw_links:
            source = mapping.get(str(raw.get("from_node")))
            target = mapping.get(str(raw.get("to_node")))
            if source is None or target is None:
                continue
            link = GraphLink(
                source.id,
                str(raw.get("from_port")),
                target.id,
                str(raw.get("to_port")),
            )
            try:
                self.graph_scene.document.links.append(link)
                self.graph_scene._add_edge_item(link)
            except (KeyError, GraphError):
                if link in self.graph_scene.document.links:
                    self.graph_scene.document.links.remove(link)
        if mapping:
            self.graph_scene.mark_changed()

    def fit_graph(self) -> None:
        nodes = list(self.graph_scene.node_items.values())
        if not nodes:
            return
        bounds = nodes[0].sceneBoundingRect()
        for node in nodes[1:]:
            bounds = bounds.united(node.sceneBoundingRect())
        self.fitInView(bounds.adjusted(-80, -80, 80, 80), Qt.AspectRatioMode.KeepAspectRatio)
        if self.transform().m11() > 1.6:
            scale = 1.6 / self.transform().m11()
            self.scale(scale, scale)
        self.remember_viewport()


class NodePalette(QTreeWidget):
    node_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setIndentation(14)
        self.setRootIsDecorated(True)
        self.setAnimated(True)
        self.setDragEnabled(True)
        self.setMinimumWidth(205)
        self.itemDoubleClicked.connect(self._double_clicked)
        self._rebuild("")

    def _rebuild(self, query: str) -> None:
        self.clear()
        categories: dict[str, list[NodeSpec]] = defaultdict(list)
        needle = query.casefold().strip()
        for spec in NODE_SPECS:
            if spec.type_name == "start":
                continue
            haystack = f"{spec.title} {spec.category} {spec.type_name}".casefold()
            if needle and needle not in haystack:
                continue
            categories[spec.category].append(spec)
        for category, specs in categories.items():
            parent = QTreeWidgetItem([category])
            parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
            parent.setExpanded(True)
            self.addTopLevelItem(parent)
            for spec in specs:
                child = QTreeWidgetItem([spec.title])
                child.setData(0, Qt.ItemDataRole.UserRole, spec.type_name)
                child.setForeground(0, QBrush(QColor(spec.color).lighter(145)))
                parent.addChild(child)

    def set_filter(self, query: str) -> None:
        self._rebuild(query)

    def _double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        del column
        type_name = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(type_name, str):
            self.node_requested.emit(type_name)

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        del supported_actions
        item = self.currentItem()
        if item is None:
            return
        type_name = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(type_name, str):
            return
        mime = QMimeData()
        mime.setData(NODE_MIME_TYPE, QByteArray(type_name.encode("utf-8")))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


class PathEditor(QWidget):
    value_changed = Signal(str)

    def __init__(self, value: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.line_edit = QLineEdit(value)
        browse = QToolButton()
        browse.setText("…")
        browse.setToolTip("Выбрать файл")
        layout.addWidget(self.line_edit, 1)
        layout.addWidget(browse)
        self.line_edit.textChanged.connect(self.value_changed)
        browse.clicked.connect(self._browse)

    def _browse(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Выберите изображение",
            self.line_edit.text(),
            "Изображения (*.png *.jpg *.jpeg *.bmp);;Все файлы (*)",
        )
        if path:
            self.line_edit.setText(path)


class PropertyInspector(QScrollArea):
    parameter_changed = Signal(str, str, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setMinimumWidth(255)
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(14, 14, 14, 14)
        self._layout.setSpacing(12)
        self.setWidget(self._container)
        self._node: GraphNode | None = None
        self._document: GraphDocument | None = None
        self.show_empty()

    def _clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def show_empty(self) -> None:
        self._node = None
        self._document = None
        self._clear()
        title = QLabel("Свойства")
        title.setObjectName("InspectorTitle")
        help_text = QLabel("Выберите узел, чтобы изменить его параметры.")
        help_text.setWordWrap(True)
        help_text.setObjectName("MutedText")
        self._layout.addWidget(title)
        self._layout.addWidget(help_text)
        self._layout.addStretch(1)

    def set_node(self, node: GraphNode, document: GraphDocument) -> None:
        self._node = node
        self._document = document
        self._clear()
        title = QLabel(node.spec.title)
        title.setObjectName("InspectorTitle")
        category = QLabel(node.spec.category)
        category.setStyleSheet(f"color: {node.spec.color};")
        self._layout.addWidget(title)
        self._layout.addWidget(category)
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        self._layout.addWidget(separator)
        form_container = QWidget()
        form = QFormLayout(form_container)
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        linked = {link.to_port for link in document.incoming(node.id)}
        for field_spec in node.spec.fields:
            editor = self._field_editor(node, field_spec)
            if field_spec.name in linked:
                editor.setEnabled(False)
                editor.setToolTip("Значение приходит по связи")
            form.addRow(field_spec.label, editor)
        self._layout.addWidget(form_container)
        if not node.spec.fields:
            empty = QLabel("У этого узла нет параметров.")
            empty.setObjectName("MutedText")
            empty.setWordWrap(True)
            self._layout.addWidget(empty)
        info = QLabel(f"ID: {node.id[:10]}")
        info.setObjectName("MutedText")
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._layout.addStretch(1)
        self._layout.addWidget(info)

    def _field_editor(self, node: GraphNode, field_spec: FieldSpec) -> QWidget:
        value = node.value(field_spec.name)
        if field_spec.value_type == "choice":
            editor = QComboBox()
            editor.addItems(field_spec.choices)
            index = editor.findText(str(value))
            editor.setCurrentIndex(max(0, index))
            editor.currentTextChanged.connect(
                lambda current, name=field_spec.name: self._emit(name, current)
            )
            return editor
        if field_spec.value_type == "integer":
            editor = QSpinBox()
            editor.setRange(-2_000_000_000, 2_000_000_000)
            editor.setValue(int(value))
            editor.valueChanged.connect(
                lambda current, name=field_spec.name: self._emit(name, current)
            )
            return editor
        if field_spec.value_type == "number":
            editor = QDoubleSpinBox()
            editor.setDecimals(6)
            editor.setRange(-1_000_000_000, 1_000_000_000)
            editor.setSingleStep(0.1)
            editor.setValue(float(value))
            editor.valueChanged.connect(
                lambda current, name=field_spec.name: self._emit(name, current)
            )
            return editor
        if field_spec.value_type == "path":
            editor = PathEditor(str(value))
            editor.value_changed.connect(
                lambda current, name=field_spec.name: self._emit(name, current)
            )
            return editor
        editor = QLineEdit(str(value))
        editor.textChanged.connect(
            lambda current, name=field_spec.name: self._emit(name, current)
        )
        return editor

    def _emit(self, field_name: str, value: Any) -> None:
        if self._node is not None:
            self.parameter_changed.emit(self._node.id, field_name, value)


class GraphEditor(QWidget):
    document_changed = Signal()
    script_generated = Signal(str)
    import_script_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.graph_path: Path | None = None
        self.scene = GraphScene(self)
        self.view = GraphView(self.scene, self)
        self.palette = NodePalette(self)
        self.inspector = PropertyInspector(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск узла…")
        self.status = QLabel()
        self.status.setObjectName("GraphStatus")
        self.status.setWordWrap(True)
        self._dirty = False
        self._setting_document = False
        self._history: list[GraphDocument] = []
        self._history_index = -1
        self._history_timer = QTimer(self)
        self._history_timer.setSingleShot(True)
        self._history_timer.setInterval(180)
        self._history_timer.timeout.connect(self._commit_history)
        self._build_ui()
        self._connect_signals()
        self.set_document(new_graph(), fit=True)

    @property
    def document(self) -> GraphDocument:
        return self.scene.document

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.toolbar = QToolBar()
        self.toolbar.setMovable(False)
        self.toolbar.setIconSize(QSize(16, 16))
        actions = (
            ("Новый", self.new_document, "Ctrl+N"),
            ("Открыть", self.open_document, "Ctrl+O"),
            ("Сохранить", self.save_document, "Ctrl+S"),
            ("Отменить", self.undo, "Ctrl+Z"),
            ("Повторить", self.redo, "Ctrl+Y"),
            ("Из кода", self.import_script_requested.emit, "Ctrl+Shift+G"),
            ("В код", self.emit_script, "Ctrl+Shift+C"),
            ("Показать всё", self.view.fit_graph, "Home"),
        )
        for text, callback, shortcut in actions:
            action = QAction(text, self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(
                lambda _checked=False, target=callback: target()
            )
            self.toolbar.addAction(action)
        root.addWidget(self.toolbar)

        palette_panel = QWidget()
        palette_layout = QVBoxLayout(palette_panel)
        palette_layout.setContentsMargins(8, 8, 8, 8)
        palette_layout.addWidget(QLabel("Узлы"))
        palette_layout.addWidget(self.search)
        palette_layout.addWidget(self.palette, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(palette_panel)
        splitter.addWidget(self.view)
        splitter.addWidget(self.inspector)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([215, 900, 270])
        root.addWidget(splitter, 1)

        status_frame = QFrame()
        status_layout = QHBoxLayout(status_frame)
        status_layout.setContentsMargins(10, 5, 10, 5)
        status_layout.addWidget(self.status, 1)
        hint = QLabel("Колесо — масштаб · MMB/Alt+ЛКМ — перемещение · Delete — удалить")
        hint.setObjectName("MutedText")
        status_layout.addWidget(hint)
        root.addWidget(status_frame)

    def _connect_signals(self) -> None:
        self.search.textChanged.connect(self.palette.set_filter)
        self.palette.node_requested.connect(self._add_at_center)
        self.scene.document_changed.connect(self._document_changed)
        self.scene.selectionChanged.connect(self._selection_changed)
        self.scene.connection_rejected.connect(self._show_rejection)
        self.view.node_added.connect(self._select_node)
        self.inspector.parameter_changed.connect(self._parameter_changed)

    def set_document(self, document: GraphDocument, fit: bool = False) -> None:
        self._history_timer.stop()
        self._setting_document = True
        try:
            self.scene.set_document(document)
            self.view.restore_viewport()
            if fit:
                self.view.fit_graph()
            self._selection_changed()
            self._validate()
        finally:
            self._setting_document = False
        self._dirty = False
        self._history = [copy.deepcopy(document)]
        self._history_index = 0

    def load_source(self, source: str) -> None:
        self.set_document(script_to_graph(source), fit=True)
        self.graph_path = None
        self._dirty = True
        self.document_changed.emit()

    def to_source(self) -> str:
        self.view.remember_viewport()
        return graph_to_script(self.document)

    def new_document(self) -> None:
        if not self._confirm_replace():
            return
        self.graph_path = None
        self.set_document(new_graph(), fit=True)

    def open_document(self) -> None:
        if not self._confirm_replace():
            return
        path, _selected = QFileDialog.getOpenFileName(
            self,
            "Открыть граф MacroPilot",
            str(self.graph_path.parent if self.graph_path else Path.cwd()),
            "Граф MacroPilot (*.mpgraph *.json);;Все файлы (*)",
        )
        if not path:
            return
        try:
            document = load_graph(path)
        except GraphError as exc:
            QMessageBox.critical(self, "Не удалось открыть граф", str(exc))
            return
        self.graph_path = Path(path)
        self.set_document(document)

    def save_document(self) -> bool:
        path = self.graph_path
        if path is None:
            selected, _filter = QFileDialog.getSaveFileName(
                self,
                "Сохранить граф MacroPilot",
                str(Path.cwd() / "macro.mpgraph"),
                "Граф MacroPilot (*.mpgraph);;JSON (*.json)",
            )
            if not selected:
                return False
            path = Path(selected)
            if path.suffix.lower() not in {".mpgraph", ".json"}:
                path = path.with_suffix(".mpgraph")
        self.view.remember_viewport()
        self._history_timer.stop()
        self._commit_history()
        try:
            save_graph(path, self.document)
        except (GraphError, OSError) as exc:
            QMessageBox.critical(self, "Не удалось сохранить граф", str(exc))
            return False
        self.graph_path = path
        self._dirty = False
        self.status.setText(f"Сохранено: {path.name}")
        self.status.setStyleSheet("color: #86d58b;")
        return True

    def emit_script(self) -> None:
        try:
            source = self.to_source()
        except GraphError as exc:
            QMessageBox.warning(self, "Граф пока не готов", str(exc))
            return
        self.script_generated.emit(source)

    def _confirm_replace(self) -> bool:
        if not self._dirty:
            return True
        answer = QMessageBox.question(
            self,
            "Заменить граф?",
            "Несохранённые изменения графа будут потеряны. Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _add_at_center(self, type_name: str) -> None:
        center = self.view.mapToScene(self.view.viewport().rect().center())
        item = self.scene.add_node(type_name, center - QPointF(NODE_WIDTH / 2, 40))
        self._select_node(item.node.id)

    def _select_node(self, node_id: str) -> None:
        item = self.scene.node_items.get(node_id)
        if item is None:
            return
        self.scene.clearSelection()
        item.setSelected(True)
        self._selection_changed()

    def _selection_changed(self) -> None:
        selected = [item for item in self.scene.selectedItems() if isinstance(item, NodeItem)]
        if len(selected) == 1:
            self.inspector.set_node(selected[0].node, self.document)
        else:
            self.inspector.show_empty()

    def _parameter_changed(self, node_id: str, field_name: str, value: Any) -> None:
        node = self.document.node_map().get(node_id)
        if node is None:
            return
        node.params[field_name] = value
        item = self.scene.node_items.get(node_id)
        if item is not None:
            item.update()
        self.scene.mark_changed()

    def _document_changed(self) -> None:
        if self._setting_document:
            return
        self._dirty = True
        self._history_timer.start()
        self._validate()
        self.document_changed.emit()

    def _commit_history(self) -> None:
        if self._setting_document:
            return
        snapshot = copy.deepcopy(self.document)
        if self._history_index >= 0 and snapshot == self._history[self._history_index]:
            return
        del self._history[self._history_index + 1 :]
        self._history.append(snapshot)
        if len(self._history) > 100:
            self._history.pop(0)
        self._history_index = len(self._history) - 1

    def _apply_history(self) -> None:
        if not 0 <= self._history_index < len(self._history):
            return
        self._setting_document = True
        try:
            self.scene.set_document(copy.deepcopy(self._history[self._history_index]))
            self.view.restore_viewport()
            self._selection_changed()
        finally:
            self._setting_document = False
        self._dirty = True
        self._validate()
        self.document_changed.emit()

    def undo(self) -> None:
        if self._history_timer.isActive():
            self._history_timer.stop()
            self._commit_history()
        if self._history_index <= 0:
            return
        self._history_index -= 1
        self._apply_history()

    def redo(self) -> None:
        if self._history_timer.isActive():
            self._history_timer.stop()
            self._commit_history()
        if self._history_index + 1 >= len(self._history):
            return
        self._history_index += 1
        self._apply_history()

    def _validate(self) -> None:
        try:
            self.document.validate()
            graph_to_script(self.document)
        except GraphError as exc:
            self.status.setText(str(exc))
            self.status.setStyleSheet("color: #ef9b75;")
        else:
            self.status.setText(
                f"Граф готов · узлов: {len(self.document.nodes)} · связей: {len(self.document.links)}"
            )
            self.status.setStyleSheet("color: #86d58b;")

    def _show_rejection(self, reason: str) -> None:
        self.status.setText(reason)
        self.status.setStyleSheet("color: #ef9b75;")

    def set_active_node(self, node_id: str | None) -> None:
        self.scene.set_active_node(node_id)

    def set_enabled(self, enabled: bool) -> None:
        self.toolbar.setEnabled(enabled)
        self.search.setEnabled(enabled)
        self.view.setEnabled(enabled)
        self.palette.setEnabled(enabled)
        self.inspector.setEnabled(enabled)


GRAPH_STYLE_SHEET = """
QWidget {
    color: #e7ebf1;
    background: #20252c;
    font-family: "Segoe UI";
    font-size: 10pt;
}
QToolBar {
    background: #252b33;
    border: 0;
    border-bottom: 1px solid #11151a;
    spacing: 4px;
    padding: 5px;
}
QToolButton, QPushButton {
    background: #303741;
    border: 1px solid #414a57;
    border-radius: 5px;
    padding: 5px 9px;
}
QToolButton:hover, QPushButton:hover { background: #3a4350; }
QToolButton:pressed, QPushButton:pressed { background: #465261; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #171b21;
    border: 1px solid #3b4450;
    border-radius: 4px;
    padding: 5px;
    selection-background-color: #4a79b8;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border-color: #5e8fcb;
}
QTreeWidget {
    background: #191e24;
    border: 1px solid #303741;
    border-radius: 5px;
    outline: 0;
}
QTreeWidget::item { padding: 4px; }
QTreeWidget::item:selected { background: #35465c; }
QTreeWidget::item:hover { background: #2b333e; }
QSplitter::handle { background: #11151a; width: 2px; }
QScrollArea { border: 0; }
QLabel#InspectorTitle { font-size: 14pt; font-weight: 600; }
QLabel#MutedText { color: #8f99a7; }
QLabel#GraphStatus { font-size: 9pt; }
QMenu { background: #252b33; border: 1px solid #414a57; }
QMenu::item { padding: 6px 24px; }
QMenu::item:selected { background: #405674; }
QToolTip { color: #f4f6f8; background: #161a20; border: 1px solid #596574; }
"""
